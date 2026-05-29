# Limen — 产品需求文档 (PRD)

| | |
|---|---|
| **状态** | Draft v0.1 · 立项 |
| **版本线** | `limen 0.1.0-alpha` |
| **前身** | `AgentGraph` → `Crawfish` → **`Limen`**（第三次定位，从"control plane / 治理平台"转向"协调协议工具"） |
| **本文取代** | `docs/spec/vision.md`、`docs/spec/v0.1-plan.md` 的产品定义部分 |
| **日期** | 2026-05-29 |

> **一句话**：当 Claude Code、Cursor、Codex 或多个 sub-agent 共享同一个 repo 时，Limen 给每个 agent 发一张**边界内的写入租约**和一条**可追溯的审计记录**——让它们不互相覆盖。它不运行你的 agent，只防止它们撞车。

---

## 1. 问题

### 1.1 具体场景

今天一个开发者的同一份 repo 上，可能同时有：

- 一个 **Claude Code** session 在写 `src/auth/`
- 一个 **Cursor** 实例打开着、buffer 里是旧版本
- 一个 **Codex** 后台任务在跑测试
- Claude Code 自己 spawn 的 **2–3 个 sub-agent** 在并行改不同文件

**没有任何一层在协调它们。** 后果是具体且可复现的：

| 失败模式 | 后果 |
|---|---|
| 两个 agent 同时写同一个文件 | 后写覆盖先写，先写的工作**静默丢失** |
| agent A 改了函数签名，agent B 还在用旧签名调用 | **build break** / 测试挂 |
| Cursor 的 buffer 基于旧版本保存 | 覆盖掉另一个 agent 刚提交的修改 |
| 后台 agent 删了文件，前台还在编辑 | 编辑器写回一个"已删除"的幽灵文件 |
| 出了 bug，`git blame` 全是 user | **无法归因**到是哪个 agent / 哪个 prompt 造成的 |

### 1.2 为什么是现在

- **harness 爆发**：Claude Code、Cursor、Codex、Gemini CLI、Copilot CLI、aider、cline……一个开发者同时用 2–3 个已是常态。
- **sub-agent 普及**：主流 harness 都已支持并行 spawn sub-agent（Claude Code 的 `Agent` tool 就是）。一个 harness 内部就已经是 multi-writer。
- **没人占这一层**：harness 厂商在各自卷 IDE 体验；Anthropic 在写 [Zero Trust for AI Agents](https://claude.com/blog/zero-trust-for-ai-agents) 的范式倡议。**"让它们在同一个 workspace 和平共处"这一层是空的。**

### 1.3 这不是什么问题

- 不是"如何编排 agent 的工作流"（那是 harness 自己的事）。
- 不是"如何让 agent 更聪明"（那是模型的事）。
- 不是"如何治理 agent swarm 的宏大命题"。**它是一个 workspace 级别的并发控制 + 身份 + 审计问题**——操作系统/数据库层级的具体工程问题。

---

## 2. 定位

### 2.1 一句话定位

> **Limen is a workspace coordination daemon.** When multiple AI coding agents share a repo, Limen gives each one a signed identity, a boundary-scoped lease, and a witnessed audit trail. It does not run your agent — it just keeps them from stepping on each other.

> **范畴 vs 滩头（重要）**：通用**范畴**是「协调并发自治 agent 在*共享可变状态*上的行为」，凭 lease + witness + identity 三原语——写者不限于 coding agent，状态不限于文件系统。"多 AI coding agent 共享一个 repo" 是**首个滩头/验证场景**，不是定义。对外文案（README/标题）须先讲通用范畴，再落到滩头。详见 [`spec/boundaries.md`](spec/boundaries.md) 的"通用化轴"。

### 2.2 姿态：仆从，不是统治者

能被广泛采用的同类基础设施都是**仆从姿态**——它们从不声称"统治"任何东西：

| 项目 | 它说自己是 | 它不说自己是 |
|---|---|---|
| Git | "我追踪差异" | "我管理你的代码" |
| MCP | "我规定工具调用的 wire format" | "我控制你的 agent" |
| OpenTelemetry | "我规定 span 格式" | "我监督你的系统" |
| OAuth | "我规定授权握手" | "我决定谁能访问什么" |
| **Limen** | **"我发租约、记审计"** | **"我治理 agent swarm"** |

> Crawfish 的致命错误是统治者语气（"control plane for governed swarms"、"the law layer"）。**Limen 显式抛弃这一点。**

### 2.3 与 Anthropic Zero Trust 的关系

Anthropic 那篇 blog 提出了 agent 安全的四个原语。Limen 是其中**可协调部分**的一个**已经能跑的开源实现**：

| Zero Trust 原语 | Limen 对应 | MVP 状态 |
|---|---|---|
| Cryptographically rooted **identity** | 每个 agent 的签名身份 | 📋 v0.2（MVP 先用明文 label） |
| Per-task **permission scoping** | boundary-scoped lease | ✅ 已实现 |
| **Audit** / "assume breach" 取证 | witnessed write log + attribution | ✅ 已实现 |
| Agentic **SOAR**（实时响应） | 冲突即时仲裁 | 🚧 部分（先到先得 + TTL） |

这是 Limen 唯一保留的外部锚点。其余所有 cite 一律删除。

---

## 3. 目标用户

| 用户 | 痛点 | Limen 提供 |
|---|---|---|
| **重度 AI coding 开发者** | 同时开 2–3 个 harness，经常踩到彼此 | 一条命令接入，写入不再互相覆盖 |
| **harness / IDE 开发团队** | 想让自己的产品和别家共存，但不想自建协调层 | 一个中立、协议化的接入点（MCP） |
| **agent 框架 / sub-agent 编排者** | 并行 sub-agent 的写入冲突要自己处理 | 现成的 lease + audit 原语 |
| **安全 / 平台团队**（后续） | agent 改了什么、谁改的，无法回答 | 可追溯的 attribution |

**首要用户是第一类**——重度 AI coding 开发者。adoption 从这里开始。

---

## 4. 北极星场景

**多 harness 共享同一个 repo。** 一个完整 walkthrough：

```
开发者在 ~/myproject 同时跑：
  · Claude Code   (label: claude-code:sess-A)
  · Codex CLI     (label: codex:sess-B)

两者都配置了 limen 作为 MCP server（5 行配置，见 §9）。

1. Claude Code 要重构 src/auth/，调 limen_acquire("src/auth/", "write", "claude-code:sess-A")
   → 拿到 lease L1，TTL 5 分钟

2. Codex 也想动 src/auth/login.rs，调 limen_acquire("src/auth/login.rs", "write", "codex:sess-B")
   → Limen 检测到 L1 覆盖此路径 → 返回 conflict，告知"src/auth/ 正被 claude-code:sess-A 持有"
   → Codex 转去做不冲突的 src/parser/，或等待

3. Claude Code 通过 limen_write(L1, "src/auth/login.rs", <内容>) 写入
   → Limen 校验路径在 L1 范围内 → 写文件 + 记审计（content_hash, 时间, agent label）

4. Claude Code 调 limen_release(L1)
   → src/auth/ 解锁，Codex 现在可以拿到它

5. 任何时刻，开发者跑 `limen audit`：
   → 看到每次写入：路径、字节数、hash、哪个 agent、哪个 lease
```

**关键**：两个独立的、互不知道对方存在的 harness，通过 Limen 这个共享的"门槛"实现了协调——而它们各自的代码**几乎没有改动**，只是把写入从原生 Write 换成了 Limen 的 MCP tool。

---

## 5. 非目标（Non-Goals）

这一节和功能需求同等重要。**Limen 显式不做以下事情**，其中大部分是 Crawfish 的概念膨胀来源：

### 5.1 抛弃的 Crawfish 概念（全部不做）

`doctrine pack` · `jurisdiction class` · `treaty` · `federation pack` · `evidence bundle` · `oversight checkpoint` · `continuity mode` · `degradation profile` · `verify_loop` · `scorecard` · `evaluation profile` · `review queue` · `alert rule` · `A2A 远程委托` · `OpenClaw 集成` · `pairwise comparison` · `experiment run` · `remote evidence / followup`

> 理由：这些都是"治理一个抽象 agent swarm"的概念。Limen 解决的是"两个具体的 harness 不要撞车"的工程问题，用不到任何一个。

### 5.2 MVP 阶段不做

- **强制中介**：MVP 是**建议式（advisory）**——Limen 发租约、记审计，但不拦截绕过它的原生写入。强制式留作后续可选模式。
- **daemon 平台 / 常驻服务 / dashboard**：Limen 是一个随 harness 生命周期存在的 stdio 进程，不是 etcd 那样的常驻集群服务。
- **agent 编排 / 调度 / 生命周期管理**：Limen 不启动、不停止、不调度任何 agent。
- **跨机器 / 联邦 / 多租户**：MVP 只管单机单 workspace。
- **可视化优先**：CLI 优先，没有 GUI。

### 5.3 永远不做

- 不是 consumer 助手、不是 ETL、不是 workflow 引擎、不是 K8s 替代品、不是 benchmark 刷分框架。
- 不竞争"哪个模型/harness 更强"——Limen 对所有 harness 中立。

---

## 6. 设计原则

详细的设计哲学见 `docs/spec/philosophy.md`（待重写）。此处只列 PRD 级的硬约束：

1. **建议式优先（advisory-first）**。最低接入门槛压倒强一致保证。强制模式是后续的 opt-in。
2. **协议而非平台**。Limen 的价值在它定义的接入面（MCP tools），不在它的运行时大小。运行时越小越好。
3. **5 行接入**。一个用户从"装好"到"第一次成功 acquire lease"应当 < 5 分钟、< 5 行配置。
4. **可量化优于可叙述**。每个声称的收益都要能用实验复现（见 §10.2）。
5. **对 harness 中立**。不假设任何特定 harness，不偏袒任何模型。
6. **与 Zero Trust 词汇对齐**。用 identity / scope / audit，不自创术语。

---

## 7. 核心概念

Limen 只有 5 个一等概念。每个都对应代码里的真实类型，不多造词。

| 概念 | 含义 | MVP 实现 | 代码位置 |
|---|---|---|---|
| **Agent label** | 谁在请求（一个 harness 实例 / sub-agent） | 明文字符串，如 `claude-code:sess-A`。**v0.2 升级为 ed25519 签名身份** | `agent_label` 字段 |
| **Limen / boundary** | 一道门槛：路径模式 | 字面路径或目录前缀（`src/auth/`）。MVP **不支持 glob** | `path_pattern` |
| **Lease** | 在一段时间内对某 boundary 的写权 | 带 intent、TTL（默认 5 min）、state(active/released/expired) | `store.rs::Lease` |
| **Intent** | 想干什么 | `read` / `write` / `propose`。write 互斥；read 让位于 write；propose 永不冲突 | `store.rs::Intent` |
| **Witness / audit** | 每次写入的见证记录 | 路径、字节数、content_hash、时间、归属 lease/agent | `store.rs::WriteRecord` |

**冲突规则**（MVP）：两个 lease 的 path_pattern 有前缀重叠时——

- write × write → **冲突**（拒绝后到者）
- write × read → **冲突**（read 让位）
- read × read → 不冲突
- 任意 × propose → 不冲突（propose 是广告意图，纯咨询）

---

## 8. 功能需求

状态图例：✅ 已实现 · 🚧 进行中 · 📋 计划

### 8.1 MVP（v0.1）

| # | 需求 | 状态 |
|---|---|---|
| R1 | SQLite 持久化 leases + writes 两张表 | ✅ |
| R2 | `acquire_lease`：原子检测冲突 + 插入；自动过期清理 | ✅ |
| R3 | `release_lease` | ✅ |
| R4 | `record_write`：校验 lease 有效 + 路径在范围内，写文件 + 记审计 | ✅ |
| R5 | 冲突仲裁：先到先得 + TTL（write/read/propose 规则） | ✅ |
| R6 | 路径归因 `attribute_path`：给定路径，返回每次写入和对应 agent | ✅ |
| R7 | stdio JSON-RPC 2.0 MCP server：`initialize` / `tools/list` / `tools/call` | ✅ |
| R8 | 三个 MCP tool：`limen_acquire` / `limen_write` / `limen_release` | ✅ |
| R9 | CLI `limen serve`（跑 MCP server）/ `limen audit`（看审计） | ✅ |
| R10 | 端到端 smoke test：subprocess 起 server，跑通完整生命周期 | 🚧 |
| R11 | `limen init`：在 workspace 建 `.limen/` + 默认配置 | 📋 |

### 8.2 后续（v0.2+）

| # | 需求 | 目标版本 |
|---|---|---|
| R12 | **ed25519 agent 身份**：register 发 keypair，每次 acquire/write 带签名，server 验签 | v0.2 |
| R13 | **git notes 归因**：把审计投射到 `git notes`，`git blame` 自动展开到 agent 级 | v0.2 |
| R14 | glob / 更丰富的 boundary 匹配 | v0.3 |
| R15 | 冲突仲裁策略升级：按 agent 优先级（user-foreground > sub-agent > background） | v0.3 |
| R16 | **强制模式（opt-in）**：harness 禁用原生 Write，所有 mutation 必须走 Limen | v0.3 |
| R17 | 被动观察兜底：fsnotify 检测绕过 Limen 的写入并告警 | v0.4 |
| R18 | 实验框架 + concurrent-refactor 数据集（见 §10.2） | v0.4 |

---

## 9. 接入方式

**这是产品的真正入口。** Limen 是一个 MCP server；任何支持 MCP 的 harness 都能接。

### Claude Code（`settings.json` 片段）

```json
{
  "mcpServers": {
    "limen": {
      "command": "limen",
      "args": ["serve", "--db", ".limen/state.db"]
    }
  }
}
```

接好后，Claude Code 即可调用 `limen_acquire` / `limen_write` / `limen_release` 三个 tool。Cursor、Codex 等同理（各自的 MCP 配置格式）。

### 三个 tool

| Tool | 入参 | 出参 |
|---|---|---|
| `limen_acquire` | `path_pattern`, `intent`, `agent_label`, `ttl_ms?` | `lease_id`, `expires_at` 或 conflict 错误 |
| `limen_write` | `lease_id`, `path`, `content` | `content_hash`, `bytes_written` |
| `limen_release` | `lease_id` | `released: bool` |

---

## 10. 成功指标

目标是 **adoption**。但 adoption 的前提是先证明有用，所以指标分两层。

### 10.1 接入摩擦指标（北极星，adoption）

| 指标 | 含义 | 目标 |
|---|---|---|
| **time-to-first-lease** | 从 `cargo install` 到第一次成功 acquire | < 5 分钟 |
| **接入配置行数** | 一个 harness 接入所需配置 | ≤ 5 行 |
| **接入的 harness 种类** | 实测能接的 harness | beta 前 ≥ 3（Claude Code / Codex / 一个 OSS harness） |
| GitHub stars / 周活接入数 | 社区采纳 | 持续增长 |

### 10.2 价值证明指标（实验，证明"有用"）

**Hero Experiment：Coordinated Concurrent Refactor**。N 个 agent 并行改一个高耦合 codebase，对比有无 Limen。

| Arm | 描述 |
|---|---|
| Seq-1 | 单 agent 串行（pass@1 上限基线） |
| Par-N-Naive | N=3 并行，无 Limen |
| Par-N-Limen | N=3 并行 + Limen lease |

| 指标 | 期望 |
|---|---|
| **pass@1**（patch 应用后过隐藏测试） | Par-N-Limen 逼近 Seq-1，**远超** Par-N-Naive |
| **wall-clock** | Par-N-Limen ≈ Par-N-Naive < Seq-1 |
| **lost-edit-lines**（被覆盖丢失的代码行） | Par-N-Limen ≈ 0；Par-N-Naive 随 K 增大而暴涨 |
| **build-break-rate** | Par-N-Limen ≪ Par-N-Naive |
| **attribution-accuracy** | 给定最终代码 hunk，能否正确归因到 agent（仅 Limen arm 有意义） |

**核心论点 = 条件性阈值律**：协调的价值取决于任务耦合度——耦合阈值 τ 以下，Par-N-Limen 对 Par-N-Naive 形成 Pareto 改进（近乎零时间代价换安全 + 可靠性）；τ 以上安全/可靠性收益仍在，但 wall-clock 优势反转。主指标是 **pass^k**（重复并发下全部成功），而非单次 pass@1。

> 详细实验设计（数据集、cell 数、统计方法、隐患）见后续 `docs/experiments.md`。

---

## 11. 架构

**核心反转**：Crawfish 的 `crawfish-mcp` 是 MCP **客户端**（Crawfish 调别人）。Limen 是 MCP **服务器**（被 harness 调）。这是从"指挥官"到"协调点"的根本转变。

```
   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
   │ Claude Code │    │   Cursor    │    │ Codex CLI   │
   │  + sub-agts │    │             │    │             │
   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
          │ MCP (stdio)      │ MCP              │ MCP
          ▼                  ▼                  ▼
   ╔══════════════════════════════════════════════════╗
   ║   limen serve   (stdio MCP server — 产品入口)     ║
   ║   tools: limen_acquire / limen_write / release    ║
   ╠══════════════════════════════════════════════════╣
   ║   仲裁 (lease 冲突)  ·  审计 (witness)            ║
   ╠══════════════════════════════════════════════════╣
   ║   SQLite (.limen/state.db)  —  leases + writes    ║
   ╚══════════════════════════════════════════════════╝
```

单 crate（`crates/limen`），三个模块：`store.rs`（SQLite）、`mcp.rs`（JSON-RPC server）、`main.rs`（CLI）。**总代码量目标控制在 1500 行以内**，对比 Crawfish 的 10 个 crate。

---

## 12. 路线图

| 版本 | 主题 | 内容 | 状态 |
|---|---|---|---|
| **v0.1** | MVP 可跑 | lease + write + release + audit，stdio MCP，建议式 | 🚧 接近完成（R1–R9 ✅，R10 🚧） |
| **v0.2** | 身份与续约 | ✅ ed25519 可选签名身份（`register`/`sign`）；✅ lease 续期（`limen_renew`）；✅ 区域规范化（别名 soundness）。`limen init` 已于早前完成；git-notes 归因因属文件系统/git 专有、与通用核心相悖而推迟 | ✅ |
| **v0.3** | 仲裁与强制 | glob boundary；优先级仲裁；opt-in 强制模式 | 📋 |
| **v0.4** | 验证价值 | 被动观察兜底；hero experiment 数据 + Pareto 图 | 📋 |
| 后续 | 生态 | 更多 harness 适配；可选的协调协议规范化 | 📋 |

---

## 13. 风险与开放问题

| 风险 | 说明 | 缓解 |
|---|---|---|
| **建议式 = 弱保证** | agent 可绕过 Limen 直接写文件 | v0.3 强制模式 + v0.4 fsnotify 兜底；MVP 阶段诚实标注"advisory" |
| **采用依赖 harness 配合** | 没有 harness 接入就没价值 | 从 Claude Code（MCP 原生支持好）切入，先做出一个 harness 的完整体验 |
| **Cursor 闭源难 instrument** | 实验和接入受限 | 主路径用 Claude Code + Codex + OSS harness，Cursor 留后续 |
| **lease 粒度** | 前缀匹配可能太粗（锁了 `src/` 等于锁全部） | v0.3 glob + 更细粒度；MVP 先验证概念 |
| **又一次改名的信誉成本** | 这是第三次改名 | 这次改名前已通过实现验证方向，不再是纯命名摇摆 |
| **开放问题** | 强制模式如何在不改 harness 源码的前提下实现？（拦截原生 Write） | 待 v0.3 设计：MCP 层约定 vs FS 层拦截 |

---

## 14. 附录：从 Crawfish 继承 / 抛弃了什么

| | 继承 | 抛弃 |
|---|---|---|
| **概念** | lease、audit、agent identity（雏形） | doctrine / treaty / federation / jurisdiction / continuity / verify_loop / scorecard / A2A / OpenClaw |
| **代码** | workspace 文件锁算法思路、SQLite 持久化模式、Rust workspace 结构 | 10 个 crate 压缩成 1 个；MCP 方向反转（client → server） |
| **姿态** | — | "control plane / law layer / govern" 统治者语气 |
| **文档** | — | 12 条哲学承诺、十几个外链 cite、P1a–P1o 的 15 个子阶段 |
| **hero demo** | — | `repo_indexer/repo_reviewer/ci_triage` 这套换成 concurrent refactor |

---

*本 PRD 是 Limen 的立项基石。设计哲学（`philosophy.md`）、详细架构（`architecture.md`）、MCP 协议规范（`protocol.md`）、实验设计（`experiments.md`）为后续文档。*
