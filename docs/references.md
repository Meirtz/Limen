# Limen — 参考资料索引（Annotated References）

> **用途**：归档 Limen 设计与 research 用到的所有外部参考。
> **为什么单独存**：PRD 刻意不 cite 这些（保持干净），但它们是设计决策的依据、related work 的来源、onboarding 的索引。**从 PRD 里删 ≠ 从项目里丢。**
> **谱系定位**：Limen 锚定在**分布式系统 / 并发控制**谱系，而非"agent 框架"谱系。本索引按此重排。
> **核实说明**：2026-05-29 经一次 6 线并行文献扫描 + 对抗式事实核查（57 个来源、14 confirmed / 4 refuted）扩充。被驳回的常见误引以 ⚠️ 标注更正；标 ★ 者为本次核实过的条目。论文引用以本节的更正措辞为准。

图例：⭐ 核心锚点 · 📄 论文 · 🔧 项目/工具 · 📝 博客/随笔 · 🧭 标准/协议 · ★ 已核实 · ⚠️ 误引更正

---

## 0. 直接锚点（Primary anchor）

PRD 中**唯一保留**的 cite。Limen 是它所倡导范式中"可协调部分"的一个已能跑的开源实现。

- ⭐★ **Anthropic — Zero Trust for AI Agents**（claude.com blog，**2026-05-27**）— identity / per-task scoping / audit / agentic SOAR 四原语。Limen 的 lease=scoping、witness=audit、agent label(未来 ed25519)=identity 直接对应；第四项 SOAR 仅部分（写入时冲突仲裁）。本次重构对话的核心输入。
  <https://claude.com/blog/zero-trust-for-ai-agents>
  > ⚠️ **更正**：该文核心是**两句相邻的话**，不是一句拼接句。逐字分别引用：「Zero Trust—trust nothing, verify everything, and assume breach has already occurred—gives security leaders a proven foundation to address this.」与「But the principles need new shape for agentic systems: identities that are cryptographically rooted, permissions scoped per task, memory protected against poisoning, and defensive operations that run at the speed of autonomous attackers.」**不要**把「a proven foundation … identities that are cryptographically rooted」拼成一句。其 gated eBook 三档是 **Foundation / Enterprise / Advanced**（非 "…Optimized"）。

---

## 1. 真正谱系：租约与并发控制 ★核心

> 这是 Limen 区别于其前身的**关键认知**：Limen 不是 agent 框架，是一个 **advisory lease + 并发控制**系统。思想根在分布式系统几十年的积累里，不在 LLM 编排。写 related work、设计 lease 语义时优先读这一节。

- 📄★ **Gray & Cheriton (1989) — "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency"**（SOSP 1989, pp. 202–210）— **lease 机制奠基论文**。"带 TTL、可过期的授权"出自这里：持有者崩溃则租约自动过期 → 失败损失性能而非正确性。这正是 Limen 租约必须带 TTL 的原因。<https://dl.acm.org/doi/10.1145/74850.74870>
- 📄★ **Burrows (2006) — "The Chubby Lock Service for Loosely-Coupled Distributed Systems"**（OSDI 2006）— Limen "建议式优先"姿态的**最强先例**。Burrows 显式拒绝强制锁，因为「Chubby 锁常保护由别的服务实现的资源，而非只是与锁关联的那个文件」——正是 Limen 守护一个自己不拥有的工作区的处境。其 **sequencer**（锁名+模式+generation）预演了 Limen 的见证/归因。<https://research.google/pubs/the-chubby-lock-service-for-loosely-coupled-distributed-systems/>
- 📄★ **Hunt et al. (2010) — "ZooKeeper: Wait-free Coordination for Internet-scale Systems"**（USENIX ATC 2010）— 最锋利地印证 Limen 架构：ZooKeeper 自称「**不是锁服务**」，而是最小协调原语，锁是客户端 recipe。写线性化（A-linearizable）、读可陈旧；ephemeral znode（session 死则删）= lease 的 liveness 思想。<https://www.usenix.org/conference/usenix-atc-10/zookeeper-wait-free-coordination-internet-scale-systems>
- 📄★ **Berenson, Bernstein, Gray, Melton, O'Neil, O'Neil (1995) — "A Critique of ANSI SQL Isolation Levels"**（SIGMOD 1995, pp. 1–10）— 给 Limen 精确词汇：**Lost Update (P4)** `r1[x]…w2[x]…w1[x]…c1` = "agent 互相静默覆盖"，由 write×write 预防；**Write Skew (A5B)** `r1[x]…r2[y]…w1[y]…w2[x]` = 区域租约单独防不住的跨区域情形，正是 write×read 规则与见证轨迹的理由。<https://arxiv.org/abs/cs/0701157>
- 📄★ **Kung & Robinson (1981) — "On Optimistic Methods for Concurrency Control"**（ACM TODS 6(2), pp. 213–226）— 乐观 vs 悲观轴。Limen = **悲观意图 + 乐观执行**（声明区域租约是悲观，建议式 + 事后见证归因是乐观）；`propose`（永不冲突）是纯非阻塞、validate-later 声明。<https://dl.acm.org/doi/10.1145/319566.319567>
- 📄★ **Bernstein & Goodman (1983) — "Multiversion Concurrency Control — Theory and Algorithms"**（ACM TODS 8(4), pp. 465–483）— 界定 MVCC vs 2PL 的设计空间。底层（git）已是多版本存储，Snapshot Isolation 消除 lost update 但**仍允许 write skew** → 即便完美版本化也不让 Limen 多余；Limen 在冲突前协调，git 在冲突后调和。<https://dl.acm.org/doi/10.1145/319996.319998>
- 📄★ **Dijkstra (1965) — "Solution of a Problem in Concurrent Programming Control"**（CACM 8(9):569）— "写前预防"传统的历史根。临界区是 Limen 区域/边界（limen）的概念祖先；write×write 是互斥的建议式、按区域松弛版。<https://dl.acm.org/doi/10.1145/365559.365617>
- 📄★ **Lamport (1974) — "A New Solution of Dijkstra's Concurrent Programming Problem"（Bakery Algorithm）**（CACM 17(8):453–455）— 从极弱假设构造稳健协调（即便读与写交叠返回任意值仍正确）——与 Limen 那个更不可靠的"内存"（被可能无视协议的 agent 改动的文件系统）直接相关；面包店的有序取号公平性是争用区域排队的模型。<https://dl.acm.org/doi/10.1145/361082.361093>
- 🔧★ **etcd Lease API**（Grant / Revoke / KeepAlive / TimeToLive）— 现代 lease API 的参考实现：每个 key 至多挂一个 lease，漏 keepAlive 即过期；Grant/KeepAlive/Revoke ≈ Limen 的 acquire/renew/release。差别：etcd 过期删 key，Limen 只释放对一个不拥有的区域的建议式占用。<https://etcd.io/docs/v3.5/learning/api/>
- 🔧★ **HashiCorp Consul — Sessions / 分布式锁** — 完整的"lease + identity + 健康联动 liveness"包：session =（identity + TTL + lock-delay + behavior），TTL 是失效下界，持有者失败自动释放；"release-not-delete" 正是 Limen 该镜像的仆从姿态。<https://developer.hashicorp.com/consul/docs/automate/session>
- 🧭★ **POSIX advisory locking — `flock(2)` / `fcntl(2)`** — OS 级证明建议式、靠合作的锁是合法且广泛部署的模型：「flock() 只放建议式锁……进程可自由忽略 flock()」。LOCK_SH/LOCK_EX 正是 Limen 的 read/write 矩阵抬到命名空间区域。⚠️ 注意：Linux ≥5.5 在 SMB 上 flock 以 byte-range 模拟、实际变成非建议式——"advisory 是姿态而非硬保证"的佐证。<https://man7.org/linux/man-pages/man2/flock.2.html>

---

## 2. 零信任 & capability 安全 ★新增

> Limen 安全那一半的谱系。引用它们是为**词汇与谱系**，不是为姿态——NIST/Microsoft/ocap 都是强制/治理模型，Limen 建议式优先。

- 📄★ **NIST SP 800-207 (2020) — "Zero Trust Architecture"**（Rose, Borchert, Mitchell, Connelly；DOI 10.6028/NIST.SP.800-207）— 标准级零信任定义。原则 3（按 session、"完成任务所需最小权限"）→ Limen 的 lease；原则 6（动态、持续重评授权）→ 带时限的重新获取；原则 7（尽量采集信息）→ 见证轨迹。<https://csrc.nist.gov/pubs/sp/800/207/final>
- 📄★ **Saltzer & Schroeder (1975) — "The Protection of Information in Computer Systems"**（Proc. IEEE 63(9):1278–1308）— 最小权限的权威出处。⚠️ **逐字**：「Every program and every user of the system should operate using the least set of privileges necessary to complete the job.」——是「every **user**」（非 "privileged user"）、「least **set of privileges**」（非 "least amount of privilege"）；勿用流行的 paraphrase。租约 = 在时间（晚获取、自动过期）与空间（单区域）上加括号的最小权限。<https://ieeexplore.ieee.org/document/1451869>
- 📄★ **Dennis & Van Horn (1966) — "Programming Semantics for Multiprogrammed Computations"**（CACM 9(3):143–155）— **capability 概念的出处**。Limen 租约 = 一个被衰减的、带时限、按区域的 capability。⚠️ **勿误引**：本文提出 *capability* 概念与 "C-list"，**不是** "object-capability model"，也没有 "unforgeable token of authority" 这句——后者是 Mark Miller 等约 2003–2006 的表述。今天的 Limen 租约是 capability-**inspired**，非 capability-**enforced**。<https://dl.acm.org/doi/10.1145/365230.365252>
- 🧭 **Object-capability model**（Miller 等，约 2006）— 现代 ocap 框架（attenuation、no ambient authority、"communicable, unforgeable token of authority"）；未来 ed25519 签名租约会逼近它。（二手；"unforgeable token" 与 "object-capability" 术语属此处而非 1966。）<https://en.wikipedia.org/wiki/Object-capability_model>
- 🧭 **Microsoft — Zero Trust 概览（三原则 + JIT/JEA）** — "assume breach" 措辞与 Just-In-Time / Just-Enough-Access 的最佳操作来源：JIT = 带时限租约，JEA = 按区域；"最小化爆炸半径 / 用分析获得可见性" = 见证轨迹。（页面常更新，引用带访问日期。）<https://learn.microsoft.com/en-us/security/zero-trust/zero-trust-overview>
- 📝 **CrowdStrike — Charlotte Agentic SOAR**（2025-11-05）— 定义 Anthropic 所引的 "agentic SOAR"：「能实时推理、决策、行动的智能体」是 Limen 写入时冲突仲裁的机器速度类比。（厂商/营销来源，作业界用法示例，非中立定义；**Limen 不是 SOAR 产品**。）<https://www.crowdstrike.com/en-us/blog/crowdstrike-leads-new-evolution-of-security-automation-with-charlotte-agentic-soar/>

---

## 3. 共享状态协调：黑板 / 元组空间 / 互斥 / CRDT-OT ★新增

> 支撑 Limen 的**通用范畴**（不只 coding）。也给出"预防 vs 合并"谱系的两端。

- 📄★ **Erman, Hayes-Roth, Lesser, Reddy (1980) — "The Hearsay-II Speech-Understanding System"**（ACM Computing Surveys 12(2):213–253）— Limen 通用范畴的直系祖先：多个自治知识源通过改动一块共享、分区的黑板协作。差别：Hearsay-II **集中**一个控制/调度组件（Limen 拒绝的统治者姿态）；Limen 把它换成写者自持的建议式区域租约。<https://dl.acm.org/doi/10.1145/356810.356816>
- 📄★ **Nii (1986) — "Blackboard Systems"**（AI Magazine 7(2)/7(3)）— 黑板模型的持久定义（blackboard + knowledge sources + control）；其 "levels/regions" 映射到 Limen 的"命名空间区域"，而 Limen 把 control 去中心化为建议式区域租约。<https://ojs.aaai.org/index.php/aimagazine/article/view/537>
- 📄★ **Gelernter (1985) — "Generative Communication in Linda"**（ACM TOPLAS 7(1):80–112）— **最近的古典类比**：原子析构 `in`（行动前认领一个 tuple 的权限）≈ `limen_acquire`；"协调与计算正交" = Limen 的仆从姿态。差别：Linda 的 `in` 是强制原子，Limen 的 lease 是建议式带时限。<https://dl.acm.org/doi/10.1145/2363.2433>
- 📄★ **Hewitt, Bishop, Steiger (1973) — "A Universal Modular ACTOR Formalism for AI"**（IJCAI-73）— **反衬**：actor 通过从不共享状态、只用消息协调来绕开问题；Limen 接受相反前提（今天的异构 harness 已共用一个仓库，无法重写为纯 actor）。<https://www.ijcai.org/Proceedings/73/Papers/027B.pdf>
- 📄★ **Agha (1986) — "Actors: A Model of Concurrent Computation in Distributed Systems"**（MIT Press）— 消息传递 vs 共享状态轴的权威锚点；二者互补（actor 系统触及真正共享的外部资源时仍需协调）。<https://direct.mit.edu/books/monograph/4794/>
- 📄★ **Shavit & Touitou (1995) — "Software Transactional Memory"**（PODC 1995, pp. 204–213）— 谱系中点：写者乐观推进、原子提交/回滚。Limen 因无法回滚文件系统而在写前告警。<https://dl.acm.org/doi/10.1145/224964.224987>
- 📄★ **Shapiro, Preguiça, Baquero, Zawirski (2011) — "Conflict-free Replicated Data Types"**（SSS 2011 / INRIA RR-7687）— 对照的头条：CRDT 无需协调达成强最终一致，但只对被设计成可交换/可收敛的数据类型成立；Limen 面向任意不可交换的共享可变状态（源代码、配置、基础设施），"强行合并"得到坏 build → 写前预防而非事后合并。<https://inria.hal.science/inria-00609399v1>
- 📄★ **Ellis & Gibbs (1989) — "Concurrency Control in Groupware Systems"**（SIGMOD 1989；SIGMOD Record 18(2):399–407）— 乐观合并对照的第二支柱：操作转换 OT（dOPT，Google Docs 骨干）让人人立即写、靠转换操作调和，适合位置文本但修不了语义跨文件断裂（改了别人还在调用的 API 签名）——故 Limen 写前协调。<https://dl.acm.org/doi/10.1145/67544.66963>

---

## 4. 协议标准（Protocol standards）★核实

- 🧭⭐★ **Model Context Protocol (MCP)** — **Limen 的接入面**。host/client/server、tools/resources/prompts、stdio vs SSE/HTTP（当前 spec 版本 2025-11-25）。其章程：「只聚焦上下文交换协议——不规定 AI 应用如何用 LLM 或管理上下文」。关键：MCP 是 1:1 单连接，**没有** lease、跨 client 冲突检测、多 agent 审计——正是 Limen 补的协调语义。<https://modelcontextprotocol.io/docs/learn/architecture> · 版本史（2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25）：<https://modelcontextprotocol.io/specification/versioning>
- 🧭★ **Agent Client Protocol (ACP)**（Zed Industries）— 与 MCP 相反方向：编辑器 ↔ 单个 coding agent（prompt 回合、diff、逐操作授权），无 lease、无跨 agent 冲突检测。ACP 驱动的 agent 正是 Limen 要协调的并发写者之一。<https://agentclientprotocol.com> · <https://github.com/agentclientprotocol/agent-client-protocol>
- 🧭★ **A2A (Agent2Agent)**（Google → Linux Foundation，2025-06-23 立项；v1.0）— 跨边界远程 agent 委托。**Limen 的 gap thesis 的最强外部佐证**：A2A 让 agent 协作「无需访问彼此的内部状态、记忆或工具」，即**按设计把共享状态抽象掉**，把并发共享可变状态留给 Limen。**Limen MVP 不涉及**（非目标）。<https://a2a-protocol.org/latest/specification/> · <https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project>

---

## 5. Harness / 同类与最近表亲（Limen 要协调共存的对象）

> 这些不是竞争对手——是 Limen 的**服务对象**。Limen 的价值正比于同时在用的 harness 数量。**MCP Agent Mail 是最近的 prior art，必须诚实对标。**

- 🔧★ **MCP Agent Mail**（Jeffrey Emanuel / Dicklesworthstone，2025；FastMCP + Git + SQLite）— **最近的表亲 / prior art**：经 MCP 为异构 coding harness 提供建议式带 TTL 的文件预留 + per-agent 身份 + Git 审计轨迹，建议式冲突模型（交叠预留报告但仍授予），可选 pre-commit guard。其存在**验证范畴**而非否定 Limen。Limen 差异：三原语 over path-pattern 区域 + 类型化冲突矩阵 + **中介写入本身**（记字节+SHA-256+标签），且刻意不是消息总线。⚠️ 其精确冲突行为读自 README，发布对照表前需回源确认。<https://github.com/Dicklesworthstone/mcp_agent_mail>
- 🔧 **Claude Code** — 首要接入目标（MCP 原生、sub-agent fan-out 内置）。<https://docs.anthropic.com/en/docs/claude-code/overview> · 并行 worktree：<https://code.claude.com/docs/en/worktrees> · sub-agent：<https://code.claude.com/docs/en/sub-agents>
- 🔧 **Cursor** — 北极星场景核心一员（闭源，instrument 较难，实验中后置）。<https://cursor.com>
- 🔧 **OpenAI Codex** — 北极星场景核心一员。<https://openai.com/codex/>
- 🔧 **Gemini CLI** — 候选接入 harness。<https://github.com/google-gemini/gemini-cli>
- 🔧 **GitHub Copilot CLI** — 候选接入 harness。<https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-copilot-cli>
- 🔧 **aider** — OSS harness，实验里"可完全 instrument 的第三方"首选。<https://aider.chat>
- 🔧 **cline** — OSS harness 候选。<https://github.com/cline/cline>

---

## 6. Agent 框架 & multi-agent 现实（Related work，非竞品）★核实

> 它们解决"如何 build / orchestrate 一个 agent"，且协调的是**单应用权限内的上下文**；Limen 解决"如何让多个已存在的独立 agent 在共享外部状态上共存"。正交。

- 📝★ **Anthropic — "Building Effective Agents"**（Schluntz & Zhang，2024-12-19）— 定义了 Limen 显式**不在其中**的编排词汇（prompt chaining / routing / parallelization / orchestrator-workers / evaluator-optimizer）；全在单应用控制权内，从不处理跨独立 agent 的并发写。<https://www.anthropic.com/research/building-effective-agents>
- 📝★ **Anthropic — "How we built our multi-agent research system"**（2025-06-13）— **gap 的最强第一方供述**：research 适合 multi-agent 因其读密集；而共享上下文、写密集的 coding 被标为「如今并不适合 multi-agent 系统」，且 sub-agent「无法相互协调」。Limen 正为此情形提供外部建议式 lease+audit 边界。⚠️ 文中 90.2% / ~15× token 等数字与逐字引用，引用前回源核对。<https://www.anthropic.com/engineering/multi-agent-research-system>
- 📝★ **Cognition / Walden Yan — "Don't Build Multi-Agents"**（2025-06-12）— 失败模式（并发行动者基于互不可见的冲突假设行动）= lost-update problem；Cognition 的解药是避免并发，Limen 则接受独立并发 agent 并把其冲突写入变成显式的、见证过的 lease 声明。⚠️ 后续有一篇 Cognition 文章部分回调此立场。<https://cognition.ai/blog/dont-build-multi-agents>
- 📝★ **Harrison Chase / LangChain — "How and when to build multi-agent systems"**（2025-06-16）— 第三方独立佐证 Limen 的冲突语义：「Read actions are inherently more parallelizable than write actions」，冲突写比冲突读更糟——正是 Limen 编码的（read×read 可，write×write / write×read 冲突）。⚠️ 此引用承重，复现前逐字核对。<https://www.langchain.com/blog/how-and-when-to-build-multi-agent-systems>
- 🔧 **OpenAI Swarm**（2024，已被取代）— "handoff" 协调习语的出处：协调 = 在单次 run 内传递控制+对话上下文，无外部共享可变状态概念。<https://github.com/openai/swarm>
- 🔧★ **OpenAI Agents SDK（Handoffs）**（2025，Swarm 的生产继任）— 调查中最明确的边界陈述：「Handoffs stay within a single run」；SDK 在单应用权限内协调上下文与控制，把跨 run/跨 harness 的共享外部状态显式划在范围之外——正是 Limen 服务的空间。<https://openai.github.io/openai-agents-python/handoffs/>
- 🔧 **Microsoft AutoGen / Swarm** — 主流 multi-agent 的"共享 state"是单 team 进程内的共享消息上下文；AutoGen 自己警告并行工具调用可能「unexpected behavior」，但其解法是在单应用内串行化。<https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/swarm.html>
- 🔧 **LangChain / LangGraph — Multi-Agent** — 最清晰的"multi-agent = 单应用内共享 state 上的上下文工程"框架：协调 = "决定每个 agent 看到什么"。Limen 是正交的另一半。<https://docs.langchain.com/oss/python/langchain/multi-agent> · <https://docs.langchain.com/oss/python/langgraph/overview>

---

## 7. 并行 coding agents & 评估方法学 ★新增

> Limen 的 hero experiment（Seq-1 / Par-N-Naive / Par-N-Limen；pass@1、wall-clock、lost-edit-lines、build-break-rate、attribution-accuracy；Pareto 支配论点）的学术地基。⚠️ 这一节的具体数字多为 summarizer 来源，引用前务必回源 PDF。

- 📄★ **Pugachev (2025) — "CodeCRDT: Observation-Driven Coordination for Multi-Agent LLM Code Generation"**（arXiv:2510.18893）— 最近的学术锚点 + 乐观设计对照点：朴素并行多 agent 代码生成在某些任务加速、另一些减速，语义冲突率非零——直接支撑 Par-N-Naive 臂与"必须实测 wall-clock"。Limen 写前预防 vs CodeCRDT 写后 CRDT 合并。<https://arxiv.org/abs/2510.18893>
- 📄★ **Chacon Sartori (2026) — "The Specification Gap: Coordination Failure Under Partial Knowledge in Code Agents"**（arXiv:2603.24284）— 提供 Limen 需要的学术词汇：独立讨论并发 code agent 间的 lost updates、interface breakage、stale partial views，并指出现有系统缺乏在部分知识下检测 specification mismatch 的机制（Limen 写前 lease 边界处理的 gap）。⚠️ 读自 abstract/intro，依赖前核对术语是否为正式范畴。<https://arxiv.org/abs/2603.24284>
- 📄★ **Ehsani et al. (2026) — "Where Do AI Coding Agents Fail? An Empirical Study of Failed Agentic Pull Requests in GitHub"**（MSR '26）— 用真实数据支撑 build-break-rate 与重复劳动指标（CI/test 失败、重复 PR 为主要拒绝原因）。⚠️ 仅测单 PR 结果，**不**测并发 agent 碰撞——作为单 agent 基线失败率上下文引用，非多 agent lost update 证据。<https://arxiv.org/html/2601.15195v1>
- 📄★ **Chen et al. (2021) — "Evaluating Large Language Models Trained on Code"（Codex；引入 HumanEval 与 pass@k）**（arXiv:2107.03374）— Limen 的 pass@1 指标的经典出处：pass@k = k 个样本至少一个通过，附无偏估计量。<https://arxiv.org/abs/2107.03374>
- 📄★ **Khanal, Tao, Zhou (2026) — "Beyond pass@1: A Reliability Science Framework for Long-Horizon LLM Agents"**（arXiv:2603.29231）— 支撑更严的可靠性框架：pass^k（k 次重复全部成功）度量协调层真正买到的一致性。最锋利假设：Par-N-Naive 可能 pass@1 打平却在 pass^k 崩塌。⚠️ pass^k 定义与头条数字引用前核对。<https://arxiv.org/abs/2603.29231>
- 📄 **Agent systems scaling（2025–2026 多 agent scaling 文献）** — 支撑把实验主张框为"固定 N 下的 Pareto 支配"而非"agent 越多越好"：强单 agent 基线之上协调可能递减/负回报。⚠️ 来源异质，具体阈值视任务而定，作示例引用并标明原始论文。<https://arxiv.org/pdf/2512.08296>

---

## 8. 可观测 / 评估（审计与实验参考）

> Limen 的 witness/audit 层、以及 hero experiment 的度量设计，可借鉴这些工具的"操作形态"（但 Limen 不内建 hosted UI）。

- 🔧 **LangSmith** — observability / pairwise eval / annotation queue / automation / experiment compare。实验度量与审计 UX 的参考。<https://docs.langchain.com/langsmith/observability-concepts> · <https://docs.langchain.com/langsmith/evaluate-pairwise> · <https://docs.langchain.com/langsmith/compare-experiment-results>

---

## 9. 执行模式 / 自愈（"我们刻意没走"的对照）

- 🔧 **Ralph pattern** — fresh-context + deterministic-check + feedback 的迭代执行模式。Limen 不内建，实验里 agent 的执行循环可能用到。<https://github.com/iannuttall/ralph> · <https://github.com/vercel-labs/ralph-loop-agent>
- 🔧 **Temporal** — durable workflow / replay / timer。前身 continuity 思想来源；Limen **不做**，留作"刻意没走这条路"的记录。<https://docs.temporal.io/workflows>
- 🧭 **Kubernetes self-healing** — 期望状态调和。前身 supervisor 灵感源；Limen **不做**编排。<https://kubernetes.io/docs/concepts/architecture/self-healing/>
- 📄 **IBM (2003) — "Autonomic Computing: Architectural Approach and Prototype"**（MAPE-K）— 自管理系统经典框架。前身 self-repair 理论根；Limen 显式不追求自治，保留作对照。<https://research.ibm.com/publications/autonomic-computing-architectural-approach-and-prototype>

---

## 10. 行业观点 / 随笔（Essays）

- 📝★ **Otavio Carvalho — "Our AI Orchestration Frameworks Are Reinventing Linda (1985)"**（2026-02-12）— 独立到达 Limen 论点的近期随笔：今天的 agent 协调栈重新发明元组空间，却缺原子认领与 lease/aging，因而有 race-on-claim 与 lost-update——正是 Limen 填的洞。（观点博客，作 gap 佐证，非技术权威。）<https://otavio.cat/posts/ai-orchestration-reinventing-linda/>
- 📝 **Notion — "Steam, Steel, and Infinite Minds"** — "制度滞后于能力增长"。Limen 引用其"timing"洞察即可，不接受其"需要重型治理"的结论。<https://www.notion.com/blog/steam-steel-and-infinite-minds-ai>
- 📝 **Anthropic — Claude's Constitution / Constitutional AI** — 规则引导模型行为。与 Limen 正交（模型层 vs workspace 层），留作"model guidance ≠ runtime coordination"的区分依据。<https://www.anthropic.com/constitution> · <https://www.anthropic.com/research/constitutional-ai-harmlessness-from-ai-feedback/>

---

## 11. 已并入

- ✅ 之前 TODO 里那篇抓不到的**微信公众号文章**已确认就是 **Anthropic — Zero Trust for AI Agents**（微信端的转载/译介）。即 §0 的主锚点，无需单列。原短链：<https://mp.weixin.qq.com/s/Kgqi3XqTZSroyDCpUgsdsw>

---

## 附录：从前身（Crawfish）继承 / 抛弃的参考去留

| 参考 | 前身用途 | Limen 处置 |
|---|---|---|
| Anthropic Zero Trust | （未引用） | **升为唯一 PRD 锚点** |
| 分布式 lease / Chubby / etcd / ZooKeeper | （缺失） | **核心谱系（§1）** |
| Berenson / Kung-Robinson / MVCC / Dijkstra / Lamport | （缺失） | **新增并发控制理论（§1）** |
| NIST 800-207 / Saltzer-Schroeder / Dennis-VanHorn | （缺失） | **新增零信任/capability 谱系（§2）** |
| 黑板 / Linda / Actor / STM / CRDT / OT | （缺失） | **新增共享状态协调谱系（§3）** |
| MCP | tool plane | **升为接入面核心（§4）** |
| MCP Agent Mail | （缺失） | **新增为最近 prior art（§5）** |
| CodeCRDT / Specification Gap / pass@k / pass^k | （缺失） | **新增并行 coding & 评估（§7）** |
| A2A / ACP | 深度集成 | 降为生态背景（非目标，§4） |
| LangChain / AutoGen / OpenAI SDK / Anthropic agents | 竞品对标 | 降为 related work（正交，§6） |
| LangSmith | 评估范本 | 留作实验/审计参考（§8） |
| Temporal / K8s / IBM Autonomic | continuity/self-heal 思想源 | 留作"刻意不走"的对照（§9） |
| Notion / Constitutional AI | governance 论证 | 留作 timing/区分依据，弃其重型治理结论（§10） |

---

*维护约定：新参考请按上述分类追加，每条一句"与 Limen 的关系"。论文优先给标题+会议年份（URL 可能失效），项目给官网。承重的逐字引用先核源，数字标 ⚠️ 直至回源确认。保持注解简洁——这是索引，不是综述。*
