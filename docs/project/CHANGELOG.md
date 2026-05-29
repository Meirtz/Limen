# Changelog

Alpha changelog discipline: user-visible changes are recorded here before merge.

## Unreleased

### Added

- `limen`: a workspace coordination daemon, exposed as an MCP server, that issues
  advisory, boundary-scoped, time-bounded **write leases** and records a
  **witnessed** audit trail so concurrent AI agents stop overwriting each other.
  - CLI: `limen serve` (stdio MCP server), `limen audit` (active leases + recent writes),
    `limen attribute <path>` (per-agent attribution for a path), `limen init` (create
    `.limen/` and print ready-to-paste MCP config for Claude Code / Cursor / Codex)
  - end-to-end smoke test that drives the real binary through the full MCP lifecycle over stdio
  - MCP tools: `limen_acquire`, `limen_write`, `limen_release`
  - SQLite-backed leases and write audit; typed conflict matrix
    (`write × write` and `write × read` conflict; `read × read` and `propose` do not)
- Limen spec set: `docs/spec/philosophy.md`, `boundaries.md`, `glossary.md`
  (English canonical + `.zh.md` Chinese companions) and a `related-work.md` paper
  scaffold; `docs/PRD.md`; and a verified, annotated `docs/references.md`.
- README in English (`README.md`) and Chinese (`README.zh.md`).
- a general, **resource-pluggable** core: the lease / conflict / witness logic is
  resource-agnostic (`store.rs` coordinates regions of a namespace), with the
  filesystem as the one shipped `Resource` (`resource.rs`). Adding another backend
  is a new `Resource`, not a rewrite.
- `docs/experiments.md`: the executable hero-experiment design (Pareto-dominance thesis).

### Hardened (from an adversarial code review)

- **Security:** mediated writes refuse `..` path traversal, so a write-lease holder
  cannot escape its region (the escape also falsified the audit).
- **Correctness:** a typed conflict matrix replaces the stringly-typed check — an
  existing `propose` lease no longer wrongly blocks a later write.
- **Robustness:** the MCP stdio loop survives a non-UTF-8 line (per-message `-32700`)
  instead of terminating; batches / bare scalars / missing-method return `-32600`;
  regions are validated (reject empty / bare `/` / `..`); SQLite `busy_timeout` is set;
  a pre-epoch clock warns instead of silently clamping.

### Changed

- **Project refocus: Crawfish → Limen.** The multi-crate "control plane for governed
  agent swarms" is collapsed into a single coordination primitive. Posture shifts from
  "control plane / governance / law layer" to advisory, servant-not-ruler coordination
  (Git/MCP/OAuth/OpenTelemetry-style: it issues and witnesses; it does not govern).
- Removed the Crawfish runtime, CLI, and harness/MCP/OpenClaw/A2A/store/types crates;
  the hero-swarm and remote-swarm examples; the OpenClaw inbound bridge; and the
  Crawfish-era specs (`vision`, `architecture`, `v0.1-plan`, `experimental`) and DOCX
  exports/archive. The workspace is now the single `crates/limen`.
- `.github` governance docs and CI realigned to the single-crate Limen workspace.

> Prior Crawfish history (its full feature changelog and the retired runtime) remains
> in git history and on the `main` lineage before this refactor.
