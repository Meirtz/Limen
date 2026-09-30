# Contributing to Limen

Limen is a small, dependency-free Python tool. Keep it that way: no runtime dependencies, and
new checks only when a real validity failure needs them.

## Local checks

```bash
uv venv && uv pip install -e . pytest ruff mypy
.venv/bin/ruff format --check src tests bench
.venv/bin/ruff check src tests bench
.venv/bin/mypy
.venv/bin/python -m pytest
```

A change is done when all four are clean and a test covers the new behavior. A new or changed
check also needs a bench scenario (`bench/scenarios/dev/`) showing what it catches, and the held-out
results in the README must be re-run and reported honestly if detection logic changes.

## Branches and merges

`main` is protected: changes land through pull requests with green CI and linear history.
Branch names: `feat/…`, `fix/…`, `docs/…`, `chore/…`, `refactor/…`.

## Changelog

Record user-visible changes in [`CHANGELOG.md`](../CHANGELOG.md). The record format
(`limen.run/1`) and the CLI are public but may still change before 1.0; breaking changes must be
called out there.
