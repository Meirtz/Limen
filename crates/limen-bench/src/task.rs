//! Tasks, mock agents, and synthetic fixtures.
//!
//! A mock agent is a deterministic script of edits — no LLM needed — which lets the
//! apparatus demonstrate the *mechanism* on ground truth. Each edit is a *semantic*
//! mutation applied to the file's content **at write time**, so the coordination policy
//! (stale snapshot vs. fresh read) is what determines the outcome.

/// A semantic edit applied to the current content of a file.
#[derive(Clone, Debug)]
pub enum Mutation {
    /// Append a line (a trailing newline is ensured).
    AppendLine(String),
    /// Replace the first occurrence of `from` with `to`.
    Replace { from: String, to: String },
    /// Overwrite the whole file.
    SetContent(String),
}

impl Mutation {
    /// Apply the mutation to the current content.
    pub fn apply(&self, current: &str) -> String {
        match self {
            Mutation::AppendLine(line) => {
                let mut s = current.to_string();
                if !s.is_empty() && !s.ends_with('\n') {
                    s.push('\n');
                }
                s.push_str(line);
                s.push('\n');
                s
            }
            Mutation::Replace { from, to } => current.replacen(from, to, 1),
            Mutation::SetContent(c) => c.clone(),
        }
    }

    /// The text this mutation is intended to introduce — used by the coordination-independent
    /// oracle to detect a contribution that was lost (overwritten) in the final state.
    pub fn introduced(&self) -> Vec<String> {
        match self {
            Mutation::AppendLine(line) => vec![line.clone()],
            Mutation::Replace { to, .. } => vec![to.clone()],
            Mutation::SetContent(c) => c.lines().map(str::to_string).collect(),
        }
    }
}

/// One edit: a mutation against a relative target path.
#[derive(Clone, Debug)]
pub struct EditOp {
    pub target: String,
    pub mutation: Mutation,
}

/// A mock agent: a label and an ordered script of edits.
#[derive(Clone, Debug)]
pub struct MockAgent {
    pub label: String,
    pub ops: Vec<EditOp>,
}

/// How much the agents' work overlaps — the independent variable of the study.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Coupling {
    Disjoint,
    SharedRegion,
    Interface,
}

/// A synthetic task: initial files, the agents' scripts, and ground truth for scoring.
#[derive(Clone, Debug)]
pub struct Task {
    pub id: String,
    pub coupling: Coupling,
    /// Initial (relative path, content) of every file the task touches.
    pub initial: Vec<(String, String)>,
    pub agents: Vec<MockAgent>,
    /// Pass check: each file must contain all these substrings in the final state.
    pub expectations: Vec<(String, Vec<String>)>,
    /// Build-break check: each file must NOT contain any of these (stale references).
    pub forbidden: Vec<(String, Vec<String>)>,
    /// Attribution ground truth: which agent label should own each file's final content.
    pub gold_owner: Vec<(String, String)>,
}

/// Two agents append different functions to the **same** file — the canonical lost-update
/// case. Under naive concurrency both read the initial file and the later write clobbers the
/// earlier; under coordination they serialize on fresh reads and both contributions survive.
pub fn shared_region_merge() -> Task {
    Task {
        id: "shared-region-merge".into(),
        coupling: Coupling::SharedRegion,
        initial: vec![("src/lib.rs".into(), "pub mod thing;\n".into())],
        agents: vec![
            MockAgent {
                label: "agent-A".into(),
                ops: vec![EditOp {
                    target: "src/lib.rs".into(),
                    mutation: Mutation::AppendLine("pub fn foo() -> u32 { 1 }".into()),
                }],
            },
            MockAgent {
                label: "agent-B".into(),
                ops: vec![EditOp {
                    target: "src/lib.rs".into(),
                    mutation: Mutation::AppendLine("pub fn bar() -> u32 { 2 }".into()),
                }],
            },
        ],
        expectations: vec![("src/lib.rs".into(), vec!["fn foo".into(), "fn bar".into()])],
        forbidden: vec![],
        // Under coordination agent-B applies last, so it owns the final content.
        gold_owner: vec![("src/lib.rs".into(), "agent-B".into())],
    }
}

/// Two agents edit **different** files — no overlap. Coordination should make no difference
/// here (the honest fairness check: the advantage must vanish on disjoint work).
pub fn disjoint_independent() -> Task {
    Task {
        id: "disjoint-independent".into(),
        coupling: Coupling::Disjoint,
        initial: vec![
            ("src/a.rs".into(), "// a\n".into()),
            ("src/b.rs".into(), "// b\n".into()),
        ],
        agents: vec![
            MockAgent {
                label: "agent-A".into(),
                ops: vec![EditOp {
                    target: "src/a.rs".into(),
                    mutation: Mutation::AppendLine("pub fn a() {}".into()),
                }],
            },
            MockAgent {
                label: "agent-B".into(),
                ops: vec![EditOp {
                    target: "src/b.rs".into(),
                    mutation: Mutation::AppendLine("pub fn b() {}".into()),
                }],
            },
        ],
        expectations: vec![
            ("src/a.rs".into(), vec!["fn a".into()]),
            ("src/b.rs".into(), vec!["fn b".into()]),
        ],
        forbidden: vec![],
        gold_owner: vec![
            ("src/a.rs".into(), "agent-A".into()),
            ("src/b.rs".into(), "agent-B".into()),
        ],
    }
}
