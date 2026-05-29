# Changelog

Alpha changelog discipline: user-visible changes are recorded here before merge.

## Unreleased

### Added

- `limen`: a workspace coordination daemon, exposed as an MCP server, that issues
  advisory, boundary-scoped, time-bounded **write leases** and records a
  **witnessed** audit trail so concurrent AI agents stop overwriting each other.
  - CLI: `limen serve` (stdio MCP server), `limen audit` (active leases + recent writes)
  - MCP tools: `limen_acquire`, `limen_write`, `limen_release`
  - SQLite-backed leases and write audit; typed conflict matrix
    (`write × write` and `write × read` conflict; `read × read` and `propose` do not)
- Limen spec set: `docs/spec/philosophy.md`, `boundaries.md`, `glossary.md`
  (English canonical + `.zh.md` Chinese companions) and a `related-work.md` paper
  scaffold; `docs/PRD.md`; and a verified, annotated `docs/references.md`.
- README in English (`README.md`) and Chinese (`README.zh.md`).

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
