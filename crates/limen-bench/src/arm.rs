//! Experimental arms — the coordination policies under test.
//!
//! The decisive difference is the **read timing**: naive concurrency has every agent work
//! from the initial (stale) snapshot, so a later write clobbers an earlier one; coordinated
//! arms read the *current* state at write time, so contributions compose. The Limen arm
//! drives the real [`limen::store::Store`] (acquire / write / release / attribute), so this
//! exercises the shipped coordination code, not a re-implementation.

use crate::oracle::{self, Tree};
use crate::record::RunRecord;
use crate::task::Task;
use anyhow::Result;

pub trait Arm {
    fn id(&self) -> &'static str;
    fn run(&self, task: &Task) -> Result<RunRecord>;
}

fn initial_tree(task: &Task) -> Tree {
    task.initial.iter().cloned().collect()
}

fn record(arm: &str, task: &Task, tree: &Tree, attribution: Option<bool>) -> RunRecord {
    RunRecord {
        arm: arm.to_string(),
        task: task.id.clone(),
        coupling: format!("{:?}", task.coupling),
        passed: oracle::passed(task, tree),
        lost_edit_lines: oracle::lost_edit_lines(task, tree),
        build_break: oracle::build_break(task, tree),
        attribution_correct: attribution,
    }
}

/// One agent performs all subtasks sequentially, each on a fresh read — the correctness
/// ceiling and latency baseline.
pub struct Seq1;

impl Arm for Seq1 {
    fn id(&self) -> &'static str {
        "seq1"
    }

    fn run(&self, task: &Task) -> Result<RunRecord> {
        let mut tree = initial_tree(task);
        for agent in &task.agents {
            for op in &agent.ops {
                let cur = tree.get(&op.target).cloned().unwrap_or_default();
                tree.insert(op.target.clone(), op.mutation.apply(&cur));
            }
        }
        Ok(record(self.id(), task, &tree, None))
    }
}

/// N agents, no coordination: each reads the **initial** snapshot and writes its result,
/// last-writer-wins per file. Concurrent edits to one file lose all but the last.
pub struct ParNaive;

impl Arm for ParNaive {
    fn id(&self) -> &'static str {
        "par-naive"
    }

    fn run(&self, task: &Task) -> Result<RunRecord> {
        let initial = initial_tree(task);
        let mut tree = initial.clone();
        for agent in &task.agents {
            // Stale read: the agent works from the initial snapshot, blind to peers.
            let mut local = initial.clone();
            for op in &agent.ops {
                let cur = local.get(&op.target).cloned().unwrap_or_default();
                local.insert(op.target.clone(), op.mutation.apply(&cur));
            }
            // Write-back: last writer wins on every file the agent touched.
            for op in &agent.ops {
                if let Some(content) = local.get(&op.target) {
                    tree.insert(op.target.clone(), content.clone());
                }
            }
        }
        // No witness trail → no per-agent attribution (git blame names the human).
        Ok(record(self.id(), task, &tree, None))
    }
}

/// N agents coordinated by Limen: advisory leases serialize writes to a contended region,
/// and each write reads the *current* state, so contributions compose. Drives the real
/// `limen::store::Store` and scores attribution from the witness trail.
pub struct ParLimen;

impl Arm for ParLimen {
    fn id(&self) -> &'static str {
        "par-limen"
    }

    fn run(&self, task: &Task) -> Result<RunRecord> {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        rt.block_on(run_limen(task))
    }
}

async fn run_limen(task: &Task) -> Result<RunRecord> {
    use limen::store::{Intent, Store, DEFAULT_LEASE_TTL_MS};

    let work = tempfile::tempdir()?;
    let root = work.path();

    // Materialize the initial tree on disk.
    for (rel, content) in &task.initial {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&path, content)?;
    }

    let store = Store::open(&root.join(".limen/state.db")).await?;

    // Coordinated: acquire a write lease on the target, read current, mutate, write through
    // the witnessed mediator, release. Honored leases serialize contended writes losslessly.
    for agent in &task.agents {
        for op in &agent.ops {
            let abs = root.join(&op.target);
            let abs_s = abs.to_string_lossy().to_string();
            let lease = store
                .acquire_lease(&abs_s, Intent::Write, &agent.label, DEFAULT_LEASE_TTL_MS)
                .await?;
            let current = std::fs::read_to_string(&abs).unwrap_or_default();
            let next = op.mutation.apply(&current);
            store
                .record_write(&lease.id, &abs_s, next.as_bytes())
                .await?;
            store.release_lease(&lease.id).await?;
        }
    }

    // Read the final tree.
    let mut tree: Tree = Tree::new();
    for (rel, _) in &task.initial {
        let content = std::fs::read_to_string(root.join(rel)).unwrap_or_default();
        tree.insert(rel.clone(), content);
    }

    // Score attribution from the witness trail against the gold owner of each file.
    let mut attribution = Some(true);
    for (rel, gold) in &task.gold_owner {
        let abs_s = root.join(rel).to_string_lossy().to_string();
        let rows = store.attribute_path(&abs_s).await?; // most-recent first
        let ok = matches!(rows.first(), Some((_w, agent)) if agent == gold);
        if !ok {
            attribution = Some(false);
        }
    }

    Ok(record("par-limen", task, &tree, attribution))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::task::{disjoint_independent, shared_region_merge};

    #[test]
    fn shared_region_mechanism() {
        let task = shared_region_merge();
        let seq = Seq1.run(&task).unwrap();
        let naive = ParNaive.run(&task).unwrap();
        let limen = ParLimen.run(&task).unwrap();

        // Sequential = correctness ceiling: both contributions present.
        assert!(seq.passed && seq.lost_edit_lines == 0);

        // Naive concurrency loses a contribution and fails the merge (the lost update).
        assert!(naive.lost_edit_lines >= 1, "naive should lose an edit");
        assert!(!naive.passed, "naive should fail the merge");
        assert!(
            naive.attribution_correct.is_none(),
            "naive cannot attribute"
        );

        // Limen prevents the lost update, passes, and attributes from the witness.
        assert_eq!(limen.lost_edit_lines, 0, "limen should lose nothing");
        assert!(limen.passed, "limen should pass");
        assert_eq!(limen.attribution_correct, Some(true));
    }

    #[test]
    fn disjoint_no_difference() {
        // Fairness check: on disjoint work, coordination must not change the outcome.
        let task = disjoint_independent();
        let naive = ParNaive.run(&task).unwrap();
        let limen = ParLimen.run(&task).unwrap();
        assert!(naive.passed && naive.lost_edit_lines == 0);
        assert!(limen.passed && limen.lost_edit_lines == 0);
    }
}
