# Limen — 术语表（中文伴随版）

> 本术语表是仓库的 canonical 词汇；其他文档复用这些术语，不另行定义。
>
> **英文 [`glossary.md`](glossary.md) 为 canonical 版本**；本文是其中文伴随版，如有出入以英文为准。伴随文档：[`philosophy.zh.md`](philosophy.zh.md) · [`boundaries.zh.md`](boundaries.zh.md) · [`../references.md`](../references.md)。

Limen 的词汇刻意很小。**每个运行时术语都对应实现里的真实类型**——代码没有的东西，Limen 不造词。代码位置指向 `crates/limen/`。

## 核心词汇

| 术语 | 定义 | 代码位置 |
| --- | --- | --- |
| **Limen** | 一个 workspace 协调守护进程：单个小 Rust crate，以 MCP server 暴露，发放建议式租约、记录见证写入，让并发 agent 不撞车。拉丁语 *limen* = *门槛*。 | `crates/limen` |
| **agent** | 一个改动共享状态的自治写者——coding harness、sub-agent、管线，任何会拿租约并写入的东西。agent 由它*改动什么、在什么授权下*定义，而非由人格或聊天行为定义。Limen 既不构建也不运行 agent。 | — |
| **身份（identity / agent label）** | 谁在请求。今天是明文标签（如 `claude-code:sess-A`）；计划 ed25519 签名身份。在此之前，身份是*被声称*的，而非密码学证明的。 | `agent_label` 字段 |
| **共享状态（shared state）** | Limen 协调的可变资源命名空间。滩头里是一棵 git 工作树/文件系统；通用范畴里是任意可变资源命名空间（文档、KV、配置、基础设施）。 | 文件系统（滩头） |
| **边界（boundary，一个 *limen*）** | 租约覆盖的命名空间区域——agent 跨越以改动的门槛。MVP 里是字面路径或目录前缀（`src/auth/`）；暂无 glob。 | `path_pattern` 字段；`patterns_overlap`、`path_in_pattern` |
| **租约（lease）** | 一份建议式、按边界、带时限的授权，允许在某区域以某意图行动。核心原语。持有者崩溃/挂死时租约自动过期（Gray & Cheriton 1989），故不会让命名空间死锁。 | `store::Lease` |
| **意图（intent）** | 租约持有者想做什么：`read`、`write`、`propose`。决定冲突行为。 | `store::Intent` |
| **TTL** | 租约存活时长（毫秒，默认 5 分钟）。过期后变 `expired`、不再冲突；`acquire` 在检查冲突前先把过期租约清掉。 | `DEFAULT_LEASE_TTL_MS` |
| **租约状态** | `active`（持有）、`released`（显式释放）、`expired`（TTL 到期）。 | `store::LeaseState` |
| **冲突（conflict）** | 两张租约无法共存的条件：边界交叠（前缀包含）**且**意图相克。与某 active 租约冲突的新 acquire 被拒（先到先得 + TTL）。 | `acquire_lease` |
| **见证（witness）** | 一次中介写入的留痕证据：路径、写入字节数、SHA-256 内容哈希、时间戳、归属租约（即 agent）。系统的审计半边——把"假定破坏"落到实处。 | `store::WriteRecord` |
| **归因（attribution）** | 通过把写入接到其租约的 agent 标签，回答"谁、何时、在哪张租约下改了这个路径"。恢复 `git blame` 看不到的（它只见人）。 | `attribute_path` |
| **中介写入（mediated write）** | *经* `limen_write`、在持有的租约下完成的写入：Limen 校验租约、执行写入、记录见证。 | `record_write` |

## 冲突矩阵

两张租约冲突，当且仅当边界交叠**且**意图相克：

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
| **建议式（advisory）** | 一张租约只与*其他租约请求*冲突，并不物理阻止 agent 写入。Chubby 与 POSIX `flock` 的姿态。Limen 发放并见证，不在内核强制。强制（opt-in）是后续工作。 |
| **仆从，不是统治者** | Limen 的立场："我发租约、记见证；我不治理你的 agent。"与 Git、MCP、OAuth、OpenTelemetry 一致——描述性基础设施，不是 control plane。 |
| **通用范畴（general category）** | Limen 的持久定义：*对并发自治 agent 在共享可变状态上的协调*，凭建议式租约 + 见证 + 身份。根在并发控制与零信任，不在 LLM 编排。 |
| **滩头（beachhead）** | 范畴的首个具体实例：经 MCP 在 git 仓库上做多 harness coding。是*实例*，不是定义。 |
| **写前预防（prevention-before-write）** | Limen 在并发谱系上的位置：在租约获取时、交叠写入*发生之前*告警——区别于*写后合并*（CRDT/OT）与先推测后提交（STM）。 |
| **零信任三件套** | Limen 落地的三个可协调零信任原语：按任务的 scope（→租约）、审计/假定破坏（→见证）、密码学定根的身份（→agent 身份）。第四项 agentic SOAR 仅部分（写入时冲突仲裁）。 |

## MCP 接入面

Limen 以 MCP server（stdio JSON-RPC 2.0）暴露，恰好三个 tool：

| Tool | 作用 | 返回 |
| --- | --- | --- |
| `limen_acquire` | 在某边界以某意图获取租约（`path_pattern`、`intent`、`agent_label`、可选 `ttl_ms`） | `lease_id`、`expires_at`——或 conflict 错误 |
| `limen_write` | 在持有的租约下做中介写入（`lease_id`、`path`、`content`）；path 须落在租约边界内 | `content_hash`、`bytes_written` |
| `limen_release` | 释放持有的租约（`lease_id`） | `released: bool` |

## 刻意退役的术语（**非** Limen 词汇）

Limen 是一个大得多的项目（Crawfish）的继任者。下列术语是那个项目的概念膨胀，**不**属于 Limen。若在未迁移的文档里见到它们，视那些文档为陈旧。

`control plane` · `governed swarm` / `swarm` · `doctrine pack` · `jurisdiction class` · `treaty` · `federation pack` · `evidence bundle` · `oversight checkpoint` · `encounter` / `encounter policy` · `consent grant` · `capability lease`（Crawfish 的重型构造） · `continuity mode` · `degraded profile` · `verify_loop` · `execution strategy` · `scorecard` / `evaluation spine` · `review queue` · `alert rule` · `remote evidence` / `remote follow-up` · `agent plane` / `harness plane`（作为 control-plane 概念）。

Limen 只保留代码真正在做的：租约、见证、身份、意图、边界、冲突矩阵。

## 命名纪律

- 复用本文件里的术语，别造同义词。
- 除非 `crates/limen/` 里有真实类型支撑，否则不引入运行时术语。
- 优先用谱系的词（lease、advisory、region、witness、最小权限），而非自创词——Limen 的可信度来自站在并发控制与零信任之上，不来自新词汇。
