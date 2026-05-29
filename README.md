# Limen

> **Limen coordinates concurrent, autonomous agents that share mutable state.** It hands each agent an advisory, boundary-scoped, time-bounded **write lease** and keeps a **witnessed audit trail**, so independent agents stop silently overwriting each other's work. It does not run your agents — it keeps them from colliding.
>
> The category is general; the first proving ground is concrete: **multiple AI coding agents — Claude Code, Cursor, Codex, and their sub-agents — sharing one repository.**

![status](https://img.shields.io/badge/status-alpha-orange) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue) ![rust](https://img.shields.io/badge/rust-1.88%2B-orange) ![protocol](https://img.shields.io/badge/MCP-server-black)

**English** · [中文](README.zh.md)

Limen is a single small Rust daemon, exposed as an [MCP](https://modelcontextprotocol.io) server. It sits *beneath* the agents that share a workspace, at the one place they collide: the write. Its posture is deliberate — **servant, not ruler.** Like Git, MCP, OAuth, and OpenTelemetry, it *describes and issues*; it does not govern. It does not start, stop, schedule, route, or supervise any agent.

**The durable idea is general:** coordinate *any* concurrent, autonomous agents over *any* shared, mutable state — through three primitives: an advisory **lease** (authority over a region of a namespace), a **witness** trail (attribution), and a per-agent **identity**. The state is a filesystem today, but nothing in the primitive is bound to files or to coding — the writers can be research, ops, computer-use, or pipeline agents, and the state can be configuration, a key-value store, or infrastructure. Limen ports forty years of distributed-systems concurrency control (leases, advisory locking) into the [zero-trust-for-AI-agents](https://claude.com/blog/zero-trust-for-ai-agents) era, and proves it on the sharpest, most painful instance first: **AI coding agents on a shared repo** (multi-harness coding is the *beachhead*, not the definition).

---

## The problem

Today a single developer's repo can have, at the same moment:

- a **Claude Code** session refactoring `src/auth/`
- a **Cursor** window open with a stale buffer
- a **Codex** task running tests in the background
- 2–3 **sub-agents** that Claude Code spawned, editing different files in parallel

**No layer coordinates them.** The consequences are concrete and reproducible:

| Failure | Consequence |
| --- | --- |
| Two agents write the same file at once | the later write clobbers the earlier — the first agent's work is **silently lost** |
| Agent A changes a function signature; Agent B still calls the old one | **build break** / tests fail |
| Cursor saves a buffer based on an old version | overwrites a change another agent just committed |
| A background agent deletes a file the foreground is editing | the editor writes back a "deleted" ghost |
| A bug appears and `git blame` shows only the human | **no attribution** — which agent, which prompt? |

This is the classic **lost-update / write-skew** problem (Berenson et al., SIGMOD 1995) — except databases and version control *earned* their safety with locks, snapshots, and merge discipline, and agents inherited none of it. They are non-deterministic writers, never written to take a lock, blind to each other. Limen ports the cure to the layer where the writers are agents and the shared state is a filesystem nobody locked.

---

## How it works

Two independent harnesses, sharing one repo, coordinating through one threshold — with almost no change to either:

```
   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
   │ Claude Code │    │   Cursor    │    │  Codex CLI  │
   │  + subagents│    │             │    │             │
   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
          │ MCP (stdio)      │ MCP              │ MCP
          ▼                  ▼                  ▼
   ╔══════════════════════════════════════════════════╗
   ║   limen serve   (stdio MCP server)               ║
   ║   tools: limen_acquire / limen_write / release    ║
   ╠══════════════════════════════════════════════════╣
   ║   arbitration (lease conflicts) · witness (audit) ║
   ╠══════════════════════════════════════════════════╣
   ║   SQLite (.limen/state.db)  —  leases + writes    ║
   ╚══════════════════════════════════════════════════╝
```

1. Claude Code wants `src/auth/` → `limen_acquire("src/auth/", "write", "claude-code:sess-A")` → gets lease **L1** (TTL 5 min).
2. Codex wants `src/auth/login.rs` → `limen_acquire(...)` → Limen sees L1 covers that path → returns **conflict** ("held by claude-code:sess-A"). Codex moves to non-conflicting `src/parser/`, or waits.
3. Claude Code writes via `limen_write(L1, "src/auth/login.rs", …)` → Limen checks the path is in scope, writes the file, and records a witness (content hash, time, agent).
4. Claude Code calls `limen_release(L1)` → `src/auth/` unlocks; Codex can take it.
5. Any time, `limen audit` shows every write: path, bytes, hash, which agent, which lease.

---

## Quickstart

Limen is alpha; build it from source:

```bash
cargo install --path crates/limen     # or: cargo build -p limen --release
```

Point any MCP-speaking harness at it. For **Claude Code** (`settings.json`):

```json
{
  "mcpServers": {
    "limen": {
      "command": "limen",
      "args": ["serve", "--db", ".limen/state.db"]
    }
  }
}
```

That's it — Claude Code, Cursor, Codex, and others can now call three tools:

| Tool | Inputs | Returns |
| --- | --- | --- |
| `limen_acquire` | `path_pattern`, `intent` (`read`\|`write`\|`propose`), `agent_label`, `ttl_ms?` | `lease_id`, `expires_at` — or a conflict |
| `limen_write` | `lease_id`, `path`, `content` | `content_hash`, `bytes_written` |
| `limen_release` | `lease_id` | `released: bool` |

Inspect what happened at any time:

```bash
limen audit --db .limen/state.db          # active leases + recent witnessed writes
limen attribute src/auth/login.rs         # who changed this path, when, under which lease
```

---

## Concepts

Limen has exactly five first-class concepts, each backed by a real type in the code — no invented vocabulary. Full definitions in [`docs/spec/glossary.md`](docs/spec/glossary.md).

| Concept | Meaning | Today |
| --- | --- | --- |
| **identity** | who is requesting | plaintext agent label (`claude-code:sess-A`); ed25519 planned |
| **boundary** (a *limen*) | a region of the namespace | literal path or directory prefix (`src/auth/`) |
| **lease** | time-bounded authority over a boundary | intent + TTL (5 min) + state |
| **intent** | what the holder means to do | `read` / `write` / `propose` |
| **witness** | recorded evidence of a write | path, bytes, SHA-256, time, agent |

**Conflict rule** (overlapping boundaries): `write × write` conflicts · `write × read` conflicts (reader yields) · `read × read` is fine · `propose` never conflicts.

---

## Scope: what Limen is — and is not

Limen's predecessor died of conceptual sprawl; staying small is the whole point. Details in [`docs/spec/boundaries.md`](docs/spec/boundaries.md).

| | |
| --- | --- |
| ✅ **is** | an advisory lease manager + write mediator + witness, over MCP; harness-neutral; value scales with how many harnesses run at once |
| 🚫 **is not (yet)** | mandatory enforcement (it's advisory — it issues and witnesses, it doesn't intercept); a resident cluster service; multi-machine; cryptographic identity |
| ⛔ **is not (ever)** | an agent runtime / orchestrator / scheduler; a governance / policy / "law" layer; a model or harness competitor |

It coordinates **shared state**; it does not orchestrate agents. The line: if a feature only makes sense once Limen is *in charge* of an agent, it's out of scope — Limen is never in charge.

---

## Why now

- **Harness explosion.** Claude Code, Cursor, Codex, Gemini CLI, Copilot CLI, aider, cline — running 2–3 at once is already normal.
- **Sub-agent fan-out.** Mainstream harnesses spawn parallel sub-agents (Claude Code's `Agent` tool). A single harness is *already* multi-writer.
- **Nobody owns this layer.** Harness vendors compete on the IDE experience; Anthropic is articulating the [zero-trust](https://claude.com/blog/zero-trust-for-ai-agents) paradigm. *"Let them coexist peacefully in one workspace"* is empty.

---

## The thesis we intend to prove

Limen makes a claim an experiment can prove wrong — which is what keeps it honest rather than rhetorical:

> At a fixed degree of parallelism N, **`Par-N-Limen` Pareto-dominates `Par-N-Naive`** on (wall-clock × pass@1) — no worse on either, strictly better on at least one — while strictly winning on lost-edit-lines, build-break-rate, and attribution.

| Arm | Setup |
| --- | --- |
| **Seq-1** | one agent, sequential — correctness ceiling / latency baseline |
| **Par-N-Naive** | N concurrent agents, no coordination |
| **Par-N-Limen** | N concurrent agents behind advisory leases |

Metrics: **pass@1** (and the stricter **pass^k** for reliability under repeated concurrent runs), **wall-clock**, **lost-edit-lines**, **build-break-rate**, **attribution-accuracy**. Full design, comparables, and threats to validity: [`docs/spec/related-work.md`](docs/spec/related-work.md). *(This is the evaluation plan, not measured results.)*

---

## Lineage

Limen stands on settled ground, not on LLM-orchestration fashion — because reasoning is volatile and coordination is not.

- **Concurrency control & leases:** Gray & Cheriton, *Leases* (SOSP 1989); Burrows, *Chubby* — advisory locking (OSDI 2006); ZooKeeper, etcd, Consul; POSIX `flock`; the lost-update/write-skew taxonomy (Berenson et al., SIGMOD 1995).
- **Shared-state coordination:** blackboard systems (Hearsay-II), tuple spaces (Linda); the CRDT/OT *merge-after-write* family Limen contrasts with by *preventing before the write*.
- **Zero trust for AI agents:** Anthropic's [Zero Trust for AI agents](https://claude.com/blog/zero-trust-for-ai-agents), grounded in NIST SP 800-207 and least privilege (Saltzer & Schroeder 1975).

Annotated, verified bibliography: [`docs/references.md`](docs/references.md). Prior art in this exact space exists (notably MCP Agent Mail) — Limen's claim is the *generalized category plus a rigorous experiment*, not "first."

---

## Status

Limen is **alpha** and honest about it.

| Surface | Status |
| --- | --- |
| MVP (lease + write + release + audit, stdio MCP) | implemented |
| enforcement | **advisory only** — agents can bypass; witness still attributes |
| identity | plaintext label (ed25519 signing planned) |
| scope | single machine, single workspace |
| boundary matching | literal path / directory prefix (no globs yet) |

Lineage: AgentGraph → Crawfish → **Limen** (a refactor from an over-reaching "control plane for governed swarms" down to one sharp coordination primitive).

---

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — product requirements
- [`docs/spec/philosophy.md`](docs/spec/philosophy.md) — why this shape is correct ([中文](docs/spec/philosophy.zh.md))
- [`docs/spec/boundaries.md`](docs/spec/boundaries.md) — what Limen is and is not ([中文](docs/spec/boundaries.zh.md))
- [`docs/spec/glossary.md`](docs/spec/glossary.md) — canonical vocabulary ([中文](docs/spec/glossary.zh.md))
- [`docs/spec/related-work.md`](docs/spec/related-work.md) — related work & experimental framing
- [`docs/references.md`](docs/references.md) — annotated bibliography

Contribution and security policy: [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) · [`.github/SECURITY.md`](.github/SECURITY.md).

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.
