# Limen — Philosophy

> Canonical terminology lives in [`glossary.md`](glossary.md) · Scope and edges live in [`boundaries.md`](boundaries.md) · Sources live in [`../references.md`](../references.md).
>
> 中文伴随版：[`philosophy.zh.md`](philosophy.zh.md)。English is canonical.

Limen coordinates **concurrent, autonomous agents that mutate shared state**. It issues an advisory, boundary-scoped, time-bounded **lease**, mediates the write and records a **witness** trail (bytes, SHA-256, agent label, timestamp), and ties both to a per-agent **identity** — exposed as an MCP server (`limen_acquire` / `limen_write` / `limen_release`).

That is the **general model**, and it is the organizing principle for everything here — the docs and the code. The shared state is reached through a **resource**: a pluggable backend that says how regions compare and how a mediated change is applied. Limen ships exactly one resource today — a **filesystem** — with one worked example that makes the problem vivid: heterogeneous coding agents (Claude Code, Cursor, Codex) and their parallel sub-agents sharing one working tree. That coding case is an *example*, not the definition and not the spine — the way Git began on Linux kernel source and MCP began on the desktop without *being* those.

This document is the intellectual case for that shape. It is deliberately built on **distributed-systems concurrency control** and **zero-trust security**, not on LLM orchestration — because that is the part of the problem that will outlive any model generation.

---

## The shape of the problem

The theory of concurrent writers to shared state is settled, and it has precise names. Berenson et al., *A Critique of ANSI SQL Isolation Levels* (SIGMOD 1995), give us two:

- **Lost Update** (`r1[x] … w2[x] … w1[x] … c1`): one writer reads, a second writes, the first overwrites based on its stale read, and the second's committed work vanishes silently.
- **Write Skew** (`r1[x] … r2[y] … w1[y] … w2[x]`): two writers read overlapping data, write *disjoint* items, and together break an invariant neither broke alone.

This is exactly what happens when several AI coding agents touch one repository. The same failure modes are now being re-discovered in the agent literature under their own names — *lost updates*, *interface breakage*, *stale partial views* — by Chacon Sartori, *The Specification Gap* (arXiv:2603.24284, 2026), and described from the agent's point of view by Cognition's *Don't Build Multi-Agents* (Yan, 2025): parallel actors "cannot see what the other was doing" and act on "conflicting assumptions not prescribed upfront." That is Lost Update, told from the inside.

The crucial asymmetry is this: **databases and version-control systems earned their safety; agents inherited none of it.** A SQL transaction takes locks or runs under a snapshot and commits atomically. Git serializes through an index and an explicit merge. Agents, by contrast, are:

- **Non-deterministic writers** — the same prompt yields different edits across runs, so collisions are not reproducible and cannot be designed around statically;
- **Lock-naïve** — they were never written to `acquire` before they write; they open a file and `write()`;
- **Mutually blind** — independent harnesses launched by different humans or scripts share a working tree with no common parent to merge for them.

So the modern agent stack reconstructed a 1995 hazard while discarding the 1970s–90s cure. **Limen's thesis is that the cure does not need reinventing — it needs porting:** lift lost-update prevention to the coordination layer, where the writers are agents and the shared state is a filesystem nobody locked.

---

## Where Limen stands

Limen takes its primitives from the lineage that already solved coordination, and its security vocabulary from the lineage now adapting to agents.

- **Concurrency control & leases.** The lease itself is Gray & Cheriton, *Leases* (SOSP 1989): a time-bounded grant of authority whose holder, on crash or partition, simply lets it *expire* — so failures cost performance, not correctness. The advisory posture is Burrows, *Chubby* (OSDI 2006) and POSIX `flock(2)`. The minimal-kernel, compose-at-the-edge stance is ZooKeeper (Hunt et al., USENIX ATC 2010), which insists it is "*not a lock service*" but a coordination primitive. The modern operational templates are etcd leases (Grant/KeepAlive/Revoke/TTL) and Consul sessions.
- **Zero trust for AI agents.** The security half is Anthropic's *Zero Trust for AI agents* (2026), grounded in NIST SP 800-207 (2020) and least privilege (Saltzer & Schroeder, 1975). Limen operationalizes three of its four requirements as coordination primitives (see Principle 5).

Standing here — not on prompt-orchestration patterns — is a deliberate bet: **reasoning is volatile; coordination is not.** Models, harnesses, and orchestration fashions churn. The lost-update problem is forty years old and will outlive all of them.

---

## The principles

### 1. Coordinate shared state; don't orchestrate agents

Limen's job ends at the boundary where an agent mutates a shared resource. It does not start, stop, schedule, route, or supervise anything. This is the line that keeps the project small and the category clean: orchestration is crowded and model-coupled; *coordination of the writes that orchestration produces* is nearly empty and model-agnostic.

A useful test: if a feature only makes sense once you assume Limen is in charge of the agents, it is out of scope. Limen is never in charge.

### 2. Advisory-first

A Limen lease conflicts only with other lease attempts. It does not physically stop an agent from touching a file. This is a chosen, well-precedented posture, not a weakness papered over.

Burrows put the rationale exactly, in choosing advisory over mandatory locks for Chubby (OSDI 2006): locks "conflict only with other attempts to acquire the same lock … We rejected mandatory locks [because] Chubby locks often protect resources implemented by other services, rather than just the file associated with the lock." That is Limen's situation precisely — it guards a workspace it does not own. POSIX `flock(2)` says the same at the OS level: "a process is free to ignore the use of flock()."

The deeper reason is adoption. A mandatory lock you can only honor by rewriting every harness will be honored by none of them. An advisory lease that any MCP host can call with zero bespoke integration will be honored by the cooperating majority — and in this beachhead the agents *want* not to clobber each other, so cooperation is the common case, not the adversarial one. **A guarantee nobody opts into is worth nothing.**

### 3. Servant, not ruler

The infrastructure that won was descriptive, not governing. Limen joins it.

| It says | "I do this" | "I do **not** do this" |
| --- | --- | --- |
| Git | I track diffs | I manage your code |
| MCP | I define the tool-call wire format | I control your agent |
| OpenTelemetry | I define the span format | I supervise your system |
| OAuth | I define the authorization handshake | I decide who you are |
| **Limen** | **I issue leases and witness writes** | **I govern your agent swarm** |

MCP states its own charter this way: it "focuses solely on the protocol for context exchange — it does not dictate how AI applications use LLMs or manage the provided context." Limen adopts the identical stance — one more advisory MCP server, not a control plane. This is also a correction of course: Limen's predecessor over-reached into "control plane / law layer / governed swarm" language, and that over-reach is exactly what this project is escaping.

### 4. The model is general; the filesystem is one resource

The definition is general — **coordination of concurrent autonomous agents over shared mutable state** — and the design follows it: the core knows only namespaces, regions, resources, leases, witnesses, and identities. A **resource** is the seam where the general model meets a concrete world; v0.1 implements one (the filesystem) and measures it on multi-harness coding. Designing the model generally while shipping a single resource is the discipline that keeps the project broad in concept and small in surface — not a staged narrowing, and emphatically not feature sprawl (one resource, not a speculative zoo of backends).

The model long predates LLMs. It is the **blackboard** pattern (Hearsay-II, Erman et al. 1980; Nii 1986): many independent agents cooperating by mutating one shared, region-partitioned state. It is the **tuple space** (Gelernter, *Linda*, 1985), whose atomic destructive `in` is essentially "claim authority over a region before acting," and whose motto — coordination is orthogonal to computation — is Limen's stance verbatim. Limen generalizes these with one deliberate inversion: where Hearsay-II *centralizes* a scheduler that decides which knowledge source runs, Limen removes central scheduling — the writers self-coordinate by claiming regions. That is servant-not-ruler at the architectural level. The **actor model** (Hewitt 1973; Agha 1986) is the foil: it dodges the whole problem by never sharing state. Limen accepts the premise actors reject — today's heterogeneous harnesses *already* share one repo and cannot be rewritten as pure actors — so the shared state must be coordinated, not wished away.

The generalization axes (writers, namespace, region, resource, locality) are tabulated in [`boundaries.md`](boundaries.md).

### 5. Three coordinatable zero-trust primitives (and why the fourth is only partial)

Anthropic's *Zero Trust for AI agents* (2026) frames the agentic shift in two adjacent sentences:

> "Zero Trust — trust nothing, verify everything, and assume breach has already occurred — gives security leaders a proven foundation to address this."
>
> "But the principles need new shape for agentic systems: identities that are cryptographically rooted, permissions scoped per task, memory protected against poisoning, and defensive operations that run at the speed of autonomous attackers."

Three of those four requirements map directly onto Limen's runtime primitives. The fourth is only partial in the MVP.

| Zero-trust requirement | Limen primitive | Lineage |
| --- | --- | --- |
| permissions **scoped per task** | **Lease** — advisory, region-scoped, time-bounded authority to mutate | Least privilege (Saltzer & Schroeder 1975); NIST SP 800-207 tenet 3 (per-session, "least privileges needed to complete the task"); Just-In-Time / Just-Enough-Access; Gray & Cheriton 1989 |
| **assume breach** has occurred | **Witness** — mediated-write audit trail: bytes + SHA-256 + agent label per write | NIST SP 800-207 tenet 7 (collect pervasive telemetry); "minimize blast radius, use analytics for visibility" |
| identities **cryptographically rooted** | **Identity** — per-agent label today; ed25519 signing planned | NIST SP 800-207 tenet 4 (policy keyed on client identity) |
| defensive ops **at machine speed** (agentic SOAR) | **Conflict arbitration at write time** — *partial* | the agentic-SOAR framing (industry usage) |

The lease is least privilege made **temporal and spatial**: bracketed in time (acquire late, auto-expire) and in space (one region). Saltzer & Schroeder (1975) state the principle precisely — "Every program and every user of the system should operate using the least set of privileges necessary to complete the job." The lease is also **capability-inspired** (the *capability* concept is Dennis & Van Horn, 1966) — but only inspired: until ed25519 signing and mediation land, a Limen lease is not an unforgeable token, and we do not claim object-capability-grade unforgeability today.

The fourth primitive is honestly scoped down. Limen does real-time arbitration *at acquisition and write time* — that is "machine-speed response" in the narrow coordination sense — but Limen is **not** a SOAR product: no detect–decide–respond loop against an adversary, no incident response. The mapping is an analogy, not an identity.

A posture guardrail when citing this lineage: NIST, Microsoft, and the object-capability tradition are all *enforcing* models. Cite them for vocabulary and lineage, **not** for posture. Limen is advisory-first.

### 6. Prevention before the write, not merge after it

There is a well-mapped spectrum from pessimistic to optimistic concurrency: **prevent** (Dijkstra's critical section 1965; Lamport's bakery 1974; Linda's atomic `in`) → **speculate, then commit-or-abort** (Software Transactional Memory; Shavit & Touitou, PODC 1995) → **merge after every write** (Operational Transformation, Ellis & Gibbs 1989, the backbone of Google Docs; CRDTs, Shapiro et al. 2011).

Limen sits at the pessimistic end, and on purpose. CRDT/OT achieve convergence **without coordination**, but only over data types engineered to commute — positional text, counters, sets. Limen's beachhead state is source files, configs, infrastructure, where two edits frequently *do not* commute: change an API signature one agent still calls, and "merge anyway" yields a broken build, not a reconciled document. No least-upper-bound merge or operational transform can fix a semantic, cross-file break. Because Limen cannot roll back a filesystem the way STM rolls back memory, it warns **before** the overlapping write rather than reconciling after.

This is also why Limen **complements git rather than competing with it.** Git is asynchronous merge-after-the-fact; Limen is synchronous conflict-prevention before the write. And git cannot attribute an agent — `git blame` shows only the human who launched the harness — whereas Limen's witness records the agent label per write. One prevents the lost update up front; the other reconciles what remains.

Stated crisply: **CRDT/OT = automatic merge-after-write over specially-designed convergent state; Limen = advisory, region-scoped, time-bounded prevention-before-write over arbitrary non-convergent shared mutable state.**

### 7. Every claimed benefit must be falsifiable and measured

Limen makes a claim that an experiment can prove wrong — which is what keeps it paper-grade rather than rhetorical:

> Coordination's value is **conditional on task coupling**, and reliability is where uncoordinated concurrency fails first. As writers (N) and coupling rise, naive concurrency's **pass^k** (all of k repeated runs succeed) collapses super-linearly through lost edits and broken builds; advisory coordination recovers most of that cost **below a coupling threshold τ** (a Pareto-improvement — added safety at ~no time cost), while **above τ** the safety gain persists but the wall-clock advantage inverts.

The three-arm ablation is **Seq-1** (one agent, sequential — the correctness ceiling and latency baseline), **Par-N-Naive** (N concurrent agents, no coordination), **Par-N-Limen** (N concurrent agents behind advisory leases). The claim is *conditional on coupling*, not "more agents win": below a coupling threshold τ coordination Pareto-improves (added safety at ~no time cost), above it the safety gain persists while the wall-clock advantage inverts — consistent with the scaling literature, where parallelism has diminishing or negative returns past a strong single-agent baseline. The naive arm's cost is empirically real, not a strawman: CodeCRDT (Pugachev, arXiv:2510.18893, 2025) measures parallel multi-agent code generation and finds speedups on some tasks and slowdowns on others, with nonzero semantic-conflict rates. The correctness metric is pass@k (Chen et al., 2021); the stricter reliability framing is pass^k (all k repeated runs succeed) — and the sharpest hypothesis is that Par-N-Naive may *match* on pass@1 yet *collapse* on pass^k from non-deterministic collisions, with Limen compressing that gap.

The full design, metrics, comparables, and threats to validity are scaffolded in [`related-work.md`](related-work.md).

---

## Tensions we hold honestly

A philosophy that only lists its strengths is marketing. These are the real edges.

**Advisory is a weak guarantee, and bypass is real.** An agent that ignores `limen_acquire` and writes directly is not stopped — Limen issues, it does not enforce at the kernel. Four things make this the right trade anyway: (i) it is the same trade Chubby and `flock` made deliberately, at scale; (ii) the witness trail converts "prevented" into "attributed" — *assume breach* made concrete, so a bypass is at least forensically reconstructable beyond `git blame`; (iii) the beachhead is cooperative by nature; (iv) advisory is the *only* posture that achieves adoption across harnesses that cannot be compelled to participate. The MVP must not over-claim: it is capability-inspired, not capability-enforced, until ed25519 signing plus mediation land.

**Prefix leases can be too coarse.** The MVP's region match is literal-path / directory-prefix only (no globs): a lease on `src/` conflicts with any write beneath it. This can serialize agents that would not actually have collided (false conflicts), and a lease on `src/` says nothing about a Write Skew between `src/api/` and `src/caller/`. Coarse granularity buys a simple, fast, auditable conflict check for the MVP; finer regions (globs, byte ranges, semantic regions) are future work, and the witness trail is the backstop for the skew cases leases cannot see.

**"Don't build multi-agent systems" is the strongest counter-argument — and Limen agrees with it.** Cognition argues parallel sub-agents are fragile and the right default is a single linear agent. If so, why a multi-agent coordination layer? Because **Limen does not advocate multi-agent; it makes the multi-agent that already happens safer.** It is agnostic on whether you *should* run concurrent agents and observes that people *already do* — multiple harnesses, multiple humans, sub-agent fan-out. Even Anthropic's own multi-agent research system notes that "subagents cannot coordinate with each other" and that shared-context, write-heavy coding is "not a good fit for multi-agent systems today." Cognition's cure (collapse to one agent, or engineer richer in-app context) does not reach the case where *independent* harnesses share one repo with no common parent. Cognition advises on architecture; Limen provides safety for the concurrency that exists regardless.

**There is a nearest cousin, and we name it.** It is not true that "almost nobody" coordinates concurrent writes across independent harnesses. **MCP Agent Mail** (Dicklesworthstone, 2025) occupies almost exactly this beachhead: advisory TTL-leased file reservations, per-agent identities, and a Git-backed audit trail for heterogeneous coding harnesses over MCP. Its existence **validates the category** rather than refuting Limen. Limen's defensible differentiation is narrowness and purity — three primitives over path-pattern regions with a crisply typed conflict matrix, *mediating the write itself* (`limen_write` records bytes + SHA-256 + agent label) rather than only reserving and relying on a hook, and deliberately not layering a messaging/mailbox model on top. The honest claim is therefore the **generalized category plus a rigorous experiment**, not "first advisory file lease for coding agents."

---

## One line

Limen is **advisory, region-scoped, time-bounded prevention-before-change over arbitrary shared mutable state** — the fifty-year lost-update cure (leases, advisory locking, mutual exclusion, blackboard/tuple-space coordination) ported to non-deterministic, lock-naïve, mutually-blind agents, joined to the zero-trust triple of identity + per-task lease + witnessed audit, shipped servant-not-ruler as a general model with the filesystem as its first resource.

For what Limen explicitly is *not*, see [`boundaries.md`](boundaries.md).
