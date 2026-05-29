#![forbid(unsafe_code)]
//! Limen — the coordination core as a library.
//!
//! The daemon binary (`main.rs`) and the benchmark harness (`limen-bench`) share
//! this surface: the resource-agnostic coordination [`store`], the pluggable
//! [`resource`] backends, ed25519 [`identity`], and the [`mcp`] server.

pub mod identity;
pub mod mcp;
pub mod resource;
pub mod store;
