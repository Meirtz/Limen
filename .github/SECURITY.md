# Security Policy

## Supported State

Limen is currently an alpha project. Security fixes will be prioritized for the latest code on `main` and the latest unreleased work that is expected to merge soon.

There is no backward-compatibility or long-term support guarantee yet.

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability.

Preferred path:

1. Use GitHub private vulnerability reporting or a security advisory if available.
2. If that is not available, contact the maintainer privately and include:
   - affected commit or branch
   - reproduction steps
   - impact
   - any suggested mitigation

Project maintainer: `@Meirtz`

## Response Expectations

- Initial triage target: within a reasonable best-effort window
- Fixes may land on a private branch first and be released publicly afterward
- Public disclosure should wait until the maintainer confirms a fix or mitigation path

## Known Limitations (alpha)

Limen is an **advisory** coordination layer, not a sandbox. Know the boundaries before relying on it:

- **Advisory, not enforced.** A lease conflicts only with other lease attempts. An agent that bypasses Limen and writes directly is not stopped; the witness trail attributes mediated writes, but cannot see writes that never went through `limen_write`. Mandatory enforcement is future work.
- **Filesystem-resource hardening is partial.** Mediated writes refuse `..` path traversal, so a lease holder cannot lexically escape its region. Two gaps remain, tracked for a later release:
  - **Symlinked directories** inside a region can still redirect a mediated write outside it (the witness would then record the in-region target, not the resolved destination). Kernel-enforced containment (`openat2` `RESOLVE_BENEATH` / `RESOLVE_NO_SYMLINKS`) is planned.
  - **Region aliasing**: differently-spelled descriptors of the same directory (`src/` vs `./src/` vs an absolute path) are not yet normalized, so two such leases may both be granted. Pass consistent, normalized paths until region canonicalization lands.
- **Single machine, single namespace.** No multi-machine or multi-tenant isolation.
- **Asserted identity.** Agent identity is a plaintext label, not cryptographically verified (ed25519 planned).

These are deliberate alpha scope boundaries, documented so you can judge whether Limen's guarantees match your threat model.
