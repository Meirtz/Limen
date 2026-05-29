# Limen — 术语表（中文伴随版）

> 本术语表是仓库的 canonical 词汇；其他文档复用这些术语，不另行定义。
>
> **英文 [`glossary.md`](glossary.md) 为 canonical 版本**；本文是其中文伴随版，如有出入以英文为准。伴随文档：[`philosophy.zh.md`](philosophy.zh.md) · [`boundaries.zh.md`](boundaries.zh.md) · [`../references.md`](../references.md)。

Limen 的词汇小而**通用**。这些术语描述的是对*任意*共享可变状态的协调；**文件系统**是今天唯一实现的资源，列在右栏作为一个 worked example。每个运行时术语都对应 `crates/limen/` 里的真实类型。

## 核心词汇

| 术语 | 定义（通用） | 文件系统资源（今天） |
| --- | --- | --- |
| **Limen** | 一个协调守护进程，以 MCP server 暴露，对某命名空间的区域发放建议式租约、并记录中介变更的见证审计，让并发 agent 不撞车。拉丁语 *limen* = *门槛*。 | — |
| **agent** | 一个改动共享状态的自治写者——coding harness、sub-agent、research/ops/computer-use agent、管线。由它*改动什么、在什么授权下*定义，而非由人格定义。Limen 既不构建也不运行 agent。 | 一个 Claude Code / Cursor / Codex session |
| **命名空间（namespace）** | 被协调的可变资源的可寻址空间。 | 工作区的文件 |
| **资源（resource）** | 一个可插拔后端，赋予命名空间含义：区域如何比较、中介变更如何施加。v0.1 恰好实现一种。 | 文件系统 |
| **区域（region，一个 *limen*）** | 租约覆盖的命名空间的一块——agent 跨越以改动的门槛。 | 字面路径或目录前缀（`src/auth/`） |
| **身份（identity）** | 谁在请求。默认明文标签；agent 可 `register` 一个 ed25519 密钥并为每次 acquire 签名，使身份从*被声称*升级为*密码学定根*。可选、向后兼容。 | `agent_label`，如 `claude-code:sess-A` |
| **租约（lease）** | 一份建议式、按区域、带时限的授权，允许在某区域以某意图行动。核心原语。持有者崩溃/挂死时租约自动过期（Gray & Cheriton 1989），故不会让命名空间死锁。 | `store::Lease` |
| **意图（intent）** | 租约持有者想做什么：`read`、`write`、`propose`。决定冲突行为。 | `store::Intent` |
| **TTL** | 租约存活时长（默认 5 分钟）。过期后不再冲突；`acquire` 在检查冲突前先清掉过期租约。 | `DEFAULT_LEASE_TTL_MS` |
| **租约状态** | `active`、`released`、`expired`。 | `store::LeaseState` |
| **冲突（conflict）** | 两张租约无法共存：区域交叠**且**意图相克。冲突的 acquire 被拒（先到先得 + TTL）。 | `acquire_lease` |
| **见证（witness）** | 一次中介变更的留痕证据：目标、大小、内容哈希、时间戳、归属租约（即 agent）。审计半边——把"假定破坏"落到实处。 | `store::WriteRecord` |
| **归因（attribution）** | 通过把变更接到其租约的身份，回答"谁、何时、在哪张租约下改了它"。恢复 `git blame` 看不到的。 | `attribute_path` |
| **中介变更（mediated change）** | *经* Limen、在持有的租约下完成的变更：Limen 校验租约、施加到资源、记录见证。 | `record_write`（一次文件写入） |

## 冲突矩阵

两张租约冲突，当且仅当区域交叠**且**意图相克：

| | write | read | propose |
| --- | --- | --- | --- |
| **write** | 冲突 | 冲突（读者让位） | 可 |
| **read** | 冲突 | 可 | 可 |
| **propose** | 可 | 可 | 可 |

- **write × write**——协调层对 *Lost Update* 的预防。
- **write × read**——区域的读者向写者让位（对 *Write Skew* 的部分防御）。
- **read × read**——永不冲突；读可并行。
- **propose**——非阻塞建议式声明（"我打算碰这块"）；与任何意图都不冲突。

## 姿态与范围术语

| 术语 | 定义 |
| --- | --- |
| **建议式（advisory）** | 一张租约只与*其他租约请求*冲突，并不物理阻止变更。Chubby 与 POSIX `flock` 的姿态。Limen 发放并见证，不强制。强制（opt-in）是后续工作。 |
| **仆从，不是统治者** | Limen 的立场："我发租约、记见证；我不治理你的 agent。"与 Git、MCP、OAuth、OpenTelemetry 一致——描述性基础设施，不是 control plane。 |
| **通用模型（general model）** | Limen 的定义：*对并发自治 agent 在共享可变状态上的协调*，凭 lease + witness + identity over 命名空间的区域。根在并发控制与零信任，不在 LLM 编排。模型是产品，资源是它接触世界的方式。 |
| **变更前预防（prevention-before-change）** | Limen 在并发谱系上的位置：在租约获取时、交叠变更*发生之前*告警——区别于*写后合并*（CRDT/OT）与先推测后提交（STM）。 |
| **零信任三件套** | Limen 落地的三个可协调零信任原语：按任务的 scope（→租约）、审计/假定破坏（→见证）、密码学定根的身份（→agent 身份）。第四项 agentic SOAR 仅部分（请求时的冲突仲裁）。 |

## MCP 接入面

Limen 以 MCP server（stdio JSON-RPC 2.0）暴露，恰好三个 tool。参数承载通用含义；文件系统资源给它们具体形态（region 是路径/前缀，target 是路径）：

| Tool | 作用 | 返回 |
| --- | --- | --- |
| `limen_acquire` | 在某区域以某意图获取租约 | `lease_id`、`expires_at`——或 conflict 错误 |
| `limen_write` | 在持有的租约下对某目标施加中介变更 | `content_hash`、`bytes_written` |
| `limen_release` | 释放持有的租约 | `released: bool` |
| `limen_renew` | 延长持有租约的 TTL（keepalive） | `expires_at` |

## 刻意退役的术语（**非** Limen 词汇）

Limen 是一个大得多的项目（Crawfish）的继任者。下列术语是那个项目的概念膨胀，**不**属于 Limen。若在未迁移的文档里见到它们，视那些文档为陈旧。

`control plane` · `governed swarm` / `swarm` · `doctrine pack` · `jurisdiction class` · `treaty` · `federation pack` · `evidence bundle` · `oversight checkpoint` · `encounter` / `encounter policy` · `consent grant` · `capability lease`（Crawfish 的重型构造） · `continuity mode` · `degraded profile` · `verify_loop` · `execution strategy` · `scorecard` / `evaluation spine` · `review queue` · `alert rule` · `remote evidence` / `remote follow-up` · `agent plane` / `harness plane`（作为 control-plane 概念）。

Limen 只保留模型所需的：命名空间、区域、资源、身份、意图、租约、见证、冲突矩阵。

## 命名纪律

- 复用本文件里的术语，别造同义词。
- 保持词汇**通用**——别让文件系统特有的词（path、file）渗进核心模型；它们只属于"文件系统资源"那一栏。
- 除非 `crates/limen/` 里有真实类型支撑，否则不引入运行时术语。
- 优先用谱系的词（lease、advisory、region、witness、最小权限），而非自创词——Limen 的可信度来自站在并发控制与零信任之上，不来自新词汇。
