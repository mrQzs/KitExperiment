# 05 三平台命令与入口

**审阅增强版｜跨平台运行时、参数边界、进程监督与恢复**

状态：**拟议实施说明，尚未接入实际项目**；不是已审核合同，不授权安装依赖、修改 CI、修改受保护核心、启动真实模型或扩大权限。本文附带的离线参考及 Linux 合成子进程测试，不等于本模块生产实现已完成。

总纲：[完整设计](../code-kit-v2-design.md)；导航：[实施索引](README.md)。  
对应总设计：第 5、9 节及 P2。  
审阅日期：2026-09-25。  
主要依据：上传的 `05-cross-platform-runtime.md`，以及本会话提供的 01–04 原文；针对交接点另核对了 03、04 增强版的准入、Invocation、TransportSummary 相关段落。[^P05][^P03E][^P04E]

> **阅读约定**：保留原文 1–9 章、4.1–4.3 主线和 `05-A01`–`05-A10`。各节“原文保留”沿用上传材料；新增字段、状态、算法、阈值、Schema、参考实现和验收编号均为**本版建议**，须由正式实施合同审定。外部资料标注来源，用于辨认系统行为及设计风险，不能替代用户实际版本和机器的取证。代码中的 synthetic/stub 表示人工合成，不是 Pi 或项目真实运行记录。

**本次证据边界**：未取得实际项目仓库、AGENTS、总设计正文、现行 `run_supervised` / `_WinJob` 源码、原项目测试或 06–11 完整正文。原文所述现状保留为“原文记载”，不提升为本次源码核验结论。上述导航为原导航，未验证所有目标文件存在。正文给出完整实施设计，附录只实现明确列出的参考子集；不借示例掩盖尚未实现的 Windows Job、生产审核、脱敏、恢复和三平台验收。

**阅读路径**：正文 1–9 为实施说明；附录 A 为结构契约与语义校验边界；附录 B 为可提取源码、夹具和复算程序；附录 C 为本次实际验证；附录 D 为原文追溯；附录 E 为资料来源。

## 1. 目标与非目标

### 1.1 原文保留

让同一调度核心在 Linux、macOS、Windows **原生环境**执行明确的平台命令。分别探测操作系统、用户 shell、CLI 工具能力，保留环境锁与退出语义。

无 shell 需求的命令直接运行 argv；确有需求时，显式声明 shell 及平台实现。不自动翻译 Bash/PowerShell，不将 WSL 与原生 Windows 当成同一个运行环境。不通过 `shell=True` 普遍兼容不安全字符串，不把模型输出当成可展开的命令模板。

正常超时/取消复用现有进程监督。**不承诺 POSIX 平台在调度器被强杀后清掉所有后代**；首期提供检测、证据与恢复说明。[^P05]

### 1.2 审阅结论与补强重点

原文已经明确职责与安全底线，但“可执行的平台命令”“进程树清理”“参数原样传递”仍需要落实到可判定的契约。

| 缺口 | 直接风险 | 本版补充位置 |
|---|---|---|
| 共同字段、平台变体与默认值未展开 | 变体改掉 required，缺平台被误算通过 | 4.1.1–4.1.4 |
| Invocation 与检查命令没有统一落地层 | adapter 重新解析 PATH，shell 绕过锁 | 2.2、4.1.5 |
| 环境锁只列路径/版本，没有完整入口链 | node/python 本体未变，脚本或包已被替换 | 4.2.2–4.2.3 |
| PowerShell 转发未区分版本与 API | 空参数丢失，内嵌引号被重解释 | 4.2.5–4.2.6 |
| cwd 只有字符串级边界 | symlink/junction 逃逸、检查后目录被替换 | 4.1.7 |
| timeout 与取消未规定竞态裁决 | 超时后返回 0 被误记为正常完成 | 4.3.2–4.3.4 |
| 主进程退出与 EOF/后代存活未分离 | 永久等待管道，后台后代继续修改候选 | 4.3.5–4.3.8 |
| Windows Job 绑定时序未定义 | 子进程在归入 Job 前执行或派生后代 | 4.3.7 |
| 恢复只有 PID，缺身份和协调器版本 | PID 复用误杀、启动结果未知时重复执行 | 4.3.9、第 8 章 |
| mock 与原生验收未建立映射 | 在 Linux 模拟 Windows 后宣称全平台支持 | 第 7 章、附录 C |

这些不是对现有实现已存在缺陷的断言；它们是原文尚未提供可核验细节的部分。

### 1.3 不可弱化的不变量

| 编号 | 不变量 |
|---|---|
| I01 | 执行依据是锁定机器结构；Markdown、模型报告、终端展示文本均不是二次执行源。 |
| I02 | 一个命令在一个目标平台恰好选出一个载体；不得猜测、回退或合并多个变体。 |
| I03 | argv 顺序、空项、Unicode、引号和换行是数据；不进行 shell 展开或“修复性”拆分。 |
| I04 | `request_id` 是预算根，重试另建 `run_id`；05 不自动重试、不计费、不签发候选 PASS。 |
| I05 | `cwd`、环境过滤、Job/进程组属于不同边界，任何一个都不能单独称为 OS 沙箱。 |
| I06 | 授权、审核、输入锁、预算准入、OS 启动是不同事实，启动成功不能倒推此前已获准。 |
| I07 | 当前环境与锁定环境不符即阻塞；配置语义哈希相同不能代替环境核验。 |
| I08 | 自然退出、停止原因、采集完整性、清理状态分别保存，原始退出码不得被统一“失败码”覆盖。 |
| I09 | 主进程结束不等于后代结束；后代结束不等于证据封存；证据封存不等于验收通过。 |
| I10 | 超时/取消后继续排空已产生的输出；超限或日志故障不能被静默丢弃后包装成完整结果。 |
| I11 | 只终止有足够归属证据的运行范围；不对未知、复用或身份不足的 PID 进行猜测性清理。 |
| I12 | Windows 与 WSL 的进程、路径、Git、Python、凭据来源不可在同一 attempt 中自动混搭。 |
| I13 | 已发布资源、历史 attempt、原始证据和旧 verdict 保持原字节，不重写历史哈希。 |
| I14 | 支持能力按 OS、架构、执行环境、shell、CLI build、入口链和测试场景联合声明。 |

## 2. 依赖与输入输出

### 2.1 原文保留与模块分工

前置依赖是 [04 CLI 适配器](04-cli-adapters-and-pi.md)，复用 01 公共协议与 02 配置结果。输入为 Invocation 或合同检查定义、目标平台、工作目录、过滤后的环境、时限与取消信号；输出为已解析的命令/工具信息、日志、退出码、超时/取消/启动错误和进程清理证据。[^P05]

| 模块 | 交接职责 | 05 不得越界承担 |
|---|---|---|
| 01 | ResourceRef、固定字节哈希、公共事件、错误和旧退出码语义 | 另造资源哈希算法，改变旧 wait / verdict 语义 |
| 02 | 已验证配置、所选 profile、固定参数、环境计划 | 临时改模型、工具策略或合并任意 local 字段 |
| 03 | 固定输入审核、精确环境锁、实时授权与一次性准入 | 创建审批、把存在 permit 文件当成授权 |
| 04 | Invocation、无模型 probe 计划、协议解析、ResultBundle | 猜 Pi JSON、改终止协议、用进程 0 退出填补模型证据 |
| 05 | 平台预检、受控启动、I/O、停止、进程归属、TransportSummary | 自行决定真实模型重试、费用放行或代码集成 |
| 07 / 现有调度器 | 领取、并发、取消授权、预算和 G0–G6 编排 | 以清理成功代替业务成功，以本地检查 SATISFIED 代替候选 PASS |
| 10 | 生命周期索引、状态展示、恢复入口关联 | 用可变 UI 状态代替不可变事实和当前守卫 |

03 不是 05 的循环实现依赖：05 可以用无模型 fixture 验证进程机制；正式 v2 调用必须接入 03/07 的真实守卫。03、04 增强版的建议仍需实施合同共同审定，而不是因为本文引用就自动升级为已部署协议。[^P03E][^P04E]

### 2.2 两类输入，一层最终执行计划

```text
04 锁定 Invocation（首期 exec_only）──┐
                                    ├─→ 核心核对意图 → 05 FinalLaunchPlan
合同中的 CommandSpec（argv/shell）──┘                        │
                                           03/07 准入 + handoff
                                                            │
                                               05 平台后端与 I/O
                                                            │
                               TransportSummary + 进程/清理/证据引用
                                      │                     │
                              04 协议结果归约       检查断言与现有门禁
```

**不能用“统一 runner”改变输入的许可范围**。04 的 `execution_mode=exec_only` 不得因目标机器缺 native 入口而自动换成 shell；合同检查明确声明 shell，才可进入显式 shell 路径。probe 也必须有明确本机执行权限、时限和禁止副作用范围，不能通过“只是 --help”跳过入口可信性核验。

### 2.3 建议交接对象

| 对象 | 核心内容 | 生成/消费方 |
|---|---|---|
| `CommandSet` | 已声明平台、命令集合、完整共同语义 | 合同层生成，05 全集合校验 |
| `CommandSpec` | id、required、cwd、timeout、expected_exit、断言、单一载体 | 检查执行路径 |
| `Invocation` | 04 的固定 config/descriptor/environment/cwd/entry 引用、argv_tail、输入与限制 | 04 生成，05 不篡改 |
| `ExecutableBinding` | 启动入口、解释器/launcher/脚本/模块链、版本和字节指纹 | 05 预检生成 |
| `EnvironmentRecord` | OS、架构、环境实例、实际 shell/工具、过滤策略、能力证据 | 05 与 04 汇总，03 消费 |
| `FinalLaunchPlan` | 完整 argv、入口/cwd/environment 引用、stdin、平台后端和有效限额 | 05 在准入前封存 |
| `HandoffReceipt` | operation/run、准入来源、plan 摘要、controller generation、接纳序号 | 03/07/05 协调层 |
| `ProcessIdentity` | host/boot/environment/run、PID、创建标记、入口、PGID 或 Job 关联 | 05 受信观察 |
| `TransportSummary` | 是否启动、退出事实、停止原因、两条流、证据及清理状态 | 05 输出；04/检查层只读 |
| `CleanupReport` | 清理对象、身份判定、动作、等待与复核、残留/限制 | 05 输出，恢复和门禁消费 |

这些名称是独立记录家族；`schema_version: 1` 可以表示某个新记录家族的首版，**不表示合同回退到 schema v1**。最终字段需与 01 注册表合并，未知记录家族或版本拒绝，不能随意按“类似字段”解释。

### 2.4 平台与执行环境不是同一个维度

原平台枚举固定为 `linux`、`macos`、`windows`，不新增 `wsl`、`darwin`、`posix` 等业务枚举。建议另记：

| 字段 | 示例/要求 |
|---|---|
| `platform` | 三个固定值；实际探测与请求一致 |
| `execution_environment_id` | 核心登记的稳定环境身份；不能只用主机名 |
| `environment_kind` | `native`、`wsl1`、`wsl2`、`container`、`vm` 等登记分类 |
| `host_os` / `kernel_release` | 主机/内核事实；未知保留 unknown |
| `process_arch` / `host_arch` | 区分 x86_64、arm64 和转译运行；不能仅记录硬件型号 |
| `filesystem_profile` | worktree 的实际大小写、链接、挂载、路径和权限能力 |
| `controller_generation` | 本次主控代际，用于旧协调器隔离，不是每次连接的序号 |

WSL 内的 Linux Python属于 `platform=linux`，但要有独立 environment ID 和 WSL 分类。Linux 容器内通过的合成测试也不能冒称用户 Linux 裸机、macOS 或 Windows 已验收。

## 3. 现状与复用点

### 3.1 原文记载的复用基础

| 现有位置 | 原文记载 | 建议迁移目标 | 必须补录的真实基准 |
|---|---|---|---|
| `run_supervised` | 已提供执行监督 | `process.py` 公共 runner 外观 | 起止边界、I/O、异常、退出和清理时序 |
| `_WinJob` | Windows 进程清理 | 专用 Windows 后端 | Job 创建/加入时点、句柄、嵌套 Job、失败路径 |
| `_terminate_tree` | POSIX 清理 | 专用 POSIX 后端 | PGID、组长退出、setsid、信号和残留判据 |
| `_run_command_item` | 工作目录、日志、超时、退出码 | 检查适配层 | required、expected_exit、断言和旧报告语义 |
| `_resolve_argv` | argv 解析 | 平台入口解析与锁定层 | PATH、解释器、shim、特殊参数原行为 |
| `scripts/dispatch-pi.sh` | 参数转发、Python 探测、Bash 3.2 空数组考虑 | 显式旧入口 + 新薄包装 | 无参/空参、Unicode、退出码和入口定位 |
| Windows npm shim 检查 | 特殊字符限制，优先 native/node | adapter 识别 + 05 可信链验证 | shim 内容/包入口/Node 精确版本与负例 |

原文指出仍缺中立 Python/PowerShell 入口、合同平台变体和显式 shell 结构；真实三平台测试不能由单平台 mock 替代。本文未取得上述源码，不能判断已有实现是否存在本版列出的具体竞态。[^P05]

### 3.2 迁移前应形成的基线包

记录实际 HEAD、工作区差异、现有配置/规则引用、解释器路径与版本、现有测试命令、stdout/stderr、原始退出码、执行环境与未运行项。按“旧输入 → 旧结构化结果 → 新结果 → 差异理由”建立回归映射。

优先固定以下行为：旧入口 `wait=3`，无 shell argv 边界，Pi shim 拒绝项，启动异常和自然非零退出，超时/取消，调度器强杀，父先退子仍活，日志截断，工作目录越界，以及 v1 合同读取。**迁移先保持事实和失败判定一致，再单独引入新保证**，不在提取代码时同时更换 CLI、模型、shell 或运行策略。

## 4. 接口和行为约束

### 4.1 合同命令模型

#### 4.1.1 原文保留与字段级建议

共同字段包括 `id`、`required`、`cwd`、`timeout_seconds`、`expected_exit_code` 及输出断言。载体是非空 `argv`，或成对的 `{shell, script}`；两者互斥。`by_platform` 只能提供完整载体，不能覆盖共同语义。所有声明平台都要在检查集合锁定前完成覆盖验证，缺平台不能跳过后通过。[^P05]

| 字段 | 首版建议类型与必填规则 | 默认/空值/失败处理 |
|---|---|---|
| `id` | 必填、集合内唯一；`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` | 大写 V01 与原例兼容；不允许靠索引充当身份 |
| `required` | 必填 boolean | 不接受 0/1、字符串或 null；不能由变体修改 |
| `cwd` | 必填、worktree 相对正斜杠路径；`.` 表示根 | 不做 `~`、环境变量展开；缺失不默认当前 shell 目录 |
| `timeout_seconds` | 必填，首版建议整数 1–86400 | 不接受 boolean、浮点表示、0、负数、无限时限；实际上限另受政策收紧 |
| `expected_exit_code` | 必填；可移植首版建议 0–255 整数 | 正常非零退出可符合检查预期；负信号不是预期退出码 |
| `output_assertions` | 可缺省为 `[]`，锁定前显式展开 | null 拒绝；错误/缺失数据不能自动当断言通过 |
| `argv` | 非空 string[]；argv[0] 非空、禁止 NUL/CR/LF | 后续参数允许空字符串；禁止非 Unicode 标量和 NUL |
| `shell` | 已登记 shell ID；版本/入口由环境绑定 | 不是用户默认 shell，不接受任意可执行路径冒充 ID |
| `script` | 本版建议为 `kind=shell-script` 的文件 ResourceRef | 完整字节锁定；不对未受审模型字符串临时生成脚本 |
| `by_platform` | 只含三个登记平台键，值为完整载体 | 不与通用载体并存；不做深层合并或通用 fallback |

以上数值上限是待审设计值，不是原文已有约束或 OS 上限。必须在发布阶段展开默认值、验证全部字段，再生成语义字节，不能先删掉未知字段后宣称合法。01 的说明字段例外不适用于合同。

`script` 文件化是本版具体化建议：原文只要求成对声明，未固定 script 字段形状。需要保留旧内联文本时，应走单独、版本化的编译步骤，把**原始受审文本**生成为可引用文件；新审核同时绑定源文本、编译规则和输出文件，不能在启动时任意插入安全前缀。

#### 4.1.2 检查集合与平台覆盖

建议 `CommandSet` 承载 `supported_platforms` 与 `commands`，单条命令继承集合平台范围。

```text
严格 JSON → Schema → 命令 ID 去重 → 所有共同字段校验
    → 每条命令验证载体互斥
    → 对每个声明平台验证完整覆盖
    → 核对 shell/工具/argv 能力与环境要求
    → 展开明确定义的默认值 → 封存检查集合
```

本版首期采用**闭合平台集**：`by_platform` 的键集合必须与 `supported_platforms` 恰好相等，避免未声明变体悄悄成为未来执行入口。若未来允许额外变体，必须显式修订 Schema 和锁定规则，不能在运行时宽松解释。

`required=false` 只影响合同定义的聚合门禁，不意味着配置损坏、缺平台、越界路径、缺 shell、启动失败可以记为成功。可选检查未运行的结果必须是 `NOT_RUN_OPTIONAL` 或相应阻塞/失败事实，且未覆盖内容不能被写进“已验证能力”。

#### 4.1.3 唯一选择算法

```text
resolve_command(check_set, command_id, target_platform):
    validate_entire_set(check_set)       # 不只校验正在运行的单条/单平台
    require target_platform in supported_platforms
    command = require_unique_id(command_id)
    if command has by_platform:
        require no generic argv/shell/script
        carrier = exact_copy(command.by_platform[target_platform])
    else:
        carrier = exact_copy(command.generic_carrier)
    require carrier is exactly argv OR exactly shell+script
    resolved = copy(common_fields) + copy(carrier)
    attach source_ref, command_id, target_platform and field provenance
    return resolved                     # 不写回原集合
```

集合级支持声明可能包含当前没有机器的平台：**结构覆盖可先验证，原生支持仍是 NOT_RUN**。不得把“有 windows 键”解释为 Windows 已能运行。

#### 4.1.4 原示例与新记录包装

原示例完整保留，路径和 Python 入口仍要在正式合同中确定，不能据此自动安装 pytest：

```json
{
  "id": "V01",
  "required": true,
  "cwd": ".",
  "timeout_seconds": 300,
  "expected_exit_code": 0,
  "by_platform": {
    "linux": {"argv": ["python3", "-m", "pytest", "tests"]},
    "macos": {"argv": ["python3", "-m", "pytest", "tests"]},
    "windows": {"argv": ["py", "-3", "-m", "pytest", "tests"]}
  }
}
```

附录使用 `record_type=command_set` 包装这个例子。`py -3` 是原文中的人类可读候选，不是精确环境锁；正式运行须核验 launcher 实际选中的 Python，并按已批准规则固定实际入口或固定可复核的 launcher 链。

#### 4.1.5 FinalLaunchPlan：逻辑意图与 OS 调用分离

04 增强版建议 Invocation 保存 `executable_binding_ref`、`argv_tail`、`cwd_ref`、`env_names`、输入引用及限额；05 将其解析为最终启动计划，不能重新跟随同名 profile 或任意 PATH。[^P04E]

建议封存以下项目：

| 类别 | FinalLaunchPlan 的内容 |
|---|---|
| 归属 | request/run/operation、输入来源引用；检查路径另带 command_id |
| 环境 | platform、execution_environment_id、environment_ref、入口链与 cwd 绑定 |
| 命令 | 完整有序 argv、`exec_only` 或 `explicit_shell`、受控 script_ref |
| 输入 | 无输入则 DEVNULL；否则固定资源到 stdin；04 的 request-file 通道保持其已审协议 |
| 环境变量 | 已核准名称和安全绑定；明文凭据值只在受控内存中组装 |
| 上限 | 有效运行时限、优雅停止、强制等待、管道排空与两流字节上限 |
| 进程机制 | 版本化 backend_id、无交互/无 TTY、句柄继承及隔离能力要求 |

有效限制取合同、组织策略、adapter 要求、工作流剩余限额的交集；不是随意对所有数值做 min。例如“需完整证据”与“不保存输出”不是可取更小值的两个数字，应当判定冲突。

**参数、环境、工作目录不得在封存后可变**。实现应复制或冻结所用对象；不要把可修改 dict 交给后台线程，再在审核之后原地追加 flag。计划不内嵌自己的摘要，独立 handoff 引用它的最终完整字节。

#### 4.1.6 argv 的真正边界

需要分别验证三层：

1. 机器定义中的元素边界与 Unicode 内容。
2. 平台启动 API 的传递方式，以及子程序的实际参数解码。
3. 目标 CLI 对 `--`、`@file`、通配符、选项别名、响应文件和正文的语义。

Windows 进程创建涉及命令行字符串转换，不能把 Python list 当成所有程序共享的参数解析协议；`.bat/.cmd` 还可能由系统 shell 解释，即使调用者没有显式要求 `shell=True`。[^W01] 因此本版要求 native/解释器链分别做回显夹具；未知批处理入口阻塞，不以加引号、`shlex.split` 或把全部参数连接后重拆作为通用修复。

每条完整 argv 要做实际平台长度检查：Windows 按最终编码/引用后的 UTF-16 命令行，POSIX 按 argv、环境及系统限制；不可只计算 Unicode 字符数。超限时明确报错，不截断参数、不改成未经审核的响应文件或 stdin 模式。平台 API 边界与预算上限分别报告。

附录参考的 1024 参数 / 1 MiB UTF-8 总量仅是输入防护上限，**不能证明 Windows 命令行可接受这些大小**。真正启动前仍要使用实际后端做可传递性校验。

#### 4.1.7 cwd、业务路径与检查后替换

原文要求解析后的 cwd 位于对应 worktree，使用实际平台的链接和大小写语义核验；简单文件操作优先 Python/pathlib。[^P05]

建议执行顺序：

```text
受信 worktree 绑定 → 相对路径文法 → 逐段解析/归属核验
    → 记录根与目标的 OS 身份、链接链、实际挂载/大小写事实
    → 启动前重新核验 → 平台可用时使用稳定句柄/受控启动目录
    → 不能固定的窗口列入保证限制；高风险合同因此阻塞
```

业务路径拒绝绝对路径、空段、`.`/`..` 组合、反斜杠、盘符、UNC、NUL 和换行；`.` 仅作为整个 cwd 的根目录特例。本版将冒号等保留结构拒绝作为可移植业务路径策略，**不是声称这些字符在所有 OS 都非法**。机器真实路径由核心环境记录保存，不套用业务路径的正斜杠限制。

Windows 另核对 junction/reparse point、设备命名空间、保留设备名、ADS、尾点/尾空格、盘符相对路径和大小写能力。官方文件命名说明区分命名空间和文件系统规则，并不支持“一律转小写后比较”的做法。[^W09]

`Path.resolve()` 再做 `relative_to()` 只能发现当时的静态越界，**不是 TOCTOU 防护**。允许同账户不受控写目录时，检查之后仍可能替换目录、链接或脚本；文件哈希也不是文件系统访问控制。附录明确只实现静态检查，生产需以目录权限、受控对象版本、平台句柄和正式隔离配置补齐。

拒绝把用户工作区、主仓库或 `.agent/runs/` 当成临时可删除目录。清理脚本只接受核心分配的运行目录身份及精确资源清单，不接受模型输出的 `rm -rf` / `Remove-Item -Recurse` 模板。

#### 4.1.8 环境过滤与行为绑定

实际环境从白名单构建，不默认复制整个父环境再删少量键。名称集合、必要项、凭据引用、配置目录、代理/endpoint、locale、临时目录和运行时加载规则均有来源与有效版本。

| 类别 | 建议规则 |
|---|---|
| 运行基础 | PATH、系统目录、受控 HOME/USERPROFILE、临时目录等按用途选取；最小集合不等于空环境 |
| 凭据 | 来自既有凭据管理；不写 argv、公开 JSON、完整环境日志、异常文本或低熵秘密哈希 |
| 启动注入 | BASH_ENV、ENV、SHELLOPTS/BASHOPTS、ZDOTDIR、PYTHONPATH、NODE_OPTIONS、动态库注入等默认不继承 |
| 工作流/模型覆盖 | v2 不消费 PI_EXEC_* / PI_DISPATCH_PI_CMD 改写锁定值；其他变量按登记用途分别处理 |
| 业务行为值 | 影响 endpoint、账号权限域、代理及加载资源的值不能一概视为机器噪声删除 |
| 大小写 | Windows 名称统一后检测碰撞；POSIX 按原名称区分；不“最后一项覆盖” |
| 值类型 | 只允许明确字符串，空字符串可以是合法值；null 与缺失含义不混同，NUL 拒绝 |

更换凭据版本可能不改变配置语义哈希，但账号/权限域/外发目标或实际环境绑定变化仍须按 03 重新核验。05 不把过滤环境当成网络隔离；CLI 自行读取用户配置、调用系统服务或子进程的行为仍需要独立约束与证据。

#### 4.1.9 输出断言与同源展示

首版建议仅提供 `contains_utf8`、`equals_utf8` 两类有界文本断言，明确 stream 和非空 text。将来加入文件/JSONPath/正则断言时，另行规定路径归属、文件稳定性、引擎版本、计算上限及正则超时，不直接对任意大日志跑不受限正则。

文本断言必须基于指定编码和明确行尾规则；默认严格 UTF-8、原样行尾。不把 CRLF 换成 LF 再计算原流哈希。协议字节保存与人类展示文本分离，解码失败不使用替换字符后继续当作相同原文。

**满足检查的必要条件建议**：确已启动；无超时/取消/采集故障停止；正常退出码匹配；声明范围清理已核验；所需原始证据完整；全部必需断言满足。只能输出本地 `SATISFIED`，是否形成候选 PASS 由原有门禁决定。

原机器定义生成 Markdown 展示，保留共同字段、目标平台、载体、参数索引、入口链和引用。可显示 JSON 数组或逐参数表，shell 风格的一行命令只作展示；不从渲染文本反向执行，也不手工维护第二份命令。MD/JSON 两份合同按 03 同时绑定，展示内容变更仍按既有双格式审核规则处理。

### 4.2 Shell 与入口

#### 4.2.1 原文三平台入口保留

| 环境 | 调度入口 | 需要分别核验 |
|---|---|---|
| Linux | `python3 scripts/dispatch.py`，可选 `.sh` | Python/CLI 的真实入口；Bash/zsh 版本；进程组和信号 |
| macOS | 同上，`.sh` 覆盖原有旧 Bash 场景 | 默认 zsh 与脚本解释器不同；Bash 3.2、系统工具、架构和文件系统 |
| Windows | `py -3 scripts/dispatch.py` 或 Python；可选 `.ps1` | 实际 Python；PowerShell 7 与 Windows PowerShell；cmd 显式选择 |

新增 `.sh/.ps1` 只探测所需解释器、转发参数和退出码，不重复业务逻辑。PowerShell wrapper 原样传播外部程序 `$LASTEXITCODE`，包括 `wait` 的 3。指定 shell 缺失/版本不符必须阻塞，不静默换另一种语法。[^P05]

**支持中立 Windows 入口不以 Bash 为前提**；某个 CLI 内置工具需要 Bash 时，只阻塞该 CLI 的相关用途，不把“中立入口能运行”误报成“所有模型工具均可用”。

#### 4.2.2 入口解析与冻结算法

```text
resolve_entry(locked_runtime, target_environment):
    核对平台、环境实例、已批准 runtime/adapter
    按受控入口配置或已批准 PATH 解析候选，不调用交互 shell 查 alias
    区分 native binary / Python or Node / launcher / shim / shell script
    识别完整解释器与目标脚本/模块链，未知 wrapper 不执行
    核验文件类型、字节指纹、版本、权限、资源/包绑定和所需能力
    由 04 计划、05 执行已允许的无模型 probe
    封存 ExecutableBinding 与 EnvironmentRecord
    启动前对同一绑定做复核，不重新选择“当前 PATH 第一个匹配项”
```

入口链示例是 `launcher → python → dispatch module`、`node → CLI entry script → approved package/runtime set`。只哈希解释器而漏掉脚本，或只比较 `--version` 而漏掉插件/配置/包解析规则，都不足以绑定运行行为。按 02 分离：机器定位进环境锁；固定参数、模块选择、shell 执行选项仍是行为语义。

`python -m package` 的模块解析受搜索路径和环境影响，必须固定导入来源或按受审安装布局验证；不能把模块名本身当作文件哈希。动态库/包依赖全闭包未覆盖时，记录其范围和部署信任假设，不宣称“入口一个 sha256 已锁住全部机器状态”。

#### 4.2.3 PATH、shim 与 bootstrap 信任

PATH 搜索只发生在受控解析/预检阶段。相对可执行路径必须有明确解析根；当前目录隐式搜索、别名、shell 函数、可变 npm shim、脚本 shebang 和 `env` 间接选择都需要纳入入口链。不能把 `command -v` / `Get-Command` 输出的任意目标立即当作受信可执行文件。

Windows npm shim 由 04 按所选 CLI/包规则识别，05 核验最终 native/node 入口及其资源；不执行任意 `.cmd` 以“看它最终调用谁”，也不使用简单正则猜测所有 shim 内容。无法可靠解析的 shim 继续保留原拒绝策略；新增放行必须有锁定版本和参数负例。

**bootstrap 的边界**：用户启动调度器自身时，wrapper/Python 还没有进入本模块的审核流程；这些入口必须先通过受信安装位置、组织策略或用户明确选择建立信任。薄 wrapper 内的解释器探测不是自我授权机制。附录 `CODEKIT_BOOTSTRAP_PYTHON` 仅供合成 wrapper 演示，不是新增生产模型覆盖变量，也不得由模型自由指定。

#### 4.2.4 显式 shell 的登记与受控脚本

建议 shell 登记项至少包含：ID、适用平台、实际入口链引用、精确版本、noninteractive 参数、启动文件规则、脚本编码、行尾策略、退出策略、动态参数支持范围、控制台/取消能力及证据引用。

| shell ID 示例 | 受控执行建议 | 必须单独处理的风险 |
|---|---|---|
| `bash` | 指定 Bash 文件；不用 login shell；启动参数和脚本一起锁定 | BASH_ENV、导出函数、shell options、pipefail/errexit、旧 Bash |
| `zsh` | 明确 noninteractive/no-user-rc 策略和精确文件 | 不能把 `-f` 解读为“系统启动文件都不读” |
| `pwsh` | `-NoProfile -NonInteractive -File` 与固定脚本 | 原生命令参数模式、非终止错误、字符编码 |
| `windows-powershell` | 单独注册，不伪装为 pwsh | 5.1 参数/编码差异；未经验证不宣称通用转发 |
| `cmd` | 只运行已锁定批处理和受限制的入口路径 | 百分号、感叹号、引号、延迟展开、AutoRun、二次解析 |

外部核对：Bash 非交互启动会考虑 `BASH_ENV`；GNU 文档对它与 profile/rc 文件分别说明。zsh 官方文档说明系统 zshenv 存在独立读取规则。[^W07][^W08] 本版据此要求先控制环境/系统启动资源，而不是只添加 `--noprofile --norc` 或 `-f` 就宣称启动环境完全干净。

脚本的哈希针对原字节；不能启动前偷偷添加 `set -euo pipefail`、改编码、改行尾、追加 `exit 0`。需要这些选项时，应在脚本或 shell 登记的行为参数中显式声明并审核。`set -e` 也不是所有 shell 语法场景下的统一失败传播协议；多步骤检查优先拆为多条 argv 检查，复杂脚本显式检查每个关键步骤的退出值。

首期建议不给 shell 脚本增加任意动态文本插值接口。需要运行数据时，使用已审核的输入协议、固定数据文件或明确绑定的参数规则；不得用字符串替换把模型内容变成脚本代码。脚本内自行调用另一个 shell 也要在能力/资源/权限范围中体现，不能把外层“显式 shell”当成无限递归许可。

#### 4.2.5 PowerShell 参数保真：版本与 API 不能混淆

官方资料明确：PowerShell 7.3 调整了原生命令参数传递，`$PSNativeCommandArgumentPassing` 包含 Legacy / Standard / Windows；新行为涉及空字符串和内嵌引号，Windows 模式对部分程序/扩展保留 Legacy 行为。[^W04]

因此首期建议按以下边界实施：

| 入口组合 | 本版建议 |
|---|---|
| 05 原生 Windows 后端 → 已验证 native 程序 | 使用受控 API/编码规则；完整 argv 回显是必须场景 |
| 新 `.ps1` wrapper → 已固定 Python `.exe` | 建议先限定 PowerShell 7.3+、显式 Standard，并做原生回显测试 |
| Windows PowerShell 5.1 → 原生程序 | 单列支持范围；不能直接凭 `& $exe @args` 宣称任意参数保真 |
| `.ps1` → `.cmd/.bat` | 不作为通用透明代理；保持批处理参数限制 |
| 旧 shell 已经丢失/重解释的用户参数 | 下游无法复原；记录调用方限制，使用另行审核的结构化输入入口 |

PowerShell `Start-Process -ArgumentList` 会把数组连接为字符串；它不等于 .NET `ProcessStartInfo.ArgumentList` 的逐项参数集合。[^W05][^W06] 不以变量名都叫 ArgumentList 就假设边界相同，也不把 `--%` 当成所有场景的安全转义器。

对于必须覆盖 5.1 的项目，可另设计一个只接收安全文件路径的结构化 argv 传输入口，但需要明确文件归属、Schema、生命周期和锁定；这不是本版已实现能力。直接从 5.1 键入 Python 命令并不能复原在上一层已丢掉的空参数。

#### 4.2.6 退出码、wrapper 与脚本错误

PowerShell `$LASTEXITCODE` 是原生程序退出状态，与 `$?`、脚本是否抛异常并不等价。[^W10] 薄 wrapper 的实现要求：

```text
核验 wrapper 支持版本及原生参数模式
    → 选择受信解释器
    → 不修改用户参数的数组转发
    → 调用完成后立即保存 $LASTEXITCODE
    → 原样 exit 已保存值
```

不要在读取 `$LASTEXITCODE` 前运行另一个原生帮助命令，不把非零值统一转换成 1，不因为 `wait=3` 触发 wrapper 自己的异常策略而吞掉原值。未取得子进程退出码时报告 bootstrap 错误，不使用旧会话残留值。附录 PowerShell 例子仅面向 7.3+，本次未原生运行。

Shell 错误建议分为：`syntax_precheck_failed`、`shell_launch_failed`、`script_failed`、`program_reported_nonzero` 和 `unknown_origin`。**不能只凭退出码 2/127 或 stderr 的一句话断定错误来自哪层**。若做语法预检，使用同一脚本字节与解释器版本，在受控无副作用条件下进行；预检本身可能读取启动配置，仍须纳入受控执行。

脚本中的程序返回 7，随后执行一个成功的输出命令，不保证脚本还会返回 7；必须显式传播。PowerShell 对非终止错误和外部程序失败也需在已审脚本中定义策略。不要把 `$ErrorActionPreference='Stop'` 当作所有版本下统一的原生命令退出协议。

#### 4.2.7 编码、行尾与非交互模式

业务协议 UTF-8、文件实际编码和终端显示编码分开保存。PowerShell 文档区分 Windows PowerShell 与较新 PowerShell 的编码默认值；5.1 对含非 ASCII 的无 BOM 脚本存在兼容问题。[^W11] 因此：

- 新生成的协议 JSON/JSONL遵循 01 的 UTF-8 无 BOM、固定 LF 规则；任意外部脚本按已经锁定的字节及编码执行，不临时“修复”。
- `.sh` 兼容性要实际覆盖 LF、执行位、路径和旧 Bash；`.ps1` 的 BOM/编码必须属于受审脚本产物。改 BOM 也改变原始字节哈希。
- 非交互 runner 默认 stdin 不连终端、不分配 PTY/ConPTY，不因 CLI 提示登录就自动打开浏览器或申请 TTY。确需交互会话时另设计专用后端和验收。

对于用户输入参数，CR/LF 可以是合法数据；JSONL 的一个物理行边界与 argv 参数内换行不是同一对象。日志展示要转义控制字符、限制 ANSI/OSC 影响，保留安全来源引用，而不是将终端控制序列原样播放给操作人员。

#### 4.2.8 Windows、WSL 与 CLI 内部 shell

原文要求 WSL 独立 Linux 预检，不在同一个 attempt 自动混用两套 Git、Python 和路径。官方 WSL 支持跨系统互操作，**项目禁止自动混用是本系统的受控执行策略，不是 WSL 技术上不能互操作**。[^P05][^W12]

仅靠路径前缀不能可靠识别环境：`/mnt/c/...` 不自动等于非法，普通文本参数里出现 `C:\...` 也不等于正在启动 Windows 进程。应按字段语义判断：执行入口、worktree、Git/Python、挂载来源和进程类型均需匹配已锁定环境。响应正文、日志中的路径作为数据，不进行全字符串误拦截。

建议首期拒绝自动调用 `wsl.exe`、WSL 内自动调用 `.exe`、Git Bash/MSYS 路径自动转换等跨环境执行链，除非有单独注册并审定的环境适配方案。工作目录位于挂载盘时，单独核验文件系统能力；不能因为 platform=linux 就按 Linux 本地盘的权限/大小写推定。

CLI 内部工具能力由 04 提出并给出证据，05 核验可用环境。`dispatch_shell=pwsh` 不会把 Pi 的 Bash 工具改为 PowerShell；缺所需 Bash 时阻塞相应用途，不自动安装、不改工具名、不删掉必要检查。

### 4.3 参数和进程监督

#### 4.3.1 监督接口与责任划分

建议接口为显式依赖注入，而非全局“当前 run”：

```text
build_launch_plan(locked_input, environment, policy) -> FinalLaunchPlan
validate_launch_plan(plan, resource_reader, current_guards) -> Diagnostics
accept_handoff(operation_id, plan_ref, controller_generation) -> HandoffReceipt
start_and_supervise(plan, receipt, cancellation_source, evidence_sink) -> TransportSummary
inspect_run_identity(run_ref, observed_environment) -> RecoveryInspection
stop_owned_scope(inspection, maintenance_authorization) -> CleanupReport
```

`build_launch_plan` 不启动进程；`start_and_supervise` 不能主动选择 profile；`inspect_run_identity` 只读；`stop_owned_scope` 是独立有副作用动作，不能藏在查询接口里。资源读取、I/O sink、身份观察和 platform backend 均为受信依赖，不允许普通 adapter 注入任意可执行回调。

原文要求 argv 边界经专门回显验证、日志记录平台/入口/shell、分别记录启动失败、自然非零、超时和取消；这些行为在本版状态机中保持不变。[^P05]

#### 4.3.2 四组状态分开建模

| 状态域 | 建议状态/事实 | 不可混淆的含义 |
|---|---|---|
| 准入与 handoff | ADMITTED → HANDOFF_ACCEPTED → STARTED；另有 CANCELLED / EXPIRED / START_UNKNOWN | 由 03/07/05 协调，不代表运行完成 |
| 进程监督 | VALIDATED → STARTING → RUNNING → STOPPING/DRAINING → FINALIZING → TERMINAL | supervisor 的观察状态，不覆盖原 attempt 状态 |
| I/O 与证据 | 每流 OPEN / EOF / READ_ERROR / TRUNCATED；证据 STAGING / SEALED / INCOMPLETE | EOF 与证据完整不是同义词 |
| 清理与验收 | scope_empty_verified / residual_detected / unknown / failed；检查 SATISFIED/FAILED/INCOMPLETE | 范围清空不代表全系统无后代，更不代表候选 PASS |

最终记录 `started` 使用 true / false / null：false 只用于有可靠证据证明未创建目标进程，null 表示不知道，不能以 false 隐藏 START_UNKNOWN。Windows 创建了 suspended 目标进程但未恢复，也已经是“目标进程存在”，需另记 `application_resumed=false` 的观察，不当作从未产生 OS 对象。

建议转移表：

| 当前状态 | 输入/守卫 | 下一状态与必须写出的事实 |
|---|---|---|
| VALIDATED | 当前准入无效或提前取消 | NOT_STARTED；禁止 OS 创建 |
| STARTING | API 明确拒绝、无进程句柄 | START_ERROR；记录 OS 错误代码和阶段 |
| STARTING | 创建结果无法判明或证据丢失 | START_UNKNOWN；封锁盲目重发 |
| STARTING | 进程身份、监督范围和启动事实已取得 | RUNNING；绑定实际身份引用 |
| RUNNING | 正常观察到主进程退出 | DRAINING；保留原退出值，继续排空与后代核验 |
| RUNNING | 超时/取消/输出限额/采集错误 | STOPPING；锁定首个停止原因并继续采集 |
| STOPPING | 优雅期限届满且仍有受控进程 | 强制停止并等待；记录动作/结果 |
| DRAINING | 后代或管道长时间未完成 | 按已审策略清理；证据不足则 INCOMPLETE |
| FINALIZING | 已封存全部可得事实 | TERMINAL；即使失败也必须输出具体缺口 |

这些是新增内部状态，不擅自修改 01 所要求保持的旧 attempt/verdict 名称。

#### 4.3.3 时限定义与单调时钟

至少分开记录：排队时间、预检时间、启动 API 时间、运行时限、优雅停止、强制回收等待、I/O 排空、证据封存。`timeout_seconds` 首期建议从进入本次受控启动调用时开始计时；排队和前置预检另有工作流期限。

```text
t0 = monotonic_ns()                   # 已准入、开始本次启动尝试
run_deadline = t0 + runtime_limit
first_stop_at = 第一次接受停止原因时刻
force_at = first_stop_at + grace
reap_deadline = force_at + kill_wait
# 主进程已退时，drain 起点按已审规则记录；不得被每块输出无限续期
```

使用单调时钟计算本次进程内相对时长，UTC 用于审计显示。Python 对 monotonic 的定义不支持用它作为跨重启的通用持久时标；恢复时不得把旧 monotonic 数值减当前值。[^W13] 机器睡眠是否计入某个 OS 时钟、恢复后如何执行墙钟总截止，要按实际平台时钟能力和策略单独验证，不能默认全平台等价。

Python 文档指出某些平台的初始进程创建无法被 timeout 立即中断。[^W01] 因此声明的是监督触发期限和可观测滞后，不承诺任意 OS 状态下“第 N 秒精确无进程”。若 API 返回时期限已过但进程确已创建，立即进入停止/清理，不伪装成从未启动。整体封顶要求更强时，需独立 supervisor/watchdog，不能只把 Popen 包在线程里就宣称能撤销创建。

建议起始参数：运行 300 s（由合同明确）；grace 2 s；kill-wait 5 s；drain 2 s。probe 继续受 04 更小限额约束。所有值是设计初值，须测量实际 CLI 输出/退出特性，不是性能 SLO 或已经验证的通用默认。

#### 4.3.4 自然退出、取消与超时的竞态裁决

建议由单一监督循环顺序化事件。每个已接受事件有 run 内递增 seq、单调观察时间与来源；不使用不同线程写出的墙钟时间假装精确先后。

- 主进程退出已被接受后，晚到的取消记录为 `cancel_after_exit`，不改写既有退出事实；仍可能影响尚未完成的后续业务动作。
- 首个超时/取消已被接受，随后进程优雅返回 0，也保留停止原因；不能改为自然成功。
- 同轮检测到多种可接受停止条件时，本版建议顺序为：已有持久停止决定 → 已排入队列的取消 → 本轮超时；该顺序是本系统选择，不是 OS 的真实因果证明。均保留观察事实。
- 任意时刻发生采集失败或证据缺失，都会降低完整性；不能因为主停止原因是 cancel 就删除后来的 read_error。

`StopLatch` 只固定首个控制停止原因，后续诊断追加保存。时间线应允许表述“先观察到取消，随后观察到退出”，不武断断言子进程在物理时间上一定先执行了哪个动作。

#### 4.3.5 stdout、stderr、stdin 与背压

生产 runner 必须并发处理 stdout、stderr 和必要 stdin，不能先 `wait()` 再读取满管道；普通 `communicate()` 适合有界结果，但会将输出收集到内存，不能直接充当无限流日志管线。[^W01] asyncio 文档也明确区分 communicate、wait 以及平台事件循环支持。[^W24]

建议结构：

```text
stdin writer → 有界写入/明确 EOF
stdout reader ─┐
               ├─→ 有界块队列 → 脱敏/计数/分段封存 → 04 parser 或检查视图
stderr reader ─┘
监督循环：身份与进程状态 / 取消 / 时限 / 采集健康 / 限额 / 清理
```

细化要求：

| 场景 | 必须行为 |
|---|---|
| 两条流同时大量输出 | 两条流持续排空，按 stream 各自记录 offset；不合并 stderr 到协议 stdout |
| 无换行或超长行 | reader 按字节块读，不等整行无限增长；04 的帧上限独立执行 |
| stdin 阻塞 | 不妨碍 stderr/stdout 读取与取消；写入受时限和上限约束 |
| 子进程提前关闭 stdin | 记录 broken pipe；是否构成协议失败由输入协议/04 判断，不能无条件正常 |
| sink 慢或队列满 | 停止继续派遣，受控停止当前运行；不能把日志块默默丢掉 |
| 日志盘满/权限变化 | 停止正常成功路径，保留可得错误、已封存前缀和未保存范围 |
| 输出超限 | 记录已观察与已保留字节，触发停止；后续排空可丢弃但必须计数，未读尾部标为未知 |
| reader 自身错误 | 标记该流 READ_ERROR，触发监督处置，不把 reader 线程退出当 EOF |

**计量区分**：`observed_bytes` 是从 OS 读取到的字节；`sealed_bytes` 是脱敏后封存字节，二者可能不同；`dropped_observed_bytes` 只计确知丢掉的输入字节；根本没读到的尾部用 `unobserved_tail=true`，不能伪造精确丢失长度。

脱敏必须处理跨块匹配，变换后的 byte offset 以封存结果为准。块级简单 replace 无法保证跨块秘密不泄漏。stdout 协议依赖字段被脱敏时，保留脱敏说明和审计限制；04 不能拿删改后的关键内容继续宣称完整验证。

默认首期建议 stdout/stderr 各 8 MiB，单读块 64 KiB，内存队列容量由 worker 并发与预算限定；04 probe 的“合计 8 MiB”限制必须额外保持，不能因为每流各有额度而翻倍。超限策略是 fail-closed 停止，不是继续跑完并丢掉大部分审计流。

#### 4.3.6 POSIX 后端：进程组不等于强隔离

建议复用现有实现时优先使用受控的新 session/process group，让正常子进程归入本次监督范围；Python 平台支持时使用 `start_new_session`，避免在多线程程序中引入不安全的复杂 `preexec_fn`。[^W01]

正常停止算法建议：

```text
冻结停止原因与 run 关联
核对 PGID/运行身份及当前拥有关系
对本次拥有的进程组发送 SIGTERM
持续读取输出，等待主进程与已知后代
到达 grace 后仍未结束：对可确认仍属本 run 的范围发送 SIGKILL
回收本进程直接子进程，检查已知后代/组成员及证据流
生成 CleanupReport，明确观察范围、残留与未知项
```

`kill(..., 0)` 成功只说明存在且当前可发送信号，并不证明身份、没有竞态或已经停止；没有权限也不等于进程不存在。[^W16] 不允许使用 `kill(0, ...)`、猜测当前 PGID 或无归属 `pkill -f` 作为恢复清理。

Linux `setsid()` 可以创建新 session 和进程组，后代可以脱离原组；原组变空不能证明所有衍生进程都已结束。[^W14] 文档因此区分 `scope_empty_verified` 与“完整后代清除”。出现已知 setsid/daemon 脱离时，应记录 residual/coverage gap，不让组级成功掩盖它；需要严格限制脱离的合同必须提供更强受控运行环境。

Linux 父死亡信号受父线程、fork/exec 等具体语义影响，不是自动覆盖所有后代的清理工具；macOS 也不能借用 Linux 专属能力冒称同样实现。[^W15] 更强方案可采用经批准的独立 supervisor、服务管理或隔离范围，但须单独设计/授权/测试，本版不默认引入。

组长退出后不能继续以 `poll()==None` 作为是否清理后代的唯一条件；也不能仅因旧 PGID 数字相同就永远向它发信号。观察范围失去可信关联时转人工恢复，不以误伤其他进程换取“清理完成”。

#### 4.3.7 Windows 后端：Job 建立、绑定与句柄生命周期

Job Object 可以将关联进程作为一组管理；默认子进程归属受 breakaway 等机制影响，`KILL_ON_JOB_CLOSE` 的触发条件是**最后一个 Job 句柄关闭**。[^W02] 因而必须验证机制真正生效，而非只写入某个 flag。

**优先方案建议**：在目标 Windows/API 版本支持并实测后，使用创建期 Job 关联，结合 suspended 创建、防止过早执行、只允许必要句柄继承。官方 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 提供创建属性中的 Job 关联；HANDLE_LIST 是另外的继承控制属性。[^W25]

**兼容方案建议**：`CREATE_SUSPENDED → AssignProcessToJobObject → 核验 → ResumeThread`。创建挂起和恢复线程分别有明确 API 语义，加入 Job 也可能受已有 Job/限制影响而失败。[^W03][^W17][^W18]

```text
创建独立受控 Job，并设置 kill-on-close / 禁止已知 breakaway
配置必要句柄白名单；Job 句柄不得被目标继承
创建挂起目标进程（优先创建期关联 Job）
若不是创建期关联：立即 AssignProcessToJobObject
核验进程归属、OS 身份、当前控制代际和可恢复记录
所有守卫仍满足 → 恢复主线程 → 发布启动观察
任一步失败 → 不恢复；终止已创建对象、关闭句柄、留证
```

**重要剩余窗口**：先创建 suspended、后 Assign 的兼容路径，若调度器恰好在两者之间被强杀，尚未入 Job 的挂起进程可能遗留。它通常尚未执行应用逻辑，但仍是遗留进程/资源，不能把“suspended”写成“绝无残留”。高保证合同应要求已验证的创建期绑定或外部 broker；兼容路径必须明确该限制并验收恢复。

原生后端须持有有效的 process/thread handle，不能只给普通 Popen 加一个挂起 flag 就假定已经获得可靠恢复与清理接口。Job 绑定失败时不可静默退化为只杀父进程；CI 已有 Job、权限不足、嵌套限制、资源限制等均应先 fail-closed 留证。

| 细节 | 验证要求 |
|---|---|
| Job 句柄所有权 | 子进程/旁路服务不持有意外副本；记录合法持有者，避免强杀 supervisor 后最后句柄仍未关闭 |
| 嵌套 Job | 实际 CI/终端宿主中验证，不因 OS 版本支持嵌套就假定本机所有限制允许 |
| breakaway | 不主动开启；已知外部服务/WMI 等旁路能力另列，Job 不是全机进程沙箱 |
| 优雅终止 | 仅对已验证共享控制台/组的进程用相应事件；不能默认 headless CI 可用 |
| 强制终止 | Job 范围强制停止后等待并复查；不能把 API 调用成功直接当完成 |
| Job 结束观察 | 联合主动查询、进程句柄、成员观察和超时；不把某条通知未收到解释成事件没发生 |

控制台事件只作用于满足相应控制台条件的进程组，CTRL_C 与 CTRL_BREAK 行为也不同。[^W19] 不向整个共享控制台广播信号来清理一个 run。缺优雅通道时标记 `graceful_stop_unavailable`，在已审策略允许下直接进入 Job 强制终止，而不是谎报“已优雅退出”。

#### 4.3.8 主进程已退出，后代仍存活或管道未 EOF

必须同时处理两个不同情况：后代持有 stdout/stderr 写端而导致 EOF 延迟，以及后代关闭管道但仍在后台运行。只等待 EOF 或只查看主 PID 都不足以覆盖这两类。

本版建议普通检查与模型调用默认采用 `no_background_children`：在已审的短暂结束宽限后仍有受控后代，记录 `residual_after_exit` 并执行清理；即使清理成功也不把这次异常当作普通正常退出。确需持久服务时，另设计可领取、续租、取消和回收的服务对象，不能隐藏在一次普通检查中。

| 主进程 | 两流 | 监督范围 | 处理 |
|---|---|---|---|
| 已退 | EOF 完整 | 已核验清空 | 进入封存和检查断言 |
| 已退 | 未 EOF | 后代持有句柄/未知 | 有界 drain；核对后代；停止/超期后标 INCOMPLETE |
| 已退 | EOF 完整 | 已知后代存活 | 执行后代策略；不得提前候选通过 |
| 已退 | EOF 完整 | 进程归属无法证明 | cleanup=unknown，阻塞要求清理证明的路径 |
| 未退 | EOF 完整 | 进程仍活 | 继续监督，不能以无输出结束调用 |

监督器不能无限等待继承的管道；drain deadline 不能因每次来一字节而无限延长。强制关闭 reader 是截断，不是自然 EOF。生产线程/事件循环要有可停止 I/O 设计；不能让不可中断的 reader 线程阻塞整个调度器退出。

#### 4.3.9 进程身份与恢复核对

恢复身份建议至少绑定：`host_id`、boot 标记、execution_environment_id、run/operation、PID、创建标记、入口链引用、协调器代际及监督范围关联。Linux 的进程创建标记、Windows 创建时间、macOS 可用身份 API 要按后端规范说明精度和限制，不能用粗粒度 `ps` 文本取代稳定句柄。

Linux pidfd 可提供对特定进程的稳定引用；它不等于整个后代集合，也不自动成为重启后可恢复的序列化句柄。[^W20] Windows process handle 与数字 PID 具有不同生命周期。[^W21] 建议活动监督期间保留可用的稳定句柄，恢复时重新核验当前对象；不能将旧 JSON 中的整数“handle”当成跨进程可用句柄。

```text
identity verdict = MATCH / MISMATCH / UNKNOWN
MATCH    → 仅表示必要身份比对满足；仍需当前维护授权和受控句柄
MISMATCH → 禁止终止该对象，标注可能复用或环境变化
UNKNOWN  → 只读取证/人工升级；不能凭运行目录名或进程名猜测
```

同账户环境下 run marker、命令行和日志可被伪造；这些只能作为关联证据，不是密码学身份认证。强对抗环境必须另行建立 OS 权限隔离和受信 supervisor，本文不把环境变量 `RUN_ID` 当作安全边界。

#### 4.3.10 准入、启动与崩溃窗口

沿用 03 增强版：一次性准入绑定 operation/run；相同键重试返回已有状态；真正重试由 07 新建 run，仍归属同一 request。`HANDOFF_ACCEPTED` 不代表 `STARTED`，不确定窗口进入 START_UNKNOWN。[^P03E]

建议以唯一 `(operation_id)` 登记 final_plan_ref 和请求指纹。同键同内容查询原状态；同键不同 plan 直接冲突。handoff 必须核对当前 controller generation、授权 epoch、审核、环境锁、取消状态和预算准入，不能只看首次通过的缓存。

| 崩溃点 | 可能事实 | 恢复规则 |
|---|---|---|
| plan 未封存 | 部分临时文件，无可消费引用 | 不启动；保留/补偿 staging |
| handoff 前 | ADMITTED，未受理执行 | 核对当前守卫后取消或正常消费；不复活过期准入 |
| handoff 已记、创建尚未知 | 不知道 OS 调用是否发生 | START_UNKNOWN；调查而不是重发 |
| 已创建，身份记录未完成 | 可能有受控/挂起进程 | 按 OS 句柄/外部监督与 run 证据恢复；身份不足隔离 |
| 已退出，结果未封存 | 已有部分流和清理事实 | 完成证据封存，不重新执行“补一个成功结果” |
| 结果已提交，响应丢失 | 有固定 summary/ref | 查询原结果，不重复启动 |
| controller 重启 | 旧 generation 的在途对象仍可能存活 | 冻结新派遣、核验旧对象；代际号本身不能杀死旧进程 |

数据库、文件发布、OS 创建和网络副作用没有天然原子事务；本版提供重复拒绝、恢复判定和明确未知状态，**不承诺一般进程执行 exactly-once**。已经发出的模型请求或完成的文件写入不能被取消记录“撤销”。

#### 4.3.11 退出事实与调度结果

| 事实 | 存储与解释 |
|---|---|
| POSIX 正常退出 | 保存 API 观察的 returncode；预期范围由合同定义 |
| POSIX 信号终止 | 保存负 returncode 与独立 signal；不改为 shell 的 128+signal 伪装原码 |
| Windows 状态 | 保存观察 API 的原值、无符号 32 位视图和来源；负整数不套用 POSIX signal |
| 明确启动失败 | returncode=null，保存 stage/errno/winerror；没有子进程退出事实 |
| 启动结果未知 | started=null、START_UNKNOWN；不能填一个猜测退出码 |
| 超时/取消 | 独立 stop_reason，与最后原退出值并存 |
| wrapper 退出 | 记录 wrapper 层与被调用程序层；不把 wrapper 0 代替目标状态 |

`expected_exit_code=7` 可以让一次正常返回 7 的专用检查满足合同，但**不能**放行超时、缺日志、身份不足或未知清理。Windows 异常状态不作为首版可移植 expected_exit 的扩展通道，需要专项检查时另设经过审核的协议。

01 要求保留旧入口 0/1/2/3/4 及 wait 语义。本次没有读取旧退出码映射的源码，因而不为 1/2/4 重新命名；只明确 `wait=3` 为原文要求保留的值，`wait` 完成不等于候选 PASS。[^P01]

#### 4.3.12 TransportSummary、封存与保密

每个 run 最终应有一个不可变总结版本，后续恢复只能追加新观察/补充视图并引用旧结果，不原地修写旧 summary。至少包括：来源 plan、启动三态、环境/进程身份引用、原退出值、停止原因、两条流的独立计数/EOF/错误、清理 scope 与结论、单调耗时及结构化诊断。

日志段先完整写入受控临时对象，关闭/按存储承诺同步，再发布不可变版本并登记引用。文件发布与数据库提交分别处理失败；已发布无索引的孤儿不得被自动当作正式证据。OS rename 的原子性、断电持久性、网络文件系统行为不混为一谈，生产必须实测。

默认不保存未脱敏全量原始秘密；正文、凭据、终端控制序列和错误文本走既有安全处理。需要独立保留受限原始证据时，应有单独授权、访问控制、加密与保留期，而不是“为调试方便”追加 raw_env.json。

检查报告只能引用封存资源；stdout/stderr 同一事件被多次读取、渲染或解析不会变成多次执行或多次计费。05 输出事实供 04/07 使用，不重复汇总 vendor token 或将 unknown 费用归零。

#### 4.3.13 错误合同与修复方向

沿用 01 的 `code/message/details/retryable`。错误细节列安全字段位置、阶段、引用和建议动作，不回显原始 argv、环境值、令牌或完整异常对象。

| 建议错误 | 阶段 | 处理方向 |
|---|---|---|
| `E_PLATFORM_COVERAGE` / `E_CARRIER` | 静态校验 | 补齐载体/平台，重新锁定检查集合 |
| `E_ENTRY_UNTRUSTED` / `E_ENTRY_DRIFT` | 入口核验 | 固定真实链、重新预检/审核；不猜测下一个 PATH 项 |
| `E_SHELL_MISSING` / `E_SHELL_VERSION` | 环境预检 | 阻塞该用途；安装或变更单独授权 |
| `E_ARGV_UNREPRESENTABLE` / `E_ARGV_LIMIT` | 平台转换 | 调整正式输入协议；不丢参数、不自动换 shell |
| `E_CWD_ESCAPE` / `E_CWD_DRIFT` | 路径核验 | 核实 worktree 归属、权限与链接；禁止继续 |
| `E_ENV_COLLISION` / `E_ENV_FORBIDDEN` | 环境构建 | 修复明确白名单和来源，不日志输出值 |
| `E_JOB_ASSIGN` / `E_CONTAINMENT_REQUIRED` | 创建/监督 | 不恢复未受控目标；清理已创建对象并保留失败 |
| `E_SPAWN` / `E_START_UNKNOWN` | 启动 | 区分已知未启动与未知；未知禁止自动重发 |
| `E_RUNTIME_TIMEOUT` / `E_RUNTIME_CANCELLED` | 运行 | 执行已审停止策略，记录真实结束与清理 |
| `E_OUTPUT_LIMIT` / `E_CAPTURE_WRITE` / `E_DRAIN_TIMEOUT` | 采集 | 保留前缀与缺口，停止正常通过路径 |
| `E_PROCESS_IDENTITY` / `E_CLEANUP_UNKNOWN` | 恢复/清理 | 只读调查与升级，不猜 PID |
| `E_EVIDENCE_SEAL` | 封存 | 不产出可消费的完整结论；补偿发布/索引故障 |

上述 code 需登记到公共错误表。附录参考仅覆盖其中部分代码，不声称生产错误注册已完成。`retryable=true` 说明可能是暂时故障，不自动授予新进程、模型调用、预算或重跑许可。

## 5. 预计文件范围

### 5.1 原文保留与建议职责拆分

下列路径沿用原文的命名方向，不表示本次已经修改项目仓库。正式合同必须列出确切文件，不能用整个 `scripts/` 或整个 `.agent/` 作为无限范围授权。

| 路径/范围 | 建议职责 | 必须保留或单独核验的内容 |
|---|---|---|
| `scripts/dispatch.py` | 中立入口；解析调度命令并调用核心 | 不复制 Pi 业务逻辑；旧退出码映射由源码取证决定 |
| `scripts/dispatch.sh` | 已核验 Python 的薄引导与参数转发 | Bash 3.2 单独实测；不依赖未声明现代语法 |
| `scripts/dispatch.ps1` | 显式 PowerShell 版本下的薄引导 | 转发模式、原生参数往返、保存并退出 `$LASTEXITCODE` |
| `scripts/pi_dispatch.py`、`scripts/dispatch-pi.sh` | v1 兼容入口及逐步迁移 | 保留原路径、参数、wait 与失败行为，不读取 v2 后降级执行 |
| `scripts/codekit/platforms.py` | 平台/环境身份、shell 能力、入口解析 | WSL、架构、实际文件系统事实不能只靠字符串猜测 |
| `scripts/codekit/process.py` | FinalLaunchPlan、监督接口与公共生命周期 | 不导入 vendor 参数或发布审核；不把所有异常压成退出码 1 |
| 平台后端模块，具体路径待定 | POSIX group / Windows Job 的创建、停止、身份和复核 | 共享接口不等于共享所有 OS 实现；能力不足必须阻塞 |
| 命令 Schema、合同渲染器 | 共同字段、完整变体、唯一选择、同源展示 | v1/v2 明确分派；未知字段不能偷偷丢掉 |
| `tests/` 的离线与原生场景 | 静态契约、回显、流、清理、故障矩阵 | 旧回归保留，新增测试注明合成/真实来源及平台 |
| 环境说明、源技能、AGENTS | 中立入口、临时目录、平台与权限边界 | 受保护改动由主模型在后续获准范围内直接处理 |
| CI 定义 | 仅在明确授权后增加原生矩阵 | 不能把阅读本文理解为同意安装 runner、上传凭据或修改工作流 |

当前 AGENTS 含 POSIX 临时目录写法是**原文记载**；未取得真实规则正文，不在本文构造替代版本。迁移时应分别列出临时目录分配、权限、清理归属、路径呈现和历史证据保留规则，不把 Bash 的 `/tmp/...` 字符串机械换成 PowerShell 字符串。[^P05]

### 5.2 生产文件与参考文件必须隔开

附录的 `runtime_contracts.py`、`fixtures/process_stub.py`、`wrappers/dispatch-stub.*` 是独立演示包。它们没有真实审核、预算、凭据和 Job 后端，**不能复制到生产入口后直接宣告迁移完成**。其中 Bash/PowerShell wrapper 调用的是合成 stub，不调用项目 `scripts/dispatch.py`。

建议在正式仓库把可信执行核心纳入既有保护范围；普通 executor 不能通过新增同名模块、修改加载路径或改 Scope 绕过保护。Python 模块的导入位置、运行目录和包来源也要纳入引导信任，不能因为入口脚本已锁定就默认所有导入均受控。

## 6. 实施步骤

### 6.1 原有实施顺序与交付门槛

原文六步保留为主线：先保留进程/argv 回归，确定模型与平台覆盖；再实现中立入口和薄 wrapper；接 04；运行无网络子进程场景；三平台原生验收；获准后再验证真实 CLI/平台组合。[^P05]

| 阶段 | 具体动作 | 阶段产物 | 进入下一阶段的条件 |
|---|---|---|---|
| S0 基线取证 | 固定 HEAD、脏工作区、相关未跟踪文件、原入口、工具链和测试范围 | 基线报告、旧行为/失败类别映射、环境缺口 | 已知原行为；没有把当前失败归咎于尚未实施的新模块 |
| S1 静态协议 | Schema、严格 JSON、平台唯一选择、字段来源、同源渲染、环境名称校验 | 正反例和语义黄金向量 | 关键拒绝发生在任何目标进程创建之前 |
| S2 中立入口 | 提取业务入口；分别实现与测试 `.sh` / `.ps1` | argv 回显与退出码差分、兼容入口报告 | 空项、中文、引号、CRLF 数据与旧 wait 值均保持 |
| S3 最终调用计划 | 接 02 的环境计划与 04 Invocation；封存完整入口链和输入绑定 | FinalLaunchPlan、EnvironmentRecord、审核输入清单 | 无可变名称二次跟随；无模型/参数/环境越权覆盖 |
| S4 平台监督 | 建立监督状态、管道、单调截止、取消、分级停止与清理证据 | 各平台真实进程报告、故障结果、残余风险 | 不以父进程结束代替范围清空；未知状态不被正常通过 |
| S5 准入与恢复接线 | 接 03/07 handoff、幂等、代际、恢复隔离和证据发布 | 跨模块故障注入与恢复报告 | START_UNKNOWN 不重发、已退出不重跑、旧协调器被隔离 |
| S6 真实平台与 CLI | 原生三平台同套场景；再按获准的 CLI/build/model/profile 验证 | 联合支持矩阵、G0–G6 原回归、发布/回退记录 | 采用的必需验收项全有证据；未测组合不宣称支持 |

以上阶段是实施顺序，不是自动授予下一阶段权限；真实模型调用、安装依赖、修改 CI 与受保护代码仍以现有业务授权和正式合同为准。

### 6.2 基线取证操作清单

在**已获准的实际项目环境**执行。本文未运行以下项目命令；输出可能包含路径或代码差异，应按现有保密规则归档。

```text
1. 记录实际项目路径、当前执行环境身份、解释器路径与版本。
2. 记录完整 Git HEAD / 对象格式，以及相关工作区和未跟踪文件状态。
3. 从源码提取旧入口 0/1/2/3/4、wait、超时、取消、清理和 shim 的真实规则。
4. 固定需要保留的旧 fixture 与测试哈希；不改 fixture 掩盖现有失败。
5. 按已核验的 Python 入口运行：python tests/test_dispatch.py
6. 分别记录退出码、完整日志、实际开始/结束、未运行项和环境限制。
7. 测试后复核相关输入有无变化，记录漂移而不是继续使用测试前摘要。
```

原测试命令的 `python` 是说明性名称，应替换为本机已核验入口的单一 argv 项；不要用未经审定的下载器、自动安装器或 PATH fallback 替代缺失解释器。上述 baseline 与附录验证包是不同测试对象。

### 6.3 本地原生测试的安全要求

所有子进程必须来自固定合成 fixture 或已核验工具；使用专用临时目录和最小环境，不在用户仓库里做强杀、死循环、随机 PID 清理或破坏性磁盘填充测试。故障注入应有外部 watchdog，并明确只管理测试拥有的进程/Job/目录。

普通回归可先覆盖受控范围内的超时与取消；**supervisor 被强杀、挂起窗口、setsid 逃逸、磁盘故障**属于单独隔离场景，不能为了证明功能而冒险留下未知后台任务。每次故障测试结束都应由独立观察器记录残留和回收情况。

### 6.4 实施报告最小模板

```text
run / request / operation:
项目基线与相关输入摘要:
测试代码/fixture 来源：项目真实 / 厂商捕获 / 本版人工合成
实际平台、架构、环境种类、shell 和工具版本:
采用的检查集合与 final_plan_ref:
授权/审核/准入来源（安全引用，不放凭据）:
原生执行或纯函数模拟:
启动事实、停止原因、原退出值:
stdout/stderr EOF、已观察/封存字节、缺口和脱敏版本:
进程身份、监督范围、终止动作、残留与未知项:
本次检查结果：SATISFIED / FAILED / INCOMPLETE / ABORTED / NOT_RUN
对应原验收编号与新增编号:
未运行原因、仍需真实机器验证的组合:
结果与日志资源引用:
```

状态文本须与既有项目术语映射。本版新增状态是为描述参考检查，不直接更名原 attempt / verdict；生产映射必须通过旧行为差分。

## 7. 验收标准

### 7.1 原验收编号完整保留

以下十项沿用原文，未将本次参考测试结果填入为已验收。[^P05]

- **05-A01**：原生 Windows 无 Bash 可跑中立入口、argv 检查和 stub 调度流程。
- **05-A02**：Linux/macOS 原生跑通同套核心场景；缺机器时明确标为未验收。
- **05-A03**：argv 与 shell 互斥、平台缺失、变体覆盖共同语义均在运行前拒绝。
- **05-A04**：空格、中文、引号、空参数、特殊字符往返一致；不安全 shim 参数被拒绝。
- **05-A05**：wrapper 保持程序非零退出和 `wait=3`；语法错误与程序失败留下准确记录。
- **05-A06**：正常超时/取消回收受监督进程树，保留超时、取消和清理状态证据。
- **05-A07**：调度器强杀单独验证；POSIX 潜在残留有检测/恢复说明，无全回收虚假承诺。
- **05-A08**：Windows/WSL 混合路径与环境被拒绝；切换环境需要新锁和预检。
- **05-A09**：机器合同与 Markdown 命令展示同源，CRLF/Unicode 不改变 argv 参数边界。
- **05-A10**：缺 CLI 内部必需 shell 时明确阻塞该用途，不因调度器可运行就报告完整支持。

### 7.2 新增建议验收项

采用这些建议前应纳入正式合同；它们不是对原验收项目的静默替换。

| 编号 | 必须证明的结果 | 最低证据层次 |
|---|---|---|
| **05-A11** | CommandSet 全量校验先于单平台选择；optional 命令不能掩盖载体错误 | 静态正反例 + 核心零启动断言 |
| **05-A12** | 固定参数、入口脚本/模块、解释器及环境绑定分别核验；变化有准确失效原因 | 真实入口链漂移测试 |
| **05-A13** | Windows 名称大小写冲突、未许可 loader 环境和明文凭据不会进入执行/日志 | 平台原生测试 + 脱敏负例 |
| **05-A14** | PowerShell 5.1 与 7.x 的支持声明分开；目标 EXE 空参数、引号等完整往返 | 指定版本的 Windows 原生回显 |
| **05-A15** | 三平台不存在 stdin/stdout/stderr 相互等待导致无限挂起；缓冲与排空有上限 | 双流压力、EOF、阻塞输入、超限故障 |
| **05-A16** | 首次停止原因不被后续 returncode=0 覆盖；时钟变更不使截止判断回退 | 状态机测试 + 原生竞态注入 |
| **05-A17** | Windows 进程在应用代码运行前满足受审 Job 归属；失败不降级 | 实际创建/归属/恢复线程证据 |
| **05-A18** | 挂起创建—Job 绑定窗口和 Job 句柄泄漏分别验证；保证范围如实呈现 | 外部监督下的强杀场景 |
| **05-A19** | 父进程退出但持管道/不持管道的后代仍存活时都不误判完整 | 两类真实后代 fixture |
| **05-A20** | `setsid` / breakaway / 外部创建不被误报为已覆盖；要求更强边界时阻塞 | 原生逃逸/限制验证，不靠 mock |
| **05-A21** | PID 相同但创建标记、boot 或环境不同，恢复拒绝终止 | 身份纯函数 + 隔离原生验证 |
| **05-A22** | 同 operation 同 plan 不重复启动；同 operation 不同 plan 冲突 | 持久账本并发/响应丢失测试 |
| **05-A23** | START_UNKNOWN 不自动重跑；状态调查与新 run 的预算归属清楚 | 03/05/07 跨模块崩溃恢复 |
| **05-A24** | cwd/脚本/入口在核验后被替换时有受控拒绝或明确能力不足 | 真实文件系统 TOCTOU 测试 |
| **05-A25** | stdout/stderr 丢失、日志写失败、脱敏失败分别阻断完整结论 | 故障注入 + 保密检查 |
| **05-A26** | Windows 原退出状态与 POSIX 信号分别记录，未知退出不补 0 | 原生退出样本 + 单元反例 |
| **05-A27** | 结果封存与索引失败可补偿；已完成运行不为补证据重执行 | 存储故障/重启验证 |
| **05-A28** | shell 启动资源、profile、参数模式和内部 Bash 能力按用途锁定 | 实际版本探测 + 资源加载检查 |
| **05-A29** | 升级/回退不改历史证据或绕过新审核，旧 wait/失败语义保持 | v1/v2 差分 + 回退演练 |
| **05-A30** | 支持矩阵按实际环境记录，缺机器/权限/fixture 时保留 NOT_RUN | 证据清单与发布检查 |

### 7.3 原生平台矩阵与“通过”的范围

| 组合 | 必需关注点 | 本次状态 |
|---|---|---|
| 当前容器 Linux + 当前 Python/Bash + 本版 fixture | 参数、普通退出、双流、stdin、受控组取消、TERM/KILL、Bash wrapper | 已运行的局部合成测试，详见附录 C |
| 用户项目的 Linux 原生目标环境 | 真实源码、03/04/07 接线、权限、清理与完整回归 | `NOT_RUN`：未取得项目仓库 |
| macOS 原生 + 实际系统/用户 Bash、zsh | Bash 3.2、信号、路径、管道、系统工具差异 | `NOT_RUN`：没有该测试环境 |
| Windows 原生 + 直接 Python/EXE，无 Bash | argv、Job、句柄、创建/退出、stub 调度流程 | `NOT_RUN`：没有 Windows 环境 |
| Windows + Windows PowerShell 5.1 | 旧参数模式、编码、薄入口或受控桥接 | `NOT_RUN`，不得沿用 7.x 声明 |
| Windows + 已指定 PowerShell 7.x | 版本/模式锁、空参数、引号、wait 3、异常退出 | `NOT_RUN`；附录 PS wrapper 未运行 |
| WSL 1 / WSL 2 | 独立 Linux 预检、interop 拒绝策略、路径与工具来源 | `NOT_RUN`；不是 native Windows 替代验收 |
| 真实 Pi / 第二 CLI × 任一目标平台 | 指定 build、模型、内部 shell、工具、协议、成本和门禁 | `NOT_RUN`：没有真实调用授权与项目接线 |

纯函数在 Linux 上选择出 Windows 的 argv，证明的是**选择算法**，不是 Windows 可以执行该 argv。当前容器测试不等于用户机器或原生三平台验收，也不验证所有进程树形态。

### 7.4 待执行的真实集成与故障场景

下表 30 项是**测试设计，全部未在真实项目执行**。每项须保存输入锁、外部观察、原始退出、清理/缺口和检查结果；不能用附录中的同名概念单元测试替代。

| ID | 注入/操作 | 成功判据与禁止的错误结论 |
|---|---|---|
| 05-I01 | required=false 的命令缺某个声明平台 | 锁定前拒绝；不能 skip 后通过 |
| 05-I02 | 变体加入 required/timeout 或同时声明 argv/shell | 拒绝共同语义覆盖和载体冲突；零目标启动 |
| 05-I03 | 审核后替换 python/node 后的脚本 | 入口链漂移；仅解释器 hash 未变不能继续 |
| 05-I04 | PATH 中增加更靠前同名程序 | 正式调用仍用已锁定入口或拒绝；不跟随新解析 |
| 05-I05 | Windows `Path`/`PATH` 冲突、NODE_OPTIONS 注入 | 拒绝或按已审白名单过滤；日志不显示值 |
| 05-I06 | cwd 的 symlink/junction 在核验后交换 | 拒绝或报告能力不足；不得实际进入未授权目录 |
| 05-I07 | argv 包含空项、双引号、尾反斜线、中文、CRLF、`%`、`&` | 真实目标回显字节/字符序列与锁定参数一致 |
| 05-I08 | 相同用例分别经 PS 5.1、7.x、cmd shim | 每条链独立出结果；不以一条通过覆盖其他链 |
| 05-I09 | wrapper 目标退出 3，紧接有日志输出需求 | 保存原 `$LASTEXITCODE` 并传播；不得被后续命令覆盖 |
| 05-I10 | Bash/PS 脚本语法错误与目标程序退出 2 | 记录阶段/诊断，不能仅凭同一个整数混淆原因 |
| 05-I11 | 父进程阻塞写 stdin，子进程同时大量写 stdout/stderr | 监督保持响应，无无限死锁；停止仍能清理 |
| 05-I12 | 单条无换行输出超过帧/流限制 | 有限内存、明确超限、已保留前缀和缺口；不正常通过 |
| 05-I13 | 日志写入 ENOSPC/拒绝访问，仍有子进程输出 | 记录采集故障并停止策略；不能只静默关闭记录 |
| 05-I14 | 秘密跨两个读取块或 UTF-8 字符被拆块 | 脱敏/解码有受控状态，无法保证时不泄露并标不完整 |
| 05-I15 | timeout 与 cancel 在相邻观察序号到达 | 首个停止原因保持，全部观察留证，不依墙钟猜因果 |
| 05-I16 | 超时后子进程捕获 TERM 并返回 0 | 退出 0 与 timeout 并存；本次仍非正常满足 |
| 05-I17 | 主进程退出，后代保持 stdout 写句柄 | 排空有截止，检测残留；不永久等待 EOF |
| 05-I18 | 主进程退出，后代关闭管道但继续修改文件 | 即使 EOF 也不能报告范围清空/候选稳定 |
| 05-I19 | 后代 setsid 离开原 POSIX group | 不声称 group 信号覆盖该进程；按更强策略或恢复限制处理 |
| 05-I20 | Windows Job 创建/设置/绑定/恢复线程任一步失败 | 不运行未受控目标；保留可确认的创建和清理事实 |
| 05-I21 | 在 Windows 挂起创建之后、Assign 之前强杀 supervisor | 由外部监督观察未绑定窗口；不能虚称 Job 自动覆盖 |
| 05-I22 | Job 句柄被后代意外继承，随后强杀 supervisor | 检出最后句柄条件不成立及残留；修复继承规则 |
| 05-I23 | CI 父 Job 的限制与子 Job 组合冲突 | 真实核验 nested 条件；失败不退化为只杀 PID |
| 05-I24 | POSIX supervisor 被 SIGKILL | 检测残留并保留本模块保证限制，不宣称全回收 |
| 05-I25 | 重启后同 PID 对应不同创建标记/boot | 拒绝终止；不因旧目录写着 PID 就动手 |
| 05-I26 | handoff 提交后、STARTED 登记前崩溃 | START_UNKNOWN 调查，禁止重复派生同一 operation |
| 05-I27 | 同 operation 并发重复请求/不同计划冲突 | 同计划返回既有对象，不同计划拒绝；无双启动 |
| 05-I28 | 输出已封存但索引提交失败/响应丢失 | 补偿或返回原结果；不重新执行检查 |
| 05-I29 | Windows attempt 调用 WSL launcher 或 Linux attempt 调用 Windows EXE | 未声明跨界路径被拒；切换环境需新锁和动作 |
| 05-I30 | v2 故障后尝试用旧入口/旧 schema 标签逃避审核 | 拒绝降级；合法回退保留历史与明确授权范围 |

### 7.5 发布门槛与例外处理

建议检查状态统一记录为 `PASS`、`FAIL`、`NOT_RUN`、`BLOCKED`、`NOT_APPLICABLE`，这里仅指**验收条目状态**，不是候选 verdict 的直接赋值。`NOT_APPLICABLE` 必须有事先确定的适用条件及批准来源；缺平台、缺权限、超时、日志不全不能事后改成不适用。

必需条目只有在相应实际平台/版本证据完整时才能 PASS。可选项未运行仍需列明影响；不得把分母减小后宣传“全部通过”。正式例外需指定范围、原因、期限、风险和批准记录，不得豁免原有权限/审核、真实模型核对或必要候选门禁。

## 8. 兼容与恢复

### 8.1 原文保留

旧 argv 合同保持语义，新 shell/平台结构由新版 Schema 解释。保留旧包装入口的参数和退出码，不要求一次性替换所有路径。执行中断保留证据与工作目录，恢复先确认进程状态，再由明确新动作重跑。平台或路径解析漂移需重新预检，不使用其他平台的通过记录。回退不自动改合同版本、不清理旧证据、不安装缺失 shell。[^P05]

### 8.2 值班恢复流程

本流程是操作步骤设计。`inspect/cleanup/reconcile` 等动作必须由后续正式实现提供；没有对应 CLI 的当前项目不能把名称当成可运行命令。**本版不提供对任意 PID 的一键清理脚本。**

| 步骤 | 操作 | 成功判据 | 失败/未知时的动作 |
|---|---|---|---|
| R0 隔离 | 通过受信调度入口暂停受影响环境的新派遣；保留当前日志与工作目录 | 已确认隔离范围与 controller generation | 无法阻断派遣时升级主控/运维，不并行试跑 |
| R1 读取事实 | 读取 operation、准入、final_plan_ref、身份、结果封存与最近守卫状态 | 引用完整、哈希可核验、来源明确 | 损坏或缺失标记，不以缓存/执行成功倒推审核 |
| R2 确认环境 | 比较 host、boot、执行环境、平台、入口与 worktree 归属 | 当前对象与原运行环境关系明确 | 已换机器/重启/路径漂移：新预检；旧证据保持 |
| R3 分类状态 | 区分明确未创建、START_UNKNOWN、仍运行、已退出未封存、已完成 | 每个 operation 进入唯一调查分支 | 只见 PID/文本报告不足：保持 UNKNOWN |
| R4 核对进程 | 使用仍持有的 OS 句柄或充分的原生身份/监督范围证据 | MATCH 且停止权限/当前归属有效 | MISMATCH 不终止；UNKNOWN 只读调查和升级 |
| R5 受控停止 | 依已审策略执行温和停止、宽限、升级与范围复核 | 每个动作有目标、身份、时间和结果；范围达到要求 | 仍有残留/观测不足：不宣告清理完成，阻断后续候选写入 |
| R6 完成证据 | 排空可读流、记录缺口、封存段、补偿索引；追加恢复报告 | 全部引用可读且状态真实，不覆盖旧结果 | 存储失败保持隔离；不要重跑来补日志 |
| R7 处理后续 | 已完成则返回原结果；需重试交 07 建新 run、复核当前审核/授权/预算 | 新旧 operation/run 关系明确，同 request 预算归属 | 不确定旧副作用时拒绝盲目重试 |
| R8 解除隔离 | 主控核对残留、证据、代际、受影响候选及新环境锁 | 明确授权解除，记录范围和时间 | 未满足任一关键条件则继续隔离并升级 |

恢复报告至少保存：操作者/授权来源、调查依据、原运行引用、当前身份观察、未证实事项、信号/Job 动作、清理边界、存储补偿、后续 run 关系及仍需人工处理的事项。报告本身不能代替实际 OS 观察。

### 8.3 典型恢复分支

**明确未启动**：只有在受信入口能证明 OS 创建未发生时，才允许记 `started=false`。后续执行仍需要当前有效准入；不复用已撤销或已消费许可。

**启动未知**：暂不重跑。优先从外部 supervisor、保留句柄、运行目录的受控记录和原生创建时间取证；不能找到足够证据不等于进程从未启动。费用/外发副作用由 04/07 对账，05 不能把未知费用改零。

**已退出但日志不完整**：保留退出事实和 incomplete，恢复能补读的证据并追加来源。原本未采集的字节不能靠模型报告“重建”为原始输出；补跑是新检查，不补写旧 run 的历史。

**已发现错误 PID**：停止清理动作，记录身份不匹配并调查 PID 复用/记录关联错误；不能继续按进程名批量寻找替代目标。

**存储回滚到旧备份**：先按 03 核对撤销/代际高水位与当前授权，保持隔离；旧备份中“仍为有效”的准入不能自动复活。已有记录的原字节恢复与修改事实内容是不同操作。[^P03E]

### 8.4 回退步骤与检查

| 顺序 | 操作 | 不允许的捷径 |
|---|---|---|
| 1 | 固定回退原因、受影响版本和回退目标，核验实际授权 | 把故障当成自动允许改模型/装依赖 |
| 2 | 暂停新派遣并调查/处理已有运行 | 不确认旧运行就并行启旧入口重跑 |
| 3 | 切回经验证的旧入口与明确旧配置，仅处理它支持的合同 | 给 v2 文件改标签，骗入 v1 |
| 4 | 保留新旧环境、合同、审核、日志与索引；追加回退事件 | 删除新增证据或重算历史哈希 |
| 5 | 重跑获准的无模型兼容用例，逐项核对旧参数与退出码 | 仅看到命令返回 0 就宣布旧门禁恢复 |
| 6 | 确认待处理合同重新路由与不支持项阻塞后解除隔离 | 将旧平台的历史 PASS 用作新环境证明 |

回退的成功判据是“明确范围内恢复已证明的旧能力且保留证据”，不是“所有新功能自动继续可用”。

## 9. 后续合同拆分建议

### 9.1 原四个工作包保留并细化

| 工作包 | 建议交付 | 必需输入 | 不包含的内容 |
|---|---|---|---|
| **入口与 argv** | 中立 Python、Bash/PowerShell 薄入口、回显矩阵、退出码差分 | 实际旧入口/规则、目标版本、固定测试基线 | 不同时迁移模型、审核和预算 |
| **命令模型** | CommandSet、载体互斥、平台覆盖、同源渲染、FinalLaunchPlan | 01/02/04 已审接口、旧合同 fixture | 不把新 Schema 标签倒写旧合同 |
| **进程与恢复** | 平台后端、管道、截止/取消、Job/group、身份与恢复演练 | 可用原生机器、故障测试隔离、03/07 handoff | 不承诺没有证据的全后代/强杀保障 |
| **规则与文档迁移** | 临时目录规则、源技能入口、支持矩阵、升级/回退说明 | 真实 AGENTS/源技能与受保护改动授权 | 不代替 CI 或安装依赖的独立批准 |

每包正式实施前需明确实际基线、文件 Scope、授权、profile、预算、可用平台、验收命令、失败升级和回退目标。离线测试不能替代原生验收；本清单不构成可直接派遣合同。[^P05]

### 9.2 必须由实际项目决定的未决项

| 未决项 | 本版建议起点 | 需要实际材料决定的部分 |
|---|---|---|
| Python/PowerShell 最低版本 | 按已证实参数与 API 能力选择；不同版本分别列证据 | 用户机器/CI 库存、既有兼容要求 |
| Windows Job 创建路径 | 优先核验创建时 Job 列表；兼容挂起路径披露绑定窗口 | 目标系统、Python 后端/原生绑定、父 Job 限制 |
| POSIX 保证级别 | 受控 group + 残留检测，非强隔离 | 是否要求 cgroup/外部 supervisor、macOS 对应方案及授权 |
| cwd/入口固定消费 | 静态 resolve 不足；受控目录权限与平台句柄设计 | 实际 FS、并发写权限、是否能提供更强无竞态后端 |
| deadline 与资源限额 | 正文给出设计初值，允许受审调小 | 真实 CLI 吞吐、日志规模、取消延迟和运维要求 |
| shell 启动配置 | 资源列举/关闭可控自动加载，锁定实际 profile | 目标 shell build、系统强制启动资源、CLI 内部行为 |
| 输出断言与失败归约 | 首期有界文本断言，多维完整性先于退出比较 | 原 check/verdict 的精准映射与必要门禁 |
| 标识与公共记录注册 | 引用 01/03/04 的建议结构，不重复权威来源 | Schema/error/kind 注册、数据库事务及版本发布流程 |

---

## 附录 A：结构契约与语义校验边界

### A.1 验证顺序

```text
受限字节输入 → UTF-8 / BOM / 深度 / 重复键 / 数字与字符串检查
 → 记录家族及版本 → JSON Schema → 跨字段语义
 → 资源注册、定位、完整字节哈希与依赖闭包
 → 平台/入口/cwd/环境实际核验 → 当前权限/审核/预算/一次性准入
 → 固定对象消费与平台执行 → 封存事实 → 04 / 检查层 / 原有门禁
```

Python 默认 JSON 解析允许重复键覆盖和某些非标准数值，因此“能 `json.loads`”不足以作为严格入口。附录使用重复键钩子、深度检查、非有限数拒绝和 Unicode 标量检查；固定输出继续沿用 01，**不是 RFC 8785/JCS**。[^W22][^P01]

JSON Schema 2020-12 的结构与数值语义不能代替平台执行语义。例如 JSON 的数值 `1.0` 可以满足 Schema 的 integer 判断，而本版命令语义要求超时等字段使用 Python 解析后的精确 int 表示；此差异由应用层拒绝。`format` 也不能仅因写进 Schema 就假设始终执行断言，参考验证器显式启用检查器。[^W23]

### A.2 四个有结构 Schema 的记录家族

完整文件见附录 B 的 `schemas/runtime.schema.json`，使用本地 `$defs`，不联网拉取不受信远端 Schema。

| 家族 | 主要内容 | Schema 之外仍要做的事 |
|---|---|---|
| `command_set` | 支持平台、命令共同字段、argv/shell/变体的联合结构 | 平台键精确覆盖、ID 唯一、类型表示、版本/路径/shell 适用性 |
| `final_launch_plan` | 源引用、环境/入口/cwd 绑定、完整 argv、stdin、后端与限额 | 实际入口链核验、来源参数不变、权限、exec_only 与 script 的关联 |
| `process_identity` | 本地标识、host/boot/environment、PID/创建标记、入口、group/Job | 身份来自受信 OS 观察、句柄归属、时间粒度与重启语义 |
| `transport_summary` | 启动三态、状态/停止原因、退出、两流、清理与证据 | 事实一致性、完整性推导、与实际启动/停止及资源的关联 |

`EnvironmentRecord`、`ExecutableBinding`、`CwdBinding`、`CleanupReport` 在本附录只有**合成载荷样例和资源摘要**，没有声称提供其生产完整 Schema。这些公共记录需要与 01–04 及真实平台一起定稿；不能因为样例可哈希就视为真实环境证明。

### A.3 必须另外实现的语义守卫

| 条件 | 必须拒绝/限制的组合 |
|---|---|
| `execution_mode=exec_only` | `script_ref` 非空、未经许可的 shell/shim 改写 |
| `execution_mode=explicit_shell` | 缺脚本引用、解释器与声明 shell 不一致、脚本字节漂移 |
| platform 与 backend | windows 使用 POSIX group，或 Linux/macOS 使用 Windows Job |
| 来源与完整 argv | adapter 的 argv_tail / 固定前缀 / 模型或工具参数被覆盖 |
| `started=false` | 存在真实子进程退出码/信号，或声称 RUNNING/已捕获目标执行 |
| `started=null` | 填入“确定未执行”的结论，或按正常完成消费 |
| `state=TERMINAL` | 身份/退出/未知原因无法解释；状态标签不能填补缺失事实 |
| POSIX/Windows 退出 | Windows 把负返回值当 POSIX 信号；POSIX 用无来源的 Windows DWORD |
| `evidence_complete=true` | 必需流没 EOF、read_error、已知丢字节/未知尾部或脱敏失败 |
| `scope_empty_verified` | 没有对应范围复核，或把 group 空等同于所有逃逸后代清除 |
| containment_sufficient | 不能由仅一项配置布尔值决定，应对照当前角色/检查的所需保证 |
| `required=false` | 不能绕过 Schema、平台覆盖、权限或输入锁 |
| 引用 | kind 未登记、资源缺失、哈希/上下文不匹配、路径越界、循环依赖 |
| 结果满足 | timeout/cancel/采集失败/未知清理时即使退出值匹配也不正常满足 |

附录 `check_result` 只演示最后一行的部分纯函数归约，它接收压平的合成事实，不是完整 `TransportSummary` 语义验证器。附录 `compare_identity` 只演示字段比较，不是进程所有权证明；`safe_cwd_static` 明确不防止检查后路径替换。

### A.4 建议资源类型注册与哈希方向

建议类型包括 `command-set`、`final-launch-plan`、`process-identity`、`transport-summary`、`shell-script` 及共享环境/入口/cwd/cleanup 类别。实际名称须以公共注册表为准；不能由插件任意自注册可信类型。

```text
环境/入口/cwd/请求/脚本/检查集合（先封存）
                    ↓
              FinalLaunchPlan
                    ↓
       handoff / 身份 / 输出段 / 清理事实
                    ↓
             TransportSummary
                    ↓
             04 视图与检查结果
```

一个资源不嵌入自己的摘要；示例引用均校验完整字节，二进制空文件也有确定 SHA-256。配置语义路径分离不意味着所有资源可以忽略行尾；脚本从 LF 改 CRLF 会改变资源摘要，即使某一解释器碰巧接受两者。argv 内的 CRLF 则是参数数据，不能因规范文件行尾而被改写。

### A.5 参考实现覆盖范围

| 已提供可运行参考 | 明确没有提供的生产实现 |
|---|---|
| 严格 JSON、确定性字节、CommandSet 语义校验/平台选择 | 完整 01–04 生产 Schema 注册和真实仓库迁移 |
| 静态 cwd 边界、环境名称/值形状及小型禁用集 | TOCTOU 安全后端、OS 沙箱、完整凭据与环境策略 |
| 首次停止原因锁存、退出格式、局部结果/身份比较 | 生产 event loop、跨平台 deadline/取消协调、持久恢复账本 |
| 本机 Linux 的固定合成子进程与 Bash 薄 wrapper | Windows Job、PowerShell 实测、macOS/Bash 3.2、WSL 原生验收 |
| 本地 Schema 样例、哈希/黄金向量、提取与复验 | 真实 Pi、模型费用、G0–G6、生产脱敏、断电恢复 |

这一划分是交付范围，不是能力推定。生产实现应逐项补足右列所需部分后再进入真实验收。

## 附录 B：可提取的参考实现、Schema 与合成夹具

### B.1 提取与运行

以下具名块构成 23 个文件的独立参考包。六个反引号用于包裹源码，使 README 内的普通代码围栏不会破坏提取。文本编码 UTF-8；二进制输出段以 base64 表示，空 stderr 按零字节恢复。

先阅读 `README.md` 和 `extract_reference.py`，将提取器保存到独立目录；使用新的目标目录提取，再运行其中 `verify.py`。本附录不是完整的生产 runner，不安装依赖、不调用模型，也不执行原项目测试。PowerShell 示例为未运行的建议写法。

| 文件类别 | 用途 |
|---|---|
| README / 提取器 / 验证器 | 说明、完整字节提取、Schema/资源/黄金向量/测试复验 |
| runtime_contracts.py | 静态契约与局部状态参考 |
| schemas/ | 四个记录家族的结构 Schema |
| fixtures/ | 人工合成配置/计划/身份/输出/清理、固定子进程脚本 |
| tests/ | 70 项契约/静态检查与 13 项 Linux 合成进程探测 |
| wrappers/ | 薄转发示例；Bash 当前版本实测，PowerShell 未运行 |

### B.2 `README.md`

<!-- reference-file: README.md; encoding: utf-8 -->
``````markdown
# 05 跨平台运行时：独立参考验证包

本包来自《05 三平台命令与入口（审阅增强版）》的具名代码块，全部业务/环境记录均为人工合成。不是生产调度器、不是 Pi fixture，不授权真实模型调用、安装、CI 或受保护项目改动。

## 执行范围

`runtime_contracts.py` 实现纯函数/静态边界参考；`tests/test_native.py` 只在 Linux 运行固定合成子进程。Windows、macOS、WSL、Bash 3.2 和 PowerShell wrapper 未在本次交付中执行。本包不实现 Windows Job、生产 I/O 监督、审核账本、预算、脱敏或抗 TOCTOU 的文件系统访问。

`fixtures/environment.json` 等记录内的路径、PID、版本及清理结论是合成值，不对应真实 OS 观察。真实本机测试信息位于外部验证报告；不要把两者混用。

## 依赖与运行

本次使用预装 Python 3.13.5、jsonschema 4.26.0 和本机 Bash。没有安装或升级任何包。这些是已用版本，不是对所有其他版本的兼容承诺。代码采用 Python 3.10+ 语法/API；该声明不代替其他版本测试。jsonschema 需要支持 Draft 2020-12；缺依赖时验证器失败，不自动下载。

在独立解压目录内，以已核验的 Python 路径运行：

```text
python verify.py --report ../reference-verification.json
```

报告目标必须尚不存在，且位于包目录外。运行包含无网络的固定合成子进程、有限临时文件和对测试自身所创建进程组的信号；不得把生产仓库或任意 PID 当作测试对象。类名为 NativeLinuxTests 的测试在非 Linux 平台显式跳过，跳过不等于对应平台通过。

`--report` 使用的相对路径按当前工作目录解释。以 PowerShell 调用时只替换成已核验 Python 入口，不把整条字符串拼成未经声明的 shell 模板。

## 从 Markdown 重新提取

将文档中 `extract_reference.py` 的具名代码保存为该脚本，以新目录为目标运行：

```text
python extract_reference.py 05-cross-platform-runtime-审阅增强版.md extracted-reference
```

该程序只提取、不执行；目标目录已存在则拒绝，不覆盖原文件。文本按 UTF-8/LF 保存，二进制 fixture 以 base64 提取。对文件内容的信任由使用者审阅与来源核验建立，不因被 Markdown 包裹就自动可信。

## 验证的实际含义

- 结构 Schema 的五个正例、十六个反例，仅证明结构校验；额外四个边界演示明确指出哪些无效生产组合仍需应用语义守卫。
- SHA-256 检查证明合成资源字节与合成引用一致，不证明真实环境、授权或平台能力。
- 十组黄金向量证明本文固定序列化表达式的字节结果，不是 JCS 或跨语言完全兼容性认证。
- Linux 子进程探测使用标准库和合成 stub，不经过用户项目 `run_supervised`，因此不证明项目后端正确。

Bash wrapper 只演示 `exec` 转发；PowerShell wrapper 只演示指定版本/模式下的建议写法，**未运行**。两者均不是安全 bootstrap、实际调度器或完整监督后端。
``````

### B.3 `extract_reference.py`

<!-- reference-file: extract_reference.py; encoding: utf-8 -->
``````python
"""Extract named reference blocks into a NEW directory; never execute them."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

BLOCK = re.compile(
    r'^<!-- reference-file: ([A-Za-z0-9._/-]+); encoding: (utf-8|base64) -->\n'
    r'``````[a-zA-Z0-9_-]*\n(.*?)^``````[ \t]*$',
    re.MULTILINE | re.DOTALL,
)
MARKER = re.compile(r'^<!-- reference-file: ', re.MULTILINE)


def extract(document: Path, destination: Path) -> dict:
    raw = document.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('document size limit')
    text = raw.decode('utf-8', errors='strict')
    matches = list(BLOCK.finditer(text))
    if not 1 <= len(matches) <= 100 or len(matches) != len(MARKER.findall(text)):
        raise ValueError('missing, malformed, or excessive named blocks')
    payloads: dict[str, bytes] = {}
    for match in matches:
        name, encoding, body = match.groups()
        path = PurePosixPath(name)
        if path.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')):
            raise ValueError('unsafe relative path')
        if name in payloads:
            raise ValueError('duplicate reference file')
        if encoding == 'base64':
            content = base64.b64decode(''.join(body.split()), validate=True)
        else:
            content = body.encode('utf-8')
        if len(content) > 4 * 1024 * 1024:
            raise ValueError('reference file size limit')
        payloads[name] = content
    # Require a fresh destination; do not touch existing repositories or evidence.
    destination.mkdir(parents=False, exist_ok=False)
    records = []
    for name, content in sorted(payloads.items()):
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(content)
        records.append({'path': name, 'bytes': len(content),
                        'sha256': hashlib.sha256(content).hexdigest()})
    return {'source_sha256': hashlib.sha256(raw).hexdigest(),
            'extracted_files': len(records), 'files': records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        result = extract(args.document, args.destination)
    except (OSError, UnicodeError, ValueError) as exc:
        # Avoid exposing arbitrary document contents in error text.
        parser.exit(2, f'extraction failed: {type(exc).__name__}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
``````

### B.4 `fixtures/check.sh`

<!-- reference-file: fixtures/check.sh; encoding: utf-8 -->
``````bash
#!/usr/bin/env bash
printf "%s\n" "synthetic-ok"
exit 0
``````

### B.5 `fixtures/cleanup.json`

<!-- reference-file: fixtures/cleanup.json; encoding: utf-8 -->
``````json
{"fixture":"synthetic","scope":"posix_group","state":"scope_empty_verified"}
``````

### B.6 `fixtures/command-set-shell.json`

<!-- reference-file: fixtures/command-set-shell.json; encoding: utf-8 -->
``````json
{"commands":[{"cwd":".","expected_exit_code":0,"id":"S01","output_assertions":[{"kind":"contains_utf8","stream":"stdout","text":"synthetic-ok"}],"required":true,"script":{"id":"check","kind":"shell-script","path":"fixtures/check.sh","sha256":"199a5ab69fb9e850f99d4f6ae97881e1f401dfee4317bc06f4f54f034c34b0ee"},"shell":"bash","timeout_seconds":10}],"record_type":"command_set","schema_version":1,"supported_platforms":["linux"]}
``````

### B.7 `fixtures/command-set.json`

<!-- reference-file: fixtures/command-set.json; encoding: utf-8 -->
``````json
{"commands":[{"by_platform":{"linux":{"argv":["python3","-m","pytest","tests"]},"macos":{"argv":["python3","-m","pytest","tests"]},"windows":{"argv":["py","-3","-m","pytest","tests"]}},"cwd":".","expected_exit_code":0,"id":"V01","required":true,"timeout_seconds":300}],"record_type":"command_set","schema_version":1,"supported_platforms":["linux","macos","windows"]}
``````

### B.8 `fixtures/cwd-binding.json`

<!-- reference-file: fixtures/cwd-binding.json; encoding: utf-8 -->
``````json
{"fixture":"synthetic","relative":".","worktree":"/synthetic/worktree"}
``````

### B.9 `fixtures/environment.json`

<!-- reference-file: fixtures/environment.json; encoding: utf-8 -->
``````json
{"execution_environment_id":"synthetic-linux","fixture":"synthetic","limitations":["not-a-real-preflight"],"platform":"linux"}
``````

### B.10 `fixtures/executable-binding.json`

<!-- reference-file: fixtures/executable-binding.json; encoding: utf-8 -->
``````json
{"chain_state":"not-verified","entry":"/synthetic/runtime/python","fixture":"synthetic"}
``````

### B.11 `fixtures/final-launch-plan.json`

<!-- reference-file: fixtures/final-launch-plan.json; encoding: utf-8 -->
``````json
{"argv":["/synthetic/runtime/python","-m","pytest","tests"],"backend_id":"posix_group_v1","command_id":"V01","cwd_binding_ref":{"id":"cwd-binding","kind":"cwd-binding","path":"fixtures/cwd-binding.json","sha256":"696253fef0ff3f4a95aa43c635cf53bf0041feeee607de8647e8191c3fd83310"},"env_names":["PATH"],"environment_ref":{"id":"environment","kind":"environment","path":"fixtures/environment.json","sha256":"616505100e0d0a5b56b73c900f7ffab17813f210dcd244f8c9dc8038eee9bc90"},"executable_binding_ref":{"id":"executable-binding","kind":"executable-binding","path":"fixtures/executable-binding.json","sha256":"2b7d50f14175261cce4ae1af75ba712ffe41be84f2f2ea7f57cf530187cde41c"},"execution_environment_id":"synthetic-linux","execution_mode":"exec_only","limits":{"drain_ms":2000,"grace_ms":2000,"kill_wait_ms":5000,"max_stderr_bytes":8388608,"max_stdout_bytes":8388608,"runtime_seconds":300},"operation_id":"op-synthetic","platform":"linux","record_type":"final_launch_plan","request_id":"request-synthetic","run_id":"run-synthetic","schema_version":1,"script_ref":null,"source_ref":{"id":"command-set","kind":"command-set","path":"fixtures/command-set.json","sha256":"444ab9c8ee780d34fd71f28262727d5710821e77027f16b16d910e56d47b7363"},"stdin":{"kind":"null"},"tty":false}
``````

### B.12 `fixtures/golden-vectors.json`

<!-- reference-file: fixtures/golden-vectors.json; encoding: utf-8 -->
``````json
[{"sha256":"ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356","utf8_hex":"7b7d0a","value":{}},{"sha256":"e8d38819d39f705646bfb643368eca78f7db476c16471dbc33b941b27326410d","utf8_hex":"7b2261223a312c2262223a327d0a","value":{"a":1,"b":2}},{"sha256":"3ed00169fe0d5bc53eb2b28294d6cf76c6b5691470aabe28c4abf90903daa1a4","utf8_hex":"7b2274657874223a22e4b8ade69687227d0a","value":{"text":"中文"}},{"sha256":"e500120f2945cc1dc5500ad78ab7dff7fd40459f03d572fb7100fd6bf7b7b765","utf8_hex":"7b2261726776223a5b22222c22612062222c22615c2262222c22433a5c5c7461696c5c5c222c22255825222c2224484f4d45225d7d0a","value":{"argv":["","a b","a\"b","C:\\tail\\","%X%","$HOME"]}},{"sha256":"cedf74272c9fc8db5448283a93277e7e7eb7534b71df3bd8ab35fd9b1b73404c","utf8_hex":"7b226e223a317d0a","value":{"n":1}},{"sha256":"4352df4233f0d6492477e70dab0a2b2aa9683d2297d61c8e8d98b515a4bc2f8d","utf8_hex":"7b226e223a312e307d0a","value":{"n":1.0}},{"sha256":"4dbc4b223d0da4858700d9a483553a9f2d5c31d2c2fcb7e45e41c56e3bd1f20c","utf8_hex":"7b2274657874223a22c3a9227d0a","value":{"text":"é"}},{"sha256":"4ae0b8cab5f8efd099785478bbd470b23d1c0eb7424b785416f3ee5496245948","utf8_hex":"7b2274657874223a2265cc81227d0a","value":{"text":"é"}},{"sha256":"72f5a9828b31a9e8f0174eefa8d0585f05d6381f2397f811a5ec7c4527e48e4a","utf8_hex":"7b2274657874223a22615c725c6e62227d0a","value":{"text":"a\r\nb"}},{"sha256":"ab166c40d63f948cca53bebb046034c6edab5fa092f17d2dec317a5eef5da77a","utf8_hex":"7b227265717569726564223a66616c73657d0a","value":{"required":false}}]
``````

### B.13 `fixtures/process-identity.json`

<!-- reference-file: fixtures/process-identity.json; encoding: utf-8 -->
``````json
{"boot_id":"boot-synthetic","controller_generation":1,"creation_marker":"synthetic-not-an-os-observation","executable_ref":{"id":"executable-binding","kind":"executable-binding","path":"fixtures/executable-binding.json","sha256":"2b7d50f14175261cce4ae1af75ba712ffe41be84f2f2ea7f57cf530187cde41c"},"execution_environment_id":"synthetic-linux","group_id":12345,"host_id":"host-synthetic","job_binding_id":null,"operation_id":"op-synthetic","pid":12345,"platform":"linux","record_type":"process_identity","request_id":"request-synthetic","run_id":"run-synthetic","schema_version":1}
``````

### B.14 `fixtures/process_stub.py`

<!-- reference-file: fixtures/process_stub.py; encoding: utf-8 -->
``````python
"""Synthetic, bounded, offline subprocess. No CLI, network or user repository use."""
import json
import os
import signal
import subprocess
import sys
import time

mode=sys.argv[1]
if mode=='echo':
    print(json.dumps({'argv':sys.argv[2:],'cwd':os.getcwd()},ensure_ascii=False))
elif mode=='exit':
    code=int(sys.argv[2]);assert 0<=code<=255;sys.exit(code)
elif mode=='dual':
    for _ in range(8):
        os.write(1,b'a'*8192);os.write(2,b'b'*8192)
elif mode=='stdin':
    sys.stdout.buffer.write(sys.stdin.buffer.read(65536))
elif mode=='sleep':
    time.sleep(3)
elif mode=='ignore-term':
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    print('ready',flush=True);time.sleep(3)
elif mode=='tree':
    child=subprocess.Popen([sys.executable,__file__,'sleep'],stdin=subprocess.DEVNULL)
    def stop(signum,frame):
        # Parent reaps the grandchild; the test signals the entire new process group.
        try:child.wait(timeout=1)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=1)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    print(json.dumps({'child_pid':child.pid,'pgid':os.getpgrp()}),flush=True)
    try:time.sleep(3)
    finally:
        if child.poll() is None:child.terminate()
        child.wait(timeout=1)
else:
    sys.exit(2)
``````

### B.15 `fixtures/stderr.bin`

<!-- reference-file: fixtures/stderr.bin; encoding: base64 -->
``````base64

``````

### B.16 `fixtures/stdout.bin`

<!-- reference-file: fixtures/stdout.bin; encoding: base64 -->
``````base64
c3ludGhldGljLW9rCg==
``````

### B.17 `fixtures/transport-summary.json`

<!-- reference-file: fixtures/transport-summary.json; encoding: utf-8 -->
``````json
{"cleanup":{"containment_sufficient":true,"evidence_refs":[{"id":"cleanup","kind":"cleanup-report","path":"fixtures/cleanup.json","sha256":"5c2e74f1c62b4c2ccf102580a898f929ae10cbe02bbb4389407bd9aa7d4f1e36"}],"scope":"posix_group","status":"scope_empty_verified"},"diagnostics":[],"elapsed_monotonic_ms":42,"evidence_complete":true,"execution_environment_id":"synthetic-linux","exit":{"observation_api":"synthetic-fixture","returncode":0,"signal":null,"status_u32":null},"final_plan_ref":{"id":"final-launch-plan","kind":"final-launch-plan","path":"fixtures/final-launch-plan.json","sha256":"54b3175a969fa0d42b6fda8d989463dea059e42f845352333a8f04a3677233ec"},"operation_id":"op-synthetic","platform":"linux","process_identity_ref":{"id":"process-identity","kind":"process-identity","path":"fixtures/process-identity.json","sha256":"a6e94eee2c1bd7aea47c96ce58f010dd4220b361993f76eb345f90f6323c5f19"},"record_type":"transport_summary","request_id":"request-synthetic","run_id":"run-synthetic","schema_version":1,"started":true,"state":"TERMINAL","stderr":{"dropped_observed_bytes":0,"eof":true,"observed_bytes":0,"read_error":null,"sealed_bytes":0,"segments":[{"id":"stderr","kind":"raw-segment","path":"fixtures/stderr.bin","sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}],"unobserved_tail":false},"stdout":{"dropped_observed_bytes":0,"eof":true,"observed_bytes":13,"read_error":null,"sealed_bytes":13,"segments":[{"id":"stdout","kind":"raw-segment","path":"fixtures/stdout.bin","sha256":"03b035400b802461110b426f44247573a04ebefd43bc9c9720a0f95c7c490ed8"}],"unobserved_tail":false},"stop_reason":null}
``````

### B.18 `runtime_contracts.py`

<!-- reference-file: runtime_contracts.py; encoding: utf-8 -->
``````python
"""Offline reference subset; not a production runner, sandbox, or approval service.
All errors intentionally omit raw input values and environment values.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

PLATFORMS = frozenset({'linux', 'macos', 'windows'})
SHELLS = frozenset({'bash', 'zsh', 'pwsh', 'windows-powershell', 'cmd'})
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
SHA = re.compile(r'^[0-9a-f]{64}$')
DENIED_ENV = frozenset({
    'BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS', 'ZDOTDIR',
    'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'NODE_OPTIONS',
    'LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES',
    'DYLD_LIBRARY_PATH', 'WSLENV', 'PI_DISPATCH_PI_CMD',
})

class ContractError(ValueError):
    def __init__(self, code: str, path: str):
        self.code, self.path = code, path
        super().__init__(f'{code} at {path}')

def reject(code: str, path: str) -> None:
    raise ContractError(code, path)

def _string(value: Any, path: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        reject('E_STRING', path)
    if '\0' in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        reject('E_STRING_SCALAR', path)
    return value

def strict_json(data: bytes, limit: int = 1_048_576) -> Any:
    if not isinstance(data, bytes):
        reject('E_JSON_TYPE', '/')
    if len(data) > limit:
        reject('E_JSON_LIMIT', '/')
    if data.startswith(b'\xef\xbb\xbf'):
        reject('E_JSON_BOM', '/')
    try:
        text = data.decode('utf-8', 'strict')
    except UnicodeError:
        reject('E_JSON_ENCODING', '/')
    # A grammar-independent allocation guard. json.loads validates grammar later.
    depth, in_string, escaped = 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in '[{':
            depth += 1
            if depth > 64:
                reject('E_JSON_DEPTH', '/')
        elif ch in ']}':
            depth -= 1
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in items:
            if key in out:
                reject('E_JSON_DUPLICATE_KEY', '/')
            out[key] = val
        return out
    def nonfinite(_: str) -> Any:
        reject('E_JSON_NUMBER', '/')
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        reject('E_JSON_SYNTAX', '/')
    def walk(v: Any) -> None:
        if isinstance(v, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in v):
                reject('E_JSON_UNICODE', '/')
        elif type(v) is int and abs(v) > 9_007_199_254_740_991:
            reject('E_JSON_NUMBER', '/')
        elif type(v) is float and not math.isfinite(v):
            reject('E_JSON_NUMBER', '/')
        elif isinstance(v, dict):
            for k, item in v.items():
                walk(k); walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
    walk(value)
    return value

def canonical_bytes(value: Any) -> bytes:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    strict_json(data)
    return data

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def object_keys(value: Any, required: set[str], optional: set[str], path: str) -> None:
    if not isinstance(value, dict):
        reject('E_OBJECT', path)
    if not required <= value.keys():
        reject('E_REQUIRED', path)
    if value.keys() - required - optional:
        reject('E_UNKNOWN_FIELD', path)

def relative_path(value: Any, path: str = '/cwd') -> str:
    value = _string(value, path, nonempty=True)
    if value == '.':
        return value
    if ('\\' in value or ':' in value or value.startswith('/') or
        any(c in value for c in '\r\n') or
        any(part in {'', '.', '..'} for part in value.split('/'))):
        reject('E_PATH_FORM', path)
    return value

def resource_ref(value: Any, path: str) -> None:
    object_keys(value, {'kind', 'id', 'sha256', 'path'}, set(), path)
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', _string(value['kind'], path)):
        reject('E_REF_KIND', path)
    if not re.fullmatch(r'[a-z0-9][a-z0-9._:-]{0,127}', _string(value['id'], path)) or not SHA.fullmatch(_string(value['sha256'], path)):
        reject('E_REF_FORMAT', path)
    if relative_path(value['path'], path) == '.':
        reject('E_REF_PATH', path)
    # File existence, registration, provenance and byte verification are external.

def validate_carrier(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        reject('E_CARRIER', path)
    if set(value) == {'argv'}:
        args = value['argv']
        if not isinstance(args, list) or not 1 <= len(args) <= 1024:
            reject('E_ARGV', path)
        for i, arg in enumerate(args):
            _string(arg, f'{path}/argv/{i}', nonempty=i == 0)
        if any(c in args[0] for c in '\r\n'):
            reject('E_ARGV_ENTRY', path)
        if sum(len(a.encode('utf-8')) + 1 for a in args) > 1_048_576:
            reject('E_ARGV_LIMIT', path)
    elif set(value) == {'shell', 'script'}:
        if not isinstance(value['shell'], str) or value['shell'] not in SHELLS:
            reject('E_SHELL', path)
        resource_ref(value['script'], path + '/script')
        if value['script']['kind'] != 'shell-script':
            reject('E_SCRIPT_KIND', path)
    else:
        reject('E_CARRIER', path)

def validate_command_set(value: Any) -> dict[str, Any]:
    object_keys(value, {'schema_version', 'record_type', 'supported_platforms', 'commands'}, set(), '/')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['record_type'] != 'command_set':
        reject('E_SCHEMA_VERSION', '/')
    supported = value['supported_platforms']
    if (not isinstance(supported, list) or not supported or
        any(not isinstance(p, str) or p not in PLATFORMS for p in supported) or
        len(set(supported)) != len(supported)):
        reject('E_PLATFORM_SET', '/supported_platforms')
    commands = value['commands']
    if not isinstance(commands, list) or not 1 <= len(commands) <= 256:
        reject('E_COMMANDS', '/commands')
    ids = set()
    out = copy.deepcopy(value)
    for n, item in enumerate(out['commands']):
        p = f'/commands/{n}'
        object_keys(item, {'id', 'required', 'cwd', 'timeout_seconds', 'expected_exit_code'},
                    {'argv', 'shell', 'script', 'by_platform', 'output_assertions'}, p)
        if not ID.fullmatch(_string(item['id'], p + '/id')) or item['id'] in ids:
            reject('E_COMMAND_ID', p + '/id')
        ids.add(item['id'])
        if type(item['required']) is not bool:
            reject('E_REQUIRED_TYPE', p + '/required')
        relative_path(item['cwd'])
        if type(item['timeout_seconds']) is not int or not 1 <= item['timeout_seconds'] <= 86400:
            reject('E_TIMEOUT', p + '/timeout_seconds')
        if type(item['expected_exit_code']) is not int or not 0 <= item['expected_exit_code'] <= 255:
            reject('E_EXPECTED_EXIT', p + '/expected_exit_code')
        variants = item.get('by_platform')
        direct = {k: item[k] for k in ('argv', 'shell', 'script') if k in item}
        if 'by_platform' in item:
            if direct or not isinstance(variants, dict) or set(variants) != set(supported):
                reject('E_PLATFORM_COVERAGE', p + '/by_platform')
            for plat, carrier in variants.items():
                validate_carrier(carrier, p + '/by_platform/' + plat)
        else:
            validate_carrier(direct, p)
        assertions = item.setdefault('output_assertions', [])
        if not isinstance(assertions, list) or len(assertions) > 32:
            reject('E_ASSERTION', p + '/output_assertions')
        for a in assertions:
            object_keys(a, {'kind', 'stream', 'text'}, set(), p + '/output_assertions')
            if a['kind'] not in ('contains_utf8', 'equals_utf8') or a['stream'] not in ('stdout', 'stderr'):
                reject('E_ASSERTION', p + '/output_assertions')
            _string(a['text'], p + '/output_assertions/text', nonempty=True)
    # supported_platforms is a schema-defined set; command and argv order stay intact.
    out['supported_platforms'] = sorted(supported)
    return out

def select_command(value: Any, command_id: str, target: str) -> dict[str, Any]:
    normalized = validate_command_set(value)
    if target not in normalized['supported_platforms']:
        reject('E_PLATFORM_UNDECLARED', '/target')
    item = next((c for c in normalized['commands'] if c['id'] == command_id), None)
    if item is None:
        reject('E_COMMAND_UNKNOWN', '/id')
    if 'by_platform' in item:
        carrier = item.pop('by_platform')[target]
        item.update(carrier)
    return item

def safe_cwd_static(root: Path, relative: str) -> Path:
    """Static containment only; a resolve/check sequence is NOT TOCTOU protection."""
    relative_path(relative)
    base = root.resolve(strict=True)
    actual = (base / relative).resolve(strict=True)
    if not actual.is_dir():
        reject('E_CWD_TYPE', '/cwd')
    try:
        actual.relative_to(base)
    except ValueError:
        reject('E_CWD_ESCAPE', '/cwd')
    return actual

def filtered_env(pairs: Iterable[tuple[str, str]], allowed: set[str],
                 required: set[str], platform: str) -> dict[str, str]:
    if platform not in PLATFORMS:
        reject('E_PLATFORM_UNDECLARED', '/platform')
    norm = str.upper if platform == 'windows' else lambda x: x
    allowed_norm, required_norm = {norm(x) for x in allowed}, {norm(x) for x in required}
    if not required_norm <= allowed_norm:
        reject('E_ENV_POLICY', '/env')
    seen: set[str] = set()
    result: dict[str, str] = {}
    for name, val in pairs:
        _string(name, '/env/name', nonempty=True); _string(val, '/env/value')
        if not NAME.fullmatch(name):
            reject('E_ENV_NAME', '/env/name')
        key = norm(name)
        if key in seen:
            reject('E_ENV_COLLISION', '/env/name')
        seen.add(key)
        # This prototype intentionally implements a small deny-list, not every CLI policy.
        if key in allowed_norm:
            if key in DENIED_ENV or key.startswith('PI_EXEC_'):
                reject('E_ENV_FORBIDDEN', '/env/name')
            result[key] = val
    if not required_norm <= result.keys():
        reject('E_ENV_MISSING', '/env')
    return result

@dataclass
class StopLatch:
    reason: str | None = None
    seq: int | None = None
    last_seq: int = 0

    def observe(self, seq: int, reason: str) -> str:
        if type(seq) is not int or seq <= self.last_seq:
            reject('E_EVENT_ORDER', '/seq')
        if reason not in {'timeout', 'cancel', 'output_limit', 'capture_error'}:
            reject('E_STOP_REASON', '/reason')
        self.last_seq = seq
        if self.reason is None:
            self.reason, self.seq = reason, seq
        return self.reason

def normalize_exit(platform: str, code: int | None) -> dict[str, Any]:
    if platform not in PLATFORMS:
        reject('E_PLATFORM_UNDECLARED', '/platform')
    if code is None:
        return {'returncode': None, 'signal': None, 'status_u32': None}
    if type(code) is not int:
        reject('E_EXIT_TYPE', '/returncode')
    return {'returncode': code,
            'signal': -code if platform != 'windows' and code < 0 else None,
            'status_u32': code & 0xffffffff if platform == 'windows' else None}

def check_result(transport: Mapping[str, Any], expected: int, assertions_ok: bool) -> str:
    """Local check result only. This function never returns a candidate/gate PASS."""
    if type(expected) is not int or not 0 <= expected <= 255 or type(assertions_ok) is not bool:
        reject('E_CHECK_INPUT', '/')
    if transport.get('started') is not True:
        return 'BLOCKED_OR_START_UNKNOWN'
    if transport.get('stop_reason') is not None:
        return 'ABORTED'
    if transport.get('evidence_complete') is not True:
        return 'INCOMPLETE'
    if (transport.get('cleanup_status') != 'scope_empty_verified' or
            transport.get('containment_sufficient') is not True):
        return 'INCOMPLETE'
    if transport.get('signal') is not None or transport.get('returncode') != expected:
        return 'FAILED'
    return 'SATISFIED' if assertions_ok else 'FAILED'

def compare_identity(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> str:
    """Comparison prerequisite, never permission to signal a PID."""
    fields = ('host_id', 'boot_id', 'execution_environment_id', 'run_id',
              'pid', 'creation_marker', 'executable_sha256')
    if any(expected.get(k) is None or observed.get(k) is None for k in fields):
        return 'UNKNOWN'
    return 'MATCH' if all(expected[k] == observed[k] for k in fields) else 'MISMATCH'

def render_view(command: Mapping[str, Any]) -> str:
    """Readable JSON data. Never parse this display back as a shell command."""
    return json.dumps(command, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
``````

### B.19 `schemas/runtime.schema.json`

<!-- reference-file: schemas/runtime.schema.json; encoding: utf-8 -->
``````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Proposed Code-Kit 05 runtime records — structure only",
  "oneOf": [
    {
      "$ref": "#/$defs/command_set"
    },
    {
      "$ref": "#/$defs/final_launch_plan"
    },
    {
      "$ref": "#/$defs/process_identity"
    },
    {
      "$ref": "#/$defs/transport_summary"
    }
  ],
  "$defs": {
    "resource_ref": {
      "type": "object",
      "properties": {
        "kind": {
          "type": "string",
          "pattern": "^[a-z][a-z0-9-]{0,63}$"
        },
        "id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        },
        "path": {
          "type": "string",
          "minLength": 1
        }
      },
      "required": [
        "kind",
        "id",
        "sha256",
        "path"
      ],
      "additionalProperties": false
    },
    "carrier": {
      "oneOf": [
        {
          "type": "object",
          "properties": {
            "argv": {
              "type": "array",
              "minItems": 1,
              "maxItems": 1024,
              "prefixItems": [
                {
                  "type": "string",
                  "minLength": 1,
                  "pattern": "^[^\\u0000\\r\\n]+$"
                }
              ],
              "items": {
                "type": "string",
                "pattern": "^[^\\u0000]*$"
              }
            }
          },
          "required": [
            "argv"
          ],
          "additionalProperties": false
        },
        {
          "type": "object",
          "properties": {
            "shell": {
              "enum": [
                "bash",
                "zsh",
                "pwsh",
                "windows-powershell",
                "cmd"
              ]
            },
            "script": {
              "$ref": "#/$defs/resource_ref"
            }
          },
          "required": [
            "shell",
            "script"
          ],
          "additionalProperties": false
        }
      ]
    },
    "assertion": {
      "type": "object",
      "properties": {
        "kind": {
          "enum": [
            "contains_utf8",
            "equals_utf8"
          ]
        },
        "stream": {
          "enum": [
            "stdout",
            "stderr"
          ]
        },
        "text": {
          "type": "string",
          "minLength": 1,
          "pattern": "^[^\\u0000]*$"
        }
      },
      "required": [
        "kind",
        "stream",
        "text"
      ],
      "additionalProperties": false
    },
    "command": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
        },
        "required": {
          "type": "boolean"
        },
        "cwd": {
          "type": "string",
          "minLength": 1
        },
        "timeout_seconds": {
          "type": "integer",
          "minimum": 1,
          "maximum": 86400
        },
        "expected_exit_code": {
          "type": "integer",
          "minimum": 0,
          "maximum": 255
        },
        "output_assertions": {
          "type": "array",
          "maxItems": 32,
          "items": {
            "$ref": "#/$defs/assertion"
          }
        },
        "argv": {
          "type": "array",
          "minItems": 1,
          "maxItems": 1024,
          "prefixItems": [
            {
              "type": "string",
              "minLength": 1,
              "pattern": "^[^\\u0000\\r\\n]+$"
            }
          ],
          "items": {
            "type": "string",
            "pattern": "^[^\\u0000]*$"
          }
        },
        "shell": {
          "enum": [
            "bash",
            "zsh",
            "pwsh",
            "windows-powershell",
            "cmd"
          ]
        },
        "script": {
          "$ref": "#/$defs/resource_ref"
        },
        "by_platform": {
          "type": "object",
          "properties": {
            "linux": {
              "$ref": "#/$defs/carrier"
            },
            "macos": {
              "$ref": "#/$defs/carrier"
            },
            "windows": {
              "$ref": "#/$defs/carrier"
            }
          },
          "required": [],
          "additionalProperties": false
        }
      },
      "required": [
        "id",
        "required",
        "cwd",
        "timeout_seconds",
        "expected_exit_code"
      ],
      "additionalProperties": false,
      "oneOf": [
        {
          "required": [
            "argv"
          ],
          "not": {
            "anyOf": [
              {
                "required": [
                  "shell"
                ]
              },
              {
                "required": [
                  "script"
                ]
              },
              {
                "required": [
                  "by_platform"
                ]
              }
            ]
          }
        },
        {
          "required": [
            "shell",
            "script"
          ],
          "not": {
            "anyOf": [
              {
                "required": [
                  "argv"
                ]
              },
              {
                "required": [
                  "by_platform"
                ]
              }
            ]
          }
        },
        {
          "required": [
            "by_platform"
          ],
          "not": {
            "anyOf": [
              {
                "required": [
                  "argv"
                ]
              },
              {
                "required": [
                  "shell"
                ]
              },
              {
                "required": [
                  "script"
                ]
              }
            ]
          }
        }
      ]
    },
    "command_set": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "command_set"
        },
        "supported_platforms": {
          "type": "array",
          "minItems": 1,
          "maxItems": 3,
          "uniqueItems": true,
          "items": {
            "enum": [
              "linux",
              "macos",
              "windows"
            ]
          }
        },
        "commands": {
          "type": "array",
          "minItems": 1,
          "maxItems": 256,
          "items": {
            "$ref": "#/$defs/command"
          }
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "supported_platforms",
        "commands"
      ],
      "additionalProperties": false
    },
    "final_launch_plan": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "final_launch_plan"
        },
        "request_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "run_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "operation_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "source_ref": {
          "$ref": "#/$defs/resource_ref"
        },
        "command_id": {
          "anyOf": [
            {
              "type": "string",
              "minLength": 1
            },
            {
              "type": "null"
            }
          ]
        },
        "platform": {
          "enum": [
            "linux",
            "macos",
            "windows"
          ]
        },
        "execution_environment_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "environment_ref": {
          "$ref": "#/$defs/resource_ref"
        },
        "executable_binding_ref": {
          "$ref": "#/$defs/resource_ref"
        },
        "cwd_binding_ref": {
          "$ref": "#/$defs/resource_ref"
        },
        "execution_mode": {
          "enum": [
            "exec_only",
            "explicit_shell"
          ]
        },
        "argv": {
          "type": "array",
          "minItems": 1,
          "maxItems": 1024,
          "prefixItems": [
            {
              "type": "string",
              "minLength": 1,
              "pattern": "^[^\\u0000\\r\\n]+$"
            }
          ],
          "items": {
            "type": "string",
            "pattern": "^[^\\u0000]*$"
          }
        },
        "env_names": {
          "type": "array",
          "uniqueItems": true,
          "items": {
            "type": "string",
            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"
          }
        },
        "stdin": {
          "oneOf": [
            {
              "type": "object",
              "properties": {
                "kind": {
                  "const": "null"
                }
              },
              "required": [
                "kind"
              ],
              "additionalProperties": false
            },
            {
              "type": "object",
              "properties": {
                "kind": {
                  "const": "resource"
                },
                "ref": {
                  "$ref": "#/$defs/resource_ref"
                }
              },
              "required": [
                "kind",
                "ref"
              ],
              "additionalProperties": false
            }
          ]
        },
        "script_ref": {
          "anyOf": [
            {
              "$ref": "#/$defs/resource_ref"
            },
            {
              "type": "null"
            }
          ]
        },
        "limits": {
          "type": "object",
          "properties": {
            "runtime_seconds": {
              "type": "integer",
              "minimum": 1
            },
            "grace_ms": {
              "type": "integer",
              "minimum": 0
            },
            "kill_wait_ms": {
              "type": "integer",
              "minimum": 0
            },
            "drain_ms": {
              "type": "integer",
              "minimum": 0
            },
            "max_stdout_bytes": {
              "type": "integer",
              "minimum": 1
            },
            "max_stderr_bytes": {
              "type": "integer",
              "minimum": 1
            }
          },
          "required": [
            "runtime_seconds",
            "grace_ms",
            "kill_wait_ms",
            "drain_ms",
            "max_stdout_bytes",
            "max_stderr_bytes"
          ],
          "additionalProperties": false
        },
        "backend_id": {
          "enum": [
            "posix_group_v1",
            "windows_job_v1"
          ]
        },
        "tty": {
          "const": false
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "request_id",
        "run_id",
        "operation_id",
        "source_ref",
        "command_id",
        "platform",
        "execution_environment_id",
        "environment_ref",
        "executable_binding_ref",
        "cwd_binding_ref",
        "execution_mode",
        "argv",
        "env_names",
        "stdin",
        "script_ref",
        "limits",
        "backend_id",
        "tty"
      ],
      "additionalProperties": false
    },
    "process_identity": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "process_identity"
        },
        "request_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "run_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "operation_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "host_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "boot_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "execution_environment_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "platform": {
          "enum": [
            "linux",
            "macos",
            "windows"
          ]
        },
        "pid": {
          "type": "integer",
          "minimum": 1
        },
        "creation_marker": {
          "type": "string",
          "minLength": 1
        },
        "executable_ref": {
          "$ref": "#/$defs/resource_ref"
        },
        "group_id": {
          "anyOf": [
            {
              "type": "integer",
              "minimum": 1
            },
            {
              "type": "null"
            }
          ]
        },
        "job_binding_id": {
          "anyOf": [
            {
              "type": "string",
              "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
            },
            {
              "type": "null"
            }
          ]
        },
        "controller_generation": {
          "type": "integer",
          "minimum": 1
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "request_id",
        "run_id",
        "operation_id",
        "host_id",
        "boot_id",
        "execution_environment_id",
        "platform",
        "pid",
        "creation_marker",
        "executable_ref",
        "group_id",
        "job_binding_id",
        "controller_generation"
      ],
      "additionalProperties": false
    },
    "transport_summary": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "transport_summary"
        },
        "request_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "run_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "operation_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "final_plan_ref": {
          "$ref": "#/$defs/resource_ref"
        },
        "platform": {
          "enum": [
            "linux",
            "macos",
            "windows"
          ]
        },
        "execution_environment_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "process_identity_ref": {
          "anyOf": [
            {
              "$ref": "#/$defs/resource_ref"
            },
            {
              "type": "null"
            }
          ]
        },
        "started": {
          "type": [
            "boolean",
            "null"
          ]
        },
        "state": {
          "enum": [
            "NOT_STARTED",
            "START_UNKNOWN",
            "TERMINAL"
          ]
        },
        "stop_reason": {
          "enum": [
            null,
            "timeout",
            "cancel",
            "output_limit",
            "capture_error",
            "supervisor_error",
            "residual_after_exit"
          ]
        },
        "exit": {
          "type": "object",
          "properties": {
            "returncode": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ]
            },
            "signal": {
              "anyOf": [
                {
                  "type": "integer",
                  "minimum": 1
                },
                {
                  "type": "null"
                }
              ]
            },
            "status_u32": {
              "anyOf": [
                {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 4294967295
                },
                {
                  "type": "null"
                }
              ]
            },
            "observation_api": {
              "type": "string",
              "minLength": 1
            }
          },
          "required": [
            "returncode",
            "signal",
            "status_u32",
            "observation_api"
          ],
          "additionalProperties": false
        },
        "stdout": {
          "type": "object",
          "properties": {
            "observed_bytes": {
              "type": "integer",
              "minimum": 0
            },
            "sealed_bytes": {
              "type": "integer",
              "minimum": 0
            },
            "dropped_observed_bytes": {
              "type": "integer",
              "minimum": 0
            },
            "unobserved_tail": {
              "type": "boolean"
            },
            "eof": {
              "type": "boolean"
            },
            "read_error": {
              "anyOf": [
                {
                  "type": "string",
                  "minLength": 1
                },
                {
                  "type": "null"
                }
              ]
            },
            "segments": {
              "type": "array",
              "items": {
                "$ref": "#/$defs/resource_ref"
              },
              "minItems": 0
            }
          },
          "required": [
            "observed_bytes",
            "sealed_bytes",
            "dropped_observed_bytes",
            "unobserved_tail",
            "eof",
            "read_error",
            "segments"
          ],
          "additionalProperties": false
        },
        "stderr": {
          "type": "object",
          "properties": {
            "observed_bytes": {
              "type": "integer",
              "minimum": 0
            },
            "sealed_bytes": {
              "type": "integer",
              "minimum": 0
            },
            "dropped_observed_bytes": {
              "type": "integer",
              "minimum": 0
            },
            "unobserved_tail": {
              "type": "boolean"
            },
            "eof": {
              "type": "boolean"
            },
            "read_error": {
              "anyOf": [
                {
                  "type": "string",
                  "minLength": 1
                },
                {
                  "type": "null"
                }
              ]
            },
            "segments": {
              "type": "array",
              "items": {
                "$ref": "#/$defs/resource_ref"
              },
              "minItems": 0
            }
          },
          "required": [
            "observed_bytes",
            "sealed_bytes",
            "dropped_observed_bytes",
            "unobserved_tail",
            "eof",
            "read_error",
            "segments"
          ],
          "additionalProperties": false
        },
        "evidence_complete": {
          "type": "boolean"
        },
        "cleanup": {
          "type": "object",
          "properties": {
            "status": {
              "enum": [
                "not_required",
                "scope_empty_verified",
                "residual_detected",
                "unknown",
                "failed"
              ]
            },
            "scope": {
              "enum": [
                "posix_group",
                "windows_job",
                "not_created"
              ]
            },
            "containment_sufficient": {
              "type": "boolean"
            },
            "evidence_refs": {
              "type": "array",
              "items": {
                "$ref": "#/$defs/resource_ref"
              },
              "minItems": 0
            }
          },
          "required": [
            "status",
            "scope",
            "containment_sufficient",
            "evidence_refs"
          ],
          "additionalProperties": false
        },
        "elapsed_monotonic_ms": {
          "anyOf": [
            {
              "type": "integer",
              "minimum": 0
            },
            {
              "type": "null"
            }
          ]
        },
        "diagnostics": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "code": {
                "type": "string",
                "pattern": "^E_[A-Z0-9_]+$"
              },
              "message": {
                "type": "string"
              },
              "details": {
                "type": "object",
                "properties": {
                  "phase": {
                    "type": "string",
                    "pattern": "^[^\\u0000]*$"
                  },
                  "field": {
                    "type": "string",
                    "pattern": "^[^\\u0000]*$"
                  },
                  "reason": {
                    "type": "string",
                    "pattern": "^[^\\u0000]*$"
                  }
                },
                "required": [
                  "phase",
                  "field",
                  "reason"
                ],
                "additionalProperties": false
              },
              "retryable": {
                "type": "boolean"
              }
            },
            "required": [
              "code",
              "message",
              "details",
              "retryable"
            ],
            "additionalProperties": false
          },
          "minItems": 0
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "request_id",
        "run_id",
        "operation_id",
        "final_plan_ref",
        "platform",
        "execution_environment_id",
        "process_identity_ref",
        "started",
        "state",
        "stop_reason",
        "exit",
        "stdout",
        "stderr",
        "evidence_complete",
        "cleanup",
        "elapsed_monotonic_ms",
        "diagnostics"
      ],
      "additionalProperties": false
    }
  }
}
``````

### B.20 `tests/test_contracts.py`

<!-- reference-file: tests/test_contracts.py; encoding: utf-8 -->
``````python
import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime_contracts import (ContractError, StopLatch, canonical_bytes, check_result,
    compare_identity, filtered_env, normalize_exit, render_view, safe_cwd_static,
    select_command, strict_json, validate_command_set)

BASE = Path(__file__).resolve().parents[1]

def fixture():
    return json.loads((BASE / 'fixtures/command-set.json').read_text(encoding='utf-8'))

def good_transport():
    return dict(started=True, stop_reason=None, evidence_complete=True,
                cleanup_status='scope_empty_verified', containment_sufficient=True,
                signal=None, returncode=0)

class ContractsTests(unittest.TestCase):
    def reject(self, code, call, *args, **kwargs):
        with self.assertRaises(ContractError) as cm:
            call(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)

    def test_complete_three_platform_selection(self):
        for p, executable in [('linux','python3'),('macos','python3'),('windows','py')]:
            self.assertEqual(select_command(fixture(), 'V01', p)['argv'][0], executable)

    def test_selection_does_not_mutate_source(self):
        value=fixture(); before=copy.deepcopy(value)
        select_command(value,'V01','windows')
        self.assertEqual(value,before)

    def test_common_carrier(self):
        value=fixture(); c=value['commands'][0]
        c.pop('by_platform');c['argv']=['tool','','a b']
        self.assertEqual(select_command(value,'V01','linux')['argv'],['tool','','a b'])

    def test_shell_fixture(self):
        value=json.loads((BASE/'fixtures/command-set-shell.json').read_text())
        self.assertEqual(select_command(value,'S01','linux')['shell'],'bash')

    def test_default_assertions_are_explicit_before_sealing(self):
        self.assertEqual(validate_command_set(fixture())['commands'][0]['output_assertions'],[])

    def test_platform_set_normalization_is_order_independent(self):
        a=fixture();b=fixture();b['supported_platforms'].reverse()
        self.assertEqual(canonical_bytes(validate_command_set(a)),canonical_bytes(validate_command_set(b)))

    def test_command_order_is_not_normalized(self):
        a=fixture();second=copy.deepcopy(a['commands'][0]);second['id']='V02'
        a['commands'].append(second);b=copy.deepcopy(a);b['commands'].reverse()
        self.assertNotEqual(canonical_bytes(validate_command_set(a)),canonical_bytes(validate_command_set(b)))

    def test_missing_target_is_not_optional_skip(self):
        value=fixture();value['commands'][0]['required']=False
        del value['commands'][0]['by_platform']['macos']
        self.reject('E_PLATFORM_COVERAGE',validate_command_set,value)

    def test_variant_cannot_override_required(self):
        value=fixture();value['commands'][0]['by_platform']['windows']['required']=False
        self.reject('E_CARRIER',validate_command_set,value)

    def test_direct_plus_variants_rejected(self):
        value=fixture();value['commands'][0]['argv']=['x']
        self.reject('E_PLATFORM_COVERAGE',validate_command_set,value)

    def test_unknown_target(self):
        self.reject('E_PLATFORM_UNDECLARED',select_command,fixture(),'V01','wsl')

    def test_unknown_command(self):
        self.reject('E_COMMAND_UNKNOWN',select_command,fixture(),'missing','linux')

    def test_duplicate_command_id(self):
        value=fixture();value['commands'].append(copy.deepcopy(value['commands'][0]))
        self.reject('E_COMMAND_ID',validate_command_set,value)

    def test_shell_without_script(self):
        value=fixture();c=value['commands'][0];c.pop('by_platform');c['shell']='bash'
        self.reject('E_CARRIER',validate_command_set,value)

    def test_script_kind_is_not_arbitrary_fixture(self):
        value=json.loads((BASE/'fixtures/command-set-shell.json').read_text())
        value['commands'][0]['script']['kind']='fixture'
        self.reject('E_SCRIPT_KIND',validate_command_set,value)

    def test_secret_not_in_diagnostic(self):
        value=fixture();value['commands'][0]['password']='secret-DO-NOT-LOG'
        try: validate_command_set(value)
        except ContractError as e: self.assertNotIn('secret-DO-NOT-LOG',str(e))
        else: self.fail('unknown secret field accepted')

    def test_render_view_keeps_exact_data(self):
        c={'argv':['tool','','a b','中文','a"b','line\r\nend','`code`']}
        self.assertEqual(json.loads(render_view(c)),c)

    def test_unicode_is_not_normalized(self):
        self.assertNotEqual(canonical_bytes({'s':'é'}),canonical_bytes({'s':'e\u0301'}))

    def test_numeric_representation_is_not_rewritten(self):
        self.assertNotEqual(canonical_bytes({'n':1}),canonical_bytes({'n':1.0}))

    def test_empty_env_value_is_preserved(self):
        self.assertEqual(filtered_env([('X','')],{'X'},{'X'},'linux'),{'X':''})

    def test_windows_case_collision(self):
        self.reject('E_ENV_COLLISION',filtered_env,[('Path','a'),('PATH','b')],{'PATH'},set(),'windows')

    def test_posix_case_distinct(self):
        self.assertEqual(filtered_env([('Path','a'),('PATH','b')],{'Path','PATH'},set(),'linux'),{'Path':'a','PATH':'b'})

    def test_required_env_missing(self):
        self.reject('E_ENV_MISSING',filtered_env,[],{'X'},{'X'},'linux')

    def test_forbidden_loader_env(self):
        self.reject('E_ENV_FORBIDDEN',filtered_env,[('NODE_OPTIONS','secret')],{'NODE_OPTIONS'},set(),'linux')

    def test_unlisted_env_not_inherited(self):
        self.assertEqual(filtered_env([('X','value')],set(),set(),'linux'),{})

    def test_environment_values_not_in_error(self):
        try: filtered_env([('X','secret\0value')],{'X'},set(),'linux')
        except ContractError as e: self.assertNotIn('secret',str(e))
        else:self.fail('NUL accepted')

    def test_static_inside_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'子目录').mkdir()
            self.assertEqual(safe_cwd_static(p,'子目录'),p.resolve()/'子目录')

    def test_static_symlink_escape(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            try:(Path(a)/'link').symlink_to(b,target_is_directory=True)
            except OSError:self.skipTest('symlink privilege unavailable')
            self.reject('E_CWD_ESCAPE',safe_cwd_static,Path(a),'link')

    def test_file_is_not_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'file').write_text('x')
            self.reject('E_CWD_TYPE',safe_cwd_static,p,'file')

    def test_first_stop_reason_is_sticky(self):
        latch=StopLatch();latch.observe(1,'timeout');latch.observe(2,'cancel')
        self.assertEqual((latch.reason,latch.seq),('timeout',1))

    def test_event_order_rejected(self):
        latch=StopLatch();latch.observe(10,'cancel')
        self.reject('E_EVENT_ORDER',latch.observe,10,'timeout')

    def test_posix_signal_not_exit_code(self):
        self.assertEqual(normalize_exit('linux',-15),{'returncode':-15,'signal':15,'status_u32':None})

    def test_windows_signed_exit_is_not_posix_signal(self):
        result=normalize_exit('windows',-1073741819)
        self.assertEqual(result['status_u32'],3221225477);self.assertIsNone(result['signal'])

    def test_exit_unknown_is_not_zero(self):
        self.assertIsNone(normalize_exit('linux',None)['returncode'])

    def test_nonzero_expected_normal_exit_can_satisfy(self):
        t=good_transport();t['returncode']=7
        self.assertEqual(check_result(t,7,True),'SATISFIED')

    def test_no_candidate_PASS_label(self):
        self.assertEqual(check_result(good_transport(),0,True),'SATISFIED')

    def test_timeout_zero_exit_stays_aborted(self):
        t=good_transport();t['stop_reason']='timeout'
        self.assertEqual(check_result(t,0,True),'ABORTED')

    def test_start_unknown_cannot_satisfy(self):
        t=good_transport();t['started']=None
        self.assertEqual(check_result(t,0,True),'BLOCKED_OR_START_UNKNOWN')

    def test_missing_eof_evidence_cannot_satisfy(self):
        t=good_transport();t['evidence_complete']=False
        self.assertEqual(check_result(t,0,True),'INCOMPLETE')

    def test_residuals_cannot_satisfy(self):
        t=good_transport();t['cleanup_status']='residual_detected'
        self.assertEqual(check_result(t,0,True),'INCOMPLETE')

    def test_inadequate_containment_cannot_satisfy(self):
        t=good_transport();t['containment_sufficient']=False
        self.assertEqual(check_result(t,0,True),'INCOMPLETE')

    def test_failed_output_assertion(self):
        self.assertEqual(check_result(good_transport(),0,False),'FAILED')

    def test_identity_pid_reuse_rejected(self):
        a=dict(host_id='h',boot_id='b',execution_environment_id='e',run_id='r',pid=7,creation_marker='a',executable_sha256='a'*64)
        b={**a,'creation_marker':'b'}
        self.assertEqual(compare_identity(a,b),'MISMATCH')

    def test_identity_unknown_rejected(self):
        self.assertEqual(compare_identity({'pid':7},{'pid':7}),'UNKNOWN')

    def test_identity_equal_is_only_match_not_permission(self):
        a=dict(host_id='h',boot_id='b',execution_environment_id='e',run_id='r',pid=7,creation_marker='a',executable_sha256='a'*64)
        self.assertEqual(compare_identity(a,a),'MATCH')

# Named single-fault cases: these are separate tests, not production capability counts.
JSON_BAD = [
    ('duplicate',b'{"a":1,"a":2}','E_JSON_DUPLICATE_KEY'),
    ('escaped_duplicate',b'{"a":1,"\\u0061":2}','E_JSON_DUPLICATE_KEY'),
    ('bom',b'\xef\xbb\xbf{}','E_JSON_BOM'),
    ('nan',b'{"n":NaN}','E_JSON_NUMBER'),
    ('overflow',b'{"n":1e999}','E_JSON_NUMBER'),
    ('large_integer',b'{"n":9007199254740992}','E_JSON_NUMBER'),
    ('surrogate',b'{"s":"\\ud800"}','E_JSON_UNICODE'),
    ('invalid_utf8',b'{"s":"\xff"}','E_JSON_ENCODING'),
    ('trailing_document',b'{}{}','E_JSON_SYNTAX'),
    ('too_deep',b'['*65+b'0'+b']'*65,'E_JSON_DEPTH'),
]
for name,data,code in JSON_BAD:
    def test(self,data=data,code=code):self.reject(code,strict_json,data)
    setattr(ContractsTests,'test_json_'+name,test)

BAD_FIELDS = [
    ('timeout_bool','timeout_seconds',True,'E_TIMEOUT'),
    ('timeout_zero','timeout_seconds',0,'E_TIMEOUT'),
    ('timeout_real','timeout_seconds',1.0,'E_TIMEOUT'),
    ('expected_signal','expected_exit_code',-9,'E_EXPECTED_EXIT'),
    ('expected_bool','expected_exit_code',True,'E_EXPECTED_EXIT'),
    ('required_null','required',None,'E_REQUIRED_TYPE'),
    ('cwd_parent','cwd','../outside','E_PATH_FORM'),
    ('cwd_absolute','cwd','/tmp','E_PATH_FORM'),
    ('cwd_drive','cwd','C:/work','E_PATH_FORM'),
    ('cwd_backslash','cwd','a\\b','E_PATH_FORM'),
]
for name,key,value,code in BAD_FIELDS:
    def test(self,key=key,value=value,code=code):
        d=fixture();d['commands'][0][key]=value
        self.reject(code,validate_command_set,d)
    setattr(ContractsTests,'test_field_'+name,test)

BAD_ARGV=[('empty',[],'E_ARGV'),('empty_entry',[''],'E_STRING'),('nul',['tool','a\0b'],'E_STRING_SCALAR'),('scalar',['tool',None],'E_STRING'),('newline_entry',['tool\n'],'E_ARGV_ENTRY')]
for name,args,code in BAD_ARGV:
    def test(self,args=args,code=code):
        d=fixture();d['commands'][0]['by_platform']['linux']['argv']=args
        self.reject(code,validate_command_set,d)
    setattr(ContractsTests,'test_argv_'+name,test)

if __name__=='__main__':unittest.main()
``````

### B.21 `tests/test_native.py`

<!-- reference-file: tests/test_native.py; encoding: utf-8 -->
``````python
"""Native tests below exercise only trusted synthetic fixtures, not a runner API."""
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
STUB=BASE/'fixtures/process_stub.py'
BASH=shutil.which('bash')

def argv(mode,*args):return [sys.executable,str(STUB),mode,*args]
def minimal_env():
    # No credentials: inherited values are restricted to this known local test input.
    out={'PATH':os.defpath,'PYTHONIOENCODING':'utf-8','CODEKIT_BOOTSTRAP_PYTHON':sys.executable}
    return out

def spawn(mode,*args):
    return subprocess.Popen(argv(mode,*args),stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,
        env=minimal_env())

def ready_line(proc):
    readable,_,_=select.select([proc.stdout],[],[],2)
    if not readable:raise AssertionError('fixture readiness timeout')
    return proc.stdout.readline()

def cleanup_owned(proc):
    # Only a retained direct child created by this test is signalled.
    if proc.poll() is None:
        os.killpg(proc.pid,signal.SIGKILL)
    proc.communicate(timeout=3)
    if proc.stdout:proc.stdout.close()
    if proc.stderr:proc.stderr.close()

@unittest.skipUnless(sys.platform.startswith('linux'),'Linux-only synthetic probes; not cross-platform acceptance')
class NativeLinuxTests(unittest.TestCase):
    def test_argv_roundtrip(self):
        args=['',' ','a b','中文','😀','a"b',"a'b",'C:\\tail\\','a\\"b','%PATH%','$HOME','a&b','a|b','a;b','*','--','@file','a\r\nb']
        r=subprocess.run(argv('echo',*args),capture_output=True,env=minimal_env(),timeout=3,check=True)
        self.assertEqual(json.loads(r.stdout)['argv'],args)

    def test_native_nonzero_and_wait_three(self):
        for code in (0,1,2,3,4,17):
            with self.subTest(code=code):
                r=subprocess.run(argv('exit',str(code)),capture_output=True,env=minimal_env(),timeout=3)
                self.assertEqual(r.returncode,code)

    def test_stdout_stderr_drained_separately(self):
        r=subprocess.run(argv('dual'),capture_output=True,env=minimal_env(),timeout=3,check=True)
        self.assertEqual(r.stdout,b'a'*65536);self.assertEqual(r.stderr,b'b'*65536)

    def test_stdin_eof_and_byte_preservation(self):
        raw='中文\r\n'.encode()+b'\0\xff'
        r=subprocess.run(argv('stdin'),input=raw,capture_output=True,env=minimal_env(),timeout=3,check=True)
        self.assertEqual(r.stdout,raw)

    def test_unicode_cwd(self):
        with tempfile.TemporaryDirectory(prefix='runtime05-') as tmp:
            cwd=Path(tmp)/'中文 空格';cwd.mkdir()
            r=subprocess.run(argv('echo'),cwd=cwd,capture_output=True,env=minimal_env(),timeout=3,check=True)
            self.assertEqual(Path(json.loads(r.stdout)['cwd']),cwd)

    def test_start_failure_has_no_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                subprocess.Popen([str(Path(tmp)/'missing-executable')],env=minimal_env())

    def test_timeout_is_not_automatic_communicate_tree_kill(self):
        p=spawn('sleep')
        try:
            with self.assertRaises(subprocess.TimeoutExpired):p.communicate(timeout=0.05)
            os.killpg(p.pid,signal.SIGTERM);p.communicate(timeout=2)
            self.assertEqual(p.returncode,-signal.SIGTERM)
        finally:cleanup_owned(p)

    def test_cancel_owned_group_and_reap_grandchild(self):
        p=spawn('tree')
        try:
            meta=json.loads(ready_line(p));self.assertEqual(meta['pgid'],p.pid)
            os.killpg(p.pid,signal.SIGTERM);p.communicate(timeout=2)
            self.assertEqual(p.returncode,0)
            with self.assertRaises(ProcessLookupError):os.kill(meta['child_pid'],0)
        finally:cleanup_owned(p)

    def test_term_escalation_for_known_single_child(self):
        p=spawn('ignore-term')
        try:
            self.assertEqual(ready_line(p),b'ready\n')
            os.killpg(p.pid,signal.SIGTERM)
            with self.assertRaises(subprocess.TimeoutExpired):p.wait(timeout=0.05)
            os.killpg(p.pid,signal.SIGKILL);p.communicate(timeout=2)
            self.assertEqual(p.returncode,-signal.SIGKILL)
        finally:cleanup_owned(p)

    @unittest.skipUnless(BASH,'bash unavailable')
    def test_bash_wrapper_arguments(self):
        args=['','中文 空格','a"b',"a'b",'$HOME','%X%','a\r\nb','x\\']
        r=subprocess.run([BASH,str(BASE/'wrappers/dispatch-stub.sh'),'echo',*args],capture_output=True,env=minimal_env(),timeout=3,check=True)
        self.assertEqual(json.loads(r.stdout)['argv'],args)

    @unittest.skipUnless(BASH,'bash unavailable')
    def test_bash_wrapper_wait_three(self):
        r=subprocess.run([BASH,str(BASE/'wrappers/dispatch-stub.sh'),'exit','3'],capture_output=True,env=minimal_env(),timeout=3)
        self.assertEqual(r.returncode,3)

    @unittest.skipUnless(BASH,'bash unavailable')
    def test_bash_syntax_and_program_failure_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'syntax.sh';script.write_text('if then\n',encoding='utf-8')
            syntax=subprocess.run([BASH,'-n',str(script)],capture_output=True,env=minimal_env(),timeout=3)
            script.write_text('exit 7\n',encoding='utf-8')
            valid=subprocess.run([BASH,'-n',str(script)],capture_output=True,env=minimal_env(),timeout=3)
            program=subprocess.run([BASH,str(script)],capture_output=True,env=minimal_env(),timeout=3)
            self.assertNotEqual(syntax.returncode,0);self.assertEqual(valid.returncode,0);self.assertEqual(program.returncode,7)

    @unittest.skipUnless(BASH,'bash unavailable')
    def test_pipefail_is_a_semantic_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'pipe.sh';script.write_text('false | true\n',encoding='utf-8')
            normal=subprocess.run([BASH,str(script)],capture_output=True,env=minimal_env(),timeout=3)
            strict=subprocess.run([BASH,'-o','pipefail',str(script)],capture_output=True,env=minimal_env(),timeout=3)
            self.assertEqual(normal.returncode,0);self.assertEqual(strict.returncode,1)

if __name__=='__main__':unittest.main()
``````

### B.22 `verify.py`

<!-- reference-file: verify.py; encoding: utf-8 -->
``````python
"""Verify only this synthetic reference package. No installs or model calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone

from runtime_contracts import canonical_bytes, strict_json, validate_command_set

ROOT = Path(__file__).resolve().parent


def read_json(name: str):
    return strict_json((ROOT / name).read_bytes())


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids: list[str] = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_ids.append(test.id())


def iter_refs(value):
    if isinstance(value, dict):
        if set(value) == {'kind', 'id', 'sha256', 'path'} and all(isinstance(x, str) for x in value.values()):
            yield value
        for child in value.values():
            yield from iter_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_refs(child)


def run_verification() -> dict:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        raise RuntimeError('jsonschema is required; no installation was attempted')
    schema = read_json('schemas/runtime.schema.json')
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    names = ['command-set', 'command-set-shell', 'final-launch-plan',
             'process-identity', 'transport-summary']
    positives = {name: read_json(f'fixtures/{name}.json') for name in names}
    for value in positives.values():
        validator.validate(value)
    for name in ('command-set', 'command-set-shell'):
        validate_command_set(positives[name])

    # Each mutation is a structural fault; unrelated semantic guards remain separate.
    negatives = []
    def negative(name, source, mutate):
        value = copy.deepcopy(positives[source])
        mutate(value)
        if validator.is_valid(value):
            raise AssertionError(f'schema accepted negative fixture: {name}')
        negatives.append(name)

    negative('unknown-record-version', 'command-set', lambda v: v.update(schema_version=99))
    negative('extra-root-field', 'command-set', lambda v: v.update(secret='SYNTHETIC-NOT-A-SECRET'))
    negative('required-not-boolean', 'command-set', lambda v: v['commands'][0].update(required=1))
    negative('timeout-zero', 'command-set', lambda v: v['commands'][0].update(timeout_seconds=0))
    negative('negative-expected-exit', 'command-set', lambda v: v['commands'][0].update(expected_exit_code=-1))
    negative('empty-argv', 'command-set', lambda v: v['commands'][0]['by_platform']['linux'].update(argv=[]))
    negative('null-in-argv', 'command-set', lambda v: v['commands'][0]['by_platform']['linux'].update(argv=['x',None]))
    negative('variant-overrides-required', 'command-set', lambda v: v['commands'][0]['by_platform']['linux'].update(required=False))
    negative('conflicting-carriers', 'command-set', lambda v: v['commands'][0].update(argv=['x']))
    negative('script-missing', 'command-set-shell', lambda v: v['commands'][0].pop('script'))
    negative('invalid-platform', 'final-launch-plan', lambda v: v.update(platform='wsl'))
    negative('tty-not-supported', 'final-launch-plan', lambda v: v.update(tty=True))
    negative('pid-zero', 'process-identity', lambda v: v.update(pid=0))
    negative('hash-not-lowercase-sha256', 'process-identity', lambda v: v['executable_ref'].update(sha256='Z'*64))
    negative('invalid-started-type', 'transport-summary', lambda v: v.update(started='yes'))
    negative('negative-stream-count', 'transport-summary', lambda v: v['stdout'].update(observed_bytes=-1))

    # These are NOT valid production records. Demonstrate that structure alone
    # cannot enforce coverage, backend/platform matching, or evidence consistency.
    gaps = []
    def semantic_gap(name, source, mutate):
        value=copy.deepcopy(positives[source]); mutate(value)
        if not validator.is_valid(value):
            raise AssertionError(f'structural boundary unexpectedly changed: {name}')
        gaps.append({'case': name, 'schema_only_accepts': True,
                     'production_semantic_guard_required': True})
    semantic_gap('missing-declared-platform', 'command-set',
                 lambda v: v['commands'][0]['by_platform'].pop('macos'))
    semantic_gap('backend-platform-mismatch', 'final-launch-plan',
                 lambda v: v.update(backend_id='windows_job_v1'))
    semantic_gap('not-started-with-exit', 'transport-summary', lambda v: v.update(started=False))
    semantic_gap('complete-without-eof', 'transport-summary', lambda v: v['stdout'].update(eof=False))

    json_paths = sorted(ROOT.rglob('*.json'))
    json_paths = [p for p in json_paths if '__pycache__' not in p.parts]
    ref_count=0; targets={}
    for path in json_paths:
        value=strict_json(path.read_bytes())
        for ref in iter_refs(value):
            relative=Path(ref['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise AssertionError('unsafe fixture resource path')
            target=(ROOT/relative).resolve(strict=True)
            if not target.is_relative_to(ROOT.resolve()):
                raise AssertionError('fixture reference escapes package')
            actual=hashlib.sha256(target.read_bytes()).hexdigest()
            if actual!=ref['sha256']:
                raise AssertionError(f'fixture digest mismatch: {relative}')
            ref_count+=1;targets[ref['path']]=actual
    vectors=read_json('fixtures/golden-vectors.json')
    for vector in vectors:
        data=canonical_bytes(vector['value'])
        if data.hex()!=vector['utf8_hex'] or hashlib.sha256(data).hexdigest()!=vector['sha256']:
            raise AssertionError('golden vector mismatch')

    log=io.StringIO()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern='test_*.py')
    start=time.monotonic()
    result=unittest.TextTestRunner(stream=log,verbosity=2,resultclass=RecordedResult).run(suite)
    elapsed=time.monotonic()-start
    bash=shutil.which('bash')
    bash_version=None
    if bash:
        observed=subprocess.run([bash,'--version'],capture_output=True,text=True,
                                env={'PATH':os.defpath},timeout=3,check=True)
        bash_version=observed.stdout.splitlines()[0]
    native_pass=[x for x in result.passed_ids if x.startswith('test_native.')]
    pure_pass=[x for x in result.passed_ids if x.startswith('test_contracts.')]
    return {
        'record_type':'reference_validation_report',
        'scope':'synthetic reference only; not project acceptance or production runtime',
        'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'environment':{'python':sys.version, 'platform':platform.platform(),
                       'machine':platform.machine(), 'container_execution':True,
                       'bash':bash_version, 'pwsh_available':shutil.which('pwsh') is not None,
                       'jsonschema_version':importlib.metadata.version('jsonschema')},
        'schema':{'meta_validated':1,'positive_samples':len(positives),
                  'negative_samples_rejected':len(negatives),'negative_cases':negatives,
                  'semantic_boundary_demonstrations':gaps},
        'strict_json_files_checked':len(json_paths),
        'resource_references_checked':ref_count,
        'unique_resource_targets_checked':len(targets),
        'resource_target_hashes':targets,
        'golden_vectors_passed':len(vectors),
        'tests':{'run':result.testsRun,'passed':len(result.passed_ids),
                 'contract_tests_passed':len(pure_pass),'linux_synthetic_tests_passed':len(native_pass),
                 'skipped':len(result.skipped),'failures':len(result.failures),'errors':len(result.errors),
                 'skipped_cases':[{'id':t.id(),'reason':r} for t,r in result.skipped],
                 'elapsed_seconds':round(elapsed,6),'passed_ids':result.passed_ids},
        'overall_reference_success':result.wasSuccessful(),
        'not_run':[
            'actual project python tests/test_dispatch.py and G0-G6',
            'actual 03/04/07 integration, authorization, budget or persistent handoff',
            'Windows native, Job Objects and PowerShell wrapper',
            'macOS native, Bash 3.2 and WSL',
            'real Pi, another CLI, model calls or billing',
            'production redaction, TOCTOU defense, hard-kill supervisor and power-loss recovery',
            '30 project integration/fault-injection scenarios in chapter 7.4'
        ],
        'test_log':log.getvalue(),
    }


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,help='new file outside the reference package')
    args=parser.parse_args()
    if args.report and (args.report.resolve().is_relative_to(ROOT.resolve()) or args.report.exists()):
        parser.error('report must be a new file outside the reference package')
    try:
        report=run_verification()
    except Exception as exc:
        print(json.dumps({'overall_reference_success':False,
                          'failure_type':type(exc).__name__,
                          'scope':'reference verification; inspect locally'},ensure_ascii=False))
        return 1
    text=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.report:
        with args.report.open('x',encoding='utf-8',newline='\n') as stream:stream.write(text)
    else:print(text,end='')
    return 0 if report['overall_reference_success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
``````

### B.23 `wrappers/dispatch-stub.ps1`

<!-- reference-file: wrappers/dispatch-stub.ps1; encoding: utf-8 -->
``````powershell
# Proposed PowerShell 7.3+ forwarding example. NOT native-tested in this deliverable.
# Start the host with -NoProfile -NonInteractive. This is not a supervisor/backend.
if ($PSVersionTable.PSVersion -lt [version]'7.3') {
    [Console]::Error.WriteLine('E_WRAPPER_VERSION: use an approved supported entry.')
    exit 2
}
$PSNativeCommandArgumentPassing = 'Standard'
$PSNativeCommandUseErrorActionPreference = $false
$ErrorActionPreference = 'Stop'
try {
    $python = $env:CODEKIT_BOOTSTRAP_PYTHON
    if ([string]::IsNullOrWhiteSpace($python)) {
        [Console]::Error.WriteLine('E_BOOTSTRAP_ENTRY: a trusted Python path is required.')
        exit 2
    }
    if (-not [IO.Path]::IsPathRooted($python) -or -not [IO.File]::Exists($python) -or
        [IO.Path]::GetExtension($python) -ine '.exe') {
        [Console]::Error.WriteLine('E_BOOTSTRAP_ENTRY: expected an approved native .exe.')
        exit 2
    }
    $target = Join-Path $PSScriptRoot '../fixtures/process_stub.py'
    & $python $target @args
    $childExit = $LASTEXITCODE
    if ($null -eq $childExit) {
        [Console]::Error.WriteLine('E_BOOTSTRAP_EXIT: missing child exit status.')
        exit 2
    }
    exit $childExit
} catch {
    # Never print full exception text, environment values, or argv.
    [Console]::Error.WriteLine('E_BOOTSTRAP_START: Python entry failed.')
    exit 2
}
``````

### B.24 `wrappers/dispatch-stub.sh`

<!-- reference-file: wrappers/dispatch-stub.sh; encoding: utf-8 -->
``````bash
#!/usr/bin/env bash
# Bootstrap demonstration only: forwards to the synthetic stub, not project code.
set -u
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 2
python_cmd=${CODEKIT_BOOTSTRAP_PYTHON:-python3}
exec "$python_cmd" "$base/../fixtures/process_stub.py" "$@"
``````


## 附录 C：本次实际验证与未运行项

### C.1 验证对象与方法

本次从最终 Markdown 的具名代码块重新提取 **23 个参考文件**，核对每个文件与编写阶段源文件的完整字节一致，再在提取目录运行 `verify.py`。没有把工作副本的通过结果直接当成文档代码已经通过。

验证环境为当前容器的 Linux x86_64、Python **3.13.5**、GNU Bash **5.2.37**、预装 jsonschema **4.26.0**。当前环境没有 `pwsh`。这些版本是本次观察值；联网引用的 Python 3.14/PowerShell 7.5 文档用于说明 API 行为，不表示本次在这些文档版本对应环境执行了测试。

### C.2 实际结果

| 检查对象 | 本次结果 | 可证明的范围 |
|---|---|---|
| 具名文件提取及字节比较 | **23/23 一致** | 最终 Markdown 可恢复这份参考包；不含文件权限/OS 安装状态证明 |
| `test_contracts.py` | **70/70 通过** | 严格输入、平台选择、静态路径、环境过滤、停止/退出/局部结果与身份规则 |
| `test_native.py` | **13/13 通过** | 当前 Linux 环境的合成子进程、受控组信号、Bash wrapper 和有限管道行为 |
| 单元/合成测试合计 | **83/83 通过，0 跳过、0 失败、0 错误** | 不包含用户仓库或其他平台 |
| JSON Schema | **1 份根 Schema 元验证通过**；**5 个正例**通过、**16 个反例**拒绝 | 结构契约；四个额外边界演示明确标出仍需语义守卫的组合 |
| 严格 JSON 读取 | **11 个文件通过** | 参考 Schema、合成记录与黄金向量的输入合法性 |
| 引用完整字节哈希 | **11 处引用、10 个不同资源目标核验通过** | 合成资源与引用相符，不证明环境或授权真实 |
| 固定序列化黄金向量 | **10/10 通过** | UTF-8/LF、Unicode 原值、参数字符、整数/浮点表示等具体字节 |

**四个 Schema 边界演示不是“合法生产样例”**：缺声明平台、平台/后端不匹配、未启动却有退出值、流未 EOF 却声称完整，都可能结构合法；必须经应用语义拒绝。其中平台覆盖已由参考 `validate_command_set` 实现，其他完整生产交接守卫仍待实施。这些演示专门防止将 Schema 通过误当执行许可。

### C.3 13 个 Linux 合成场景的明细

| 测试 | 实际覆盖 |
|---|---|
| argv 往返 | 18 类参数，包括空项、空格、中文、emoji、引号、反斜线、特殊符号与 CRLF |
| 原生退出 | 0、1、2、3、4、17 的子进程实际返回；这些是数据点，不擅自命名旧业务码 |
| 双流采集 | stdout/stderr 各 64 KiB，独立保留 |
| stdin 与 EOF | 含中文、CRLF、NUL 及非 UTF-8 字节的输入按字节回传 |
| Unicode cwd | 专用临时目录中的中文/空格目录 |
| 明确启动失败 | 不存在的测试入口抛出启动异常，没有目标退出码 |
| 超时后受控停止 | `communicate` 超时后明确向测试拥有的组发送 TERM，不假设其自动回收树 |
| 组取消与后代回收 | 协作式合成父子进程同组，父进程回收自己的子进程；不是任意后代树 |
| TERM 后升级 | 已知单个合成子进程忽略 TERM 后，KILL 并观察退出 |
| Bash wrapper 参数 | 指定合成 stub 的参数保持 |
| Bash wrapper 返回 3 | 保持目标返回值 3；不是原项目 wait 端到端调用 |
| Bash 语法/程序失败 | 分别执行语法预检与正常脚本非零退出，不仅比较整数 |
| pipefail 行为 | 相同合成 pipeline 在明确 flag 差异下结果不同，说明 flag 需要锁定 |

测试通过不能推广为 `setsid` 逃逸、未知 PID 恢复、调度器 SIGKILL、磁盘故障或生产 I/O supervisor 均已正确。这些测试直接调用标准库/合成脚本，不经过用户实际 `run_supervised`。

### C.4 明确未运行

原项目 `python tests/test_dispatch.py`、真实源码迁移、03/04/07 授权/审核/预算/持久 handoff、G0–G6、Windows Job 与 PowerShell、macOS/Bash 3.2、WSL、真实 Pi/第二 CLI/模型费用、生产脱敏、TOCTOU 防护、supervisor 强杀及断电恢复均为 **NOT_RUN**。第 7.4 的 30 项真实集成/故障场景是待执行设计，不包含在 83 项结果内。

本次报告附带逐个测试 ID、日志、环境、资源摘要和未运行项。它是参考验证记录，不是项目发布证明或已审核合同。

## 附录 D：原文追溯与新增边界

| 原文内容 | 保留位置 | 新增细化，不改变的底线 |
|---|---|---|
| 原文状态、目标/非目标 | 开篇、第 1 章 | 新原型不等于已实施；无安装/模型/CI 授权 |
| 依赖 04、复用 01/02、环境与语义锁分离 | 第 2 章、4.1.5、4.2 | 新对象需要注册与跨模块定稿，不自行改变审批 |
| 现有函数及 wrapper 复用 | 第 3、5 章 | 保留“原文记载”归属，不宣称已核验源码 |
| 共同字段、载体互斥、完整平台覆盖 | 4.1.1–4.1.4、A、B | 不因 optional 缺平台而 skip-pass；全集合先验 |
| pathlib、cwd、环境白名单、同源渲染 | 4.1.7–4.1.9 | 补充真实路径竞态、环境名称、展示不执行 |
| Linux/macOS/Windows 入口与 shell 版本 | 4.2 | 新 PS 示例仅建议，不冒充 5.1/7.x 均已通过 |
| 原样参数、退出码、wait 3 | 4.2.5–4.2.7、4.3.11 | 版本化转发与目标回显；不重命名旧 1/2/4 |
| 超时、取消、Job/group、强杀恢复 | 4.3、第 8 章 | 分开主退出/EOF/清理；不承诺 POSIX 全后代回收 |
| Windows/WSL 边界、CLI 内部 Bash | 2.4、4.2.8 | 区分技术互操作与项目禁混用策略 |
| 文件范围、AGENTS/技能保护、CI 范围 | 第 5、6 章 | 实施授权另定，不令普通执行器越权 |
| 六个实施步骤 | 第 6 章 | 增加产物、进入条件与失败升级 |
| 05-A01–05-A10 | 7.1 原样保留 | 7.2 新编号为建议；不以原型测试冒充原验收 |
| 兼容与恢复 | 第 8 章 | 不覆盖旧证据、不盲杀 PID、不自动重跑未知启动 |
| 四个合同工作包 | 第 9 章 | 补足输入/交付/边界，仍不是已获准合同 |

**需要审定的新增选择**：CommandSet/记录家族与字段名、平台键精确集合规则、expected_exit 首版范围、时间/输出初始上限、停止竞态裁决、no_background_children、Windows 创建期 Job 方案、结构 Schema、参考错误代码、新增验收项目。保留这些选择的明确建议性质，避免实施人员误以为原文已经决定全部细节。

## 附录 E：依据与官方技术资料

### E.1 使用说明

上传文件是项目要求的依据；网络资料仅用于解释平台/API 的机制和潜在差异。资料核对日期为 **2026-09-25**；页面抓取与实际环境验证是两件事。引用不表示该文档是所有相关软件的最新发布版本，也不表示用户已经安装它。正式实施需把实际软件版本、依赖和采集证据固定进环境锁。

下列官方文档主要用作事实来源，正文中的架构、状态机、限额与验收流程是本版提出的工程方案。GNU 当前手册的部分入口本次访问失败，因此 Bash 启动行为使用可读取的 GNU 历史手册，并明确其历史版本；没有将历史手册冒称当前 Bash 3.2/5.x 的完整验证。具体目标 shell 仍须实际探测。

[^P05]: 用户上传 `05-cross-platform-runtime.md`，共 141 个文本行；主要对应第 1–9 节。原文件完整字节 SHA-256：`f9cc508af91b4f226025e380e02e353d34ecbd8cda4c5e1db3361e3fd9459bf9`。原文是拟议实施说明，不是源码或已执行报告。

[^P01]: 用户提供的 `01-baseline-and-protocols.md`，重点第 4.1–4.4 节：公共标识、ResourceRef、固定序列化、事件及旧退出语义。完整字节 SHA-256：`49b880b87c037b00f73de0214c6859d60f268fb10d569f7186d1dae7834b18c2`。02/03/04 原文也用于核对模块所有权，不依它们推定尚未实现能力。

[^P03E]: 本会话 `03-approvals-and-locks-审阅增强版.md`，本次重点核对 4.3 的一次性准入、撤销/控制代际、START_UNKNOWN 与恢复相关段落。完整字节 SHA-256：`5727ab30e73962fd8a0dfefe3a250885624657bdc91423b0210bbc0b5b26cae2`。其中新增协议仍属于建议。

[^P04E]: 本会话 `04-cli-adapters-and-pi-审阅增强版.md`，本次重点核对 2.1–2.3、4.1.4–4.1.8 的 Invocation、FinalLaunchPlan、TransportSummary、环境过滤与 05 所有权。完整字节 SHA-256：`14deca96b56e7d6a9ed1d0676d60805548122acd73a9b18cb9d966bec0bbd376`。没有把原型当作已部署 adapter。

[^W01]: Python 官方文档，subprocess（固定文档路径 3.14）：<https://docs.python.org/3.14/library/subprocess.html>。用于启动/参数、批处理例外、管道、timeout、returncode 与进程选项边界；本次实际 Python 为 3.13.5，非 3.14 原生验收。

[^W02]: Microsoft Learn，Job Objects：<https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects>。用于 Job 成员、继承/脱离、句柄关闭和监督范围边界。

[^W03]: Microsoft Learn，AssignProcessToJobObject：<https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject>。用于归属操作、已有 Job/限制及失败处理。

[^W04]: Microsoft Learn，PowerShell about_Parsing（7.5 文档视图）：<https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing?view=powershell-7.5>。用于 7.3 参数传递变化、Legacy/Standard/Windows 模式及空参数/引号差异。

[^W05]: Microsoft Learn，Start-Process：<https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process?view=powershell-7.5>。用于 `ArgumentList` 数组合并成命令行的限制，不能直接等同可靠 argv 边界。

[^W06]: Microsoft Learn，.NET ProcessStartInfo.ArgumentList：<https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.processstartinfo.argumentlist?view=net-10.0>。用于区分 .NET 集合 API、Arguments 字符串与 PowerShell cmdlet 的不同层次。

[^W07]: GNU 官方历史 Bash Reference Manual 2.05a，Bash Features / Startup Files：<https://ftp.gnu.org/old-gnu/Manuals/bash-2.05a/html_chapter/bashref_6.html>。仅作为可读取的历史启动机制说明，非当前版本完整兼容承诺；当前官方手册部分入口本次未能成功读取。

[^W08]: Zsh 官方文档，Files：<https://zsh.sourceforge.io/Doc/Release/Files.html>。用于全局/用户启动文件及不能简单靠 `-f` 声称零隐式读取的边界。

[^W09]: Microsoft Learn，Naming Files, Paths, and Namespaces：<https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file>。用于 Windows 路径、名称、命名空间及保留规则。

[^W10]: Microsoft Learn，PowerShell about_Automatic_Variables：<https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_automatic_variables?view=powershell-7.5>。用于 `$LASTEXITCODE`、`$?` 与调用方式的区别。

[^W11]: Microsoft Learn，PowerShell about_Character_Encoding：<https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-7.5>。用于 Windows PowerShell 与较新 PowerShell 的默认编码差异。

[^W12]: Microsoft Learn，Working across Windows and Linux file systems：<https://learn.microsoft.com/en-us/windows/wsl/filesystems>。用于 WSL 互操作及跨文件系统差异；本文的禁混用要求是项目策略。

[^W13]: Python 官方文档，time：<https://docs.python.org/3.14/library/time.html>。用于单调时钟的参考点和持续时间语义。

[^W14]: Linux man-pages，setsid(2)：<https://man7.org/linux/man-pages/man2/setsid.2.html>。用于 session/process group 变更及组级清理边界。

[^W15]: Linux man-pages，PR_SET_PDEATHSIG：<https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html>。用于父死亡信号的线程/fork/exec 条件，不推导全后代保证。

[^W16]: Linux man-pages，kill(2)：<https://man7.org/linux/man-pages/man2/kill.2.html>。用于 PID/进程组及 signal 0 的存在性/权限检查语义。

[^W17]: Microsoft Learn，Process Creation Flags：<https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags>。用于挂起创建、控制台/进程组标志及能力条件。

[^W18]: Microsoft Learn，ResumeThread：<https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread>。用于持有有效线程句柄并恢复挂起线程的要求。

[^W19]: Microsoft Learn，GenerateConsoleCtrlEvent：<https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent>。用于控制台、进程组以及 CTRL_C/CTRL_BREAK 的作用条件。

[^W20]: Linux man-pages，pidfd_open(2)：<https://man7.org/linux/man-pages/man2/pidfd_open.2.html>。用于对特定进程的稳定引用，不等于所有后代或跨重启可用句柄。

[^W21]: Microsoft Learn，Process Handles and Identifiers：<https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers>。用于句柄和数字 PID 的生命周期区别。

[^W22]: Python 官方文档，json：<https://docs.python.org/3.14/library/json.html>。用于重复名称、数值与默认解析行为，支持严格入口设计。

[^W23]: JSON Schema 官方规范，Validation 2020-12：<https://json-schema.org/draft/2020-12/json-schema-validation>。用于结构/整数/format 约束及其应用边界；验证器不承担操作系统语义。

[^W24]: Python 官方文档，asyncio subprocess：<https://docs.python.org/3.14/library/asyncio-subprocess.html>。用于异步子进程、管道读取与平台支持说明。

[^W25]: Microsoft Learn，UpdateProcThreadAttribute：<https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute>。用于 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 和 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 的区别及版本条件；创建期 Job 关联仍须实际后端验证。
