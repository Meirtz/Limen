# Limen — 边界与范围（中文伴随版）

> 术语见 [`glossary.md`](glossary.md) · 思想论证见 [`philosophy.zh.md`](philosophy.zh.md) · 参考文献见 [`../references.md`](../references.md)。
>
> **英文 [`boundaries.md`](boundaries.md) 为 canonical 版本**；本文是其中文伴随版，如有出入以英文为准。

一个项目的边界和它的功能同等重要。Limen 的前身死于概念膨胀；本文存在的意义，就是不让那件事重演。它陈述 Limen **是什么**、**刻意不是什么**（现在与永远），以及它相对邻居们究竟坐在哪里。

---

## 通用模型与它的第一个资源

**通用模型（定义）。** Limen 协调**在共享可变状态上并发操作的自治 agent**，凭三个原语：

- 一张建议式、按区域、带时限的**租约（lease）**（对命名空间某区域的变更授权），
- 一条**见证（witness）**轨迹（对每次中介变更的归因与取证），
- 以及一个 per-agent 的**身份（identity）**。

**区域**——*limen*，拉丁语"门槛"——是命名空间中的一块。模型的根在分布式系统的并发控制与零信任安全，不在 LLM 编排；它是代码与文档共同的组织原则。

**第一个资源（今天）。** 命名空间通过一个*资源（resource）*触达——一个可插拔后端，规定区域如何比较、中介变更如何施加。v0.1 恰好发布一种：**文件系统**。它的 worked example 是若干 AI coding agent（Claude Code、Cursor、Codex）及其 sub-agent 共用一棵 git 工作树，以 MCP server 接入、建议式优先。文件系统是*一个示例资源*，不是定义——模型里没有任何东西绑定于文件或 coding。

## 一句话

> **Limen 是一个 workspace 协调守护进程。** 当多个 AI agent 共享一个工作区时，Limen 给每个 agent 一个（准）签名身份、一张按边界划定的租约、和一条见证审计轨迹——让它们不互相踩踏。它不运行你的 agent，只防止它们撞车。

## 姿态：仆从，不是统治者

这个领域里被广泛采用的基础设施从不声称"统治"任何东西。Limen 采取同样的立场，并显式抛弃其前身那套"control plane / 治理 / 法律层"语气。

| 它说 | "我做这个" | "我**不**做这个" |
| --- | --- | --- |
| Git | 我追踪 diff | 我管理你的代码 |
| MCP | 我规定工具调用的 wire format | 我控制你的 agent |
| OpenTelemetry | 我规定 span 格式 | 我监督你的系统 |
| OAuth | 我规定授权握手 | 我决定你是谁 |
| **Limen** | **我发租约、记见证** | **我治理你的 agent swarm** |

---

## Limen 是什么

- 一个单一的小守护进程（一个 Rust crate），以 **MCP server** 形态经 stdio 暴露。
- 一个**租约管理器**：原子的、带冲突检测的 `acquire`，带 TTL 自动过期、`release`、以及一张类型化的冲突矩阵。
- 一个**写入中介 + 见证器**：`write` 校验租约、执行写入、并记录 `(lease_id, path, 字节数, SHA-256, 时间戳, agent 标签)`。
- 一个**归因面**：给定一个路径，回答谁改的、何时、在哪张租约下。
- **对 harness 中立**：不假设任何特定 harness，不偏袒任何模型。

## Limen 现在不是什么（MVP 阶段非目标）

这些当前线之外，但不是永远。

- **强制中介。** MVP 是建议式：它发租约、记见证，但不拦截绕过它的写入。强制（以及被动的绕过检测）是后续的、opt-in 的模式。
- **常驻平台 / 集群服务 / dashboard。** Limen 是一个随 harness session 经 stdio 共生共死的进程，不是 etcd 式的常驻集群，也不是 GUI。CLI 优先。
- **跨机 / 联邦 / 多租户。** MVP 只协调单机、单工作区。（谱系——Chubby、etcd——*本就是*分布式的，所以这是泛化维度，不是墙。）
- **密码学身份。** agent 身份今天是明文标签；ed25519 签名身份是计划而非现状。在此之前，Limen 受 capability *启发*、而非 capability *强制*。

## Limen 永远不是什么（范畴级非目标）

这些与范畴相悖，不会加入。

- **agent 运行时 / 编排器 / 调度器。** Limen 永不启动、停止、调度、路由或监督一个 agent。如果一个功能只有在 Limen "管着"这些 agent 时才说得通，它就出界——**Limen 永远不在管**。
- **治理 / 策略 / "法律"层。** 没有 doctrine、jurisdiction、treaty、federation、证据可采性、审批门控这些机器。（这些是其前身的概念膨胀，全部抛弃。）
- **模型或 harness 竞争者。** Limen 不试图让 agent 更聪明，也不争"哪个 harness 最强"。它是位于它们之下的中立基础设施，其价值随在用 harness 数量的增加而*上升*。
- **消费级助手、ETL/批处理引擎、workflow 引擎、Kubernetes/Temporal 替代品、benchmark 刷分框架。**

---

## 五个一等概念

Limen 恰好只有五个概念，每个都对应实现里的真实类型——不造新词。

| 概念 | 含义 | 滩头形态（代码） | 通用形态 |
| --- | --- | --- | --- |
| **身份**（`agent_label`） | 谁在请求 | 明文标签，如 `claude-code:sess-A`；计划 ed25519 | 任何可密码学定根的 principal |
| **边界 / limen** | 跨越一块区域的门槛 | 字面路径或目录前缀（`src/auth/`）；暂无 glob | 命名空间上的任意 selector |
| **租约** | 对某边界的带时限授权 | `intent` + TTL（默认 5 分钟）+ 状态（active/released/expired） | 对某区域的带时限 capability |
| **意图（Intent）** | 持有者想做什么 | `read` / `write` / `propose` | 同样的访问模式，泛化 |
| **见证** | 一次写入的留痕证据 | 路径、字节数、SHA-256、时间、归属租约 + agent | 对任意资源的逐次写入归因 |

### 冲突矩阵

两张租约冲突，当且仅当它们的边界交叠（前缀包含）**且**意图相克：

| | write | read | propose |
| --- | --- | --- | --- |
| **write** | ⛔ 冲突 | ⛔ 冲突（读者让位） | ✅ 可 |
| **read** | ⛔ 冲突 | ✅ 可 | ✅ 可 |
| **propose** | ✅ 可 | ✅ 可 | ✅ 可 |

`write × write` 是协调层对 **Lost Update** 的预防。`write × read` 的存在，是因为接口区域的读者必须在写者拿走它时让位（对 **Write Skew** 的部分防御）。`propose` 是一个纯粹的非阻塞建议式声明——"我打算碰这块"——永不冲突。

---

## 边界线（Limen 在哪里结束、邻居从哪里开始）

| 邻居 | 它做什么 | 与 Limen 的关系 |
| --- | --- | --- |
| **Harness**（Claude Code、Cursor、Codex、Gemini CLI） | 跑 agent 循环、改文件 | Limen **服务**它们；价值随同时运行的数量上升。单 harness 不需要协调；N 个同在一棵树上才是碰撞所在。 |
| **agent 框架**（LangGraph、OpenAI Agents SDK、AutoGen、Swarm） | 构建并编排 agent；协调**单应用权限内的上下文**（"handoff 停留在单次 run 内"） | **正交。** 它们工程化 agent *看到*什么；Limen 协调一个独立 agent 跨 run、跨 harness 可以*改动*什么。你可以用它们构建，但两个这样的 agent 一旦碰同一个仓库，你仍需要 Limen。 |
| **git / VCS** | 异步、事后合并；在 merge 步调和；`git blame` 只归因到人 | **互补，且在写入的相反一侧。** Limen 是写*前*的同步预防，并恢复 git 结构上缺失的 per-agent 归因。Limen 在前端预防丢失更新；git 调和剩下的。 |
| **OS / DB 锁**（flock/fcntl、2PL、lease、Chubby、etcd、ZooKeeper） | 对文件/键的强制或建议式加锁 | **直系祖先**，被改造为 agent 感知、协议原生：对命名空间*区域*的建议式租约，经 MCP 携带，打上 agent 标签，带 TTL 自动过期，使挂死的 agent 不会让命名空间死锁。 |
| **CRDT / OT**（协同编辑、Google Docs） | 对*可收敛*数据类型做乐观的写后合并 | **同一谱系的相反端。** 它们对被设计成可交换的数据自动合并；Limen 对*任意不可收敛*状态（源代码、配置、基础设施）做写前预防，那里"强行合并"会 break build。 |
| **agent 协议**（MCP、ACP、A2A） | 工具访问（MCP）、编辑器↔单 agent 回合（ACP）、不透明的 agent 间 handoff（A2A） | Limen **骑在 MCP 上**作为接入面，并填补三者都留下的空缺：没有一个仲裁共享状态的*并发改动*或提供租约+审计层。 |
| **治理 / control plane**（其前身） | doctrine、treaty、federation、审批 | **显式抛弃。** Limen 是协调原语，不是统治者。 |
| **MCP Agent Mail**（最近的表亲） | 经 MCP 的建议式文件租约 + 身份 + Git 审计 + 异步消息 | **验证了范畴；不是同一产品。** Limen 刻意更窄更纯（三原语、类型化冲突矩阵、中介写入本身），且不是消息总线。 |

---

## 泛化维度（模型如何接触各个世界）

文件系统只是其中一个资源，这是刻意的。下表每一维都展示*同一*模型如何泛化——并表明设计中没有任何东西被焊死在代码文件上。

| 维度 | 文件系统资源（今天） | 通用模型 |
| --- | --- | --- |
| **写者** | AI coding harness + 其 sub-agent | 任何不协调的自治 agent——research、ops、computer-use、数据管线，乃至人/agent 混合 |
| **共享状态** | 一个 git 仓库 / 文件系统 | 任何可变资源的命名空间——文档、KV 存储、配置、基础设施、外部系统状态 |
| **区域**（*limen*） | 一个路径前缀 | 命名空间上的任意 selector |
| **传输** | 经 stdio 的 MCP server | 任何 agent 能请求授权的协议 |
| **本地性** | 单机、单工作区 | 谱系（Chubby、etcd、ZooKeeper）本就是分布式 |

纪律是**面向通用模型设计、同时只发一个资源**——正是 Git 从内核源码、MCP 从桌面端走过的弧线。模型指引 Limen *可能*协调什么，而不膨胀 Limen *今天是*什么：一个建议式原语、一个资源。

---

## 让 Limen 保持小的那条线

当一个待加功能存疑时，按序应用此判据：

1. 它是否要求 Limen *管着*某个 agent（启动/停止/调度/路由/监督）？→ **出界。**（原则 1）
2. 它是否引入 doctrine/策略/治理/审批机器？→ **永远出界。**
3. 它是否让 Limen 在采用尚未形成之前，就去取得或强制它并不拥有的权威（强制拦截）？→ **推迟**到 opt-in 的强制模式。
4. 它是否在协调*一个 agent 可以在共享状态里改动什么*，以建议式、带归因的方式？→ **在范围内。**

Limen 所做的一切都应能归约为：发一张租约、中介一次写入、记一条见证、或回答"谁碰了这个"。若不能，它就属于某个邻居。
