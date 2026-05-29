# Limen — 设计哲学（中文伴随版）

> 术语见 [`glossary.md`](glossary.md) · 边界与范围见 [`boundaries.zh.md`](boundaries.zh.md) · 参考文献见 [`../references.md`](../references.md)。
>
> **英文 [`philosophy.md`](philosophy.md) 为 canonical 版本**；本文是其中文伴随版，如有出入以英文为准。

Limen 协调的是**并发、自治、且会改动共享状态的 agent**。它发放一张建议式（advisory）、按边界划定、带 TTL 的**租约（lease）**，中介每一次写入并记录一条**见证（witness）**记录（字节数、SHA-256、agent 标签、时间戳），并把两者绑定到一个 per-agent 的**身份（identity）**上——以 MCP server 形态暴露（`limen_acquire` / `limen_write` / `limen_release`）。

这是 Limen 的**通用范畴（general category）**。它的第一个具体**滩头（beachhead）**是多 harness 的 AI coding 共享一个 git 仓库：异构 coding agent（Claude Code、Cursor、Codex、Gemini CLI）及其并行 sub-agent 共用一棵工作树，却没有任何一层在阻止它们互相踩踏。**coding 这个场景是一个实例，不是定义**——正如 Git 始于 Linux 内核源码、MCP 始于桌面端，但都不"等于"那个起点。

本文给出这个形态的论证。它刻意建立在**分布式系统的并发控制**与**零信任安全**之上，而不是 LLM 编排之上——因为那才是会比任何一代模型活得更久的那部分问题。

---

## 问题的形状

并发写共享状态的理论早已尘埃落定，而且有精确的命名。Berenson 等，《A Critique of ANSI SQL Isolation Levels》（SIGMOD 1995）给了我们两个名字：

- **Lost Update（丢失更新，`r1[x] … w2[x] … w1[x] … c1`）**：一个写者读取，第二个写者写入，第一个基于过期读再写，于是第二个已提交的工作**静默消失**。
- **Write Skew（写偏斜，`r1[x] … r2[y] … w1[y] … w2[x]`）**：两个写者读取了有交叠的数据，却写入**不相交**的项，合起来破坏了任何一方单独都没破坏的不变式。

多个 AI coding agent 动同一个仓库时，发生的正是这件事。同样的失败模式如今正在 agent 文献里被重新发现并冠以新名——*lost updates*、*interface breakage*、*stale partial views*——见 Chacon Sartori《The Specification Gap》（arXiv:2603.24284, 2026）；而 Cognition 的《Don't Build Multi-Agents》（Yan, 2025）从 agent 视角描述了机制：并行的行动者"看不见对方在做什么"，基于"事先未约定的冲突假设"行动。这就是 Lost Update，从内部讲出来。

关键的不对称在于：**数据库和版本控制系统是挣来的安全；agent 一样都没继承到。** SQL 事务加锁或在快照下运行并原子提交；Git 通过 index 和显式 merge 串行化。而 agent：

- **非确定性写者**——同一 prompt 每次产生不同的编辑，冲突不可复现，无法在静态层面绕开；
- **不懂加锁**——它们从没被写成"先 acquire 再写"，而是打开文件直接 `write()`；
- **彼此盲视**——由不同的人或脚本启动的独立 harness 共用一棵工作树，却没有共同的父级替它们 merge。

于是现代 agent 栈重建了一个 1995 年的危害，却丢掉了 1970s–90s 的解药。**Limen 的命题是：解药不需要重新发明，只需要移植**——把"丢失更新的预防"抬到协调层，那里写者是 agent，共享状态是一个没人上过锁的文件系统。

---

## Limen 站在哪里

Limen 的原语取自那条早已解决了协调问题的谱系，安全词汇取自那条正在适配 agent 的谱系。

- **并发控制与租约。** 租约本身是 Gray & Cheriton《Leases》（SOSP 1989）：一份带时限的授权，持有者崩溃或分区时**自动过期**——于是失败损失的是性能，不是正确性。建议式姿态来自 Burrows《Chubby》（OSDI 2006）和 POSIX `flock(2)`。最小内核、把策略组合留给边缘的立场来自 ZooKeeper（Hunt 等, USENIX ATC 2010），它坚持自己"**不是一个锁服务**"，而是协调原语。现代操作模板是 etcd 的 lease（Grant/KeepAlive/Revoke/TTL）与 Consul 的 session。
- **AI agent 的零信任。** 安全那一半来自 Anthropic《Zero Trust for AI agents》（2026），其根基是 NIST SP 800-207（2020）与最小权限原则（Saltzer & Schroeder, 1975）。Limen 把它四项要求中的三项落地成协调原语（见原则 5）。

站在这里、而不站在 prompt 编排范式上，是一个刻意的赌注：**推理是易变的，协调不是。** 模型、harness、编排时尚都会更迭；丢失更新问题已经四十岁了，会比它们都活得久。

---

## 原则

### 1. 协调共享状态，而非编排 agent

Limen 的职责止于"agent 改动共享资源"那道边界。它不启动、不停止、不调度、不路由、不监督任何东西。这条线让项目保持小、让范畴保持干净：编排拥挤且与模型耦合；而*对编排所产生的写入进行协调*几乎是空白的，且与模型无关。

一个有用的判据：如果一个功能只有在"假设 Limen 在管着这些 agent"时才说得通，它就出界了。**Limen 永远不在管。**

### 2. 建议式优先（Advisory-first）

一张 Limen 租约只与其他租约请求冲突，它在物理上并不阻止 agent 去碰文件。这是一个有充分先例的、被选定的姿态，不是被遮掩的弱点。

Burrows 在为 Chubby 选择建议式而非强制锁时把理由讲得很准（OSDI 2006）：锁"只与获取同一把锁的其他尝试冲突…… 我们拒绝强制锁，因为 Chubby 的锁常常保护的是由别的服务实现的资源，而不只是与锁关联的那个文件"。这正是 Limen 的处境——它守护一个自己并不拥有的工作区。POSIX `flock(2)` 在 OS 层说了同样的话："进程可以自由地忽略 flock() 的使用"。

更深的理由是采用率。一把"只有重写每个 harness 才能遵守"的强制锁，谁都不会遵守；一张"任何 MCP host 零定制即可调用"的建议式租约，会被合作的大多数遵守——而在这个滩头里，agent 们**本就不想**互相覆盖，合作是常态而非对抗。**没人愿意采用的保证一文不值。**

### 3. 仆从，不是统治者

赢得采用的基础设施都是描述性的，不是统治性的。Limen 加入它们。

| 它说 | "我做这个" | "我**不**做这个" |
| --- | --- | --- |
| Git | 我追踪 diff | 我管理你的代码 |
| MCP | 我规定工具调用的 wire format | 我控制你的 agent |
| OpenTelemetry | 我规定 span 格式 | 我监督你的系统 |
| OAuth | 我规定授权握手 | 我决定你是谁 |
| **Limen** | **我发租约、记见证** | **我治理你的 agent swarm** |

MCP 这样表述自己的章程：它"只聚焦于上下文交换的协议——不规定 AI 应用如何使用 LLM 或如何管理所提供的上下文"。Limen 采取完全相同的立场——一个普通的、建议式的 MCP server，不是 control plane。这也是一次纠偏：Limen 的前身曾过度伸张到"control plane / law layer / governed swarm"的语气，而这种过度伸张正是本项目要逃离的东西。

### 4. 通用原语，在窄滩头上验证

持久的范畴是**对并发自治 agent 在共享可变状态上的协调**；滩头是**经 MCP 在 git 仓库上做多 harness coding**。**持有范畴、同时只发一个窄的首个实例，是策略，不是矛盾。**

这个范畴远早于 LLM。它是**黑板（blackboard）**模式（Hearsay-II, Erman 等 1980；Nii 1986）：许多独立 agent 通过改动一块共享、分区的状态来协作。它是**元组空间（tuple space）**（Gelernter《Linda》1985），其原子析构操作 `in` 本质就是"行动前先认领一块区域的权限"，而它的格言——协调与计算正交——正是 Limen 的立场。Limen 在泛化它们时做了一个刻意的反转：Hearsay-II *集中*一个调度器来决定哪个知识源运行，而 Limen 取消了中央调度——写者通过认领区域来自协调。这就是架构层面的"仆从而非统治者"。**actor 模型**（Hewitt 1973；Agha 1986）是反衬：它通过从不共享状态来绕开整个问题。Limen 接受了 actor 拒绝的前提——今天的异构 harness *已经*共用一个仓库，无法被重写成纯 actor——所以共享状态必须被协调，而不是被许愿掉。

泛化的各个维度（写者、命名空间、区域、传输、本地性）列在 [`boundaries.zh.md`](boundaries.zh.md)。

### 5. 三个可协调的零信任原语（以及第四个为何只是部分）

Anthropic《Zero Trust for AI agents》（2026）用**两句相邻的话**勾勒了 agent 时代的转变：

> "零信任——什么都不信任、一切都验证、并假定破坏已经发生——为安全负责人提供了一个经过验证的基础。"
>
> "但这些原则需要为 agentic 系统赋予新的形态：以密码学为根的身份、按任务划分的权限、对投毒有防护的记忆，以及以自治攻击者的速度运行的防御操作。"

这四项要求中有三项直接映射到 Limen 的运行时原语，第四项在 MVP 里只是部分。

| 零信任要求 | Limen 原语 | 谱系 |
| --- | --- | --- |
| 权限**按任务划分** | **租约（Lease）**——建议式、按区域、带时限的写入授权 | 最小权限（Saltzer & Schroeder 1975）；NIST SP 800-207 原则 3（按 session、"完成任务所需的最小权限"）；Just-In-Time / Just-Enough-Access；Gray & Cheriton 1989 |
| **假定破坏**已发生 | **见证（Witness）**——中介写入的审计轨迹：每次写记录字节数 + SHA-256 + agent 标签 | NIST SP 800-207 原则 7（广泛采集遥测）；"最小化爆炸半径、用分析获得可见性" |
| 身份**以密码学为根** | **身份（Identity）**——今天是 per-agent 标签；计划上 ed25519 签名 | NIST SP 800-207 原则 4（策略以客户端身份为键） |
| **机器速度**的防御操作（agentic SOAR） | **写入时的冲突仲裁**——*部分* | agentic-SOAR 的业界用法 |

租约是把最小权限**时间化与空间化**：在时间上加括号（晚获取、自动过期），在空间上加括号（单一区域）。Saltzer & Schroeder（1975）把原则讲得很准——"系统中的每个程序、每个用户都应当以完成工作所需的最小权限集合来运行"。租约也是**受 capability 启发**的（*capability* 概念出自 Dennis & Van Horn, 1966）——但仅仅是启发：在 ed25519 签名与中介落地之前，Limen 租约不是一个不可伪造的 token，我们今天不主张 object-capability 级别的不可伪造性。

第四个原语被诚实地缩小了。Limen 确实在*获取与写入时*做实时仲裁——这是狭义协调意义上的"机器速度响应"——但 Limen **不是** SOAR 产品：没有针对对手的 检测–决策–响应 回路，没有安全事件响应。这个映射是类比，不是等同。

引用这条谱系时的姿态护栏：NIST、Microsoft、object-capability 传统全都是*强制/治理*模型。引用它们是为了词汇与谱系，**不是**为了姿态。Limen 建议式优先。

### 6. 写之前预防，而非写之后合并

并发控制有一条被充分勾勒的、从悲观到乐观的谱系：**预防**（Dijkstra 的临界区 1965；Lamport 的面包店算法 1974；Linda 的原子 `in`）→ **先推测、再提交或回滚**（软件事务内存 STM；Shavit & Touitou, PODC 1995）→ **每次写后合并**（操作转换 OT，Ellis & Gibbs 1989，Google Docs 的骨干；CRDT，Shapiro 等 2011）。

Limen 坐在悲观这一端，且是刻意的。CRDT/OT 在**无需协调**的情况下达成收敛，但只对被设计成可交换的数据类型成立——位置文本、计数器、集合。Limen 滩头的状态是源代码、配置、基础设施，两次编辑常常*不*可交换：改了一个 agent 还在调用的 API 签名，"强行合并"得到的是 build break，而不是被调和的文档。没有任何 least-upper-bound 合并或操作转换能修复一个语义的、跨文件的断裂。因为 Limen 无法像 STM 回滚内存那样回滚文件系统，它在交叠写入**发生之前**就告警，而不是事后调和。

这也是为什么 Limen **是对 git 的补充而非竞争**。git 是异步的、事后合并；Limen 是同步的、写前预防。而且 git 无法归因到 agent——`git blame` 只显示启动 harness 的那个人——Limen 的见证则记录每次写入的 agent 标签。一个在前端预防丢失更新，另一个调和剩下的。

干净地说：**CRDT/OT = 对专门设计的可收敛状态做自动的写后合并；Limen = 对任意不可收敛的共享可变状态做建议式、按区域、带时限的写前预防。**

### 7. 每一项宣称的收益都必须可证伪、可度量

Limen 给出一个实验能证伪的主张——这正是它"论文级"而非"修辞级"的原因：

> 在固定并行度 N 下，**`Par-N-Limen` 在 (wall-clock × pass@1) 上 Pareto 支配 `Par-N-Naive`**——两个维度都不更差，至少一个严格更好——同时在 lost-edit-lines、build-break-rate、归因准确率上严格占优。

三臂消融是 **Seq-1**（单 agent 串行——正确性上限与时延基线）、**Par-N-Naive**（N 个并发 agent，无协调）、**Par-N-Limen**（N 个并发 agent + 建议式租约）。主张是*固定 N 下的 Pareto 支配*，刻意**不是**"agent 越多越好"——scaling 文献表明并行在强单 agent 基线之上有递减甚至负的回报，单调主张站不住。naive 臂的代价是经验上真实的，不是稻草人：CodeCRDT（Pugachev, arXiv:2510.18893, 2025）度量并行多 agent 代码生成，发现某些任务加速、另一些减速，且语义冲突率非零。正确性指标是 pass@k（Chen 等, 2021）；更严的可靠性框架是 pass^k（k 次重复全部成功）——最锋利的假设是：Par-N-Naive 可能在 pass@1 上*打平*，却因非确定性碰撞在 pass^k 上*崩塌*，而 Limen 应当压缩这道缝隙。

完整设计、指标、可比工作与效度威胁在 [`related-work.md`](related-work.md) 里搭好骨架（英文）。

---

## 我们诚实持有的张力

只列优点的哲学是营销。这些是真实的棱角。

**建议式是弱保证，绕过是真实存在的。** 一个无视 `limen_acquire`、直接写文件的 agent 不会被拦住——Limen 发放，不在内核强制。四点让这仍是对的权衡：(i) 这与 Chubby、`flock` 刻意做的、并在规模上验证过的权衡相同；(ii) 见证轨迹把"已预防"转成"已归因"——把"假定破坏"落到实处，于是一次绕过至少在取证上可重建，远超 `git blame`；(iii) 滩头本质合作；(iv) 在无法被强迫参与的 harness 之间，建议式是*唯一*能达成采用的姿态。MVP 不能夸大：在 ed25519 签名加中介落地之前，它是受 capability 启发、而非 capability 强制。

**前缀租约可能太粗。** MVP 的区域匹配只是字面路径/目录前缀（暂无 glob）：`src/` 的租约会与其下任何写入冲突。这会串行化本不会真正碰撞的 agent（假冲突），而 `src/` 的租约对 `src/api/` 与 `src/caller/` 之间的 Write Skew 一无所知。粗粒度为 MVP 换来一个简单、快、可审计的冲突检查；更细的区域（glob、字节范围、语义区域）是后续工作，而见证轨迹是租约看不见的 skew 情形的兜底。

**"不要造多 agent 系统"是最强的反论——而 Limen 同意它。** Cognition 主张并行 sub-agent 脆弱、正确的默认是单线性 agent。若如此，为何要做多 agent 协调层？因为 **Limen 不鼓吹多 agent；它让已经在发生的多 agent 更安全。** 它对"你*该不该*跑并发 agent"保持中立，并观察到人们*已经在跑*——多 harness、多人、sub-agent fan-out。连 Anthropic 自己的多 agent research 系统都指出"sub-agent 之间无法相互协调"、且共享上下文、写密集的 coding"如今并不适合多 agent 系统"。Cognition 的解药（收敛到单 agent，或在应用内做更丰富的上下文工程）够不到那种*独立* harness 共用一个仓库、却没有共同父级的情形。Cognition 谈架构；Limen 为无论如何都存在的并发提供安全。

**有一个最近的表亲，我们点名它。** "几乎没人"协调跨独立 harness 的并发写入——这并不成立。**MCP Agent Mail**（Dicklesworthstone, 2025）几乎正好占据这个滩头：经 MCP 为异构 coding harness 提供建议式、带 TTL 的文件预留、per-agent 身份、以及 Git 支撑的审计轨迹。它的存在**验证了范畴**，而非否定 Limen。Limen 可辩护的差异在于窄与纯——三个原语、按 path-pattern 区域、带明确类型化的冲突矩阵，且*中介写入本身*（`limen_write` 记录字节 + SHA-256 + agent 标签）而非仅预留再依赖 hook，并刻意不在上面叠一层消息/邮箱模型。因此诚实的主张是**泛化的范畴 + 一个严格的实验**，而非"首个面向 coding agent 的建议式文件租约"。

---

## 一句话

Limen 是**对任意共享可变状态做建议式、按区域、带时限的写前预防**——把五十年的丢失更新解药（租约、建议式锁、互斥、黑板/元组空间协调）移植到非确定性、不懂锁、彼此盲视的 agent 上，并入零信任的三件套（身份 + 按任务的租约 + 见证审计），以仆从而非统治者的姿态，在一个锋利的 coding 滩头上发布，服务于一个持久的通用范畴。

Limen *不是*什么，见 [`boundaries.zh.md`](boundaries.zh.md)。
