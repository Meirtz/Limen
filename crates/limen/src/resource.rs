//! Resources: the seam where Limen's general model meets a concrete world.
//!
//! The core ([`crate::store`]) coordinates *regions of a namespace* and records a
//! witness for every mediated change; it never touches a real resource directly.
//! A [`Resource`] says how regions compare and how a change is applied. v0.1 ships
//! exactly one — the [`FilesystemResource`] — but the core is resource-agnostic, so
//! adding a key-value store, a config tree, or a cloud backend is a new `Resource`,
//! not a rewrite.

use crate::store::StoreError;
use sha2::{Digest, Sha256};
use std::path::{Component, Path};

/// The outcome of a mediated change: how many bytes landed, and their content hash.
pub struct Applied {
    pub bytes: i64,
    pub content_hash: String,
}

/// A pluggable backend that gives a namespace meaning.
///
/// Sync by design: Limen serves one request at a time over stdio, so a resource's
/// I/O is a brief, uncontended syscall rather than a concurrency bottleneck — which
/// keeps the trait object-safe and dependency-free.
pub trait Resource: Send + Sync {
    /// Whether two region descriptors overlap (and could therefore conflict).
    fn regions_overlap(&self, a: &str, b: &str) -> bool;

    /// Whether `target` lies within `region`.
    fn region_contains(&self, region: &str, target: &str) -> bool;

    /// Reject region descriptors that are malformed or unsafe for this resource.
    fn validate_region(&self, region: &str) -> Result<(), StoreError>;

    /// Apply a mediated change to `target`. The caller has already confirmed the
    /// holder's lease and that `target` is in-region; the resource is still
    /// responsible for its own safety (e.g. refusing path traversal).
    fn apply(&self, target: &str, content: &[u8]) -> Result<Applied, StoreError>;
}

/// The filesystem resource: regions are literal paths or directory prefixes (a
/// trailing `/` marks a directory), targets are file paths. No globs yet.
#[derive(Debug, Default, Clone, Copy)]
pub struct FilesystemResource;

impl Resource for FilesystemResource {
    fn regions_overlap(&self, a: &str, b: &str) -> bool {
        patterns_overlap(a, b)
    }

    fn region_contains(&self, region: &str, target: &str) -> bool {
        // A `..` component would let a lexically in-region target escape once the OS
        // resolves the path, so such a target is never considered contained.
        !has_parent_dir(target) && path_in_pattern(target, region)
    }

    fn validate_region(&self, region: &str) -> Result<(), StoreError> {
        if region.is_empty() || region == "/" || has_parent_dir(region) {
            return Err(StoreError::InvalidRegion(region.to_string()));
        }
        Ok(())
    }

    fn apply(&self, target: &str, content: &[u8]) -> Result<Applied, StoreError> {
        // Defense in depth: never write through a `..` traversal even if a caller
        // reached here without the region check.
        if has_parent_dir(target) {
            return Err(StoreError::PathOutOfScope {
                path: target.to_string(),
                pattern: "(`..` traversal refused)".to_string(),
            });
        }
        if let Some(parent) = Path::new(target).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        std::fs::write(target, content)?;
        Ok(Applied {
            bytes: content.len() as i64,
            content_hash: hex_sha256(content),
        })
    }
}

/// True if the path contains a `..` (parent-dir) component.
fn has_parent_dir(s: &str) -> bool {
    Path::new(s)
        .components()
        .any(|c| matches!(c, Component::ParentDir))
}

/// Two patterns overlap when one is a prefix of the other (a trailing `/` marks a
/// directory). Literal paths or directory prefixes only — no globs.
pub fn patterns_overlap(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    if a.ends_with('/') && b.starts_with(a) {
        return true;
    }
    if b.ends_with('/') && a.starts_with(b) {
        return true;
    }
    false
}

/// Whether `path` falls within `pattern`.
pub fn path_in_pattern(path: &str, pattern: &str) -> bool {
    if pattern.ends_with('/') {
        path.starts_with(pattern)
    } else {
        path == pattern
    }
}

/// Lowercase hex SHA-256 of `content`.
pub fn hex_sha256(content: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content);
    format!("{:x}", hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patterns_overlap_basic() {
        assert!(patterns_overlap("src/", "src/auth/login.rs"));
        assert!(patterns_overlap("src/auth/", "src/"));
        assert!(patterns_overlap("a.rs", "a.rs"));
        assert!(!patterns_overlap("a.rs", "b.rs"));
        assert!(!patterns_overlap("src/auth/", "src/other/"));
    }

    #[test]
    fn path_in_pattern_basic() {
        assert!(path_in_pattern("src/auth/login.rs", "src/auth/"));
        assert!(!path_in_pattern("src/auth/login.rs", "src/other/"));
        assert!(path_in_pattern("a.rs", "a.rs"));
        assert!(!path_in_pattern("a.rs", "b.rs"));
    }

    #[test]
    fn region_contains_refuses_parent_dir_traversal() {
        let fs = FilesystemResource;
        assert!(fs.region_contains("src/auth/", "src/auth/login.rs"));
        assert!(!fs.region_contains("src/auth/", "src/auth/../../etc/passwd"));
        assert!(!fs.region_contains("src/auth/", "src/other/x.rs"));
    }

    #[test]
    fn validate_region_rejects_degenerate_or_unsafe() {
        let fs = FilesystemResource;
        assert!(fs.validate_region("src/auth/").is_ok());
        assert!(fs.validate_region("").is_err());
        assert!(fs.validate_region("/").is_err());
        assert!(fs.validate_region("src/../etc/").is_err());
    }
}
