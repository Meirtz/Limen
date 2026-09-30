# Limen

**Limen 检查一次实验是否真的按它声称的方式运行。**

当编码 agent 负责实验循环（改代码、启动运行、读数字、汇报结论）时，出问题的多半不是蓄意作弊，而是一个数字并不代表它声称的意思：

- 处理（treatment）根本没有生效：旧的代码副本遮蔽了修改后的包，或者变量名拼错了；
- 两组之间除了处理还改了别的：运行之间改了配置、重新生成了数据；
- 某一组的条目崩溃或解析失败，被算成了"失败"；
- gate 被跳过了，或者根本不可能失败；
- held-out 数据漏进了训练或选择。

Limen 记录一个 Python 运行**实际执行了什么**，并与它**声明**的内容对照检查。它是一个零依赖的小型库和命令行工具，只观察和报告，不替你运行实验，也不改变实验的行为。

## 安装

```bash
pip install git+https://github.com/Meirtz/Limen
```

需要 Python 3.10 及以上，无其他依赖（3.10 上需要 `tomli` 来读取配置文件）。

## 使用

用 `limen run` 运行每一组，并声明这一组改变了什么：

```bash
limen run --name wiki --arm base --treatment env:WIKI_ROOT eval.py
WIKI_ROOT=kb/rich limen run --name wiki --arm rich --treatment env:WIKI_ROOT eval.py
limen compare wiki:base wiki:rich
```

如果 `eval.py` 实际导入的是一个从不读取 `WIKI_ROOT` 的旧副本，`compare` 会报告 `PLACEBO`、`SHADOWED` 和 `TREATMENT_NOT_READ`，并以退出码 1 拒绝这次比较。在实验里用 `limen.outcome`、`limen.metric`、`limen.param` 上报逐条结果和指标；用 `@limen.gate(..., positives=[...], negatives=[...])` 给 gate 配上已知正确和已知错误的对照，不可能失败的 gate 会在评判任何候选之前就被发现。

其他命令：`limen check`（检查单次运行）、`limen trace`（某个文件由哪次运行、从什么输入产生）、`limen leak`（在数据文件中查找 held-out 条目 id）、`limen ls`、`limen show`。所有检查项见 [docs/reference.md](docs/reference.md)（英文）。

## 实测能发现什么

`bench/` 用两个小型实验循环跑故障场景，并与 MLflow 式跟踪（git 提交、命令行和记录的参数、依赖版本、运行状态）和 Sacred 式跟踪（再加源码哈希和主机信息）在同一比较规则下对比。

| 场景 | 检测器 | 拦住的无效比较 | 误报的合法比较 |
|---|---|---|---|
| 开发集（样本内） | MLflow 式 | 2 / 12 | 0 / 7 |
| 开发集（样本内） | Sacred 式 | 4 / 12 | 1 / 7 |
| 开发集（样本内） | Limen | 11 / 12 | 0 / 7 |
| **留出集** | MLflow 式 | 7 / 18 | 2 / 9 |
| **留出集** | Sacred 式 | 8 / 18 | 3 / 9 |
| **留出集** | **Limen** | **14 / 18** | **2 / 9** |

开发集场景与检查规则一起编写，只有留出集能说明泛化能力。留出集在检查规则冻结（标签 `bench-freeze`）之后，由三位从未看过 Limen 源码或输出的独立作者编写，并由另一位盲判者独立标注（27 个标签全部一致）。这些场景规模小、是合成的、全部是 Python，衡量的是已知类型的故障能否被拦住，而不是它们在真实项目中有多常见。

留出集上：7 个无效比较只有 Limen 拦住；3 个拦截来自副作用而非故障本身（两个"N 个种子里挑最好"因为比较的运行种子不同被拦下，一个在验证集上调阈值因为读了不同的配置文件被拦下，跟踪基线同样能拦住这些）；4 个漏掉（用评估集自身标签构造的特征、噪声级别的效应只给了警告、处理变量的取值拼错导致代码静默回退默认值、候选代码导入了评测的测试用例）；2 个合法比较被误报（配置文件只是重新格式化、内容寻址的判决缓存使声明的 gate 在命中时未执行）。逐场景结果见 [bench/results/results.md](bench/results/results.md)。

## 看不到什么

- 子进程、worker、其他进程，以及自行打开文件的原生代码（Arrow 数据集、safetensors、HDF5）读取的内容。Limen 会列出它看到的子进程和原生读取模块。
- 一个检查是否**有意义**：测错了东西但能通过也能失败的 gate 看起来是健康的。
- 蓄意对抗：记录不是防篡改的。

## 状态

Alpha。记录格式（`limen.run/1`）和命令行仍可能变化。本仓库之前的设计（面向并发 agent 的建议式写租约）保存在标签 `leases-final`。

采用 MIT 或 Apache-2.0 许可，任选其一。
