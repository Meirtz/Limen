# Changelog

## 0.4.0-alpha.1 — 2026-10-03

The repository now centres on AOSR, an agent operating system that grows itself from an empty
image. Limen, the experiment checker, ships alongside it unchanged.

- `aosr boot` creates the empty image. `aosr grow` runs a task stream through the kernel, and
  every verified solution extends the image with:
  - the helpers it could not do without, each confirmed by a knockout check and admitted together
    with what it calls;
  - the whole program, which later tasks are probed against.
  The image is committed after every batch.
- Kernel evolution (`--epoch-every`): an engineer model rewrites the worker's operating notes and
  retrieval policy from recent failures. A rewrite is kept only if it solves more of the same
  recent tasks on an A/B with fresh samples.
- Two worlds:
  - ARC-AGI-1: programs are verified in a sandboxed child process, and on macOS the child cannot
    read task data;
  - AppWorld: interactive episodes run in worker processes, and a solution is consolidated into
    helpers plus `solve(apis)` and replayed in a fresh world.
- `aosr eval` scores frozen images on held-out tasks at every budget prefix. `aosr report` compares
  arms at a common budget with paired bootstrap intervals.
- Images are content-addressed and keep their lineage:
  - `aosr show`, `aosr lineage`, `aosr knockout` and `aosr slot` inspect and derive images;
  - `aosr demo` writes an HTML page of how an image grew.
- Model calls go through the `claude` CLI, and are cached and replayable. CI never makes a live
  call.
- Measured: see the README. On 400 held-out ARC-AGI-1 evaluation tasks, an image grown by the same
  small model solved 45 against 35 for the empty image at the same number of calls. In AppWorld a
  grown image solved more tasks within 8 and within 12 turns. Kernel rewrites passed their gate
  but did not measurably help on held-out tasks.

## 0.3.0-alpha.1 — 2026-10-01

Limen is rebuilt from scratch as a small, dependency-free Python tool that checks whether an
experiment ran the way it claims.

- `limen run` records what a Python run actually executed: the environment variables it read,
  the files it read, listed and wrote, the child processes it started, and the modules it imported,
  with their bytes, their location and their git state. Executed sources are kept in the store.
- `limen check` reports runs that did not finish, packages imported from a stale copy instead of
  their source, standard-library modules shadowed by project files, declared treatments the code
  never read, declared gates that never ran or that fail their known-good/known-bad controls, and
  held-out data read (directly or through derived files) by runs not allowed to read it.
- `limen compare` checks that two arms differ in the declared treatment and in nothing else,
  that both were scored on the same items with the same rate of never-evaluated items, and
  estimates the effect with a confidence interval.
- `limen trace` walks a file back to the runs and inputs that produced it; `limen leak` finds
  held-out item ids inside training or tuning files.
- `bench/` contains two small experiment loops, development and held-out fault scenarios, and a
  harness comparing Limen with MLflow-style and Sacred-style tracking. On 27 held-out scenarios
  written by independent authors after the checks were frozen, Limen blocked 14 of 18 invalid
  comparisons and 2 of 9 valid ones (MLflow-style tracking: 7 and 2; Sacred-style: 8 and 3).
- Records are private by default: the store is owner-only, ignores itself in git, and secret-looking
  values (by name or by shape) in the environment, command lines, child command lines, exception
  text, parameters and credential files are replaced by keyed digests.

The previous design (advisory write leases over MCP, a Rust daemon) is retired. Its final state is
tagged `leases-final`.
