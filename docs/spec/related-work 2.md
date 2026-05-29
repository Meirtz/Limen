# Limen — Related Work & Experimental Framing (Paper Scaffold)

> **Status: scaffold, not the paper.** This is the planning skeleton for an eventual research write-up of Limen. Every load-bearing factual claim here was cross-checked by an adversarial verifier pass; refuted claims were dropped and uncertain ones flagged. Numeric cells in the experiment are intentionally left as `[TBD — measure]` so no figure from related work is laundered into our own claims.
>
> Companions: [`philosophy.md`](philosophy.md) · [`boundaries.md`](boundaries.md) · sources in [`../references.md`](../references.md). The product framing of the hero experiment is in [`../PRD.md`](../PRD.md) §10.2.

---

## 1. Contribution

**Limen is an advisory-lease + witnessed-audit coordination layer for concurrent autonomous agents operating over shared mutable state, delivered as a drop-in MCP server, first instantiated for multi-harness AI coding.**

Concretely:

1. **A coordination layer, not an orchestrator.** Limen sits *beneath* independently-built, independently-orchestrated agents at the point where they mutate a shared resource (one git working tree). It does not schedule, route, or supervise. Posture: *servant-not-ruler*, aligned with the descriptive charters of Git, OAuth, OpenTelemetry, and MCP rather than a control/governance plane.
2. **Three primitives over a region of a namespace.** `limen_acquire` (lease a path / directory-prefix region under an intent), `limen_write` (mutate within a held lease; the write is mediated and recorded), `limen_release`. Leases are advisory, region-scoped, time-bounded (TTL default 5 min, auto-expiring).
3. **A typed conflict matrix** (verified against the implementation): `write × write` conflicts on overlapping regions; `write × read` conflicts (reader yields); `read × read` never conflicts; `propose × anything` never conflicts. Region overlap is literal-path / directory-prefix containment (no globs yet) — a deliberate MVP narrowing.
4. **A witnessed audit trail with attribution.** Each mediated write records `(lease_id, path, bytes, SHA-256, timestamp)` joined to a per-agent identity, in SQLite — turning "git blame shows only the human" into per-agent forensic attribution (the *assume-breach* posture made concrete).
5. **A drop-in MCP server** (`limen serve`, stdio JSON-RPC 2.0) exposing the three tools, so any MCP-speaking harness coordinates with zero bespoke integration.

**One-line positioning:** *pessimistic intent (declare a time-bounded lease over a region) with optimistic, advisory enforcement (cooperate + witness)*, over **arbitrary non-convergent shared mutable state** — distinct from optimistic merge-after-write systems that require purpose-built convergent data types.

> **Novelty honesty — do NOT overclaim "first."** A small, contemporaneous cohort already occupies the coding beachhead — most notably **MCP Agent Mail** (advisory file leases + identities + Git-backed audit over MCP), plus related MCP coordination tools. The defensible "first/novel" claim is the **generalized category** (advisory lease + witnessed audit + identity for *any* uncoordinated agents over *any* shared mutable resource) and the **rigor of the head-to-head experiment** — not "first advisory file lease for coding agents." Benchmark against the cohort; do not claim sole ownership of the idea.

---

## 2. Related work and the precise gap

Four groups. Each *has* a piece of what Limen needs; none places a lease+audit layer between independent modern AI agents and their shared workspace.

### 2.1 Concurrency control for shared state — the primary lineage

Three design commitments, each tracing to a named precedent:

- **Time-bounded** ← Gray & Cheriton, *Leases* (SOSP 1989). A lease is a time-limited grant; on expiry the server reclaims without contacting the holder. "A crashed client's lease simply expires" is *exactly* why Limen leases carry a TTL: a hung/killed agent cannot deadlock the namespace. Operational templates: **etcd** (Grant/KeepAlive/Revoke/TTL) and **Consul** sessions (identity + TTL + lock-delay + health-tied auto-release); **ZooKeeper** ephemeral znodes are the same liveness idea.
- **Advisory** ← Burrows, *Chubby* (OSDI 2006) and POSIX `flock(2)`. Chubby *explicitly rejected* mandatory locks because its locks "often protect resources implemented by other services, rather than just the file associated with the lock" — precisely Limen guarding a workspace it does not own. Chubby's **sequencer** (lock name + mode + generation number) prefigures Limen's witnessed/attributed trail.
- **Region-scoped** ← Chubby's file/directory granularity and ZooKeeper's znode-path hierarchy. ZooKeeper most sharply validates Limen's architecture: it is "*not a lock service*" but a wait-free coordination kernel on which locks are a client-side recipe.

The **lost-update / write-skew taxonomy** (Berenson et al., *A Critique of ANSI SQL Isolation Levels*, SIGMOD 1995) names exactly what Limen prevents and what it does not. "Agents silently overwrite each other" *is* **Lost Update (P4)**, prevented by `write × write`. Region leases alone do **not** prevent **Write Skew (A5B)** — two agents editing *disjoint* regions that jointly break a cross-region invariant (one changes an interface another still calls) — which justifies both the `write × read` rule and the witness trail. **MVCC** (Bernstein & Goodman, ACM TODS 1983) closes the loop: the substrate (git) is already a multiversion store, and Snapshot Isolation eliminates lost updates but still permits write skew — so even perfect versioning does not make Limen redundant. The optimistic/pessimistic axis is framed by **Kung & Robinson** (ACM TODS 1981); the mutual-exclusion roots by **Dijkstra** (CACM 1965) and **Lamport's bakery** (CACM 1974).

**Gap left:** all are *mandatory* (or, where advisory, scoped to OS files / KV keys, not to agent-aware regions of a developer workspace with per-agent attribution). None targets heterogeneous autonomous agents that cannot be compelled to participate.

### 2.2 Coordination models for cooperating processes

- **Blackboard systems** (Hearsay-II, *ACM Computing Surveys* 1980; Nii, *AI Magazine* 1986): many independent knowledge sources cooperate by mutating one shared, level-partitioned state. *Difference:* the model *centralizes* a control/scheduler — the ruler posture Limen rejects.
- **Tuple spaces / Linda** (Gelernter, *ACM TOPLAS* 1985): coordination through shared associative memory; the atomic destructive `in` is the closest classical analog to `limen_acquire`; "coordination is orthogonal to computation" is Limen's stance verbatim. *Difference:* Linda's `in` is *mandatory and atomic*; Limen's lease is *advisory and time-bounded*.
- **Actor model** (Hewitt et al., IJCAI-73; Agha, MIT Press 1986): the deliberate **foil** — avoids the problem by never sharing state. Limen accepts the premise actors reject (heterogeneous harnesses already share one repo).
- **Optimistic merge-after-write** — what Limen is *not*: **STM** (Shavit & Touitou, PODC 1995) is the speculative midpoint; **CRDTs** (Shapiro et al., SSS 2011) and **OT** (Ellis & Gibbs, SIGMOD 1989) reconcile after every write but only over *purpose-engineered convergent* data.

**Gap left:** these establish shared-state coordination and the prevent-vs-merge spectrum, but the *advisory + time-bounded + per-agent-attributed* point aimed at modern autonomous agents is unoccupied.

### 2.3 Agent protocols and integration surfaces

- **MCP** (host/client/server; 1:1 connection; tools/resources/prompts): standardizes how *one* agent gets context and calls tools. Its charter "does not dictate how AI applications use LLMs or manage the provided context." No multi-client mutation coordination, lease, conflict detection, or cross-agent audit — but it is the *right surface* for Limen (harness-agnostic, advisory by charter, `acquire/write/release` map cleanly to tools).
- **ACP** (Zed; editor ↔ one coding agent): the human-in-editor turn loop for a single agent — wrong layer for multi-agent write coordination.
- **A2A** (Google → Linux Foundation; v1.0): coordinates discovery and task delegation between *opaque* peers and, by design, abstracts shared state *away*.

**Gap left:** the stack assumes a single writer (MCP, ACP) or fully isolated peers (A2A). None arbitrates concurrent mutation of a shared resource.

### 2.4 Multi-agent / harness reality and the security framing

- Mainstream multi-agent work coordinates **context inside one app authority**: Anthropic *Building Effective Agents* and *multi-agent research system*; OpenAI *Swarm* / *Agents SDK* ("Handoffs stay within a single run"); AutoGen *Swarm* ("all agents share the same message context"); LangGraph (coordination = "deciding what information each agent sees"). The industry answer to "many agents, one repo" is to *eliminate* shared state via **git worktrees** (isolation), presupposing a single orchestrator who fans out and re-integrates.
- **Independent corroboration of the conflict model:** LangChain — "Read actions are inherently more parallelizable than write actions"; Anthropic's research post flags that shared-context / write-heavy coding is "not a good fit for multi-agent systems today"; Carvalho (2026) argues current stacks reinvent Linda but lack lease/aging mechanisms. *(Blog/essay sources — corroboration of the gap, not authority for technical facts.)*
- **Security half of the lineage:** Anthropic *Zero Trust for AI agents* (2026) supplies identity + per-task scoping + audit + agentic-SOAR vocabulary, grounded in **NIST SP 800-207** (per-session least privilege; pervasive telemetry) and least privilege from **Saltzer & Schroeder** (1975).

### 2.5 The named gap (one paragraph for the paper)

> Concurrency control for shared state is decades mature (leases, advisory locking, MVCC, lost-update/write-skew theory), but targets databases, OS files, and KV stores — not agent-aware regions of a developer workspace with per-agent attribution — and is overwhelmingly *mandatory*. Coordination models for cooperating processes (blackboards, tuple spaces) either centralize control or impose mandatory atomic claims. Agent protocols (MCP, ACP, A2A) coordinate tool access, editor turns, and inter-agent handoffs but explicitly exclude concurrent mutation of shared external state. The multi-agent literature coordinates context inside one application authority, answering "many agents, one repo" by *eliminating* the sharing (worktrees) rather than coordinating it. **No prior line of work places an advisory, time-bounded, region-scoped lease plus a witnessed, per-agent-attributed audit trail between independent modern AI agents and their shared workspace.** A small, contemporaneous cohort (MCP Agent Mail and peers) has begun to occupy the coding-specific instance; Limen generalizes the category and subjects it to a controlled experiment.

---

## 3. Hero experiment, mapped onto established methodology

### 3.1 Design — a three-arm ablation at fixed task and fixed parallelism N

| Arm | Description | Role in the ablation |
| --- | --- | --- |
| **Seq-1** | one agent, sequential | correctness ceiling / latency baseline |
| **Par-N-Naive** | N concurrent agents on one shared tree, **no** coordination | the uncoordinated-concurrency condition (lost updates, interface breaks) |
| **Par-N-Limen** | N concurrent agents on one shared tree, coordinated by the Limen MCP server | the treatment |
| *(optional)* **Par-N-Peer** | N concurrent agents behind the nearest near-peer (e.g. MCP Agent Mail) | head-to-head against existing prior art |

A clean single-factor ablation: Seq-1 vs Par-N isolates the cost/benefit of parallelism; Par-N-Naive vs Par-N-Limen isolates the effect of the coordination layer with task, model, and N held constant. The thesis is **Pareto-dominance at fixed N**, not "more agents win" — the scaling literature shows parallelism has diminishing/negative returns past a strong single-agent baseline, so a monotone claim would be unsupportable.

### 3.2 Metrics

| Metric | Definition | Lineage / grounding |
| --- | --- | --- |
| **pass@1** | fraction of runs whose final state passes the task's tests | pass@k (Chen et al. 2021): "at least one of k samples passes," with an unbiased estimator |
| **wall-clock** | end-to-end time to completion | naive parallelism is not free; coordination cost is the central variable |
| **lost-edit-lines** | lines committed by one agent then silently overwritten/discarded by another | operationalizes Lost Update (P4) |
| **build-break-rate** | fraction of runs ending non-building / failing-CI | operationalizes Write Skew (A5B) / interface breakage |
| **attribution-accuracy** | fraction of final-state changes correctly attributed to the responsible agent | the witnessed-trail payoff; → 1.0 by construction for Limen, degrades to "the human" for naive |

**Recommended addition — pass^k.** Because the *value* of a coordination layer is consistency across repeated concurrent runs, report **pass^k** (probability *all* k repeated episodes succeed) alongside pass@1. Sharp hypothesis: Par-N-Naive may match Par-N-Limen on pass@1 but **collapse on pass^k** from nondeterministic collisions; Limen should compress the pass@1→pass^k gap.

### 3.3 Closest comparable and the design contrast

**CodeCRDT** (Pugachev, arXiv:2510.18893, 2025) is the nearest academic anchor and the *optimistic merge-after-write* design point: it measures parallel multi-agent code generation over CRDTs and reports speedups on some tasks and slowdowns on others, driven by code-volume inflation and coordination overhead — directly motivating the **Par-N-Naive** arm and the need to measure wall-clock. The contrast to surface: CodeCRDT merges *after* every write over convergent structures; Limen *prevents the overlapping write up front* via a boundary-scoped lease over arbitrary non-convergent state. The story to aim for: Limen's leases avoid CodeCRDT-style code-volume-inflation slowdowns *and* peers' residual overwrite risk, while preserving parallel speedup.

> **Numeric discipline:** do not import CodeCRDT's, the MSR'26 PR study's, or the pass^k paper's figures as if they were ours. Use them only to motivate metric choice; report our own measured `[TBD]` values for every cell.

---

## 4. Threats to validity

1. **Construct / mechanism — advisory bypass.** Limen is advisory; an agent that ignores the protocol or writes through an unmediated path defeats prevention, so lost-edit-lines / build-break-rate would reflect *protocol adherence*, not the mechanism. *Mitigations:* state plainly that advisory is a posture, not a guarantee; measure adherence separately or instrument harnesses so all writes route through `limen_write`; note that even under bypass the *witness still provides attribution* (the audit half degrades gracefully where prevention does not); disclose any optional hard-enforcement used.
2. **Internal — harness instrumentation bias.** Routing writes through `limen_write` changes the I/O path and may alter latency/behavior independent of coordination; closed harnesses (Cursor, Codex) are hard to instrument uniformly. *Mitigations:* prefer a fully-instrumentable OSS harness (e.g. aider) for controlled cells; apply identical instrumentation to Par-N-Naive (record-only, no leasing) so the *only* difference between Par-N arms is conflict arbitration; report per-harness, don't pool.
3. **External / construct — dataset coupling.** Benefit is mechanically tied to region overlap: disjoint-file subtasks show little `write × write` conflict (advantage → 0); highly-coupled tasks exaggerate it. The literal/prefix-only region model further couples results to repo layout. *Mitigations:* stratify tasks by an a-priori coupling measure; report the conflict-rate distribution; don't claim generality beyond the tested coupling regime; flag the no-glob limitation.
4. **Statistical power / cell count.** arms (3–4) × N (multiple) × task-strata × repeated episodes (for pass^k) × harnesses explodes; pass^k needs many repeats per cell; LLM nondeterminism inflates variance. *Mitigations:* pre-register the primary comparison (Par-N-Limen vs Par-N-Naive at one headline N); power that cell explicitly; report CIs and the pass@k/pass^k estimator; don't over-interpret underpowered secondary cells.
5. **Positioning / novelty.** Near-peers exist, so a coding-scoped "first" claim is invalid. *Mitigation:* scope novelty to the generalized category and the controlled experiment; if feasible add **Par-N-Peer** so the contribution is shown head-to-head, not asserted.

---

## Appendix — citation-accuracy notes (carry these into the paper)

The verifier pass refuted four commonly-circulated phrasings. Use the corrected forms:

- **Zero Trust thesis = two adjacent sentences, not one.** Quote separately and verbatim: *"Zero Trust — trust nothing, verify everything, and assume breach has already occurred — gives security leaders a proven foundation to address this."* and *"But the principles need new shape for agentic systems: identities that are cryptographically rooted, permissions scoped per task, memory protected against poisoning, and defensive operations that run at the speed of autonomous attackers."* Do **not** fuse "a proven foundation … identities that are cryptographically rooted."
- **Least privilege (Saltzer & Schroeder 1975) verbatim:** *"Every program and every user of the system should operate using the least set of privileges necessary to complete the job."* — "every **user**" (not "privileged user"), "least **set of privileges**" (not "least amount of privilege").
- **Capability vs object-capability:** Dennis & Van Horn (1966) introduced the *capability* concept; the term "object-capability model" and the phrase "unforgeable token of authority" are later (Miller et al.). Cite the lease as capability-**inspired**; attribute ocap framing separately.
- **Anthropic Zero Trust eBook tiers** (if cited) are "Foundation / Enterprise / Advanced" — not "…Optimized."
- **Verify before quoting numbers:** CodeCRDT per-task speedup/slowdown, pass^k headline figures, MSR'26 PR-failure percentages, and MCP Agent Mail's exact conflict behavior were summarizer-sourced; confirm against primary PDFs/source before reproducing.
