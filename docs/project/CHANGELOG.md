# Changelog

Alpha changelog discipline: user-visible changes are recorded here before merge.

## 0.2.0-alpha.1 — 2026-05-30

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
- **lease renewal** (`limen_renew` / `Store::renew_lease`): extend a held lease before its
  TTL expires — a keepalive in the etcd / Consul / Gray-Cheriton lineage.
- **opt-in ed25519 signed identity** (`limen register` / `limen sign`, `agents` table):
  a registered agent must sign its `acquire`, verified server-side; the lease is then a
  bearer capability for the writes that follow. Unregistered labels keep the plaintext
  advisory path, so the simple workflow is unchanged.
- `Store::dependents(region)` — active read leases overlapping a region, surfacing the
  write×read coupling advisorily (it never blocks the write) — and `Store::sweep_expired()`
  for lease GC. Audit/attribution queries now use a deterministic total order.
- **measurement apparatus** (`crates/limen-bench`, internal): experimental arms
  (naive / placebo / Limen / dependency-aware), a coordination-independent oracle, a
  coupling-class toy-task family, a Monte-Carlo of the interference model, and
  `pilot` / `sweep` / `analyze` subcommands over any OpenAI-compatible endpoint (endpoint
  and credentials read from the environment — never committed).
- `#![forbid(unsafe_code)]` across the workspace.
- **second shipped `Resource`: a Redis-backed KV store** (`RedisKvResource`, behind the optional
  `redis` feature — the core stays dependency-lean by default). Regions are key prefixes; a mediated
  change `SET`s the key. Lets Limen coordinate concurrent agents over a shared Redis namespace (e.g.
  shared agent memory) with the same leases + witness as the filesystem — the first non-filesystem
  backend, proving the resource-agnostic core in shipped code. Region logic is unit-tested; the live
  round-trip is an `#[ignore]`d test behind `REDIS_URL`. `limen serve --resource redis --redis-url
  redis://…` runs the daemon over Redis (clear error if the binary was built without `--features
  redis`); the filesystem remains the default.
- **machine-readable witness export**: `limen audit --json` and `limen attribute <path> --json`
  emit the same facts as the text output inside versioned envelopes (`limen.audit/v1` /
  `limen.attribute/v1`) — active leases (id, region, intent, agent, expiry) and witnessed writes
  (time, target, bytes, full SHA-256, lease id) — so an external verifier can consume the audit
  trail without linking the crate or scraping the db. Same queries, second renderer; no new data
  collection. Versioning rule: additive fields bump nothing (consumers ignore unknown fields);
  renames/removals bump `/v1` → `/v2`. The shapes are pinned by an integration test against the
  shipped binary.

### Process

- `main` is protected: changes land via pull request, with three required CI checks
  (`ubuntu-stable`, `ubuntu-msrv`, `macos-stable`) and linear history. See
  [`.github/CONTRIBUTING.md`](../../.github/CONTRIBUTING.md).

### Hardened (from an adversarial code review)

- **Region soundness:** filesystem regions are lexically normalized (drop `.`/empty
  components, reject `..`), so `src/` and `./src/` are recognized as one region and
  aliased leases now conflict.

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
