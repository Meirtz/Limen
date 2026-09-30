# Limen reference

## The model

An experiment run *declares* what it is: its name and arm, its role, the treatment it changes,
the gates it must pass and the protocol it shares with other arms. `limen run` records what the
run *executed*. Every check compares the two.

```
limen run --name retrieval --arm rich --role eval \
          --treatment env:WIKI_ROOT --gate correct --protocol tolerance=1e-4 \
          eval.py --split val
```

The same declarations are available in Python: `with limen.run("retrieval", arm="rich", ...)`,
and `limen.declare(...)` inside a run.

## What a record contains

| Field | How it is observed |
|---|---|
| environment variables read | `os.environ` is instrumented in place; bulk copies (`dict(os.environ)`) are recorded as such, not as reads of every variable |
| declared treatment variables | their values at start, whether or not the code reads them |
| files read, listed, created, written | PEP 578 audit events (`open`, `os.listdir`/`scandir`, `glob`, `os.mkdir`, `os.rename`, `sqlite3.connect`); content sha256 up to `max_hash_bytes`, else size and mtime |
| executed code | every project module and the main script, hashed when the import system loaded it, with its path, origin and git state against the HEAD the run started from; the source is copied into the store |
| child processes and workers | `subprocess`, `os.system`, `os.exec*`, `os.posix_spawn`, `os.fork`, multiprocessing spawn |
| reported values | `limen.outcome(item, status)`, `limen.metric(name, value)`, `limen.param(name, value)`, gates |
| status | `ok`, `failed`, `interrupted`, `killed` (SIGTERM, SIGHUP, unless the script or its launcher handles them); a record left `running` means the process died without warning |

Statuses for `limen.outcome`: `pass` and `fail` for items that were evaluated; `error`, `timeout`,
`infra` and `skip` for items that were not. Keep the two apart: that is what makes
`ASYMMETRIC_NOT_EVALUATED` possible.

**Blind spots.** Reads made by child processes and workers, by native code that opens files
itself (Arrow/Parquet datasets, safetensors, HDF5), and by other processes. The record lists the
child processes and native readers it saw (`CHILD_PROCESSES`, `NATIVE_READERS`) so a clean report
is not mistaken for a complete one.

## Specs

Treatments and waivers select *fields* of a run's identity:

| Spec | Selects |
|---|---|
| `env:NAME`, `env:PREFIX_*` | environment variables the code read |
| `arg:--flag`, `arg:#0`, `arg:key` | command-line flags, positionals, Hydra-style `key=value` overrides |
| `param:name` | values reported with `limen.param` |
| `file:path`, `file:dir/` | files and directory listings, relative to the project root |
| `module:pkg.mod`, `code` | executed code (a module and its submodules; any code) |
| `dist:name`, `protocol:key` | installed distributions; declared protocol fields |
| `python`, `platform`, `gpu` | interpreter, OS/architecture, GPU models |

## Checks

`limen check RUN` reports the run-level findings; `limen compare A B` reports them for every run
involved plus the comparison findings. Blocking findings make the command exit 1.

### Did the run finish, and run the code it claims?

| Code | Severity | Meaning |
|---|---|---|
| `INCOMPLETE` | block | The record was never finalized: the process was killed outright or is still running. |
| `RUN_FAILED` | block | Nonzero exit, exception or termination signal. Its outputs are not results. |
| `SHADOWED` | block | A project package was imported from somewhere other than its source (a stale copy, an old installed version). |
| `SHADOWS_STDLIB` | block | A project file named like a standard-library module replaced it. |
| `CODE_CHANGED_DURING_RUN` | block | An executed source file changed while the run was going. The record keeps the bytes that were loaded. |
| `CODE_EDITED_BEFORE_RUN` | warn | With `limen.run()` in a long-lived process: a module imported before the run was edited after the process started, so the loaded code may not be what the record shows. |
| `AMBIGUOUS_IMPORT` | warn | Several copies of a project package are importable; the first on `sys.path` wins. |
| `EXTERNAL_CODE`, `UNTRACKED_CODE` | warn | Executed code outside the project, or not in git. Copies are kept in the store. |
| `MODIFIED_CODE` | info | Executed code differs from the HEAD the run started from. |
| `GENERATED_CODE` | info | Modules generated during the run (compiler and JIT caches) are listed but not counted as code identity. |

### Did the treatment reach the code?

| Code | Severity | Meaning |
|---|---|---|
| `TREATMENT_NOT_READ` | block | The run never read the declared treatment variable or parameter: this arm cannot differ from its control. |
| `TREATMENT_OVERRIDDEN` | block | The run set the variable itself before reading it. |
| `TREATMENT_REWRITTEN` | warn | The run read the variable and then set it; later reads see the run's value. |
| `TREATMENT_UNVERIFIED` | warn | Not read by name, but a bulk copy of the environment or a child process could have used it. |
| `PLACEBO` | block | Across arms, the treatment was never observed, or had the same value in both. |
| `TREATMENT_NOT_APPLIED` | block | Some runs have the other arm's treatment value: the arms are mixed. |

### Did anything else differ?

| Code | Severity | Meaning |
|---|---|---|
| `CONFOUND` | block | An input other than the treatment differs between the arms, in value or in mix. Waive it with `--waive` or `waive = [...]` in the config if it cannot matter. |
| `INPUT_ONLY_IN_ONE_ARM` | warn | An input was read by one arm only and the treatment does not obviously explain it. |
| `POSSIBLE_CONFOUND` | warn | A variable differs between arms and reached the code only through a bulk copy of the environment (a settings loader). |
| `TREATED_AS_OUTPUTS` | info | Differing values that were treated as output locations or run labels (`--out`, `--run-name`), listed so a wrong call is visible. |
| `EXPLAINED_BY_TREATMENT`, `TREATMENT_RECORDED_TWICE`, `REPLICATE_FIELDS` | info | Differences Limen attributed to the treatment or to replicates (seeds), listed so they can be checked. |

Only run-level settings (`param:`, `arg:`, `env:`) may vary inside both arms as replicates.
Launcher identifiers such as `MASTER_PORT` and `SLURM_JOB_ID` are waived by default.

### Were the arms measured the same way?

| Code | Severity | Meaning |
|---|---|---|
| `ITEMS_DIFFER` | block | The arms, or some runs, were scored on different items. |
| `ASYMMETRIC_NOT_EVALUATED` | block | Items that were never evaluated (errors, timeouts, infrastructure failures) are much more frequent in one arm. |
| `METRIC_MISSING` | block | A requested metric is missing or not a finite number in some run. |
| `NOT_EVALUATED`, `DUPLICATE_OUTCOMES`, `METRIC_NOT_FINITE` | warn | Signs of a harness problem in a single run. |
| `EFFECT_WITHIN_NOISE`, `NO_REPLICATES`, `FEW_ITEMS`, `SIGN_DEPENDS_ON_ERRORS` | warn | The effect is not resolved, cannot be estimated, or depends on how unevaluated items are counted. |
| `OMITTED_RUNS` | warn | Completed runs of the same name and arm are left out of the comparison. |

Effects: metric deltas get a Welch interval (conservative Student-t quantiles) when both arms
have at least two runs; per-item pass rates get a paired bootstrap interval over shared items.

### Can the gates fail?

| Code | Severity | Meaning |
|---|---|---|
| `GATE_NOT_EXECUTED` | block | A declared gate never ran. |
| `GATE_CONTROLS_FAILED` | block | The gate accepted a known-bad control or rejected a known-good one. By default it also raises before judging any real candidate. |
| `GATE_UNCONTROLLED` | warn | A declared gate ran without controls. |
| `GATE_NEVER_FAILED` | warn | The gate passed at least 20 candidates and was never shown to reject anything. |

```python
@limen.gate("correct", positives=[reference], negatives=[limen.Case(("add", "return None"), label="null")])
def correct(task, source): ...
```

### Did anything read held-out data?

| Code | Severity | Meaning |
|---|---|---|
| `HOLDOUT_READ` | block | A run whose role is not an allowed reader (default: `eval`) read a held-out path. |
| `HOLDOUT_DERIVED` | block | It read a file produced, directly or through other runs, from held-out data. |

`limen leak --holdout test.jsonl --key id train.jsonl` finds held-out item ids inside data files
(`.jsonl`, `.json`, `.csv`, `.tsv`, one id per line).

### Where did this file come from?

`limen trace FILE` walks the records back from a file to the runs and inputs that produced it:
`NO_RECORD` (warn), `CHANGED_SINCE_RUN` (warn), `FROM_FAILED_RUN` (block).

### What the record cannot see

| Code | Severity | Meaning |
|---|---|---|
| `CHILD_PROCESSES` | warn | Child processes or workers ran; their reads are not in the record. |
| `NATIVE_READERS` | info | Modules that read files natively were loaded. |
| `INPUT_CHANGED_DURING_RUN` | warn/info | Files read were created or modified after the run started (its own native writes, or another process). They are not counted as inputs. |
| `ENV_NOT_OBSERVED` | warn | `os.environ` had been replaced before the run. |

## Configuration

`[tool.limen]` in `pyproject.toml`, or `limen.toml`, at the project root (the git work tree of the
script being run):

```toml
holdout = ["data/test.jsonl", "data/private/"]   # readable only by holdout_readers
holdout_readers = ["eval"]
waive = ["env:WANDB_RUN_ID"]                      # differences that cannot matter
packages = { mypkg = "src/mypkg" }                # where a package's source lives, if not src/<pkg> or <pkg>
max_hash_bytes = 67108864                         # larger files are identified by size and mtime
store = ".limen"
```

Environment: `LIMEN_HOME` (store location), `LIMEN_ROOT` (project root), `LIMEN_HOLDOUT`
(extra held-out paths, `os.pathsep`-separated).

## The store

`.limen/runs/<id>.json` (schema `limen.run/1`), `.limen/objects/` (executed sources by sha256) and
`.limen/key` (the key for secret digests). The directory is private to its owner and ignores
itself in git. Secret-looking values in the environment, the command line, child command lines and
exception text are replaced by keyed digests, which compare equal within one store and cannot be
reversed by guessing.
