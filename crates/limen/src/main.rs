#![forbid(unsafe_code)]

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use limen::store::{self, Lease, Store, WriteRecord};
use limen::{identity, mcp};

/// Ready-to-paste MCP server config (Claude Code `settings.json` shape).
const MCP_CONFIG_SNIPPET: &str = r#"  {
    "mcpServers": {
      "limen": {
        "command": "limen",
        "args": ["serve", "--db", ".limen/state.db"]
      }
    }
  }"#;

#[derive(Parser, Debug)]
#[command(
    name = "limen",
    version,
    about = "Workspace coordination for multi-agent AI coding harnesses"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

/// Which resource the daemon coordinates. The filesystem is the default; `redis` requires
/// building with `--features redis` and `--redis-url`.
#[derive(Clone, Copy, Debug, clap::ValueEnum)]
enum ResourceKind {
    Filesystem,
    Redis,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Run the Limen MCP server over stdio (the integration surface for Claude Code, Cursor, Codex).
    Serve {
        /// Path to the state database.
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
        /// The resource to coordinate (filesystem by default).
        #[arg(long, default_value = "filesystem")]
        resource: ResourceKind,
        /// Redis URL when `--resource redis`, e.g. `redis://127.0.0.1/` (needs `--features redis`).
        #[arg(long)]
        redis_url: Option<String>,
    },
    /// Print active leases and recent writes from the audit log.
    Audit {
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
        #[arg(long, default_value_t = 20)]
        limit: i64,
        /// Emit versioned JSON (`limen.audit/v1`) instead of human text — the same
        /// facts, machine-readable for external verifiers.
        #[arg(long)]
        json: bool,
    },
    /// Show per-agent attribution for every witnessed write to a path.
    Attribute {
        /// The file path to attribute.
        path: String,
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
        /// Emit versioned JSON (`limen.attribute/v1`) instead of human text — the same
        /// facts, machine-readable for external verifiers.
        #[arg(long)]
        json: bool,
    },
    /// Create the `.limen/` state directory and print MCP setup for your harnesses.
    Init {
        /// Workspace directory to initialize (defaults to the current directory).
        #[arg(default_value = ".")]
        dir: PathBuf,
    },
    /// Register an ed25519 identity for an agent: generate a keypair, store the
    /// public key, and write the private key to `.limen/keys/<label>.ed25519`.
    Register {
        /// Agent label to register (e.g. `claude-code:sess-A`).
        label: String,
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
    },
    /// Print the ed25519 signature a registered agent passes to `limen_acquire`.
    Sign {
        /// Agent label (must have a private key under `.limen/keys/`).
        label: String,
        /// The region (path or directory prefix) to be acquired.
        path_pattern: String,
        /// The intent: read | write | propose.
        intent: String,
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
    },
}

// --- Machine-readable witness export (`--json`) -----------------------------------------------
//
// Versioned envelopes over exactly the facts the human text output shows — the same store
// queries, a second renderer, no new data collection. The schema-versioning rule: **adding**
// a field is backward-compatible and bumps nothing (consumers must ignore unknown fields);
// **renaming, removing, or changing the meaning of** a field bumps the version (`/v1` → `/v2`).
// The shapes are pinned by `tests/json_export.rs`, so any change here is a conscious decision.

/// Schema identifier for `limen audit --json`.
const AUDIT_SCHEMA: &str = "limen.audit/v1";
/// Schema identifier for `limen attribute <path> --json`.
const ATTRIBUTE_SCHEMA: &str = "limen.attribute/v1";

/// `limen audit --json`: active leases + recent witnessed writes.
#[derive(Serialize)]
struct AuditExport<'a> {
    schema: &'static str,
    active_leases: Vec<LeaseExport<'a>>,
    recent_writes: Vec<WriteExport<'a>>,
}

/// One active lease, as the audit text shows it: id, region, intent, agent, expiry.
#[derive(Serialize)]
struct LeaseExport<'a> {
    id: &'a str,
    path_pattern: &'a str,
    intent: &'static str,
    agent_label: &'a str,
    expires_at: i64,
}

impl<'a> From<&'a Lease> for LeaseExport<'a> {
    fn from(l: &'a Lease) -> Self {
        Self {
            id: &l.id,
            path_pattern: &l.path_pattern,
            intent: l.intent.as_str(),
            agent_label: &l.agent_label,
            expires_at: l.expires_at,
        }
    }
}

/// One witnessed write, as the audit text shows it: time, target, bytes, hash, lease.
/// The hash is the full SHA-256 hex (the text truncates for display; the fact is the digest).
#[derive(Serialize)]
struct WriteExport<'a> {
    written_at: i64,
    path: &'a str,
    bytes_written: i64,
    content_hash: &'a str,
    lease_id: &'a str,
}

impl<'a> From<&'a WriteRecord> for WriteExport<'a> {
    fn from(w: &'a WriteRecord) -> Self {
        Self {
            written_at: w.written_at,
            path: &w.path,
            bytes_written: w.bytes_written,
            content_hash: &w.content_hash,
            lease_id: &w.lease_id,
        }
    }
}

/// `limen attribute <path> --json`: every witnessed write to `path`, newest first.
/// No writes is the same envelope with an empty `writes` array.
#[derive(Serialize)]
struct AttributeExport<'a> {
    schema: &'static str,
    path: &'a str,
    writes: Vec<AttributionExport<'a>>,
}

/// One attribution row, as the attribute text shows it: time, agent, bytes, hash, lease.
#[derive(Serialize)]
struct AttributionExport<'a> {
    written_at: i64,
    agent_label: &'a str,
    bytes_written: i64,
    content_hash: &'a str,
    lease_id: &'a str,
}

/// Serialize an export envelope to pretty JSON on stdout.
fn print_json<T: Serialize>(export: &T) -> Result<()> {
    println!(
        "{}",
        serde_json::to_string_pretty(export).context("serializing json export")?
    );
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();
    match cli.command {
        Command::Serve {
            db,
            resource,
            redis_url,
        } => {
            tracing::info!(db = %db.display(), ?resource, "limen serve starting on stdio");
            let store = Arc::new(open_serve_store(&db, resource, redis_url.as_deref()).await?);
            mcp::run_stdio(store).await?;
            tracing::info!("limen serve exiting");
            Ok(())
        }
        Command::Audit { db, limit, json } => {
            let store = Store::open(&db).await.context("opening store")?;
            let leases = store.list_active_leases().await?;
            if json {
                let writes = store.list_recent_writes(limit).await?;
                return print_json(&AuditExport {
                    schema: AUDIT_SCHEMA,
                    active_leases: leases.iter().map(LeaseExport::from).collect(),
                    recent_writes: writes.iter().map(WriteExport::from).collect(),
                });
            }
            println!("Active leases ({}):", leases.len());
            for l in &leases {
                println!(
                    "  {id}  pattern={pat:30}  intent={intent:7}  agent={agent}  expires_at={exp}",
                    id = l.id,
                    pat = l.path_pattern,
                    intent = l.intent.as_str(),
                    agent = l.agent_label,
                    exp = l.expires_at,
                );
            }
            let writes = store.list_recent_writes(limit).await?;
            println!("\nRecent writes (last {}):", writes.len());
            for w in &writes {
                let short_hash = &w.content_hash[..8.min(w.content_hash.len())];
                println!(
                    "  {ts}  path={path}  bytes={bytes}  hash={hash}  lease={lease}",
                    ts = w.written_at,
                    path = w.path,
                    bytes = w.bytes_written,
                    hash = short_hash,
                    lease = w.lease_id,
                );
            }
            Ok(())
        }
        Command::Attribute { path, db, json } => {
            let store = Store::open(&db).await.context("opening store")?;
            let rows = store.attribute_path(&path).await?;
            if json {
                return print_json(&AttributeExport {
                    schema: ATTRIBUTE_SCHEMA,
                    path: &path,
                    writes: rows
                        .iter()
                        .map(|(w, agent)| AttributionExport {
                            written_at: w.written_at,
                            agent_label: agent,
                            bytes_written: w.bytes_written,
                            content_hash: &w.content_hash,
                            lease_id: &w.lease_id,
                        })
                        .collect(),
                });
            }
            if rows.is_empty() {
                println!("No witnessed writes for path: {path}");
            } else {
                println!("Attribution for {path} ({} writes):", rows.len());
                for (w, agent) in &rows {
                    let short_hash = &w.content_hash[..8.min(w.content_hash.len())];
                    println!(
                        "  {ts}  agent={agent}  bytes={bytes}  hash={hash}  lease={lease}",
                        ts = w.written_at,
                        agent = agent,
                        bytes = w.bytes_written,
                        hash = short_hash,
                        lease = w.lease_id,
                    );
                }
            }
            Ok(())
        }
        Command::Init { dir } => {
            let limen_dir = dir.join(".limen");
            std::fs::create_dir_all(&limen_dir)
                .with_context(|| format!("creating {}", limen_dir.display()))?;
            println!("Initialized Limen state directory: {}", limen_dir.display());
            println!();
            println!("Next steps:");
            println!(
                "  1. Install the daemon so `limen` is on PATH:  cargo install --path crates/limen"
            );
            println!("  2. Point each MCP-speaking harness at it. Claude Code (settings.json):");
            println!();
            println!("{MCP_CONFIG_SNIPPET}");
            println!();
            println!(
                "     Cursor, Codex, and other MCP hosts use the same command in their own config format."
            );
            println!("  3. Add `.limen/` to your .gitignore so coordination state stays local.");
            println!();
            println!(
                "Your agents can then call limen_acquire / limen_write / limen_release. Inspect with `limen audit`."
            );
            Ok(())
        }
        Command::Register { label, db } => {
            let store = Store::open(&db).await.context("opening store")?;
            let (private_hex, public_hex) = identity::generate_keypair();
            store.register_agent(&label, &public_hex).await?;
            let dir = keys_dir(&db);
            std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
            let key_path = dir.join(format!("{label}.ed25519"));
            write_private_key(&key_path, &private_hex)?;
            println!("Registered agent '{label}'.");
            println!("  public key:  {public_hex}");
            println!("  private key: {}", key_path.display());
            println!();
            println!("Keep the private key safe. A registered agent must sign each acquire — use");
            println!("`limen sign {label} <region> <intent>` and pass the result as the `signature` arg.");
            Ok(())
        }
        Command::Sign {
            label,
            path_pattern,
            intent,
            db,
        } => {
            // Normalize the intent so the signed message matches the server's.
            let intent = store::Intent::parse(&intent)?.as_str();
            let key_path = keys_dir(&db).join(format!("{label}.ed25519"));
            let private_hex = std::fs::read_to_string(&key_path)
                .with_context(|| format!("reading private key {}", key_path.display()))?;
            let message = identity::acquire_message(&path_pattern, intent, &label);
            println!("{}", identity::sign(private_hex.trim(), &message)?);
            Ok(())
        }
    }
}

/// Open the store for `serve` over the selected resource. Filesystem is always available; Redis
/// requires building with `--features redis` and a `--redis-url`.
async fn open_serve_store(
    db: &Path,
    resource: ResourceKind,
    redis_url: Option<&str>,
) -> Result<Store> {
    match resource {
        ResourceKind::Filesystem => Store::open(db).await.context("opening store"),
        ResourceKind::Redis => {
            #[cfg(feature = "redis")]
            {
                let url = redis_url
                    .context("--resource redis requires --redis-url (e.g. redis://127.0.0.1/)")?;
                let res = limen::resource::RedisKvResource::connect(url)
                    .with_context(|| format!("connecting to redis at {url}"))?;
                Store::open_with(db, Box::new(res))
                    .await
                    .context("opening store over redis")
            }
            #[cfg(not(feature = "redis"))]
            {
                let _ = redis_url;
                anyhow::bail!("--resource redis requires building limen with --features redis")
            }
        }
    }
}

/// The directory holding agent private keys, derived from the state-db path
/// (`.limen/state.db` -> `.limen/keys`).
fn keys_dir(db: &Path) -> PathBuf {
    db.parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
        .join("keys")
}

/// Write a private key to disk, owner-read/write only on Unix.
fn write_private_key(path: &Path, private_hex: &str) -> Result<()> {
    std::fs::write(path, private_hex).with_context(|| format!("writing {}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
            .with_context(|| format!("setting permissions on {}", path.display()))?;
    }
    Ok(())
}
