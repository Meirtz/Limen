# Contributing to Limen

Thanks for your interest. Limen is held to a high engineering bar — it coordinates concurrent
agents over shared mutable state, and the apparatus around it doubles as a reproducibility
backbone — so the workflow is deliberately strict.

## Workflow

`main` is protected. All changes land through pull requests; no one pushes to `main` directly.

1. **Branch** off `main`: `feat/...`, `fix/...`, `docs/...`, `chore/...`, `refactor/...`.
2. **Commit** in focused, self-contained steps with [Conventional Commits](https://www.conventionalcommits.org/)
   messages (`feat(bench): …`, `fix(store): …`).
3. **Open a PR.** CI runs on every PR.
4. **Green CI is required** — formatting, lints, and the full test suite must pass on every
   target before a PR can merge.
5. **Merge** with rebase or squash (merge commits are disabled — history stays linear).

## Before you push

Run the same gate CI runs, from the workspace root:

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

A change is not done until all three are clean. A new behavior is not done until a test covers it.

## CI

`.github/workflows/ci.yml` runs three required checks on each PR: `ubuntu-stable`, `ubuntu-msrv`
(minimum supported Rust version), and `macos-stable`. Each runs fmt-check, clippy (`-D warnings`),
and the test suite.

## Tests that need a network or credentials

Live model/integration tests are marked `#[ignore]` and read configuration from the environment;
they never run in CI and never hardcode endpoints or keys. Run them locally only, e.g.:

```sh
INFERENCE_HUB_API_KEY=… INFERENCE_HUB_BASE_URL=… cargo test -- --ignored
```

Never commit credentials, provider endpoints, or model identifiers — those belong in your local
environment, not the repository.
