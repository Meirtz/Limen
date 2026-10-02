# AOSR

**An agent operating system that grows itself from an empty image.**

AOSR boots with nothing in it. As a model works through a stream of tasks, AOSR keeps what it
learns in a form it can run:

- **capabilities**: functions the model wrote that a verified solution could not do without.
  They are loaded into every later task, and every call to them is traced;
- **programs**: whole solutions that worked. On a new task AOSR runs them against the task's
  examples and shows the model the closest ones; when one already fits, the task is solved with no
  model call at all;
- **its own kernel**: operating notes and a retrieval policy that AOSR rewrites from its failures.
  A rewrite is kept only if it solves more of the same recent tasks than the version it replaces.

Every state is a content-addressed image with a parent. You can boot an image, diff it, knock
capabilities out of it, or replay how it grew. Model calls and sandbox runs are cached, so a growth
run replays exactly, offline.

## Results

All numbers come from a small, cheap model (Claude Haiku 4.5 with extended thinking off) working
through each image. They are compared with the same model working from the empty image, on
held-out tasks no growth run read, at the same number of model calls. Intervals are paired
bootstrap 95% intervals over tasks.

**ARC-AGI-1, 400 evaluation tasks.** Two model calls per task (write a program, then repair it
once).

| image | solved | model calls | USD (list price) |
|---|---|---|---|
| empty (`head_0`) | 35 | 774 | 5.45 |
| grown by the same model on 200 training tasks | **45** (+2.5 pts, 95% CI [+0.3, +4.8]) | 766 | 6.06 |
| empty image, four calls per task | 44 | 1490 | 10.65 |

The grown image matches the empty image given twice the calls, at 57% of the cost. Four of its 45
solutions needed no model call: a program grown on a training task already fit.

**AppWorld, 57 dev tasks** (an ecosystem of nine apps behind 457 APIs; tasks are judged by the
benchmark's own state checks). Up to 20 turns per task.

| image | solved by turn 8 | by turn 12 | by turn 20 | model calls |
|---|---|---|---|---|
| empty | 11 | 15 | 21 | 646 |
| grown on 90 training tasks | **17** (+10.5 pts, [0.0, +21.1]) | **22** (+12.3 pts, [0.0, +24.6]) | 24 (+5.3 pts, [−5.3, +15.8]) | 407 |

The grown image solves in 11 turns what the empty one solves in 20, with 37% fewer calls. It grew
58 capabilities up to three layers deep: account and login services, a paginator, readers for each
app's libraries, and task-level operations built on those.

**The kernel rewrites itself.** In a 200-task ARC growth run with an evolution step every 50 tasks,
three of four proposed kernel rewrites passed the A/B gate. The operating notes the system wrote
for itself grew from "describe the rule, check it on every example, trace the code by hand" to
rules targeted at its own observed failures, such as never submitting code with undefined helpers.
At the last step it also retuned its retrieval policy. It now shows up to three earlier programs,
but only those that already get at least 80% of the example cells right. That is the same lesson
the over-trust failures below teach. In AppWorld none of three proposals passed.

The rewrites did not measurably help on held-out tasks. On the 100 dev tasks the evolved image
solved 34, and its notes alone on the empty image solved 31, against 36 and 33 in two runs of the
empty image. The gate accepts what helped on 30 recent stream tasks, and at that window size it
cannot tell a real improvement from noise (see below).

### What did not work

These results are reported as measured.

- **A library shown indiscriminately hurts.** On the 100 dev tasks, an image whose index listed
  three unrelated helpers solved 26 against 36 for the empty image. The model called a helper
  whose name sounded right but whose result was wrong. With the index hidden it solved 32.
- **In AppWorld the model over-trusted grown helpers.** In a first run, helpers told it to "call
  directly" led it to finish in one to four turns with wrong answers (20 solved against 24).
  Telling it that each helper was right for the task it came from but may not fit this one, and
  showing reuse counts, reversed this.
- **Helpers alone do not carry ARC.** Precedent programs do. In an earlier pilot on 80 training
  tasks, with an image grown by the same model with extended thinking on, helpers alone gave 22
  against 23 for the empty image; adding the closest earlier programs gave 28.
- **The kernel's A/B gate is noisy.** Two runs of the same image differ by about 3 points on 100
  tasks, so a 30-task window accepts a change with no effect about one time in ten at the default
  margin of 3.

## Quickstart

```bash
pip install git+https://github.com/Meirtz/Limen
aosr fetch                                   # ARC-AGI-1 at a pinned commit, into ~/.cache/aosr
aosr boot                                    # head_0: the empty image
export AOSR_ALLOW_LIVE=1                     # model calls go through the `claude` CLI
aosr grow --run demo --n 50 --checkpoints 25,50 --out runs/demo --epoch-every 25
aosr eval --image demo/n50 --arm grown --split dev --out runs/demo/grown.jsonl
aosr eval --image head_0 --arm empty --split dev --out runs/demo/empty.jsonl
aosr report --budget 2 runs/demo/empty.jsonl runs/demo/grown.jsonl \
    --compare runs/demo/empty.jsonl@2,runs/demo/grown.jsonl@2
aosr show demo/n50                            # what it grew
aosr demo --run demo --grow-log runs/demo/grow.jsonl --out demo.html runs/demo/*.jsonl
```

Live calls need the [`claude` CLI](https://docs.anthropic.com/en/docs/claude-code) on the path.
Without `AOSR_ALLOW_LIVE=1`, every model call is refused, and runs can only replay from the cache.
The AppWorld world needs Python 3.11 and `pip install appworld`
(`aosr grow --world appworld ...`).

Two pages written by `aosr demo` show what a grown image looks like (download them and open them
in a browser):
- [docs/demo/arc-grown.html](docs/demo/arc-grown.html), the image behind the evaluation-split
  result;
- [docs/demo/arc-grown-and-evolved.html](docs/demo/arc-grown-and-evolved.html), a run that also
  rewrote its kernel.

## How it works

**Images.** The store holds immutable objects addressed by the SHA-256 of their content:
capabilities, programs, and kernel slots (`notes`, `policy`). An image names one version of each,
plus use counts, and points at its parent. `aosr lineage`, `aosr show`, `aosr knockout` and
`aosr slot` work on images.

**Solving a task.**
- The kernel loads the image's capabilities into a sandboxed child process, one capability at a
  time, so a broken one cannot take the rest down.
- It runs every one-argument capability and every stored program on the task's example inputs,
  and ranks them by how close they come to the example outputs.
- An exact match is verified and submitted with no model call.
- Otherwise the worker sees the closest capabilities and programs, and alternates fresh attempts
  with repairs that show it where its output was wrong.

**Growing.**
- A solution that is correct on its task's held-back test contributes its program as a precedent.
- It also contributes each helper the program cannot do without: one that, replaced by an identity
  stub, breaks the solution. Such helpers are admitted together with what they call.
- A helper is not admitted if it reads module state, or if it behaves exactly like an existing
  capability (its callers are pointed at that capability instead).
- The task's `solve`, stripped of its helpers, must still verify on top of the extended library.
- In AppWorld, a consolidation call first rewrites the solved episode as helpers plus
  `solve(apis)`. This is replayed in a fresh copy of the task's world before anything is kept.

**Evolving the kernel.**
- Every N tasks, an engineer model reads digests of recent failures and successes. It proposes new
  operating notes and, optionally, a new policy: how much of the library and of earlier programs
  the worker is shown.
- The proposal runs against the current kernel on the most recent stream tasks. It uses fresh
  samples, and an image without what those tasks themselves contributed.
- It is kept only if it solves at least `margin` more of them.

**Measuring.** Attempts follow a fixed schedule, so one run at budget B also scores every budget
b ≤ B. Arms are compared at matched calls with a paired bootstrap. The ARC evaluation split cannot
be loaded without `AOSR_UNLOCK_EVAL=1`. On macOS the sandbox cannot read task data or reach the
network. CI runs offline: a test fails if anything tries to start the `claude` CLI.

## Layout

```
src/aosr/   the operating system: store, kernel, sandbox, admission, growth, evolution,
            evaluation, statistics, worlds (ARC-AGI-1, AppWorld), CLI, demo page
src/limen/  Limen, the experiment checker this project grew out of
```

Limen checks that an experiment ran the way it claims: the treatment executed, nothing else
changed between arms, gates could fail, held-out data stayed out. See
[docs/limen-checker.md](docs/limen-checker.md).

## License

MIT or Apache-2.0, at your option.
