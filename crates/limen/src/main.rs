use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::sync::Arc;

mod mcp;
mod resource;
mod store;

use store::Store;

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

#[derive(Subcommand, Debug)]
enum Command {
    /// Run the Limen MCP server over stdio (the integration surface for Claude Code, Cursor, Codex).
    Serve {
        /// Path to the state database.
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
    },
    /// Print active leases and recent writes from the audit log.
    Audit {
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
        #[arg(long, default_value_t = 20)]
        limit: i64,
    },
    /// Show per-agent attribution for every witnessed write to a path.
    Attribute {
        /// The file path to attribute.
        path: String,
        #[arg(long, default_value = ".limen/state.db")]
        db: PathBuf,
    },
    /// Create the `.limen/` state directory and print MCP setup for your harnesses.
    Init {
        /// Workspace directory to initialize (defaults to the current directory).
        #[arg(default_value = ".")]
        dir: PathBuf,
    },
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
        Command::Serve { db } => {
            tracing::info!(db = %db.display(), "limen serve starting on stdio");
            let store = Arc::new(Store::open(&db).await.context("opening store")?);
            mcp::run_stdio(store).await?;
            tracing::info!("limen serve exiting");
            Ok(())
        }
        Command::Audit { db, limit } => {
            let store = Store::open(&db).await.context("opening store")?;
            let leases = store.list_active_leases().await?;
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
        Command::Attribute { path, db } => {
            let store = Store::open(&db).await.context("opening store")?;
            let rows = store.attribute_path(&path).await?;
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
    }
}
