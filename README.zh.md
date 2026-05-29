# Limen

> **Limen 协调共享可变状态的并发自治 agent。** 它给每个 agent 一张建议式、按边界划定、带时限的**租约**，并保留一条**可追溯的见证审计**——让相互独立的 agent 不再静默覆盖彼此的工作。它不运行你的 agent，只防止它们撞车。

![status](https://img.shields.io/badge/status-alpha-orange) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue) ![rust](https://img.shields.io/badge/rust-1.88%2B-orange) ![protocol](https://img.shields.io/badge/MCP-server-black)

[English](README.md) · **中文**

Limen 是一个单一的小 Rust 守护进程，以 [MCP](https://modelcontextprotocol.io) server 形态暴露。它坐在共享状态的 agent *之下*，就在它们唯一会碰撞的地方：变更。它的姿态是刻意的——**仆从，不是统治者。** 像 Git、MCP、OAuth、OpenTelemetry 一样，它*描述并发放*，不治理。它不启动、不停止、不调度、不路由、不监督任何 agent。

这个模型是通用的。一张**租约（lease）**是对某**命名空间（namespace）**的一块**区域（region）**的带时限授权；一条**见证（witness）**把每次中介变更连同其**身份（identity）**一并记录。这一切都不绑定于文件——命名空间是任意可寻址的可变资源空间（文件系统、KV 存储、配置树、一组云对象），agent 可以是 coding、research、ops、computer-use agent，或纯粹的管线。Limen 把四十年的分布式系统并发控制——租约、建议式锁——移植进 [AI agent 零信任](https://claude.com/blog/zero-trust-for-ai-agents)时代。

今天它实现了一种资源——**文件系统**——并附带一个让问题变得鲜活的例子：多个 AI coding agent 共享一个仓库。

---

## 问题

当多个自治 agent 在没有任何协调者的情况下变更共享状态，它们就重新制造了经典的并发危害——数据库与版本控制花了几十年才驯服的那些：

| 危害 | 会发生什么 |
| --- | --- |
| **丢失更新（Lost update）** | 两个 agent 改同一资源；后写静默抹掉先写 agent 的工作 |
| **不变式破坏（Broken invariant）** | 一个 agent 改了另一个还依赖的东西；合起来的状态不一致——build 挂、schema 不匹配 |
| **陈旧/撕裂读（Stale / torn read）** | 一个 agent 基于另一个已经越过的快照行动 |
| **无法归因（No attribution）** | 出了问题，却没有记录*哪个* agent 在何种意图下改了*什么* |

数据库和版本控制是用锁、快照、merge 纪律*挣来*的安全。今天的 agent 一样都没继承到：它们是非确定性写者，从没被写成"先加锁"，且彼此盲视。Limen 把解药移植到那一层：写者是 agent，共享状态是某个没人上过锁的东西。

**一个具体例子**——一个开发者的仓库，在同一时刻：

- 一个 **Claude Code** session 在重构 `src/auth/`
- 一个 **Cursor** 窗口开着、buffer 是旧版本
- 一个 **Codex** 后台任务在跑测试
- Claude Code 自己 spawn 的 **2–3 个 sub-agent** 在并行改不同文件

没有任何一层在协调它们，于是上面每一种危害都具体且可复现——而 `git blame` 只显示那个人。

---

## 怎么工作

对任何资源，机制都一样：

1. **acquire**：在某区域以某意图（`read` / `write` / `propose`）获取租约——或得知它与别人已持有的租约冲突
2. **write**：在租约内变更——Limen 校验目标落在区域内、施加变更、记一条见证（字节数、内容哈希、时间、agent）
3. **release**：区域释放给下一个持有者

冲突由区域交叠决定：`write × write`、`write × read` 冲突，`read × read` 不冲突，`propose` 永不冲突。每张租约带 TTL 并自动过期，故崩溃的 agent 不会让命名空间死锁。

**例子——两个 coding harness 共享一个仓库**（文件系统资源）：

```
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │   Agent A    │   │   Agent B    │   │   Agent C    │
   │ (Claude Code)│   │   (Cursor)   │   │   (Codex)    │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │ MCP (stdio)      │ MCP              │ MCP
          ▼                  ▼                  ▼
   ╔══════════════════════════════════════════════════╗
   ║   limen serve   (stdio MCP server)               ║
   ║   tools: limen_acquire / limen_write / release    ║
   ╠══════════════════════════════════════════════════╣
   ║   仲裁 (lease 冲突)  ·  见证 (审计)                ║
   ╠══════════════════════════════════════════════════╣
   ║   资源: 文件系统       ·       SQLite state.db     ║
   ╚══════════════════════════════════════════════════╝
```

1. Agent A 要 `src/auth/` → `limen_acquire("src/auth/", "write", "claude-code:sess-A")` → 租约 **L1**（TTL 5 分钟）。
2. Agent C 要 `src/auth/login.rs` → **conflict**（A 持有）。它转去 `src/parser/`，或等待。
3. A 通过 `limen_write(L1, "src/auth/login.rs", …)` 写入 → 在区域内、已施加、已见证。
4. A 调 `limen_release(L1)` → 区域释放；C 可以拿到。
5. `limen audit` 展示每次变更：区域、字节数、hash、哪个 agent、哪张租约。

---

## 快速开始

Limen 处于 alpha，从源码构建，然后初始化工作区：

```bash
cargo install --path crates/limen        # 或：cargo build -p limen --release
cd your-project && limen init            # 创建 .limen/ 并打印下面的 MCP 配置
```

`limen init` 会打印可直接粘贴的配置。把任何支持 MCP 的 host 指向它——以 **Claude Code**（`settings.json`）为例：

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

就这样——Claude Code、Cursor、Codex 等任何 MCP host 现在都能调用三个 tool（参数以文件系统资源为例）：

| Tool | 入参 | 出参 |
| --- | --- | --- |
| `limen_acquire` | `path_pattern`（区域）、`intent`（`read`\|`write`\|`propose`）、`agent_label`、`ttl_ms?` | `lease_id`、`expires_at`——或 conflict |
| `limen_write` | `lease_id`、`path`（目标）、`content` | `content_hash`、`bytes_written` |
| `limen_release` | `lease_id` | `released: bool` |
| `limen_renew` | `lease_id`、`ttl_ms?` | `expires_at`（已延长） |

随时查看发生了什么：

```bash
limen audit --db .limen/state.db          # 活跃租约 + 最近的见证变更
limen attribute src/auth/login.rs         # 谁、何时、在哪张租约下改了它
```

**可选——密码学身份。** 注册一个 agent，把它的标签从*被声称*升级为 *ed25519 可验证*：

```bash
limen register claude-code:sess-A                # 生成密钥对；打印公钥
limen sign claude-code:sess-A src/auth/ write    # 把签名作为 limen_acquire 的 `signature` 传入
```

注册后，该 agent 的 `limen_acquire` 必须带有效 `signature`（其后租约即作为 write 的 bearer capability）；未注册的标签仍走明文路径。

---

## 核心概念

Limen 的词汇小而通用——每个术语都对应代码里的真实类型，不造新词。完整定义见 [`docs/spec/glossary.zh.md`](docs/spec/glossary.zh.md)。

| 概念 | 含义 | 文件系统资源（今天） |
| --- | --- | --- |
| **命名空间（namespace）** | 被协调的可变资源的可寻址空间 | 工作区的文件 |
| **区域（region，一个 *limen*）** | 租约覆盖的命名空间的一块 | 路径或目录前缀（`src/auth/`） |
| **身份（identity）** | 谁在请求 | 明文标签，或注册的 **ed25519** 密钥（`limen register`），agent 用它为每次 acquire 签名 |
| **租约（lease）** | 对某区域的带时限授权 | 意图 + TTL（5 分钟）+ 状态 |
| **意图（intent）** | 持有者想做什么 | `read` / `write` / `propose` |
| **见证（witness）** | 一次中介变更的留痕证据 | 目标、字节数、SHA-256、时间、agent |

**冲突规则**（区域交叠时）：`write × write` 冲突 · `write × read` 冲突（读者让位） · `read × read` 不冲突 · `propose` 永不冲突。

---

## 范围：Limen 是什么、不是什么

Limen 的前身死于概念膨胀；保持小是全部要义。概念是通用的，但暴露面只有一个锋利的原语。详见 [`docs/spec/boundaries.zh.md`](docs/spec/boundaries.zh.md)。

| | |
| --- | --- |
| ✅ **是** | 一个经 MCP 的建议式租约管理器 + 变更中介 + 见证器；对 agent 中立、对资源可插拔；价值随共享命名空间的 agent 数量上升 |
| 🚫 **现在不是** | 强制中介（它是建议式的——发放并见证，不拦截）；不止一种资源后端；跨机；密码学身份 |
| ⛔ **永远不是** | agent 运行时 / 编排器 / 调度器；治理 / 策略 / "法律"层；模型或 harness 竞争者 |

它协调**共享状态**，不编排 agent。那条线：如果一个功能只有在 Limen *管着*某个 agent 时才说得通，它就出界——Limen 永远不在管。

---

## 为什么是现在

- **会写的 agent 在变多。** coding harness（Claude Code、Cursor、Codex、Gemini CLI、aider）、research 与 ops agent、computer-use agent——每个月都有更多在动共享状态。
- **fan-out 是内建的。** 主流 harness 都已并行 spawn sub-agent（Claude Code 的 `Agent` tool）。一个 host 内部就已是 multi-writer。
- **没人占这一层。** 厂商各自卷 agent loop；Anthropic 在写 [零信任](https://claude.com/blog/zero-trust-for-ai-agents)范式。*"让相互独立的 agent 安全共享一个命名空间"*这一层是空的。

---

## 我们打算证明的命题

Limen 给出一个实验能证伪的主张——这正是它诚实而非修辞的原因。我们在文件系统这个例子上检验它（并发 coding agent 共享一个仓库），因为那是今天痛感可度量之处：

> 协调的价值**取决于任务耦合度**，且最先崩的是可靠性。随写者数 N 与耦合度上升，朴素并发的 **pass^k**（k 次重复全部成功）因丢失编辑与 build break 而超线性崩塌；建议式协调在**耦合阈值 τ 以下**收回大部分代价（Pareto 改进——以近乎零时间代价换安全），而 **τ 以上**安全收益仍在、wall-clock 优势反转。

| Arm | 设置 |
| --- | --- |
| **Seq-1** | 单 agent 串行——正确性上限 / 时延基线 |
| **Par-N-Naive** | N 个并发 agent，无协调 |
| **Par-N-Placebo** | N 个 agent 走见证 wrapper 但**不仲裁**——把 wrapper 与协调本身区分开 |
| **Par-N-Limen** | N 个并发 agent + 建议式区域租约 |
| **Par-N-Limen+Deps** | 增加一轮建议式 write×read 调和，救回区域租约挡不住的跨文件耦合 |

指标：**pass@1**（以及更严的 **pass^k**，度量重复并发下的可靠性）、**wall-clock**、**lost-edit-lines**、**build-break-rate**、**归因准确率**。测量装置已实现于 [`crates/limen-bench`](crates/limen-bench)（各 arm、与协调无关的 oracle、按耦合分类的任务族，以及 `pilot` / `sweep` / `analyze` 子命令）；完整可执行设计见 [`docs/experiments.md`](docs/experiments.md)，related work 与框架见 [`docs/spec/related-work.md`](docs/spec/related-work.md)。*（大规模预注册研究仍是 future work；此处不声称任何 headline 数字。）*

---

## 谱系

Limen 站在已尘埃落定的地基上，而非 LLM 编排时尚——因为推理易变，协调不变。

- **并发控制与租约：** Gray & Cheriton《Leases》(SOSP 1989)；Burrows《Chubby》——建议式锁 (OSDI 2006)；ZooKeeper、etcd、Consul；POSIX `flock`；lost-update/write-skew 分类 (Berenson 等, SIGMOD 1995)。
- **共享状态协调：** 黑板系统 (Hearsay-II)、元组空间 (Linda)；以及 Limen 通过*变更前预防*来对照的 CRDT/OT *写后合并*家族。
- **AI agent 零信任：** Anthropic [Zero Trust for AI agents](https://claude.com/blog/zero-trust-for-ai-agents)，根基是 NIST SP 800-207 与最小权限 (Saltzer & Schroeder 1975)。

带注解、经核实的参考索引：[`docs/references.md`](docs/references.md)。这一空间已有 prior art（尤其 MCP Agent Mail）——Limen 的主张是*泛化的模型 + 一个严格的实验*，而非"首个"。

---

## 状态

Limen 处于 **alpha**，并对此诚实。

| 面 | 状态 |
| --- | --- |
| MVP（lease + write + release + audit，stdio MCP） | 已实现 |
| 资源 | 一种（文件系统）；模型对资源可插拔 |
| 强制 | **仅建议式**——agent 可绕过；见证仍归因 |
| 身份 | 默认明文；可选 **ed25519** 签名身份（`limen register` / `limen sign`） |
| 范围 | 单机、单命名空间 |
| 区域匹配 | 字面路径 / 目录前缀（暂无 glob） |
| 实验装置 | [`crates/limen-bench`](crates/limen-bench)——各 arm、与协调无关的 oracle、按耦合分类的任务、`pilot`/`sweep`/`analyze` |

命名线：AgentGraph → Crawfish → **Limen**（从过度伸张的"governed swarm 的 control plane"重构为一个通用的协调原语）。

---

## 文档

- [`docs/PRD.md`](docs/PRD.md) — 产品需求文档
- [`docs/spec/philosophy.zh.md`](docs/spec/philosophy.zh.md) — 为何是这个形态（[English](docs/spec/philosophy.md)）
- [`docs/spec/boundaries.zh.md`](docs/spec/boundaries.zh.md) — Limen 是什么、不是什么（[English](docs/spec/boundaries.md)）
- [`docs/spec/glossary.zh.md`](docs/spec/glossary.zh.md) — canonical 术语（[English](docs/spec/glossary.md)）
- [`docs/spec/related-work.md`](docs/spec/related-work.md) — related work 与实验框架
- [`docs/experiments.md`](docs/experiments.md) — hero experiment 设计
- [`docs/references.md`](docs/references.md) — 带注解的参考索引

贡献与安全策略：[`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) · [`.github/SECURITY.md`](.github/SECURITY.md)。

## 许可

双许可：[MIT](LICENSE-MIT) 或 [Apache-2.0](LICENSE-APACHE)，任选其一。
