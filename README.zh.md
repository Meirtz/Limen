# Limen

> **一个面向 AI agent 的 workspace 协调守护进程。** 当多个 AI coding agent 共享同一个仓库时，Limen 给每个 agent 一张按边界划定的**写入租约**和一条**可追溯的见证审计**——让它们不再互相静默覆盖。它不运行你的 agent，只防止它们撞车。

![status](https://img.shields.io/badge/status-alpha-orange) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue) ![rust](https://img.shields.io/badge/rust-1.88%2B-orange) ![protocol](https://img.shields.io/badge/MCP-server-black)

[English](README.md) · **中文**

Limen 是一个单一的小 Rust 守护进程，以 [MCP](https://modelcontextprotocol.io) server 形态暴露。它坐在共享工作区的 agent *之下*，就在它们唯一会碰撞的地方：写入。它的姿态是刻意的——**仆从，不是统治者。** 像 Git、MCP、OAuth、OpenTelemetry 一样，它*描述并发放*，不治理。它不启动、不停止、不调度、不路由、不监督任何 agent。

它最初的窄目标（"滩头"）是多 harness 的 AI coding 共享一个 git 仓库。背后那个持久的想法是通用的：**对并发自治 agent 在共享可变状态上的协调**，凭三个原语——建议式**租约（lease）**、**见证（witness）**轨迹、per-agent **身份（identity）**——从四十年的分布式系统并发控制移植而来，并入 [AI agent 零信任](https://claude.com/blog/zero-trust-for-ai-agents)的范式。

---

## 问题

今天一个开发者的同一份仓库上，可能同一时刻：

- 一个 **Claude Code** session 在重构 `src/auth/`
- 一个 **Cursor** 窗口开着、buffer 是旧版本
- 一个 **Codex** 后台任务在跑测试
- Claude Code 自己 spawn 的 **2–3 个 sub-agent** 在并行改不同文件

**没有任何一层在协调它们。** 后果具体且可复现：

| 失败模式 | 后果 |
| --- | --- |
| 两个 agent 同时写同一文件 | 后写覆盖先写——先写的工作**静默丢失** |
| agent A 改了函数签名，agent B 还在用旧签名调用 | **build break** / 测试挂 |
| Cursor 基于旧版本保存 buffer | 覆盖掉另一 agent 刚提交的修改 |
| 后台 agent 删了文件，前台还在编辑 | 编辑器写回一个"已删除"的幽灵文件 |
| 出 bug，`git blame` 全是 user | **无法归因**——哪个 agent、哪个 prompt？ |

这就是经典的 **lost-update / write-skew** 问题（Berenson 等, SIGMOD 1995）——只不过数据库和版本控制是用锁、快照、merge 纪律*挣来*的安全，而 agent 一样都没继承到。它们是非确定性写者，从没被写成"先加锁"，且彼此盲视。Limen 把解药移植到那一层：写者是 agent，共享状态是一个没人上过锁的文件系统。

---

## 怎么工作

两个互不知道对方存在的独立 harness，共享一个仓库，通过同一道门槛协调——而它们各自几乎不用改：

```
   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
   │ Claude Code │    │   Cursor    │    │  Codex CLI  │
   │  + subagents│    │             │    │             │
   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
          │ MCP (stdio)      │ MCP              │ MCP
          ▼                  ▼                  ▼
   ╔══════════════════════════════════════════════════╗
   ║   limen serve   (stdio MCP server)               ║
   ║   tools: limen_acquire / limen_write / release    ║
   ╠══════════════════════════════════════════════════╣
   ║   仲裁 (lease 冲突)  ·  见证 (审计)                ║
   ╠══════════════════════════════════════════════════╣
   ║   SQLite (.limen/state.db)  —  leases + writes    ║
   ╚══════════════════════════════════════════════════╝
```

1. Claude Code 要 `src/auth/` → `limen_acquire("src/auth/", "write", "claude-code:sess-A")` → 拿到租约 **L1**（TTL 5 分钟）。
2. Codex 要 `src/auth/login.rs` → `limen_acquire(...)` → Limen 检测到 L1 覆盖此路径 → 返回 **conflict**（"由 claude-code:sess-A 持有"）。Codex 转去不冲突的 `src/parser/`，或等待。
3. Claude Code 通过 `limen_write(L1, "src/auth/login.rs", …)` 写入 → Limen 校验路径在范围内、写文件、记一条见证（内容 hash、时间、agent）。
4. Claude Code 调 `limen_release(L1)` → `src/auth/` 解锁；Codex 现在可以拿到它。
5. 任何时刻，`limen audit` 展示每次写入：路径、字节数、hash、哪个 agent、哪张租约。

---

## 快速开始

Limen 处于 alpha，从源码构建：

```bash
cargo install --path crates/limen     # 或：cargo build -p limen --release
```

把任何支持 MCP 的 harness 指向它。以 **Claude Code**（`settings.json`）为例：

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

就这样——Claude Code、Cursor、Codex 等现在都能调用三个 tool：

| Tool | 入参 | 出参 |
| --- | --- | --- |
| `limen_acquire` | `path_pattern`、`intent`（`read`\|`write`\|`propose`）、`agent_label`、`ttl_ms?` | `lease_id`、`expires_at`——或 conflict |
| `limen_write` | `lease_id`、`path`、`content` | `content_hash`、`bytes_written` |
| `limen_release` | `lease_id` | `released: bool` |

随时查看发生了什么：

```bash
limen audit --db .limen/state.db        # 活跃租约 + 最近的见证写入
```

---

## 核心概念

Limen 恰好只有五个一等概念，每个都对应代码里的真实类型——不造新词。完整定义见 [`docs/spec/glossary.zh.md`](docs/spec/glossary.zh.md)。

| 概念 | 含义 | 今天 |
| --- | --- | --- |
| **身份** | 谁在请求 | 明文 agent 标签（`claude-code:sess-A`）；计划 ed25519 |
| **边界**（一个 *limen*） | 命名空间中的一块区域 | 字面路径或目录前缀（`src/auth/`） |
| **租约** | 对某边界的带时限授权 | 意图 + TTL（5 分钟）+ 状态 |
| **意图** | 持有者想做什么 | `read` / `write` / `propose` |
| **见证** | 一次写入的留痕证据 | 路径、字节数、SHA-256、时间、agent |

**冲突规则**（边界交叠时）：`write × write` 冲突 · `write × read` 冲突（读者让位） · `read × read` 不冲突 · `propose` 永不冲突。

---

## 范围：Limen 是什么、不是什么

Limen 的前身死于概念膨胀；保持小是全部要义。详见 [`docs/spec/boundaries.zh.md`](docs/spec/boundaries.zh.md)。

| | |
| --- | --- |
| ✅ **是** | 一个经 MCP 的建议式租约管理器 + 写入中介 + 见证器；对 harness 中立；价值随同时运行的 harness 数量上升 |
| 🚫 **现在不是** | 强制中介（它是建议式的——发放并见证，不拦截）；常驻集群服务；跨机；密码学身份 |
| ⛔ **永远不是** | agent 运行时 / 编排器 / 调度器；治理 / 策略 / "法律"层；模型或 harness 竞争者 |

它协调**共享状态**，不编排 agent。那条线：如果一个功能只有在 Limen *管着*某个 agent 时才说得通，它就出界——**Limen 永远不在管。**

---

## 为什么是现在

- **harness 爆发。** Claude Code、Cursor、Codex、Gemini CLI、Copilot CLI、aider、cline——同时用 2–3 个已是常态。
- **sub-agent 普及。** 主流 harness 都已并行 spawn sub-agent（Claude Code 的 `Agent` tool）。一个 harness 内部就已是 multi-writer。
- **没人占这一层。** harness 厂商各自卷 IDE 体验；Anthropic 在写 [零信任](https://claude.com/blog/zero-trust-for-ai-agents)范式。*"让它们在同一个 workspace 和平共处"*这一层是空的。

---

## 我们打算证明的命题

Limen 给出一个实验能证伪的主张——这正是它诚实而非修辞的原因：

> 在固定并行度 N 下，**`Par-N-Limen` 在 (wall-clock × pass@1) 上 Pareto 支配 `Par-N-Naive`**——两个维度都不更差，至少一个严格更好——同时在 lost-edit-lines、build-break-rate、归因上严格占优。

| Arm | 设置 |
| --- | --- |
| **Seq-1** | 单 agent 串行——正确性上限 / 时延基线 |
| **Par-N-Naive** | N 个并发 agent，无协调 |
| **Par-N-Limen** | N 个并发 agent + 建议式租约 |

指标：**pass@1**（以及更严的 **pass^k**，度量重复并发下的可靠性）、**wall-clock**、**lost-edit-lines**、**build-break-rate**、**归因准确率**。完整设计、可比工作与效度威胁见 [`docs/spec/related-work.md`](docs/spec/related-work.md)。*（这是评估计划，不是已测结果。）*

---

## 谱系

Limen 站在已尘埃落定的地基上，而非 LLM 编排时尚——因为推理易变，协调不变。

- **并发控制与租约：** Gray & Cheriton《Leases》(SOSP 1989)；Burrows《Chubby》——建议式锁 (OSDI 2006)；ZooKeeper、etcd、Consul；POSIX `flock`；lost-update/write-skew 分类 (Berenson 等, SIGMOD 1995)。
- **共享状态协调：** 黑板系统 (Hearsay-II)、元组空间 (Linda)；以及 Limen 通过*写前预防*来对照的 CRDT/OT *写后合并*家族。
- **AI agent 零信任：** Anthropic [Zero Trust for AI agents](https://claude.com/blog/zero-trust-for-ai-agents)，根基是 NIST SP 800-207 与最小权限 (Saltzer & Schroeder 1975)。

带注解、经核实的参考索引：[`docs/references.md`](docs/references.md)。这一空间已有 prior art（尤其 MCP Agent Mail）——Limen 的主张是*泛化的范畴 + 一个严格的实验*，而非"首个"。

---

## 状态

Limen 处于 **alpha**，并对此诚实。

| 面 | 状态 |
| --- | --- |
| MVP（lease + write + release + audit，stdio MCP） | 已实现 |
| 强制 | **仅建议式**——agent 可绕过；见证仍归因 |
| 身份 | 明文标签（计划 ed25519 签名） |
| 范围 | 单机、单工作区 |
| 边界匹配 | 字面路径 / 目录前缀（暂无 glob） |

命名线：AgentGraph → Crawfish → **Limen**（从过度伸张的"governed swarm 的 control plane"重构为一个锋利的协调原语）。

---

## 文档

- [`docs/PRD.md`](docs/PRD.md) — 产品需求文档
- [`docs/spec/philosophy.zh.md`](docs/spec/philosophy.zh.md) — 为何是这个形态（[English](docs/spec/philosophy.md)）
- [`docs/spec/boundaries.zh.md`](docs/spec/boundaries.zh.md) — Limen 是什么、不是什么（[English](docs/spec/boundaries.md)）
- [`docs/spec/glossary.zh.md`](docs/spec/glossary.zh.md) — canonical 术语（[English](docs/spec/glossary.md)）
- [`docs/spec/related-work.md`](docs/spec/related-work.md) — related work 与实验框架
- [`docs/references.md`](docs/references.md) — 带注解的参考索引

贡献与安全策略：[`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) · [`.github/SECURITY.md`](.github/SECURITY.md)。

## 许可

双许可：[MIT](LICENSE-MIT) 或 [Apache-2.0](LICENSE-APACHE)，任选其一。
