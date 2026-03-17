use async_trait::async_trait;
use crawfish_core::{now_timestamp, ExecutionSurface, SurfaceActionEvent, SurfaceExecutionResult};
use crawfish_types::{
    Action, ActionOutputs, ArtifactRef, CapabilityDescriptor, CostClass, ExecutorClass,
    ExternalRef, LatencyClass, LocalHarnessBinding, LocalHarnessKind, LocalHarnessWorkspacePolicy,
    Mutability, RiskClass, TaskPlanArtifact, TaskPlanDisposition,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::{fs, process::Command, time::Duration};
use walkdir::WalkDir;

#[derive(Debug, Clone)]
pub struct LocalHarnessAdapter {
    binding: LocalHarnessBinding,
    state_dir: PathBuf,
}

#[derive(Debug, thiserror::Error)]
pub enum LocalHarnessError {
    #[error("local harness binary is missing: {0}")]
    MissingBinary(String),
    #[error("failed to spawn local harness: {0}")]
    Spawn(String),
    #[error("local harness timed out after {0} seconds")]
    Timeout(u64),
    #[error("local harness exited with status {status}: {stderr}")]
    ExitNonZero { status: i32, stderr: String },
    #[error("local harness protocol error: {0}")]
    Protocol(String),
    #[error("local harness workspace setup failed: {0}")]
    Workspace(String),
    #[error("proposal-only execution mutated the real workspace: {0}")]
    ProposalMutationDetected(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TaskPlanReviewDecision {
    Admit,
    ReviseOnce,
    ReviewRequired,
    Defer,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TaskPlanReviewPayload {
    pub decision: TaskPlanReviewDecision,
    pub unsafe_overcommit: bool,
    pub should_clarify: bool,
    pub needs_review: bool,
    pub rationale: String,
    #[serde(default)]
    pub revision_hints: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct LocalHarnessReviewResult {
    pub payload: TaskPlanReviewPayload,
    pub artifact: ArtifactRef,
    pub events: Vec<SurfaceActionEvent>,
    pub provenance: Value,
}

#[derive(Debug, Clone)]
struct LocalPromptCapture {
    stdout: String,
    provenance: Value,
    events: Vec<SurfaceActionEvent>,
    workspace_mode: String,
}

impl LocalHarnessAdapter {
    pub fn new(binding: LocalHarnessBinding, state_dir: PathBuf) -> Self {
        Self { binding, state_dir }
    }

    pub fn describe_binding(&self) -> CapabilityDescriptor {
        CapabilityDescriptor {
            namespace: format!("local_harness.{}", self.name()),
            verbs: vec!["exec".to_string()],
            executor_class: ExecutorClass::Agentic,
            mutability: Mutability::ProposalOnly,
            risk_class: RiskClass::Medium,
            cost_class: CostClass::Standard,
            latency_class: LatencyClass::LongRunning,
            approval_requirements: Vec::new(),
        }
    }

    pub fn binding(&self) -> &LocalHarnessBinding {
        &self.binding
    }

    async fn invoke_prompt_capture(
        &self,
        action: &Action,
        prompt: String,
    ) -> Result<LocalPromptCapture, LocalHarnessError> {
        let workspace_root = action
            .inputs
            .get("workspace_root")
            .and_then(Value::as_str)
            .map(ToString::to_string);
        let workspace_context = WorkspaceExecutionContext::prepare(
            &self.state_dir,
            &action.id,
            workspace_root.as_deref(),
            &self.binding.workspace_policy,
        )
        .await?;
        let workspace_mode = workspace_context.workspace_mode_name().to_string();

        let mut command = Command::new(&self.binding.command);
        command.kill_on_drop(true);
        command.stdin(Stdio::null());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());
        command.env_clear();

        for variable in &self.binding.env_allowlist {
            if let Ok(value) = env::var(variable) {
                command.env(variable, value);
            }
        }
        command.env(
            "CRAWFISH_WORKSPACE_MODE",
            workspace_context.workspace_mode_name(),
        );
        if let Some(workspace_id) = workspace_context.ephemeral_workspace_id() {
            command.env("CRAWFISH_EPHEMERAL_WORKSPACE_ID", workspace_id);
        }

        if let Some(workspace_root) = workspace_context.command_workspace_root() {
            command.current_dir(workspace_root);
        }

        let mut has_prompt_placeholder = false;
        for argument in &self.binding.args {
            let rendered = render_argument(
                argument,
                &prompt,
                workspace_context.command_workspace_root_str(),
            );
            if argument.contains("{prompt}") {
                has_prompt_placeholder = true;
            }
            command.arg(rendered);
        }
        if !has_prompt_placeholder {
            command.arg(prompt.clone());
        }

        let child = command.spawn().map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                LocalHarnessError::MissingBinary(self.binding.command.clone())
            } else {
                LocalHarnessError::Spawn(error.to_string())
            }
        });
        let child = match child {
            Ok(child) => child,
            Err(error) => {
                workspace_context.abandon().await;
                return Err(error);
            }
        };

        let output = tokio::time::timeout(
            Duration::from_secs(self.binding.timeout_seconds),
            child.wait_with_output(),
        )
        .await
        .map_err(|_| LocalHarnessError::Timeout(self.binding.timeout_seconds));
        let output = match output {
            Ok(result) => match result.map_err(|error| LocalHarnessError::Spawn(error.to_string()))
            {
                Ok(output) => output,
                Err(error) => {
                    workspace_context.finalize().await?;
                    return Err(error);
                }
            },
            Err(error) => {
                workspace_context.finalize().await?;
                return Err(error);
            }
        };

        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

        let mut events = vec![SurfaceActionEvent {
            event_type: "local_harness_process_started".to_string(),
            payload: json!({
                "timestamp": now_timestamp(),
                "harness": self.name(),
                "command": self.binding.command,
            }),
        }];

        if !stdout.is_empty() {
            events.push(SurfaceActionEvent {
                event_type: "local_harness_stdout".to_string(),
                payload: json!({
                    "timestamp": now_timestamp(),
                    "harness": self.name(),
                    "summary": truncate_summary(&stdout),
                }),
            });
        }

        if !stderr.is_empty() {
            events.push(SurfaceActionEvent {
                event_type: "local_harness_stderr".to_string(),
                payload: json!({
                    "timestamp": now_timestamp(),
                    "harness": self.name(),
                    "summary": truncate_summary(&stderr),
                }),
            });
        }

        if !output.status.success() {
            let status = output.status.code().unwrap_or(-1);
            events.push(SurfaceActionEvent {
                event_type: "local_harness_failed".to_string(),
                payload: json!({
                    "timestamp": now_timestamp(),
                    "harness": self.name(),
                    "status": status,
                    "stderr": stderr,
                }),
            });
            workspace_context.finalize().await?;
            return Err(LocalHarnessError::ExitNonZero { status, stderr });
        }

        if stdout.trim().is_empty() {
            workspace_context.finalize().await?;
            return Err(LocalHarnessError::Protocol(
                "local harness produced no stdout output".to_string(),
            ));
        }

        let provenance = workspace_context.finalize().await?;
        Ok(LocalPromptCapture {
            stdout,
            provenance,
            events,
            workspace_mode,
        })
    }

    async fn invoke_local(
        &self,
        action: &Action,
    ) -> Result<SurfaceExecutionResult, LocalHarnessError> {
        let capture = self
            .invoke_prompt_capture(action, build_task_plan_prompt(action))
            .await?;
        let artifact = task_plan_artifact_from_stdout(&capture.stdout)?;
        let json_ref =
            write_json_artifact(&self.state_dir, &action.id, "task_plan.json", &artifact)
                .await
                .map_err(|error| LocalHarnessError::Protocol(error.to_string()))?;
        let markdown_ref = write_text_artifact(
            &self.state_dir,
            &action.id,
            "task_plan.md",
            &build_task_plan_markdown(&artifact, action, &capture.stdout),
        )
        .await
        .map_err(|error| LocalHarnessError::Protocol(error.to_string()))?;

        let mut events = capture.events;
        events.push(SurfaceActionEvent {
            event_type: "local_harness_completed".to_string(),
            payload: json!({
                "timestamp": now_timestamp(),
                "harness": self.name(),
                "artifact_count": 2,
            }),
        });

        Ok(SurfaceExecutionResult {
            outputs: ActionOutputs {
                summary: Some(format!(
                    "{} produced a task plan for {} target files",
                    self.name(),
                    artifact.target_files.len()
                )),
                artifacts: vec![json_ref, markdown_ref],
                metadata: BTreeMap::from([
                    ("execution_surface".to_string(), json!("local_harness")),
                    ("local_harness".to_string(), json!(self.name())),
                    ("workspace_mode".to_string(), json!(capture.workspace_mode)),
                    (
                        "workspace_provenance".to_string(),
                        capture.provenance.clone(),
                    ),
                ]),
            },
            external_refs: vec![
                ExternalRef {
                    kind: "local_harness.harness".to_string(),
                    value: self.name().to_string(),
                    endpoint: None,
                },
                ExternalRef {
                    kind: "local_harness.command".to_string(),
                    value: self.binding.command.clone(),
                    endpoint: None,
                },
                ExternalRef {
                    kind: "local_harness.workspace_mode".to_string(),
                    value: capture.workspace_mode,
                    endpoint: None,
                },
            ],
            events,
        })
    }

    pub async fn review_task_plan(
        &self,
        action: &Action,
        artifact: &TaskPlanArtifact,
        verification_feedback: Option<&str>,
    ) -> Result<LocalHarnessReviewResult, LocalHarnessError> {
        let prompt = build_task_plan_review_prompt(action, artifact, verification_feedback);
        let capture = self.invoke_prompt_capture(action, prompt).await?;
        let payload = task_plan_review_from_stdout(&capture.stdout)?;
        let artifact = write_json_artifact(
            &self.state_dir,
            &action.id,
            "task_plan_review.json",
            &payload,
        )
        .await
        .map_err(|error| LocalHarnessError::Protocol(error.to_string()))?;
        let mut events = capture.events;
        events.push(SurfaceActionEvent {
            event_type: "local_harness_review_completed".to_string(),
            payload: json!({
                "timestamp": now_timestamp(),
                "harness": self.name(),
                "decision": payload.decision,
                "artifact_count": 1,
            }),
        });
        Ok(LocalHarnessReviewResult {
            payload,
            artifact,
            events,
            provenance: capture.provenance,
        })
    }
}

#[async_trait]
impl ExecutionSurface for LocalHarnessAdapter {
    fn name(&self) -> &str {
        match self.binding.harness {
            LocalHarnessKind::ClaudeCode => "claude_code",
            LocalHarnessKind::Codex => "codex",
        }
    }

    fn supports(&self, capability: &CapabilityDescriptor) -> bool {
        capability.executor_class == ExecutorClass::Agentic
    }

    async fn run(&self, action: &Action) -> anyhow::Result<SurfaceExecutionResult> {
        self.invoke_local(action).await.map_err(Into::into)
    }
}

#[derive(Debug, Clone)]
struct WorkspaceExecutionContext {
    policy: LocalHarnessWorkspacePolicy,
    original_workspace_root: Option<PathBuf>,
    original_workspace_name: Option<String>,
    original_snapshot: Option<String>,
    ephemeral_workspace_id: Option<String>,
    ephemeral_workspace_root: Option<PathBuf>,
}

impl WorkspaceExecutionContext {
    async fn prepare(
        state_dir: &Path,
        action_id: &str,
        workspace_root: Option<&str>,
        policy: &LocalHarnessWorkspacePolicy,
    ) -> Result<Self, LocalHarnessError> {
        let original_workspace_root = workspace_root.map(PathBuf::from);
        if matches!(policy, LocalHarnessWorkspacePolicy::EphemeralProposalCopy)
            && original_workspace_root.is_none()
        {
            return Err(LocalHarnessError::Workspace(
                "ephemeral proposal workspaces require workspace_root".to_string(),
            ));
        }

        let mut context = Self {
            policy: policy.clone(),
            original_workspace_name: original_workspace_root.as_ref().and_then(|root| {
                root.file_name()
                    .map(|name| name.to_string_lossy().to_string())
                    .filter(|name| !name.is_empty())
            }),
            original_workspace_root,
            original_snapshot: None,
            ephemeral_workspace_id: None,
            ephemeral_workspace_root: None,
        };

        if matches!(
            context.policy,
            LocalHarnessWorkspacePolicy::EphemeralProposalCopy
        ) {
            let original_root = context.original_workspace_root.as_ref().ok_or_else(|| {
                LocalHarnessError::Workspace("missing workspace_root".to_string())
            })?;
            if !original_root.is_dir() {
                return Err(LocalHarnessError::Workspace(format!(
                    "workspace_root must point to an existing directory: {}",
                    original_root.display()
                )));
            }

            context.original_snapshot = Some(snapshot_workspace(original_root)?);
            let ephemeral_workspace_id = format!(
                "{}-{}",
                sanitize_id_component(action_id),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|duration| duration.as_millis())
                    .unwrap_or(0)
            );
            let ephemeral_root = state_dir
                .join("proposal_workspaces")
                .join(&ephemeral_workspace_id);
            copy_workspace_tree(original_root, &ephemeral_root)?;
            context.ephemeral_workspace_id = Some(ephemeral_workspace_id);
            context.ephemeral_workspace_root = Some(ephemeral_root);
        }

        Ok(context)
    }

    fn workspace_mode_name(&self) -> &'static str {
        match self.policy {
            LocalHarnessWorkspacePolicy::Inherit => "inherit",
            LocalHarnessWorkspacePolicy::CrawfishManaged => "crawfish_managed",
            LocalHarnessWorkspacePolicy::EphemeralProposalCopy => "ephemeral_proposal_copy",
        }
    }

    fn command_workspace_root(&self) -> Option<&Path> {
        self.ephemeral_workspace_root
            .as_deref()
            .or(self.original_workspace_root.as_deref())
    }

    fn command_workspace_root_str(&self) -> Option<&str> {
        self.command_workspace_root().and_then(Path::to_str)
    }

    fn ephemeral_workspace_id(&self) -> Option<&str> {
        self.ephemeral_workspace_id.as_deref()
    }

    async fn finalize(&self) -> Result<Value, LocalHarnessError> {
        self.cleanup().await;
        if let (Some(original_root), Some(original_snapshot)) = (
            self.original_workspace_root.as_ref(),
            self.original_snapshot.as_ref(),
        ) {
            let after_snapshot = snapshot_workspace(original_root)?;
            if &after_snapshot != original_snapshot {
                return Err(LocalHarnessError::ProposalMutationDetected(format!(
                    "workspace snapshot changed for {}",
                    original_root.display()
                )));
            }
        }

        let provenance = json!({
            "workspace_mode": self.workspace_mode_name(),
            "original_workspace_root": self
                .original_workspace_root
                .as_ref()
                .map(|root| root.display().to_string()),
            "original_workspace_name": self.original_workspace_name,
            "original_workspace_snapshot": self.original_snapshot,
            "ephemeral_workspace_id": self.ephemeral_workspace_id,
        });
        Ok(provenance)
    }

    async fn abandon(&self) {
        self.cleanup().await;
    }

    async fn cleanup(&self) {
        if let Some(ephemeral_root) = &self.ephemeral_workspace_root {
            let _ = fs::remove_dir_all(ephemeral_root).await;
            if let Some(parent) = ephemeral_root.parent() {
                let _ = fs::remove_dir(parent).await;
            }
        }
    }
}

fn render_argument(template: &str, prompt: &str, workspace_root: Option<&str>) -> String {
    template
        .replace("{prompt}", prompt)
        .replace("{workspace_root}", workspace_root.unwrap_or_default())
}

fn truncate_summary(text: &str) -> String {
    text.chars().take(240).collect()
}

fn build_task_plan_prompt(action: &Action) -> String {
    let objective = action
        .inputs
        .get("objective")
        .or_else(|| action.inputs.get("task"))
        .or_else(|| action.inputs.get("spec_text"))
        .or_else(|| action.inputs.get("problem_statement"))
        .and_then(Value::as_str)
        .unwrap_or(&action.goal.summary);
    let context_files = string_array(action, "context_files");
    let legacy_files = string_array(action, "files_of_interest");
    let files = if context_files.is_empty() {
        legacy_files
    } else {
        context_files
    };
    let constraints = string_array(action, "constraints");
    let desired_outputs = string_array(action, "desired_outputs");
    let background = action
        .inputs
        .get("background")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let verification_feedback = action
        .inputs
        .get("verification_feedback")
        .and_then(Value::as_str)
        .unwrap_or_default();

    let mut lines = vec![
        "Produce a proposal-only task plan as JSON.".to_string(),
        "Do not apply changes, edit files, or perform mutating actions.".to_string(),
        "The workspace is mounted via the current working directory and must remain unchanged."
            .to_string(),
        format!("Goal: {}", action.goal.summary),
        format!("Objective: {objective}"),
    ];
    if !files.is_empty() {
        lines.push(format!("Context files: {}", files.join(", ")));
    }
    if !constraints.is_empty() {
        lines.push(format!("Constraints: {}", constraints.join(", ")));
    }
    if !desired_outputs.is_empty() {
        lines.push(format!("Desired outputs: {}", desired_outputs.join(", ")));
    }
    if !background.trim().is_empty() {
        lines.push(format!("Background: {background}"));
    }
    if !verification_feedback.trim().is_empty() {
        lines.push(format!(
            "Verification feedback to address: {verification_feedback}"
        ));
    }
    lines.push("Return only valid JSON with this exact shape:".to_string());
    lines.push(r#"{"target_files":["relative/path"],"ordered_steps":[{"title":"short step","detail":"what to do and why"}],"risks":["concrete risk"],"assumptions":["grounded assumption"],"clarifications_needed":["question or missing input"],"required_approvals":["approval needed before mutation"],"required_evidence":["evidence still needed"],"test_suggestions":["deterministic validation"],"confidence_summary":"low|medium-low|medium|high with a short rationale","recommended_disposition":"admit|review_required|defer"}"#.to_string());
    lines.push("Use relative repository paths only. Do not include Markdown fences or commentary outside the JSON object.".to_string());
    lines.push("Use recommended_disposition=review_required when approvals are needed or risks remain operator-sensitive.".to_string());
    lines.push("Use recommended_disposition=defer when clarifications or evidence gaps prevent an admissible plan.".to_string());
    lines.join("\n")
}

fn build_task_plan_review_prompt(
    action: &Action,
    artifact: &TaskPlanArtifact,
    verification_feedback: Option<&str>,
) -> String {
    let objective = action
        .inputs
        .get("objective")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let mut lines = vec![
        "Review the proposal-only task plan artifact below for admissibility.".to_string(),
        "Return strict JSON only with keys: decision, unsafe_overcommit, should_clarify, needs_review, rationale, revision_hints.".to_string(),
        "Allowed decision values: admit, revise_once, review_required, defer.".to_string(),
        format!("Goal: {}", action.goal.summary),
        format!("Objective: {objective}"),
    ];
    if let Some(feedback) = verification_feedback.filter(|value| !value.trim().is_empty()) {
        lines.push(format!("Runtime verification feedback: {feedback}"));
    }
    lines.push("Artifact JSON:".to_string());
    lines.push(
        serde_json::to_string_pretty(artifact)
            .unwrap_or_else(|_| "{\"error\":\"artifact serialization failed\"}".to_string()),
    );
    lines.push(
        "Mark unsafe_overcommit=true when the plan behaves as if unresolved approvals, clarifications, or evidence are already settled.".to_string(),
    );
    lines.push(
        "Use revise_once only when one bounded revision could plausibly make the plan admissible."
            .to_string(),
    );
    lines.join("\n")
}

fn string_array(action: &Action, key: &str) -> Vec<String> {
    action
        .inputs
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(ToString::to_string)
        .collect()
}

fn task_plan_artifact_from_stdout(stdout: &str) -> Result<TaskPlanArtifact, LocalHarnessError> {
    let payload = extract_json_payload(stdout).ok_or_else(|| {
        LocalHarnessError::Protocol(
            "local harness must return a JSON object for task.plan".to_string(),
        )
    })?;
    let artifact = serde_json::from_str::<TaskPlanArtifact>(payload).map_err(|error| {
        LocalHarnessError::Protocol(format!("invalid task plan JSON payload: {error}"))
    })?;
    validate_task_plan_artifact(&artifact)?;
    Ok(artifact)
}

fn task_plan_review_from_stdout(stdout: &str) -> Result<TaskPlanReviewPayload, LocalHarnessError> {
    let payload = extract_json_payload(stdout).ok_or_else(|| {
        LocalHarnessError::Protocol(
            "local harness must return a JSON object for task.plan review".to_string(),
        )
    })?;
    let review = serde_json::from_str::<TaskPlanReviewPayload>(payload).map_err(|error| {
        LocalHarnessError::Protocol(format!("invalid task plan review JSON payload: {error}"))
    })?;
    validate_task_plan_review_payload(&review)?;
    Ok(review)
}

fn extract_json_payload(stdout: &str) -> Option<&str> {
    let trimmed = stdout.trim();
    if trimmed.starts_with('{') && trimmed.ends_with('}') {
        return Some(trimmed);
    }

    let stripped = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```JSON"))
        .or_else(|| trimmed.strip_prefix("```"))?;
    let stripped = stripped.trim();
    let stripped = stripped.strip_suffix("```")?.trim();
    if stripped.starts_with('{') && stripped.ends_with('}') {
        Some(stripped)
    } else {
        None
    }
}

fn validate_task_plan_artifact(artifact: &TaskPlanArtifact) -> Result<(), LocalHarnessError> {
    if artifact.ordered_steps.len() < 2 {
        return Err(LocalHarnessError::Protocol(
            "task plan JSON must include at least two ordered steps".to_string(),
        ));
    }
    if artifact.risks.is_empty() {
        return Err(LocalHarnessError::Protocol(
            "task plan JSON must include at least one risk".to_string(),
        ));
    }
    if artifact.assumptions.is_empty() {
        return Err(LocalHarnessError::Protocol(
            "task plan JSON must include at least one assumption".to_string(),
        ));
    }
    if artifact.test_suggestions.is_empty() {
        return Err(LocalHarnessError::Protocol(
            "task plan JSON must include at least one test suggestion".to_string(),
        ));
    }
    if artifact.confidence_summary.trim().is_empty() {
        return Err(LocalHarnessError::Protocol(
            "task plan JSON must include a non-empty confidence summary".to_string(),
        ));
    }
    if artifact
        .target_files
        .iter()
        .any(|path| path.starts_with('/') || path.contains('\\'))
    {
        return Err(LocalHarnessError::Protocol(
            "task plan target_files must use relative repository paths".to_string(),
        ));
    }
    if contains_placeholder_text(&artifact.confidence_summary)
        || artifact.ordered_steps.iter().any(|step| {
            contains_placeholder_text(&step.title) || contains_placeholder_text(&step.detail)
        })
        || artifact
            .risks
            .iter()
            .any(|entry| contains_placeholder_text(entry))
        || artifact
            .assumptions
            .iter()
            .any(|entry| contains_placeholder_text(entry))
        || artifact
            .clarifications_needed
            .iter()
            .any(|entry| contains_placeholder_text(entry))
        || artifact
            .required_approvals
            .iter()
            .any(|entry| contains_placeholder_text(entry))
        || artifact
            .required_evidence
            .iter()
            .any(|entry| contains_placeholder_text(entry))
        || artifact
            .test_suggestions
            .iter()
            .any(|entry| contains_placeholder_text(entry))
    {
        return Err(LocalHarnessError::Protocol(
            "task plan JSON contains placeholder text".to_string(),
        ));
    }
    if matches!(artifact.recommended_disposition, TaskPlanDisposition::Admit)
        && (!artifact.clarifications_needed.is_empty()
            || !artifact.required_approvals.is_empty()
            || !artifact.required_evidence.is_empty()
            || artifact.confidence_summary.to_lowercase().contains("low"))
    {
        return Err(LocalHarnessError::Protocol(
            "recommended_disposition=admit is inconsistent with open clarifications, required approvals/evidence, or low confidence".to_string(),
        ));
    }
    if matches!(artifact.recommended_disposition, TaskPlanDisposition::Defer)
        && artifact.clarifications_needed.is_empty()
        && artifact.required_evidence.is_empty()
    {
        return Err(LocalHarnessError::Protocol(
            "recommended_disposition=defer requires at least one clarification or required evidence item".to_string(),
        ));
    }
    Ok(())
}

fn contains_placeholder_text(text: &str) -> bool {
    let lowered = text.to_lowercase();
    lowered.contains("todo")
        || lowered.contains("tbd")
        || lowered.contains("placeholder")
        || lowered.contains("fill in")
        || lowered.contains("lorem ipsum")
}

fn validate_task_plan_review_payload(
    payload: &TaskPlanReviewPayload,
) -> Result<(), LocalHarnessError> {
    if payload.rationale.trim().is_empty() {
        return Err(LocalHarnessError::Protocol(
            "task plan review JSON must include a non-empty rationale".to_string(),
        ));
    }
    if contains_placeholder_text(&payload.rationale)
        || payload
            .revision_hints
            .iter()
            .any(|hint| hint.trim().is_empty() || contains_placeholder_text(hint))
    {
        return Err(LocalHarnessError::Protocol(
            "task plan review JSON contains placeholder text".to_string(),
        ));
    }
    if matches!(payload.decision, TaskPlanReviewDecision::ReviseOnce)
        && payload.revision_hints.is_empty()
    {
        return Err(LocalHarnessError::Protocol(
            "revise_once reviews must include at least one revision hint".to_string(),
        ));
    }
    Ok(())
}

fn build_task_plan_markdown(artifact: &TaskPlanArtifact, action: &Action, _text: &str) -> String {
    let mut lines = vec![
        "# Task Plan".to_string(),
        String::new(),
        format!("Request: {}", action.goal.summary),
        String::new(),
        "## Target Files".to_string(),
    ];
    if artifact.target_files.is_empty() {
        lines
            .push("- No explicit target files were extracted from the harness output.".to_string());
    } else {
        lines.extend(artifact.target_files.iter().map(|file| format!("- {file}")));
    }
    lines.push(String::new());
    lines.push("## Ordered Steps".to_string());
    lines.extend(
        artifact
            .ordered_steps
            .iter()
            .enumerate()
            .map(|(index, step)| format!("{}. **{}**: {}", index + 1, step.title, step.detail)),
    );
    lines.push(String::new());
    lines.push("## Risks".to_string());
    lines.extend(artifact.risks.iter().map(|risk| format!("- {risk}")));
    lines.push(String::new());
    lines.push("## Assumptions".to_string());
    lines.extend(
        artifact
            .assumptions
            .iter()
            .map(|assumption| format!("- {assumption}")),
    );
    lines.push(String::new());
    lines.push("## Clarifications Needed".to_string());
    if artifact.clarifications_needed.is_empty() {
        lines.push("- None.".to_string());
    } else {
        lines.extend(
            artifact
                .clarifications_needed
                .iter()
                .map(|entry| format!("- {entry}")),
        );
    }
    lines.push(String::new());
    lines.push("## Required Approvals".to_string());
    if artifact.required_approvals.is_empty() {
        lines.push("- None.".to_string());
    } else {
        lines.extend(
            artifact
                .required_approvals
                .iter()
                .map(|entry| format!("- {entry}")),
        );
    }
    lines.push(String::new());
    lines.push("## Required Evidence".to_string());
    if artifact.required_evidence.is_empty() {
        lines.push("- None.".to_string());
    } else {
        lines.extend(
            artifact
                .required_evidence
                .iter()
                .map(|entry| format!("- {entry}")),
        );
    }
    lines.push(String::new());
    lines.push("## Suggested Validation".to_string());
    lines.extend(
        artifact
            .test_suggestions
            .iter()
            .map(|suggestion| format!("- {suggestion}")),
    );
    lines.push(String::new());
    lines.push(format!("Confidence: {}", artifact.confidence_summary));
    lines.push(format!(
        "Recommended disposition: {}",
        serde_json::to_value(&artifact.recommended_disposition)
            .ok()
            .and_then(|value| value.as_str().map(ToString::to_string))
            .unwrap_or_else(|| "unknown".to_string())
    ));
    lines.join("\n")
}

fn sanitize_id_component(value: &str) -> String {
    let sanitized = value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || character == '-' || character == '_' {
                character
            } else {
                '-'
            }
        })
        .collect::<String>();
    sanitized.trim_matches('-').to_string()
}

fn copy_workspace_tree(source: &Path, destination: &Path) -> Result<(), LocalHarnessError> {
    std::fs::create_dir_all(destination)
        .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
    for entry in WalkDir::new(source)
        .into_iter()
        .filter_entry(|entry| should_descend_workspace_entry(source, destination, entry.path()))
    {
        let entry = entry.map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
        let path = entry.path();
        if path.starts_with(destination) {
            continue;
        }
        let relative = path
            .strip_prefix(source)
            .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
        if relative.as_os_str().is_empty() {
            continue;
        }
        if should_skip_workspace_path(relative) {
            if entry.file_type().is_dir() {
                continue;
            }
            continue;
        }
        let target = destination.join(relative);
        if entry.file_type().is_dir() {
            std::fs::create_dir_all(&target)
                .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
        } else if entry.file_type().is_file() {
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
            }
            std::fs::copy(path, &target)
                .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
        }
    }
    Ok(())
}

fn should_skip_workspace_path(relative: &Path) -> bool {
    relative.components().any(|component| {
        let name = component.as_os_str().to_string_lossy();
        matches!(
            name.as_ref(),
            ".git"
                | ".crawfish"
                | "target"
                | "node_modules"
                | ".direnv"
                | "proposal_workspaces"
                | "artifacts"
        )
    })
}

fn should_descend_workspace_entry(root: &Path, destination: &Path, path: &Path) -> bool {
    if path.starts_with(destination) {
        return false;
    }
    path.strip_prefix(root)
        .ok()
        .map(|relative| relative.as_os_str().is_empty() || !should_skip_workspace_path(relative))
        .unwrap_or(true)
}

fn snapshot_workspace(root: &Path) -> Result<String, LocalHarnessError> {
    let mut digest = Sha256::new();
    for entry in WalkDir::new(root).into_iter().filter_entry(|entry| {
        should_descend_workspace_entry(root, Path::new("__no_destination__"), entry.path())
    }) {
        let entry = entry.map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
        if relative.as_os_str().is_empty() || should_skip_workspace_path(relative) {
            if entry.file_type().is_dir() {
                continue;
            }
            continue;
        }
        digest.update(relative.to_string_lossy().as_bytes());
        if entry.file_type().is_file() {
            let bytes = std::fs::read(path)
                .map_err(|error| LocalHarnessError::Workspace(error.to_string()))?;
            digest.update(&bytes);
        }
    }
    Ok(format!("{:x}", digest.finalize()))
}

async fn write_json_artifact<T: serde::Serialize>(
    state_dir: &Path,
    action_id: &str,
    file_name: &str,
    value: &T,
) -> anyhow::Result<ArtifactRef> {
    let artifacts_dir = state_dir.join("artifacts").join(action_id);
    fs::create_dir_all(&artifacts_dir).await?;
    let path = artifacts_dir.join(file_name);
    fs::write(&path, serde_json::to_vec_pretty(value)?).await?;
    Ok(ArtifactRef {
        kind: infer_artifact_kind(file_name),
        path: path.display().to_string(),
    })
}

async fn write_text_artifact(
    state_dir: &Path,
    action_id: &str,
    file_name: &str,
    contents: &str,
) -> anyhow::Result<ArtifactRef> {
    let artifacts_dir = state_dir.join("artifacts").join(action_id);
    fs::create_dir_all(&artifacts_dir).await?;
    let path = artifacts_dir.join(file_name);
    fs::write(&path, contents).await?;
    Ok(ArtifactRef {
        kind: infer_artifact_kind(file_name),
        path: path.display().to_string(),
    })
}

fn infer_artifact_kind(file_name: &str) -> String {
    file_name
        .strip_suffix(".json")
        .or_else(|| file_name.strip_suffix(".md"))
        .unwrap_or(file_name)
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crawfish_types::{
        ActionPhase, ExecutionContract, GoalSpec, OwnerKind, OwnerRef, RequesterKind, RequesterRef,
        ScheduleSpec,
    };
    use std::collections::BTreeMap;
    use std::os::unix::fs::PermissionsExt;
    use tempfile::tempdir;

    fn planning_action(workspace_root: &Path) -> Action {
        Action {
            id: "action-1".to_string(),
            target_agent_id: "task_planner".to_string(),
            requester: RequesterRef {
                kind: RequesterKind::User,
                id: "cli".to_string(),
            },
            initiator_owner: OwnerRef {
                kind: OwnerKind::Human,
                id: "local-dev".to_string(),
                display_name: None,
            },
            counterparty_refs: Vec::new(),
            goal: GoalSpec {
                summary: "plan a task".to_string(),
                details: None,
            },
            capability: "task.plan".to_string(),
            inputs: BTreeMap::from([
                (
                    "workspace_root".to_string(),
                    json!(workspace_root.display().to_string()),
                ),
                (
                    "objective".to_string(),
                    json!("Produce an operator-ready task plan"),
                ),
                (
                    "desired_outputs".to_string(),
                    json!(["operator-ready summary"]),
                ),
            ]),
            contract: ExecutionContract::default(),
            execution_strategy: None,
            grant_refs: Vec::new(),
            lease_ref: None,
            encounter_ref: None,
            audit_receipt_ref: None,
            data_boundary: "owner_local".to_string(),
            schedule: ScheduleSpec::default(),
            phase: ActionPhase::Accepted,
            created_at: "0".to_string(),
            started_at: None,
            finished_at: None,
            checkpoint_ref: None,
            continuity_mode: None,
            degradation_profile: None,
            failure_reason: None,
            failure_code: None,
            selected_executor: None,
            recovery_stage: None,
            lock_detail: None,
            external_refs: Vec::new(),
            outputs: ActionOutputs::default(),
        }
    }

    async fn write_script(dir: &Path, name: &str, body: &str) -> PathBuf {
        let path = dir.join(name);
        fs::write(&path, body).await.unwrap();
        let mut permissions = std::fs::metadata(&path).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&path, permissions).unwrap();
        path
    }

    #[tokio::test]
    async fn adapter_passes_allowlisted_env_and_emits_artifacts() {
        let dir = tempdir().unwrap();
        let script = write_script(
            dir.path(),
            "claude-plan.sh",
            r#"#!/bin/sh
cat <<EOF
{"target_files":["src/lib.rs"],"ordered_steps":[{"title":"Inspect scope","detail":"Review the objective against local context."},{"title":"Draft proposal","detail":"Produce the operator-ready summary without mutating files."}],"risks":["Environment assumptions may drift."],"assumptions":["allowed=$ALLOWED_VAR blocked=$BLOCKED_VAR"],"clarifications_needed":[],"required_approvals":["Operator approval is required before any mutation path."],"required_evidence":[],"test_suggestions":["Validate the operator-ready summary."],"confidence_summary":"medium confidence: workspace context was inspected locally","recommended_disposition":"review_required"}
EOF
"#,
        )
        .await;
        env::set_var("ALLOWED_VAR", "safe");
        env::set_var("BLOCKED_VAR", "hidden");

        let adapter = LocalHarnessAdapter::new(
            LocalHarnessBinding {
                capability: "task.plan".to_string(),
                harness: LocalHarnessKind::ClaudeCode,
                command: "sh".to_string(),
                args: vec![script.display().to_string()],
                required_scopes: Vec::new(),
                lease_required: false,
                workspace_policy: LocalHarnessWorkspacePolicy::EphemeralProposalCopy,
                env_allowlist: vec!["ALLOWED_VAR".to_string()],
                timeout_seconds: 5,
            },
            dir.path().to_path_buf(),
        );

        let result = adapter.run(&planning_action(dir.path())).await.unwrap();
        let json_artifact = result
            .outputs
            .artifacts
            .iter()
            .find(|artifact| artifact.path.ends_with("task_plan.json"))
            .unwrap();
        let artifact: TaskPlanArtifact =
            serde_json::from_slice(&fs::read(&json_artifact.path).await.unwrap()).unwrap();
        assert!(artifact
            .assumptions
            .iter()
            .any(|assumption| assumption.contains("allowed=safe")));
        assert!(artifact
            .assumptions
            .iter()
            .all(|assumption| !assumption.contains("blocked=hidden")));
        assert_eq!(
            artifact.recommended_disposition,
            TaskPlanDisposition::ReviewRequired
        );
    }

    #[tokio::test]
    async fn adapter_rejects_non_json_stdout_for_task_plan() {
        let dir = tempdir().unwrap();
        let script = write_script(
            dir.path(),
            "freeform-plan.sh",
            "#!/bin/sh\nprintf '%s\n' '- inspect files' 'Risk: maybe' 'Assumption: maybe'\n",
        )
        .await;

        let adapter = LocalHarnessAdapter::new(
            LocalHarnessBinding {
                capability: "task.plan".to_string(),
                harness: LocalHarnessKind::Codex,
                command: "sh".to_string(),
                args: vec![script.display().to_string()],
                required_scopes: Vec::new(),
                lease_required: false,
                workspace_policy: LocalHarnessWorkspacePolicy::EphemeralProposalCopy,
                env_allowlist: Vec::new(),
                timeout_seconds: 5,
            },
            dir.path().to_path_buf(),
        );

        let error = adapter.run(&planning_action(dir.path())).await.unwrap_err();
        assert!(error
            .downcast_ref::<LocalHarnessError>()
            .is_some_and(|error| matches!(error, LocalHarnessError::Protocol(message) if message.to_lowercase().contains("json"))));
    }

    #[tokio::test]
    async fn adapter_reports_missing_binary() {
        let dir = tempdir().unwrap();
        let adapter = LocalHarnessAdapter::new(
            LocalHarnessBinding {
                capability: "task.plan".to_string(),
                harness: LocalHarnessKind::Codex,
                command: "__missing_local_harness__".to_string(),
                args: vec!["exec".to_string()],
                required_scopes: Vec::new(),
                lease_required: false,
                workspace_policy: LocalHarnessWorkspacePolicy::Inherit,
                env_allowlist: Vec::new(),
                timeout_seconds: 5,
            },
            dir.path().to_path_buf(),
        );

        let error = adapter.run(&planning_action(dir.path())).await.unwrap_err();
        assert!(error
            .downcast_ref::<LocalHarnessError>()
            .is_some_and(|error| matches!(error, LocalHarnessError::MissingBinary(_))));
    }

    #[tokio::test]
    async fn adapter_reports_timeout() {
        let dir = tempdir().unwrap();
        let script = write_script(
            dir.path(),
            "sleepy-plan.sh",
            "#!/bin/sh\nsleep 2\nprintf '%s\n' '{\"target_files\":[\"src/lib.rs\"],\"ordered_steps\":[{\"title\":\"Inspect scope\",\"detail\":\"Review the objective.\"},{\"title\":\"Draft proposal\",\"detail\":\"Produce the plan.\"}],\"risks\":[\"Timeout risk.\"],\"assumptions\":[\"The request is still valid.\"],\"clarifications_needed\":[],\"required_approvals\":[\"Operator approval before mutation.\"],\"required_evidence\":[],\"test_suggestions\":[\"Validate the plan.\"],\"confidence_summary\":\"medium confidence: timeout path fixture\",\"recommended_disposition\":\"review_required\"}'\n",
        )
        .await;
        let adapter = LocalHarnessAdapter::new(
            LocalHarnessBinding {
                capability: "task.plan".to_string(),
                harness: LocalHarnessKind::ClaudeCode,
                command: "sh".to_string(),
                args: vec![script.display().to_string()],
                required_scopes: Vec::new(),
                lease_required: false,
                workspace_policy: LocalHarnessWorkspacePolicy::Inherit,
                env_allowlist: Vec::new(),
                timeout_seconds: 1,
            },
            dir.path().to_path_buf(),
        );

        let error = adapter.run(&planning_action(dir.path())).await.unwrap_err();
        assert!(error
            .downcast_ref::<LocalHarnessError>()
            .is_some_and(|error| matches!(error, LocalHarnessError::Timeout(1))));
    }

    #[tokio::test]
    async fn adapter_reports_nonzero_exit() {
        let dir = tempdir().unwrap();
        let script = write_script(
            dir.path(),
            "failing-plan.sh",
            "#!/bin/sh\necho 'boom' >&2\nexit 7\n",
        )
        .await;
        let adapter = LocalHarnessAdapter::new(
            LocalHarnessBinding {
                capability: "task.plan".to_string(),
                harness: LocalHarnessKind::Codex,
                command: "sh".to_string(),
                args: vec![script.display().to_string()],
                required_scopes: Vec::new(),
                lease_required: false,
                workspace_policy: LocalHarnessWorkspacePolicy::Inherit,
                env_allowlist: Vec::new(),
                timeout_seconds: 5,
            },
            dir.path().to_path_buf(),
        );

        let error = adapter.run(&planning_action(dir.path())).await.unwrap_err();
        assert!(error
            .downcast_ref::<LocalHarnessError>()
            .is_some_and(|error| matches!(
                error,
                LocalHarnessError::ExitNonZero { status: 7, .. }
            )));
    }

    #[tokio::test]
    async fn adapter_detects_real_workspace_mutation_under_ephemeral_proposal_copy() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("tracked.txt"), "before\n")
            .await
            .unwrap();
        let script = write_script(
            dir.path(),
            "mutating-plan.sh",
            &format!(
                "#!/bin/sh\nprintf 'tampered\\n' > \"{}\"\ncat <<'EOF'\n{{\"target_files\":[\"tracked.txt\"],\"ordered_steps\":[{{\"title\":\"Inspect scope\",\"detail\":\"Review the tracked file.\"}},{{\"title\":\"Draft proposal\",\"detail\":\"Propose the safe follow-up work.\"}}],\"risks\":[\"The original workspace must remain unchanged.\"],\"assumptions\":[\"This path is proposal only.\"],\"clarifications_needed\":[],\"required_approvals\":[\"Operator approval before mutation.\"],\"required_evidence\":[],\"test_suggestions\":[\"Confirm the tracked file hash remains unchanged.\"],\"confidence_summary\":\"medium confidence: mutation detection fixture\",\"recommended_disposition\":\"review_required\"}}\nEOF\n",
                dir.path().join("tracked.txt").display()
            ),
        )
        .await;

        let adapter = LocalHarnessAdapter::new(
            LocalHarnessBinding {
                capability: "task.plan".to_string(),
                harness: LocalHarnessKind::Codex,
                command: "sh".to_string(),
                args: vec![script.display().to_string()],
                required_scopes: Vec::new(),
                lease_required: false,
                workspace_policy: LocalHarnessWorkspacePolicy::EphemeralProposalCopy,
                env_allowlist: Vec::new(),
                timeout_seconds: 5,
            },
            dir.path().to_path_buf(),
        );

        let error = adapter.run(&planning_action(dir.path())).await.unwrap_err();
        assert!(error
            .downcast_ref::<LocalHarnessError>()
            .is_some_and(|error| matches!(error, LocalHarnessError::ProposalMutationDetected(_))));
        let tracked = fs::read_to_string(dir.path().join("tracked.txt"))
            .await
            .unwrap();
        assert_eq!(tracked, "tampered\n");
    }
}
