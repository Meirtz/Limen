# Limen

> **Limen coordinates concurrent, autonomous agents that share mutable state.** It hands each agent an advisory, boundary-scoped, time-bounded **lease** and keeps a **witnessed audit trail**, so independent agents stop silently overwriting each other's work. It does not run your agents — it keeps them from colliding.

![status](https://img.shields.io/badge/status-alpha-orange) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue) ![rust](https://img.shields.io/badge/rust-1.88%2B-orange) ![protocol](https://img.shields.io/badge/MCP-server-black)

**English** · [中文](README.zh.md)

Limen is a single small Rust daemon, exposed as an [MCP](https://modelcontextprotocol.io) server. It sits *beneath* the agents that share state, at the one place they collide: the change. Its posture is deliberate — **servant, not ruler.** Like Git, MCP, OAuth, and OpenTelemetry, it *describes and issues*; it does not govern. It does not start, stop, schedule, route, or supervise any agent.

The model is general. A **lease** is time-bounded authority over a **region** of a **namespace**; a **witness** records every mediated change against an **identity**. None of it is bound to files — a namespace is any addressable space of mutable resources (a filesystem, a key-value store, a config tree, a set of cloud objects), and the agents can be coding, research, ops, or computer-use agents, or plain pipelines. Limen ports forty years of distributed-systems concurrency control — leases, advisory locking — into the [zero-trust-for-AI-agents](https://claude.com/blog/zero-trust-for-ai-agents) era.

It ships today with one resource implemented — a **filesystem** — and one example that makes the problem vivid: several AI coding agents sharing a repository.

---

## The problem

When several autonomous agents change shared state with nothing coordinating them, they reintroduce the classic concurrency hazards — the ones databases and version control spent decades taming:

| Hazard | What happens |
| --- | --- |
| **Lost update** | two agents change the same resource; the later write silently erases the earlier agent's work |
| **Broken invariant** | one agent changes something another still relies on; the combined state is inconsistent — a build breaks, a schema mismatches |
| **Stale / torn read** | an agent acts on a snapshot another has already moved past |
| **No attribution** | something breaks and nothing records *which* agent changed *what*, under whose intent |

Databases and version control *earned* their safety with locks, snapshots, and merge discipline. Today's agents inherited none of it: they are non-deterministic writers, never written to take a lock, blind to each other. Limen ports the cure to the layer where the writers are agents and the shared state is something nobody locked.

**A concrete example** — one developer's repository, at a single moment:

- a **Claude Code** session refactoring `src/auth/`
- a **Cursor** window open on a stale buffer
- a **Codex** task running tests in the background
- 2–3 **sub-agents** Claude Code spawned, editing different files in parallel

Nothing coordinates them, so every hazard above is concrete and reproducible — and `git blame` shows only the human.

---

## How it works

The mechanism is the same for any resource:

1. **acquire** a lease over a region under an intent (`read` / `write` / `propose`) — or learn it conflicts with a lease someone already holds
2. **write** within the lease — Limen checks the target lies inside the region, applies the change, and records a witness (bytes, content hash, time, agent)
3. **release** — the region frees for the next holder

Conflicts are decided by region overlap: `write × write` and `write × read` conflict, `read × read` is fine, and `propose` never conflicts. Every lease carries a TTL and auto-expires, so a crashed agent can't deadlock the namespace.

**Example — two coding harnesses on one repo** (the filesystem resource):

```
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │   Agent A    │   │   Agent B    │   │   Agent C    │
   │ (Claude Code)│   │   (Cursor)   │   │   (Codex)    │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │ MCP (stdio)      │ MCP              │ MCP
          ▼                  ▼                  ▼
   ╔══════════════════════════════════════════════════╗
   ║   limen serve   (stdio MCP server)               ║
   ║   tools: limen_acquire / limen_write / release    ║
   ╠══════════════════════════════════════════════════╣
   ║   arbitration (lease conflicts) · witness (audit) ║
   ╠══════════════════════════════════════════════════╣
   ║   resource: filesystem     ·     SQLite state.db  ║
   ╚══════════════════════════════════════════════════╝
```

1. Agent A wants `src/auth/` → `limen_acquire("src/auth/", "write", "claude-code:sess-A")` → lease **L1** (TTL 5 min).
2. Agent C wants `src/auth/login.rs` → **conflict** (held by A). It moves to `src/parser/`, or waits.
3. A writes via `limen_write(L1, "src/auth/login.rs", …)` → in region, applied, witnessed.
4. A calls `limen_release(L1)` → the region frees; C can take it.
5. `limen audit` shows every change: region, bytes, hash, which agent, which lease.

---

## Quickstart

Limen is alpha; build it from source, then initialize your workspace:

```bash
cargo install --path crates/limen        # or: cargo build -p limen --release
cd your-project && limen init            # create .limen/ and print the MCP config
```

`limen init` prints a ready-to-paste config. Point any MCP-speaking host at it — for example, **Claude Code** (`settings.json`):

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

That's it — Claude Code, Cursor, Codex, and any other MCP host can now call three tools (parameters shown for the filesystem resource):

| Tool | Inputs | Returns |
| --- | --- | --- |
| `limen_acquire` | `path_pattern` (region), `intent` (`read`\|`write`\|`propose`), `agent_label`, `ttl_ms?` | `lease_id`, `expires_at` — or a conflict |
| `limen_write` | `lease_id`, `path` (target), `content` | `content_hash`, `bytes_written` |
| `limen_release` | `lease_id` | `released: bool` |
| `limen_renew` | `lease_id`, `ttl_ms?` | `expires_at` (extended) |

Inspect what happened at any time:

```bash
limen audit --db .limen/state.db          # active leases + recent witnessed changes
limen attribute src/auth/login.rs         # who changed this, when, under which lease
```

**Optional — cryptographic identity.** Register an agent to upgrade its label from *asserted* to *ed25519-verified*:

```bash
limen register claude-code:sess-A                # generate a keypair; print the public key
limen sign claude-code:sess-A src/auth/ write    # signature to pass as limen_acquire's `signature`
```

Once registered, that agent's `limen_acquire` must carry a valid `signature` (the lease is then a bearer capability for the writes that follow); unregistered labels keep the plaintext path.

---

## Concepts

Limen has a small, general vocabulary — each term backed by a real type in the code, no invented words. Full definitions in [`docs/spec/glossary.md`](docs/spec/glossary.md).

| Concept | Meaning | Filesystem resource (today) |
| --- | --- | --- |
| **namespace** | the addressable space of mutable resources being coordinated | the workspace's files |
| **region** (a *limen*) | a slice of the namespace a lease covers | a path or directory prefix (`src/auth/`) |
| **identity** | who is requesting | plaintext label, or a registered **ed25519** key (`limen register`) the agent signs each acquire with |
| **lease** | time-bounded authority over a region | intent + TTL (5 min) + state |
| **intent** | what the holder means to do | `read` / `write` / `propose` |
| **witness** | recorded evidence of a mediated change | target, bytes, SHA-256, time, agent |

**Conflict rule** (overlapping regions): `write × write` conflicts · `write × read` conflicts (reader yields) · `read × read` is fine · `propose` never conflicts.

---

## Scope: what Limen is — and is not

Limen's predecessor died of conceptual sprawl; staying small is the whole point. The concept is general, but the surface stays one sharp primitive. Details in [`docs/spec/boundaries.md`](docs/spec/boundaries.md).

| | |
| --- | --- |
| ✅ **is** | an advisory lease manager + change mediator + witness, over MCP; agent-neutral and resource-pluggable; value scales with how many agents share the namespace |
| 🚫 **is not (yet)** | mandatory enforcement (it's advisory — it issues and witnesses, it doesn't intercept); more than one resource backend; multi-machine; cryptographic identity |
| ⛔ **is not (ever)** | an agent runtime / orchestrator / scheduler; a governance / policy / "law" layer; a model or harness competitor |

It coordinates **shared state**; it does not orchestrate agents. The line: if a feature only makes sense once Limen is *in charge* of an agent, it's out of scope — Limen is never in charge.

---

## Why now

- **Agents that write are multiplying.** Coding harnesses (Claude Code, Cursor, Codex, Gemini CLI, aider), research and ops agents, computer-use agents — more of them act on shared state every month.
- **Fan-out is built in.** Mainstream harnesses spawn parallel sub-agents (Claude Code's `Agent` tool). A single host is *already* multi-writer.
- **Nobody owns this layer.** Vendors compete on the agent loop; Anthropic is articulating the [zero-trust](https://claude.com/blog/zero-trust-for-ai-agents) paradigm. *"Let independent agents share one namespace safely"* is empty.

---

## The thesis we intend to prove

Limen makes a claim an experiment can prove wrong — which keeps it honest rather than rhetorical. We test it on the filesystem example (concurrent coding agents on one repo), because that is where the pain is measurable today:

> Coordination's value is **conditional on task coupling**, and reliability is where uncoordinated concurrency fails first. As writers (N) and coupling rise, naive concurrency's **pass^k** (all of k repeated runs succeed) collapses super-linearly through lost edits and broken builds; advisory coordination recovers most of that cost **below a coupling threshold τ** (a Pareto-improvement — added safety at ~no time cost), while **above τ** the safety gain persists but the wall-clock advantage inverts.

| Arm | Setup |
| --- | --- |
| **Seq-1** | one agent, sequential — correctness ceiling / latency baseline |
| **Par-N-Naive** | N concurrent agents, no coordination |
| **Par-N-Placebo** | N agents behind the witnessed wrapper but **without** arbitration — isolates the wrapper from the coordination |
| **Par-N-Limen** | N concurrent agents behind advisory region leases |
| **Par-N-Limen+Deps** | adds an advisory write×read round, recovering cross-file coupling that region leases alone cannot |

Metrics: **pass@1** (and the stricter **pass^k** for reliability under repeated concurrent runs), **wall-clock**, **lost-edit-lines**, **build-break-rate**, **attribution-accuracy**. The apparatus is implemented in [`crates/limen-bench`](crates/limen-bench) (the arms, a coordination-independent oracle, a coupling-class task family, and `pilot` / `sweep` / `analyze` subcommands); the full executable design is in [`docs/experiments.md`](docs/experiments.md), related work & framing in [`docs/spec/related-work.md`](docs/spec/related-work.md). *(The pre-registered study at scale is future work; no headline numbers are claimed here.)*

---

## Lineage

Limen stands on settled ground, not on LLM-orchestration fashion — because reasoning is volatile and coordination is not.

- **Concurrency control & leases:** Gray & Cheriton, *Leases* (SOSP 1989); Burrows, *Chubby* — advisory locking (OSDI 2006); ZooKeeper, etcd, Consul; POSIX `flock`; the lost-update / write-skew taxonomy (Berenson et al., SIGMOD 1995).
- **Shared-state coordination:** blackboard systems (Hearsay-II), tuple spaces (Linda); the CRDT/OT *merge-after-write* family Limen contrasts with by *preventing before the change*.
- **Zero trust for AI agents:** Anthropic's [Zero Trust for AI agents](https://claude.com/blog/zero-trust-for-ai-agents), grounded in NIST SP 800-207 and least privilege (Saltzer & Schroeder 1975).

Annotated, verified bibliography: [`docs/references.md`](docs/references.md). Prior art in this space exists (notably MCP Agent Mail) — Limen's claim is the *generalized model plus a rigorous experiment*, not "first."

---

## Status

Limen is **alpha** and honest about it.

| Surface | Status |
| --- | --- |
| MVP (lease + write + release + audit, stdio MCP) | implemented |
| resources | one (filesystem); the model is resource-pluggable |
| enforcement | **advisory only** — agents can bypass; witness still attributes |
| identity | plaintext by default; opt-in **ed25519** signed identity (`limen register` / `limen sign`) |
| scope | single machine, single namespace |
| region matching | literal path / directory prefix (no globs yet) |
| experiment apparatus | [`crates/limen-bench`](crates/limen-bench) — arms, coordination-independent oracle, coupling-class tasks, `pilot`/`sweep`/`analyze` |

Lineage: AgentGraph → Crawfish → **Limen** (a refactor from an over-reaching "control plane for governed swarms" down to one general coordination primitive).

---

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — product requirements
- [`docs/spec/philosophy.md`](docs/spec/philosophy.md) — why this shape is correct ([中文](docs/spec/philosophy.zh.md))
- [`docs/spec/boundaries.md`](docs/spec/boundaries.md) — what Limen is and is not ([中文](docs/spec/boundaries.zh.md))
- [`docs/spec/glossary.md`](docs/spec/glossary.md) — canonical vocabulary ([中文](docs/spec/glossary.zh.md))
- [`docs/spec/related-work.md`](docs/spec/related-work.md) — related work & experimental framing
- [`docs/experiments.md`](docs/experiments.md) — hero experiment design
- [`docs/references.md`](docs/references.md) — annotated bibliography

Contribution and security policy: [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) · [`.github/SECURITY.md`](.github/SECURITY.md).

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.
