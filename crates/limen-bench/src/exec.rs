//! Test executors — run a task's `test_cmd` inside a materialized repo and report pass/fail.
//!
//! `Local` runs the command on the host (fast; for trusted toy tasks and development). `Docker`
//! runs it inside a `--network none` container — the isolation required before executing
//! untrusted, model-generated code at scale. Both speak the same [`ExecOutcome`], so the runner
//! is agnostic to which one scores a run.

use std::path::Path;
use tokio::process::Command;

/// The result of running a `test_cmd`. `passed` is the exit-code-0 verdict the pilot keys on.
#[derive(Clone, Debug)]
pub struct ExecOutcome {
    pub passed: bool,
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

/// Where a task's `test_cmd` runs.
#[derive(Clone, Debug)]
pub enum Executor {
    /// Run on the host, cwd = repo dir. Trusted code only.
    Local,
    /// Run inside `docker run --rm --network none -v <repo>:/work -w /work <image> <cmd>`.
    Docker { image: String },
}

impl Executor {
    /// Run `test_cmd` against the repo at `repo_dir`. The repo root is placed on `PYTHONPATH`
    /// so package imports resolve from the materialized tree.
    pub async fn run(&self, repo_dir: &Path, test_cmd: &[String]) -> std::io::Result<ExecOutcome> {
        if test_cmd.is_empty() {
            return Ok(ExecOutcome {
                passed: false,
                exit_code: None,
                stdout: String::new(),
                stderr: "empty test_cmd".into(),
            });
        }
        let output = match self {
            Executor::Local => {
                Command::new(&test_cmd[0])
                    .args(&test_cmd[1..])
                    .current_dir(repo_dir)
                    .env("PYTHONPATH", repo_dir)
                    .env("PYTHONDONTWRITEBYTECODE", "1")
                    .output()
                    .await?
            }
            Executor::Docker { image } => {
                let mount = format!("{}:/work", repo_dir.display());
                let mut cmd = Command::new("docker");
                cmd.args([
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    &mount,
                    "-w",
                    "/work",
                    "-e",
                    "PYTHONPATH=/work",
                    image,
                ]);
                cmd.args(test_cmd);
                cmd.output().await?
            }
        };
        Ok(ExecOutcome {
            passed: output.status.success(),
            exit_code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

/// Materialize a `(relative path, content)` tree under `root`, creating parent dirs.
pub fn materialize<'a>(
    root: &Path,
    files: impl IntoIterator<Item = (&'a str, &'a str)>,
) -> std::io::Result<()> {
    for (rel, content) in files {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&path, content)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // A correct solution to the shared-region toy task passes its test_cmd on the host.
    #[tokio::test]
    async fn local_executor_passes_a_correct_repo() {
        let tmp = tempfile::tempdir().unwrap();
        materialize(
            tmp.path(),
            [
                ("mathx/__init__.py", ""),
                (
                    "mathx/ops.py",
                    "def add(a, b): return a + b\ndef mul(a, b): return a * b\n",
                ),
            ],
        )
        .unwrap();
        let test_cmd = vec![
            "python".to_string(),
            "-c".to_string(),
            "from mathx.ops import add, mul; assert add(2,3)==5 and mul(2,3)==6".to_string(),
        ];
        let out = Executor::Local.run(tmp.path(), &test_cmd).await.unwrap();
        assert!(out.passed, "correct repo should pass: {}", out.stderr);
        assert_eq!(out.exit_code, Some(0));
    }

    // A repo missing one contribution (the lost-update outcome) fails the same test_cmd.
    #[tokio::test]
    async fn local_executor_fails_a_broken_repo() {
        let tmp = tempfile::tempdir().unwrap();
        materialize(
            tmp.path(),
            [
                ("mathx/__init__.py", ""),
                ("mathx/ops.py", "def add(a, b): return a + b\n"), // mul lost
            ],
        )
        .unwrap();
        let test_cmd = vec![
            "python".to_string(),
            "-c".to_string(),
            "from mathx.ops import add, mul".to_string(),
        ];
        let out = Executor::Local.run(tmp.path(), &test_cmd).await.unwrap();
        assert!(!out.passed, "missing contribution should fail import");
    }

    #[tokio::test]
    async fn empty_test_cmd_is_a_failure() {
        let tmp = tempfile::tempdir().unwrap();
        let out = Executor::Local.run(tmp.path(), &[]).await.unwrap();
        assert!(!out.passed);
    }
}
