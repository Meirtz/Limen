//! A minimal real coding agent over the inference hub.
//!
//! Toy by design (the benchmark suite is a separate, serious sub-project): the agent edits
//! **one file at a time** — given a subtask and the file's current content, it asks the model
//! for the COMPLETE new content and extracts it from a fenced code block. Full-content rather
//! than diffs avoids patch-application fragility for the pilot. Deterministic where the backend
//! honors `seed`. The extraction parser is unit-tested without network; the live call is gated
//! behind `INFERENCE_HUB_API_KEY`.

use crate::model::{ChatMessage, CompletionParams, ModelClient};
use anyhow::{Context, Result};

/// A real coding agent: a label, a model id, a borrowed client, and sampling params.
pub struct ModelAgent<'a> {
    pub label: String,
    pub model: String,
    pub client: &'a ModelClient,
    pub params: CompletionParams,
}

impl ModelAgent<'_> {
    /// Produce the complete new content of `path` after applying `subtask` to `current`.
    pub async fn edit_file(&self, subtask: &str, path: &str, current: &str) -> Result<String> {
        let system = ChatMessage::system(
            "You are a coding agent editing exactly one file. Apply the user's subtask to the \
             file. Reply with ONLY the complete new content of the file inside a single fenced \
             code block (```), and nothing else — no prose, no explanation.",
        );
        let user = ChatMessage::user(format!(
            "File: {path}\n\nCurrent content:\n```\n{current}\n```\n\nSubtask: {subtask}\n\n\
             Return the COMPLETE new content of {path} in one fenced code block."
        ));
        let reply = self
            .client
            .complete(&self.model, &[system, user], &self.params)
            .await
            .with_context(|| format!("agent '{}' editing {path}", self.label))?;
        extract_code_block(&reply)
            .with_context(|| format!("agent '{}' reply had no fenced code block", self.label))
    }

    /// Reconcile `path` after a file it depends on changed. The advisory coordinator surfaced the
    /// change (it does not edit for the agent); the agent updates its own file to stay consistent
    /// — e.g. fixing a call to a renamed symbol. This is what recovers cross-region write skew
    /// that per-file leases cannot.
    pub async fn reconcile_file(
        &self,
        subtask: &str,
        path: &str,
        current: &str,
        dep_path: &str,
        dep_content: &str,
    ) -> Result<String> {
        let system = ChatMessage::system(
            "You are a coding agent editing exactly one file. A file your file depends on has \
             changed. Update your file so it stays correct against the new dependency — for \
             example, fix imports or calls to a renamed symbol. Reply with ONLY the complete new \
             content of the file inside a single fenced code block (```), and nothing else.",
        );
        let user = ChatMessage::user(format!(
            "File you are editing: {path}\n\nIts current content:\n```\n{current}\n```\n\nA file \
             it depends on, {dep_path}, has changed to:\n```\n{dep_content}\n```\n\nOriginal \
             subtask: {subtask}\n\nReconcile {path} so it still works with the new {dep_path}. \
             Return the COMPLETE new content of {path} in one fenced code block."
        ));
        let reply = self
            .client
            .complete(&self.model, &[system, user], &self.params)
            .await
            .with_context(|| format!("agent '{}' reconciling {path}", self.label))?;
        extract_code_block(&reply).with_context(|| {
            format!(
                "agent '{}' reconcile reply had no fenced code block",
                self.label
            )
        })
    }
}

/// Extract the body of the first fenced ``` code block, ignoring an optional language tag on the
/// opening fence. Returns `None` if there is no complete fenced block.
pub fn extract_code_block(reply: &str) -> Option<String> {
    let start = reply.find("```")?;
    let after = &reply[start + 3..];
    // Skip the rest of the opening-fence line (an optional language tag like `rust`).
    let body_start = after.find('\n').map(|i| i + 1)?;
    let body = &after[body_start..];
    let end = body.find("```")?;
    Some(body[..end].to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_fenced_code_with_language_tag() {
        let reply = "Sure!\n```rust\nfn main() {}\n```\nDone.";
        assert_eq!(extract_code_block(reply).as_deref(), Some("fn main() {}\n"));
    }

    #[test]
    fn extracts_plain_fence() {
        let reply = "```\nplain text\n```";
        assert_eq!(extract_code_block(reply).as_deref(), Some("plain text\n"));
    }

    #[test]
    fn none_without_a_complete_block() {
        assert!(extract_code_block("no code here").is_none());
        assert!(extract_code_block("```rust\nunclosed").is_none());
    }
}
