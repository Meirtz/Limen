# Contributing to Limen

Thanks for contributing.

Limen is a public Rust alpha project — one small coordination daemon. We want serious maintenance discipline now, even though the public surfaces are still unstable.

## Before You Open A PR

- Read [`../README.md`](../README.md) and the specs under [`../docs/spec/`](../docs/spec/).
- Prefer small, reviewable changes.
- Open an issue first for major behavior changes, public API changes, or architecture shifts.

## Local Checks

Run the same gate CI runs, before pushing:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

A change is not done until all three are clean, and a new behavior is not done until a test covers
it. Never commit credentials, provider endpoints, or model identifiers — those live in your local
environment, not the repository.

## Branch And Merge Policy

`main` is protected: every change lands through a pull request, no one pushes to `main` directly,
and three required CI checks (`ubuntu-stable`, `ubuntu-msrv`, `macos-stable`) must pass before
merge. History stays linear (merge commits are disabled — rebase or squash).

- Branch off `main` with a typed prefix: `feat/…`, `fix/…`, `docs/…`, `chore/…`, `refactor/…`
  (maintainer automation may use `codex/*`).
- Open an issue first for major behavior, public-API, or architecture changes.
- Release tags are cut from `main` only.

## Documentation And Changelog Discipline

- If a change is user-visible, update [`../docs/project/CHANGELOG.md`](../docs/project/CHANGELOG.md).
- If a change affects user-visible behavior, update these in the same PR:
  - [`../README.md`](../README.md) (and [`../README.zh.md`](../README.zh.md))
  - the relevant spec under [`../docs/spec/`](../docs/spec/)
- If a breaking alpha change affects users, include a short migration note in the changelog and docs.

## Public Surface Expectations

The following surfaces are public but still unstable in alpha:

- the `limen` CLI (`serve`, `audit`)
- the MCP tool surface (`limen_acquire` / `limen_write` / `limen_release`)
- the on-disk state format (`.limen/state.db`)
- the `limen` crate

Breaking changes are allowed during alpha, but they must be explicit and documented.
