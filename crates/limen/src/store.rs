//! Coordination core: leases, conflict arbitration, and the witness audit.
//!
//! This layer is **resource-agnostic** — it coordinates *regions of a namespace*
//! and records a witness for every mediated change, delegating "do these regions
//! overlap / is this target in-region / apply the change" to a [`Resource`]
//! (see [`crate::resource`]). The filesystem is the one resource shipped today.
//!
//! Schema is three tables. `leases` tracks who holds which region for how long.
//! `writes` is the witnessed audit of mediated changes, **hash-chained**: each
//! witness stores a SHA-256 over its canonical facts (including the attributed
//! agent) and the previous witness's hash, and `witness_head` pins the chain tip
//! so tail truncation is detectable. Every open re-walks the chain and fails
//! closed ([`Store::verify_witness_chain`]), upgrading "witnessed = recorded" to
//! "witnessed = tamper-evident" against in-place edits and deletions. The honest
//! limit: an attacker with write access to the database file can re-chain it
//! wholesale — anchor the head hash externally to detect that.

use crate::resource::{Applied, FilesystemResource, Resource};
use anyhow::Context;
use serde::{Deserialize, Serialize};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqliteRow};
use sqlx::{Row, SqlitePool};
use std::path::Path;
use std::time::{Duration, SystemTime};
use thiserror::Error;
use uuid::Uuid;

pub const DEFAULT_LEASE_TTL_MS: i64 = 5 * 60 * 1000;

/// The anchor the first witness chains from: 64 hex zeros (no predecessor).
pub const WITNESS_CHAIN_GENESIS: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

/// Version token bound into every witness hash. Renaming, removing, or changing the
/// meaning of a hashed fact bumps this (`/v1` → `/v2`); the stored rows record which
/// message they were hashed under implicitly by their column set.
const WITNESS_MESSAGE_SCHEMA: &str = "limen.witness/v1";

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

/// The conflict matrix, in one place: two intents over overlapping regions conflict
/// unless either is `propose` (purely advisory) or both are `read`.
pub fn conflicts(a: Intent, b: Intent) -> bool {
    !matches!(
        (a, b),
        (Intent::Propose, _) | (_, Intent::Propose) | (Intent::Read, Intent::Read)
    )
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
    /// The previous witness's `witness_hash` ([`WITNESS_CHAIN_GENESIS`] for the first).
    pub prev_hash: String,
    /// SHA-256 over this witness's canonical facts (including the attributed agent
    /// label) and `prev_hash` — the link the chain is made of.
    pub witness_hash: String,
}

/// The verified state of the witness hash chain, as returned by
/// [`Store::verify_witness_chain`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChainStatus {
    /// How many witnesses the chain covers.
    pub witnesses: i64,
    /// The chain tip: the newest witness's hash ([`WITNESS_CHAIN_GENESIS`] when empty).
    pub head_hash: String,
}

/// The canonical facts a witness hash covers. `agent_label` is the attribution the
/// witness exists to preserve — it lives on the owning lease, so hashing it here makes
/// editing the lease's label as detectable as editing the witness row itself.
struct WitnessFacts<'a> {
    id: &'a str,
    lease_id: &'a str,
    agent_label: &'a str,
    path: &'a str,
    bytes_written: i64,
    content_hash: &'a str,
    written_at: i64,
}

/// SHA-256 of the canonical witness message: a fixed-field JSON object (deterministic
/// for a fixed struct — declaration order, minimal escaping) so facts containing
/// newlines or separators cannot forge frame boundaries.
fn chained_witness_hash(facts: &WitnessFacts<'_>, prev_hash: &str) -> String {
    #[derive(Serialize)]
    struct Message<'a> {
        schema: &'static str,
        id: &'a str,
        lease_id: &'a str,
        agent_label: &'a str,
        path: &'a str,
        bytes_written: i64,
        content_hash: &'a str,
        written_at: i64,
        prev_hash: &'a str,
    }
    let message = Message {
        schema: WITNESS_MESSAGE_SCHEMA,
        id: facts.id,
        lease_id: facts.lease_id,
        agent_label: facts.agent_label,
        path: facts.path,
        bytes_written: facts.bytes_written,
        content_hash: facts.content_hash,
        written_at: facts.written_at,
        prev_hash,
    };
    let bytes = serde_json::to_vec(&message).expect("witness message serializes");
    crate::resource::hex_sha256(&bytes)
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

    #[error("target '{path}' not within lease region '{pattern}'")]
    PathOutOfScope { path: String, pattern: String },

    #[error("invalid region '{0}'")]
    InvalidRegion(String),

    #[error("agent '{0}' is already registered")]
    AgentAlreadyRegistered(String),

    #[error("agent '{agent}' is registered; a valid signature is required")]
    SignatureRequired { agent: String },

    #[error("invalid signature")]
    SignatureInvalid,

    #[error("invalid key: {0}")]
    InvalidKey(String),

    #[error("invalid intent '{0}' (expected read|write|propose)")]
    InvalidIntent(String),

    #[error("corrupt row: {0}")]
    Corrupt(String),

    #[error("witness chain broken: {0}")]
    WitnessChainBroken(String),

    #[error("resource error: {0}")]
    Resource(String),

    #[error(transparent)]
    Db(#[from] sqlx::Error),

    #[error(transparent)]
    Io(#[from] std::io::Error),
}

/// The coordination store. Holds the lease/witness tables and a [`Resource`] that
/// gives the coordinated namespace meaning.
pub struct Store {
    pool: SqlitePool,
    resource: Box<dyn Resource>,
}

impl Store {
    /// Open a store backed by the default resource (the filesystem).
    pub async fn open(db_path: &Path) -> anyhow::Result<Self> {
        Self::open_with(db_path, Box::new(FilesystemResource)).await
    }

    /// Open a store over an explicit resource.
    pub async fn open_with(db_path: &Path, resource: Box<dyn Resource>) -> anyhow::Result<Self> {
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
            .busy_timeout(Duration::from_secs(5))
            .journal_mode(SqliteJournalMode::Wal);
        let pool = SqlitePool::connect_with(opts)
            .await
            .with_context(|| format!("opening db {}", db_path.display()))?;
        let store = Self { pool, resource };
        store.init_schema().await?;
        store.verify_on_open().await?;
        Ok(store)
    }

    #[cfg(test)]
    pub async fn open_in_memory() -> anyhow::Result<Self> {
        Self::open_in_memory_with(Box::new(FilesystemResource)).await
    }

    #[cfg(test)]
    pub async fn open_in_memory_with(resource: Box<dyn Resource>) -> anyhow::Result<Self> {
        let opts = SqliteConnectOptions::new()
            .filename(":memory:")
            .create_if_missing(true);
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(opts)
            .await?;
        let store = Self { pool, resource };
        store.init_schema().await?;
        store.verify_on_open().await?;
        Ok(store)
    }

    async fn init_schema(&self) -> anyhow::Result<()> {
        for stmt in SCHEMA_STATEMENTS {
            sqlx::query(stmt).execute(&self.pool).await?;
        }
        // A fresh (or pre-chain) database starts with an empty chain at genesis; a
        // database that already chained keeps its head (INSERT OR IGNORE).
        sqlx::query(
            "INSERT OR IGNORE INTO witness_head (id, head_hash, chain_len) VALUES (1, ?1, 0)",
        )
        .bind(WITNESS_CHAIN_GENESIS)
        .execute(&self.pool)
        .await?;
        self.migrate_pre_chain_writes()
            .await
            .context("migrating pre-chain witness rows")?;
        Ok(())
    }

    /// Fail closed at open: refuse to serve (and to append witnesses to) an audit
    /// trail whose chain no longer verifies.
    async fn verify_on_open(&self) -> anyhow::Result<()> {
        self.verify_witness_chain().await.context(
            "the witness trail failed verification at open; \
             inspect it with `limen verify`, or move the state db aside \
             (keeping it as evidence) to start a fresh trail",
        )?;
        Ok(())
    }

    /// One-time, transparent migration of a pre-chain `writes` table: add the chain
    /// columns and backfill the chain in insertion order. The chain attests integrity
    /// **from this migration onward** — it cannot retroactively prove the pre-chain
    /// rows were never altered before it ran.
    async fn migrate_pre_chain_writes(&self) -> Result<(), StoreError> {
        let columns = sqlx::query("PRAGMA table_info(writes)")
            .fetch_all(&self.pool)
            .await?;
        let has_chain = columns.iter().any(|row| {
            row.try_get::<String, _>("name")
                .map(|name| name == "witness_hash")
                .unwrap_or(false)
        });
        if has_chain {
            return Ok(());
        }

        let mut tx = self.pool.begin().await?;
        sqlx::query("ALTER TABLE writes ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
            .execute(&mut *tx)
            .await?;
        sqlx::query("ALTER TABLE writes ADD COLUMN witness_hash TEXT NOT NULL DEFAULT ''")
            .execute(&mut *tx)
            .await?;
        let rows = sqlx::query(
            "SELECT w.rowid AS rid, w.id, w.lease_id, w.path, w.bytes_written, w.content_hash, \
             w.written_at, l.agent_label \
             FROM writes w LEFT JOIN leases l ON w.lease_id = l.id ORDER BY w.rowid ASC",
        )
        .fetch_all(&mut *tx)
        .await?;
        let mut prev = WITNESS_CHAIN_GENESIS.to_string();
        let mut chained: i64 = 0;
        for row in &rows {
            let id: String = row.try_get("id")?;
            let agent_label: Option<String> = row.try_get("agent_label")?;
            let Some(agent_label) = agent_label else {
                return Err(StoreError::WitnessChainBroken(format!(
                    "write {id} references a missing lease; refusing to chain an \
                     unattributable witness"
                )));
            };
            let facts = WitnessFacts {
                id: &id,
                lease_id: &row.try_get::<String, _>("lease_id")?,
                agent_label: &agent_label,
                path: &row.try_get::<String, _>("path")?,
                bytes_written: row.try_get("bytes_written")?,
                content_hash: &row.try_get::<String, _>("content_hash")?,
                written_at: row.try_get("written_at")?,
            };
            let hash = chained_witness_hash(&facts, &prev);
            sqlx::query("UPDATE writes SET prev_hash = ?1, witness_hash = ?2 WHERE rowid = ?3")
                .bind(&prev)
                .bind(&hash)
                .bind(row.try_get::<i64, _>("rid")?)
                .execute(&mut *tx)
                .await?;
            prev = hash;
            chained += 1;
        }
        sqlx::query(
            "INSERT INTO witness_head (id, head_hash, chain_len) VALUES (1, ?1, ?2) \
             ON CONFLICT(id) DO UPDATE SET head_hash = ?1, chain_len = ?2",
        )
        .bind(&prev)
        .bind(chained)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        if chained > 0 {
            tracing::info!(
                witnesses = chained,
                "backfilled witness hash chain over pre-chain rows"
            );
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
        self.resource.validate_region(path_pattern)?;

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
            if !self.resource.regions_overlap(path_pattern, &other_pattern) {
                continue;
            }
            let other_intent_s: String = row.try_get("intent")?;
            let other_intent = Intent::parse(&other_intent_s)
                .map_err(|_| StoreError::Corrupt(format!("unknown intent: {other_intent_s}")))?;
            if conflicts(intent, other_intent) {
                return Err(StoreError::Conflict {
                    existing_id: row.try_get("id")?,
                    existing_agent: row.try_get("agent_label")?,
                    existing_pattern: other_pattern,
                });
            }
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

    /// Extend an active lease's TTL (a keepalive). Errors if the lease is missing,
    /// not active, or already expired — a holder must still hold a live lease to renew.
    pub async fn renew_lease(&self, lease_id: &str, ttl_ms: i64) -> Result<Lease, StoreError> {
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
        let expires_at = now + ttl_ms;
        sqlx::query("UPDATE leases SET expires_at = ?1 WHERE id = ?2 AND state = 'active'")
            .bind(expires_at)
            .bind(lease_id)
            .execute(&self.pool)
            .await?;
        Ok(Lease {
            expires_at,
            ..lease
        })
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

    /// Validate that `lease` permits writing to `target`, apply the change through
    /// the resource, then record the witness.
    pub async fn record_write(
        &self,
        lease_id: &str,
        target: &str,
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
        if !self.resource.region_contains(&lease.path_pattern, target) {
            return Err(StoreError::PathOutOfScope {
                path: target.to_string(),
                pattern: lease.path_pattern,
            });
        }

        let Applied {
            bytes,
            content_hash,
        } = self.resource.apply(target, content)?;

        // Chain the witness to the current head and advance the head, atomically:
        // a concurrent writer racing this transaction errors (SQLite serializes
        // writers) rather than forking the chain.
        let id = Uuid::new_v4().to_string();
        let facts = WitnessFacts {
            id: &id,
            lease_id,
            agent_label: &lease.agent_label,
            path: target,
            bytes_written: bytes,
            content_hash: &content_hash,
            written_at: now,
        };
        let mut tx = self.pool.begin().await?;
        let prev_hash: String = sqlx::query("SELECT head_hash FROM witness_head WHERE id = 1")
            .fetch_one(&mut *tx)
            .await?
            .try_get("head_hash")?;
        let witness_hash = chained_witness_hash(&facts, &prev_hash);
        sqlx::query(
            "INSERT INTO writes (id, lease_id, path, bytes_written, content_hash, written_at, \
             prev_hash, witness_hash) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        )
        .bind(&id)
        .bind(lease_id)
        .bind(target)
        .bind(bytes)
        .bind(&content_hash)
        .bind(now)
        .bind(&prev_hash)
        .bind(&witness_hash)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "UPDATE witness_head SET head_hash = ?1, chain_len = chain_len + 1 WHERE id = 1",
        )
        .bind(&witness_hash)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;

        Ok(WriteRecord {
            id,
            lease_id: lease_id.to_string(),
            path: target.to_string(),
            bytes_written: bytes,
            content_hash,
            written_at: now,
            prev_hash,
            witness_hash,
        })
    }

    /// Re-walk the entire witness chain from genesis: every witness's facts (including
    /// the attributed agent label on its owning lease) must recompute to its stored
    /// `witness_hash`, each link must reference the one before it, and the stored head
    /// must match the walked tip — so in-place edits, interior deletions, and tail
    /// truncation are all detected. Runs at every open (fail closed) and behind
    /// `limen verify`. O(chain length); a workspace audit db is small.
    pub async fn verify_witness_chain(&self) -> Result<ChainStatus, StoreError> {
        let rows = sqlx::query(
            "SELECT w.id, w.lease_id, w.path, w.bytes_written, w.content_hash, w.written_at, \
             w.prev_hash, w.witness_hash, l.agent_label \
             FROM writes w LEFT JOIN leases l ON w.lease_id = l.id ORDER BY w.rowid ASC",
        )
        .fetch_all(&self.pool)
        .await?;
        let mut prev = WITNESS_CHAIN_GENESIS.to_string();
        let mut walked: i64 = 0;
        for row in &rows {
            let id: String = row.try_get("id")?;
            let agent_label: Option<String> = row.try_get("agent_label")?;
            let Some(agent_label) = agent_label else {
                return Err(StoreError::WitnessChainBroken(format!(
                    "witness {id} references a missing lease"
                )));
            };
            let stored_prev: String = row.try_get("prev_hash")?;
            if stored_prev != prev {
                return Err(StoreError::WitnessChainBroken(format!(
                    "witness {id} does not link to the preceding witness \
                     (a witness may have been removed or reordered)"
                )));
            }
            let facts = WitnessFacts {
                id: &id,
                lease_id: &row.try_get::<String, _>("lease_id")?,
                agent_label: &agent_label,
                path: &row.try_get::<String, _>("path")?,
                bytes_written: row.try_get("bytes_written")?,
                content_hash: &row.try_get::<String, _>("content_hash")?,
                written_at: row.try_get("written_at")?,
            };
            let stored_hash: String = row.try_get("witness_hash")?;
            if chained_witness_hash(&facts, &prev) != stored_hash {
                return Err(StoreError::WitnessChainBroken(format!(
                    "witness {id}: recorded facts do not match its witness_hash \
                     (the row or its lease's agent_label was altered)"
                )));
            }
            prev = stored_hash;
            walked += 1;
        }
        let head = sqlx::query("SELECT head_hash, chain_len FROM witness_head WHERE id = 1")
            .fetch_optional(&self.pool)
            .await?
            .ok_or_else(|| {
                StoreError::WitnessChainBroken("the witness_head row is missing".to_string())
            })?;
        let head_hash: String = head.try_get("head_hash")?;
        let chain_len: i64 = head.try_get("chain_len")?;
        if head_hash != prev || chain_len != walked {
            return Err(StoreError::WitnessChainBroken(format!(
                "stored head does not match the walked chain (walked {walked} witnesses, \
                 head records {chain_len}); the newest witnesses may have been removed"
            )));
        }
        Ok(ChainStatus {
            witnesses: walked,
            head_hash,
        })
    }

    pub async fn list_active_leases(&self) -> Result<Vec<Lease>, StoreError> {
        let now = now_millis();
        let rows = sqlx::query(
            "SELECT id, path_pattern, intent, agent_label, acquired_at, expires_at, released_at, state \
             FROM leases WHERE state = 'active' AND expires_at >= ?1 ORDER BY acquired_at DESC, rowid DESC",
        )
        .bind(now)
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(row_to_lease).collect()
    }

    /// Active **read** leases whose region overlaps `region` — the agents that *depend on* (read)
    /// what a writer to `region` is about to change. This surfaces the write×read coupling so a
    /// coordinator can advise the dependents; it is purely informational and never blocks the
    /// write (servant, not ruler). Resource-agnostic — overlap is the [`Resource`]'s call.
    pub async fn dependents(&self, region: &str) -> Result<Vec<Lease>, StoreError> {
        let now = now_millis();
        let rows = sqlx::query(
            "SELECT id, path_pattern, intent, agent_label, acquired_at, expires_at, released_at, state \
             FROM leases WHERE state = 'active' AND expires_at >= ?1 AND intent = 'read' \
             ORDER BY acquired_at DESC, rowid DESC",
        )
        .bind(now)
        .fetch_all(&self.pool)
        .await?;
        let mut out = Vec::new();
        for row in &rows {
            let lease = row_to_lease(row)?;
            if self.resource.regions_overlap(&lease.path_pattern, region) {
                out.push(lease);
            }
        }
        Ok(out)
    }

    /// Mark active leases whose TTL has elapsed as expired, returning how many were swept. Audit
    /// hygiene: callers and the audit view then see only genuinely-held leases, without waiting
    /// for the next `acquire_lease` to lazily expire them.
    pub async fn sweep_expired(&self) -> Result<u64, StoreError> {
        let now = now_millis();
        let res = sqlx::query(
            "UPDATE leases SET state = 'expired' WHERE state = 'active' AND expires_at < ?1",
        )
        .bind(now)
        .execute(&self.pool)
        .await?;
        Ok(res.rows_affected())
    }

    pub async fn list_recent_writes(&self, limit: i64) -> Result<Vec<WriteRecord>, StoreError> {
        let rows = sqlx::query(
            "SELECT id, lease_id, path, bytes_written, content_hash, written_at, \
             prev_hash, witness_hash \
             FROM writes ORDER BY written_at DESC, rowid DESC LIMIT ?1",
        )
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(row_to_write).collect()
    }

    /// Returns (write_record, agent_label) for every change recorded against
    /// `target`, most recent first.
    pub async fn attribute_path(
        &self,
        target: &str,
    ) -> Result<Vec<(WriteRecord, String)>, StoreError> {
        let rows = sqlx::query(
            "SELECT w.id, w.lease_id, w.path, w.bytes_written, w.content_hash, w.written_at, \
             w.prev_hash, w.witness_hash, l.agent_label \
             FROM writes w JOIN leases l ON w.lease_id = l.id WHERE w.path = ?1 ORDER BY w.written_at DESC, w.rowid DESC",
        )
        .bind(target)
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

    /// Register an agent's ed25519 public key (hex). Errors if the label is already
    /// registered (rotate by removing it first) or the key is malformed.
    pub async fn register_agent(
        &self,
        label: &str,
        public_key_hex: &str,
    ) -> Result<(), StoreError> {
        crate::identity::validate_public_key(public_key_hex)?;
        if self.agent_public_key(label).await?.is_some() {
            return Err(StoreError::AgentAlreadyRegistered(label.to_string()));
        }
        sqlx::query("INSERT INTO agents (label, public_key, registered_at) VALUES (?1, ?2, ?3)")
            .bind(label)
            .bind(public_key_hex)
            .bind(now_millis())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    /// The registered public key (hex) for `label`, if any.
    pub async fn agent_public_key(&self, label: &str) -> Result<Option<String>, StoreError> {
        let row = sqlx::query("SELECT public_key FROM agents WHERE label = ?1")
            .bind(label)
            .fetch_optional(&self.pool)
            .await?;
        match row {
            Some(r) => Ok(Some(r.try_get("public_key")?)),
            None => Ok(None),
        }
    }

    /// Authorize an action by `label` over `message`: a *registered* agent must
    /// present a valid signature; an unregistered label takes the plaintext
    /// advisory path. Identity is opt-in and back-compatible.
    pub async fn authorize(
        &self,
        label: &str,
        message: &str,
        signature: Option<&str>,
    ) -> Result<(), StoreError> {
        let Some(public_key) = self.agent_public_key(label).await? else {
            return Ok(());
        };
        match signature {
            Some(sig) => crate::identity::verify(&public_key, message, sig),
            None => Err(StoreError::SignatureRequired {
                agent: label.to_string(),
            }),
        }
    }
}

fn row_to_lease(row: &SqliteRow) -> Result<Lease, StoreError> {
    let intent_s: String = row.try_get("intent")?;
    let state_s: String = row.try_get("state")?;
    let intent = Intent::parse(&intent_s)
        .map_err(|_| StoreError::Corrupt(format!("unknown intent: {intent_s}")))?;
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
        prev_hash: row.try_get("prev_hash")?,
        witness_hash: row.try_get("witness_hash")?,
    })
}

/// Milliseconds since the UNIX epoch. Clamps to 0 if the wall clock is somehow set
/// before the epoch, warning rather than silently misbehaving.
pub fn now_millis() -> i64 {
    match SystemTime::now().duration_since(SystemTime::UNIX_EPOCH) {
        Ok(d) => d.as_millis() as i64,
        Err(_) => {
            tracing::warn!("system clock is before UNIX_EPOCH; treating now as 0");
            0
        }
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
        prev_hash     TEXT NOT NULL,
        witness_hash  TEXT NOT NULL,
        FOREIGN KEY(lease_id) REFERENCES leases(id)
    )"#,
    r#"CREATE INDEX IF NOT EXISTS idx_writes_path ON writes(path)"#,
    r#"CREATE TABLE IF NOT EXISTS witness_head (
        id        INTEGER PRIMARY KEY CHECK (id = 1),
        head_hash TEXT NOT NULL,
        chain_len INTEGER NOT NULL
    )"#,
    r#"CREATE TABLE IF NOT EXISTS agents (
        label         TEXT PRIMARY KEY,
        public_key    TEXT NOT NULL,
        registered_at INTEGER NOT NULL
    )"#,
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conflict_matrix() {
        use Intent::*;
        assert!(conflicts(Write, Write));
        assert!(conflicts(Write, Read));
        assert!(conflicts(Read, Write));
        assert!(!conflicts(Read, Read));
        // propose never conflicts, as acquirer or as incumbent
        assert!(!conflicts(Propose, Write));
        assert!(!conflicts(Write, Propose));
        assert!(!conflicts(Propose, Propose));
    }

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
    async fn aliased_write_leases_conflict() {
        // Regression: differently-spelled descriptors of the same directory must
        // be recognized as the same region.
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let err = store
            .acquire_lease("./src/", Intent::Write, "agent-B", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap_err();
        assert!(matches!(err, StoreError::Conflict { .. }));
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
    async fn propose_never_conflicts_as_acquirer() {
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
    async fn propose_incumbent_does_not_block_write() {
        // Regression: an existing propose lease must not block a later write.
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/", Intent::Propose, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        store
            .acquire_lease("src/auth/", Intent::Write, "agent-B", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn renew_extends_lease_ttl() {
        let store = Store::open_in_memory().await.unwrap();
        let lease = store
            .acquire_lease("src/", Intent::Write, "agent-A", 1000)
            .await
            .unwrap();
        let renewed = store
            .renew_lease(&lease.id, DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        assert!(renewed.expires_at > lease.expires_at);
        let got = store.get_lease(&lease.id).await.unwrap().unwrap();
        assert_eq!(got.expires_at, renewed.expires_at);
    }

    #[tokio::test]
    async fn renew_released_lease_rejected() {
        let store = Store::open_in_memory().await.unwrap();
        let lease = store
            .acquire_lease("src/", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        store.release_lease(&lease.id).await.unwrap();
        let err = store
            .renew_lease(&lease.id, DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap_err();
        assert!(matches!(err, StoreError::LeaseInactive { .. }));
    }

    #[tokio::test]
    async fn invalid_region_rejected() {
        let store = Store::open_in_memory().await.unwrap();
        let err = store
            .acquire_lease("", Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap_err();
        assert!(matches!(err, StoreError::InvalidRegion(_)));
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
    async fn write_parent_dir_traversal_rejected() {
        // Regression: a `..` target lexically under the region must not escape it.
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("test.db");
        let store = Store::open(&db).await.unwrap();
        let pattern = format!("{}/scope/", tmp.path().display());
        let lease = store
            .acquire_lease(&pattern, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let escape = format!("{}/scope/../escaped.txt", tmp.path().display());
        let err = store
            .record_write(&lease.id, &escape, b"nope")
            .await
            .unwrap_err();
        assert!(matches!(err, StoreError::PathOutOfScope { .. }));
        assert!(
            !tmp.path().join("escaped.txt").exists(),
            "the escaping write must not have landed on disk"
        );
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

    #[tokio::test]
    async fn attribution_order_is_deterministic_under_same_ms_writes() {
        // Many writes to one path land within the same millisecond; attribution must still be a
        // deterministic total order (newest insertion first), never a timestamp-tie coin flip.
        // Random write ids cannot order ties, so the store falls back to insertion order (rowid).
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(&tmp.path().join("test.db")).await.unwrap();
        let pattern = format!("{}/scope/", tmp.path().display());
        let lease = store
            .acquire_lease(&pattern, Intent::Write, "agent-A", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        let path = format!("{}/scope/k.txt", tmp.path().display());
        for i in 0..8u8 {
            store
                .record_write(&lease.id, &path, format!("v{i}").as_bytes())
                .await
                .unwrap();
        }
        let rows = store.attribute_path(&path).await.unwrap();
        let got: Vec<String> = rows.iter().map(|(w, _)| w.content_hash.clone()).collect();
        let mut expected: Vec<String> = (0..8u8)
            .map(|i| crate::resource::hex_sha256(format!("v{i}").as_bytes()))
            .collect();
        expected.reverse(); // newest-insertion-first
        assert_eq!(
            got, expected,
            "attribution must be a deterministic total order"
        );
    }

    #[tokio::test]
    async fn dependents_lists_overlapping_readers() {
        let store = Store::open_in_memory().await.unwrap();
        store
            .acquire_lease("src/", Intent::Read, "reader", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        store
            .acquire_lease("docs/", Intent::Read, "elsewhere", DEFAULT_LEASE_TTL_MS)
            .await
            .unwrap();
        // A writer about to change src/main.rs: only the reader of src/ depends on it.
        let deps = store.dependents("src/main.rs").await.unwrap();
        assert_eq!(deps.len(), 1, "exactly the overlapping reader");
        assert_eq!(deps[0].agent_label, "reader");
        // A region nobody reads has no dependents.
        assert!(store.dependents("other/x").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn sweep_expired_marks_elapsed_leases() {
        let store = Store::open_in_memory().await.unwrap();
        // A lease whose TTL has already elapsed (negative TTL): recorded active, but expired.
        store
            .acquire_lease("src/", Intent::Write, "a", -1000)
            .await
            .unwrap();
        assert_eq!(store.sweep_expired().await.unwrap(), 1, "should sweep one");
        assert_eq!(store.sweep_expired().await.unwrap(), 0, "idempotent");
        assert!(
            store.list_active_leases().await.unwrap().is_empty(),
            "swept lease must not show as active"
        );
    }

    #[tokio::test]
    async fn register_and_authorize() {
        let store = Store::open_in_memory().await.unwrap();
        let (sk, pk) = crate::identity::generate_keypair();
        store.register_agent("a", &pk).await.unwrap();
        // duplicate registration is rejected
        assert!(matches!(
            store.register_agent("a", &pk).await.unwrap_err(),
            StoreError::AgentAlreadyRegistered(_)
        ));
        // unregistered label: plaintext path, no signature needed
        store.authorize("other", "msg", None).await.unwrap();
        // registered label: signature required
        assert!(matches!(
            store.authorize("a", "msg", None).await.unwrap_err(),
            StoreError::SignatureRequired { .. }
        ));
        // registered label: a valid signature is accepted, a bad one rejected
        let sig = crate::identity::sign(&sk, "msg").unwrap();
        store.authorize("a", "msg", Some(&sig)).await.unwrap();
        assert!(matches!(
            store.authorize("a", "msg", Some("00")).await.unwrap_err(),
            StoreError::SignatureInvalid
        ));
    }
}
