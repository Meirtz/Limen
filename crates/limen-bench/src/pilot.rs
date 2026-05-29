//! Pilot task model + a few toy tasks.
//!
//! The real benchmark is a separate, serious sub-project (contamination control, mutation-
//! validated tests, third-party authorship — see `docs/paper/`). These hand-written toy tasks
//! only seed the pilot: a small repo, one single-file subtask per agent, and a test command whose
//! exit code (run in a local-docker sandbox) decides pass/fail. Coupling is reused from [`task`].

use crate::task::Coupling;

/// One agent's instruction over one file (its write region), plus the files it *reads*
/// (its cross-file dependencies). `reads` is what makes interface coupling visible to the
/// coordinator: a write to a file another agent reads is a write×read coupling.
#[derive(Clone, Debug)]
pub struct PilotSubtask {
    pub prompt: String,
    pub region: String,
    pub reads: Vec<String>,
}

impl PilotSubtask {
    /// A subtask with no cross-file dependencies.
    pub fn new(prompt: impl Into<String>, region: impl Into<String>) -> Self {
        Self {
            prompt: prompt.into(),
            region: region.into(),
            reads: Vec::new(),
        }
    }

    /// Declare that this subtask depends on (reads) `path`.
    pub fn reading(mut self, path: impl Into<String>) -> Self {
        self.reads.push(path.into());
        self
    }
}

/// A pilot task: seed repo + one subtask per agent + a pass/fail test command.
#[derive(Clone, Debug)]
pub struct PilotTask {
    pub id: String,
    pub language: String,
    pub coupling: Coupling,
    /// Seed repo as (relative path, content).
    pub initial: Vec<(String, String)>,
    /// One subtask per agent (N = `subtasks.len()`).
    pub subtasks: Vec<PilotSubtask>,
    /// Command run in the sandbox after edits; exit code 0 = pass.
    pub test_cmd: Vec<String>,
}

impl PilotTask {
    /// Degree of parallelism this task induces.
    pub fn n(&self) -> usize {
        self.subtasks.len()
    }
}

fn py_test(import_and_assert: &str) -> Vec<String> {
    vec!["python".into(), "-c".into(), import_and_assert.into()]
}

/// Two agents add two functions to the **same** module — a shared-region merge. Naive
/// concurrency loses one function (the import fails); coordination composes both.
pub fn py_shared_region_merge() -> PilotTask {
    PilotTask {
        id: "py-shared-region-merge".into(),
        language: "python".into(),
        coupling: Coupling::SharedRegion,
        initial: vec![
            ("mathx/__init__.py".into(), String::new()),
            ("mathx/ops.py".into(), "\"\"\"arithmetic ops\"\"\"\n".into()),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Add a function `def add(a, b): return a + b` to this module. Keep all existing content.",
                "mathx/ops.py",
            ),
            PilotSubtask::new(
                "Add a function `def mul(a, b): return a * b` to this module. Keep all existing content.",
                "mathx/ops.py",
            ),
        ],
        test_cmd: py_test("from mathx.ops import add, mul; assert add(2, 3) == 5 and mul(2, 3) == 6; print('ok')"),
    }
}

/// Two agents edit **different** modules — disjoint work; coordination should make no difference
/// (the honest fairness check).
pub fn py_disjoint_independent() -> PilotTask {
    PilotTask {
        id: "py-disjoint-independent".into(),
        language: "python".into(),
        coupling: Coupling::Disjoint,
        initial: vec![
            ("pkg/__init__.py".into(), String::new()),
            ("pkg/a.py".into(), "\"\"\"a\"\"\"\n".into()),
            ("pkg/b.py".into(), "\"\"\"b\"\"\"\n".into()),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Add a function `def a(): return 'A'` to this module. Keep all existing content.",
                "pkg/a.py",
            ),
            PilotSubtask::new(
                "Add a function `def b(): return 'B'` to this module. Keep all existing content.",
                "pkg/b.py",
            ),
        ],
        test_cmd: py_test("from pkg.a import a; from pkg.b import b; assert a() == 'A' and b() == 'B'; print('ok')"),
    }
}

/// An `interface` task: one agent renames a symbol another file calls — a **cross-region write
/// skew**. Per-file leases don't serialize it (the files don't overlap), so it breaks under both
/// naive and plain Limen; it is recovered only when the coordinator surfaces the write×read
/// coupling to the dependent agent (`LimenDeps`). The caller subtask therefore declares it
/// *reads* `svc/api.py`.
pub fn py_interface_break() -> PilotTask {
    PilotTask {
        id: "py-interface-break".into(),
        language: "python".into(),
        coupling: Coupling::Interface,
        initial: vec![
            ("svc/__init__.py".into(), String::new()),
            (
                "svc/api.py".into(),
                "def handle(req):\n    return req\n".into(),
            ),
            (
                "svc/caller.py".into(),
                "from svc.api import handle\n\ndef run(x):\n    return handle(x)\n".into(),
            ),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Rename the function `handle` to `process` in this file (update its definition).",
                "svc/api.py",
            ),
            PilotSubtask::new(
                "Add a docstring to the `run` function. Keep the existing call as-is.",
                "svc/caller.py",
            )
            .reading("svc/api.py"),
        ],
        test_cmd: py_test("from svc.caller import run; assert run(7) == 7; print('ok')"),
    }
}

/// All toy tasks.
pub fn all() -> Vec<PilotTask> {
    vec![
        py_shared_region_merge(),
        py_disjoint_independent(),
        py_interface_break(),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn toy_tasks_are_well_formed() {
        for t in all() {
            assert!(!t.subtasks.is_empty(), "{}: needs subtasks", t.id);
            assert!(!t.test_cmd.is_empty(), "{}: needs a test command", t.id);
            assert!(!t.initial.is_empty(), "{}: needs seed files", t.id);
            let files: BTreeSet<&str> = t.initial.iter().map(|(p, _)| p.as_str()).collect();
            for s in &t.subtasks {
                assert!(!s.prompt.is_empty(), "{}: empty prompt", t.id);
                assert!(
                    files.contains(s.region.as_str()),
                    "{}: subtask region {} is not a seed file",
                    t.id,
                    s.region
                );
                for r in &s.reads {
                    assert!(
                        files.contains(r.as_str()),
                        "{}: read dependency {} is not a seed file",
                        t.id,
                        r
                    );
                }
            }
        }
    }

    #[test]
    fn only_the_interface_task_declares_a_dependency() {
        // Coupling visibility: disjoint/shared have no cross-file reads; interface does.
        let deps = |t: &PilotTask| t.subtasks.iter().any(|s| !s.reads.is_empty());
        assert!(!deps(&py_shared_region_merge()));
        assert!(!deps(&py_disjoint_independent()));
        assert!(deps(&py_interface_break()));
    }
}
