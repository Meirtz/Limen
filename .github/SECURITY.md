# Security Policy

## Reporting a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting on this repository, or
contact the maintainer (`@Meirtz`) privately with the affected commit, reproduction steps and impact.

## What Limen records, and where

Run records are JSON files under `.limen/` in your project (or `$LIMEN_HOME`), together with copies
of the source files your runs executed. They can contain file paths, command lines, the values of
environment variables your code read, and parameter values. Limen hashes values that look like
secrets, but treat `.limen/` as private: it is created with its own `.gitignore`, and you should
not publish records without reading them.

## Known limitations

Limen observes a Python process; it is not a sandbox and cannot stop anything.

- **Blind spots.** It sees what the recorded Python process does through Python: files opened
  with `open`/`os.open`, directory listings, environment reads through `os.environ`, child processes
  started, and modules imported. It does not see reads made by child processes, by other processes,
  by native code that opens files itself (for example memory-mapped weights loaded by a Rust or C++
  library), or environment reads made from C.
- **Records are not tamper-evident.** Anyone who can write to `.limen/` can edit a record. Limen
  is built to catch honest mistakes, not to prove anything to an adversary.
- **Checks are heuristics with stated rules.** A clean report means none of the documented checks
  fired, not that an experiment is valid. See `docs/checks.md` for what each check does and
  does not establish.
