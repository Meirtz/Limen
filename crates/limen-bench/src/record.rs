//! The per-run record emitted by every arm.

/// Outcome of running one task under one arm. `attribution_correct` is `None` when the
/// arm has no attribution mechanism (e.g. naive concurrency, where `git blame` would name
/// only the human), and `Some(bool)` when a witness trail can be scored against ground truth.
#[derive(Clone, Debug)]
pub struct RunRecord {
    pub arm: String,
    pub task: String,
    pub coupling: String,
    pub passed: bool,
    pub lost_edit_lines: usize,
    pub build_break: bool,
    pub attribution_correct: Option<bool>,
}
