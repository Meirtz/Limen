# Limen — Glossary

> This glossary is the canonical vocabulary for the repository. Other documents reuse these terms exactly rather than redefining them.
>
> 中文伴随版：[`glossary.zh.md`](glossary.zh.md)。English is canonical. Companions: [`philosophy.md`](philosophy.md) · [`boundaries.md`](boundaries.md) · [`../references.md`](../references.md).

Limen has a deliberately small vocabulary. **Every runtime term maps to a real type in the implementation** — Limen does not invent words for things the code does not have. Code locations point at `crates/limen/`.

## Core vocabulary

| Term | Definition | In code |
| --- | --- | --- |
| **Limen** | A workspace coordination daemon: a single small Rust crate exposed as an MCP server that issues advisory leases and records witnessed writes so concurrent agents do not collide. Latin *limen* = *threshold*. | `crates/limen` |
| **agent** | An autonomous writer that mutates shared state — a coding harness, a sub-agent, a pipeline, anything that takes a lease and writes. An agent is defined by what it *mutates and under what authority*, not by personality or chat behavior. Limen neither builds nor runs agents. | — |
| **identity** (agent label) | Who is requesting. A plaintext label today (e.g. `claude-code:sess-A`); ed25519-signed identity is planned. Until then, identity is *asserted*, not cryptographically proven. | `agent_label` field |
| **shared state** | The mutable resource namespace Limen coordinates. In the beachhead it is a git working tree / filesystem; in the general category it is any namespace of mutable resources (documents, KV store, config, infrastructure). | the filesystem (beachhead) |
| **boundary** (a *limen*) | A region of the namespace that a lease covers — the threshold an agent crosses to mutate. In the MVP it is a literal path or a directory prefix (`src/auth/`); no globs yet. | `path_pattern` field; `patterns_overlap`, `path_in_pattern` |
| **lease** | An advisory, boundary-scoped, time-bounded grant of authority to act on a region under a given intent. The central primitive. A crashed/hung holder's lease simply expires (Gray & Cheriton 1989), so it cannot deadlock the namespace. | `store::Lease` |
| **intent** | What the lease holder means to do: `read`, `write`, or `propose`. Determines conflict behavior. | `store::Intent` |
| **TTL** | Lease lifetime in milliseconds (default 5 minutes). On expiry the lease becomes `expired` and no longer conflicts; `acquire` expires stale leases before checking conflicts. | `DEFAULT_LEASE_TTL_MS` |
| **lease state** | `active` (held), `released` (explicitly dropped), or `expired` (TTL elapsed). | `store::LeaseState` |
| **conflict** | The condition where two leases cannot coexist: their boundaries overlap (prefix containment) **and** their intents clash. A new acquire that conflicts with an active lease is refused (first-come-first-served + TTL). | `acquire_lease` |
| **witness** | The recorded evidence of a mediated write: path, bytes written, SHA-256 content hash, timestamp, and the owning lease (hence agent). The audit half of the system — *assume breach* made concrete. | `store::WriteRecord` |
| **attribution** | Answering "who changed this path, when, under which lease," by joining a write to its lease's agent label. Restores what `git blame` cannot show (it sees only the human). | `attribute_path` |
| **mediated write** | A write performed *through* `limen_write` under a held lease: Limen validates the lease, performs the write, and records the witness. | `record_write` |

## The conflict matrix

Two leases conflict when their boundaries overlap **and** their intents clash:

| | write | read | propose |
| --- | --- | --- | --- |
| **write** | conflict | conflict (reader yields) | ok |
| **read** | conflict | ok | ok |
| **propose** | ok | ok | ok |

- **write × write** — the coordination-layer prevention of *Lost Update*.
- **write × read** — a reader of a region yields to a writer (a partial defense against *Write Skew*).
- **read × read** — never conflicts; reads are parallelizable.
- **propose** — a non-blocking advisory declaration ("I intend to touch this"); never conflicts with anything.

## Posture & scope terms

| Term | Definition |
| --- | --- |
| **advisory** | A lease conflicts only with *other lease attempts*; it does not physically prevent an agent from writing. The posture of Chubby and POSIX `flock`. Limen issues and witnesses; it does not enforce at the kernel. Opt-in enforcement is future work. |
| **servant, not ruler** | Limen's stance: "I issue leases and witness writes; I do not govern your agents." Aligned with Git, MCP, OAuth, OpenTelemetry — descriptive infrastructure, not a control plane. |
| **general category** | Limen's durable definition: *coordination of concurrent autonomous agents over shared mutable state* via advisory lease + witness + identity. Rooted in concurrency control and zero-trust, not LLM orchestration. |
| **beachhead** | The first concrete instantiation of the category: multi-harness AI coding over a git repo via MCP. An *instance*, not the definition. |
| **prevention-before-write** | Limen's place on the concurrency spectrum: warn at lease-acquisition time, *before* the overlapping write — as opposed to *merge-after-write* (CRDT/OT) or speculate-then-commit (STM). |
| **zero-trust triple** | The three coordinatable zero-trust primitives Limen operationalizes: per-task scope (→ lease), audit/assume-breach (→ witness), cryptographically-rooted identity (→ agent identity). The fourth, agentic SOAR, is only partial (write-time conflict arbitration). |

## MCP surface

Limen is exposed as an MCP server (stdio JSON-RPC 2.0) with exactly three tools:

| Tool | Does | Returns |
| --- | --- | --- |
| `limen_acquire` | Acquire a lease on a boundary under an intent (`path_pattern`, `intent`, `agent_label`, optional `ttl_ms`) | `lease_id`, `expires_at` — or a conflict error |
| `limen_write` | Perform a mediated write under a held lease (`lease_id`, `path`, `content`); path must fall within the lease boundary | `content_hash`, `bytes_written` |
| `limen_release` | Release a held lease (`lease_id`) | `released: bool` |

## Deliberately retired terms (NOT Limen vocabulary)

Limen is the successor to a far larger project (Crawfish). The following terms were that project's conceptual inflation and are **not** part of Limen. If you see them in unmigrated documents, treat those documents as stale.

`control plane` · `governed swarm` / `swarm` · `doctrine pack` · `jurisdiction class` · `treaty` · `federation pack` · `evidence bundle` · `oversight checkpoint` · `encounter` / `encounter policy` · `consent grant` · `capability lease` (Crawfish's heavier construct) · `continuity mode` · `degraded profile` · `verify_loop` · `execution strategy` · `scorecard` / `evaluation spine` · `review queue` · `alert rule` · `remote evidence` / `remote follow-up` · `agent plane` / `harness plane` (as control-plane concepts).

Limen keeps only what its code actually does: leases, witnesses, identities, intents, boundaries, and a conflict matrix.

## Naming discipline

- Reuse a term from this file rather than coining a synonym.
- Do not introduce a runtime term unless a real type backs it in `crates/limen/`.
- Prefer the lineage's words (lease, advisory, region, witness, least privilege) over bespoke coinages — Limen's credibility comes from standing on concurrency control and zero-trust, not from new vocabulary.
