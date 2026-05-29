//! SQLite persistence for Limen: leases and writes.
//!
//! Schema is two tables. `leases` tracks who holds what for how long.
//! `writes` is the audit log of mediated mutations. Both keyed by UUID.

use anyhow::Context;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqliteRow};
use sqlx::{Row, SqlitePool};
use std::path::Path;
use std::time::SystemTime;
use thiserror::Error;
use uuid::Uuid;

pub const DEFAULT_LEASE_TTL_MS: i64 = 5 * 60 * 1000;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Intent {
    Read,
    Write,
    Propose,
}

impl Intent {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Write => "write",
            Self::Propose => "propose",
        }
    }

    pub fn parse(s: &str) -> Result<Self, StoreError> {
        match s {
            "read" => Ok(Self::Read),
            "write" => Ok(Self::Write),
            "propose" => Ok(Self::Propose),
            other => Err(StoreError::InvalidIntent(other.to_string())),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum LeaseState {
    Active,
    Released,
    Expired,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lease {
    pub id: String,
    pub path_pattern: String,
    pub intent: Intent,
    pub agent_label: String,
    pub acquired_at: i64,
    pub expires_at: i64,
    pub released_at: Option<i64>,
    pub state: LeaseState,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WriteRecord {
    pub id: String,
    pub lease_id: String,
    pub path: String,
    pub bytes_written: i64,
    pub content_hash: String,
    pub written_at: i64,
}

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("conflict: lease {existing_id} on '{existing_pattern}' held by '{existing_agent}'")]
    Conflict {
        existing_id: String,
        existing_agent: String,
        existing_pattern: String,
    },

    #[error("lease not found: {0}")]
    LeaseNotFound(String),

    #[error("lease {id} is {state:?}, not active")]
    LeaseInactive { id: String, state: LeaseState },

    #[error("lease {id} expired at {expires_at}; now is {now}")]
    LeaseExpired {
        id: String,
        expires_at: i64,
        now: i64,
    },

    #[error("lease {id} intent is {intent:?}, not write")]
    NotAWriteLease { id: String, intent: Intent },

    #[error("path '{path}' not within lease pattern '{pattern}'")]
    PathOutOfScope { path: String, pattern: String },

    #[error("invalid intent '{0}' (expected read|write|propose)")]
    InvalidIntent(String),

    #[error("corrupt row: {0}")]
    Corrupt(String),

    #[error(transparent)]
    Db(#[from] sqlx::Error),

    #[error(transparent)]
    Io(#[from] std::io::Error),
}

pub struct Store {
    pool: SqlitePool,
}

impl Store {
    pub async fn open(db_path: &Path) -> anyhow::Result<Self> {
        if let Some(parent) = db_path.parent() {
            if !parent.as_os_str().is_empty() {
                tokio::fs::create_dir_all(parent)
                    .await
                    .with_context(|| format!("creating parent for {}", db_path.display()))?;
            }
        }
        let opts = SqliteConnectOptions::new()
            .filename(db_path)
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal);
        let pool = SqlitePool::connect_with(opts)
            .await
            .with_context(|| format!("opening db {}", db_path.display()))?;
        let store = Self { pool };
        store.init_schema().await?;
        Ok(store)
    }

    #[cfg(test)]
    pub async fn open_in_memory() -> anyhow::Result<Self> {
        let opts = SqliteConnectOptions::new()
            .filename(":memory:")
            .create_if_missing(true);
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(opts)
            .await?;
        let store = Self { pool };
        store.init_schema().await?;
        Ok(store)
    }

    async fn init_schema(&self) -> anyhow::Result<()> {
        for stmt in SCHEMA_STATEMENTS {
            sqlx::query(stmt).execute(&self.pool).await?;
        }
        Ok(())
    }

    /// Atomically check conflicts and insert a new lease.
    pub async fn acquire_lease(
        &self,
        path_pattern: &str,
        intent: Intent,
        agent_label: &str,
        ttl_ms: i64,
    ) -> Result<Lease, StoreError> {
        let now = now_millis();
        let expires_at = now + ttl_ms;

        let mut tx = self.pool.begin().await?;

        sqlx::query(
            "UPDATE leases SET state = 'expired' WHERE state = 'active' AND expires_at < ?1",
        )
        .bind(now)
        .execute(&mut *tx)
        .await?;

        let rows = sqlx::query(
            "SELECT id, path_pattern, intent, agent_label FROM leases WHERE state = 'active'",
        )
        .fetch_all(&mut *tx)
        .await?;

        for row in rows {
            let other_pattern: String = row.try_get("path_pattern")?;
            let other_intent_s: String = row.try_get("intent")?;
            if !patterns_overlap(path_pattern, &other_pattern) {
                continue;
            }
            let other_is_write = other_intent_s == "write";
            let new_is_write = intent == Intent::Write;
            // Propose never conflicts; two reads never conflict.
            if intent == Intent::Propose || (!new_is_write && !other_is_write) {
                continue;
            }
            return Err(StoreError::Conflict {
                existing_id: row.try_get("id")?,
                existing_agent: row.try_get("agent_label")?,
                existing_pattern: other_pattern,
            });
        }

        let id = Uuid::new_v4().to_string();
        sqlx::query(
            "INSERT INTO leases (id, path_pattern, intent, agent_label, acquired_at, expires_at, released_at, state) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, 'active')",
        )
        .bind(&id)
        .bind(path_pattern)
        .bind(intent.as_str())
        .bind(agent_label)
        .bind(now)
        .bind(expires_at)
        .execute(&mut *tx)
        .await?;

        tx.commit().await?;

        Ok(Lease {
            id,
            path_pattern: path_pattern.to_string(),
            intent,
            agent_label: agent_label.to_string(),
            acquired_at: now,
            expires_at,
            released_at: None,
            state: LeaseState::Active,
        })
    }

    pub async fn release_lease(&self, lease_id: &str) -> Result<bool, StoreError> {
        let now = now_millis();
        let result = sqlx::query(
            "UPDATE leases SET state = 'released', released_at = ?1 WHERE id = ?2 AND state = 'active'",
        )
        .bind(now)
        .bind(lease_id)
        .execute(&self.pool)
        .await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn get_lease(&self, lease_id: &str) -> Result<Option<Lease>, StoreError> {
        let row = sqlx::query(
            "SELECT id, path_pattern, intent, agent_label, acquired_at, expires_at, released_at, state \
             FROM leases WHERE id = ?1",
        )
        .bind(lease_id)
        .fetch_optional(&self.pool)
        .await?;
        match row {
            Some(r) => Ok(Some(row_to_lease(&r)?)),
            None => Ok(None),
        }
    }

    /// Validate that the given lease permits writing to `path`, then write the file
    /// and record the audit entry.
    pub async fn record_write(
        &self,
        lease_id: &str,
        path: &str,
        content: &[u8],
    ) -> Result<WriteRecord, StoreError> {
        let lease = self
            .get_lease(lease_id)
            .await?
            .ok_or_else(|| StoreError::LeaseNotFound(lease_id.to_string()))?;
        if lease.state != LeaseState::Active {
            return Err(StoreError::LeaseInactive {
                id: lease.id,
                state: lease.state,
            });
        }
        let now = now_millis();
        if lease.expires_at < now {
            return Err(StoreError::LeaseExpired {
                id: lease.id,
                expires_at: lease.expires_at,
                now,
            });
        }
        if lease.intent != Intent::Write {
            return Err(StoreError::NotAWriteLease {
                id: lease.id,
                intent: lease.intent,
            });
        }
        if !path_in_pattern(path, &lease.path_pattern) {
            return Err(StoreError::PathOutOfScope {
                path: path.to_string(),
                pattern: lease.path_pattern,
            });
        }

        if let Some(parent) = Path::new(path).parent() {
            if !parent.as_os_str().is_empty() {
                tokio::fs::create_dir_all(parent).await?;
            }
        }
        tokio::fs::write(path, content).await?;

        let id = Uuid::new_v4().to_string();
        let hash = hex_sha256(content);
        sqlx::query(
            "INSERT INTO writes (id, lease_id, path, bytes_written, content_hash, written_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        )
        .bind(&id)
        .bind(lease_id)
        .bind(path)
        .bind(content.len() as i64)
        .bind(&hash)
        .bind(now)
        .execute(&self.pool)
        .await?;

        Ok(WriteRecord {
            id,
            lease_id: lease_id.to_string(),
            path: path.to_string(),
            bytes_written: content.len() as i64,
            content_hash: hash,
            written_at: now,
        })
    }

    pub async fn list_active_leases(&self) -> Result<Vec<Lease>, StoreError> {
        let now = now_millis();
        let rows = sqlx::query(
            "SELECT id, path_pattern, intent, agent_label, acquired_at, expires_at, released_at, state \
             FROM leases WHERE state = 'active' AND expires_at >= ?1 ORDER BY acquired_at DESC",
        )
        .bind(now)
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(row_to_lease).collect()
    }

    pub async fn list_recent_writes(&self, limit: i64) -> Result<Vec<WriteRecord>, StoreError> {
        let rows = sqlx::query(
            "SELECT id, lease_id, path, bytes_written, content_hash, written_at \
             FROM writes ORDER BY written_at DESC LIMIT ?1",
        )
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(row_to_write).collect()
    }

    /// Returns (write_record, agent_label) for every write recorded against `path`,
    /// most recent first.
    pub async fn attribute_path(
        &self,
        path: &str,
    ) -> Result<Vec<(WriteRecord, String)>, StoreError> {
        let rows = sqlx::query(
            "SELECT w.id, w.lease_id, w.path, w.bytes_written, w.content_hash, w.written_at, l.agent_label \
             FROM writes w JOIN leases l ON w.lease_id = l.id WHERE w.path = ?1 ORDER BY w.written_at DESC",
        )
        .bind(path)
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|r| {
                let w = row_to_write(r)?;
                let label: String = r.try_get("agent_label")?;
                Ok((w, label))
            })
            .collect()
    }
}

fn row_to_lease(row: &SqliteRow) -> Result<Lease, StoreError> {
    let intent_s: String = row.try_get("intent")?;
    let state_s: String = row.try_get("state")?;
    let intent = match intent_s.as_str() {
        "read" => Intent::Read,
        "write" => Intent::Write,
        "propose" => Intent::Propose,
        other => return Err(StoreError::Corrupt(format!("unknown intent: {other}"))),
    };
    let state = match state_s.as_str() {
        "active" => LeaseState::Active,
        "released" => LeaseState::Released,
        "expired" => LeaseState::Expired,
        other => return Err(StoreError::Corrupt(format!("unknown state: {other}"))),
    };
    Ok(Lease {
        id: row.try_get("id")?,
        path_pattern: row.try_get("path_pattern")?,
        intent,
        agent_label: row.try_get("agent_label")?,
        acquired_at: row.try_get("acquired_at")?,
        expires_at: row.try_get("expires_at")?,
        released_at: row.try_get("released_at")?,
        state,
    })
}

fn row_to_write(row: &SqliteRow) -> Result<WriteRecord, StoreError> {
    Ok(WriteRecord {
        id: row.try_get("id")?,
        lease_id: row.try_get("lease_id")?,
        path: row.try_get("path")?,
        bytes_written: row.try_get("bytes_written")?,
        content_hash: row.try_get("content_hash")?,
        written_at: row.try_get("written_at")?,
    })
}

pub fn now_millis() -> i64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

fn hex_sha256(content: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content);
    format!("{:x}", hasher.finalize())
}

/// Two patterns overlap when one is a prefix of the other (treating trailing `/`
/// as a directory marker). MVP scope: literal paths or directory prefixes only — no globs.
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

/// Whether a write to `path` falls within `pattern`.
pub fn path_in_pattern(path: &str, pattern: &str) -> bool {
    if pattern.ends_with('/') {
        path.starts_with(pattern)
    } else {
        path == pattern
    }
}

const SCHEMA_STATEMENTS: &[&str] = &[
    r#"CREATE TABLE IF NOT EXISTS leases (
        id           TEXT PRIMARY KEY,
        path_pattern TEXT NOT NULL,
        intent       TEXT NOT NULL,
        agent_label  TEXT NOT NULL,
        acquired_at  INTEGER NOT NULL,
        expires_at   INTEGER NOT NULL,
        released_at  INTEGER,
        state        TEXT NOT NULL
    )"#,
    r#"CREATE INDEX IF NOT EXISTS idx_leases_active ON leases(state, path_pattern)"#,
    r#"CREATE TABLE IF NOT EXISTS writes (
        id            TEXT PRIMARY KEY,
        lease_id      TEXT NOT NULL,
        path          TEXT NOT NULL,
        bytes_written INTEGER NOT NULL,
        content_hash  TEXT NOT NULL,
        written_at    INTEGER NOT NULL,
        FOREIGN KEY(lease_id) REFERENCES leases(id)
    )"#,
    r#"CREATE INDEX IF NOT EXISTS idx_writes_path ON writes(path)"#,
];

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn acquire_and_release_write_lease() {
        let store = Store::open_in_memory().await.unwrap();
        let lease = store
            .acquire_lease("src/auth/", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        assert_eq!(lease.state, LeaseState::Active);
        assert!(store.release_lease(&lease.id).await.unwrap());
        let after = store.get_lease(&lease.id).await.unwrap().unwrap();
        assert_eq!(after.state, LeaseState::Released);
    }

    #[tokio::test]
    async fn write_write_conflict() {
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/auth/", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let err = store
            .acquire_lease(
                "src/auth/login.rs",
                Intent::Write,
                "agent-B",
                DEFAULT_LEASE_TTL_MS,
            )
            .await
            .unwrap_err();
        match err {
            StoreError::Conflict { existing_agent, .. } => assert_eq!(existing_agent, "agent-A"),
            other => panic!("expected conflict, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn read_read_no_conflict() {
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/", Intent::Read, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        store
            .acquire_lease("src/", Intent::Read, "agent-B", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn write_then_read_conflict() {
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let err = store
            .acquire_lease(
                "src/auth/login.rs",
                Intent::Read,
                "agent-B",
                DEFAULT_LEASE_TTL_MS,
            )
            .await
            .unwrap_err();
        assert!(matches!(err, StoreError::Conflict { .. }));
    }

    #[tokio::test]
    async fn propose_never_conflicts() {
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        store
            .acquire_lease("src/", Intent::Propose, "agent-B", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn write_out_of_scope_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("test.db");
        let store = Store::open(&db).await.unwrap();
        let pattern = format!("{}/scope/", tmp.path().display());
        let lease = store
            .acquire_lease(&pattern, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let bad_path = format!("{}/other/file.rs", tmp.path().display());
        let err = store
            .record_write(&lease.id, &bad_path, b"hi")
            .await
            .unwrap_err();
        assert!(matches!(err, StoreError::PathOutOfScope { .. }));
    }

    #[tokio::test]
    async fn write_in_scope_records_audit() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("test.db");
        let store = Store::open(&db).await.unwrap();
        let pattern = format!("{}/scope/", tmp.path().display());
        let lease = store
            .acquire_lease(&pattern, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let good_path = format!("{}/scope/file.rs", tmp.path().display());
        let rec = store
            .record_write(&lease.id, &good_path, b"hello")
            .await
            .unwrap();
        assert_eq!(rec.bytes_written, 5);
        let writes = store.list_recent_writes(10).await.unwrap();
        assert_eq!(writes.len(), 1);
        assert_eq!(writes[0].path, good_path);

        let attrib = store.attribute_path(&good_path).await.unwrap();
        assert_eq!(attrib.len(), 1);
        assert_eq!(attrib[0].1, "agent-A");
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
}
