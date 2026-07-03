//! Golden-shape pins for the machine-readable witness export:
//! `limen audit --json` and `limen attribute <path> --json`.
//!
//! These tests seed a deterministic fixture db through the public `Store` API
//! (the same helpers the store tests use), then run the shipped binary and pin
//! the versioned envelope: the exact schema string and the exact key set of
//! every object. A smuggled extra key or a missing field breaks the pin — by
//! design, so shape changes are conscious, versioned decisions.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Command;

use limen::store::{Intent, Store, DEFAULT_LEASE_TTL_MS};
use serde_json::Value;

/// The one witnessed write every fixture db contains.
const CONTENT: &[u8] = b"pub fn login() {}\n";

/// Run the shipped binary with `args` and parse its stdout as one JSON document.
fn run_json(args: &[&str]) -> Value {
    let out = Command::new(env!("CARGO_BIN_EXE_limen"))
        .args(args)
        .output()
        .expect("run limen");
    assert!(
        out.status.success(),
        "limen {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("stdout is a single JSON document")
}

/// The exact key set of a JSON object — order-independent, nothing extra, nothing missing.
fn keys(v: &Value) -> BTreeSet<&str> {
    v.as_object()
        .expect("a JSON object")
        .keys()
        .map(String::as_str)
        .collect()
}

/// Seed one active write lease and one witnessed write; returns
/// (db path, written file path, lease id, content hash).
async fn seed(dir: &Path) -> (PathBuf, String, String, String) {
    let db = dir.join("state.db");
    let store = Store::open(&db).await.unwrap();
    let pattern = format!("{}/scope/", dir.display());
    let lease = store
        .acquire_lease(&pattern, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
        .await
        .unwrap();
    let path = format!("{}/scope/login.rs", dir.display());
    let rec = store.record_write(&lease.id, &path, CONTENT).await.unwrap();
    (db, path, lease.id, rec.content_hash)
}

#[tokio::test]
async fn audit_json_envelope_is_pinned() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, path, lease_id, hash) = seed(tmp.path()).await;
    let db = db.to_str().unwrap();

    let v = run_json(&["audit", "--db", db, "--json"]);

    assert_eq!(
        keys(&v),
        BTreeSet::from(["schema", "active_leases", "recent_writes"])
    );
    assert_eq!(v["schema"], "limen.audit/v1");

    let leases = v["active_leases"].as_array().unwrap();
    assert_eq!(leases.len(), 1);
    assert_eq!(
        keys(&leases[0]),
        BTreeSet::from(["id", "path_pattern", "intent", "agent_label", "expires_at"])
    );
    assert_eq!(leases[0]["id"], lease_id.as_str());
    assert_eq!(leases[0]["intent"], "write");
    assert_eq!(leases[0]["agent_label"], "agent-A");
    assert!(leases[0]["expires_at"].is_i64());

    let writes = v["recent_writes"].as_array().unwrap();
    assert_eq!(writes.len(), 1);
    assert_eq!(
        keys(&writes[0]),
        BTreeSet::from([
            "written_at",
            "path",
            "bytes_written",
            "content_hash",
            "lease_id"
        ])
    );
    assert_eq!(writes[0]["path"], path.as_str());
    assert_eq!(writes[0]["bytes_written"], CONTENT.len() as i64);
    assert_eq!(writes[0]["content_hash"], hash.as_str());
    assert_eq!(writes[0]["lease_id"], lease_id.as_str());
    assert!(writes[0]["written_at"].is_i64());

    // Without --json the human text renderer is unchanged.
    let out = Command::new(env!("CARGO_BIN_EXE_limen"))
        .args(["audit", "--db", db])
        .output()
        .expect("run limen audit");
    assert!(out.status.success());
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(
        text.starts_with("Active leases (1):"),
        "text output changed: {text}"
    );
}

#[tokio::test]
async fn attribute_json_envelope_is_pinned() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, path, lease_id, hash) = seed(tmp.path()).await;

    let v = run_json(&["attribute", &path, "--db", db.to_str().unwrap(), "--json"]);

    assert_eq!(keys(&v), BTreeSet::from(["schema", "path", "writes"]));
    assert_eq!(v["schema"], "limen.attribute/v1");
    assert_eq!(v["path"], path.as_str());

    let writes = v["writes"].as_array().unwrap();
    assert_eq!(writes.len(), 1);
    assert_eq!(
        keys(&writes[0]),
        BTreeSet::from([
            "written_at",
            "agent_label",
            "bytes_written",
            "content_hash",
            "lease_id"
        ])
    );
    assert_eq!(writes[0]["agent_label"], "agent-A");
    assert_eq!(writes[0]["bytes_written"], CONTENT.len() as i64);
    assert_eq!(writes[0]["content_hash"], hash.as_str());
    assert_eq!(writes[0]["lease_id"], lease_id.as_str());
    assert!(writes[0]["written_at"].is_i64());
}

#[tokio::test]
async fn attribute_json_with_no_writes_is_an_empty_array() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, _, _, _) = seed(tmp.path()).await;
    let other = format!("{}/scope/untouched.rs", tmp.path().display());

    let v = run_json(&["attribute", &other, "--db", db.to_str().unwrap(), "--json"]);

    assert_eq!(keys(&v), BTreeSet::from(["schema", "path", "writes"]));
    assert_eq!(v["schema"], "limen.attribute/v1");
    assert_eq!(v["path"], other.as_str());
    assert_eq!(v["writes"], serde_json::json!([]));
}
