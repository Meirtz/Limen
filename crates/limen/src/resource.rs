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
        // Normalize first so differently-spelled descriptors of the same directory
        // (e.g. `src/` vs `./src/`) are compared as one region. A `..` makes a
        // descriptor unnormalizable, so it never overlaps anything.
        match (normalize(a), normalize(b)) {
            (Some(a), Some(b)) => patterns_overlap(&a, &b),
            _ => false,
        }
    }

    fn region_contains(&self, region: &str, target: &str) -> bool {
        // `..` is unnormalizable, so a traversal target is never contained — it
        // could otherwise escape the region once the OS resolves the path.
        match (normalize(region), normalize(target)) {
            (Some(region), Some(target)) => path_in_pattern(&target, &region),
            _ => false,
        }
    }

    fn validate_region(&self, region: &str) -> Result<(), StoreError> {
        match normalize(region) {
            Some(n) if !n.is_empty() && n != "/" => Ok(()),
            _ => Err(StoreError::InvalidRegion(region.to_string())),
        }
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

/// Lexically normalize a filesystem descriptor so differently-spelled descriptors
/// of the same path compare equal: drop `.` and empty components (collapsing `./`
/// and `//`), preserve a leading `/` and the trailing-`/` directory marker, and
/// reject any `..` component as unnormalizable (returning `None`). Purely lexical —
/// it never touches the disk, so it does not resolve symlinks.
fn normalize(s: &str) -> Option<String> {
    let is_abs = s.starts_with('/');
    let is_dir = s.ends_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for comp in s.split('/') {
        match comp {
            "" | "." => continue,
            ".." => return None,
            other => parts.push(other),
        }
    }
    let mut out = parts.join("/");
    if is_abs {
        out.insert(0, '/');
    }
    if is_dir && !out.is_empty() && !out.ends_with('/') {
        out.push('/');
    }
    Some(out)
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

#[cfg(feature = "redis")]
pub use redis_kv::RedisKvResource;

/// A Redis-backed key-value [`Resource`] (enable with `--features redis`).
///
/// Regions are key prefixes (a trailing `/` marks a prefix) or exact keys — the same prefix
/// semantics as the filesystem, via [`patterns_overlap`] / [`path_in_pattern`]. A mediated change
/// `SET`s the key. This lets Limen coordinate concurrent agents over a shared Redis namespace —
/// e.g. a shared agent-memory / scratchpad store — with the same leases and witness as files.
/// Reads (`get`) are the agent's own concern; writes flow through Limen.
///
/// Uses the synchronous client, consistent with the trait's "one request at a time" design.
#[cfg(feature = "redis")]
mod redis_kv {
    use super::{hex_sha256, path_in_pattern, patterns_overlap, Applied, Resource};
    use crate::store::StoreError;

    pub struct RedisKvResource {
        client: redis::Client,
    }

    impl RedisKvResource {
        /// Connect to Redis at `url`, e.g. `redis://127.0.0.1/`.
        pub fn connect(url: &str) -> Result<Self, StoreError> {
            let client = redis::Client::open(url)
                .map_err(|e| StoreError::Resource(format!("redis open {url}: {e}")))?;
            Ok(Self { client })
        }

        fn conn(&self) -> Result<redis::Connection, StoreError> {
            self.client
                .get_connection()
                .map_err(|e| StoreError::Resource(format!("redis connect: {e}")))
        }

        /// Current value at `key`, if any. Agents read before writing through Limen.
        pub fn get(&self, key: &str) -> Result<Option<String>, StoreError> {
            let mut con = self.conn()?;
            redis::cmd("GET")
                .arg(key)
                .query::<Option<String>>(&mut con)
                .map_err(|e| StoreError::Resource(format!("redis get {key}: {e}")))
        }
    }

    impl Resource for RedisKvResource {
        fn regions_overlap(&self, a: &str, b: &str) -> bool {
            patterns_overlap(a, b)
        }
        fn region_contains(&self, region: &str, target: &str) -> bool {
            path_in_pattern(target, region)
        }
        fn validate_region(&self, region: &str) -> Result<(), StoreError> {
            if region.is_empty() {
                Err(StoreError::InvalidRegion(region.to_string()))
            } else {
                Ok(())
            }
        }
        fn apply(&self, target: &str, content: &[u8]) -> Result<Applied, StoreError> {
            let value = String::from_utf8_lossy(content).to_string();
            let mut con = self.conn()?;
            redis::cmd("SET")
                .arg(target)
                .arg(&value)
                .query::<()>(&mut con)
                .map_err(|e| StoreError::Resource(format!("redis set {target}: {e}")))?;
            Ok(Applied {
                bytes: content.len() as i64,
                content_hash: hex_sha256(content),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Live check: needs a running Redis. Run with:
    //   REDIS_URL=redis://127.0.0.1/ cargo test -p limen --features redis -- --ignored redis
    #[cfg(feature = "redis")]
    #[tokio::test]
    #[ignore = "live Redis: set REDIS_URL (e.g. redis://127.0.0.1/)"]
    async fn store_coordinates_a_redis_kv_resource() {
        use crate::store::{Intent, Store, DEFAULT_LEASE_TTL_MS};
        let url = std::env::var("REDIS_URL").expect("set REDIS_URL");
        let reader = RedisKvResource::connect(&url).unwrap();
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open_with(
            &tmp.path().join("state.db"),
            Box::new(RedisKvResource::connect(&url).unwrap()),
        )
        .await
        .unwrap();

        let key = "limen:test:config/app";
        // Two agents compose onto one Redis key under write leases, each reading current first.
        for (agent, addition) in [("agent-A", "feature_a"), ("agent-B", "feature_b")] {
            let lease = store
                .acquire_lease(
                    "limen:test:config/",
                    Intent::Write,
                    agent,
                    DEFAULT_LEASE_TTL_MS,
                )
                .await
                .unwrap();
            let current = reader.get(key).unwrap().unwrap_or_default();
            let next = if current.is_empty() {
                addition.to_string()
            } else {
                format!("{current}\n{addition}")
            };
            store
                .record_write(&lease.id, key, next.as_bytes())
                .await
                .unwrap();
            store.release_lease(&lease.id).await.unwrap();
        }

        let final_value = reader.get(key).unwrap().unwrap();
        assert!(
            final_value.contains("feature_a") && final_value.contains("feature_b"),
            "coordination over Redis should compose both contributions: {final_value:?}"
        );
    }

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
    fn aliased_descriptors_overlap_and_contain() {
        let fs = FilesystemResource;
        assert!(fs.regions_overlap("src/", "./src/"));
        assert!(fs.regions_overlap("src/", "src//auth/"));
        assert!(fs.region_contains("./src/", "src/auth/login.rs"));
        assert!(fs.region_contains("src/", "./src/auth/login.rs"));
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
