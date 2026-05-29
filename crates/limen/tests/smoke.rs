//! End-to-end smoke test: spawn the real `limen serve` binary and drive the full
//! JSON-RPC 2.0 / MCP lifecycle over stdio. This complements the in-process unit
//! tests in `mcp.rs` by proving the shipped binary actually speaks the protocol.

use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};

use serde_json::{json, Value};

/// A line-oriented JSON-RPC client over a child's stdin/stdout.
struct Rpc {
    child: Child,
    stdin: ChildStdin,
    lines: std::io::Lines<BufReader<std::process::ChildStdout>>,
}

impl Rpc {
    fn spawn(db: &Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_limen"))
            .arg("serve")
            .arg("--db")
            .arg(db)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .env("RUST_LOG", "error")
            .spawn()
            .expect("spawn `limen serve`");
        let stdin = child.stdin.take().expect("child stdin");
        let lines = BufReader::new(child.stdout.take().expect("child stdout")).lines();
        Self {
            child,
            stdin,
            lines,
        }
    }

    /// Send one request and read exactly one response line back.
    fn call(&mut self, req: Value) -> Value {
        let mut s = req.to_string();
        s.push('\n');
        self.stdin.write_all(s.as_bytes()).expect("write request");
        self.stdin.flush().expect("flush request");
        let line = self
            .lines
            .next()
            .expect("a response line")
            .expect("read response line");
        serde_json::from_str(&line).expect("parse response json")
    }

    /// Close stdin and wait for a clean exit.
    fn shutdown(mut self) {
        drop(self.stdin);
        let status = self.child.wait().expect("wait for child");
        assert!(status.success(), "limen serve exited with {status:?}");
    }
}

/// Extract the structured payload from a successful `tools/call` result.
fn structured(resp: &Value) -> &Value {
    assert_eq!(
        resp["result"]["isError"],
        json!(false),
        "tool returned error: {resp}"
    );
    &resp["result"]["structuredContent"]
}

#[test]
fn end_to_end_mcp_lifecycle_over_stdio() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("state.db");
    let work_dir = tmp.path().join("work");
    let pattern = format!("{}/", work_dir.display());
    let file_path = work_dir.join("login.rs");
    let file_path_s = file_path.display().to_string();
    let content = "pub fn login() {}\n";

    let mut rpc = Rpc::spawn(&db);

    // 1. initialize
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}
    }));
    assert_eq!(resp["result"]["serverInfo"]["name"], "limen");

    // 2. tools/list exposes the three coordination tools
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    }));
    let names: Vec<&str> = resp["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|t| t["name"].as_str())
        .collect();
    for expected in ["limen_acquire", "limen_write", "limen_release"] {
        assert!(
            names.contains(&expected),
            "missing tool {expected}: {names:?}"
        );
    }

    // 3. agent A acquires a write lease over work/
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": { "name": "limen_acquire", "arguments": {
            "path_pattern": pattern, "intent": "write", "agent_label": "agent-A"
        }}
    }));
    let lease_id = structured(&resp)["lease_id"].as_str().unwrap().to_string();
    assert!(!lease_id.is_empty());

    // 4. agent A writes a file under the lease; it lands on disk
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": { "name": "limen_write", "arguments": {
            "lease_id": lease_id, "path": file_path_s, "content": content
        }}
    }));
    assert_eq!(structured(&resp)["bytes_written"], content.len() as i64);
    let on_disk = std::fs::read_to_string(&file_path).expect("file written to disk");
    assert_eq!(on_disk, content);

    // 5. agent B's overlapping write lease conflicts
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": { "name": "limen_acquire", "arguments": {
            "path_pattern": file_path_s, "intent": "write", "agent_label": "agent-B"
        }}
    }));
    assert_eq!(resp["result"]["isError"], json!(true));
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("conflict"), "expected conflict, got: {text}");

    // 6. agent A releases
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": { "name": "limen_release", "arguments": { "lease_id": lease_id }}
    }));
    assert_eq!(structured(&resp)["released"], json!(true));

    // 7. agent B can now acquire the released region
    let resp = rpc.call(json!({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": { "name": "limen_acquire", "arguments": {
            "path_pattern": pattern, "intent": "write", "agent_label": "agent-B"
        }}
    }));
    assert!(!structured(&resp)["lease_id"].as_str().unwrap().is_empty());

    rpc.shutdown();
}
