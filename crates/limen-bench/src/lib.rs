//! `limen-bench` — the measurement apparatus for the concurrent-agent interference study.
//!
//! Two compute-free instruments, both deterministic and testable without any LLM:
//!
//! - [`arm`] + [`task`] + [`oracle`]: run the same synthetic task under different
//!   coordination policies (Seq-1, naive-parallel, Limen) and measure outcomes with a
//!   **coordination-independent** oracle, so the *mechanism* (lost update under naive
//!   concurrency, prevented by advisory coordination) is demonstrated on ground truth.
//! - [`sim`]: a Monte-Carlo of the interference model, so the model's qualitative
//!   predictions (super-linear growth in writer count; full cooperation eliminates loss)
//!   are checked numerically before any model spend.
//!
//! Real-LLM harness adapters, the worktree/CRDT/peer arms, and the full grid are the
//! compute-bound parts and live outside this skeleton (see `docs/experiments.md`).

pub mod arm;
pub mod kv;
pub mod model;
pub mod oracle;
pub mod record;
pub mod sim;
pub mod task;
