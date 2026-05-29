# Limen — Glossary

> This glossary is the canonical vocabulary for the repository. Other documents reuse these terms exactly rather than redefining them.
>
> 中文伴随版：[`glossary.zh.md`](glossary.zh.md)。English is canonical. Companions: [`philosophy.md`](philosophy.md) · [`boundaries.md`](boundaries.md) · [`../references.md`](../references.md).

Limen has a small, **general** vocabulary. The terms describe coordination over *any* shared mutable state; the **filesystem** is the one resource implemented today, shown in the right-hand column as a worked example. Every runtime term maps to a real type in `crates/limen/`.

## Core vocabulary

| Term | Definition (general) | Filesystem resource (today) |
| --- | --- | --- |
| **Limen** | A coordination daemon, exposed as an MCP server, that issues advisory leases over regions of a namespace and records a witnessed audit of mediated changes, so concurrent agents do not collide. Latin *limen* = *threshold*. | — |
| **agent** | An autonomous writer that changes shared state — a coding harness, a sub-agent, a research/ops/computer-use agent, a pipeline. Defined by *what it changes and under what authority*, not by personality. Limen neither builds nor runs agents. | a Claude Code / Cursor / Codex session |
| **namespace** | The addressable space of mutable resources being coordinated. | the workspace's files |
| **resource** | A pluggable backend that gives a namespace meaning: how regions are compared and how a mediated change is applied. v0.1 ships exactly one. | the filesystem |
| **region** (a *limen*) | A slice of the namespace that a lease covers — the threshold an agent crosses to change something. | a literal path or directory prefix (`src/auth/`) |
| **identity** | Who is requesting. A plaintext label by default; an agent can `register` an ed25519 key and then sign each acquire, making identity *cryptographically rooted* rather than merely *asserted*. Opt-in and back-compatible. | `agent_label`, e.g. `claude-code:sess-A` |
| **lease** | An advisory, region-scoped, time-bounded grant of authority to act under a given intent. The central primitive. A crashed/hung holder's lease simply expires (Gray & Cheriton 1989), so it cannot deadlock the namespace. | `store::Lease` |
| **intent** | What the lease holder means to do: `read`, `write`, or `propose`. Determines conflict behavior. | `store::Intent` |
| **TTL** | Lease lifetime (default 5 minutes). On expiry the lease no longer conflicts; `acquire` expires stale leases before checking conflicts. | `DEFAULT_LEASE_TTL_MS` |
| **lease state** | `active`, `released`, or `expired`. | `store::LeaseState` |
| **conflict** | Two leases cannot coexist: their regions overlap **and** their intents clash. A conflicting acquire is refused (first-come-first-served + TTL). | `acquire_lease` |
| **witness** | The recorded evidence of a mediated change: target, size, content hash, timestamp, owning lease (hence agent). The audit half — *assume breach* made concrete. | `store::WriteRecord` |
| **attribution** | Answering "who changed this, when, under which lease," by joining a change to its lease's identity. Restores what `git blame` cannot show. | `attribute_path` |
| **mediated change** | A change performed *through* Limen under a held lease: Limen validates the lease, applies it to the resource, and records the witness. | `record_write` (a file write) |

## The conflict matrix

Two leases conflict when their regions overlap **and** their intents clash:

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
| **advisory** | A lease conflicts only with *other lease attempts*; it does not physically prevent a change. The posture of Chubby and POSIX `flock`. Limen issues and witnesses; it does not enforce. Opt-in enforcement is future work. |
| **servant, not ruler** | Limen's stance: "I issue leases and witness changes; I do not govern your agents." Aligned with Git, MCP, OAuth, OpenTelemetry — descriptive infrastructure, not a control plane. |
| **general model** | Limen's definition: *coordination of concurrent autonomous agents over shared mutable state* via lease + witness + identity over regions of a namespace. Rooted in concurrency control and zero-trust, not LLM orchestration. The model is the product; resources are how it meets the world. |
| **prevention-before-change** | Limen's place on the concurrency spectrum: warn at lease-acquisition time, *before* an overlapping change — as opposed to *merge-after-write* (CRDT/OT) or speculate-then-commit (STM). |
| **zero-trust triple** | The three coordinatable zero-trust primitives Limen operationalizes: per-task scope (→ lease), audit/assume-breach (→ witness), cryptographically-rooted identity (→ agent identity). The fourth, agentic SOAR, is only partial (conflict arbitration at request time). |

## MCP surface

Limen is exposed as an MCP server (stdio JSON-RPC 2.0) with exactly three tools. Parameters carry general meaning; the filesystem resource gives them concrete form (a region is a path/prefix, a target is a path):

| Tool | Does | Returns |
| --- | --- | --- |
| `limen_acquire` | Acquire a lease on a region under an intent | `lease_id`, `expires_at` — or a conflict error |
| `limen_write` | Apply a mediated change to a target within a held lease | `content_hash`, `bytes_written` |
| `limen_release` | Release a held lease | `released: bool` |
| `limen_renew` | Extend the TTL of a held lease (keepalive) | `expires_at` |

## Deliberately retired terms (NOT Limen vocabulary)

Limen is the successor to a far larger project (Crawfish). The following were that project's conceptual inflation and are **not** part of Limen. If you see them in unmigrated documents, treat those documents as stale.

`control plane` · `governed swarm` / `swarm` · `doctrine pack` · `jurisdiction class` · `treaty` · `federation pack` · `evidence bundle` · `oversight checkpoint` · `encounter` / `encounter policy` · `consent grant` · `capability lease` (Crawfish's heavier construct) · `continuity mode` · `degraded profile` · `verify_loop` · `execution strategy` · `scorecard` / `evaluation spine` · `review queue` · `alert rule` · `remote evidence` / `remote follow-up` · `agent plane` / `harness plane` (as control-plane concepts).

Limen keeps only what its model needs: namespaces, regions, resources, identities, intents, leases, witnesses, and a conflict matrix.

## Naming discipline

- Reuse a term from this file rather than coining a synonym.
- Keep the vocabulary **general** — do not let filesystem-specific words (path, file) leak into the core model; they belong only in the filesystem-resource column.
- Do not introduce a runtime term unless a real type backs it in `crates/limen/`.
- Prefer the lineage's words (lease, advisory, region, witness, least privilege) over bespoke coinages — Limen's credibility comes from standing on concurrency control and zero-trust, not from new vocabulary.
