# Limen — Hero Experiment Design

> **Status: apparatus implemented; the full study is future work.** The measurement harness lives
> in [`crates/limen-bench`](../crates/limen-bench): the experimental arms, a coordination-independent
> oracle, the coupling-class task family, and `pilot` / `sweep` / `analyze` subcommands. What remains
> is the *pre-registered, contamination-controlled study at scale* — this document fixes that design
> so the claim stays concrete, falsifiable, and reviewable.
> Companions: [`spec/related-work.md`](spec/related-work.md) (related work + framing),
> [`PRD.md`](PRD.md) §10.2 (product-level summary), [`spec/philosophy.md`](spec/philosophy.md) (Principle 7).
>
> **Numeric discipline:** every measured quantity below is a `[TBD — measure]`. No headline numbers
> are quoted here; do not import figures from related work as if they were ours.

## 1. The claim under test

> Coordination's value is **conditional on task coupling**, and **reliability** (not single-shot
> success) is where uncoordinated concurrency fails first. As writers (N) and coupling rise,
> naive concurrency's **pass^k** (all of k repeated runs succeed) collapses super-linearly through
> lost edits and broken builds; advisory coordination recovers most of that cost **below a coupling
> threshold τ** (a Pareto-improvement — added safety at ~no time cost), while **above τ** the safety
> gain persists but the wall-clock advantage inverts.

It is deliberately a **conditional, coupling-dependent** claim, not "more agents = better." The
multi-agent scaling literature shows parallelism has diminishing or negative returns past a strong
single-agent baseline, and the nearest empirical anchor (CodeCRDT) finds coordination helps on some
tasks and hurts on others — exactly the two sides of τ this claim predicts. The contribution is that
an advisory coordination layer *moves the coordination-cost curve*, and that the crossover τ is
measurable and predictable from task structure.

**Primary endpoint:** **pass^k** (reliability across k repeated concurrent runs) on the
pre-registered primary cell. **Null hypothesis (H0):** advisory coordination does not improve pass^k
on coupled strata, and there is no measurable coupling threshold separating Pareto-improvement from a
safety–time tradeoff. Rejecting H0 on the primary cell is the result.

## 2. Arms (single-factor ablation)

| Arm | Setup | Isolates |
| --- | --- | --- |
| **Seq-1** | one agent, the N subtasks done sequentially | correctness ceiling + latency baseline |
| **Par-N-Naive** | N agents, one shared working tree, **no** coordination | the cost of uncoordinated concurrency |
| **Par-N-Limen** | N agents + the Limen MCP server (advisory leases) | the treatment |
| **Par-N-Peer** *(optional)* | N agents + the nearest prior-art peer (e.g. MCP Agent Mail) | head-to-head vs existing work |

Task, model, temperature, N, and the agent harness are held constant across arms. The
**only** difference between the two `Par-N` arms is whether writes go through a lease.

### 2.1 As implemented in `limen-bench`

The harness implements four arms over a pilot task, each matched on I/O path so the only
difference is the coordination policy:

| Implemented arm | Coordination policy |
| --- | --- |
| `naive` | stale read, last-writer-wins (no coordination) |
| `limen-placebo` | witnessed lease but **stale** read — isolates the wrapper from the arbitration |
| `limen` | witnessed write lease + **fresh** read — composes same-file edits |
| `limen-deps` | `limen` plus an advisory write×read reconciliation round — also recovers cross-file skew |

Run them with `cargo run -p limen-bench -- pilot <model-id…>` (full grid), `… sweep <model-id…>`
(coupling-fraction sweep), and `… analyze [results.jsonl]` (per-(task, coordination) pass rate with
95% Wilson intervals). Endpoints and credentials come from the environment; nothing provider-specific
is committed. The compute-free mechanism (`arm` + `sim`) and a custom-resource integration test run
without any model.

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
| **lost-edit-lines** | lines one agent wrote that a later write overwrote/discarded before the final state | a **coordination-independent external oracle** (replay each agent's edit stream against the final tree) — the *same* instrument for every arm, so the ruler never differs across arms | Lost Update / P4 |
| **build-break-rate** | fraction of trials whose final tree fails to build/compile | run build before tests | Write Skew / interface breakage |
| **attribution-accuracy** | fraction of final-state hunks correctly attributed to the responsible agent | scored against an **independent ground-truth attribution map** (the witness gives ground-truth-at-source; `git blame` collapses to the human) — both judged by the same gold | assume-breach → audit |

**`pass^k` is the primary endpoint** (reliability across repeated concurrent runs); `wall-clock`
(normalized per 1000 output chars) is the cost axis; `pass@1` is secondary. lost-edit-lines,
build-break-rate, and attribution are the mechanism metrics that explain *why* — they should move
sharply in Limen's favor on the coupled strata and barely at all on `disjoint`.

## 6. Harness and instrumentation (avoiding bias)

- Use a **fully instrumentable OSS harness** (e.g. aider) for the controlled cells so every
  agent's writes can be routed through `limen_write` uniformly. Closed harnesses (Cursor,
  Codex) are reported only in a separate, clearly-labeled ecological run.
- **Identical instrumentation across `Par-N` arms.** Par-N-Naive runs the same write-wrapper
  as Par-N-Limen but in **record-only** mode — no lease acquired or enforced — so the *only*
  difference between the arms is conflict arbitration, not the I/O path. Metrics (lost-edit-lines,
  attribution) are computed by a **coordination-independent external oracle**, not the wrapper, so
  the measurement ruler is identical across arms; a **placebo arm** (wrapper installed, arbitration
  disabled) isolates the wrapper's effect from arbitration.
- **Adherence is a measured quantity, not an assumption.** Limen is advisory; an agent that
  bypasses it would defeat prevention. Either (a) instrument the harness so all writes go
  through `limen_write`, and/or (b) report a per-trial adherence rate and treat low-adherence
  trials explicitly. Disclose any optional hard-enforcement (e.g. a pre-commit guard) and
  whether it was active.

## 7. Statistical design

- **Pre-registration (OSF, locked before any runs).** The single confirmatory comparison is
  **Par-N-Limen vs Par-N-Naive at N = 3 on the `shared-region` stratum**; primary endpoint =
  **pass^k superiority**, with a non-inferiority (TOST) test on wall-clock to establish the
  Pareto-improvement regime, plus a pre-specified test for the coupling threshold τ. Everything else
  (other N, other strata, secondary benchmark, the mechanism metrics) is **secondary/exploratory**
  and labeled as such.
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

- **Headline figure — the reliability–cost frontier across coupling.** Plot each arm in
  (wall-clock ↓, **pass^k** ↑) space, one panel per coupling stratum (with a sweep over N). The
  story is the frontier *shifting*: below τ, Par-N-Limen sits up-and-left of Par-N-Naive
  (Pareto-improvement); above τ it sits up-and-right (more reliable, slower) — the crossover τ is
  the result. Seq-1 and Par-N-Worktree are the references.
- **Threshold figure.** (Δpass^k, Δwall-clock) vs continuous coupling p, per N — locating τ(N) and
  showing it decreases with N.
- **Mechanism panel.** Grouped bars with CIs for lost-edit-lines, build-break-rate, and
  attribution-accuracy, faceted by coupling stratum — the advantage appears on
  `shared-region`/`interface` and vanishes on `disjoint` (the honest fairness check).
- **Model-fit panel.** Measured interference vs the predicted `N²·p·e` curve, and recovered-cost
  fraction vs adherence α — model vs a coupling-independent null.

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
