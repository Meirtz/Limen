# Limen — Boundaries & Scope

> Canonical terminology lives in [`glossary.md`](glossary.md) · The intellectual case lives in [`philosophy.md`](philosophy.md) · Sources live in [`../references.md`](../references.md).
>
> 中文伴随版：[`boundaries.zh.md`](boundaries.zh.md)。English is canonical.

A project's boundaries are as load-bearing as its features. Limen's predecessor died of conceptual sprawl; this document exists to keep that from recurring. It states what Limen **is**, what it **deliberately is not** (now and ever), and exactly where it sits relative to its neighbors.

---

## The category, and the beachhead

**General category (the durable definition).** Limen coordinates **concurrent, autonomous agents operating on shared, mutable state**, via three primitives:

- an advisory, boundary-scoped, time-bounded **lease** (authority to mutate a region of a namespace),
- a **witness** trail (attribution and forensics over every mediated write),
- and a per-agent **identity**.

The boundary — *limen*, Latin for *threshold* — is a region of a namespace. The category is rooted in distributed-systems concurrency control and zero-trust security, not in LLM orchestration.

**Beachhead (today's first instantiation — one case, not the definition).** Multiple AI coding harnesses (Claude Code, Cursor, Codex, Gemini CLI) and their parallel sub-agents sharing one git working tree on one machine, integrated as an MCP server, advisory-first. Here the shared state is the filesystem and the regions are path patterns — but the primitive is not specific to files or to coding.

## In one sentence

> **Limen is a workspace coordination daemon.** When multiple AI agents share a workspace, Limen gives each a signed-ish identity, a boundary-scoped lease, and a witnessed audit trail — so they don't step on each other. It does not run your agents; it keeps them from colliding.

## Posture: servant, not ruler

The widely-adopted infrastructure in this space never claims to *rule* anything. Limen takes the same stance, and explicitly rejects the "control plane / governance / law layer" framing of its predecessor.

| It says | "I do this" | "I do **not** do this" |
| --- | --- | --- |
| Git | I track diffs | I manage your code |
| MCP | I define the tool-call wire format | I control your agent |
| OpenTelemetry | I define the span format | I supervise your system |
| OAuth | I define the authorization handshake | I decide who you are |
| **Limen** | **I issue leases and witness writes** | **I govern your agent swarm** |

---

## What Limen is

- A single small daemon (one Rust crate) exposed as an **MCP server** over stdio.
- A **lease manager**: atomic conflict-checked `acquire`, with automatic TTL expiry, `release`, and a typed conflict matrix.
- A **write mediator + witness**: `write` validates the lease, performs the mutation, and records `(lease_id, path, bytes, SHA-256, timestamp, agent label)`.
- An **attribution surface**: given a path, who changed it, when, under which lease.
- **Harness-neutral**: it assumes no specific harness and favors no model.

## What Limen is not — now (MVP-stage non-goals)

These are out of scope for the current line, but not forever.

- **Mandatory mediation.** The MVP is advisory: it issues leases and witnesses writes, but does not intercept writes that bypass it. Enforcement (and passive bypass detection) is a later, opt-in mode.
- **A resident platform / cluster service / dashboard.** Limen is a process that lives and dies with the harness session over stdio, not an etcd-style always-on cluster, and not a GUI. CLI first.
- **Multi-machine / federation / multi-tenant.** The MVP coordinates one machine, one workspace. (The lineage — Chubby, etcd — *is* distributed, so this is a generalization axis, not a wall.)
- **Cryptographic identity.** Agent identity is a plaintext label today; ed25519-signed identity is planned, not present. Until then, Limen is capability-*inspired*, not capability-*enforced*.

## What Limen is not — ever (category non-goals)

These contradict the category and will not be added.

- **An agent runtime / orchestrator / scheduler.** Limen never starts, stops, schedules, routes, or supervises an agent. If a feature only makes sense once Limen is "in charge" of the agents, it is out of scope — Limen is never in charge.
- **A governance / policy / "law" layer.** No doctrine, jurisdiction, treaty, federation, evidence-admissibility, or approval-gating machinery. (These were its predecessor's conceptual inflation; all are dropped.)
- **A model or harness competitor.** Limen does not try to make agents smarter or to win "which harness is best." It is neutral infrastructure beneath all of them, and its value *increases* with the number of harnesses in play.
- **A consumer assistant, an ETL/batch engine, a workflow engine, a Kubernetes/Temporal replacement, or a benchmark-maximization framework.**

---

## The five first-class concepts

Limen has exactly five concepts, each backed by a real type in the implementation — no invented vocabulary.

| Concept | Meaning | Beachhead form (code) | General form |
| --- | --- | --- | --- |
| **Identity** (`agent_label`) | who is requesting | plaintext label, e.g. `claude-code:sess-A`; ed25519 planned | any cryptographically-rootable principal |
| **Boundary / limen** | a threshold over a region | literal path or directory prefix (`src/auth/`); no globs yet | any selector over a namespace |
| **Lease** | time-bounded authority over a boundary | `intent` + TTL (default 5 min) + state (active/released/expired) | a time-bounded capability over a region |
| **Intent** | what the holder means to do | `read` / `write` / `propose` | the same access modes, generalized |
| **Witness** | the recorded evidence of a write | path, bytes, SHA-256, time, owning lease + agent | per-mutation attribution over any resource |

### The conflict matrix

Two leases conflict when their boundaries overlap (prefix containment) **and** their intents clash:

| | write | read | propose |
| --- | --- | --- | --- |
| **write** | ⛔ conflict | ⛔ conflict (reader yields) | ✅ ok |
| **read** | ⛔ conflict | ✅ ok | ✅ ok |
| **propose** | ✅ ok | ✅ ok | ✅ ok |

`write × write` is the coordination-layer prevention of **Lost Update**. `write × read` exists because a reader of an interface region must yield when a writer takes it (a partial defense against **Write Skew**). `propose` is a pure non-blocking advisory declaration — "I intend to touch this" — that never conflicts.

---

## Boundary lines (where Limen ends and a neighbor begins)

| Neighbor | What it does | Relationship to Limen |
| --- | --- | --- |
| **Harnesses** (Claude Code, Cursor, Codex, Gemini CLI) | run the agent loop, edit files | Limen **serves** them; value scales with how many run concurrently. One harness needs no coordination; N on one tree is where collisions live. |
| **Agent frameworks** (LangGraph, OpenAI Agents SDK, AutoGen, Swarm) | build & orchestrate agents; coordinate **context inside one app authority** ("handoffs stay within a single run") | **Orthogonal.** They engineer what an agent *sees*; Limen coordinates what an independent agent may *mutate* across runs and harnesses. You can build with them and still need Limen the moment two such agents touch one repo. |
| **git / VCS** | asynchronous merge-after-the-fact; reconciles at a merge step; `git blame` attributes only the human | **Complementary, opposite side of the write.** Limen is synchronous prevention *before* the write and restores per-agent attribution git structurally lacks. Limen prevents the lost update up front; git reconciles what remains. |
| **OS / DB locks** (flock/fcntl, 2PL, leases, Chubby, etcd, ZooKeeper) | mandatory or advisory locking over files / keys | **Direct ancestry**, made agent-aware and protocol-native: advisory leases over namespace *regions*, carried over MCP, tagged with an agent label, with TTL auto-expiry so a hung agent can't deadlock the namespace. |
| **CRDT / OT** (collaborative editing, Google Docs) | optimistic merge-after-write over *convergent* data types | **Opposite end of the same spectrum.** They merge automatically over data engineered to commute; Limen prevents-before-write over *arbitrary non-convergent* state (source, config, infra) where "merge anyway" breaks the build. |
| **Agent protocols** (MCP, ACP, A2A) | tool access (MCP), editor↔one-agent turns (ACP), opaque agent-to-agent handoff (A2A) | Limen **rides on MCP** as its integration surface and fills the gap all three leave open: none arbitrates *concurrent mutation* of shared state or provides a lease+audit layer. |
| **Governance / control planes** (its predecessor) | doctrine, treaties, federation, approvals | **Explicitly rejected.** Limen is a coordination primitive, not a ruler. |
| **MCP Agent Mail** (nearest cousin) | advisory file leases + identities + Git audit + async messaging, over MCP | **Validates the category; not the same product.** Limen is deliberately narrower and purer (three primitives, a typed conflict matrix, mediating the write itself), and is not a messaging bus. |

---

## Generalization axes (how the one beachhead opens onto the category)

The beachhead is narrow on purpose. Each axis below shows how the *same* primitive generalizes — and signals that nothing in the design is welded to code files.

| Axis | Beachhead (today) | General category (durable) |
| --- | --- | --- |
| **Writers** | AI coding harnesses + their sub-agents | any uncoordinated autonomous agents — research, ops, computer-use, data pipelines, even mixed human/agent |
| **Shared state** | a git repo / filesystem | any namespace of mutable resources — documents, KV stores, config, infrastructure, external system state |
| **Region** (the *limen*) | a path prefix | any selector over the namespace |
| **Transport** | an MCP server over stdio | any protocol where an agent can request authority |
| **Locality** | one machine, one workspace | the lineage (Chubby, etcd, ZooKeeper) is already distributed |

The strategy is to **own the category while shipping a narrow first instance** — the arc Git took from kernel source and MCP took from the desktop. The discipline is to let the category guide what Limen *could* become without letting it inflate what Limen *is today*.

---

## The line that keeps Limen small

When a proposed feature is in doubt, apply this test in order:

1. Does it require Limen to be *in charge* of an agent (start/stop/schedule/route/supervise)? → **Out.** (Principle 1)
2. Does it add doctrine/policy/governance/approval machinery? → **Out, forever.**
3. Does it make Limen take or enforce authority it doesn't own (mandatory interception) before adoption exists? → **Defer** to an opt-in enforcement mode.
4. Does it coordinate *what an agent may mutate in shared state*, advisorily, with attribution? → **In scope.**

Everything Limen does should reduce to issuing a lease, mediating a write, recording a witness, or answering "who touched this." If it doesn't, it belongs to a neighbor.
