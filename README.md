# Limen

**Limen checks that an experiment ran the way it claims.**

When coding agents run the experimental loop (editing code, launching runs, reading the numbers,
reporting the result), most of what goes wrong is not sabotage. It is a number that does not mean
what it says:

- the treatment never executed (a stale copy of the package shadowed the edited one, or a
  variable was misspelled);
- the arms differed in more than the treatment (a config was edited between runs, the data was
  regenerated);
- one arm's items crashed or failed to parse and were counted as failures;
- a gate was skipped, or could not fail;
- held-out data leaked into training or selection.

Limen records what a Python run actually executed and checks it against what the run declared.
It is a small library and CLI with no dependencies. It observes and reports; it does not run your
experiments or change what they do.

## Install

```bash
pip install git+https://github.com/Meirtz/Limen
```

Python 3.10 or newer. No dependencies (on 3.10, `tomli` to read the config file).

## Use

Run each arm under `limen run`, declaring what the arm changes:

```bash
limen run --name wiki --arm base --treatment env:WIKI_ROOT eval.py
WIKI_ROOT=kb/rich limen run --name wiki --arm rich --treatment env:WIKI_ROOT eval.py
limen compare wiki:base wiki:rich
```

If `eval.py` picked up a stale copy of your package that never reads `WIKI_ROOT` (here, a
leftover `sys.path.insert(0, "deploy")`), the comparison is refused:

```
$ limen compare wiki:base wiki:rich
A: 1 run(s) ['wiki:base']
B: 1 run(s) ['wiki:rich']
treatment: env:WIKI_ROOT
effect item_pass_rate: A=0.6 B=0.6 delta=+0  95% CI [+0, +0]
BLOCK PLACEBO [env:WIKI_ROOT]: WIKI_ROOT was set differently in the arms but no run ever read it
BLOCK SHADOWED [kb]: package 'kb' was imported from /repo/deploy/kb, not from its source /repo/src/kb (2 runs: ...)
BLOCK TREATMENT_NOT_READ [env:WIKI_ROOT]: the declared treatment variable was never read by the process, so this arm cannot differ from its control (2 runs: ...)
WARN  EFFECT_WITHIN_NOISE [item_pass_rate]: pass-rate delta +0.000, 95% CI [+0.000, +0.000] over 40 items includes 0
3 blocking, 1 warnings
```

With the stray line removed, the same commands give:

```
treatment: env:WIKI_ROOT
  env:WIKI_ROOT: A=<unset> | B=kb/rich
effect item_pass_rate: A=0.6 B=1 delta=+0.4  95% CI [+0.25, +0.55]
INFO  EXPLAINED_BY_TREATMENT: 2 input(s) read by one arm only lie under a treatment path
0 blocking, 0 warnings
```

`compare` exits 1 on any blocking finding. Report per-item results and metrics from the experiment so the arms can be checked item by item:

```python
import limen

for item in items:
    limen.outcome(item.id, "pass" if ok else "fail")   # or "error", "timeout", "infra", "skip"
limen.metric("accuracy", acc)
lr = limen.param("lr", cfg.lr)
```

Gates can be given known-good and known-bad controls, so a gate that cannot fail is caught before
it judges anything:

```python
@limen.gate("correct", positives=[reference_solution], negatives=[null_solution])
def correct(candidate): ...
```

Other commands: `limen check RUN` (one run), `limen trace FILE` (which run produced a file, from
what), `limen leak --holdout test.jsonl --key id train.jsonl` (held-out ids inside data files),
`limen ls`, `limen show`. Declaring `holdout = ["data/test.jsonl"]` in `limen.toml` makes any run
that is not an evaluation and reads it, directly or through derived files, a blocking finding.

Every check and field is described in [docs/reference.md](docs/reference.md).

## What it catches, measured

`bench/` runs two small experiment loops (a trainer and a code judge) through fault scenarios,
and compares Limen with what MLflow-style tracking (git commit, command-line and logged parameters,
package versions, run status) and Sacred-style tracking (plus source hashes and host) would let
you catch with the same comparison rule.

| scenarios | detector | invalid comparisons caught | valid comparisons flagged |
|---|---|---|---|
| development (in-sample) | MLflow-style | 2 / 12 | 0 / 7 |
| development (in-sample) | Sacred-style | 4 / 12 | 1 / 7 |
| development (in-sample) | Limen | 11 / 12 | 0 / 7 |
| **held-out** | MLflow-style | 7 / 18 | 2 / 9 |
| **held-out** | Sacred-style | 8 / 18 | 3 / 9 |
| **held-out** | **Limen** | **14 / 18** | **2 / 9** |

The development scenarios were written together with Limen's checks, so only the held-out row
says anything about generalization. The held-out scenarios were written after the checks were
frozen (tag `bench-freeze`), by three independent authors who never saw Limen's source or output,
and labeled by a separate blind adjudicator (27 of 27 labels agreed). They are small, synthetic and
all Python, so they measure whether known kinds of fault are caught, not how common they are.

On the held-out set:

- Seven invalid comparisons were caught only by Limen: arms scored on different items or with
  different rates of unparseable items (three times, once because of stray `._*.py` files in one
  arm's candidates), a stale verdict cache (twice), a variable left exported in one arm's shell,
  and a data snapshot re-pointed between arms.
- Three catches came from a side effect rather than the fault itself: two "best of N seeds"
  comparisons were flagged because the compared runs used different seeds, and a threshold tuned
  on the validation split was flagged because the tuned arm read a different config file. The
  tracking baselines catch these too.
- Four were missed: a feature computed from the evaluation split's own labels, a noise-level
  effect (warned as `EFFECT_WITHIN_NOISE`, not blocked), a treatment value misspelled so the code
  silently fell back to its default, and candidate code importing the judge's test cases.
- Two valid comparisons were flagged: a config file that was only reformatted between arms, and a
  content-addressed verdict cache that skipped the declared gate on cache hits.

Per-scenario results: [bench/results/results.md](bench/results/results.md); protocol:
[bench/README.md](bench/README.md).

## What it cannot see

- Reads made by child processes, workers, other processes, or native code that opens files itself
  (Arrow datasets, safetensors, HDF5). Limen lists the children and native readers it saw.
- Whether a check is *meaningful*: a gate that tests the wrong thing but can pass and fail looks
  healthy. Limen catches gates that did not run or cannot fail, not weak tests.
- Anything a determined adversary does: records are not tamper-evident.

## Status

Alpha. The record format (`limen.run/1`) and the CLI may still change. The previous design of
this repository (advisory write leases for concurrent agents) is kept at the tag `leases-final`.

Licensed under MIT or Apache-2.0, at your option.
