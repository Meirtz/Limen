# Documentation

Product and design documentation for **Limen** — a workspace coordination daemon for AI agents. Start at the root [`../README.md`](../README.md) ([中文](../README.zh.md)).

## Canonical set

English is canonical; each public spec has a `*.zh.md` Chinese companion.

| Doc | What it is |
| --- | --- |
| [`PRD.md`](PRD.md) | Product requirements (the product definition of record) |
| [`spec/philosophy.md`](spec/philosophy.md) · [zh](spec/philosophy.zh.md) | Why Limen's shape is correct — the 7 principles |
| [`spec/boundaries.md`](spec/boundaries.md) · [zh](spec/boundaries.zh.md) | What Limen is and is not; the boundary lines |
| [`spec/glossary.md`](spec/glossary.md) · [zh](spec/glossary.zh.md) | Canonical vocabulary (every term maps to a real type in `crates/limen/`) |
| [`spec/related-work.md`](spec/related-work.md) | Related work + experimental-framing scaffold for the eventual paper |
| [`references.md`](references.md) | Annotated, verified bibliography (the lineage index) |

## Source of truth

The product definition of record is [`PRD.md`](PRD.md); the design rationale is the `spec/` set above. The implementation is the single crate at [`../crates/limen/`](../crates/limen/) (a Rust MCP server). Limen is **Rust-only** at its core — it is one small daemon, not a multi-crate platform.

## Governance

Public maintenance and contribution policy:

- [`../.github/CONTRIBUTING.md`](../.github/CONTRIBUTING.md)
- [`../.github/CODE_OF_CONDUCT.md`](../.github/CODE_OF_CONDUCT.md)
- [`../.github/SECURITY.md`](../.github/SECURITY.md)
- [`../.github/SUPPORT.md`](../.github/SUPPORT.md)
- [`project/CHANGELOG.md`](project/CHANGELOG.md)

## Legacy (pending cleanup)

These are retained Crawfish-era materials from before the Limen refactor and are **not** current Limen specs. They are slated for archival or removal:

- `spec/vision.md`, `spec/architecture.md`, `spec/v0.1-plan.md` — superseded by `PRD.md` and the `spec/` set above
- `experimental/` — Crawfish experimental-surface notes (OpenClaw / A2A / federation)
- `exports/`, `archive/` — generated DOCX exports and historical PRD source

Until they are removed, treat anything using the retired Crawfish vocabulary (control plane, swarm, treaty, federation, doctrine, …; see [`spec/glossary.md`](spec/glossary.md)) as stale.
