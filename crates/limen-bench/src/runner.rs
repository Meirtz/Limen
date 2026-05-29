//! The pilot runner: drive N agents through one coordination policy over one [`PilotTask`],
//! score the result with a [`Executor`], and emit a [`PilotRun`] record.
//!
//! Three policies, increasing in coordination:
//!
//! - `Naive` — every agent reads the *initial* (stale) content of its file and writes back,
//!   last-writer-wins. Two agents on one file lose all but the last edit.
//! - `Limen` — agents serialize on their file via an advisory write lease from the real
//!   [`limen::store::Store`]; each reads the *current* content before editing, so same-file
//!   contributions compose, and every change is witnessed.
//! - `LimenDeps` — Limen plus a **write×read advisory round**: each subtask declares the files
//!   it *reads*; after the writes, any reader whose dependency was changed by another agent is
//!   shown the new dependency and reconciles its own file. This recovers cross-region write skew
//!   (a renamed symbol whose caller lives in another file) that per-file leases cannot —
//!   advisory *information*, not enforcement or auto-merge.
//!
//! On disjoint files with no declared dependencies, all three arms are identical — the control.

use crate::agent::ModelAgent;
use crate::exec::{materialize, Executor};
use crate::model::{CompletionParams, ModelClient};
use crate::pilot::{PilotSubtask, PilotTask};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// The coordination policy under test.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Coordination {
    Naive,
    /// Limen's witnessed mediator but with a *stale* read (no fresh-read arbitration). It records
    /// the witness yet still loses the shared-region edit — isolating the wrapper from the
    /// arbitration (a kill-criterion guard: if Limen ≈ placebo, the win is a wrapper artifact).
    LimenPlacebo,
    Limen,
    LimenDeps,
}

impl Coordination {
    pub fn label(self) -> &'static str {
        match self {
            Coordination::Naive => "naive",
            Coordination::LimenPlacebo => "limen-placebo",
            Coordination::Limen => "limen",
            Coordination::LimenDeps => "limen-deps",
        }
    }

    /// Whether the witnessed arms read the *current* file (fresh) or the initial snapshot (stale).
    fn reads_fresh(self) -> bool {
        !matches!(self, Coordination::LimenPlacebo)
    }
}

/// Who produces each file edit.
pub enum PilotAgent<'a> {
    /// Deterministic, no-network agent: appends a per-label marker on edit and a reconciliation
    /// marker on reconcile. It does not solve the task (so `test_cmd` fails), but it exercises
    /// the coordination plumbing on real files.
    Reference,
    /// A real coding agent over the inference hub.
    Model {
        client: &'a ModelClient,
        model: String,
        params: CompletionParams,
    },
}

impl PilotAgent<'_> {
    /// The model name recorded for this run.
    pub fn model_name(&self) -> String {
        match self {
            PilotAgent::Reference => "reference".to_string(),
            PilotAgent::Model { model, .. } => model.clone(),
        }
    }

    /// Sampling settings recorded for provenance: `(temperature, max_tokens)`. The reference
    /// agent does no sampling, so both are zero.
    pub fn sampling(&self) -> (f32, u32) {
        match self {
            PilotAgent::Reference => (0.0, 0),
            PilotAgent::Model { params, .. } => (params.temperature, params.max_tokens),
        }
    }

    fn model_agent(&self, label: &str) -> Option<ModelAgent<'_>> {
        match self {
            PilotAgent::Reference => None,
            PilotAgent::Model {
                client,
                model,
                params,
            } => Some(ModelAgent {
                label: label.to_string(),
                model: model.clone(),
                client,
                params: params.clone(),
            }),
        }
    }

    /// Produce the complete new content of the subtask's file from `current`.
    async fn edit(&self, label: &str, subtask: &PilotSubtask, current: &str) -> Result<String> {
        match self.model_agent(label) {
            None => Ok(format!("{current}\n# edit by {label}\n")),
            Some(agent) => {
                agent
                    .edit_file(&subtask.prompt, &subtask.region, current)
                    .await
            }
        }
    }

    /// Reconcile the subtask's file after a dependency changed.
    async fn reconcile(
        &self,
        label: &str,
        subtask: &PilotSubtask,
        current: &str,
        dep_path: &str,
        dep_content: &str,
    ) -> Result<String> {
        match self.model_agent(label) {
            None => Ok(format!(
                "{current}\n# reconciled after {dep_path} changed\n"
            )),
            Some(agent) => {
                agent
                    .reconcile_file(
                        &subtask.prompt,
                        &subtask.region,
                        current,
                        dep_path,
                        dep_content,
                    )
                    .await
            }
        }
    }
}

/// One pilot run's record (JSONL-serializable). `files` is the final repo content, kept for
/// provenance and offline inspection (toy tasks are small).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PilotRun {
    pub task_id: String,
    pub coupling: String,
    pub coordination: String,
    pub model: String,
    pub n: usize,
    pub seed: u64,
    #[serde(default)]
    pub temperature: f32,
    #[serde(default)]
    pub max_tokens: u32,
    /// Content hash of the task (seed repo + subtasks + test command) — pins exactly what was run.
    #[serde(default)]
    pub task_hash: String,
    pub passed: bool,
    pub exit_code: Option<i32>,
    pub stderr_tail: String,
    pub files: BTreeMap<String, String>,
}

fn labels(n: usize) -> Vec<String> {
    (0..n)
        .map(|i| format!("agent-{}", (b'A' + i as u8) as char))
        .collect()
}

/// A content hash of the task — seed repo, each subtask's region/prompt/reads, and the test
/// command — so a run record pins exactly which task instance produced it.
fn task_hash(task: &PilotTask) -> String {
    let mut s = String::new();
    s.push_str(&task.id);
    s.push('\n');
    for (p, c) in &task.initial {
        s.push_str(p);
        s.push('\u{0}');
        s.push_str(c);
        s.push('\n');
    }
    for sub in &task.subtasks {
        s.push_str(&sub.region);
        s.push('\u{0}');
        s.push_str(&sub.prompt);
        s.push('\u{0}');
        s.push_str(&sub.reads.join(","));
        s.push('\n');
    }
    s.push_str(&task.test_cmd.join(" "));
    limen::resource::hex_sha256(s.as_bytes())
}

/// Indices of subtasks that read a file in `changed` which is *not* their own write region —
/// i.e. readers whose cross-file dependency was altered by another agent (the write×read
/// coupling the dependency-aware arm must surface). Pure and unit-tested.
pub fn coupled_readers(subtasks: &[PilotSubtask], changed: &BTreeSet<String>) -> Vec<usize> {
    subtasks
        .iter()
        .enumerate()
        .filter(|(_, s)| {
            s.reads
                .iter()
                .any(|d| d != &s.region && changed.contains(d))
        })
        .map(|(i, _)| i)
        .collect()
}

/// Run one (task, agent, coordination) instance and score it.
pub async fn run_pilot(
    task: &PilotTask,
    agent: &PilotAgent<'_>,
    coord: Coordination,
    exec: &Executor,
    seed: u64,
) -> Result<PilotRun> {
    use limen::store::{Intent, Store, DEFAULT_LEASE_TTL_MS};

    let work = tempfile::tempdir()?;
    let root = work.path();
    materialize(
        root,
        task.initial.iter().map(|(p, c)| (p.as_str(), c.as_str())),
    )?;
    let initial: BTreeMap<String, String> = task.initial.iter().cloned().collect();
    let labels = labels(task.n());
    let read_file = |rel: &str| std::fs::read_to_string(root.join(rel)).unwrap_or_default();

    match coord {
        Coordination::Naive => {
            for (i, sub) in task.subtasks.iter().enumerate() {
                let stale = initial.get(&sub.region).cloned().unwrap_or_default();
                let next = agent.edit(&labels[i], sub, &stale).await?;
                write_file(root, &sub.region, &next)?;
            }
        }
        Coordination::Limen | Coordination::LimenDeps | Coordination::LimenPlacebo => {
            let store = Store::open(&root.join(".limen/state.db")).await?;
            let fresh_read = coord.reads_fresh();

            // Write phase: serialize on the file via a witnessed lease. Fresh read composes
            // (Limen); stale read witnesses but still loses the edit (placebo).
            for (i, sub) in task.subtasks.iter().enumerate() {
                let abs_s = root.join(&sub.region).to_string_lossy().to_string();
                let lease = store
                    .acquire_lease(&abs_s, Intent::Write, &labels[i], DEFAULT_LEASE_TTL_MS)
                    .await?;
                let current = if fresh_read {
                    read_file(&sub.region)
                } else {
                    initial.get(&sub.region).cloned().unwrap_or_default()
                };
                let next = agent.edit(&labels[i], sub, &current).await?;
                store
                    .record_write(&lease.id, &abs_s, next.as_bytes())
                    .await?;
                store.release_lease(&lease.id).await?;
            }

            // Advisory write×read rounds: reconcile readers whose dependency changed, then
            // propagate to readers of *those* reconciled files, until a fixpoint (capped). One hop
            // fixes independent interface pairs; chained dependencies (A→B→C) need more.
            if coord == Coordination::LimenDeps {
                const MAX_ROUNDS: usize = 4;
                // Round-0 frontier: files the write phase changed vs the seed.
                let mut frontier: BTreeSet<String> = task
                    .initial
                    .iter()
                    .filter(|(rel, init)| &read_file(rel) != init)
                    .map(|(rel, _)| rel.clone())
                    .collect();

                for _round in 0..MAX_ROUNDS {
                    let readers = coupled_readers(&task.subtasks, &frontier);
                    if readers.is_empty() {
                        break;
                    }
                    let mut next: BTreeSet<String> = BTreeSet::new();
                    for i in readers {
                        let sub = &task.subtasks[i];
                        let Some(dep) = sub
                            .reads
                            .iter()
                            .find(|d| *d != &sub.region && frontier.contains(*d))
                        else {
                            continue;
                        };
                        let dep_content = read_file(dep);
                        let abs_s = root.join(&sub.region).to_string_lossy().to_string();
                        let before = read_file(&sub.region);
                        let lease = store
                            .acquire_lease(&abs_s, Intent::Write, &labels[i], DEFAULT_LEASE_TTL_MS)
                            .await?;
                        let after = agent
                            .reconcile(&labels[i], sub, &before, dep, &dep_content)
                            .await?;
                        store
                            .record_write(&lease.id, &abs_s, after.as_bytes())
                            .await?;
                        store.release_lease(&lease.id).await?;
                        if after != before {
                            next.insert(sub.region.clone());
                        }
                    }
                    if next.is_empty() {
                        break;
                    }
                    frontier = next;
                }
            }
        }
    }

    let outcome = exec.run(root, &task.test_cmd).await?;
    let mut files = BTreeMap::new();
    for (rel, _) in &task.initial {
        files.insert(rel.clone(), read_file(rel));
    }

    let (temperature, max_tokens) = agent.sampling();
    Ok(PilotRun {
        task_id: task.id.clone(),
        coupling: format!("{:?}", task.coupling),
        coordination: coord.label().to_string(),
        model: agent.model_name(),
        n: task.n(),
        seed,
        temperature,
        max_tokens,
        task_hash: task_hash(task),
        passed: outcome.passed,
        exit_code: outcome.exit_code,
        stderr_tail: tail(&outcome.stderr, 400),
        files,
    })
}

fn write_file(root: &Path, rel: &str, content: &str) -> std::io::Result<()> {
    let abs = root.join(rel);
    if let Some(parent) = abs.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(abs, content)
}

/// Append a run record as one JSON line.
pub fn append_jsonl(path: &Path, run: &PilotRun) -> Result<()> {
    use std::io::Write;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(f, "{}", serde_json::to_string(run)?)?;
    Ok(())
}

/// Last `n` characters of a trimmed string (UTF-8 safe), for compact error tails.
fn tail(s: &str, n: usize) -> String {
    let t = s.trim();
    let chars: Vec<char> = t.chars().collect();
    if chars.len() <= n {
        t.to_string()
    } else {
        format!("…{}", chars[chars.len() - n..].iter().collect::<String>())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pilot::{py_disjoint_independent, py_interface_break, py_shared_region_merge};

    #[test]
    fn coupled_readers_finds_cross_file_dependencies() {
        let task = py_interface_break();
        // api.py changed → the caller (reads api.py) is a coupled reader; the api author is not.
        let changed: BTreeSet<String> = ["svc/api.py".to_string()].into_iter().collect();
        assert_eq!(coupled_readers(&task.subtasks, &changed), vec![1]);
        // nothing changed → no readers
        assert!(coupled_readers(&task.subtasks, &BTreeSet::new()).is_empty());
        // a subtask reading only its own region is not "coupled" to itself
        let shared = py_shared_region_merge();
        let all_changed: BTreeSet<String> = shared.initial.iter().map(|(p, _)| p.clone()).collect();
        assert!(coupled_readers(&shared.subtasks, &all_changed).is_empty());
    }

    // The mechanism, on real files through the real Store, with a deterministic agent.
    #[tokio::test]
    async fn naive_loses_a_shared_region_edit_while_limen_composes() {
        let task = py_shared_region_merge();
        let exec = Executor::Local;
        let naive = run_pilot(&task, &PilotAgent::Reference, Coordination::Naive, &exec, 1)
            .await
            .unwrap();
        let limen = run_pilot(&task, &PilotAgent::Reference, Coordination::Limen, &exec, 1)
            .await
            .unwrap();

        let ops_naive = &naive.files["mathx/ops.py"];
        assert!(ops_naive.contains("# edit by agent-B"));
        assert!(
            !ops_naive.contains("# edit by agent-A"),
            "naive last-writer-wins should lose agent-A's edit"
        );

        let ops_limen = &limen.files["mathx/ops.py"];
        assert!(
            ops_limen.contains("# edit by agent-A") && ops_limen.contains("# edit by agent-B"),
            "limen should compose both edits: {ops_limen:?}"
        );
    }

    // Placebo: the witnessed mediator with a STALE read still loses the edit (like naive) — so the
    // Limen win is the fresh-read arbitration, not merely routing writes through the store.
    #[tokio::test]
    async fn limen_placebo_witnesses_but_still_loses_the_shared_region_edit() {
        let task = py_shared_region_merge();
        let exec = Executor::Local;
        let placebo = run_pilot(
            &task,
            &PilotAgent::Reference,
            Coordination::LimenPlacebo,
            &exec,
            1,
        )
        .await
        .unwrap();
        let ops = &placebo.files["mathx/ops.py"];
        assert!(ops.contains("# edit by agent-B"));
        assert!(
            !ops.contains("# edit by agent-A"),
            "placebo (stale read) should still lose agent-A's edit: {ops:?}"
        );
    }

    // The dependency-aware arm fires a reconciliation round on the coupled reader (and only it).
    #[tokio::test]
    async fn limen_deps_reconciles_the_coupled_reader() {
        let task = py_interface_break();
        let exec = Executor::Local;

        // Plain Limen does NOT reconcile — the caller is untouched by any advisory round.
        let limen = run_pilot(&task, &PilotAgent::Reference, Coordination::Limen, &exec, 1)
            .await
            .unwrap();
        assert!(!limen.files["svc/caller.py"].contains("# reconciled"));

        // LimenDeps reconciles the caller (reads svc/api.py, which the other agent changed)…
        let deps = run_pilot(
            &task,
            &PilotAgent::Reference,
            Coordination::LimenDeps,
            &exec,
            1,
        )
        .await
        .unwrap();
        assert!(
            deps.files["svc/caller.py"].contains("# reconciled after svc/api.py changed"),
            "the coupled reader should be reconciled: {:?}",
            deps.files["svc/caller.py"]
        );
        // …but the api author (no read deps) is not reconciled.
        assert!(!deps.files["svc/api.py"].contains("# reconciled"));
    }

    // The fixpoint loop reconciles every coupled reader in a multi-pair task and terminates.
    #[tokio::test]
    async fn limen_deps_reconciles_every_coupled_reader_in_a_mixed_task() {
        let task = crate::pilot::mixed_coupling(1, 3); // 1 shared + 3 interface pairs
        let exec = Executor::Local;
        let run = run_pilot(
            &task,
            &PilotAgent::Reference,
            Coordination::LimenDeps,
            &exec,
            1,
        )
        .await
        .unwrap();
        for i in 0..3 {
            let caller = &run.files[&format!("if{i}/caller.py")];
            assert!(
                caller.contains("# reconciled"),
                "if{i} caller not reconciled: {caller:?}"
            );
        }
        // the shared-region pair has no cross-file dependency → never reconciled
        assert!(!run.files["sh0/ops.py"].contains("# reconciled"));
    }

    #[tokio::test]
    async fn disjoint_is_unaffected_by_coordination() {
        let task = py_disjoint_independent();
        let exec = Executor::Local;
        for coord in [
            Coordination::Naive,
            Coordination::Limen,
            Coordination::LimenDeps,
        ] {
            let r = run_pilot(&task, &PilotAgent::Reference, coord, &exec, 1)
                .await
                .unwrap();
            assert!(
                r.files["pkg/a.py"].contains("# edit by agent-A"),
                "{coord:?}: a lost"
            );
            assert!(
                r.files["pkg/b.py"].contains("# edit by agent-B"),
                "{coord:?}: b lost"
            );
            // no cross-file deps → no reconciliation in any arm
            assert!(!r.files["pkg/a.py"].contains("# reconciled"));
            assert!(!r.files["pkg/b.py"].contains("# reconciled"));
        }
    }

    #[test]
    fn jsonl_round_trips() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("runs.jsonl");
        let run = PilotRun {
            task_id: "t".into(),
            coupling: "Interface".into(),
            coordination: "limen-deps".into(),
            model: "reference".into(),
            n: 2,
            seed: 1,
            temperature: 0.0,
            max_tokens: 0,
            task_hash: "deadbeef".into(),
            passed: true,
            exit_code: Some(0),
            stderr_tail: String::new(),
            files: BTreeMap::new(),
        };
        append_jsonl(&path, &run).unwrap();
        append_jsonl(&path, &run).unwrap();
        let body = std::fs::read_to_string(&path).unwrap();
        assert_eq!(body.lines().count(), 2);
        let parsed: serde_json::Value = serde_json::from_str(body.lines().next().unwrap()).unwrap();
        assert_eq!(parsed["coordination"], "limen-deps");
    }
}
