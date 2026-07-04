//! Tamper evidence for the witness trail: every witness is hash-chained to the one
//! before it, the head is stored, and every `Store` open re-walks the chain and
//! fails closed. These tests forge the audit through raw SQL — exactly what the
//! chain exists to detect — and assert that reopen and `limen verify` reject it.

use std::path::{Path, PathBuf};
use std::process::Command;

use limen::store::{Intent, Store, DEFAULT_LEASE_TTL_MS, WITNESS_CHAIN_GENESIS};

/// A raw SQLite connection to the state db, bypassing the store — the attacker's view.
async fn raw_db(db: &Path) -> sqlx::SqlitePool {
    let opts = sqlx::sqlite::SqliteConnectOptions::new().filename(db);
    sqlx::SqlitePool::connect_with(opts)
        .await
        .expect("raw sqlite connection")
}

/// Seed a store with one write lease and three witnessed writes; returns the db path
/// and the witness ids in insertion order. The store is dropped before returning.
async fn seeded(dir: &Path) -> (PathBuf, Vec<String>) {
    let db = dir.join("state.db");
    let store = Store::open(&db).await.expect("open fresh store");
    let region = format!("{}/scope/", dir.display());
    let lease = store
        .acquire_lease(&region, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
        .await
        .expect("acquire write lease");
    let mut ids = Vec::new();
    for i in 0..3u8 {
        let path = format!("{}/scope/f{i}.txt", dir.display());
        let rec = store
            .record_write(&lease.id, &path, format!("v{i}").as_bytes())
            .await
            .expect("witnessed write");
        ids.push(rec.id);
    }
    (db, ids)
}

/// Assert that opening the store at `db` fails with a broken witness chain.
async fn assert_open_rejects(db: &Path) {
    let err = match Store::open(db).await {
        Err(e) => format!("{e:#}"),
        Ok(_) => panic!("open must fail closed on a tampered witness trail"),
    };
    assert!(
        err.contains("witness chain broken"),
        "expected a witness-chain error, got: {err}"
    );
}

#[tokio::test]
async fn writes_are_chained_and_verify_reports_the_chain() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, _ids) = seeded(tmp.path()).await;

    // Reopen (which itself verifies) and check the chain shape explicitly.
    let store = Store::open(&db).await.expect("reopen verified store");
    let status = store.verify_witness_chain().await.expect("chain intact");
    assert_eq!(status.witnesses, 3);
    assert_eq!(status.head_hash.len(), 64, "head is a sha-256 hex digest");

    // list_recent_writes is newest-first: the oldest witness anchors to genesis,
    // each later witness links to the hash of the one before it.
    let writes = store.list_recent_writes(10).await.unwrap();
    assert_eq!(writes.len(), 3);
    let oldest = &writes[2];
    assert_eq!(oldest.prev_hash, WITNESS_CHAIN_GENESIS);
    assert_eq!(writes[1].prev_hash, oldest.witness_hash);
    assert_eq!(writes[0].prev_hash, writes[1].witness_hash);
    assert_eq!(writes[0].witness_hash, status.head_hash);
}

#[tokio::test]
async fn in_place_edit_of_a_witness_is_rejected_at_open() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, ids) = seeded(tmp.path()).await;

    // Forge the target path of the middle witness through raw SQL.
    let pool = raw_db(&db).await;
    sqlx::query("UPDATE writes SET path = '/forged/elsewhere.txt' WHERE id = ?1")
        .bind(&ids[1])
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    assert_open_rejects(&db).await;
}

#[tokio::test]
async fn forged_attribution_is_rejected_at_open() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, _ids) = seeded(tmp.path()).await;

    // Rewrite who did it: the chain covers the attributed agent label, so editing
    // the lease's label falsifies every witness recorded under it.
    let pool = raw_db(&db).await;
    sqlx::query("UPDATE leases SET agent_label = 'mallory'")
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    assert_open_rejects(&db).await;
}

#[tokio::test]
async fn deleting_the_newest_witness_is_rejected_at_open() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, ids) = seeded(tmp.path()).await;

    // Truncating the tail leaves a self-consistent chain; the stored head detects it.
    let pool = raw_db(&db).await;
    sqlx::query("DELETE FROM writes WHERE id = ?1")
        .bind(&ids[2])
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    assert_open_rejects(&db).await;
}

#[tokio::test]
async fn deleting_a_middle_witness_is_rejected_at_open() {
    let tmp = tempfile::tempdir().unwrap();
    let (db, ids) = seeded(tmp.path()).await;

    let pool = raw_db(&db).await;
    sqlx::query("DELETE FROM writes WHERE id = ?1")
        .bind(&ids[1])
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    assert_open_rejects(&db).await;
}

#[tokio::test]
async fn pre_chain_db_migrates_and_new_writes_extend_the_chain() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("state.db");

    // Build a database with the pre-chain schema by hand: `writes` without the
    // chain columns and no witness_head table — what an existing alpha db looks like.
    let opts = sqlx::sqlite::SqliteConnectOptions::new()
        .filename(&db)
        .create_if_missing(true);
    let pool = sqlx::SqlitePool::connect_with(opts).await.unwrap();
    for stmt in [
        "CREATE TABLE leases (
            id TEXT PRIMARY KEY, path_pattern TEXT NOT NULL, intent TEXT NOT NULL,
            agent_label TEXT NOT NULL, acquired_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, released_at INTEGER, state TEXT NOT NULL
        )",
        "CREATE TABLE writes (
            id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, path TEXT NOT NULL,
            bytes_written INTEGER NOT NULL, content_hash TEXT NOT NULL,
            written_at INTEGER NOT NULL, FOREIGN KEY(lease_id) REFERENCES leases(id)
        )",
        "INSERT INTO leases VALUES
            ('L1', 'legacy/', 'write', 'legacy-agent', 1, 2, 2, 'released')",
        "INSERT INTO writes VALUES
            ('W1', 'L1', 'legacy/a.txt', 2, 'aa', 1),
            ('W2', 'L1', 'legacy/b.txt', 2, 'bb', 2)",
    ] {
        sqlx::query(stmt).execute(&pool).await.unwrap();
    }
    pool.close().await;

    // Opening migrates: legacy witnesses are backfilled into the chain.
    let store = Store::open(&db)
        .await
        .expect("legacy db opens and migrates");
    let status = store.verify_witness_chain().await.expect("chain intact");
    assert_eq!(status.witnesses, 2, "both legacy witnesses are chained");

    // New writes extend the migrated chain.
    let region = format!("{}/scope/", tmp.path().display());
    let lease = store
        .acquire_lease(&region, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
        .await
        .unwrap();
    let path = format!("{}/scope/new.txt", tmp.path().display());
    store.record_write(&lease.id, &path, b"new").await.unwrap();
    drop(store);

    let store = Store::open(&db).await.expect("reopen after extending");
    let status = store.verify_witness_chain().await.unwrap();
    assert_eq!(status.witnesses, 3);

    // Tampering with a migrated legacy row is now detected too.
    drop(store);
    let pool = raw_db(&db).await;
    sqlx::query("UPDATE writes SET content_hash = 'cc' WHERE id = 'W1'")
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;
    assert_open_rejects(&db).await;
}

#[test]
fn verify_cli_reports_intact_and_rejects_tampering() {
    let tmp = tempfile::tempdir().unwrap();
    let rt = tokio::runtime::Runtime::new().unwrap();
    let (db, ids) = rt.block_on(seeded(tmp.path()));

    let run = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_limen"))
            .args(args)
            .output()
            .expect("run limen")
    };
    let db_s = db.display().to_string();

    // Intact: `limen verify` succeeds and reports the chain; `limen audit` shows it too.
    let out = run(&["verify", "--db", &db_s]);
    assert!(out.status.success(), "verify on intact db must succeed");
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("intact") && stdout.contains("3 witnesses"),
        "got: {stdout}"
    );
    let out = run(&["audit", "--db", &db_s]);
    assert!(out.status.success());
    assert!(
        String::from_utf8_lossy(&out.stdout).contains("Witness chain: intact"),
        "audit must surface chain validity"
    );

    // Tampered: verify and audit both refuse.
    rt.block_on(async {
        let pool = raw_db(&db).await;
        sqlx::query("UPDATE writes SET bytes_written = 9999 WHERE id = ?1")
            .bind(&ids[0])
            .execute(&pool)
            .await
            .unwrap();
        pool.close().await;
    });
    for cmd in ["verify", "audit"] {
        let out = run(&[cmd, "--db", &db_s]);
        assert!(!out.status.success(), "{cmd} must fail on a tampered db");
        assert!(
            String::from_utf8_lossy(&out.stderr).contains("witness chain broken"),
            "{cmd} must say why"
        );
    }
}
