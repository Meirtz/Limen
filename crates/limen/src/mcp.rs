//! Stdio JSON-RPC 2.0 MCP server.
//!
//! Reads newline-delimited JSON-RPC requests from stdin, writes responses to stdout.
//! Logging goes to stderr (configured by the binary's tracing setup).
//!
//! Implements the minimum MCP surface: `initialize`, `tools/list`, `tools/call`,
//! plus standard `notifications/initialized` (no-op). Three tools exposed:
//! `limen_acquire`, `limen_write`, `limen_release`.

use crate::store::{Intent, Store, DEFAULT_LEASE_TTL_MS};
use anyhow::Result;
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

const PROTOCOL_VERSION: &str = "2024-11-05";
const SERVER_NAME: &str = "limen";
const SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");

pub async fn run_stdio(store: Arc<Store>) -> Result<()> {
    let mut reader = BufReader::new(tokio::io::stdin());
    let mut stdout = tokio::io::stdout();
    let mut buf: Vec<u8> = Vec::new();

    loop {
        buf.clear();
        // Read raw bytes so a non-UTF-8 line becomes a per-message parse error
        // rather than terminating the whole session.
        if reader.read_until(b'\n', &mut buf).await? == 0 {
            break; // EOF
        }
        while matches!(buf.last(), Some(b'\n') | Some(b'\r')) {
            buf.pop();
        }
        if buf.is_empty() {
            continue;
        }
        let response = match std::str::from_utf8(&buf) {
            Ok(line) => {
                tracing::debug!(message = %line, "<- stdin");
                handle_message(&store, line).await
            }
            Err(_) => Some(error_response(
                Value::Null,
                -32700,
                "parse error: request was not valid UTF-8".to_string(),
            )),
        };
        if let Some(response) = response {
            let mut payload = serde_json::to_string(&response)?;
            payload.push('\n');
            tracing::debug!(message = %payload.trim(), "-> stdout");
            stdout.write_all(payload.as_bytes()).await?;
            stdout.flush().await?;
        }
    }
    Ok(())
}

/// Returns Some(response) for requests, None for notifications.
pub async fn handle_message(store: &Store, line: &str) -> Option<Value> {
    let request: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => {
            return Some(error_response(
                Value::Null,
                -32700,
                format!("parse error: {e}"),
            ))
        }
    };

    // A request must be a single JSON-RPC object. Batches and bare scalars are
    // Invalid Request; Limen does not implement batching.
    if !request.is_object() {
        return Some(error_response(
            Value::Null,
            -32600,
            "invalid request: expected a single JSON-RPC object".to_string(),
        ));
    }

    let id = request.get("id").cloned();
    let method = request.get("method").and_then(|m| m.as_str());

    // No id => notification: never reply.
    let Some(id) = id else {
        tracing::debug!(method = ?method, "notification");
        return None;
    };

    // Has an id but no usable method => Invalid Request.
    let Some(method) = method else {
        return Some(error_response(
            id,
            -32600,
            "invalid request: missing or non-string method".to_string(),
        ));
    };

    let params = request.get("params").cloned().unwrap_or(Value::Null);
    let result: Result<Value, JsonRpcError> = match method {
        "initialize" => Ok(initialize_response()),
        "tools/list" => Ok(tools_list_response()),
        "tools/call" => handle_tool_call(store, &params).await,
        "ping" => Ok(json!({})),
        other => Err(JsonRpcError::method_not_found(other)),
    };

    Some(match result {
        Ok(value) => json!({ "jsonrpc": "2.0", "id": id, "result": value }),
        Err(err) => error_response(id, err.code, err.message),
    })
}

/// Build a JSON-RPC error response object.
fn error_response(id: Value, code: i32, message: String) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
}

struct JsonRpcError {
    code: i32,
    message: String,
}

impl JsonRpcError {
    fn method_not_found(method: &str) -> Self {
        Self {
            code: -32601,
            message: format!("method not found: {method}"),
        }
    }
    fn invalid_params(message: impl Into<String>) -> Self {
        Self {
            code: -32602,
            message: message.into(),
        }
    }
}

fn initialize_response() -> Value {
    json!({
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": { "tools": {} },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        }
    })
}

fn tools_list_response() -> Value {
    json!({
        "tools": [
            {
                "name": "limen_acquire",
                "description": "Acquire a lease on a path or directory prefix for a given intent (read/write/propose). Returns lease_id to use with limen_write.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path_pattern": {
                            "type": "string",
                            "description": "Literal file path or directory prefix ending with '/' (e.g. 'src/auth/')."
                        },
                        "intent": {
                            "type": "string",
                            "enum": ["read", "write", "propose"],
                            "description": "'write' is exclusive across overlapping patterns; 'read' yields to writes; 'propose' is advisory and never conflicts."
                        },
                        "agent_label": {
                            "type": "string",
                            "description": "Identifier for the requesting agent, e.g. 'claude-code:session-abc'."
                        },
                        "ttl_ms": {
                            "type": "integer",
                            "description": "Lease lifetime in milliseconds. Defaults to 300000 (5 min)."
                        }
                    },
                    "required": ["path_pattern", "intent", "agent_label"]
                }
            },
            {
                "name": "limen_write",
                "description": "Write file content under a held lease. The path must fall within the lease's path_pattern. Records the write to the audit log.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lease_id": { "type": "string" },
                        "path": {
                            "type": "string",
                            "description": "Target file path; must fall within the lease's path_pattern."
                        },
                        "content": {
                            "type": "string",
                            "description": "File content (UTF-8)."
                        }
                    },
                    "required": ["lease_id", "path", "content"]
                }
            },
            {
                "name": "limen_release",
                "description": "Release a previously acquired lease.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lease_id": { "type": "string" }
                    },
                    "required": ["lease_id"]
                }
            },
            {
                "name": "limen_renew",
                "description": "Extend the TTL of a held lease before it expires (a keepalive). Returns the new expiry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lease_id": { "type": "string" },
                        "ttl_ms": {
                            "type": "integer",
                            "description": "New lifetime in milliseconds from now. Defaults to 300000 (5 min)."
                        }
                    },
                    "required": ["lease_id"]
                }
            }
        ]
    })
}

#[derive(Deserialize)]
struct ToolCallParams {
    name: String,
    #[serde(default)]
    arguments: Value,
}

async fn handle_tool_call(store: &Store, params: &Value) -> Result<Value, JsonRpcError> {
    let call: ToolCallParams = serde_json::from_value(params.clone())
        .map_err(|e| JsonRpcError::invalid_params(format!("invalid tool call params: {e}")))?;

    let tool_result = match call.name.as_str() {
        "limen_acquire" => tool_acquire(store, &call.arguments).await,
        "limen_write" => tool_write(store, &call.arguments).await,
        "limen_release" => tool_release(store, &call.arguments).await,
        "limen_renew" => tool_renew(store, &call.arguments).await,
        other => {
            return Err(JsonRpcError::invalid_params(format!(
                "unknown tool: {other}"
            )))
        }
    };

    Ok(match tool_result {
        Ok(structured) => json!({
            "content": [
                {
                    "type": "text",
                    "text": serde_json::to_string_pretty(&structured)
                        .unwrap_or_else(|_| structured.to_string())
                }
            ],
            "structuredContent": structured,
            "isError": false,
        }),
        Err(message) => json!({
            "content": [
                { "type": "text", "text": format!("Error: {message}") }
            ],
            "isError": true,
        }),
    })
}

#[derive(Deserialize)]
struct AcquireArgs {
    path_pattern: String,
    intent: String,
    agent_label: String,
    #[serde(default)]
    ttl_ms: Option<i64>,
}

async fn tool_acquire(store: &Store, args: &Value) -> Result<Value, String> {
    let a: AcquireArgs =
        serde_json::from_value(args.clone()).map_err(|e| format!("invalid arguments: {e}"))?;
    let intent = Intent::parse(&a.intent).map_err(|e| e.to_string())?;
    let ttl = a.ttl_ms.unwrap_or(DEFAULT_LEASE_TTL_MS);
    match store
        .acquire_lease(&a.path_pattern, intent, &a.agent_label, ttl)
        .await
    {
        Ok(lease) => Ok(json!({
            "lease_id": lease.id,
            "path_pattern": lease.path_pattern,
            "intent": lease.intent,
            "agent_label": lease.agent_label,
            "acquired_at": lease.acquired_at,
            "expires_at": lease.expires_at,
        })),
        Err(e) => Err(e.to_string()),
    }
}

#[derive(Deserialize)]
struct WriteArgs {
    lease_id: String,
    path: String,
    content: String,
}

async fn tool_write(store: &Store, args: &Value) -> Result<Value, String> {
    let a: WriteArgs =
        serde_json::from_value(args.clone()).map_err(|e| format!("invalid arguments: {e}"))?;
    match store
        .record_write(&a.lease_id, &a.path, a.content.as_bytes())
        .await
    {
        Ok(rec) => Ok(json!({
            "write_id": rec.id,
            "lease_id": rec.lease_id,
            "path": rec.path,
            "bytes_written": rec.bytes_written,
            "content_hash": rec.content_hash,
            "written_at": rec.written_at,
        })),
        Err(e) => Err(e.to_string()),
    }
}

#[derive(Deserialize)]
struct ReleaseArgs {
    lease_id: String,
}

async fn tool_release(store: &Store, args: &Value) -> Result<Value, String> {
    let a: ReleaseArgs =
        serde_json::from_value(args.clone()).map_err(|e| format!("invalid arguments: {e}"))?;
    match store.release_lease(&a.lease_id).await {
        Ok(released) => Ok(json!({ "released": released })),
        Err(e) => Err(e.to_string()),
    }
}

#[derive(Deserialize)]
struct RenewArgs {
    lease_id: String,
    #[serde(default)]
    ttl_ms: Option<i64>,
}

async fn tool_renew(store: &Store, args: &Value) -> Result<Value, String> {
    let a: RenewArgs =
        serde_json::from_value(args.clone()).map_err(|e| format!("invalid arguments: {e}"))?;
    let ttl = a.ttl_ms.unwrap_or(DEFAULT_LEASE_TTL_MS);
    match store.renew_lease(&a.lease_id, ttl).await {
        Ok(lease) => Ok(json!({
            "lease_id": lease.id,
            "expires_at": lease.expires_at,
        })),
        Err(e) => Err(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    async fn fresh() -> Store {
        Store::open_in_memory().await.unwrap()
    }

    fn req_str(value: Value) -> String {
        value.to_string()
    }

    #[tokio::test]
    async fn initialize_reports_server_info() {
        let store = fresh().await;
        let req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {}
        }));
        let resp = handle_message(&store, &req).await.unwrap();
        assert_eq!(resp["id"], 1);
        assert_eq!(resp["result"]["serverInfo"]["name"], "limen");
    }

    #[tokio::test]
    async fn tools_list_returns_all_three_tools() {
        let store = fresh().await;
        let req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }));
        let resp = handle_message(&store, &req).await.unwrap();
        let tools = resp["result"]["tools"].as_array().unwrap();
        let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
        assert!(names.contains(&"limen_acquire"));
        assert!(names.contains(&"limen_write"));
        assert!(names.contains(&"limen_release"));
        assert!(names.contains(&"limen_renew"));
    }

    #[tokio::test]
    async fn notification_returns_no_response() {
        let store = fresh().await;
        let req = req_str(json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }));
        assert!(handle_message(&store, &req).await.is_none());
    }

    #[tokio::test]
    async fn acquire_release_round_trip() {
        let store = fresh().await;
        let acquire_req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "limen_acquire",
                "arguments": {
                    "path_pattern": "src/auth/",
                    "intent": "write",
                    "agent_label": "test-agent"
                }
            }
        }));
        let resp = handle_message(&store, &acquire_req).await.unwrap();
        assert_eq!(resp["result"]["isError"], false);
        let lease_id = resp["result"]["structuredContent"]["lease_id"]
            .as_str()
            .unwrap()
            .to_string();

        let release_req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "limen_release",
                "arguments": { "lease_id": lease_id }
            }
        }));
        let release_resp = handle_message(&store, &release_req).await.unwrap();
        assert_eq!(release_resp["result"]["isError"], false);
        assert_eq!(
            release_resp["result"]["structuredContent"]["released"],
            true
        );
    }

    #[tokio::test]
    async fn renew_round_trip() {
        let store = fresh().await;
        let acquire_req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "limen_acquire",
                "arguments": {
                    "path_pattern": "src/",
                    "intent": "write",
                    "agent_label": "agent-A"
                }
            }
        }));
        let resp = handle_message(&store, &acquire_req).await.unwrap();
        let lease_id = resp["result"]["structuredContent"]["lease_id"]
            .as_str()
            .unwrap()
            .to_string();

        let renew_req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "limen_renew",
                "arguments": { "lease_id": lease_id }
            }
        }));
        let renew_resp = handle_message(&store, &renew_req).await.unwrap();
        assert_eq!(renew_resp["result"]["isError"], false);
        assert!(renew_resp["result"]["structuredContent"]["expires_at"].is_i64());
    }

    #[tokio::test]
    async fn conflicting_acquire_reports_iserror_true() {
        let store = fresh().await;
        let acq = |label: &str| {
            req_str(json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "limen_acquire",
                    "arguments": {
                        "path_pattern": "src/",
                        "intent": "write",
                        "agent_label": label
                    }
                }
            }))
        };
        let _ = handle_message(&store, &acq("first")).await.unwrap();
        let second = handle_message(&store, &acq("second")).await.unwrap();
        assert_eq!(second["result"]["isError"], true);
        let text = second["result"]["content"][0]["text"].as_str().unwrap();
        assert!(text.contains("conflict"), "got: {text}");
    }

    #[tokio::test]
    async fn unknown_method_returns_method_not_found() {
        let store = fresh().await;
        let req = req_str(json!({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "nope",
            "params": {}
        }));
        let resp = handle_message(&store, &req).await.unwrap();
        assert_eq!(resp["error"]["code"], -32601);
    }

    #[tokio::test]
    async fn malformed_json_returns_parse_error() {
        let store = fresh().await;
        let resp = handle_message(&store, "{not json").await.unwrap();
        assert_eq!(resp["error"]["code"], -32700);
    }

    #[tokio::test]
    async fn batch_request_returns_invalid_request() {
        let store = fresh().await;
        let batch = req_str(json!([{ "jsonrpc": "2.0", "id": 1, "method": "ping" }]));
        let resp = handle_message(&store, &batch).await.unwrap();
        assert_eq!(resp["error"]["code"], -32600);
    }

    #[tokio::test]
    async fn bare_scalar_returns_invalid_request() {
        let store = fresh().await;
        let resp = handle_message(&store, "42").await.unwrap();
        assert_eq!(resp["error"]["code"], -32600);
    }

    #[tokio::test]
    async fn missing_method_with_id_returns_invalid_request() {
        let store = fresh().await;
        let req = req_str(json!({ "jsonrpc": "2.0", "id": 5 }));
        let resp = handle_message(&store, &req).await.unwrap();
        assert_eq!(resp["error"]["code"], -32600);
    }
}
