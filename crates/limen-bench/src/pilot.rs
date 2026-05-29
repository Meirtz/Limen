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

    /// Fraction of subtasks that carry a cross-file read dependency (a write×read coupling) — an
    /// a-priori measure of how interface-coupled the task is, independent of any run. 0 means all
    /// work is same-file or disjoint; higher means more cross-file skew potential.
    pub fn coupling_fraction(&self) -> f64 {
        if self.subtasks.is_empty() {
            return 0.0;
        }
        let cross = self
            .subtasks
            .iter()
            .filter(|s| s.reads.iter().any(|r| r != &s.region))
            .count();
        cross as f64 / self.subtasks.len() as f64
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

/// A three-way (N=3) shared-region merge: three agents add three functions to the **same** module.
/// Naive last-writer-wins keeps only one (loses two); coordination composes all three. Confirms the
/// gradient is not a two-agent artifact.
pub fn py_three_way_shared() -> PilotTask {
    PilotTask {
        id: "py-three-way-shared".into(),
        language: "python".into(),
        coupling: Coupling::SharedRegion,
        initial: vec![
            ("mathx3/__init__.py".into(), String::new()),
            ("mathx3/ops.py".into(), "\"\"\"ops\"\"\"\n".into()),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Add a function `def add(a, b): return a + b` to this module. Keep all existing content.",
                "mathx3/ops.py",
            ),
            PilotSubtask::new(
                "Add a function `def mul(a, b): return a * b` to this module. Keep all existing content.",
                "mathx3/ops.py",
            ),
            PilotSubtask::new(
                "Add a function `def sub(a, b): return a - b` to this module. Keep all existing content.",
                "mathx3/ops.py",
            ),
        ],
        test_cmd: py_test(
            "from mathx3.ops import add, mul, sub; assert add(2,3)==5 and mul(2,3)==6 and sub(3,2)==1; print('ok')",
        ),
    }
}

/// An `interface` variant: the renamed function also gains a parameter, so the caller must update
/// both the name and the call site — a stricter cross-file reconciliation.
pub fn py_interface_signature_change() -> PilotTask {
    PilotTask {
        id: "py-interface-signature-change".into(),
        language: "python".into(),
        coupling: Coupling::Interface,
        initial: vec![
            ("svc2/__init__.py".into(), String::new()),
            ("svc2/api.py".into(), "def greet(name):\n    return 'hi ' + name\n".into()),
            (
                "svc2/caller.py".into(),
                "from svc2.api import greet\n\ndef run():\n    return greet('x')\n".into(),
            ),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Rename `greet` to `salute` and add a second parameter `punct` with default `'!'`, returning `'hi ' + name + punct`.",
                "svc2/api.py",
            ),
            PilotSubtask::new(
                "Add a docstring to `run`. Keep the existing call as-is.",
                "svc2/caller.py",
            )
            .reading("svc2/api.py"),
        ],
        test_cmd: py_test("from svc2.caller import run; assert run() == 'hi x!'; print('ok')"),
    }
}

/// A shared-region merge of a different shape: two agents add entries to a shared `dict` in one
/// module (a registry merge, not function append). Naive loses one entry; coordination composes.
pub fn py_registry_merge() -> PilotTask {
    PilotTask {
        id: "py-registry-merge".into(),
        language: "python".into(),
        coupling: Coupling::SharedRegion,
        initial: vec![
            ("reg/__init__.py".into(), String::new()),
            ("reg/registry.py".into(), "REGISTRY = {}\n".into()),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Append a line `REGISTRY['a'] = 1` after the REGISTRY definition. Keep all existing content.",
                "reg/registry.py",
            ),
            PilotSubtask::new(
                "Append a line `REGISTRY['b'] = 2` after the REGISTRY definition. Keep all existing content.",
                "reg/registry.py",
            ),
        ],
        test_cmd: py_test("from reg.registry import REGISTRY; assert REGISTRY == {'a': 1, 'b': 2}; print('ok')"),
    }
}

/// An `interface` variant over a module-level **constant**: one agent renames `TIMEOUT`, the
/// caller still references the old name. Same cross-file write skew, different symbol kind.
pub fn py_interface_constant_rename() -> PilotTask {
    PilotTask {
        id: "py-interface-constant-rename".into(),
        language: "python".into(),
        coupling: Coupling::Interface,
        initial: vec![
            ("cfg/__init__.py".into(), String::new()),
            ("cfg/settings.py".into(), "TIMEOUT = 30\n".into()),
            (
                "cfg/caller.py".into(),
                "from cfg.settings import TIMEOUT\n\ndef budget():\n    return TIMEOUT * 2\n".into(),
            ),
        ],
        subtasks: vec![
            PilotSubtask::new(
                "Rename the constant `TIMEOUT` to `DEFAULT_TIMEOUT` in this file (keep the value 30).",
                "cfg/settings.py",
            ),
            PilotSubtask::new(
                "Add a docstring to `budget`. Keep the existing reference as-is.",
                "cfg/caller.py",
            )
            .reading("cfg/settings.py"),
        ],
        test_cmd: py_test("from cfg.caller import budget; assert budget() == 60; print('ok')"),
    }
}

/// A **mixed-coupling** task: `n_shared` same-file merges plus `n_interface` cross-file
/// rename/caller pairs, each in its own package, scored by one test that imports everything.
/// Dialing `n_interface` vs `n_shared` sweeps the fraction of cross-file (write×read) coupling —
/// the independent variable for the coupling-threshold experiment. Each pair is two subtasks
/// (N = `2 * (n_shared + n_interface)`).
pub fn mixed_coupling(n_shared: usize, n_interface: usize) -> PilotTask {
    let mut initial = Vec::new();
    let mut subtasks = Vec::new();
    let mut asserts = Vec::new();

    for i in 0..n_shared {
        let pkg = format!("sh{i}");
        initial.push((format!("{pkg}/__init__.py"), String::new()));
        initial.push((format!("{pkg}/ops.py"), "\"\"\"ops\"\"\"\n".into()));
        subtasks.push(PilotSubtask::new(
            "Add a function `def add(a, b): return a + b` to this module. Keep all existing content.",
            format!("{pkg}/ops.py"),
        ));
        subtasks.push(PilotSubtask::new(
            "Add a function `def mul(a, b): return a * b` to this module. Keep all existing content.",
            format!("{pkg}/ops.py"),
        ));
        asserts.push(format!(
            "from {pkg}.ops import add as a{i}, mul as m{i}; assert a{i}(2, 3) == 5 and m{i}(2, 3) == 6"
        ));
    }
    for i in 0..n_interface {
        let pkg = format!("if{i}");
        initial.push((format!("{pkg}/__init__.py"), String::new()));
        initial.push((
            format!("{pkg}/api.py"),
            "def handle(req):\n    return req\n".into(),
        ));
        initial.push((
            format!("{pkg}/caller.py"),
            format!("from {pkg}.api import handle\n\ndef run(x):\n    return handle(x)\n"),
        ));
        subtasks.push(PilotSubtask::new(
            "Rename the function `handle` to `process` in this file (update its definition).",
            format!("{pkg}/api.py"),
        ));
        subtasks.push(
            PilotSubtask::new(
                "Add a docstring to the `run` function. Keep the existing call as-is.",
                format!("{pkg}/caller.py"),
            )
            .reading(format!("{pkg}/api.py")),
        );
        asserts.push(format!(
            "from {pkg}.caller import run as r{i}; assert r{i}(7) == 7"
        ));
    }
    asserts.push("print('ok')".to_string());

    PilotTask {
        id: format!("mixed-s{n_shared}-i{n_interface}"),
        language: "python".into(),
        coupling: if n_interface > 0 {
            Coupling::Interface
        } else {
            Coupling::SharedRegion
        },
        initial,
        subtasks,
        test_cmd: vec!["python".into(), "-c".into(), asserts.join("; ")],
    }
}

/// The mix sequence for a coupling sweep: keep the total pair count at `max_pairs` and move pairs
/// from same-file to cross-file, so the coupling fraction rises monotonically from 0. Returns
/// `(n_shared, n_interface)` steps.
pub fn sweep_plan(max_pairs: usize) -> Vec<(usize, usize)> {
    (0..=max_pairs).map(|i| (max_pairs - i, i)).collect()
}

/// All toy tasks.
pub fn all() -> Vec<PilotTask> {
    vec![
        py_shared_region_merge(),
        py_disjoint_independent(),
        py_interface_break(),
        py_three_way_shared(),
        py_interface_signature_change(),
        py_registry_merge(),
        py_interface_constant_rename(),
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
    fn mixed_coupling_is_well_formed_and_measures_its_coupling() {
        // 0 interface → no cross-file coupling; all shared.
        let t = mixed_coupling(2, 0);
        assert_eq!(t.n(), 4);
        assert_eq!(t.coupling_fraction(), 0.0);

        // 2 interface, 0 shared → the 2 callers (of 4 subtasks) carry cross-file reads.
        let t = mixed_coupling(0, 2);
        assert_eq!(t.n(), 4);
        assert!((t.coupling_fraction() - 0.5).abs() < 1e-9);

        // 1 shared + 1 interface → 1 cross reader of 4 subtasks.
        let t = mixed_coupling(1, 1);
        assert!((t.coupling_fraction() - 0.25).abs() < 1e-9);

        // Well-formed: every region and read is a seed file.
        let files: BTreeSet<&str> = t.initial.iter().map(|(p, _)| p.as_str()).collect();
        for s in &t.subtasks {
            assert!(
                files.contains(s.region.as_str()),
                "region {} missing",
                s.region
            );
            for r in &s.reads {
                assert!(files.contains(r.as_str()), "read {r} missing");
            }
        }
        assert!(!t.test_cmd.is_empty());
    }

    #[test]
    fn sweep_plan_raises_coupling_monotonically() {
        let plan = sweep_plan(3);
        assert_eq!(plan, vec![(3, 0), (2, 1), (1, 2), (0, 3)]);
        let fracs: Vec<f64> = plan
            .iter()
            .map(|&(s, i)| mixed_coupling(s, i).coupling_fraction())
            .collect();
        for w in fracs.windows(2) {
            assert!(
                w[1] >= w[0],
                "coupling fraction should not decrease: {fracs:?}"
            );
        }
        assert_eq!(fracs[0], 0.0);
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
