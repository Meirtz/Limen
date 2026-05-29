//! Coordination-independent metric extractors.
//!
//! Every metric here is computed from the task's intended ops and the final tree, the
//! **same way for every arm** — so the measurement ruler is identical across arms (no
//! metric is true "by construction" of the coordinator). The final tree is a
//! `path -> content` map read off disk (or built in memory) by the arm.

use crate::task::Task;
use std::collections::BTreeMap;

pub type Tree = BTreeMap<String, String>;

/// Lines that some agent intended to introduce but are absent from the final tree —
/// i.e. contributions lost to an overwriting concurrent write (the Lost Update, P4).
pub fn lost_edit_lines(task: &Task, final_tree: &Tree) -> usize {
    let mut lost = 0;
    for agent in &task.agents {
        for op in &agent.ops {
            let content = final_tree.get(&op.target).map(String::as_str).unwrap_or("");
            for intended in op.mutation.introduced() {
                let needle = intended.trim();
                if !needle.is_empty() && !content.contains(needle) {
                    lost += 1;
                }
            }
        }
    }
    lost
}

/// Did the final tree satisfy every expectation (the hidden-test analogue)?
pub fn passed(task: &Task, final_tree: &Tree) -> bool {
    task.expectations.iter().all(|(file, subs)| {
        let content = final_tree.get(file).map(String::as_str).unwrap_or("");
        subs.iter().all(|s| content.contains(s))
    })
}

/// Does the final tree contain a forbidden (stale-reference) token — the build-break /
/// write-skew analogue?
pub fn build_break(task: &Task, final_tree: &Tree) -> bool {
    task.forbidden.iter().any(|(file, subs)| {
        let content = final_tree.get(file).map(String::as_str).unwrap_or("");
        subs.iter().any(|s| content.contains(s))
    })
}
