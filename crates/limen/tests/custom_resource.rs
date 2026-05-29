//! Integration test: the `Store` coordinates a **non-filesystem** resource through the public API
//! alone, proving the core is genuinely resource-agnostic — the filesystem is only the shipped
//! default. A third party can implement [`limen::resource::Resource`] for any namespace (here, a
//! key-value store keyed by `/`-separated prefixes) and the same acquire / write / release /
//! dependents machinery coordinates it unchanged.

use limen::resource::{hex_sha256, Applied, Resource};
use limen::store::{Intent, Store, StoreError, DEFAULT_LEASE_TTL_MS};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

/// A shared in-memory KV namespace. A region is a key prefix (trailing `/`) or an exact key.
#[derive(Clone, Default)]
struct KvResource {
    data: Arc<Mutex<BTreeMap<String, String>>>,
}

impl KvResource {
    fn get(&self, key: &str) -> Option<String> {
        self.data.lock().unwrap().get(key).cloned()
    }
    fn set(&self, key: &str, value: &str) {
        self.data.lock().unwrap().insert(key.into(), value.into());
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
            Err(StoreError::InvalidRegion(region.into()))
        } else {
            Ok(())
        }
    }
    fn apply(&self, target: &str, content: &[u8]) -> Result<Applied, StoreError> {
        let value = String::from_utf8_lossy(content).to_string();
        self.data.lock().unwrap().insert(target.into(), value);
        Ok(Applied {
            bytes: content.len() as i64,
            content_hash: hex_sha256(content),
        })
    }
}

#[tokio::test]
async fn store_coordinates_a_custom_kv_resource() {
    let kv = KvResource::default();
    let tmp = tempfile::tempdir().unwrap();
    let store = Store::open_with(&tmp.path().join("state.db"), Box::new(kv.clone()))
        .await
        .unwrap();

    let key = "config/app";
    kv.set(key, "base");

    // Two agents compose onto the same key under write leases, each reading the current value.
    for (agent, addition) in [("agent-A", "feature_a"), ("agent-B", "feature_b")] {
        let lease = store
            .acquire_lease("config/", Intent::Write, agent, DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let current = kv.get(key).unwrap_or_default();
        store
            .record_write(&lease.id, key, format!("{current}\n{addition}").as_bytes())
            .await
            .unwrap();
        store.release_lease(&lease.id).await.unwrap();
    }

    let final_value = kv.get(key).unwrap();
    assert!(
        final_value.contains("feature_a") && final_value.contains("feature_b"),
        "coordination over a KV resource should compose both contributions: {final_value:?}"
    );

    // dependents() works over the KV resource too: a read lease on the prefix depends on the key.
    store
        .acquire_lease("config/", Intent::Read, "watcher", DEFAULT_LEASE_TTL_MS)
        .await
        .unwrap();
    let deps = store.dependents(key).await.unwrap();
    assert!(
        deps.iter().any(|l| l.agent_label == "watcher"),
        "the prefix reader should be a dependent of {key}"
    );
}
