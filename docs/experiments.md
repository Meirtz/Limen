# Limen — Hero Experiment Design

> **Status: design, not results.** This is the executable experiment plan behind the
> quantitative thesis. The harness and the runs are future work (roadmap v0.4); this
> document fixes the design so the claim is concrete, falsifiable, and reviewable.
> Companions: [`spec/related-work.md`](spec/related-work.md) (related work + framing),
> [`PRD.md`](PRD.md) §10.2 (product-level summary), [`spec/philosophy.md`](spec/philosophy.md) (Principle 7).
>
> **Numeric discipline:** every measured quantity below is a `[TBD — measure]`. Do not
> import figures from related work as if they were ours; cite them only to motivate design.

## 1. The claim under test

> **At a fixed degree of parallelism N, `Par-N-Limen` Pareto-dominates `Par-N-Naive`** on
> the pair (wall-clock, pass@1) — no worse on either axis, strictly better on at least one —
> while strictly winning on lost-edit-lines, build-break-rate, and attribution-accuracy.

It is deliberately a **Pareto-dominance claim at fixed N**, not "more agents = better."
The multi-agent scaling literature shows parallelism has diminishing or negative returns
past a strong single-agent baseline, so a monotone "parallelism helps" claim would be
unsupportable. The contribution is that an advisory coordination layer *moves the
coordination-cost curve* so that, at a given N, you keep parallel speedup without paying
the lost-update / build-break tax.

**Null hypothesis (H0):** Par-N-Limen does not Pareto-dominate Par-N-Naive — i.e. any
pass@1 gain costs wall-clock (or vice versa), and coordination-sensitive metrics do not
differ beyond noise. Rejecting H0 on the pre-registered primary cell is the result.

## 2. Arms (single-factor ablation)

| Arm | Setup | Isolates |
| --- | --- | --- |
| **Seq-1** | one agent, the N subtasks done sequentially | correctness ceiling + latency baseline |
| **Par-N-Naive** | N agents, one shared working tree, **no** coordination | the cost of uncoordinated concurrency |
| **Par-N-Limen** | N agents + the Limen MCP server (advisory leases) | the treatment |
| **Par-N-Peer** *(optional)* | N agents + the nearest prior-art peer (e.g. MCP Agent Mail) | head-to-head vs existing work |

Task, model, temperature, N, and the agent harness are held constant across arms. The
**only** difference between the two `Par-N` arms is whether writes go through a lease.

## 3. Benchmark: a Concurrent-Refactor Suite

SWE-bench tasks are single-issue and do not naturally decompose into N agents editing one
tree, so the **primary** benchmark is purpose-built for controlled concurrency; an
ecological-validity secondary reuses SWE-bench.

### 3.1 Primary — controlled Concurrent-Refactor Suite (CRS)

Each CRS case is `(repo snapshot, N subtasks, hidden test suite, coupling label)`:

- **N subtasks** are independent refactor/feature asks against one repo, assigned one per
  agent, all run **in parallel on a single shared working tree**.
- **Coupling label** is the key independent variable — controlled, not incidental:
  - `disjoint` — subtasks touch non-overlapping files/regions (Limen's advantage → ~0; a fairness check)
  - `shared-region` — two+ subtasks edit the same file/dir (drives `write × write`, i.e. Lost Update)
  - `interface` — one subtask changes an API another subtask depends on (drives Write Skew → build break)
- **Hidden test suite** defines correctness for the *combined* final tree (not per-subtask),
  so a collision that breaks an unrelated subtask is caught.
- Cases are authored at several coupling levels and **stratified** so results can be reported
  per stratum (Limen's benefit is mechanically tied to region overlap; pooling would hide it).

### 3.2 Secondary — SWE-bench-derived batches (ecological validity)

Bundle `K` SWE-bench Verified tasks drawn from the **same** repo and run them concurrently
on one tree. Less controlled coupling, but closer to real "several agents, one repo" usage.
Report separately; do not pool with CRS.

## 4. Independent variables

- **arm** ∈ {Seq-1, Par-N-Naive, Par-N-Limen, (Par-N-Peer)}
- **N** ∈ {2, 3, 5} — headline **N = 3**
- **coupling** ∈ {disjoint, shared-region, interface}
- (held constant: model + version, temperature, harness + version, task suite hash, Limen commit)

## 5. Metrics (operational definitions)

| Metric | Definition | How measured | Lineage |
| --- | --- | --- | --- |
| **pass@1** | fraction of single trials whose final tree passes the hidden suite | run suite on final tree | pass@k, Chen et al. 2021 (unbiased estimator) |
| **pass^k** | probability that **all** k repeated trials of a cell pass | repeat the full cell k times; estimate P(all pass) | reliability framing (pass^k) |
| **wall-clock** | end-to-end time from dispatch to final tree | wall timer around the trial; report median + IQR | — |
| **lost-edit-lines** | lines one agent wrote that a later write overwrote/discarded before the final state | Limen arm: from the witness log (per-write hashes + sequence); Naive arm: from a **record-only** shadow witness (see §6) | Lost Update / P4 |
| **build-break-rate** | fraction of trials whose final tree fails to build/compile | run build before tests | Write Skew / interface breakage |
| **attribution-accuracy** | fraction of final-state hunks correctly attributed to the responsible agent | Limen: join hunk → witness → agent; Naive: `git blame` (collapses to the human) | assume-breach → audit |

`pass@1` and `wall-clock` are the **primary pair** (the Pareto axes). The other three are
the mechanism metrics that explain *why* (they should move sharply in Limen's favor on the
coupled strata and barely at all on `disjoint`).

## 6. Harness and instrumentation (avoiding bias)

- Use a **fully instrumentable OSS harness** (e.g. aider) for the controlled cells so every
  agent's writes can be routed through `limen_write` uniformly. Closed harnesses (Cursor,
  Codex) are reported only in a separate, clearly-labeled ecological run.
- **Identical instrumentation across `Par-N` arms.** Par-N-Naive runs the same write-wrapper
  as Par-N-Limen but in **record-only** mode: it records the would-be witness (for
  lost-edit-lines / attribution measurement) **without** acquiring or enforcing a lease. This
  keeps the *only* difference between the arms the conflict arbitration, not the I/O path.
- **Adherence is a measured quantity, not an assumption.** Limen is advisory; an agent that
  bypasses it would defeat prevention. Either (a) instrument the harness so all writes go
  through `limen_write`, and/or (b) report a per-trial adherence rate and treat low-adherence
  trials explicitly. Disclose any optional hard-enforcement (e.g. a pre-commit guard) and
  whether it was active.

## 7. Statistical design

- **Pre-registration.** The single confirmatory comparison is **Par-N-Limen vs Par-N-Naive at
  N = 3 on the `shared-region` stratum**, primary outcome = Pareto-dominance on
  (wall-clock, pass@1). Everything else (other N, other strata, secondary benchmark, the
  three mechanism metrics) is **secondary/exploratory** and labeled as such.
- **Repetitions.** R ≥ 20 trials per cell for pass@1 confidence intervals; pass^k needs many
  repeats of the *cell* to estimate "all-k-succeed" — power that separately and do not
  over-read underpowered pass^k cells.
- **Estimators & intervals.** Use the unbiased pass@k estimator; report bootstrap CIs for
  rates and median + IQR for wall-clock. State the LLM model/version and temperature; report
  run-to-run variance (nondeterminism inflates it) rather than hiding it behind point means.
- **Power.** Power only the pre-registered cell explicitly; treat the full
  arm × N × stratum × repetition × harness grid as a budget to be reported honestly (log what
  was *not* run rather than implying full coverage).

## 8. Analysis and figures

- **Headline figure — the Pareto plot.** Each arm is a point (or bootstrap cloud) in
  (wall-clock ↓, pass@1 ↑) space. The claim is visible iff Par-N-Limen sits up-and-left of
  Par-N-Naive (dominating), with Seq-1 as the reference. One figure should tell the story.
- **Mechanism panel.** Grouped bars with CIs for lost-edit-lines, build-break-rate, and
  attribution-accuracy, faceted by coupling stratum — showing the advantage appears on
  `shared-region`/`interface` and vanishes on `disjoint` (the honest fairness check).
- **Scaling panel.** Each metric vs N, to show where coordination stops paying off.

## 9. Reproducibility

Pin and publish, per run: model id + version, temperature/seed, harness name + version, the
`limen` commit, the task-suite content hash, and the full artifact log (every Limen witness
record + every agent transcript + the final tree + test output). The harness scripts ship in
the repo so a third party can re-run a cell. No silent caps: if coverage is bounded (top-N
cases, capped repetitions), `log` what was dropped.

## 10. Threats to validity

1. **Advisory bypass (construct).** If agents ignore the protocol, results reflect adherence,
   not the mechanism. Mitigation: route writes through `limen_write`; measure adherence; note
   that even under bypass the witness still yields attribution (audit degrades gracefully where
   prevention does not).
2. **Instrumentation bias (internal).** The write-wrapper itself could change behavior.
   Mitigation: identical record-only wrapper on Par-N-Naive; report per-harness, don't pool.
3. **Dataset coupling (external).** Benefit is tied to region overlap. Mitigation: stratify by
   coupling, report the conflict-rate distribution, don't claim generality beyond tested
   coupling; flag the MVP's literal/prefix-only region model (no globs) as a scope bound.
4. **Power / cell count (statistical).** The grid explodes and pass^k is repetition-hungry.
   Mitigation: pre-register one primary cell; power it; mark the rest exploratory.
5. **Novelty positioning.** Near-peers exist (MCP Agent Mail et al.), so a coding-scoped
   "first" claim is invalid. Mitigation: scope novelty to the generalized category + the
   controlled experiment; if feasible, run **Par-N-Peer** so the contribution is shown
   head-to-head, not asserted.

## 11. Out of scope for v0.1

This is the **design**. Building the CRS cases, the instrumented harness, and running the grid
is roadmap v0.4. Shipping the design now makes the thesis concrete and lets the community
critique the methodology before any numbers exist — which is the honest order.
