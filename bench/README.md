# Limen bench

A fault-injection benchmark: does a detector object to an invalid A/B comparison *before* the
result is accepted, and stay quiet on valid ones?

## Loops

- `loops/train`: logistic regression in pure Python on synthetic data with a curved class
  boundary. The treatment switches on squared features (`TRAINER_FEATURES=quad`); `data/test.csv`
  is held out.
- `loops/evalgate`: judges candidate solutions (`candidates/v1`, `candidates/v2`) with a
  correctness gate that has known-good and known-bad controls; reports per-item outcomes.

Both run in seconds on a CPU and use only the standard library.

## Scenarios

A scenario (`scenarios/<set>/<name>.py`) copies a loop into a fresh git repository, optionally
injects a fault, runs arm A and arm B under `limen run`, and records the claim a naive
experimenter would make (B beats A on the claim metric). See `harness.py` for the format.

- `dev/`: written together with Limen's checks. Used to develop them, so these numbers are
  in-sample and say little about how Limen generalizes.
- `heldout/`: written after the checks were frozen (tag `bench-freeze`) by independent authors who
  had the loops, the scenario format and a broad, tool-agnostic list of how A/B experiments go
  wrong, but not Limen's source, its checks, or any detector output. A separate adjudicator, also
  blind to detector output, labeled each scenario fault or valid from its code and description.
  Scenarios where author and adjudicator disagreed are reported separately and excluded from the
  headline numbers.

## Detectors

- `none`: never objects.
- `mlflow`: fields an MLflow-style tracker records (git commit at start, command-line and logged
  parameters, declared protocol, package versions, Python version, run status).
- `sacred`: `mlflow` plus what a Sacred-style observer adds (dirty flag, hashes of imported local
  sources, host information).
- `limen`: `limen compare` with run checks; objects on any blocking finding.

All detectors get the same declared treatment and the same comparison rule (a field other than the
treatment differs, the treatment does not differ, or a run did not finish), so the comparison
isolates what each can see.

## What this measures, and what it does not

It measures detection of known classes of validity faults in small, controlled loops, and false
alarms on valid comparisons. It does not measure how often these faults happen in real projects,
how costly they are, or detection on workloads with native readers, worker processes or cluster
launchers. Faults outside Limen's scope (a weak but working test, reward design, noise in shared
hardware) are included in the held-out set if their authors wrote them, and count as misses.

## Run it

```bash
python bench/harness.py dev heldout --out bench/results
```

Results are written to `results/results.json` (every scenario, detector verdict and Limen finding)
and `results/results.md`.
