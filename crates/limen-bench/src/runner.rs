//! The pilot runner: drive N agents through one coordination policy over one [`PilotTask`],
//! score the result with a [`Executor`], and emit a [`PilotRun`] record.
//!
//! The decisive difference between the arms is the **read timing** (mirroring [`crate::arm`]):
//!
//! - `Naive` — every agent reads the *initial* (stale) content of its file and writes back,
//!   last-writer-wins. Two agents on one file lose all but the last edit.
//! - `Limen` — agents serialize on the file via an advisory lease from the real
//!   [`limen::store::Store`]; each reads the *current* content (post prior writes) before
//!   editing, so contributions compose, and every change is witnessed for attribution.
//!
//! On disjoint files the two arms are identical — the fairness control.

use crate::agent::ModelAgent;
use crate::exec::{materialize, Executor};
use crate::model::{CompletionParams, ModelClient};
use crate::pilot::{PilotSubtask, PilotTask};
use anyhow::Result;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;

/// The coordination policy under test.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Coordination {
    Naive,
    Limen,
}

impl Coordination {
    fn label(self) -> &'static str {
        match self {
            Coordination::Naive => "naive",
            Coordination::Limen => "limen",
        }
    }
}

/// Who produces each file edit.
pub enum PilotAgent<'a> {
    /// Deterministic, no-network agent: appends a per-label marker to the current content.
    /// It does not solve the task (so `test_cmd` fails), but it exercises the coordination
    /// plumbing on real files — naive loses a marker, Limen composes both.
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

    /// Produce the complete new content of the subtask's file from `current`.
    async fn edit(&self, label: &str, subtask: &PilotSubtask, current: &str) -> Result<String> {
        match self {
            PilotAgent::Reference => Ok(format!("{current}\n# edit by {label}\n")),
            PilotAgent::Model {
                client,
                model,
                params,
            } => {
                let agent = ModelAgent {
                    label: label.to_string(),
                    model: model.clone(),
                    client,
                    params: params.clone(),
                };
                agent
                    .edit_file(&subtask.prompt, &subtask.region, current)
                    .await
            }
        }
    }
}

/// One pilot run's record (JSONL-serializable). `files` is the final repo content, kept for
/// provenance and offline inspection (toy tasks are small).
#[derive(Clone, Debug, Serialize)]
pub struct PilotRun {
    pub task_id: String,
    pub coupling: String,
    pub coordination: String,
    pub model: String,
    pub n: usize,
    pub seed: u64,
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

    match coord {
        Coordination::Naive => {
            for (i, sub) in task.subtasks.iter().enumerate() {
                let stale = initial.get(&sub.region).cloned().unwrap_or_default();
                let next = agent.edit(&labels[i], sub, &stale).await?;
                let abs = root.join(&sub.region);
                if let Some(parent) = abs.parent() {
                    std::fs::create_dir_all(parent)?;
                }
                std::fs::write(&abs, next)?;
            }
        }
        Coordination::Limen => {
            let store = Store::open(&root.join(".limen/state.db")).await?;
            for (i, sub) in task.subtasks.iter().enumerate() {
                let abs = root.join(&sub.region);
                let abs_s = abs.to_string_lossy().to_string();
                let lease = store
                    .acquire_lease(&abs_s, Intent::Write, &labels[i], DEFAULT_LEASE_TTL_MS)
                    .await?;
                let fresh = std::fs::read_to_string(&abs).unwrap_or_default();
                let next = agent.edit(&labels[i], sub, &fresh).await?;
                store
                    .record_write(&lease.id, &abs_s, next.as_bytes())
                    .await?;
                store.release_lease(&lease.id).await?;
            }
        }
    }

    let outcome = exec.run(root, &task.test_cmd).await?;
    let mut files = BTreeMap::new();
    for (rel, _) in &task.initial {
        files.insert(
            rel.clone(),
            std::fs::read_to_string(root.join(rel)).unwrap_or_default(),
        );
    }

    Ok(PilotRun {
        task_id: task.id.clone(),
        coupling: format!("{:?}", task.coupling),
        coordination: coord.label().to_string(),
        model: agent.model_name(),
        n: task.n(),
        seed,
        passed: outcome.passed,
        exit_code: outcome.exit_code,
        stderr_tail: tail(&outcome.stderr, 400),
        files,
    })
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
    use crate::pilot::{py_disjoint_independent, py_shared_region_merge};

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

    // Fairness control: on disjoint files, both arms keep both edits.
    #[tokio::test]
    async fn disjoint_is_unaffected_by_coordination() {
        let task = py_disjoint_independent();
        let exec = Executor::Local;
        for coord in [Coordination::Naive, Coordination::Limen] {
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
        }
    }

    #[test]
    fn jsonl_round_trips() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("runs.jsonl");
        let run = PilotRun {
            task_id: "t".into(),
            coupling: "SharedRegion".into(),
            coordination: "limen".into(),
            model: "reference".into(),
            n: 2,
            seed: 1,
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
        assert_eq!(parsed["coordination"], "limen");
    }
}
