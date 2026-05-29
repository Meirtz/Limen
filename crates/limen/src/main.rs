use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::sync::Arc;

mod mcp;
mod store;

use store::Store;

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
    }
}
