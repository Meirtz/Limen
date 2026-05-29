//! A second `Resource` — an in-memory key-value namespace — proving the coordination core is
//! genuinely **resource-agnostic** (not filesystem-specific), and that the interference
//! phenomenon and Limen's prevention generalize off the filesystem. Synthetic, compute-free.
//!
//! This is the code-level half of the generality claim: the daemon's `Resource` trait is
//! implementable by a third party for a structurally different namespace (keys, not paths),
//! and the *same* `Store` (acquire / write / release / attribute) coordinates it unchanged.

use limen::resource::{hex_sha256, Applied, Resource};
use limen::store::StoreError;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

/// A shared in-memory KV namespace. Keys are "/"-separated; a region is a key prefix
/// (trailing `/`) or an exact key. The backing map is shared (Arc) so the `Store`'s copy
/// of the resource and a caller's handle see the same state.
#[derive(Clone, Default)]
pub struct KvResource {
    data: Arc<Mutex<BTreeMap<String, String>>>,
}

impl KvResource {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn get(&self, key: &str) -> Option<String> {
        self.data.lock().unwrap().get(key).cloned()
    }
    pub fn set(&self, key: &str, value: &str) {
        self.data
            .lock()
            .unwrap()
            .insert(key.to_string(), value.to_string());
    }
}

fn prefix_overlap(a: &str, b: &str) -> bool {
    a == b || (a.ends_with('/') && b.starts_with(a)) || (b.ends_with('/') && a.starts_with(b))
}

impl Resource for KvResource {
    fn regions_overlap(&self, a: &str, b: &str) -> bool {
        prefix_overlap(a, b)
    }
    fn region_contains(&self, region: &str, target: &str) -> bool {
        if region.ends_with('/') {
            target.starts_with(region)
        } else {
            target == region
        }
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
        self.data.lock().unwrap().insert(target.to_string(), value);
        Ok(Applied {
            bytes: content.len() as i64,
            content_hash: hex_sha256(content),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use limen::store::{Intent, Store, DEFAULT_LEASE_TTL_MS};

    #[tokio::test]
    async fn coordination_generalizes_to_a_kv_resource() {
        let kv = KvResource::new();
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open_with(&tmp.path().join("state.db"), Box::new(kv.clone()))
            .await
            .unwrap();

        let key = "config/app";
        kv.set(key, "base");

        // Two agents compose onto the same key, coordinated: acquire a lease on the key
        // prefix, read CURRENT, mutate, write through the witnessed mediator, release.
        for (agent, addition) in [("agent-A", "feature_a"), ("agent-B", "feature_b")] {
            let lease = store
                .acquire_lease("config/", Intent::Write, agent, DEFAULT_LEASE_TTL_MS)
                .await
                .unwrap();
            let current = kv.get(key).unwrap_or_default();
            let next = format!("{current}\n{addition}");
            store
                .record_write(&lease.id, key, next.as_bytes())
                .await
                .unwrap();
            store.release_lease(&lease.id).await.unwrap();
        }

        // Both contributions survive (no lost update), over a non-filesystem resource.
        let final_value = kv.get(key).unwrap();
        assert!(
            final_value.contains("feature_a") && final_value.contains("feature_b"),
            "coordination over KV should compose both contributions: {final_value:?}"
        );

        // Attribution from the witness trail works over the KV resource too.
        let rows = store.attribute_path(key).await.unwrap();
        assert_eq!(rows.first().map(|(_, a)| a.as_str()), Some("agent-B"));
    }

    #[test]
    fn the_hazard_exists_on_kv_too() {
        // Without coordination, two stale writes to one key lose a contribution — the same
        // lost update, off the filesystem.
        let kv = KvResource::new();
        kv.set("config/app", "base");
        let base = kv.get("config/app").unwrap();
        // Both agents read `base` (stale), compose locally, write back; last writer wins.
        let a = format!("{base}\nfeature_a");
        let b = format!("{base}\nfeature_b");
        kv.set("config/app", &a);
        kv.set("config/app", &b); // clobbers A
        let final_value = kv.get("config/app").unwrap();
        assert!(final_value.contains("feature_b"));
        assert!(
            !final_value.contains("feature_a"),
            "naive concurrent KV writes should lose a contribution"
        );
    }
}
