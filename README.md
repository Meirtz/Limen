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
It is a small, dependency-free library and CLI. It observes and reports; it does not run your
experiments or change what they do.

## Install

```bash
pip install git+https://github.com/Meirtz/Limen
```

Python 3.10 or newer, no dependencies.

## Use

Run each arm under `limen run`, declaring what the arm changes:

```bash
limen run --name wiki --arm base --treatment env:WIKI_ROOT eval.py
WIKI_ROOT=kb/rich limen run --name wiki --arm rich --treatment env:WIKI_ROOT eval.py
limen compare wiki:base wiki:rich
```

If `eval.py` imported a stale copy of your package that never reads `WIKI_ROOT`:

```
BLOCK PLACEBO [env:WIKI_ROOT]: WIKI_ROOT was set differently in the arms but no run ever read it
BLOCK SHADOWED [kb]: package 'kb' was imported from /repo/deploy/kb, not from its source /repo/src/kb (2 runs)
BLOCK TREATMENT_NOT_READ [env:WIKI_ROOT]: the declared treatment variable was never read by the process, ... (2 runs)
```

If the comparison is clean, `compare` prints the effect with a confidence interval and exits 0.
Report per-item results and metrics from the experiment so the arms can be checked item by item:

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

RESULTS_PLACEHOLDER

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
