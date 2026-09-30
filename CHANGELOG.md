# Changelog

## 0.3.0 — unreleased

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
