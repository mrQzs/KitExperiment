# 04 CLI 适配器与 Pi 迁移（审阅增强版）

状态：**拟议实施说明，尚未在实际项目实现**；不是已审核合同，不授权真实模型调用、CLI 安装、凭据迁移或修改受保护调度核心。

总纲：[完整设计](../code-kit-v2-design.md)；导航：[实施索引](README.md)。  
对应总设计：第 4.3、9 节及 P1，向 11 的第二 CLI 接入提供接口。  
审阅日期：**2026-09-25（Asia/Tokyo）**。模块编号 04、适配器 API 版本、CLI 软件版本、厂商事件版本和合同 Schema 版本相互独立。

> **阅读约定**：保留原文的 1–9 章、4.1–4.4 主线及 `04-A01`–`04-A09`。标为“原文保留”的内容沿用上传材料；新增字段、状态、上限、Schema、算法、测试及实施拆分均为**本版建议**，需要正式实施合同审定。外部资料仅用于识别协议差异与完善设计，不替代用户环境的实际取证。本文参考代码全部运行于人工合成协议，不是从 Pi 捕获的 fixture，也不是可直接投产的 Pi adapter。

**本次可确认与不可确认的范围**：已读上传的 04 原文，结合 01–03 原文与本会话提供的三份增强版核对公共接口；没有取得 `scripts/pi_dispatch.py`、`tests/test_dispatch.py`、真实 Pi 样本、项目 AGENTS、实际总设计及 05–11 完整正文。因此，原文对现有函数的描述保留为“原文记载”，不升级为本次源码实测结论。保留原导航链接，但未验证未提供文档的内容或路径可访问性。

**阅读路径**：正文 1–9 为实施说明；附录 A 为结构契约，B 为可提取源码与夹具，C 为本次验证，D 为原文追溯，E 为资料来源。

## 1. 目标与非目标

### 1.1 原文保留

把 Pi 专有参数、能力探测和事件解释从调度核心移入 adapter，Pi 成为首个内置实现。不同角色可选择不同已支持 CLI，核心只处理统一请求、能力和证据。

保持现有模型核对、工具审计、终止判定和候选门禁；不能以兼容新 CLI 为由减弱它们。不把替换 argv 第一个元素视为接入新 CLI，也不以 stub 测试冒充真实协议支持。

adapter 构建调用并解释协议；进程启动、超时、取消、进程树和 shell 执行由 05 负责。不在本模块选择第二 CLI 或预设其命令参数，具体选择与证据验收交给 11。[^P04]

### 1.2 审阅结论与优先补强项

原文已经划清了职责和证据底线，但接口名称还不足以独立实施。建议先补齐下表，再拆迁移合同；不要同时升级 Pi、替换模型和重写调度器。

| 缺口 | 可能产生的错误 | 本版落点 |
|---|---|---|
| `probe` 与“不自行启动进程”的实现关系未展开 | adapter 内部直接执行帮助命令、自动刷新认证 | 4.1.4 的计划—执行—解释三段式 |
| Invocation 缺字段归属及锁定规则 | 参数在审核后被覆盖，stdin 与文件重复送入 | 4.1.5–4.1.8 |
| 能力用布尔值概括 | 将“文档提到”当成“当前平台实测支持” | 4.2.1–4.2.3 |
| 终止判据未按协议版本定义 | 提前结束，或将正常的多轮流程当成损坏 | 4.3.4、4.4.2 |
| 模型证据层级与来源未具体化 | 把请求值回填成实际模型、只检查最后一轮 | 4.2.4–4.2.5 |
| 工具事件、流重放、用量去重尚无算法 | 丢失工具调用，重复计费，伪造报告替代检查 | 4.3.5–4.3.9 |
| 原生资源禁用边界不完整 | 用户/项目配置、上下文文件、显式扩展改变行为 | 4.4.3 |
| 异常后的恢复与回退缺少成功判据 | 覆盖旧证据、盲目重启真实模型进程 | 第 8 章 |

### 1.3 不可弱化的不变量

| 编号 | 不变量 |
|---|---|
| I01 | `request_id` 来自核心，是预算根；重试、切换 CLI、厂商 session 变化均不替换它。 |
| I02 | profile 解析结果、adapter 描述符、请求字节、环境记录都要按固定引用消费；不重新追踪可变名称。 |
| I03 | 04 不登记审核、不扩大工具/网络/文件权限、不替 07 结算预算、不签发 G0–G6 verdict。 |
| I04 | 标准化结果必须可回查脱敏原始证据；无法解释的关键内容不能被过滤成正常成功。 |
| I05 | 退出码 0、`agent_end`、会话稳定、报告完成、候选 PASS、集成完成是不同事实。 |
| I06 | 必要能力不足时阻塞；不得自动换模型、降低思考等级、转文本模式或切回 v1。 |
| I07 | 请求模型、CLI 元数据、提供商响应元数据分开保存；缺失值不互相填补。 |
| I08 | 原始流的重复**内容**不等于同一事件重放；同一证据的多种展示不等于多次用量。 |
| I09 | 观察到工具越权不等于已经预防越权；工作目录和工具白名单不等于 OS 沙箱。 |
| I10 | 新 parser、新终止规则和新脱敏规则只能形成可追溯新视图，不能重写历史原始证据。 |

### 1.4 信任边界与保证上限

受信输入包括核心注入的 RunContext、受控注册表、已封存引用及当前授权/策略判定。厂商 stdout/stderr、模型文本、插件声明、session 文件及导入 fixture 均按不受信数据处理。

在相同操作系统账户、无隔离的部署中，恶意进程可能修改文件、访问凭据或伪造 CLI 字段。哈希、流程分层、精确模型匹配只能在声明的信任边界内提供完整性与可追溯性，不能证明远端真实权重，也不能替代主机隔离。Pi 当前安全说明同样将权限隔离交给外围环境；不得将本设计描述成新增了 Pi 内置沙箱。[^W01][^W09]

## 2. 依赖与输入输出

### 2.1 原文保留与模块分工

依赖 01 基准与公共协议、02 配置与角色绑定。输入是锁定的角色请求、有效 profile、平台和工作目录、允许工具、协议能力要求；输出 Invocation、能力/版本探测记录、标准化事件、结果报告与用量/身份的证据等级。使用 01 的事件、资源引用和 `request_id`/`run_id`，不得用 vendor session ID 替代本地 run ID。03 的审核锁定在调度层，adapter 没有批准或发布合同的权限。[^P04]

| 模块 | 本次交接的所有权 | 04 不得代办 |
|---|---|---|
| 01 | 公共 ResourceRef、固定字节序列化、事件封装、稳定错误 | 为资源重新发明哈希规则，改旧退出码 |
| 02 | profile 解析、参数归属、相关语义闭包、环境计划 | 从任意配置路径动态导入 adapter 或执行 probe |
| 03 | 输入审核、精确环境锁、当前授权校验、单次准入 | adapter 自行批准、复活已撤销准入 |
| 04 | CLI 参数合同、协议解释、能力证据、结果归约 | 安装 CLI、发布合同、预算扣账、候选验收 |
| 05 | 入口解析与指纹、受控进程执行、管道、取消、进程身份 | 把 runner 的进程事实猜成 vendor 事件 |
| 06 | 经核心编译的任务插件及锁资源 | 将任务插件自动安装为 Pi 原生扩展 |
| 07 / 既有调度器 | 任务领取、重试、预算、候选与门禁的编排 | adapter 自动重试或直接产出 PASS |
| 10 | 项目生命周期索引与 brief | 将 vendor 事件当成审核或集成事件 |
| 11 | 第二真实 CLI 的选择及证据接入 | 用改 argv 的办法声称通用接入已完成 |

后续模块的职责仅按本次可见原文交接点描述；没有审阅未提供的完整设计，不据此扩充其既有实现事实。

### 2.2 推荐的数据流

```text
02：静态配置 + 受信描述符 → 有效 profile / ConfigSnapshot / EnvironmentPlan
                                 ↓
04：构造 ProbePlan → 核心核准探测 → 05 执行 → 04 解释 ProbeObservation
                                 ↓
04/05：生成实测能力与 EnvironmentRecord（不是 fixture 冒充实测）
                                 ↓
03：将合同、请求、配置、环境、描述符等固定引用纳入审核
                                 ↓
07/03：当前授权、预算、领取、epoch/generation、单次 operation 准入
                                 ↓
04：构造 Invocation → 核心核对意图 → 05 受控 handoff / 启动
                                 ↓
05/01：采集、脱敏、封存 → 04：分帧、规范化、归约 → ResultBundle
                                 ↓
07/既有门禁：模型核对、工具审计、独立检查、固定候选验收
```

“审核记录已提交”与“OS 进程已启动”不是天然原子事务。沿用 03 增强版的单次准入和 `START_UNKNOWN` 边界；04 不增加一个本地布尔值来取代实际 handoff 证据。03 当前建议 `exact` 环境锁：路径、入口链或实测能力变化，需要新环境记录及相应重新审核，不能以语义哈希未变自动复用。[^P03E]

### 2.3 输入对象与字段归属

| 对象 | 至少包含 | 来源/校验 |
|---|---|---|
| `AdapterRequest` | request/task/run、role、profile/config ref、环境 ref、请求 ref、工具/模型要求 | 核心固定对象；adapter 不覆盖 |
| `ResolvedRuntime` | 已登记 adapter、目标平台、入口链绑定、固定参数、协议 ID | 02/05；名字相同不等于内容相同 |
| `RoleRequirements` | 必要能力、可接受证据类别、模型匹配规则、报告要求 | 工作流/合同及既有 G1，不由 CLI 自定 |
| `ResourceRef` | `kind,id,sha256`；文件资源含相对 `path` | 沿用 01；kind 须注册 |
| `RawRecord` | stream、分段引用、字节范围、记录序号、解析/脱敏状态 | 01/05；核心提供本地顺序 |
| `TransportSummary` | 是否启动、退出码/信号、EOF、采集完整性、取消/超时、进程身份引用 | 05；厂商 JSON 不能伪造 |
| `ResultBundle` | 协议、身份、工具、报告、用量、异常和来源引用 | 04 派生；不含有约束力的 PASS |

`AdapterRequest` 不含明文密钥、完整环境、审核 token 或任意输出路径。请求正文、附件和输出目录由核心分配，adapter 只引用已固定资源。临时对象尚未封存时可用于预览，但不能当成最终审核输入。

## 3. 现状与复用点

### 3.1 原文记载的迁移位置

下表中的“现有行为”来自上传原文，不表示本次已经检查实际仓库。

| 原文位置 | 原文记载 | 建议提取到 | 迁移前必须补录 |
|---|---|---|---|
| `resolve_pi_command` | 可执行入口、覆盖变量、Windows npm shim | Pi 入口解释 + 05 平台解析 | wrapper/解释器链、PATH、引号、失败码 |
| `pi_preflight` | 参数、模型列表与认证就绪核验 | Pi probe 计划与解释器 | 实际命令、离线性质、认证副作用 |
| `summarize_events` | JSON 流、模型、工具、最终报告 | Pi parser/reducer + 兼容投影 | 完整真实样例、错误优先级、G1 输入 |
| `cmd_run` | 总调度、证据目录、候选流程 | 保留核心，仅接标准接口 | 新旧调用路径与候选证据一致性 |
| `RESOURCE_FLAGS` | 关闭扩展、技能和提示模板自动发现 | Pi 参数合同 | 实际常量、支持版本、未覆盖资源 |
| `tests/stub_pi.py` / help fixture | 离线协议样本 | 保留原文件，新增来源 Manifest | stub 来源、与真实协议的差异 |

**迁移基线原则**：先保持输出事实、关键失败类别和门禁语义一致，再讨论增强行为。不能同时重写事件格式、模型别名算法和报告生成规则，然后用几个新测试声称等价。[^P04]

### 3.2 外部核验发现：不直接替换用户的锁定版本

本次检索的是上游公开页面。`badlogic/pi-mono` 页面重定向至 `earendil-works/pi`；这有助于找到资料，但不能据此改变现有 runtime 或安装包。**没有获得用户安装版本，也没有取得本次页面的固定 commit ID；资料登记为 `DOCUMENTED_NOT_LOCAL_VERIFIED`，不是精确支持矩阵。**[^W01]

| 本次外部资料所示事实 | 对本设计的影响，不是自动实施指令 |
|---|---|
| JSON 模式区分低层 `agent_end` 与会话级 `agent_settled`；前者之后可能继续工作 | 每个协议 profile 声明自己的终止规则；不把当前规则套回旧 fixture。[^W02] |
| JSONL 以 LF 分帧，允许其前有 CR；Unicode 分隔符不应成为记录边界 | 按字节分帧；不直接使用 `str.splitlines()`；旧协议差异独立版本化。[^W02] |
| 当前 CLI 文档允许模糊模型匹配，并说明 thinking 可能按模型能力调整 | Code-Kit 必须先固定精确 ID，再核对实际证据，不能把 CLI 成功解析当成精确匹配。[^W03] |
| 完成消息中存在 CLI 模型字段及可选 `responseModel` / `providerThinkingLevel` | 缺可选字段不等于已获得提供商确认；不得请求值回填。[^W04] |
| 帮助可包含已加载扩展登记的参数；认证检查可能刷新 OAuth，另有输出凭据的命令 | probe 必须限制加载来源、网络和输出；不执行打印凭据命令。[^W03] |
| 禁止自动发现不取消显式资源加载；上下文文件与项目配置有独立入口 | 必须审查完整资源输入面，不能只检查几个 `--no-*`。[^W03][^W05] |
| 当前文档分别列出 `bash` 与 Windows `powershell` 工具 | 保留原文“不自动把 bash 变为 PowerShell”；新能力须逐平台取证。[^W03] |
| 当前 print-mode 源码对 text 和 JSON 的错误处理分支并不完全相同 | 再次说明不能只凭退出码判断模型工作完成；该观察限于所查源码。[^W08] |

以上是检索日观察，不宣称已确定这些变化的最早发布版本。具体版本号、包名、指纹、安装来源、入口链和事件样本必须在实施 S0/S2 阶段补齐；未知即留空/未知，不猜测 semver 区间。

### 3.3 本次未取得但实施必须取得的材料

必须补齐：实际 Git HEAD 与脏工作区、原 `RESOURCE_FLAGS` 字节、原始 G1 判定、Pi 完整 `--help`/版本记录、模型列表来源与副作用说明、无敏感数据的真实 JSONL、Windows shim 解析证据、入口链文件清单，以及所有 v1 环境变量的现行冲突测试。

没有上述材料时可以交付通用协议和合成测试，不能签收 `04-A01` 的迁移等价或宣布真实 Pi 支持。

## 4. 接口和行为约束

### 4.1 最小适配器协议

#### 4.1.1 保留接口名称，补齐调用前提

| 接口 | 输入与输出 | 原文禁止行为及本版补充 |
|---|---|---|
| `probe(runtime)` | 命令/平台 → 版本、入口、协议与能力记录 | 无付费模型调用、无凭据打印；实际执行必须走 05 |
| `validate_profile(profile)` | 模型与选项 → 合法值、角色能力诊断 | 不静默改变模型/思考级别，不安装依赖 |
| `build_invocation(request)` | 锁定请求 → argv、输入描述、cwd、环境变量名 | 纯构造；不运行进程、不拼通用 shell 模板 |
| `normalize_event(raw)` | 原始记录 → 标准事件及引用 | 不丢弃未知记录；允许一条原始记录产生多个有来源的派生事件 |
| `collect_result(events)` | 完整/不完整事件流 → 状态、报告、身份、工具、用量 | 必须同时消费 05 的 TransportSummary 和锁定要求；不直接判 PASS |

上表仍是逻辑接口。具体实现增加显式参数，不通过全局变量读取当前 profile、环境或某个“最近 run”。建议签名：

```text
validate_profile(profile, descriptor, policy) -> ValidationResult
plan_probe(runtime, probe_policy) -> ProbePlan[]
interpret_probe(runtime, observations, descriptor) -> CapabilityReport
build_invocation(locked_request, descriptor) -> Invocation
normalize_event(raw_record, parser_context) -> NormalizeResult
collect_result(normalized_view, transport, role_requirements) -> ResultBundle
```

`probe(runtime)` 作为调度层的组合服务保留：构造计划 → 核心确认允许 → 05 执行 → adapter 解释。不得让 adapter 实现该名称时顺便调用 `subprocess`。这是消除原文接口缩写歧义的**新增实现建议**，不是宣称现有代码已经拆分。

`NormalizeResult` 包括 `events[]`、`diagnostics[]` 和显式 `next_state`/checkpoint。没有可解释内容时仍保留 `protocol.unknown_record` 或 `protocol.invalid_record`；`events=[]` 不能成为悄悄吞掉未知记录的手段。状态属于单个 `(run_id, view_id)`，不同运行不得共享可变 reducer。

#### 4.1.2 版本轴与协议选择

| 版本/身份 | 所解决的问题 | 选择规则 |
|---|---|---|
| `adapter_api_version` | 核心调用哪套 adapter 接口 | 核心注册表精确支持 |
| `descriptor_revision` + ResourceRef | model/options/参数/能力合同 | 与 02 语义快照绑定 |
| `parser_ref` / parser revision | 哪一实现解释原始流 | 固定引用；派生视图保留 |
| `protocol_id` | 厂商事件、结束、用量等具体规则 | 受信兼容矩阵，不按首条字段猜 |
| CLI package/version/build fingerprint | 实际执行的程序是什么 | 05 入口链实测，不仅看字符串 |
| session header version | 厂商 session 数据格式 | 只说明该格式，不能代替整个 CLI 协议版本 |
| 公共 event/result/invocation Schema | 核心对象形状 | 各自 family + version 分派 |

未知 adapter、未知接口版本、未知协议组合在启动前拒绝。厂商流未携带独立协议版本时，由受信矩阵绑定具体已验证的 CLI build/模式/资源策略到 `protocol_id`；不能伪造一个“Pi 协议版本”字段。

#### 4.1.3 受信注册表与描述符

建议 `AdapterDescriptor` 至少绑定 adapter ID、API 版本、model/options Schema 引用、参数合同引用、parser 引用、支持协议列表、平台范围、证据引用和发布来源。完整结构建议见附录 A。

注册表由受保护核心静态装配或经批准的版本发布生成。02 只接收已加载描述符及本地 Schema registry；项目配置、模型输出和 06 插件 Manifest 不能提供 Python import path、npm 包、任意 URL 或“下载后试试”的命令。

Schema 必须关闭未声明字段并离线解析引用。结构合法不证明来源可信；描述符内容、实现代码和测试证据必须分别绑定。更换 parser 或语义参数合同要形成新资源，不能保留旧描述符 ID 却后台热换实现。[^P02E]

#### 4.1.4 无模型调用的探测流程

**三种事实必须分开**：不产生模型推理费用、不发网络请求、不执行本地代码。帮助/版本检查通常仍会启动程序；认证命令也可能执行凭据获取命令或进行刷新。不能把这些动作统一叫“纯静态检查”。当前 Pi 认证资料还允许从命令解析 API key，因此仅不打印 key 并不足以证明 probe 无额外进程。[^W06]

| 阶段 | 执行主体 | 成功产物 | 失败/未知处理 |
|---|---|---|---|
| P0 静态核验 | 核心/05 | 已批准入口、完整入口链、参数合同 | 未识别 wrapper 不执行 |
| P1 计划生成 | 04 | 版本化 ProbePlan；禁止模型调用 | 未知 flag 不盲试多个变体 |
| P2 授权与环境准备 | 核心/03/05 | 清洁环境、隔离 cwd、明确网络策略 | 缺强网络隔离时记录保证上限 |
| P3 执行并采集 | 05 | stdout/stderr、退出码、超时、入口事实 | 输出超限或失败也保留诊断 |
| P4 解释 | 04 | 版本/帮助/能力/认证局部状态 | 不将帮助关键词命中当完整支持 |
| P5 封存 | 01/核心 | 探测报告与环境资源引用 | 未完成落盘不能被审核消费 |

建议首版 probe 上限：版本命令 5 秒、帮助 10 秒、本地模型清单 15 秒；一次预检总截止 30 秒，单命令 stdout+stderr 合计不超过 8 MiB。均为**待压测的设计初值**，不是 Pi 官方保证或本次实测性能。05 执行超时及输出限制，04 只声明要求。

认证状态建议用 `not_checked`、`available_local`、`not_available`、`invalid`、`remote_verified` 分开表达。环境中有变量名、凭据文件存在或本地 check 成功，都不证明远端账户可用、权限足够、模型可调用或余额充足。

默认不允许 probe 进行 OAuth 刷新、自动 catalog 更新、安装/更新、交互登录、浏览器打开、扩展初始化或凭据输出。仅在已证明目标版本支持时使用无刷新/离线选项；这些选项属于 CLI 请求约束，不替代 05 的网络和进程限制。不能因未知选项被拒就把限制删除后重试。

#### 4.1.5 Invocation 字段级契约

建议先构造**可审核的调用意图**，再由 05 将受控绑定解析成平台最终调用；二者分别封存。

| 字段 | 类型/约束 | 归属与含义 |
|---|---|---|
| `schema_version` / `record_type` | 独立版本 / `invocation` | 结构分派，不等于合同 v2 |
| request/task/run、role | 01 规定的标识；核心注入 | vendor 值不能覆盖 |
| `adapter_id` / `protocol_id` | 已登记、与描述符相符 | 不从文件名猜 adapter |
| `descriptor_ref` / `config_ref` / `environment_ref` | 固定 ResourceRef | 构造规则和实际环境均可回查 |
| `executable_binding_ref` | 05 封存的入口链绑定 | 不是未核验 `PATH` 名称 |
| `argv_tail` | 有序字符串数组；禁止 NUL | 不含 argv[0]；每项来源可回查 |
| `input` | `stdin_utf8` 或已登记 `request_file` | 恰好一种正文通道；引用完整请求字节 |
| `cwd_ref` | 核心分配的 worktree/cwd 绑定 | 不接受模型直接给绝对路径 |
| `env_names` | 去重、受控名称集合 | 不携带值；实际值由 05/凭据系统提供 |
| `resource_policy_ref` | 原生资源加载规则及输入清单 | 未声明资源不能自动发现 |
| `limits` | 超时、流量、帧、日志等上限 | 核心策略与 adapter 能力取更严格约束 |
| `capture` | stdout 协议、stderr 字节、完整采集要求 | stdout/stderr 不合并成一个协议流 |
| `session_policy` | 首版建议 `fresh_only` | 恢复历史会话需独立受审协议 |
| `execution_mode` | 首版 `exec_only` | 禁止通用 shell 字符串 |

`FinalLaunchPlan` 由 05 追加实际 argv[0]、解释器/shim 链、完整 argv、已过滤环境的安全绑定、cwd 实际位置、输入句柄和平台启动选项。其摘要及 `operation_id` 进入 handoff 记录；不得将环境绝对路径重新混入 02 配置语义投影。

**参数仍属于行为**：`command[1:]` 的固定参数沿用 02 保留在语义锁内，不能因为 `command[0]` 是机器定位而整段删除。解释器后的脚本/模块同样需要入口链锁定，不能只指纹 `python` 或 `node`。[^P02E]

#### 4.1.6 参数冲突、输入通道与注入处理

构造器按版本化参数合同为每一参数指定拥有者：runtime 固定前缀、协议模式、模型、工具、资源加载、输入正文或核心输出位置。除合同显式允许的可重复参数外，同一语义选项只能出现一次。`--model=x`、`--model x`、短别名、合并短参数必须按具体 CLI 解析规则识别，不能用字符串 contains 判断。

本版首期建议：固定参数采用小型精确白名单；模型、thinking、模式、工具、会话和资源开关均由受信 adapter 根据锁定配置产生。未知固定参数直接拒绝，不能把它们拼到结尾并寄望“后者覆盖前者”。

请求正文优先使用当前协议已验证的 stdin 字节通道；读取后按要求发送 EOF。使用 `@file` 时，文件必须由核心生成、核验并绑定；不得将用户正文中以 `@` 或 `-` 开头的字符串误当附件或选项。不要在 stdin 和 argv 同时传送完整正文导致重复输入。当前 Pi 对 stdin、`@path` 与 `--` 有不同含义，具体选择必须固定到实际版本。[^W03]

argv 数组只能消除一部分 shell 注入面，不能阻止 CLI 自身选项注入、资源加载或认证命令执行。Windows 的 `.bat/.cmd` 及多层 wrapper 必须由 05 明确处理；Python 文档指出批处理文件可能经过系统 shell，不能把 `shell=False` 宣称为覆盖所有平台的无 shell 保证。[^W10]

#### 4.1.7 环境过滤与凭据安全

04 仅输出需要的变量名称、安全绑定与禁止继承项。05 根据当前策略形成最小环境，不使用“复制整个 `os.environ` 再删几个名字”的默认方案。

`PATH`、HOME/用户目录、临时目录、代理、CLI 配置目录、包目录、语言运行时加载选项都会影响实际行为。尤其是 `NODE_OPTIONS`、`PYTHONPATH`、动态库注入变量和 shell 启动配置，未经明确合同允许不得传入。Windows 名称比较与大小写冲突按 05 平台规则处理，不能照搬 Linux 去重。

只过滤环境变量并不能禁用用户目录下的认证、settings 或模型 endpoint 覆盖。实际读取的配置/凭据来源必须由环境锁与资源策略覆盖。凭据值不进入 argv、probe 输出、日志、Schema 错误、traceback 或 Manifest；也不能把低熵秘密的普通哈希当作安全脱敏。

v2 检测 `PI_EXEC_*`、`PI_DISPATCH_PI_CMD` 冲突时拒绝或按 02 已审规则清洁后重新构造，不从其读值覆盖模型。其他 Pi 合法运行变量不能被误认为 v1 兼容变量；每个变量仍须单独登记。当前 Pi 的过程变量、session 元数据与模型选择变量有不同语义，不能全部视为授权事实。[^W07]

#### 4.1.8 构造、锁定与使用的一致性

```text
验证固定输入引用
  → 验证 adapter/模式/必要能力
  → 纯函数构造调用意图
  → 核心按锁定模型、工具、资源、环境名做独立回查
  → 封存 Invocation
  → 03/07 当前准入校验
  → 05 解析并复核同一入口链/输入对象
  → 一次性 handoff
```

核心不需要理解 Pi 参数常量，但应核对构造器提供的语义回执：所用 profile/config ref、模型、工具、输入 hash、协议和资源策略是否与锁定请求一致。该回执不能单独代替对受信 adapter 的代码审查与差分测试。

probe 缓存建议以入口链指纹、CLI build、描述符/parser、平台、资源配置和认证安全绑定、探测策略及有效期为键。不以 mtime、路径或版本字符串单独缓存。缓存过期后可保留历史观察，不能继续作为当前环境的已核验事实。

#### 4.1.9 结构化错误合同

所有新增错误沿用 01 的 `code/message/details/retryable`。details 只报告安全字段位置、阶段、来源引用、原因和修复方向；不直接返回验证库的原始 exception。

| 错误家族 | 典型条件 | 建议恢复方向 |
|---|---|---|
| `E_ADAPTER_UNKNOWN` / `E_ADAPTER_API` | registry 未登记、API 不兼容 | 安装/发布操作另行授权；当前不启动 |
| `E_PROTOCOL_UNSUPPORTED` | CLI build 与协议矩阵不匹配 | 取证、审定新协议，不猜测回退 |
| `E_PROBE_POLICY` / `E_PROBE_TIMEOUT` | 探测要求越界、超时 | 记录具体限制，重新核准探测 |
| `E_INVOCATION_*` | 参数冲突、通道冲突、未固定输入 | 修正规则并重新封存 |
| `E_CAPABILITY_REQUIRED` | 必要能力或证据不足 | 阻塞该 role，不换模型绕过 |
| `E_IDENTITY_*` | 缺失、失配、冲突、证据层级不足 | 保留所有观察，按 G1 拒绝 |
| `E_FRAME_*` / `E_JSON_*` | 超限、截断、无效 UTF-8、重复键 | 原流留证，协议不完整 |
| `E_EVENT_*` / `E_TOOL_*` | 未知关键内容、状态错误、工具缺口 | 不正常通过；取消由 05/07 执行 |
| `E_USAGE_*` | 负数、冲突、未知来源、重复观察冲突 | 标记不可结算/待对账，不补零 |
| `E_RESULT_INCOMPLETE` | 无终止、无最终报告、采集缺失 | 保留未完成结果，不自动重启 |

新增家族需要与 01 错误注册表协调；参考代码使用的具体 code 仅是离线原型的测试合同。`retryable=true` 只表达故障可能暂时，不授予重试、模型费用或新进程权限。

### 4.2 能力与角色要求

#### 4.2.1 能力是带证据的声明，不是 `supported=true`

原文列举的 `streaming_events`、`terminal_event`、`model_identity`、`tool_events`、`usage`、`read_only_mode` 全部保留；另记录平台、可用工具、输入协议和资源加载方式。能力先由角色提出要求，再与 adapter 的具体证据匹配，缺少必要能力就阻塞该角色。[^P04]

建议每项能力包含：

| 字段 | 建议约束 |
|---|---|
| `capability_id` | 注册名；模型身份等复杂能力可另有 versioned subcapability |
| `status` | `declared`、`verified`、`unsupported`、`unknown`、`blocked` |
| `source_kind` | `documentation`、`source_inspection`、`local_probe`、`captured_run`、`synthetic` |
| `evidence_refs` | 固定原始样本/探测/测试报告引用；不能仅放宣传链接 |
| `environment_ref` | 实际支持在哪个环境成立；只有资料时为空 |
| `protocol_id` | 哪一种具体输入/事件/终止规则 |
| `verified_at` / `valid_until` | UTC 或空；缓存时效不等于授权时效 |
| `limitations` | 无法核验的平台、账号、工具、模型及隔离限制 |

`verified` 的含义必须连同 provenance 阅读。**一个合成测试可以 verified 地证明“解析该合成输入正确”，不能证明当前 CLI 支持同样能力。** 真实运行样本也只证明该样本所覆盖的版本、平台和行为，不能自动证明所有模型、工具或平台。

#### 4.2.2 匹配算法

```text
for requirement in locked_role_requirements:
    capability = inventory.lookup(requirement.id, protocol, environment)
    检查能力声明存在且无 blocked/unsupported
    检查证据来源类别在该 requirement 的允许集合中
    检查证据资源存在、哈希正确、采集来源与目标环境匹配
    检查样本确实覆盖所要求的语义，不只是提到了同名字段
    检查版本、时效、限制与当前授权条件
    任一不满足 → 返回具体 gap，不自动替换 profile
```

帮助中的 `--tools` 能证明存在该参数入口，但不能单独证明完整工具审计、只读执行或所有工具名可用。一个成功调用不能证明取消路径或截断路径可靠；这些能力必须由对应负例/故障场景补足。

#### 4.2.3 角色要求建议矩阵

下表是实施建议，不能取代当前合同和现行 G1；尤其不能借“observer 标准较低”绕开执行验收。

| 要求 | planner | executor | observer / 调查角色 |
|---|---|---|---|
| 精确模型选择与必要身份证据 | 必需 | 必需，保持既有 G1 | 按合同；不足时只能报告限制 |
| 可识别终止与完整采集 | 必需 | 必需 | 要形成正式产物时必需 |
| 工具事件 | 允许用工具就必需 | 必需 | 使用工具时必须匹配用途 |
| 只读工具选择 | 默认要求 | 按合同可写 | 默认要求 |
| OS 文件/网络约束 | 按权限风险单独要求 | 按 scope/环境要求 | 不因角色名称自动豁免 |
| token 观察 | 能提供则完整归档；缺失保留 unknown | 同左，预算策略可进一步阻塞 | 同左 |
| 提供商最终费用 | 不可访问则未知 | 不可访问则未知 | 不可访问则未知 |
| 独立检查和候选门禁 | 不直接证明执行通过 | 按既有 G0–G6 完成 | 不能代替 executor 验收 |

无需使用工具的角色可以具有明确的 `tools=[]`；“不需要工具事件”必须来自锁定要求，而不是在发现审计缺口后临时改成不需要。

#### 4.2.4 模型身份：逐次观察，不只核对最后一条

建议把身份拆成四个对象：

| 对象 | 来源 | 能证明/不能证明 |
|---|---|---|
| `requested_model` | 锁定 profile | 调用意图；不是实际执行事实 |
| `cli_selected_model` | CLI 配置/预检/状态 | CLI 选择；可能受别名、路由和默认值影响 |
| `cli_completed_message_model` | 完成 assistant 消息元数据 | CLI 对此次消息的报告；不是远端权重证明 |
| `provider_reported_model` | 有来源说明的提供商响应字段，经 CLI 转交 | 提供商报告；需考虑网关、别名和 CLI 可信边界 |

模型在自然语言中说“我是某模型”，属于不受信报告文本，不能填入上述协议身份字段。当前 Pi 的 `responseModel` 是可选字段，文档说明其用于保存与请求模型不同的具体响应模型；缺省时不能自行补成请求值。[^W04]

**逐次核对规则建议**：

1. 对每条独立完成 assistant 响应建立稳定 message/call 身份，记录其 CLI/provider 观察及原始字节来源。
2. 每条属于主执行模型的观察都与锁定 provider/model 精确比较；前面出现失配，后面正常也不能抹去。
3. 允许别名必须有单独受审的版本化映射和供应方依据；默认不做模糊、大小写折叠、去后缀或“同系列即可”。
4. 不同来源矛盾时保留全部记录并给出 `CONFLICT`；不能选择最符合预期的一条。
5. 无足够证据给出 `UNKNOWN`/`INSUFFICIENT_EVIDENCE`，不进入正常执行通过；具体最低证据等级必须由原 G1 取证后确定。
6. 提供商级确认是可独立要求的能力。原 G1 若只基于可靠 CLI 完成消息核对，本次不能未经决策就宣称已升级为提供商证明，也不能把纯请求回显降格当成完成消息证据。

辅助模型调用必须另分类：自动压缩、摘要、嵌套工具、路由失败后的备用模型等。允许时须绑定各自模型/用途/预算及证据；无法观测且合同不允许不透明辅助调用时，应禁用该功能或阻塞，不因为“主模型最后匹配”就忽略它们。

#### 4.2.5 思考级别与动态变更

保存 `thinking_requested`、`thinking_cli_reported`、`thinking_provider_reported` 和各自证据来源。只有请求值时明确写 `requested_only`，不虚构已生效；不能按输出长度或“看起来想得很深”推断。

若目标 CLI 会对不支持的 thinking 值做调整，adapter 应在启动前拒绝已知不支持组合，或在已审规则明确允许的语义内展开；不能静默降低。运行中模型、thinking、工具集合发生动态变化时，必须有对应受审状态转换，否则标记配置漂移并交调度器处置，不更新旧合同快照。

#### 4.2.6 只读与工具审计的分层

| 层次 | 含义 | 不能推导 |
|---|---|---|
| 提示词要求只读 | 模型被要求不修改 | 不是技术约束 |
| CLI 工具白名单 | 不暴露某些内置工具 | 不等于限制扩展、网络或进程自身 |
| 工具调用审计 | 知道 CLI 报告调用了什么 | 不保证每个真实副作用都有事件 |
| 只读挂载/系统权限/网络策略 | OS 或外围控制实际边界 | 仍需正确配置、真实故障测试 |

planner 只读场景不默认允许 `bash`/`powershell`；“只执行查询命令”的提示不能约束任意 shell。`read/grep/find/ls` 也可能读取敏感文件，路径访问边界仍归 05/现有安全策略。

工具事件中途发现未授权工具时，04 记录 `E_TOOL_NOT_ALLOWED` 并通知核心；若 `tool_execution_start` 已经发生，不能说“已在执行前阻止”。要实现执行前防护，必须有被验证的工具拦截点或 OS 约束，且其实现、资源和权限属于受审输入。

### 4.3 原始流与统一证据

#### 4.3.1 证据对象与持久化顺序

原文要求保留经既有脱敏流程处理的原始流与标准化流，并能相互回查。以下细化遵循 01 增强版，不把脱敏后字节说成脱敏前原始字节。[^P01E]

| 对象 | 是否默认可共享 | 引用与哈希 |
|---|---|---|
| `captured_raw` | 否；只有明确允许保留时存在 | 实际采集字节，单独访问与保留期 |
| `redacted_raw` | 仍须授权，不等于无敏感性 | 脱敏后封存字节的 SHA-256 |
| `raw_manifest` | 安全元数据可按权限分享 | 分段顺序、尺寸、表示、规则版本、采集完整性 |
| `normalized_view` | 按角色与证据权限 | 独立 view ID、parser/规则引用、来源范围 |
| `result_bundle` | 业务结果 | 引用报告、视图、身份/工具/用量观察 |

```text
05 二进制采集 → 受控脱敏 → 分段封存 → 发布完整字节 ResourceRef
                         ↓
                 已封存 RawRecord / 字节范围
                         ↓
04 严格分帧与解析 → 标准事件 → 持久日志序号 → reducer / 派生结果
```

实时展示可以在封存前产生 `pending` 预览，但不能把未封存预览作为最终审核依据。若采集与脱敏管道必须先临时解析结构，应在受控内存中进行，并确保最终语义观察仍能绑定被允许保留的证据；不能为了便于解析而落盘未经授权的秘密。

引用半开区间 `[byte_start,byte_end)`，偏移相对于**对应分段的脱敏后字节**。脱敏改变长度时重新计算范围；无法保持字段语义时将该字段证据降为不足，而不是继续宣称模型/工具核验通过。

#### 4.3.2 分帧、编码与资源上限

正式流量较大时由 05 持续排空 stdout 和 stderr，边采集边封存；不能先等进程退出再读流。Python 官方文档说明管道不被及时消费可能阻塞，内存收集也不适合无限输出。[^W10]

建议生产初值：单 JSONL 记录 1 MiB、嵌套深度 64、不可变分段目标 8 MiB、单 run 证据预算 256 MiB；具体值必须进入限制规则版本并经工作负载验证。附录参考程序为小型 fixture，使用更小的 64 KiB 单记录与 4 MiB 总输入上限，**不是生产参数**。

| 情况 | 本建议处理 |
|---|---|
| UTF-8 多字节字符被拆在不同 chunk | 先累计完整 LF 记录或使用受控增量解码，不产生替换字符 |
| CRLF | 解析时去掉紧邻 LF 的一个 CR；原证据字节不改 |
| U+2028/U+2029 | 不是 LF；字符串内保留，不另起记录 |
| 一行两个 JSON 对象 | 拒绝，不自动扫描第二个对象修复 |
| 重复键，包括转义后同名键 | 拒绝，不保留最后一个值 |
| NaN/Infinity/溢出/孤立 surrogate | 拒绝，不转 null 或替换字符 |
| 顶层数组、空行、banner、ANSI 控制输出 | 按具体协议登记；严格 JSONL 默认异常并保留 |
| 完整 JSON 但 EOF 前没有 LF | 本建议严格协议标记帧截断；只有已登记旧协议例外才可接受 |
| 单记录/总量超限 | 保留可保留范围和超限事实，按 05/07 取消策略处理；不静默丢弃后继续成功 |
| stdout/stderr 混流 | 不推断为同一事件序列；stderr 不能补造标准终止事件 |

输入严格性不能只靠 `json.dumps(..., allow_nan=False)`；那只限制输出。Python 的默认解析对重复键及非有限数的行为需要另加入口检查。[^W13]

#### 4.3.3 标准事件映射与字段归属

统一封装沿用 01 增强版：`schema_version, record_type, request_id, run_id, seq, observed_at, role, type, source_kind, payload, raw_refs, integrity, adapter_revision`。seq 由核心持久日志写入方分配；不能直接采用 vendor 序号或 parser 内存计数冒充已持久提交。

下表是**建议的映射注册表**，以 Pi 文档/类型中出现的事件概念作为接入候选，并非用户版本已验证清单。实际注册需要完整 payload Schema 和 fixture。[^W02][^W11]

| 厂商记录概念 | 建议标准类型 | 核心解释 |
|---|---|---|
| session header | `vendor.session_observed` | vendor ID 作为关联，不覆盖本地 run |
| agent/turn start/end | `agent.cycle_started/ended`、`turn.started/ended` | 记录周期，不直接签收任务 |
| message start/update/end | `message.started/delta/completed` | 完成消息是最终内容来源 |
| tool execution start/update/end | `tool.started/updated/completed` | 与调用声明、结果配对 |
| session settled | `agent.settled` | 仅在该协议确有此语义时使用 |
| retry/compaction/queue change | `agent.retry/compaction/queue_observed` | 影响状态、费用或范围，不能当日志略过 |
| 未知事件 | `protocol.unknown_record` | 保留字段/引用；默认完整性不足 |
| 无法解析记录 | `protocol.invalid_record` | 保存编码后的可用字节及错误类型 |
| 05 的退出/EOF/取消 | `process.exited` / `capture.closed` 等 core 事件 | `source_kind=core`，不能伪装厂商输出 |

厂商字段的所有权为 reported；核心字段为 bound；parser 推导字段为 derived。厂商消息里的 `approved:true`、scope、role、request_id、run_id、budget 等都不能更新控制面。工具输出文本即使包含一整段 JSON，也仍是工具结果内容，不能再次作为顶层协议、审核或调用请求解析。

当前上游 JSON 转换会对流式消息使用较精简的表示；不得假定每条 delta 都携带完整累计消息。用于展示的增量缓冲，在完成消息到达时必须被权威完成内容替换，不能再拼接一份导致报告重复。[^W12]

#### 4.3.4 生命周期、终止与结果维度

建议使用彼此关联但分离的状态，而不是一个 `success` 布尔值：

```text
transport: NOT_STARTED → STARTING → RUNNING → EXITED / START_UNKNOWN
capture:   OPEN → EOF_OBSERVED → SEALED_COMPLETE / SEALED_INCOMPLETE
protocol:  AWAIT_HEADER → ACTIVE → QUIESCENT → TERMINAL_CANDIDATE → FINALIZED
quality:   COMPLETE / INCOMPLETE / UNSUPPORTED
```

`protocol` 的具体内部子状态属于版本化 parser。它至少跟踪 agent cycle、turn、打开的 message、工具调用、重试/压缩、队列，以及所需终止事件。不能把这些状态直接映射成现有 attempt/verdict 枚举而改变历史语义。

| 输入/事实 | 合法前提 | 转换与要求 |
|---|---|---|
| 第一条 header | 协议要求 header 且未出现 | 校验结构/格式版本；厂商 session 只登记关联 |
| agent start | 没有尚未闭合的 agent cycle，或协议明确允许 | 新 cycle；不能覆盖旧 cycle 异常 |
| turn start | agent 活跃，前 turn 已闭合 | 打开 turn |
| message end | 对应 message 生命周期可验证 | 保存完成消息、身份、用量；不重复计量镜像 |
| tool end | 有配对调用/开始，工具 ID 与名称一致 | 闭合执行；工具失败仍需保留 |
| agent end | 当前 cycle 关闭，所需子状态可归约 | 候选结束；按协议决定是否还需 settled/重试 |
| settled | 协议确有此事件，无待处理自动工作 | 会话稳定候选，不等于 OS 已退出 |
| stdout/stderr EOF + 退出 | 由 05 证明 | 才能形成最终采集与进程结论 |
| 终止后关键记录 | 该模式是否支持下一条已批准 prompt | 首版单请求模式标异常；多请求需显式分组 |

当前 Pi 的低层结束与会话稳定不同，已在 3.2 单独标注。旧版若只存在 `agent_end`，必须依据旧版真实 fixture、退出/EOF 和原判据定义独立协议；**既不能强制旧版提供不存在的事件，也不能对新版仅见 agent_end 就提前结束。**

建议正式结束条件：

```text
transport.closed_and_successful
AND capture.complete_and_sealed
AND terminal_rule_of_locked_protocol_satisfied
AND no_open_required_scope
AND no_unknown_critical_record_or_parser_failure
AND identity_meets_locked_G1_requirement
AND required_tool_audit_complete
AND required_report_available
AND any_workflow_required_usage_condition_satisfied
```

满足该条件只产生 `eligible_for_core_checks=true`。独立检查、范围核验、候选树、补丁往返和 G0–G6 仍由原门禁完成；adapter 结果不得把字段命名成能被误消费的最终 `PASS`。

`stopReason` 也要按协议解释。完成消息为 `error`/`aborted`、仍然 `pending`、被长度截断、延期等待结果等，不能当作最终正常报告。中间一次错误后发生已允许且有证据的重试，可由状态机证明后续完成；此前模型、工具、费用和错误记录仍然保留，不能仅取最后一条抹掉历史。当前 Pi 类型存在多种停止原因，具体支持范围必须版本化。[^W14]

#### 4.3.5 工具调用审计算法

建议维护 `ToolCallState`：`call_key`、name、声明来源、参数摘要/受控引用、start/end 引用、结果引用、`is_error`、状态。call_key 由本地 run 与厂商定义的 ID 作用域构成；ID 允许跨哪一层复用必须由协议明确。

```text
完成 assistant 消息里的 toolCall
  → DECLARED
匹配 tool_execution_start
  → RUNNING
零到多条 update
  → RUNNING（保存引用，不当成另一次调用）
匹配 tool_execution_end
  → ENDED_OK / ENDED_ERROR
所需 toolResult 完成消息
  → AUDIT_CLOSED
```

验证 name、ID、参数一致性、先后和结果关联。并行工具调用按 call_key 分别归约，不用“最近打开的工具”全局变量。某协议没有独立 toolResult 时，在该协议中登记明确豁免，不能临时因缺记录而降低要求。

同一个工具出现两次相同参数，可能是真实执行了两次，不能按内容哈希合并。重复 start/end 若没有可验证重放身份，按冲突/协议异常处理；需要合法重试时必须有不同调用身份或协议定义的 attempt 维度。

工具返回 `isError=true` 不自动等价于整个协议损坏：错误记录和后续恢复可以完整。最终是否满足任务目标取决于合同和独立检查；反过来，`isError=false` 也不证明测试通过或文件没有越权修改。

#### 4.3.6 重放、去重与派生视图

三类 ID 不混用：

| ID | 用途 |
|---|---|
| `raw_record_key` | 定位哪一段、哪一个字节范围或稳定采集记录 |
| `(run_id, seq)` | 01 持久日志序号，保护同序号内容冲突 |
| `observation_key` | 指向一次实际模型/工具用量观察，供 07 幂等记账 |

建议标准化幂等键包含 `view_id + raw_record_key + emission_index`；一个 raw 记录可派生多个事件。标准化重放不得重新插入同一 seq 的不同内容。原始内容完全相同但字节位置不同，默认是不同记录，不能仅按 SHA-256 内容去重。

**费用幂等键不能以 view_id 为根**。新 parser 重建新 view 时，同一原始调用仍然是同一次计量；07 应绑定稳定的原始调用/观察身份。新解释若修正 token 或费用，发送显式 correction/revision 并引用原观察，不能另开一笔追加收费。

检查点至少绑定：run、view、parser/脱敏版本、已封存段 Manifest、最后原始记录、已提交 seq、打开状态、输出观察键。恢复时核验全部依赖；检查点损坏可以从可信已封存原流重新推导新视图，不能拿损坏状态继续生成成功结论。

#### 4.3.7 模型用量、金额与来源

原文要求 input/output token、费用、币种和来源分开；未知不补零；按 request 汇总，换 CLI 不重置。04 **只产生观察**，07 唯一负责预算登记和结算。[^P04][^P01]

| 项目 | 规范化建议 |
|---|---|
| input/output/cache token | 非负整数或 null + 原因；明确包含关系 |
| thinking/reasoning token | 若已包含在 output 中，不重复相加 |
| 增量流式用量 | 标记累计/增量及所属 message；不默认每条都追加 |
| 完成 message 用量 | 作为该响应的最终观察；保留更正与冲突 |
| `turn_end` / `agent_end.messages` 镜像 | 回查完成 message，不创建第二笔用量 |
| CLI 自算 cost | `cli_estimate`，绑定价格表/来源；不冒充结算账单 |
| provider 用量/费用 | 标记提供商报告来源及具体调用 |
| 缺失金额/币种 | null + reason；不能默认 USD 或零 |
| 混合币种 | 分组统计；无明确换算规则时不求统一总额 |
| 部分已知 | 报告 known subtotal + coverage，未知部分不能被 total 吞掉 |

当前 Pi 消息文档说明 reasoning 可能包含于 output，工具结果也可能包含嵌套模型用量；因此不能只把主 assistant token 相加就宣称覆盖全部调用。具体实现仍需按锁定版本验证。[^W04]

建议观察键：`request_id + run_id + call_scope + raw_anchor + metric_family`。增量更新必须绑定同一 call_scope 和累计修订，使用最终快照替代临时值；累计值意外下降时保留冲突，除非协议明确标识修订/重置。成功、失败、取消和超时调用只要发生真实用量都保留，不按结果状态删除。

费用用十进制字符串进入公共记录，数值解析保留原始来源；不得使用二进制 float 做财务求和。token 的 `1.0`、布尔值、负数和不安全整数不自动当作合法 count。金额为明确数值零仍需标记其类别：零**估算**不等于零**已结算费用**；无法区分占位零的来源时保留 raw value 并标证据不足。

#### 4.3.8 隐性工作、重试与预算保留

自动重试、压缩、摘要、缓存维护或工具内模型调用都可能让“一次 CLI 进程”包含多次外部工作。当前 Pi settings 资料确有自动重试、压缩和缓存维护相关控制；本文不使用其默认值替代用户锁定配置。[^W15]

建议初期协议选择：能受控关闭且已验证无必要依赖的隐性工作先关闭；确需开启时，锁定触发规则、次数、模型范围、费用观测和终止条件。不能把某个未实际验证的 flag 写入 argv 后就宣称已关闭。

预算至少保留 `reserved`、`observed_known`、`unresolved_exposure`、`settled` 等不同概念，具体账本归 07。用量未知或真实请求状态未知时，不能立即释放全部预留再启动新 run；按已有预算策略保留风险敞口。04 不承诺凭 CLI 事件可实现网络发送瞬间的硬费用上限。

#### 4.3.9 报告提取与检查证据

建议从协议认可的最终完成 assistant 消息提取用户可见文本块，保留 block 顺序和原始引用。不能把 thinking/signature/tool arguments 当作最终报告，也不把流式 delta、agent_end 镜像、stderr banner 再附加一遍。

报告文本、结构化任务结果、工具审计和独立检查分别保存。模型报告“所有测试通过”只是一项陈述；正式通过必须引用独立运行的检查命令、退出状态、输出、候选 tree 和检查定义。报告缺失与协议缺失分别编码，不能用固定“执行完成”字符串补报告。

如果任务要求结构化结果，使用合同声明的格式和 Schema 验证；不要在长文本中搜索第一个看起来像 JSON 的片段就当正式结果。模型给出的输出文件位置仍须经过 01 路径与资源校验，不能直接打开绝对路径或符号链接目标。

#### 4.3.10 组合结果决策表

| 观察组合 | 建议结果维度 | 能否直接正常通过 |
|---|---|---|
| 退出 0 + 完整协议 + 身份/工具通过 + 有报告 | 可进入原核心检查 | 仍不能直接 PASS |
| 退出 0 + 缺必要终止 | protocol incomplete | 否 |
| 终止齐全 + 非零退出 | protocol 可完整，process failed | 否 |
| 终止齐全 + 某轮模型失配 | identity mismatch | 否 |
| 工具 start 有、end 缺 | tool audit incomplete | 否 |
| 过程错误后按受审策略恢复并完整结束 | 保留已恢复错误与全量用量 | 由原门禁继续判断 |
| 用户取消/超时后出现成功文本 | cancellation/timeout 事实保留 | 否 |
| 完整执行但费用缺失 | cost unknown；token 单独判断 | 不补零；按预算及门禁策略决定下一步 |
| 采集盘满，部分流丢失 | capture incomplete | 否 |
| `START_UNKNOWN` | 是否已产生副作用未知 | 不盲目重发 |

多个原因同时存在时保存全部结构化诊断，再按版本化规则选择主错误供旧 CLI 展示。不得因为只显示一个主错误就删除模型失配、工具越权或费用未知的其他证据。

### 4.4 Pi 迁移边界

#### 4.4.1 原文保留

同一批 Pi fixture 先做迁移前后行为对照，保留错误类别和必要原始字段。v1 合同路由 Pi adapter，v2 通过 profile 选择 adapter，核心不再访问 Pi 参数常量。Pi 兼容变量只在 v1 解释；默认禁止未声明的 Pi 原生资源发现；06 任务插件由核心显式编译，不等于 Pi 原生扩展。真实试跑单独核对现有授权、profile、预算及模型/环境精确身份。[^P04]

#### 4.4.2 三类协议配置，不猜测版本区间

| 路径 | 资料/证据来源 | 终止与模型规则 | 准入 |
|---|---|---|---|
| `v1-compat` | 用户仓库原测试、真实旧版 fixture | 保留原门禁；明确旧保证边界 | 只接合法 v1，不接受改标签的 v2 |
| `pi-verified-<revision>` | 固定 CLI build、平台、帮助、真实脱敏流、差分结果 | 按审定描述符精确执行 | 配置/环境/描述符均被审核 |
| `synthetic-*` | 本文人工 fixture | 仅测试定义的有限子集 | `execution_allowed=false`，不能真实调用 |

本文参考实现的 `synthetic-legacy-v1` 和 `synthetic-settled-v1` 只是说明两种终止算法，**不是任何具体 Pi 发布版本的别名**。缺少用户实机版本时不编造版本阈值、不注册“所有未来版本都兼容”。

首期建议只接已经取证的一次性 JSON 模式。RPC 是长生命周期双向协议，需要命令 ID、响应匹配、队列、abort、重连及会话边界等额外设计，不能因为事件名字相似就宣布同一 adapter 已支持 RPC。未来接入必须单独锁定协议和验收，不在 04 原计划中偷偷增加范围。

#### 4.4.3 原生资源与隐式配置清单

保留原 `RESOURCE_FLAGS` 的实际行为，不先假定它已覆盖所有资源。以下是**建议审查清单**，不同 Pi 版本具有不同控制方式；目标版本未支持某开关时应制定显式替代约束或阻塞，不能盲目追加开关。

| 输入面 | 推荐处置 | 需要的证据 |
|---|---|---|
| 自动发现 extensions/skills/prompts | 保留并验证既有禁用规则 | 实际帮助、资源样本、无加载测试 |
| 显式 `-e/--skill/--prompt-template` 等 | 首期拒绝未声明项 | 参数拥有者与最终 argv |
| 全局/项目 settings、models 配置 | 受控配置视图；语义/环境分别锁定 | 生效来源与最终设置，不是仅配置文件存在 |
| agent dir / 项目 SYSTEM、APPEND_SYSTEM | 明确禁用、固定来源或纳入输入闭包 | 最终 prompt 来源清单 |
| agent dir、cwd 与父目录上下文 | 不因 worktree 新建就假定为空 | 祖先目录、context 文件发现规则 |
| session 恢复、continue、fork | 首期 fresh-only；额外历史上下文另审 | 实际 session 输入与追加记录 |
| 包资源、theme、自定义工具 | 未声明不加载；不能只锁主 executable | 包/扩展依赖闭包与初始化副作用 |
| 认证文件/命令、云 ambient credentials | 已批准来源；不复制到普通日志 | 安全账户/endpoint/权限域绑定 |
| 自动 catalog、更新检查、遥测、代理 | 按网络策略登记 | 外发目标、选项支持和网络限制实测 |
| shellPath / shellCommandPrefix / wrapper | 作为行为与环境输入审查 | 脚本/配置指纹及实际工具选择 |

上游配置资料区分全局 agent directory、项目 `.pi` 和 context files；context discovery 不等同于项目 trust 控制。因此“禁止项目配置”不能被推导成“没有额外上下文”。[^W05]

**与 AGENTS 的关系**：关闭 CLI 原生 context discovery 不等于删除项目 AGENTS 的治理要求。若采用该策略，核心必须把需要遵守的规则按受审清单编译进固定请求，并保留来源；不能省略受保护规则或让模型自行挑选。是否改变原有加载行为是单独的实施变更，不包含在本文授权中。

#### 4.4.4 v1/v2 路由及环境兼容

```text
读取受信的合同版本来源
  v1 → 原有 normalize/validate/冲突规则 → Pi 兼容 adapter → 原门禁
  v2 → 02 固定 profile/config → 03 审核与当前准入 → 已登记 adapter
  其他/来源不明/字段不一致 → 拒绝
```

v1 继续验证 `PI_EXEC_*` 和 `PI_DISPATCH_PI_CMD` 的历史优先级、空值与冲突；不为了统一接口改写旧 Step/high 等既有选择。v2 不读取这些变量作为覆盖源，也不能出错后转 v1。回退只是恢复已允许旧路径，不得绕过新版审核和发布来源。

#### 4.4.5 差分迁移算法

建议对**同一份封存原流**运行旧解释器和新 adapter，避免双跑真实 CLI 造成重复费用和不同模型输出。

```text
fixed_fixture + fixed_transport + fixed_contract
      ├─ legacy summarizer → legacy_projection
      └─ new adapter      → compatibility_projection
compare:
  终止事实 / 缺失与损坏 / 实际模型与思考证据等级
  工具调用集与异常 / 最终报告来源 / token与费用未知状态
  主错误家族 / 旧退出码映射 / 候选与独立检查入口
```

时间、内部 seq 或展示格式等非关键差异可以放入明示白名单；模型失配、结束缺失、工具缺口、未知变零不得白名单忽略。发现旧实现存在真实缺陷时，应记录“行为变更提案 + 风险 + 新版本 + 审核”，不能宣称同时做到了严格等价。

#### 4.4.6 fixture 来源与支持声明

| fixture 类别 | 必须标记 | 可以证明 |
|---|---|---|
| 现有 stub | `origin=legacy_stub` | 旧测试的模拟行为 |
| 本文合成 | `origin=synthetic` | 新增算法在指定输入下的行为 |
| 文档例子转写 | `origin=documentation_example` | 对文档描述的理解；不是实测 |
| 真实脱敏采集 | `origin=captured_redacted` | 该 CLI build/环境/模式的实际表现 |
| 从旧真实样本派生的故障变体 | `origin=mutated_capture` | 已知输入的异常处理，不是厂商真的发过 |

每个 Manifest 记录采集/构造方式、版本与平台、授权范围、输入/流/退出记录引用、脱敏版本、字段破坏情况及未运行项。真实支持至少需要正常、工具使用、模型失配/身份缺失、异常终止、取消和资源加载相关证据；缺项逐条写 `NOT_RUN` 或能力受限，不统一盖 `supported=true`。

## 5. 预计文件范围

### 5.1 原文范围与建议拆分

原文计划新增 `scripts/codekit/adapters/` 的协议、注册表及 Pi 实现，从 `scripts/pi_dispatch.py` 迁移 Pi 解析和预检，补充通用 fixture/Pi 对照/异常流测试，按需更新环境核验文档和源技能。本版保留该范围，不把下列建议路径当作仓库已存在文件。[^P04]

| 建议位置 | 职责 | 不承担 |
|---|---|---|
| `scripts/codekit/adapters/protocol.py` | 类型、接口与参数归属合同 | 进程执行、预算、审核 |
| `scripts/codekit/adapters/registry.py` | 固定描述符注册、版本与来源校验 | 任意配置 import、自动下载安装 |
| `scripts/codekit/adapters/pi/profile.py` | Pi model/options/参数校验 | 其他 CLI 的默认参数 |
| `scripts/codekit/adapters/pi/probe.py` | probe 计划与结果解释 | 自行启动子进程 |
| `scripts/codekit/adapters/pi/invocation.py` | 确定性构造 Pi 调用意图 | 读取任意环境或输出目录 |
| `scripts/codekit/adapters/pi/events.py` | 版本化分派和事件语义 | 在 parser 中重试模型 |
| `scripts/codekit/adapters/pi/result.py` | 身份、工具、报告与用量观察 | 直接扣费或判 PASS |
| `schemas/adapters/` | 已注册的公共/专有 Schema | 网络自动下载 `$ref` |
| `tests/fixtures/adapters/` | 来源 Manifest、正例与异常流 | 真实凭据、隐含付费测试 |
| `tests/test_adapters_*.py` | 离线接口/分帧/语义/兼容测试 | 用模拟结果代替真实平台验收 |
| `scripts/pi_dispatch.py` | 最小接线、明确 v1/v2 路由 | 保留新的 Pi 常量渗漏回核心 |

### 5.2 受保护范围与依赖方向

受保护调度核心及源技能由主模型在后续实施授权下直接修改、复核；普通执行器合同不能通过扩大 Scope 越权。AGENTS、`.agents/skills/`、历史 `.agent/runs/` 和审核记录不在本次文档生成的修改范围内。

建议静态检查依赖方向：核心通过注册表/协议调用实现；adapter 不反向 import 合同发布、预算结算或批准入口。05 可消费 Invocation，但不能依赖 Pi 常量来决定通用超时/进程树行为。新 adapter 成为受信代码时一并纳入保护和发布审查。

### 5.3 交付清单

实施交付至少包含：字段 Schema、描述符与参数拥有者表、真实支持矩阵、probe 原始证据、固定 fixture Manifest、标准化映射和状态机、差分报告、异常/恢复测试、依赖/资源清单、操作说明及剩余缺口。仅提交几个 Protocol 类和 `PiAdapter` 空壳不算完成迁移。

## 6. 实施步骤

### 6.1 分阶段工作与退出条件

| 阶段 | 操作 | 必须形成的证据 | 退出条件 |
|---|---|---|---|
| S0 固定现状 | 核实 HEAD、工作区、旧函数/常量与 v1 测试 | 基线报告、旧事件/门禁映射 | 未运行项、已有失败和环境缺口已区分 |
| S1 通用协议 | Schema、注册表、纯构造接口、合成 adapter | 字段正反例、无进程/无网络离线测试 | 未知 adapter/版本/参数在启动前拒绝 |
| S2 Pi 取证 | 获准后记录入口链、帮助/版本、本地能力和真实流 | 授权、环境、版本、来源完整的 Manifest | 可建立精确协议矩阵，不用 main 文档猜测 |
| S3 提取实现 | 迁移 probe、参数、事件/结果解释 | 对固定原流的新旧解释结果 | 关键事实和失败语义差异均已处理 |
| S4 接线 | 核心走注册表/Invocation，05 执行 | 03 exact 锁、单次准入、固定输入与运行证据 | 绕过接线的隐含 Pi 路径被识别并关闭 |
| S5 故障与兼容 | 取消、截断、未知事件、v1 历史读取、回放 | 故障报告、无重复调用/记账证据 | 原 A01–A09 和新增采用项满足 |
| S6 交接 11 | 输出第二 CLI 接入合同模板及证据门槛 | 接口包、支持矩阵、明确未支持项 | 不宣称第二 CLI 已实现 |

S2 中真实模型调用与本机无模型 probe 分开授权。原文已有授权可复用的范围按 03 核验，不机械要求每次重复确认；没有相应授权则停在离线阶段。

### 6.2 基线记录模板

```yaml
record_type: adapter-migration-baseline
origin: implementation_record_to_be_collected
repository_commit: null
working_tree_fingerprint: null
legacy_dispatcher_ref: null
legacy_fixture_manifest_ref: null
cli_distribution: null
cli_version: null
entry_chain_ref: null
platform: null
protocol_id: null
authorization_refs: []
checks:
  original_dispatch_tests: NOT_RUN
  real_pi_probe: NOT_RUN
  real_pi_model_run: NOT_RUN
  differential_replay: NOT_RUN
  g0_g6_regression: NOT_RUN
limitations:
  - 本模板未采集项目或实机事实。
```

模板中的 null 不是“无需记录”；真实实施时未得到事实仍保持未知并阻塞相应验收，不把设计调查基线或公开版本复制过来。

### 6.3 参考命令与执行边界

以下是**不同范围的命令**，不可混用结果：

```text
# 用户项目原有入口：本次未运行，需在固定真实仓库和允许环境中执行。
python tests/test_dispatch.py

# 本文离线验证包：只对合成协议运行，不调用 Pi。
python -m unittest test_reference -v
python verify.py
```

实际解释器路径由环境决定。`verify.py` 需要已提供的 `jsonschema`；本次实测版本在附录 C 记录。脚本不安装依赖、不执行 Pi、不读取凭据、不连接提供商、不触及项目 `.agent/`。

### 6.4 迁移上线与观察

建议先在固定原流上双解释，后在少量已批准任务中选择新实现；不要在同一真实任务后台双跑两个模型。发布切换绑定 registry/descriptor/parser 版本，既有 run 固定原版本，不热切换正在运行的 parser 或参数。

观察指标建议：按协议的解析失败率、未知关键事件率、模型未知/失配率、未闭合工具调用、终止证据缺失、probe 失败、用量未知覆盖率、重放冲突、START_UNKNOWN 数量。指标标签不含 prompt、密钥、完整 argv 或高基数用户文本；报警阈值由真实基线确定，本文不编造百分位性能或错误率承诺。

## 7. 验收标准

### 7.1 原 `04-A01`–`04-A09` 保留

| 编号 | 原文验收要求 |
|---|---|
| **04-A01** | Pi 原流程的必要证据、模型核对与门禁回归保持一致。 |
| **04-A02** | 核心用 adapter 注册表选择实现，未知 adapter 明确阻塞且无模型进程启动。 |
| **04-A03** | stub 可验证通用接口，但能力报告明确标为离线模拟而非真实 CLI 支持。 |
| **04-A04** | 缺结束事件、实际模型缺失/不符、工具事件缺口均不能得到正常执行通过。 |
| **04-A05** | 畸形、截断和未知事件保留来源；标准化流不能掩盖原始流异常。 |
| **04-A06** | 用量缺失记 unknown，请求、CLI 和提供商模型字段不互相填补。 |
| **04-A07** | probe 不调用模型；缺角色所需能力时报告缺口，不通过换模型自动绕过。 |
| **04-A08** | adapter 不自行启动/终止进程；平台 runner 接收可复现 Invocation。 |
| **04-A09** | v2 不受 Pi 兼容覆盖变量改写，Pi 资源发现仍按明确策略受控。 |

这些项目来自原文。本次原型测试仅覆盖其中部分算法，不据此签收实际项目验收。[^P04]

### 7.2 新增建议验收项

| 编号 | 必须证明的结果 | 主要实施方 |
|---|---|---|
| 04-A10 | adapter API、描述符、parser 和 CLI 协议版本独立分派，未知组合拒绝 | 04/02 |
| 04-A11 | probe 计划记录入口/加载/网络限制，帮助和认证不会越权获取凭据或触发模型 | 04/05/03 |
| 04-A12 | 输入通道互斥、参数拥有者唯一、固定参数保持语义锁定 | 04/02 |
| 04-A13 | LF/CRLF、UTF-8 分片、Unicode 分隔符、重复键、尾部截断均有稳定结果 | 04/05 |
| 04-A14 | agent cycle、turn、settled 与退出/EOF 分开，多轮不会提前结束 | 04/05 |
| 04-A15 | 每次主模型响应都核对身份，前轮失配不被最后一轮覆盖 | 04/原 G1 |
| 04-A16 | provider/thinking 证据缺失不回填，动态改变不静默接受 | 04/02/03 |
| 04-A17 | 工具声明、开始、完成、结果按 ID/名称/作用域核对，支持已登记并发规则 | 04 |
| 04-A18 | 相同内容的不同事件不被误删；同一持久事件重放不重复提交 | 04/01 |
| 04-A19 | delta/turn/agent 镜像不重复计费，新 view 不能重新收取原始调用费用 | 04/07 |
| 04-A20 | 自算费用与结算费用分开，未知/零/部分已知/币种与缓存包含关系正确 | 04/07 |
| 04-A21 | 隐式 context/settings/session/辅助模型调用纳入输入及预算范围 | 04/02/06/07 |
| 04-A22 | descriptor、环境或入口链漂移使旧 exact 锁拒绝；adapter 不绕过准入 | 03/04/05 |
| 04-A23 | 解析崩溃、盘满、采集丢失、handoff 结果未知均无盲目重启 | 01/04/05/07 |
| 04-A24 | 真实样本、文档示例、stub 和变异样本来源清楚，不混作真实支持 | 04/01 |
| 04-A25 | 新旧差分只豁免明示非关键差异，行为修复单独版本化与审核 | 04/原门禁 |
| 04-A26 | 11 接入包说明完整限制、平台缺口和待取证内容，不预设第二 CLI | 04/11 |

### 7.3 必须在真实项目补做的集成/故障场景

下列 **20 项全部为 `NOT_RUN`**，不包含在附录的离线单元测试通过数内。

| 场景 | 注入/操作 | 成功判据 |
|---|---|---|
| F01 | 运行原仓库全量回归 | 新旧 G0–G6、退出码、候选/范围行为一致或有已审变更 |
| F02 | 未知 adapter/protocol 指向看似有效 executable | 05 没有启动模型进程；返回精确缺口 |
| F03 | help 通过扩展注册额外选项 | 未批准扩展未初始化，探测不把它当内置能力 |
| F04 | 认证源包含执行命令或需 OAuth 刷新 | 未授权时不执行/不刷新，状态保持不足 |
| F05 | 模型清单触发 catalog/版本检查外发 | 网络策略阻止或按已核准范围记录，不冒充离线 |
| F06 | Windows npm shim + 空格/引号/Unicode 参数 | 实际入口链和收到的 argv 与锁定意图相同 |
| F07 | 原生 bash 与 PowerShell 工具切换 | 工具身份分离，缺平台能力时阻塞 |
| F08 | 大 stdout + 大 stderr + 慢消费者 | 不死锁、不丢关键证据；超限有确定处置 |
| F09 | 模型运行期间强制取消/超时 | 05 管理进程树，完整留证，原费用归属不丢 |
| F10 | CLI JSON 退出 0 但最终消息 error | 不得到正常执行通过 |
| F11 | 多次 agent_end/自动恢复后 settled | 按实际协议归约，既不提前成功也不误杀合法恢复 |
| F12 | 前轮模型/辅助模型与锁定范围不同 | 失配保留；最后正常不能掩盖 |
| F13 | 并行工具、ID 复用、结果乱序 | 按协议作用域配对，非法组合明确拒绝 |
| F14 | 修改 global/parent context、models endpoint | 依赖或环境锁发现漂移；不是仅检查 cwd |
| F15 | 原流脱敏跨 chunk 且长度改变 | 不泄露敏感片段，证据范围仍能准确回查 |
| F16 | 封存后、标准事件提交前崩溃 | 可从固定原流恢复；不伪造缺失 seq |
| F17 | 标准事件提交后、预算确认前断线 | 重放同一观察不产生第二笔记账 |
| F18 | handoff 接纳后未收到 STARTED | 进入 START_UNKNOWN，核对真实进程身份，不盲目重发 |
| F19 | parser 升级后重建视图或回退 | 原流/原视图不变，预算通过修订而非重复累计 |
| F20 | v2 配置失败后尝试 v1 回退 | 新版对象不能通过改标签或旧环境覆盖绕过审核 |

### 7.4 签收证据格式

每项验收记录采用 `PASS/FAIL/NOT_RUN/BLOCKED/NOT_APPLICABLE`，且附输入引用、命令、环境、返回码、实际结果、异常与责任人/流程来源。`NOT_APPLICABLE` 要有已审理由；不能把它或 NOT_RUN 计入通过数。

`04-A01` 是真实项目差分与门禁结果，不由本文件末尾测试数量替代。`04-A03` 的核心恰恰是明确模拟性质，因此本版附录始终保留 synthetic 标识。

## 8. 兼容与恢复

### 8.1 原文保留

v1 attempt 按旧 Schema 读取，不要求历史事件具备新增字段。新 parser 失败时保留原始流、stderr、调用快照与未完成状态，不覆盖重跑。恢复优先依据原始记录和版本化 parser，不把新规则当作“修正旧历史”。换 adapter/profile 的真实重试形成新 run，并保持同一 request 预算。回退仍遵守旧门禁，不能绕过 v2 审核。[^P04]

### 8.2 故障窗口与补偿矩阵

| 窗口 | 可见事实 | 正确补偿 | 禁止行为 |
|---|---|---|---|
| probe 未启动 | 无进程，仅计划 | 保留计划失败，修正后重核准 | 记录 verified 能力 |
| probe 超时/输出超限 | 05 有部分证据 | 保存来源、降为未知/失败 | 截掉错误后填成功版本 |
| Invocation 未封存 | 字节不完整 | 不准入；仅清理本次 staging | 用部分输入启动 |
| 准入后未 handoff | 单次许可尚未消费 | 按 03 复核 epoch/时效/取消 | 重用旧过期许可 |
| handoff 后状态未知 | 可能已产生副作用 | START_UNKNOWN；核对进程/调用身份 | 仅因无返回就再启动 |
| 部分原流尚未封存 | 临时采集片段 | 按权限恢复/标缺失，不能编摘要 | 伪造完整 ResourceRef |
| 原流封存但 parser 崩溃 | 原字节可核验 | 固定旧 parser 重放或新建修订视图 | 替换原始流以适配 parser |
| 标准事件已提交，响应丢失 | 已有 seq/观察键 | 返回同一结果，幂等补交后续 | 分配新 seq/再扣费用 |
| 最终事件到达，进程未退出 | terminal candidate | 由 05 等待/限制退出，继续采集 | 立即宣告 run 已完整完成 |
| 进程退出，日志盘满 | 采集不完整 | 标失败/不完整，保留能核验部分 | 以退出 0 覆盖证据缺口 |
| parser 升级 | 原流与旧视图仍在 | 新 view + 新规则 ref + 差异报告 | 原地“修复”旧 verdict |
| 版本回退 | 新旧合同与审核并存 | 显式允许的旧分支，保留所有证据 | 将 v2 草稿导入 v1 逃避审核 |

### 8.3 操作人员恢复步骤

**第一步：停止受影响的新派遣。** 按现有维护权限暂停目标 adapter/protocol 的新 run，而不是删除其配置。记录 request/run、operation、controller generation、descriptor/parser/environment 引用及失败时间。已运行进程的取消归 05/07，不由手工删文件触发。

**第二步：判断“未启动”“已启动”“启动未知”。** 核对 handoff 记录、可信进程身份和启动时间、run 目录及采集句柄。PID 本身可能复用，不能单独认定原进程。没有足够证据就保留 START_UNKNOWN；不要试探性再次调用模型。

**第三步：核验证据链。** 逐段核对 Manifest、完整 SHA-256、字节尺寸、封存状态、脱敏版本、最后提交 seq 和 parser ref。存在缺口时先标记不完整，不用后续总结文本补齐原始事件。恢复原字节须有可信备份和授权，不能只按同名文件替换。

**第四步：选择恢复方式。** 不涉及真实模型副作用的 parser 重放可以生成新 view；真正模型重试必须新建 run，重新核对 profile/环境/审核/预算，仍绑定原 request。用途变化或更换 adapter 不属于“无成本的解析重试”。

**第五步：复核和恢复派遣。** 检查无悬空子状态、无事件/用量冲突、预算未知敞口未错误释放、现行门禁未被绕过。形成恢复报告及固定引用；只恢复已核验版本。无法满足时升级到模块维护者/主会话处理，保持该协议阻塞，不返回空结果伪装可用。

### 8.4 恢复成功判据与升级路径

成功必须证明：未造成重复外部调用；完整原始证据仍可核验；旧 view 不被覆盖；新 view 与来源对应；同一用量观察没有重复结算；当前授权和锁有效；真实任务的后续门禁按原流程重新判断。

无法证明任一项时，报告具体未知对象与下一项需要取得的证据。授权/范围问题交 03/主会话；进程/网络/文件隔离问题交 05；预算对账交 07；解析语义交 04；原始字节/存储完整性交 01/运行维护。不把所有故障笼统写成“重新运行即可”。

## 9. 后续合同拆分建议

### 9.1 保留原三个小包并细化

| 小包 | 必交内容 | 开始前条件 | 签收边界 |
|---|---|---|---|
| **协议与离线基准** | API/Schema/registry、参数合同、合成 fixture、严格分帧/归约参考、证据来源规则 | 固定公共 01/02 接口和允许范围 | 可独立验收通用算法；不宣布真实 Pi 支持 |
| **Pi 提取与接线** | 原函数行为提取、真实支持矩阵、probe、Invocation、模型/工具/用量解析、核心及 05 接线 | 实际基线、真实样本、授权、预算、受保护改动允许 | 必须完成旧行为差分，不靠 stub 替代 |
| **异常与兼容收口** | 故障矩阵、v1 读取/回退、资源加载、恢复操作、11 接入说明 | 上述真实接线稳定、平台证据可取 | 所有采用的验收项有可追溯结果或显式阻塞 |

### 9.2 交给 11 的第二 CLI 接入包

交接内容至少包括：adapter API 与错误家族、model/options Schema 注入约定、参数拥有者清单、Invocation/TransportSummary、平台入口链规则、证据来源和能力矩阵、原流/标准流映射、身份最低要求、工具作用域、终止策略、费用观察/更正键、资源发现清单、取消/恢复限制及差分测试方法。

接入第二 CLI 时先问“能否获得所需证据并遵守同一边界”，不是先写 `CLI_B --mode json`。未知 CLI 参数、未取得的模型身份字段、未观测的用量和取消语义全部列为待取证，不用 Pi 的字段名做默认假设。

### 9.3 正式实施前的未决事项

必须由实际材料解决的项目：旧 G1 的精确输入字段与最低证据等级；用户 Pi build 对应的终止协议；原资源禁用覆盖范围；Windows shim 与 shell 行为；认证 probe 是否可真正离线；辅助调用的模型/预算规则；05 的隔离能力与竞态上限；07 的稳定计量键与更正事务。

**以上仍不是可派遣合同。** 正式合同需绑定实际基线、具体文件 Scope、业务授权、profile、工具/网络/费用范围、审核输入、验收命令和回退条件；本文不授权外部调用、安装、提交或修改受保护代码。

---

## 附录 A：记录 Schema 与合成结构样例

### A.1 使用顺序与约束边界

本附录定义**建议 API 版本 1** 的五个记录家族：`adapter-descriptor`、`probe-plan`、`capability-report`、`invocation`、`adapter-result`。这是 04 自己的版本轴，不能用来推断合同 Schema、Pi 软件版本或用户现有实现。

先通过 01 的严格 JSON 输入校验，再识别 `record_type` / `schema_version`，使用受控注册表中的 Schema 校验，最后执行领域校验。输入中的 `$schema` / `$id` 不授权从网络下载或导入代码；示例中所有 `$ref` 都是文档内引用。JSON Schema 的 `format` 需要验证器按选定方式执行；本次验证脚本显式启用 `FormatChecker`。[^W16]

| 记录家族 | 关键绑定 | 结构校验之后仍需检查 |
|---|---|---|
| AdapterDescriptor | API、parser、参数合同、model/options Schema、协议范围 | 受信注册/发布来源、相容版本与真实样本覆盖 |
| ProbePlan | 被探测 runtime、探测规则、步骤、网络/模型约束 | 每步无模型性质、入口身份、加载副作用、当前探测授权 |
| CapabilityReport | adapter/runtime/protocol/platform、能力与来源 | 引用真实存在且与本次环境一致；synthetic 不变成真实 verified |
| Invocation | 请求/profile/配置/环境/入口/cwd、参数和输入方式 | 03 的有效准入、参数拥有者、合法路径/文件固定消费、预算 |
| AdapterResult | 独立 transport/protocol/model/tool/report/usage 维度 | 来源完整、语义一致、未知值处理；仍不替代 G0–G6 |

**Schema 不是安全实现**：资源路径的字面模式不验证符号链接/junction、文件描述符固定读取、资源种类注册或实际摘要；日期格式不证明时钟可信；`origin`、`verified` 和 `reviewed` 一类字符串不证明信任；`model_calls_permitted=false` 也不会在操作系统层阻止网络调用。上述检查必须由 01/03/04/05 的真实实现承担。

示例记录全部使用 `origin=synthetic` 或来源标记 synthetic；其环境、配置、报告等 fixture 是最小人工数据，**仅为结构正例**，不是经过全链路领域校验的运行请求。样例不能直接派遣：没有有效审核/准入，不含真实 runtime，也不提供实际模型权限。参考程序自己构造的 `invocation-fixture` / `adapter-result-fixture` 则是更小的算法教学类型，不冒充以下完整生产建议 Schema。

### A.2 完整结构定义

下面具名代码块可原样提取为 `records.schema.json`。默认关闭未知字段；数组和文本上限是建议约束，变更时要版本化。
<!-- file: records.schema.json -->
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:codekit:proposal:04:records:1",
  "title": "04 proposed records; structural validation only",
  "oneOf": [
    {
      "$ref": "#/$defs/AdapterDescriptor"
    },
    {
      "$ref": "#/$defs/ProbePlan"
    },
    {
      "$ref": "#/$defs/CapabilityReport"
    },
    {
      "$ref": "#/$defs/Invocation"
    },
    {
      "$ref": "#/$defs/AdapterResult"
    }
  ],
  "$defs": {
    "ResourceRef": {
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
          "minLength": 1,
          "maxLength": 4096,
          "pattern": "^(?!/)(?!.*\\\\)(?!.*:)(?!.*\\u0000).+$"
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
    "Limits": {
      "type": "object",
      "properties": {
        "timeout_ms": {
          "type": "integer",
          "minimum": 1,
          "maximum": 86400000
        },
        "max_record_bytes": {
          "type": "integer",
          "minimum": 2,
          "maximum": 16777216
        },
        "max_capture_bytes": {
          "type": "integer",
          "minimum": 2,
          "maximum": 1073741824
        }
      },
      "required": [
        "timeout_ms",
        "max_record_bytes",
        "max_capture_bytes"
      ],
      "additionalProperties": false
    },
    "Protocol": {
      "type": "object",
      "properties": {
        "protocol_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "mode": {
          "enum": [
            "jsonl_one_shot",
            "rpc"
          ]
        },
        "session_header_version": {
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
        "terminal_rule": {
          "enum": [
            "agent_end_and_eof",
            "settled_and_eof",
            "rpc_scoped"
          ]
        },
        "allow_final_unterminated": {
          "type": "boolean"
        },
        "source_refs": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/ResourceRef"
          },
          "minItems": 0,
          "maxItems": 256
        }
      },
      "required": [
        "protocol_id",
        "mode",
        "session_header_version",
        "terminal_rule",
        "allow_final_unterminated",
        "source_refs"
      ],
      "additionalProperties": false
    },
    "Capability": {
      "type": "object",
      "properties": {
        "capability_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "status": {
          "enum": [
            "declared",
            "verified",
            "unsupported",
            "unknown",
            "blocked"
          ]
        },
        "source_kind": {
          "enum": [
            "documentation",
            "source_inspection",
            "local_probe",
            "captured_run",
            "synthetic"
          ]
        },
        "evidence_refs": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/ResourceRef"
          },
          "minItems": 0,
          "maxItems": 256
        },
        "limitations": {
          "type": "array",
          "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "minItems": 0,
          "maxItems": 256
        }
      },
      "required": [
        "capability_id",
        "status",
        "source_kind",
        "evidence_refs",
        "limitations"
      ],
      "additionalProperties": false,
      "allOf": [
        {
          "if": {
            "properties": {
              "status": {
                "const": "verified"
              }
            },
            "required": [
              "status"
            ]
          },
          "then": {
            "properties": {
              "evidence_refs": {
                "minItems": 1
              }
            }
          }
        }
      ]
    },
    "Transport": {
      "type": "object",
      "properties": {
        "started": {
          "type": "boolean"
        },
        "exit_code": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ]
        },
        "stdout_eof": {
          "type": "boolean"
        },
        "stderr_eof": {
          "type": "boolean"
        },
        "capture_complete": {
          "type": "boolean"
        },
        "cancelled": {
          "type": "boolean"
        },
        "timed_out": {
          "type": "boolean"
        }
      },
      "required": [
        "started",
        "exit_code",
        "stdout_eof",
        "stderr_eof",
        "capture_complete",
        "cancelled",
        "timed_out"
      ],
      "additionalProperties": false
    },
    "AdapterDescriptor": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "adapter-descriptor"
        },
        "origin": {
          "enum": [
            "synthetic",
            "captured",
            "derived_from_capture"
          ]
        },
        "adapter_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "adapter_api_version": {
          "const": 1
        },
        "descriptor_revision": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "model_schema_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "options_schema_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "argument_contract_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "parser_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "protocols": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/Protocol"
          },
          "minItems": 1,
          "maxItems": 256
        },
        "platforms": {
          "type": "array",
          "items": {
            "enum": [
              "linux",
              "macos",
              "windows"
            ]
          },
          "minItems": 1,
          "maxItems": 256,
          "uniqueItems": true
        },
        "support_status": {
          "enum": [
            "synthetic_only",
            "documented_only",
            "verified_scoped"
          ]
        },
        "evidence_refs": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/ResourceRef"
          },
          "minItems": 0,
          "maxItems": 256
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "origin",
        "adapter_id",
        "adapter_api_version",
        "descriptor_revision",
        "model_schema_ref",
        "options_schema_ref",
        "argument_contract_ref",
        "parser_ref",
        "protocols",
        "platforms",
        "support_status",
        "evidence_refs"
      ],
      "additionalProperties": false
    },
    "ProbePlan": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "probe-plan"
        },
        "origin": {
          "enum": [
            "synthetic",
            "captured",
            "derived_from_capture"
          ]
        },
        "probe_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{0,95}$"
        },
        "adapter_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "probe_type": {
          "enum": [
            "version",
            "help",
            "model_catalog",
            "auth_local"
          ]
        },
        "executable_binding_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "cwd_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "argv_tail": {
          "type": "array",
          "items": {
            "type": "string",
            "maxLength": 4096,
            "pattern": "^[^\\u0000]*$"
          },
          "minItems": 0,
          "maxItems": 256
        },
        "env_names": {
          "type": "array",
          "items": {
            "type": "string",
            "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"
          },
          "minItems": 0,
          "maxItems": 256,
          "uniqueItems": true
        },
        "network_policy": {
          "enum": [
            "deny",
            "approved_allowlist"
          ]
        },
        "model_calls_permitted": {
          "const": false
        },
        "automatic_install_permitted": {
          "const": false
        },
        "policy_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "limits": {
          "$ref": "#/$defs/Limits"
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "origin",
        "probe_id",
        "adapter_id",
        "probe_type",
        "executable_binding_ref",
        "cwd_ref",
        "argv_tail",
        "env_names",
        "network_policy",
        "model_calls_permitted",
        "automatic_install_permitted",
        "policy_ref",
        "limits"
      ],
      "additionalProperties": false
    },
    "CapabilityReport": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "capability-report"
        },
        "origin": {
          "enum": [
            "synthetic",
            "captured",
            "derived_from_capture"
          ]
        },
        "adapter_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "protocol_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "descriptor_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "environment_ref": {
          "anyOf": [
            {
              "$ref": "#/$defs/ResourceRef"
            },
            {
              "type": "null"
            }
          ]
        },
        "observed_at": {
          "type": "string",
          "format": "date-time"
        },
        "capabilities": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/Capability"
          },
          "minItems": 1,
          "maxItems": 256
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "origin",
        "adapter_id",
        "protocol_id",
        "descriptor_ref",
        "environment_ref",
        "observed_at",
        "capabilities"
      ],
      "additionalProperties": false
    },
    "Invocation": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "invocation"
        },
        "origin": {
          "enum": [
            "synthetic",
            "captured",
            "derived_from_capture"
          ]
        },
        "request_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{0,95}$"
        },
        "task_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{0,95}$"
        },
        "run_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{0,95}$"
        },
        "role": {
          "enum": [
            "planner",
            "executor",
            "observer"
          ]
        },
        "adapter_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "protocol_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "descriptor_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "config_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "environment_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "executable_binding_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "argv_tail": {
          "type": "array",
          "items": {
            "type": "string",
            "maxLength": 65536,
            "pattern": "^[^\\u0000]*$"
          },
          "minItems": 0,
          "maxItems": 256
        },
        "input": {
          "type": "object",
          "properties": {
            "mode": {
              "enum": [
                "stdin_utf8",
                "request_file"
              ]
            },
            "body_ref": {
              "$ref": "#/$defs/ResourceRef"
            }
          },
          "required": [
            "mode",
            "body_ref"
          ],
          "additionalProperties": false
        },
        "cwd_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "env_names": {
          "type": "array",
          "items": {
            "type": "string",
            "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"
          },
          "minItems": 0,
          "maxItems": 256,
          "uniqueItems": true
        },
        "resource_policy_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "limits": {
          "$ref": "#/$defs/Limits"
        },
        "capture": {
          "type": "object",
          "properties": {
            "stdout_format": {
              "enum": [
                "jsonl",
                "rpc_jsonl"
              ]
            },
            "stderr_format": {
              "const": "bytes"
            },
            "lossless_required": {
              "const": true
            }
          },
          "required": [
            "stdout_format",
            "stderr_format",
            "lossless_required"
          ],
          "additionalProperties": false
        },
        "session_policy": {
          "enum": [
            "fresh_only",
            "resume_locked"
          ]
        },
        "execution_mode": {
          "const": "exec_only"
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "origin",
        "request_id",
        "task_id",
        "run_id",
        "role",
        "adapter_id",
        "protocol_id",
        "descriptor_ref",
        "config_ref",
        "environment_ref",
        "executable_binding_ref",
        "argv_tail",
        "input",
        "cwd_ref",
        "env_names",
        "resource_policy_ref",
        "limits",
        "capture",
        "session_policy",
        "execution_mode"
      ],
      "additionalProperties": false
    },
    "AdapterResult": {
      "type": "object",
      "properties": {
        "schema_version": {
          "const": 1
        },
        "record_type": {
          "const": "adapter-result"
        },
        "origin": {
          "enum": [
            "synthetic",
            "captured",
            "derived_from_capture"
          ]
        },
        "request_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{0,95}$"
        },
        "run_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{0,95}$"
        },
        "adapter_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "protocol_id": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$"
        },
        "invocation_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "parser_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "raw_manifest_ref": {
          "$ref": "#/$defs/ResourceRef"
        },
        "transport": {
          "$ref": "#/$defs/Transport"
        },
        "protocol_status": {
          "enum": [
            "complete",
            "incomplete",
            "unsupported",
            "not_started"
          ]
        },
        "terminal_observed": {
          "type": "boolean"
        },
        "identity_status": {
          "enum": [
            "matched",
            "mismatch",
            "unknown",
            "conflict"
          ]
        },
        "identity_refs": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/ResourceRef"
          },
          "minItems": 0,
          "maxItems": 256
        },
        "tool_audit_status": {
          "enum": [
            "complete",
            "incomplete",
            "not_required"
          ]
        },
        "report_ref": {
          "anyOf": [
            {
              "$ref": "#/$defs/ResourceRef"
            },
            {
              "type": "null"
            }
          ]
        },
        "usage_observation_refs": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/ResourceRef"
          },
          "minItems": 0,
          "maxItems": 256
        },
        "token_usage_complete": {
          "type": "boolean"
        },
        "cost_evidence_complete": {
          "type": "boolean"
        },
        "eligible_for_core_checks": {
          "type": "boolean"
        },
        "issue_codes": {
          "type": "array",
          "items": {
            "type": "string",
            "pattern": "^E_[A-Z0-9_]+$"
          },
          "minItems": 0,
          "maxItems": 256,
          "uniqueItems": true
        }
      },
      "required": [
        "schema_version",
        "record_type",
        "origin",
        "request_id",
        "run_id",
        "adapter_id",
        "protocol_id",
        "invocation_ref",
        "parser_ref",
        "raw_manifest_ref",
        "transport",
        "protocol_status",
        "terminal_observed",
        "identity_status",
        "identity_refs",
        "tool_audit_status",
        "report_ref",
        "usage_observation_refs",
        "token_usage_complete",
        "cost_evidence_complete",
        "eligible_for_core_checks",
        "issue_codes"
      ],
      "additionalProperties": false
    }
  }
}
```

### A.3 五类合成结构正例

五个对象是独立记录样例，不是一个可派遣工作流；其中 `verified` 仅指合成条件下的观察，不是实际 Pi 支持。所有引用字节都随附录 B 提供，便于复算。

<!-- file: record-examples.json -->
```json
[
  {
    "adapter_api_version": 1,
    "adapter_id": "synthetic-only",
    "argument_contract_ref": {
      "id": "synthetic-argument-contract",
      "kind": "fixture",
      "path": "fixtures/argument-contract.json",
      "sha256": "f95ba5770df8d87a31ea7e4ec4d68b469821df0c1e75b6cdf5bc243a864d1228"
    },
    "descriptor_revision": "synthetic-reference-1",
    "evidence_refs": [
      {
        "id": "synthetic-synthetic-stream",
        "kind": "fixture",
        "path": "fixtures/synthetic-stream.jsonl",
        "sha256": "825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"
      }
    ],
    "model_schema_ref": {
      "id": "synthetic-model-schema",
      "kind": "fixture",
      "path": "fixtures/model.schema.json",
      "sha256": "fe1f59dd018fdfeb93eb4f75a67de1dc2dc4c3256d78f1000839109d7399824f"
    },
    "options_schema_ref": {
      "id": "synthetic-options-schema",
      "kind": "fixture",
      "path": "fixtures/options.schema.json",
      "sha256": "d0157ce84790721854763c7480015e84f7cb0c7fde53afb21c3a42908805bf35"
    },
    "origin": "synthetic",
    "parser_ref": {
      "id": "synthetic-parser",
      "kind": "parser",
      "path": "adapter_reference.py",
      "sha256": "579f10c43030a0b152205170d7730008746247d4737bc56a4e0099d46aa96ecd"
    },
    "platforms": [
      "linux"
    ],
    "protocols": [
      {
        "allow_final_unterminated": false,
        "mode": "jsonl_one_shot",
        "protocol_id": "synthetic-settled-v1",
        "session_header_version": 3,
        "source_refs": [
          {
            "id": "synthetic-synthetic-stream",
            "kind": "fixture",
            "path": "fixtures/synthetic-stream.jsonl",
            "sha256": "825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"
          }
        ],
        "terminal_rule": "settled_and_eof"
      }
    ],
    "record_type": "adapter-descriptor",
    "schema_version": 1,
    "support_status": "synthetic_only"
  },
  {
    "adapter_id": "synthetic-only",
    "argv_tail": [
      "--help"
    ],
    "automatic_install_permitted": false,
    "cwd_ref": {
      "id": "synthetic-cwd",
      "kind": "fixture",
      "path": "fixtures/cwd.json",
      "sha256": "bf3883174222e387cd1eee70bfdc1ba6bf58c063f918cae807341b3f2e9f73e8"
    },
    "env_names": [],
    "executable_binding_ref": {
      "id": "synthetic-environment",
      "kind": "fixture",
      "path": "fixtures/environment.json",
      "sha256": "fbb64b67472bd695f2a798ae88d71afc46ac1193ab91df68813ea2e682c8076a"
    },
    "limits": {
      "max_capture_bytes": 4194304,
      "max_record_bytes": 65536,
      "timeout_ms": 10000
    },
    "model_calls_permitted": false,
    "network_policy": "deny",
    "origin": "synthetic",
    "policy_ref": {
      "id": "synthetic-probe-policy",
      "kind": "fixture",
      "path": "fixtures/probe-policy.json",
      "sha256": "20ab30ea0bacddde9c69732c48aaea27d69884fb69472bfbbf57b0173e9fbe48"
    },
    "probe_id": "probe-synthetic-01",
    "probe_type": "help",
    "record_type": "probe-plan",
    "schema_version": 1
  },
  {
    "adapter_id": "synthetic-only",
    "capabilities": [
      {
        "capability_id": "terminal_event",
        "evidence_refs": [
          {
            "id": "synthetic-synthetic-stream",
            "kind": "fixture",
            "path": "fixtures/synthetic-stream.jsonl",
            "sha256": "825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"
          }
        ],
        "limitations": [
          "No real CLI or process verified."
        ],
        "source_kind": "synthetic",
        "status": "verified"
      }
    ],
    "descriptor_ref": {
      "id": "synthetic-descriptor",
      "kind": "adapter-descriptor",
      "path": "fixtures/descriptor.json",
      "sha256": "c98e6acf0718647e2432c380ed63b94a247e339b3cce0a058706ebf4fa2129a0"
    },
    "environment_ref": null,
    "observed_at": "2026-09-25T00:00:00Z",
    "origin": "synthetic",
    "protocol_id": "synthetic-settled-v1",
    "record_type": "capability-report",
    "schema_version": 1
  },
  {
    "adapter_id": "synthetic-only",
    "argv_tail": [
      "--mode",
      "json",
      "--provider",
      "fixture-provider",
      "--model",
      "fixture-model"
    ],
    "capture": {
      "lossless_required": true,
      "stderr_format": "bytes",
      "stdout_format": "jsonl"
    },
    "config_ref": {
      "id": "synthetic-config",
      "kind": "fixture",
      "path": "fixtures/config.json",
      "sha256": "c12ebc318a892fd9c7c80513fbf65226eb107c54f4baac68037d593270fcec88"
    },
    "cwd_ref": {
      "id": "synthetic-cwd",
      "kind": "fixture",
      "path": "fixtures/cwd.json",
      "sha256": "bf3883174222e387cd1eee70bfdc1ba6bf58c063f918cae807341b3f2e9f73e8"
    },
    "descriptor_ref": {
      "id": "synthetic-descriptor",
      "kind": "adapter-descriptor",
      "path": "fixtures/descriptor.json",
      "sha256": "c98e6acf0718647e2432c380ed63b94a247e339b3cce0a058706ebf4fa2129a0"
    },
    "env_names": [],
    "environment_ref": {
      "id": "synthetic-environment",
      "kind": "fixture",
      "path": "fixtures/environment.json",
      "sha256": "fbb64b67472bd695f2a798ae88d71afc46ac1193ab91df68813ea2e682c8076a"
    },
    "executable_binding_ref": {
      "id": "synthetic-environment",
      "kind": "fixture",
      "path": "fixtures/environment.json",
      "sha256": "fbb64b67472bd695f2a798ae88d71afc46ac1193ab91df68813ea2e682c8076a"
    },
    "execution_mode": "exec_only",
    "input": {
      "body_ref": {
        "id": "synthetic-request",
        "kind": "fixture",
        "path": "fixtures/request.txt",
        "sha256": "d94eb23c85f478d794ddca34db7cb1b5022737f1cbfd0ce2a7dc18e51f13ccc7"
      },
      "mode": "stdin_utf8"
    },
    "limits": {
      "max_capture_bytes": 4194304,
      "max_record_bytes": 65536,
      "timeout_ms": 10000
    },
    "origin": "synthetic",
    "protocol_id": "synthetic-settled-v1",
    "record_type": "invocation",
    "request_id": "req-synthetic-01",
    "resource_policy_ref": {
      "id": "synthetic-resource-policy",
      "kind": "fixture",
      "path": "fixtures/resource-policy.json",
      "sha256": "65e017577f8cba1b94374bcc24066336ce843ebd802cdd73240cfa73d5b8193e"
    },
    "role": "executor",
    "run_id": "run-synthetic-01",
    "schema_version": 1,
    "session_policy": "fresh_only",
    "task_id": "task-synthetic-01"
  },
  {
    "adapter_id": "synthetic-only",
    "cost_evidence_complete": false,
    "eligible_for_core_checks": false,
    "identity_refs": [],
    "identity_status": "unknown",
    "invocation_ref": {
      "id": "synthetic-invocation",
      "kind": "invocation",
      "path": "fixtures/invocation.json",
      "sha256": "1bc668b520ca645c50b66c08d281952e3e4f8d9226286fdcd1ad8b1fda48af3c"
    },
    "issue_codes": [
      "E_SYNTHETIC_NOT_RUNTIME"
    ],
    "origin": "synthetic",
    "parser_ref": {
      "id": "synthetic-parser",
      "kind": "parser",
      "path": "adapter_reference.py",
      "sha256": "579f10c43030a0b152205170d7730008746247d4737bc56a4e0099d46aa96ecd"
    },
    "protocol_id": "synthetic-settled-v1",
    "protocol_status": "not_started",
    "raw_manifest_ref": {
      "id": "synthetic-manifest",
      "kind": "raw-manifest",
      "path": "fixtures/raw-manifest.json",
      "sha256": "7658f4061d84ea26b11bae9c049065ade7665f7df682569a45080ea28394f78a"
    },
    "record_type": "adapter-result",
    "report_ref": null,
    "request_id": "req-synthetic-01",
    "run_id": "run-synthetic-01",
    "schema_version": 1,
    "terminal_observed": false,
    "token_usage_complete": false,
    "tool_audit_status": "incomplete",
    "transport": {
      "cancelled": false,
      "capture_complete": false,
      "exit_code": null,
      "started": false,
      "stderr_eof": false,
      "stdout_eof": false,
      "timed_out": false
    },
    "usage_observation_refs": []
  }
]
```


## 附录 B：可提取的离线参考实现、夹具与复算程序

### B.1 能证明什么，不能证明什么

参考实现用于检验**本版所建议的部分通用算法**：严格分帧、异常来源保留、单次/多次 agent cycle、工具生命周期、身份不回填、完成消息用量去重、未知费用、合成能力匹配，以及不执行进程的调用描述。它没有导入 Pi，没有从网络抓取模型，也没有接入真实项目；不存在“本程序通过，所以 Pi 已迁移成功”的推论。

| 层面 | 已实现的参考范围 | 未实现或只由夹具模拟的内容 |
|---|---|---|
| 数据输入 | 有上限的 LF/CRLF JSONL、严格 UTF-8、重复键/非有限数/深度检查 | 生产连续排空、背压、分段落盘、跨流有序采集 |
| 协议 | 两个 synthetic 方言、顺序 message/tool 子集、未知字段/事件拒绝 | 实际 Pi releases、RPC、多 prompt、完整重试/压缩、并发工具全部规则 |
| 工具 | 声明/开始/结束/结果顺序、名称与 ID、错误标记关联 | 工具参数/结果的完整语义对照、读写路径安全、OS 执行拦截 |
| 模型 | 每个完成 assistant 的请求/CLI/provider 观察与合成精确比较 | 真实 G1 证据最低等级、供应商认证、真实别名映射 |
| 用量 | 完成消息观察、镜像不重复累计、十进制字符串估算、结算费用未知 | Pi 原始数值 cost 格式、嵌套用量、真实价格表/账单、07 事务 |
| 证据 | 合成原始字节哈希、半开区间引用、确定性重放 | 生产脱敏、真实 Manifest 发布、持久 seq/CAS/checkpoint |
| 调用 | `execution_allowed=false` 的教学描述；拒绝未知固定参数 | 实际 argv 入口链、进程环境、准入、平台 shell 与取消 |
| Schema | 五家族结构闭合、正反例、内部引用 | 业务授权、环境可信度、跨记录引用种类与锁一致性 |

两个方言名 `synthetic-settled-v1` 与 `synthetic-legacy-v1` 是**本文自定义教学标签**。其中 session header 数字也是合成字段，不代表已经验证任意 Pi 软件版本。固定 `observed_at` 是夹具时间，不是对真实运行时间的测量。

`Context`、`Transport` 和能力清单按受信调用者提供；字段标签不能自证真实性。`capability_gaps()` 的内部最小记录使用单个 `evidence_ref`，完整建议 Schema 使用 `evidence_refs[]`；这是教学函数与完整 API 的显式区别，正式接线要适配，不可直接当作同一类型。`resource()` 只做字面路径检查并计算传入 bytes 的摘要，不打开文件，也不实现真实路径边界。

`audit_synthetic()` 输出的事件 `payload` 只保存最小诊断和来源标记；第 4.3.3 节的生产事件 payload 注册表仍须实施。内存分配的 seq 不是已持久提交序号。`eligible_for_core_checks` 仅表示合成样本满足参考规则；`gate_verdict` 永远不在这里获得有约束力的 PASS。

### B.2 提取和运行

具名文件使用如下标记组织：`<!-- file: 相对路径 -->`，下一行即代码围栏。提取器只复制这些块，不读取文档中的 shell 示例，不自动执行代码，不安装依赖，不覆盖已有目录。

直接使用配套验证包时，在专用目录执行：

```sh
python -m unittest test_reference -v
python verify.py
```

从本 Markdown 提取时，先将下方 `extract_reference.py` 代码块保存为同名脚本，再执行：

```sh
python extract_reference.py 04-cli-adapters-and-pi-审阅增强版.md NEW_REFERENCE_DIRECTORY
```

输出目录必须不存在，其父目录必须存在。随后进入该目录执行前述测试。解释器名称按环境选择，**不要使用 `python -O`**；验证器会拒绝禁用断言的执行方式。单元测试只用标准库，Schema 验证使用环境中已安装的 `jsonschema`，本次版本记于 `requirements-schema.txt`，没有自动安装步骤。

`verify.py` 在所在参考目录写出 `verification-report.json`。它不会清理临时目录、修改用户项目或覆盖 `.agent/` 证据。缺依赖或验证失败会直接失败，不自动跳过 Schema 检查后报告全通过。

### B.3 参考文件清单与用途

| 文件 | 用途 |
|---|---|
| `adapter_reference.py` | 纯 Python 合成协议与最小归约器 |
| `fixtures.py` | 人工构造事件及上下文，不从厂商捕获 |
| `test_reference.py` | 95 个独立 unittest 方法；部分含多组变体 |
| `verify.py` | 单元测试、Schema、字节向量、引用 SHA-256 与有限 AST 检查 |
| `extract_reference.py` | 从具名 Markdown 块提取到新目录，不执行代码 |
| `records.schema.json`、`record-examples.json` | 附录 A 的结构定义与五类合成正例 |
| `golden-vectors.json` | 10 组固定序列化输入、UTF-8 十六进制字节、SHA-256 |
| `resource-refs.json` | 15 个去重资源引用及其完整字节摘要 |
| `reference-invocation.json` | 不含可执行入口的调用教学样例 |
| `fixtures/` | 最小原流、资源、配置/环境占位、描述符与结果来源 |
| `README.txt`、`requirements-schema.txt` | 使用与限制、实测依赖版本 |

全套共 **26 个具名文件**。JSON 资源的单行形式是刻意保留的固定字节表示；核验前不能重新排版它们。`records.schema.json`、样例集合、向量和索引为可读格式，不改变其中所引用资源的字节。

### B.4 实现与测试源码
#### B.4.1 `adapter_reference.py`

<!-- file: adapter_reference.py -->
```python
"""Offline synthetic reference. Does not implement/certify a real Pi adapter.

No process, network, credentials, real budget ledger, or filesystem sandbox.
The caller provides already-redacted, bounded bytes and trusted context.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

MAX_SAFE_INTEGER = 9007199254740991


class Rejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # Never echo the rejected input.


def stable_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def resource(data: bytes, kind: str, ident: str, path: str) -> dict:
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(x in ("", ".", "..") for x in path.split("/")) or "\0" in path):
        raise Rejected("E_RESOURCE_PATH")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", kind):
        raise Rejected("E_RESOURCE_KIND")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", ident):
        raise Rejected("E_RESOURCE_ID")
    return {"kind": kind, "id": ident, "sha256": hashlib.sha256(data).hexdigest(), "path": path}


def strict_json(data: bytes, max_depth: int = 32) -> dict:
    if data.startswith(b"\xef\xbb\xbf"):
        raise Rejected("E_JSON_BOM")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise Rejected("E_JSON_ENCODING") from None
    # Bound nesting before recursively decoding. Quotes/escapes do not change depth.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise Rejected("E_JSON_DEPTH")
        elif char in "]}":
            depth -= 1

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise Rejected("E_JSON_DUPLICATE_KEY")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise Rejected("E_JSON_NONFINITE")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, Rejected):
            raise
        raise Rejected("E_JSON_SYNTAX") from None

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in node):
                raise Rejected("E_JSON_UNICODE")
        elif type(node) is int:
            if abs(node) > MAX_SAFE_INTEGER:
                raise Rejected("E_JSON_INTEGER")
        elif type(node) is float:
            if not math.isfinite(node):
                raise Rejected("E_JSON_NONFINITE")
        elif isinstance(node, dict):
            for key, child in node.items():
                walk(key)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    if not isinstance(value, dict):
        raise Rejected("E_JSON_OBJECT")
    return value


@dataclass(frozen=True)
class Frame:
    byte_start: int
    byte_end: int
    value: dict | None
    error: str | None


def frame_jsonl(chunks: Iterable[bytes], *, max_record: int = 65536,
                max_total: int = 4 * 1024 * 1024) -> tuple[bytes, list[Frame]]:
    """Small-fixture collector. Total input is bounded, not a production spooler."""
    if max_record < 2 or max_total < max_record:
        raise Rejected("E_LIMIT_CONFIG")
    all_bytes = bytearray()
    pending = bytearray()
    start = 0
    frames: list[Frame] = []

    def add(raw: bytes, terminated: bool) -> None:
        nonlocal start
        end = start + len(raw)
        val = None
        error = None
        if len(raw) > max_record:
            error = "E_FRAME_LIMIT"
        elif not terminated:
            error = "E_FRAME_TRUNCATED"
        else:
            body = raw[:-1]
            if body.endswith(b"\r"):
                body = body[:-1]
            try:
                val = strict_json(body)
            except Rejected as exc:
                error = exc.code
        frames.append(Frame(start, end, val, error))
        start = end

    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise Rejected("E_INPUT_BYTES")
        if len(all_bytes) + len(chunk) > max_total:
            raise Rejected("E_CAPTURE_LIMIT")
        all_bytes.extend(chunk)
        pending.extend(chunk)
        while True:
            split = pending.find(b"\n")
            if split < 0:
                break
            add(bytes(pending[:split + 1]), True)
            del pending[:split + 1]
    if pending:
        add(bytes(pending), False)
    return bytes(all_bytes), frames


@dataclass(frozen=True)
class Context:
    request_id: str
    run_id: str
    role: str
    provider: str
    model: str
    dialect: str = "synthetic-settled-v1"
    allowed_tools: tuple[str, ...] = ("read",)
    require_provider: bool = False
    require_token_usage: bool = False

    def validate(self) -> None:
        for name in (self.request_id, self.run_id):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", name):
                raise Rejected("E_CONTEXT_ID")
        if self.role not in ("planner", "executor", "observer"):
            raise Rejected("E_CONTEXT_ROLE")
        if self.dialect not in ("synthetic-settled-v1", "synthetic-legacy-v1"):
            raise Rejected("E_PROTOCOL_VERSION")
        if (not self.provider or not self.model or self.model.startswith("-")
                or any(c in self.provider + self.model for c in "\r\n\0")):
            raise Rejected("E_MODEL_LOCK")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise Rejected("E_TOOL_DUPLICATE")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", t) for t in self.allowed_tools):
            raise Rejected("E_TOOL_NAME")


@dataclass(frozen=True)
class Transport:
    started: bool = True
    exit_code: int | None = 0
    stdout_eof: bool = True
    stderr_eof: bool = True
    capture_complete: bool = True
    cancelled: bool = False
    timed_out: bool = False


def build_synthetic_invocation(context: Context, prompt: bytes, *,
                               fixed_args: tuple[str, ...] = (),
                               env_names: tuple[str, ...] = ()) -> dict:
    """Construct a NON-EXECUTABLE fixture. This is not a real Pi argv builder."""
    context.validate()
    if fixed_args not in ((), ("--quiet",)):
        raise Rejected("E_INVOCATION_FIXED_ARGS")
    if len(set(env_names)) != len(env_names):
        raise Rejected("E_ENV_DUPLICATE")
    for name in env_names:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
            raise Rejected("E_ENV_NAME")
        if name.startswith("PI_EXEC_") or name == "PI_DISPATCH_PI_CMD":
            raise Rejected("E_CFG_LEGACY_ENV")
        if name not in ("SYNTHETIC_API_KEY",):
            raise Rejected("E_ENV_NOT_ALLOWED")
    if not isinstance(prompt, bytes) or len(prompt) > 1024 * 1024:
        raise Rejected("E_REQUEST_SIZE")
    try:
        prompt.decode("utf-8", "strict")
    except UnicodeError:
        raise Rejected("E_REQUEST_ENCODING") from None
    return {
        "schema_version": 1, "record_type": "invocation-fixture",
        "execution_allowed": False, "adapter_id": "synthetic-only",
        "request_id": context.request_id, "run_id": context.run_id,
        "role": context.role, "protocol_id": context.dialect,
        "shell": False,
        "argv_tail": list(fixed_args) + ["--mode", "json", "--provider", context.provider,
                      "--model", context.model, "--tools", ",".join(context.allowed_tools)],
        "stdin_ref": resource(prompt, "request-body", "synthetic-request", "fixtures/request.txt"),
        "env_names": sorted(env_names),
    }


def capability_gaps(capabilities: Mapping[str, dict], requirements: Mapping[str, set[str]]) -> list[str]:
    """Trusted inventory input; enum labels alone do not authenticate evidence."""
    gaps = []
    for name, accepted_sources in sorted(requirements.items()):
        cap = capabilities.get(name, {})
        if (cap.get("status") != "verified" or cap.get("source_kind") not in accepted_sources
                or not cap.get("evidence_ref")):
            gaps.append(name)
    return gaps


KNOWN_FIELDS = {
    "session": {"type", "version", "id", "timestamp", "cwd"},
    "agent_start": {"type"}, "agent_end": {"type", "messages", "willRetry"},
    "agent_settled": {"type"}, "turn_start": {"type"},
    "turn_end": {"type", "message", "toolResults"},
    "message_start": {"type", "message"},
    "message_update": {"type", "usage", "assistantMessageEvent"},
    "message_end": {"type", "message"},
    "tool_execution_start": {"type", "toolCallId", "toolName", "args"},
    "tool_execution_update": {"type", "toolCallId", "toolName", "args", "partialResult"},
    "tool_execution_end": {"type", "toolCallId", "toolName", "result", "isError"},
    "notice": {"type", "text"},  # Explicit synthetic informational allowlist.
}
MESSAGE_FIELDS = {"role", "content", "provider", "model", "responseModel", "stopReason", "usage",
                  "toolCallId", "toolName", "isError", "timestamp"}
TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite")


def audit_synthetic(chunks: Iterable[bytes], context: Context,
                    transport: Transport = Transport()) -> dict:
    """Audit a documented synthetic subset; reject unsupported real-world variants."""
    context.validate()
    data, frames = frame_jsonl(chunks)
    raw_ref = resource(data, "raw-evidence", "synthetic-stdout", "fixtures/synthetic-stream.jsonl")
    issues: list[str] = []
    issue_occurrences = 0
    events = []
    header = False
    active = turn = settled = False
    pending_role: str | None = None
    agent_ends = 0
    last_will_retry = False
    tools: dict[str, dict] = {}
    messages: list[dict] = []
    cycle_messages: list[dict] = []
    identities = []
    observations = []
    final_report: str | None = None
    final_stop: str | None = None
    usage_complete = True
    tool_errors = 0

    def issue(code: str) -> None:
        nonlocal issue_occurrences
        issue_occurrences += 1
        if code not in issues:
            issues.append(code)

    def usage(msg: dict, frame: Frame) -> None:
        nonlocal usage_complete
        raw = msg.get("usage")
        counts = {}
        if raw is not None and (not isinstance(raw, dict) or
                set(raw) - {*TOKEN_FIELDS, "totalTokens", "cost", "currency"}):
            issue("E_USAGE_SCHEMA")
            raw = None
        for key in TOKEN_FIELDS:
            n = raw.get(key) if isinstance(raw, dict) else None
            if n is None:
                counts[key] = None
                usage_complete = False
            elif type(n) is not int or not 0 <= n <= MAX_SAFE_INTEGER:
                issue("E_USAGE_NUMBER")
                counts[key] = None
                usage_complete = False
            else:
                counts[key] = n
        if isinstance(raw, dict) and "totalTokens" in raw:
            n = raw["totalTokens"]
            if type(n) is not int or not 0 <= n <= MAX_SAFE_INTEGER:
                issue("E_USAGE_NUMBER")
            elif all(v is not None for v in counts.values()) and n != sum(counts.values()):
                issue("E_USAGE_TOTAL")
        amount = currency = None
        # Synthetic wire uses a decimal string, not Pi's vendor-number cost encoding.
        if isinstance(raw, dict) and raw.get("cost") is not None:
            cost = raw["cost"]
            if (not isinstance(cost, dict) or set(cost) != {"total"}
                    or not isinstance(cost.get("total"), str)
                    or not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?", cost["total"])):
                issue("E_COST_SCHEMA")
            else:
                try:
                    amount = format(Decimal(cost["total"]), "f")
                except InvalidOperation:
                    issue("E_COST_SCHEMA")
            candidate = raw.get("currency")
            if isinstance(candidate, str) and re.fullmatch(r"[A-Z]{3}", candidate):
                currency = candidate
            if currency is None:
                amount = None
        observations.append({
            "observation_key": f"{context.run_id}:stdout:{frame.byte_start}:assistant-usage",
            "source_byte_start": frame.byte_start,
            "tokens": counts, "reported_estimate": {"amount": amount, "currency": currency},
            "settled_cost": {"amount": None, "currency": None, "reason": "no_billing_receipt"},
        })

    for frame in frames:
        value = frame.value
        old_issue_count = issue_occurrences
        event_type = "vendor.record"
        if frame.error:
            issue(frame.error)
            event_type = "protocol.invalid_record"
        elif value is not None:
            kind = value.get("type")
            if not isinstance(kind, str) or kind not in KNOWN_FIELDS:
                issue("E_EVENT_UNKNOWN")
                event_type = "protocol.unknown_record"
            elif set(value) - KNOWN_FIELDS[kind]:
                issue("E_EVENT_FIELD")
            elif settled:
                issue("E_EVENT_AFTER_TERMINAL")
            elif kind == "session":
                if (header or frame.byte_start != 0 or type(value.get("version")) is not int
                        or value.get("version") != 3 or not isinstance(value.get("id"), str)
                        or not value.get("id")):
                    issue("E_SESSION_HEADER")
                else:
                    header = True
            elif not header:
                issue("E_SESSION_MISSING")
            elif kind == "agent_start":
                if active or turn:
                    issue("E_AGENT_ORDER")
                else:
                    active = True
                    cycle_messages = []
                    # Protocol IDs/byte offsets remain run-local; call IDs reset each cycle.
                    tools = {}
            elif kind == "turn_start":
                if not active or turn:
                    issue("E_TURN_ORDER")
                else:
                    turn = True
            elif kind == "message_start":
                msg = value.get("message")
                if not turn or pending_role is not None or not isinstance(msg, dict):
                    issue("E_MESSAGE_ORDER")
                else:
                    role = msg.get("role")
                    if role not in ("user", "assistant", "toolResult"):
                        issue("E_MESSAGE_ROLE")
                    else:
                        pending_role = role
            elif kind == "message_update":
                if pending_role != "assistant":
                    issue("E_MESSAGE_ORDER")
                # Live deltas and cumulative usage never count as additional final usage.
                nested = value.get("assistantMessageEvent")
                if not isinstance(nested, dict) or nested.get("type") not in ("text_delta", "thinking_delta"):
                    issue("E_UPDATE_UNSUPPORTED")
            elif kind == "message_end":
                msg = value.get("message")
                if not isinstance(msg, dict) or set(msg) - MESSAGE_FIELDS:
                    issue("E_MESSAGE_SCHEMA")
                elif pending_role is None or msg.get("role") != pending_role or not turn:
                    issue("E_MESSAGE_ORDER")
                else:
                    pending_role = None
                    role = msg["role"]
                    if role == "assistant":
                        cycle_messages.append(msg)
                        messages.append(msg)
                        provider, model = msg.get("provider"), msg.get("model")
                        response_model = msg.get("responseModel")
                        identities.append({"requested": {"provider": context.provider, "model": context.model},
                            "cli_reported": {"provider": provider, "model": model},
                            "provider_reported_model": response_model,
                            "source_byte_start": frame.byte_start})
                        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
                            issue("E_MODEL_UNKNOWN")
                        elif (provider, model) != (context.provider, context.model):
                            issue("E_MODEL_MISMATCH")
                        if response_model is not None and response_model != context.model:
                            issue("E_PROVIDER_MODEL_MISMATCH")
                        if context.require_provider and not response_model:
                            issue("E_PROVIDER_MODEL_UNKNOWN")
                        stop = msg.get("stopReason")
                        final_stop = stop
                        content = msg.get("content")
                        if stop not in ("stop", "toolUse", "error", "aborted", "length"):
                            issue("E_STOP_UNSUPPORTED")
                        if not isinstance(content, list):
                            issue("E_MESSAGE_CONTENT")
                            content = []
                        text_parts = []
                        for block in content:
                            if not isinstance(block, dict):
                                issue("E_MESSAGE_CONTENT")
                                continue
                            if block.get("type") == "text" and isinstance(block.get("text"), str):
                                text_parts.append(block["text"])
                            elif block.get("type") == "toolCall":
                                call_id, name = block.get("id"), block.get("name")
                                if (not isinstance(call_id, str) or not call_id or not isinstance(name, str)
                                        or not isinstance(block.get("arguments"), dict)):
                                    issue("E_TOOL_SCHEMA")
                                elif call_id in tools:
                                    issue("E_TOOL_DUPLICATE")
                                else:
                                    tools[call_id] = {"name": name, "state": "declared", "result_seen": False}
                                    if name not in context.allowed_tools:
                                        issue("E_TOOL_NOT_ALLOWED")
                                if stop != "toolUse":
                                    issue("E_TOOL_STOP")
                            elif block.get("type") != "thinking":
                                issue("E_CONTENT_UNKNOWN")
                        if stop == "toolUse" and not any(isinstance(b, dict) and b.get("type") == "toolCall" for b in content):
                            issue("E_TOOL_DECLARATION_MISSING")
                        final_report = "".join(text_parts) if stop == "stop" else None
                        usage(msg, frame)
                    elif role == "toolResult":
                        call_id = msg.get("toolCallId")
                        item = tools.get(call_id) if isinstance(call_id, str) else None
                        if (item is None or item["state"] != "ended" or item["result_seen"]
                                or msg.get("toolName") != item["name"]
                                or type(msg.get("isError")) is not bool):
                            issue("E_TOOL_RESULT_ORDER")
                        else:
                            item["result_seen"] = True
                            if msg["isError"] != item["is_error"]:
                                issue("E_TOOL_RESULT_CONFLICT")
            elif kind.startswith("tool_execution_"):
                call_id = value.get("toolCallId")
                name = value.get("toolName")
                item = tools.get(call_id) if isinstance(call_id, str) else None
                if not turn or item is None or item["name"] != name:
                    issue("E_TOOL_ORDER")
                elif kind == "tool_execution_start":
                    if item["state"] != "declared" or not isinstance(value.get("args"), dict):
                        issue("E_TOOL_ORDER")
                    else:
                        item["state"] = "running"
                elif kind == "tool_execution_update":
                    if item["state"] != "running":
                        issue("E_TOOL_ORDER")
                elif item["state"] != "running" or type(value.get("isError")) is not bool:
                    issue("E_TOOL_ORDER")
                else:
                    item["state"] = "ended"
                    item["is_error"] = value["isError"]
                    tool_errors += int(value["isError"])
            elif kind == "turn_end":
                if not turn or pending_role is not None:
                    issue("E_TURN_ORDER")
                if any(t["state"] != "ended" or not t["result_seen"] for t in tools.values()):
                    issue("E_TOOL_GAP")
                turn = False
            elif kind == "agent_end":
                if not active or turn or pending_role is not None:
                    issue("E_AGENT_ORDER")
                echo = value.get("messages")
                if not isinstance(echo, list):
                    issue("E_AGENT_END_SCHEMA")
                else:
                    seen = {stable_bytes(m) for m in cycle_messages}
                    for msg in echo:
                        if not isinstance(msg, dict):
                            issue("E_AGENT_END_SCHEMA")
                        elif msg.get("role") == "assistant" and stable_bytes(msg) not in seen:
                            issue("E_UNPAIRED_MESSAGE")
                if type(value.get("willRetry", False)) is not bool:
                    issue("E_AGENT_END_SCHEMA")
                last_will_retry = value.get("willRetry", False)
                active = False
                agent_ends += 1
                if context.dialect == "synthetic-legacy-v1":
                    if last_will_retry:
                        issue("E_RETRY_UNSUPPORTED")
                    settled = True
            elif kind == "agent_settled":
                if active or turn or not agent_ends or last_will_retry:
                    issue("E_SETTLED_ORDER")
                else:
                    settled = True
            # 'notice' is retained, never used to authorize or report success.
        events.append({
            "schema_version": 1, "record_type": "run-event",
            "request_id": context.request_id, "run_id": context.run_id,
            "seq": len(events) + 1, "observed_at": "2026-09-25T00:00:00.000000Z",
            "role": context.role, "type": event_type, "source_kind": "vendor",
            "payload": {"vendor_type": value.get("type") if value else None,
                        "fixture_origin": "synthetic"},
            "raw_refs": [{"resource_ref": raw_ref, "byte_start": frame.byte_start,
                          "byte_end": frame.byte_end, "stream": "stdout"}],
            "integrity": "incomplete" if issue_occurrences > old_issue_count or frame.error else "complete",
            "adapter_revision": "synthetic-reference-1",
        })

    if not header:
        issue("E_SESSION_MISSING")
    if not settled:
        issue("E_TERMINAL_MISSING")
    if active or turn or pending_role is not None:
        issue("E_STREAM_OPEN_SCOPE")
    if any(t["state"] != "ended" or not t["result_seen"] for t in tools.values()):
        issue("E_TOOL_GAP")
    if not messages:
        issue("E_MODEL_UNKNOWN")
        usage_complete = False
    if final_stop != "stop":
        issue("E_FINAL_NOT_STOP")
    if final_report is None or not final_report.strip():
        issue("E_REPORT_MISSING")
    if context.require_token_usage and not usage_complete:
        issue("E_USAGE_REQUIRED")
    if not transport.started:
        issue("E_PROCESS_NOT_STARTED")
    if transport.exit_code is None:
        issue("E_PROCESS_UNKNOWN")
    elif type(transport.exit_code) is not int or transport.exit_code != 0:
        issue("E_PROCESS_EXIT")
    if not transport.stdout_eof or not transport.stderr_eof or not transport.capture_complete:
        issue("E_CAPTURE_INCOMPLETE")
    if transport.cancelled:
        issue("E_CANCELLED")
    if transport.timed_out:
        issue("E_TIMEOUT")
    model_codes = {"E_MODEL_UNKNOWN", "E_MODEL_MISMATCH", "E_PROVIDER_MODEL_UNKNOWN", "E_PROVIDER_MODEL_MISMATCH"}
    transport_codes = {"E_PROCESS_NOT_STARTED", "E_PROCESS_UNKNOWN", "E_PROCESS_EXIT", "E_CAPTURE_INCOMPLETE", "E_CANCELLED", "E_TIMEOUT"}
    non_protocol_codes = model_codes | transport_codes | {"E_USAGE_REQUIRED", "E_REPORT_MISSING", "E_FINAL_NOT_STOP"}
    return {
        "schema_version": 1, "record_type": "adapter-result-fixture",
        "origin": "synthetic", "request_id": context.request_id, "run_id": context.run_id,
        "protocol_complete": not any(c not in non_protocol_codes for c in issues) and settled,
        "terminal_observed": settled, "transport": asdict(transport),
        "model_check": "not_matched" if any(c in model_codes for c in issues) else "cli_matched",
        "identity_observations": identities, "token_usage_complete": usage_complete,
        "cost_evidence_complete": False,
        "usage_observations": observations, "final_report": final_report,
        "tool_error_count": tool_errors, "eligible_for_core_checks": not issues,
        "issue_codes": issues, "events": events,
        "gate_verdict": None,
    }
```

#### B.4.2 `fixtures.py`

<!-- file: fixtures.py -->
```python
"""All records in this file are constructed, not captured from Pi."""
from copy import deepcopy
from adapter_reference import Context, stable_bytes

CONTEXT = Context("req-synthetic-01", "run-synthetic-01", "executor", "fixture-provider", "fixture-model")


def usage(input_tokens: int = 100, output_tokens: int = 20) -> dict:
    return {"input": input_tokens, "output": output_tokens, "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": input_tokens + output_tokens}


def assistant(*, tool: bool = False, text: str = "检查完成。") -> dict:
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "toolCall", "id": "call-01", "name": "read", "arguments": {"path": "README.md"}})
    return {"role": "assistant", "provider": "fixture-provider", "model": "fixture-model",
            "content": content, "stopReason": "toolUse" if tool else "stop", "usage": usage()}


def records(*, tool: bool = False) -> list[dict]:
    first = assistant(tool=tool)
    final = assistant(text="检查完成。")
    user = {"role": "user", "content": "仅分析测试夹具。"}
    out = [
        {"type": "session", "version": 3, "id": "vendor-synthetic-01", "timestamp": "2026-09-25T00:00:00Z", "cwd": "/synthetic"},
        {"type": "agent_start"}, {"type": "turn_start"},
        {"type": "message_start", "message": deepcopy(user)},
        {"type": "message_end", "message": deepcopy(user)},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "usage": usage(50, 1), "assistantMessageEvent": {"type": "text_delta", "delta": "检查", "contentIndex": 0}},
        {"type": "message_end", "message": deepcopy(first)},
    ]
    if tool:
        result = {"role": "toolResult", "toolCallId": "call-01", "toolName": "read", "content": [], "isError": False}
        out += [
            {"type": "tool_execution_start", "toolCallId": "call-01", "toolName": "read", "args": {"path": "README.md"}},
            {"type": "tool_execution_update", "toolCallId": "call-01", "toolName": "read", "args": {"path": "README.md"}, "partialResult": {"content": []}},
            {"type": "tool_execution_end", "toolCallId": "call-01", "toolName": "read", "result": {"content": []}, "isError": False},
            {"type": "message_start", "message": {"role": "toolResult"}},
            {"type": "message_end", "message": result},
            {"type": "turn_end", "message": deepcopy(first), "toolResults": [deepcopy(result)]},
            {"type": "turn_start"},
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "message_end", "message": deepcopy(final)},
        ]
    out += [
        {"type": "turn_end", "message": deepcopy(final if tool else first), "toolResults": []},
        {"type": "agent_end", "messages": [deepcopy(first), deepcopy(final)] if tool else [deepcopy(first)], "willRetry": False},
        {"type": "agent_settled"},
    ]
    return out


def wire(values: list[dict]) -> bytes:
    return b"".join(stable_bytes(v) for v in values)
```

#### B.4.3 `test_reference.py`

<!-- file: test_reference.py -->
```python
from __future__ import annotations
import copy
from dataclasses import replace
import unittest

from adapter_reference import (Rejected, Transport, strict_json, stable_bytes, frame_jsonl,
    audit_synthetic, build_synthetic_invocation, capability_gaps, resource)
from fixtures import CONTEXT, records, wire


def message(rows: list[dict], role: str = "assistant") -> dict:
    return next(r["message"] for r in rows if r["type"] == "message_end" and r["message"]["role"] == role)


class ReferenceTests(unittest.TestCase):
    def test_plain_is_only_eligible_not_pass(self):
        result = audit_synthetic([wire(records())], CONTEXT)
        self.assertEqual(result["issue_codes"], [])
        self.assertTrue(result["eligible_for_core_checks"])
        self.assertIsNone(result["gate_verdict"])

    def test_tool_lifecycle_complete(self):
        result = audit_synthetic([wire(records(tool=True))], CONTEXT)
        self.assertEqual(result["issue_codes"], [])
        self.assertEqual(len(result["usage_observations"]), 2)

    def test_unhashable_tool_result_id_is_rejected_not_crashed(self):
        for invalid_id in ([], {}, ["call-1"], {"id": "call-1"}):
            with self.subTest(invalid_id=invalid_id):
                rows = records(tool=True)
                message(rows, "toolResult")["toolCallId"] = invalid_id
                result = audit_synthetic([wire(rows)], CONTEXT)
                self.assertIn("E_TOOL_RESULT_ORDER", result["issue_codes"])
                self.assertFalse(result["eligible_for_core_checks"])

    def test_echo_and_deltas_not_double_counted(self):
        result = audit_synthetic([wire(records())], CONTEXT)
        self.assertEqual(len(result["usage_observations"]), 1)
        self.assertEqual(result["usage_observations"][0]["tokens"]["input"], 100)

    def test_raw_half_open_ranges(self):
        data = wire(records(tool=True))
        result = audit_synthetic([data], CONTEXT)
        pos = 0
        for event in result["events"]:
            ref = event["raw_refs"][0]
            self.assertEqual(ref["byte_start"], pos)
            self.assertTrue(data[ref["byte_start"]:ref["byte_end"]].endswith(b"\n"))
            pos = ref["byte_end"]
        self.assertEqual(pos, len(data))

    def test_chunk_invariance_every_split(self):
        data = wire(records())
        expected = audit_synthetic([data], CONTEXT)
        for cut in range(len(data) + 1):
            self.assertEqual(audit_synthetic([data[:cut], data[cut:]], CONTEXT), expected)

    def test_utf8_one_byte_chunks_and_unicode_separators(self):
        rows = records()
        rows.insert(2, {"type": "notice", "text": "中\u2028文\u2029末"})
        data = wire(rows)
        result = audit_synthetic([bytes([n]) for n in data], CONTEXT)
        self.assertTrue(result["eligible_for_core_checks"])
        self.assertEqual(len(result["events"]), len(rows))

    def test_crlf_allowed(self):
        self.assertTrue(audit_synthetic([wire(records()).replace(b"\n", b"\r\n")], CONTEXT)["eligible_for_core_checks"])

    def test_duplicate_errors_each_frame_incomplete(self):
        data = wire(records()[:-1]) + b"bad\nbad\n" + wire(records()[-1:])
        result = audit_synthetic([data], CONTEXT)
        bad = [e for e in result["events"] if e["type"] == "protocol.invalid_record"]
        self.assertEqual(len(bad), 2)
        self.assertTrue(all(e["integrity"] == "incomplete" for e in bad))

    def test_unknown_record_retained(self):
        rows = records()
        rows.insert(2, {"type": "future_event", "approved": True})
        result = audit_synthetic([wire(rows)], CONTEXT)
        self.assertEqual(len(result["events"]), len(rows))
        self.assertIn("E_EVENT_UNKNOWN", result["issue_codes"])

    def test_vendor_cannot_override_core_context(self):
        rows = records()
        rows[1]["run_id"] = "forged-run"
        rows[1]["role"] = "supervisor"
        result = audit_synthetic([wire(rows)], CONTEXT)
        self.assertIn("E_EVENT_FIELD", result["issue_codes"])
        self.assertTrue(all(e["run_id"] == CONTEXT.run_id and e["role"] == CONTEXT.role for e in result["events"]))

    def test_provider_missing_is_not_backfilled(self):
        result = audit_synthetic([wire(records())], CONTEXT)
        self.assertIsNone(result["identity_observations"][0]["provider_reported_model"])

    def test_missing_usage_is_unknown_not_zero(self):
        rows = records()
        message(rows).pop("usage")
        rows[-2]["messages"] = [copy.deepcopy(message(rows))]
        result = audit_synthetic([wire(rows)], CONTEXT)
        self.assertTrue(result["eligible_for_core_checks"])
        self.assertFalse(result["token_usage_complete"])
        self.assertIsNone(result["usage_observations"][0]["tokens"]["input"])

    def test_report_from_final_not_deltas(self):
        result = audit_synthetic([wire(records())], CONTEXT)
        self.assertEqual(result["final_report"], "检查完成。")

    def test_numeric_zero_estimate_is_not_billing_receipt(self):
        rows = records()
        message(rows)["usage"].update({"cost": {"total": "0.00"}, "currency": "USD"})
        rows[-2]["messages"] = [copy.deepcopy(message(rows))]
        result = audit_synthetic([wire(rows)], CONTEXT)
        u = result["usage_observations"][0]
        self.assertEqual(u["reported_estimate"]["amount"], "0.00")
        self.assertIsNone(u["settled_cost"]["amount"])

    def test_missing_currency_keeps_cost_unknown(self):
        rows = records()
        message(rows)["usage"]["cost"] = {"total": "1.2500"}
        result = audit_synthetic([wire(rows)], CONTEXT)
        self.assertIsNone(result["usage_observations"][0]["reported_estimate"]["amount"])

    def test_legacy_dialect_is_explicit(self):
        ctx = replace(CONTEXT, dialect="synthetic-legacy-v1")
        result = audit_synthetic([wire(records()[:-1])], ctx)
        self.assertTrue(result["eligible_for_core_checks"])

    def test_legacy_cannot_guess_modern_terminal(self):
        ctx = replace(CONTEXT, dialect="synthetic-legacy-v1")
        self.assertIn("E_EVENT_AFTER_TERMINAL", audit_synthetic([wire(records())], ctx)["issue_codes"])

    def test_two_cycles_are_not_content_deduplicated(self):
        first, second = records(), records()
        rows = first[:-1] + second[1:]
        result = audit_synthetic([wire(rows)], CONTEXT)
        self.assertTrue(result["eligible_for_core_checks"])
        self.assertEqual(len(result["usage_observations"]), 2)
        self.assertNotEqual(*[u["observation_key"] for u in result["usage_observations"]])

    def test_run_retry_has_distinct_usage_key_same_budget_root(self):
        one = audit_synthetic([wire(records())], CONTEXT)
        other = audit_synthetic([wire(records())], replace(CONTEXT, run_id="run-synthetic-02"))
        self.assertEqual(one["request_id"], other["request_id"])
        self.assertNotEqual(one["usage_observations"][0]["observation_key"], other["usage_observations"][0]["observation_key"])

    def test_replay_deterministic_same_usage_key(self):
        data = wire(records())
        self.assertEqual(audit_synthetic([data], CONTEXT), audit_synthetic([data], CONTEXT))

    def test_invocation_does_not_embed_prompt(self):
        prompt = b"--api-key=not-a-real-secret\n$(echo sentinel)"
        invocation = build_synthetic_invocation(CONTEXT, prompt)
        self.assertNotIn(prompt.decode(), invocation["argv_tail"])
        self.assertFalse(invocation["execution_allowed"])
        self.assertFalse(invocation["shell"])

    def test_invocation_deterministic(self):
        a = build_synthetic_invocation(CONTEXT, b"hello")
        self.assertEqual(a, build_synthetic_invocation(CONTEXT, b"hello"))
        self.assertNotEqual(a["stdin_ref"], build_synthetic_invocation(CONTEXT, b"hello\n")["stdin_ref"])

    def test_capability_synthetic_does_not_prove_real_support(self):
        caps = {"terminal_event": {"status": "verified", "source_kind": "synthetic", "evidence_ref": {"fixture": True}}}
        self.assertEqual(capability_gaps(caps, {"terminal_event": {"real-run"}}), ["terminal_event"])

    def test_capability_explicit_test_policy(self):
        caps = {"terminal_event": {"status": "verified", "source_kind": "synthetic", "evidence_ref": {"fixture": True}}}
        self.assertEqual(capability_gaps(caps, {"terminal_event": {"synthetic"}}), [])

    def test_declared_capability_not_verified(self):
        caps = {"terminal_event": {"status": "declared", "source_kind": "documentation", "evidence_ref": {"fixture": True}}}
        self.assertEqual(capability_gaps(caps, {"terminal_event": {"documentation"}}), ["terminal_event"])

    def test_secret_not_echoed_in_exception(self):
        with self.assertRaises(Rejected) as caught:
            build_synthetic_invocation(CONTEXT, b"ok", fixed_args=("--api-key", "sentinel-secret"))
        self.assertNotIn("sentinel-secret", str(caught.exception))

    def test_depth_inside_string_is_not_nesting(self):
        self.assertEqual(strict_json(b'{"text":"[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["}')['text'], "[" * 40)


# Every generated test is an independent single-input rejection case.
JSON_CASES = {
    "duplicate_key": (b'{"type":"a","type":"b"}', "E_JSON_DUPLICATE_KEY"),
    "escaped_duplicate_key": (b'{"type":"a","\\u0074ype":"b"}', "E_JSON_DUPLICATE_KEY"),
    "nan": (b'{"x":NaN}', "E_JSON_NONFINITE"),
    "infinity": (b'{"x":Infinity}', "E_JSON_NONFINITE"),
    "overflow": (b'{"x":1e400}', "E_JSON_NONFINITE"),
    "unsafe_integer": (b'{"x":9007199254740992}', "E_JSON_INTEGER"),
    "surrogate": (b'{"x":"\\ud800"}', "E_JSON_UNICODE"),
    "bad_utf8": (b'{"x":"\xff"}', "E_JSON_ENCODING"),
    "bom": (b'\xef\xbb\xbf{}', "E_JSON_BOM"),
    "two_documents": (b'{}{}', "E_JSON_SYNTAX"),
    "array_root": (b'[]', "E_JSON_OBJECT"),
    "empty": (b'', "E_JSON_SYNTAX"),
    "depth": (b'{"x":' + b'[' * 33 + b'0' + b']' * 33 + b'}', "E_JSON_DEPTH"),
}
for name, (data, expected) in JSON_CASES.items():
    def test(self, data=data, expected=expected):
        with self.assertRaises(Rejected) as caught:
            strict_json(data)
        self.assertEqual(caught.exception.code, expected)
    setattr(ReferenceTests, "test_json_" + name, test)


TRANSPORT_CASES = {
    "nonzero": (Transport(exit_code=1), "E_PROCESS_EXIT"),
    "unknown_exit": (Transport(exit_code=None), "E_PROCESS_UNKNOWN"),
    "not_started": (Transport(started=False), "E_PROCESS_NOT_STARTED"),
    "stdout_not_eof": (Transport(stdout_eof=False), "E_CAPTURE_INCOMPLETE"),
    "stderr_not_eof": (Transport(stderr_eof=False), "E_CAPTURE_INCOMPLETE"),
    "capture_incomplete": (Transport(capture_complete=False), "E_CAPTURE_INCOMPLETE"),
    "cancelled": (Transport(cancelled=True), "E_CANCELLED"),
    "timeout": (Transport(timed_out=True), "E_TIMEOUT"),
    "boolean_exit": (Transport(exit_code=True), "E_PROCESS_EXIT"),
}
for name, (transport, expected) in TRANSPORT_CASES.items():
    def test(self, transport=transport, expected=expected):
        result = audit_synthetic([wire(records())], CONTEXT, transport)
        self.assertIn(expected, result["issue_codes"])
        self.assertFalse(result["eligible_for_core_checks"])
    setattr(ReferenceTests, "test_transport_" + name, test)


def remove_type(rows, kind):
    rows[:] = [r for r in rows if r["type"] != kind]


MUTATIONS = {
    "missing_terminal": (False, lambda r: r.pop(), "E_TERMINAL_MISSING"),
    "missing_session": (False, lambda r: r.pop(0), "E_SESSION_MISSING"),
    "wrong_session_version": (False, lambda r: r[0].update(version=999), "E_SESSION_HEADER"),
    "missing_session_id": (False, lambda r: r[0].pop("id"), "E_SESSION_HEADER"),
    "model_missing": (False, lambda r: message(r).pop("model"), "E_MODEL_UNKNOWN"),
    "model_mismatch": (False, lambda r: message(r).update(model="other-model"), "E_MODEL_MISMATCH"),
    "provider_mismatch": (False, lambda r: message(r).update(provider="other-provider"), "E_MODEL_MISMATCH"),
    "response_model_mismatch": (False, lambda r: message(r).update(responseModel="other-model"), "E_PROVIDER_MODEL_MISMATCH"),
    "stop_pending": (False, lambda r: message(r).update(stopReason="pending"), "E_STOP_UNSUPPORTED"),
    "stop_error": (False, lambda r: message(r).update(stopReason="error"), "E_FINAL_NOT_STOP"),
    "stop_aborted": (False, lambda r: message(r).update(stopReason="aborted"), "E_FINAL_NOT_STOP"),
    "stop_length": (False, lambda r: message(r).update(stopReason="length"), "E_FINAL_NOT_STOP"),
    "empty_report": (False, lambda r: message(r).update(content=[]), "E_REPORT_MISSING"),
    "cost_negative": (False, lambda r: message(r)["usage"].update(cost={"total": "-1.0"}), "E_COST_SCHEMA"),
    "cost_number_not_synthetic_decimal": (False, lambda r: message(r)["usage"].update(cost={"total": 1}), "E_COST_SCHEMA"),
    "token_boolean": (False, lambda r: message(r)["usage"].update(input=True), "E_USAGE_NUMBER"),
    "token_negative": (False, lambda r: message(r)["usage"].update(output=-1), "E_USAGE_NUMBER"),
    "token_fractional": (False, lambda r: message(r)["usage"].update(input=1.5), "E_USAGE_NUMBER"),
    "token_total": (False, lambda r: message(r)["usage"].update(totalTokens=999), "E_USAGE_TOTAL"),
    "tool_missing_end": (True, lambda r: remove_type(r, "tool_execution_end"), "E_TOOL_GAP"),
    "tool_missing_start": (True, lambda r: remove_type(r, "tool_execution_start"), "E_TOOL_ORDER"),
    "tool_wrong_name": (True, lambda r: next(x for x in r if x["type"] == "tool_execution_start").update(toolName="write"), "E_TOOL_ORDER"),
    "tool_not_allowed": (True, lambda r: message(r)["content"][-1].update(name="write"), "E_TOOL_NOT_ALLOWED"),
    "tool_duplicate_id": (True, lambda r: message(r)["content"].append(copy.deepcopy(message(r)["content"][-1])), "E_TOOL_DUPLICATE"),
    "tool_result_conflict": (True, lambda r: message(r, "toolResult").update(isError=True), "E_TOOL_RESULT_CONFLICT"),
    "tool_unpaired_echo": (False, lambda r: r[-2]["messages"][0].update(model="echo-only-model"), "E_UNPAIRED_MESSAGE"),
    "settled_before_end": (False, lambda r: r.insert(-2, {"type": "agent_settled"}), "E_SETTLED_ORDER"),
    "event_after_terminal": (False, lambda r: r.append({"type": "agent_start"}), "E_EVENT_AFTER_TERMINAL"),
    "settled_during_retry": (False, lambda r: r[-2].update(willRetry=True), "E_SETTLED_ORDER"),
    "unknown_content": (False, lambda r: message(r)["content"].append({"type": "future-content"}), "E_CONTENT_UNKNOWN"),
}
for name, (tool, mutate, expected) in MUTATIONS.items():
    def test(self, tool=tool, mutate=mutate, expected=expected):
        rows = records(tool=tool)
        mutate(rows)
        result = audit_synthetic([wire(rows)], CONTEXT)
        self.assertIn(expected, result["issue_codes"])
        self.assertFalse(result["eligible_for_core_checks"])
    setattr(ReferenceTests, "test_audit_" + name, test)


GUARD_CASES = {
    "unknown_protocol": lambda: audit_synthetic([wire(records())], replace(CONTEXT, dialect="unknown")),
    "legacy_override_env": lambda: build_synthetic_invocation(CONTEXT, b"ok", env_names=("PI_EXEC_MODEL",)),
    "unapproved_env": lambda: build_synthetic_invocation(CONTEXT, b"ok", env_names=("NODE_OPTIONS",)),
    "duplicate_env": lambda: build_synthetic_invocation(CONTEXT, b"ok", env_names=("SYNTHETIC_API_KEY", "SYNTHETIC_API_KEY")),
    "prompt_bad_utf8": lambda: build_synthetic_invocation(CONTEXT, b"\xff"),
    "fixed_args_override": lambda: build_synthetic_invocation(CONTEXT, b"ok", fixed_args=("--model=other",)),
    "capture_limit": lambda: frame_jsonl([b"x" * 65], max_record=8, max_total=64),
    "nonbytes_chunk": lambda: frame_jsonl(["x"]),
    "relative_path_escape": lambda: resource(b"", "fixture", "fixture-1", "../secret"),
    "absolute_path": lambda: resource(b"", "fixture", "fixture-1", "/secret"),
}
for name, action in GUARD_CASES.items():
    def test(self, action=action):
        with self.assertRaises(Rejected):
            action()
    setattr(ReferenceTests, "test_guard_" + name, test)


class ExtraTests(unittest.TestCase):
    def test_complete_json_without_lf_is_truncated(self):
        _, frames = frame_jsonl([b'{"type":"agent_end"}'])
        self.assertEqual(frames[0].error, "E_FRAME_TRUNCATED")

    def test_oversize_record_retained_as_range(self):
        data, frames = frame_jsonl([b'{"x":"123456789"}\n'], max_record=8, max_total=64)
        self.assertEqual(frames[0].error, "E_FRAME_LIMIT")
        self.assertEqual(frames[0].byte_end, len(data))

    def test_provider_evidence_required(self):
        r = audit_synthetic([wire(records())], replace(CONTEXT, require_provider=True))
        self.assertIn("E_PROVIDER_MODEL_UNKNOWN", r["issue_codes"])

    def test_token_evidence_required(self):
        rows = records()
        message(rows).pop("usage")
        r = audit_synthetic([wire(rows)], replace(CONTEXT, require_token_usage=True))
        self.assertIn("E_USAGE_REQUIRED", r["issue_codes"])

    def test_early_wrong_model_not_hidden_by_final_correct_model(self):
        rows = records(tool=True)
        message(rows)["model"] = "wrong-early-model"
        r = audit_synthetic([wire(rows)], CONTEXT)
        self.assertIn("E_MODEL_MISMATCH", r["issue_codes"])
        self.assertEqual(len(r["identity_observations"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

#### B.4.4 `verify.py`

<!-- file: verify.py -->
```python
"""Reproducible offline verification. Fails explicitly if schema dependency is absent."""
from pathlib import Path
import ast
import copy
import hashlib
import importlib.metadata
import json
import platform
import sys
import unittest

from adapter_reference import stable_bytes

ROOT = Path(__file__).resolve().parent


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def verify() -> dict:
    if not __debug__:
        raise RuntimeError("verification requires assertions; do not use python -O")
    import jsonschema  # Installed dependency; this program never installs it.
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_reference.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("unit tests failed")
    schema = load("records.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    examples = load("record-examples.json")
    for sample in examples:
        validator.validate(sample)
    negative_samples = []
    for sample in examples:
        missing = copy.deepcopy(sample); missing.pop("record_type")
        extra = copy.deepcopy(sample); extra["approved"] = True
        negative_samples.extend([missing, extra])
    probe = copy.deepcopy(examples[1]); probe["model_calls_permitted"] = True
    negative_samples.append(probe)
    invocation = copy.deepcopy(examples[3]); invocation["execution_mode"] = "shell"
    negative_samples.append(invocation)
    cap = copy.deepcopy(examples[2]); cap["capabilities"][0]["evidence_refs"] = []
    negative_samples.append(cap)
    result_sample = copy.deepcopy(examples[4]); result_sample["gate_verdict"] = "PASS"
    negative_samples.append(result_sample)
    for sample in negative_samples:
        assert list(validator.iter_errors(sample)), "negative schema example accepted"
    vectors = load("golden-vectors.json")
    for vector in vectors:
        actual = stable_bytes(vector["value"])
        assert actual.hex() == vector["utf8_hex"]
        assert hashlib.sha256(actual).hexdigest() == vector["sha256"]
    refs = load("resource-refs.json")
    for ref in refs:
        path = (ROOT / ref["path"]).resolve()
        assert path.is_relative_to(ROOT)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
    # Validate every declared reference appearing in the examples, including nested ones.
    example_ref_count = 0
    def walk(node):
        nonlocal example_ref_count
        if isinstance(node, dict):
            if set(node) == {"kind", "id", "sha256", "path"}:
                path = (ROOT / node["path"]).resolve()
                assert path.is_relative_to(ROOT)
                assert hashlib.sha256(path.read_bytes()).hexdigest() == node["sha256"]
                example_ref_count += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(examples)
    # Limited AST check, not a security proof; main module contains no process/network imports.
    tree = ast.parse((ROOT / "adapter_reference.py").read_text())
    blocked = {"subprocess", "socket", "requests", "urllib", "http", "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split('.')[0] in blocked for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or '').split('.')[0] not in blocked
    return {
        "scope": "offline_synthetic_reference_only", "project_acceptance": "NOT_RUN",
        "python": platform.python_version(), "platform": sys.platform,
        "jsonschema": importlib.metadata.version("jsonschema"),
        "unit_tests": {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)},
        "schema_meta_checks": 1, "schema_positive_examples": len(examples),
        "schema_negative_examples_rejected": len(negative_samples),
        "golden_vectors": len(vectors), "unique_resource_hash_checks": len(refs),
        "example_reference_occurrences_checked": example_ref_count,
        "not_run": ["real_project_regression", "real_pi_probe", "real_model_call", "production_redaction",
                    "G0-G6", "Windows_and_macOS_processes", "budget_ledger", "power_loss_recovery"],
    }


if __name__ == "__main__":
    report = verify()
    path = ROOT / "verification-report.json"
    path.write_bytes(stable_bytes(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
```

#### B.4.5 `extract_reference.py`

<!-- file: extract_reference.py -->
```python
"""Extract only named code blocks into a new directory; never execute their content."""
from __future__ import annotations
import argparse
from pathlib import Path
import re

MARKER = re.compile(r"^<!-- file: ([A-Za-z0-9_./-]+) -->$")
FENCE = re.compile(r"^```([a-z0-9_-]*)$")
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


def collect(document: bytes) -> dict[str, bytes]:
    if len(document) > MAX_DOCUMENT_BYTES or document.startswith(b"\xef\xbb\xbf"):
        raise ValueError("document exceeds limit or contains a UTF-8 BOM")
    text = document.decode("utf-8", errors="strict")
    if "\r" in text:
        raise ValueError("expected LF document; do not silently normalize bytes")
    lines = text.splitlines(keepends=True)
    # splitlines is appropriate only for this controlled Markdown, NOT JSONL framing.
    output: dict[str, bytes] = {}
    i = 0
    while i < len(lines):
        marker = MARKER.fullmatch(lines[i].removesuffix("\n"))
        if marker is None:
            i += 1
            continue
        name = marker.group(1)
        if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
            raise ValueError("unsafe relative filename")
        if name in output or len(output) >= 64:
            raise ValueError("duplicate filename or excessive file count")
        i += 1
        if i >= len(lines) or FENCE.fullmatch(lines[i].removesuffix("\n")) is None:
            raise ValueError("named file is not immediately followed by a fenced block")
        i += 1
        start = i
        while i < len(lines) and lines[i].removesuffix("\n") != "```":
            i += 1
        if i >= len(lines):
            raise ValueError("unterminated code block")
        output[name] = "".join(lines[start:i]).encode("utf-8")
        i += 1
    if not output:
        raise ValueError("no named files found")
    return output


def extract(document: Path, destination: Path) -> dict[str, bytes]:
    files = collect(document.read_bytes())
    # No overwrite, cleanup, shell, package installation, or subprocess execution.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in files.items():
        path = destination.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("destination", type=Path, help="new directory whose parent exists")
    args = parser.parse_args()
    files = extract(args.document, args.destination)
    print(f"Extracted {len(files)} files; no code was executed.")


if __name__ == "__main__":
    main()
```

### B.5 复算向量、引用与合成资源

以下 JSONL 是人工编写的顺序工具样例，不是抓取的 Pi 输出；其余 fixture 的最小字段不能替代完整业务资源。

#### B.5.1 `golden-vectors.json`

<!-- file: golden-vectors.json -->
```json
[
  {
    "id": "V01",
    "sha256": "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
    "utf8_hex": "7b7d0a",
    "value": {}
  },
  {
    "id": "V02",
    "sha256": "e8d38819d39f705646bfb643368eca78f7db476c16471dbc33b941b27326410d",
    "utf8_hex": "7b2261223a312c2262223a327d0a",
    "value": {
      "a": 1,
      "b": 2
    }
  },
  {
    "id": "V03",
    "sha256": "dc500e3a5895219aa15fe47333b8a8df652da946b312bdaf7668d41ab4aab1ce",
    "utf8_hex": "7b2273223a22e4b8ad227d0a",
    "value": {
      "s": "中"
    }
  },
  {
    "id": "V04",
    "sha256": "74bbf766991bb3b3fddb64e4c1b3a535ebc62c6d938e3d3acfdc385b54f102ed",
    "utf8_hex": "7b2276223a5b322c315d7d0a",
    "value": {
      "v": [
        2,
        1
      ]
    }
  },
  {
    "id": "V05",
    "sha256": "6ba818e95a7f3be7f5e524c8769b89a02e096828c701bbcadbb0fb90313c60bc",
    "utf8_hex": "7b226e223a6e756c6c7d0a",
    "value": {
      "n": null
    }
  },
  {
    "id": "V06",
    "sha256": "592a64e7d18f1968d7157778083b11e53ac73a94b0770d75e4ab5723d2875f1c",
    "utf8_hex": "7b2273223a22c3a9227d0a",
    "value": {
      "s": "é"
    }
  },
  {
    "id": "V07",
    "sha256": "311dfbefb4b4a35574b5f22c483948f516be2bbb094bfee0ac9999e29abfa67a",
    "utf8_hex": "7b2273223a2265cc81227d0a",
    "value": {
      "s": "é"
    }
  },
  {
    "id": "V08",
    "sha256": "4352df4233f0d6492477e70dab0a2b2aa9683d2297d61c8e8d98b515a4bc2f8d",
    "utf8_hex": "7b226e223a312e307d0a",
    "value": {
      "n": 1.0
    }
  },
  {
    "id": "V09",
    "sha256": "63ac50753737f63908c4ff9b25d30491d0307332682eb0dc25247c04e06c1d55",
    "utf8_hex": "7b226e223a2d302e307d0a",
    "value": {
      "n": -0.0
    }
  },
  {
    "id": "V10",
    "sha256": "7561886733aae5edea7865fde1b15c42bd664013bcba3a95fc566ebb40eb5c4c",
    "utf8_hex": "7b2273223a2278e280a879227d0a",
    "value": {
      "s": "x y"
    }
  }
]
```

#### B.5.2 `resource-refs.json`

<!-- file: resource-refs.json -->
```json
[
  {
    "id": "synthetic-request",
    "kind": "fixture",
    "path": "fixtures/request.txt",
    "sha256": "d94eb23c85f478d794ddca34db7cb1b5022737f1cbfd0ce2a7dc18e51f13ccc7"
  },
  {
    "id": "synthetic-synthetic-stream",
    "kind": "fixture",
    "path": "fixtures/synthetic-stream.jsonl",
    "sha256": "825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"
  },
  {
    "id": "synthetic-model-schema",
    "kind": "fixture",
    "path": "fixtures/model.schema.json",
    "sha256": "fe1f59dd018fdfeb93eb4f75a67de1dc2dc4c3256d78f1000839109d7399824f"
  },
  {
    "id": "synthetic-options-schema",
    "kind": "fixture",
    "path": "fixtures/options.schema.json",
    "sha256": "d0157ce84790721854763c7480015e84f7cb0c7fde53afb21c3a42908805bf35"
  },
  {
    "id": "synthetic-argument-contract",
    "kind": "fixture",
    "path": "fixtures/argument-contract.json",
    "sha256": "f95ba5770df8d87a31ea7e4ec4d68b469821df0c1e75b6cdf5bc243a864d1228"
  },
  {
    "id": "synthetic-environment",
    "kind": "fixture",
    "path": "fixtures/environment.json",
    "sha256": "fbb64b67472bd695f2a798ae88d71afc46ac1193ab91df68813ea2e682c8076a"
  },
  {
    "id": "synthetic-config",
    "kind": "fixture",
    "path": "fixtures/config.json",
    "sha256": "c12ebc318a892fd9c7c80513fbf65226eb107c54f4baac68037d593270fcec88"
  },
  {
    "id": "synthetic-cwd",
    "kind": "fixture",
    "path": "fixtures/cwd.json",
    "sha256": "bf3883174222e387cd1eee70bfdc1ba6bf58c063f918cae807341b3f2e9f73e8"
  },
  {
    "id": "synthetic-resource-policy",
    "kind": "fixture",
    "path": "fixtures/resource-policy.json",
    "sha256": "65e017577f8cba1b94374bcc24066336ce843ebd802cdd73240cfa73d5b8193e"
  },
  {
    "id": "synthetic-probe-policy",
    "kind": "fixture",
    "path": "fixtures/probe-policy.json",
    "sha256": "20ab30ea0bacddde9c69732c48aaea27d69884fb69472bfbbf57b0173e9fbe48"
  },
  {
    "id": "synthetic-report",
    "kind": "fixture",
    "path": "fixtures/report.txt",
    "sha256": "f564fbd0f834ef2173c4a3f45d84b1deb19a4f06e510a91b938d0cfb8f5b6064"
  },
  {
    "id": "synthetic-parser",
    "kind": "parser",
    "path": "adapter_reference.py",
    "sha256": "579f10c43030a0b152205170d7730008746247d4737bc56a4e0099d46aa96ecd"
  },
  {
    "id": "synthetic-descriptor",
    "kind": "adapter-descriptor",
    "path": "fixtures/descriptor.json",
    "sha256": "c98e6acf0718647e2432c380ed63b94a247e339b3cce0a058706ebf4fa2129a0"
  },
  {
    "id": "synthetic-invocation",
    "kind": "invocation",
    "path": "fixtures/invocation.json",
    "sha256": "1bc668b520ca645c50b66c08d281952e3e4f8d9226286fdcd1ad8b1fda48af3c"
  },
  {
    "id": "synthetic-manifest",
    "kind": "raw-manifest",
    "path": "fixtures/raw-manifest.json",
    "sha256": "7658f4061d84ea26b11bae9c049065ade7665f7df682569a45080ea28394f78a"
  }
]
```

#### B.5.3 `reference-invocation.json`

<!-- file: reference-invocation.json -->
```json
{"adapter_id":"synthetic-only","argv_tail":["--mode","json","--provider","fixture-provider","--model","fixture-model","--tools","read"],"env_names":[],"execution_allowed":false,"protocol_id":"synthetic-settled-v1","record_type":"invocation-fixture","request_id":"req-synthetic-01","role":"executor","run_id":"run-synthetic-01","schema_version":1,"shell":false,"stdin_ref":{"id":"synthetic-request","kind":"request-body","path":"fixtures/request.txt","sha256":"d94eb23c85f478d794ddca34db7cb1b5022737f1cbfd0ce2a7dc18e51f13ccc7"}}
```

#### B.5.4 `fixtures/argument-contract.json`

<!-- file: fixtures/argument-contract.json -->
```json
{"allowed_fixed_args":[[],["--quiet"]],"origin":"synthetic"}
```

#### B.5.5 `fixtures/config.json`

<!-- file: fixtures/config.json -->
```json
{"model":"fixture-model","origin":"synthetic"}
```

#### B.5.6 `fixtures/cwd.json`

<!-- file: fixtures/cwd.json -->
```json
{"binding":"no-real-worktree","origin":"synthetic"}
```

#### B.5.7 `fixtures/descriptor.json`

<!-- file: fixtures/descriptor.json -->
```json
{"adapter_api_version":1,"adapter_id":"synthetic-only","argument_contract_ref":{"id":"synthetic-argument-contract","kind":"fixture","path":"fixtures/argument-contract.json","sha256":"f95ba5770df8d87a31ea7e4ec4d68b469821df0c1e75b6cdf5bc243a864d1228"},"descriptor_revision":"synthetic-reference-1","evidence_refs":[{"id":"synthetic-synthetic-stream","kind":"fixture","path":"fixtures/synthetic-stream.jsonl","sha256":"825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"}],"model_schema_ref":{"id":"synthetic-model-schema","kind":"fixture","path":"fixtures/model.schema.json","sha256":"fe1f59dd018fdfeb93eb4f75a67de1dc2dc4c3256d78f1000839109d7399824f"},"options_schema_ref":{"id":"synthetic-options-schema","kind":"fixture","path":"fixtures/options.schema.json","sha256":"d0157ce84790721854763c7480015e84f7cb0c7fde53afb21c3a42908805bf35"},"origin":"synthetic","parser_ref":{"id":"synthetic-parser","kind":"parser","path":"adapter_reference.py","sha256":"579f10c43030a0b152205170d7730008746247d4737bc56a4e0099d46aa96ecd"},"platforms":["linux"],"protocols":[{"allow_final_unterminated":false,"mode":"jsonl_one_shot","protocol_id":"synthetic-settled-v1","session_header_version":3,"source_refs":[{"id":"synthetic-synthetic-stream","kind":"fixture","path":"fixtures/synthetic-stream.jsonl","sha256":"825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"}],"terminal_rule":"settled_and_eof"}],"record_type":"adapter-descriptor","schema_version":1,"support_status":"synthetic_only"}
```

#### B.5.8 `fixtures/environment.json`

<!-- file: fixtures/environment.json -->
```json
{"origin":"synthetic","verified_for_real_execution":false}
```

#### B.5.9 `fixtures/invocation.json`

<!-- file: fixtures/invocation.json -->
```json
{"adapter_id":"synthetic-only","argv_tail":["--mode","json","--provider","fixture-provider","--model","fixture-model"],"capture":{"lossless_required":true,"stderr_format":"bytes","stdout_format":"jsonl"},"config_ref":{"id":"synthetic-config","kind":"fixture","path":"fixtures/config.json","sha256":"c12ebc318a892fd9c7c80513fbf65226eb107c54f4baac68037d593270fcec88"},"cwd_ref":{"id":"synthetic-cwd","kind":"fixture","path":"fixtures/cwd.json","sha256":"bf3883174222e387cd1eee70bfdc1ba6bf58c063f918cae807341b3f2e9f73e8"},"descriptor_ref":{"id":"synthetic-descriptor","kind":"adapter-descriptor","path":"fixtures/descriptor.json","sha256":"c98e6acf0718647e2432c380ed63b94a247e339b3cce0a058706ebf4fa2129a0"},"env_names":[],"environment_ref":{"id":"synthetic-environment","kind":"fixture","path":"fixtures/environment.json","sha256":"fbb64b67472bd695f2a798ae88d71afc46ac1193ab91df68813ea2e682c8076a"},"executable_binding_ref":{"id":"synthetic-environment","kind":"fixture","path":"fixtures/environment.json","sha256":"fbb64b67472bd695f2a798ae88d71afc46ac1193ab91df68813ea2e682c8076a"},"execution_mode":"exec_only","input":{"body_ref":{"id":"synthetic-request","kind":"fixture","path":"fixtures/request.txt","sha256":"d94eb23c85f478d794ddca34db7cb1b5022737f1cbfd0ce2a7dc18e51f13ccc7"},"mode":"stdin_utf8"},"limits":{"max_capture_bytes":4194304,"max_record_bytes":65536,"timeout_ms":10000},"origin":"synthetic","protocol_id":"synthetic-settled-v1","record_type":"invocation","request_id":"req-synthetic-01","resource_policy_ref":{"id":"synthetic-resource-policy","kind":"fixture","path":"fixtures/resource-policy.json","sha256":"65e017577f8cba1b94374bcc24066336ce843ebd802cdd73240cfa73d5b8193e"},"role":"executor","run_id":"run-synthetic-01","schema_version":1,"session_policy":"fresh_only","task_id":"task-synthetic-01"}
```

#### B.5.10 `fixtures/model.schema.json`

<!-- file: fixtures/model.schema.json -->
```json
{"additionalProperties":false,"properties":{"id":{"const":"fixture-model"},"provider":{"const":"fixture-provider"}},"required":["provider","id"],"type":"object"}
```

#### B.5.11 `fixtures/options.schema.json`

<!-- file: fixtures/options.schema.json -->
```json
{"additionalProperties":false,"properties":{},"required":[],"type":"object"}
```

#### B.5.12 `fixtures/probe-policy.json`

<!-- file: fixtures/probe-policy.json -->
```json
{"origin":"synthetic","real_process_authorized":false}
```

#### B.5.13 `fixtures/raw-manifest.json`

<!-- file: fixtures/raw-manifest.json -->
```json
{"origin":"synthetic","real_capture":false,"segments":[{"id":"synthetic-synthetic-stream","kind":"fixture","path":"fixtures/synthetic-stream.jsonl","sha256":"825c552d7009d4387c4841cbcd53938680d43543c302ee4a0c7ccdd0ea9e9ca9"}]}
```

#### B.5.14 `fixtures/report.txt`

<!-- file: fixtures/report.txt -->
```text
检查完成。
```

#### B.5.15 `fixtures/request.txt`

<!-- file: fixtures/request.txt -->
```text
Analyze only the synthetic fixture.
```

#### B.5.16 `fixtures/resource-policy.json`

<!-- file: fixtures/resource-policy.json -->
```json
{"native_resources":[],"origin":"synthetic"}
```

#### B.5.17 `fixtures/synthetic-stream.jsonl`

<!-- file: fixtures/synthetic-stream.jsonl -->
```jsonl
{"cwd":"/synthetic","id":"vendor-synthetic-01","timestamp":"2026-09-25T00:00:00Z","type":"session","version":3}
{"type":"agent_start"}
{"type":"turn_start"}
{"message":{"content":"仅分析测试夹具。","role":"user"},"type":"message_start"}
{"message":{"content":"仅分析测试夹具。","role":"user"},"type":"message_end"}
{"message":{"role":"assistant"},"type":"message_start"}
{"assistantMessageEvent":{"contentIndex":0,"delta":"检查","type":"text_delta"},"type":"message_update","usage":{"cacheRead":0,"cacheWrite":0,"input":50,"output":1,"totalTokens":51}}
{"message":{"content":[{"text":"检查完成。","type":"text"},{"arguments":{"path":"README.md"},"id":"call-01","name":"read","type":"toolCall"}],"model":"fixture-model","provider":"fixture-provider","role":"assistant","stopReason":"toolUse","usage":{"cacheRead":0,"cacheWrite":0,"input":100,"output":20,"totalTokens":120}},"type":"message_end"}
{"args":{"path":"README.md"},"toolCallId":"call-01","toolName":"read","type":"tool_execution_start"}
{"args":{"path":"README.md"},"partialResult":{"content":[]},"toolCallId":"call-01","toolName":"read","type":"tool_execution_update"}
{"isError":false,"result":{"content":[]},"toolCallId":"call-01","toolName":"read","type":"tool_execution_end"}
{"message":{"role":"toolResult"},"type":"message_start"}
{"message":{"content":[],"isError":false,"role":"toolResult","toolCallId":"call-01","toolName":"read"},"type":"message_end"}
{"message":{"content":[{"text":"检查完成。","type":"text"},{"arguments":{"path":"README.md"},"id":"call-01","name":"read","type":"toolCall"}],"model":"fixture-model","provider":"fixture-provider","role":"assistant","stopReason":"toolUse","usage":{"cacheRead":0,"cacheWrite":0,"input":100,"output":20,"totalTokens":120}},"toolResults":[{"content":[],"isError":false,"role":"toolResult","toolCallId":"call-01","toolName":"read"}],"type":"turn_end"}
{"type":"turn_start"}
{"message":{"role":"assistant"},"type":"message_start"}
{"message":{"content":[{"text":"检查完成。","type":"text"}],"model":"fixture-model","provider":"fixture-provider","role":"assistant","stopReason":"stop","usage":{"cacheRead":0,"cacheWrite":0,"input":100,"output":20,"totalTokens":120}},"type":"message_end"}
{"message":{"content":[{"text":"检查完成。","type":"text"}],"model":"fixture-model","provider":"fixture-provider","role":"assistant","stopReason":"stop","usage":{"cacheRead":0,"cacheWrite":0,"input":100,"output":20,"totalTokens":120}},"toolResults":[],"type":"turn_end"}
{"messages":[{"content":[{"text":"检查完成。","type":"text"},{"arguments":{"path":"README.md"},"id":"call-01","name":"read","type":"toolCall"}],"model":"fixture-model","provider":"fixture-provider","role":"assistant","stopReason":"toolUse","usage":{"cacheRead":0,"cacheWrite":0,"input":100,"output":20,"totalTokens":120}},{"content":[{"text":"检查完成。","type":"text"}],"model":"fixture-model","provider":"fixture-provider","role":"assistant","stopReason":"stop","usage":{"cacheRead":0,"cacheWrite":0,"input":100,"output":20,"totalTokens":120}}],"type":"agent_end","willRetry":false}
{"type":"agent_settled"}
```

### B.6 包说明与验证依赖记录

<!-- file: README.txt -->
```text
04 CLI 适配器与 Pi 迁移：离线合成参考验证包

范围：本文作者编写的 synthetic 协议与夹具，不是实际 Pi adapter 或真实 CLI 录制。
不启动 Pi，不访问网络，不解析凭据，不安装依赖，不修改项目或 .agent 原始证据。

运行（在本包目录；解释器名称按本机环境选择）：
  python -m unittest test_reference -v
  python verify.py

第一项只使用 Python 标准库。第二项还要求运行环境已有 jsonschema；本次实测版本为
4.26.0。requirements-schema.txt 是复核版本记录，不意味着自动安装已获授权。
verify.py 会在本包目录写 verification-report.json，不会清理其他文件。
不要使用 python -O 执行本参考验证；assert 断言参与检查。

从 Markdown 还原：
  python extract_reference.py 04-cli-adapters-and-pi-审阅增强版.md NEW_DIRECTORY
输出目录必须尚不存在；脚本仅提取具名代码块，不执行它们。提取失败时保留现场，
不要把任意用户目录当作可删除测试目录。随后在 NEW_DIRECTORY 执行上述离线检查。

adapter_reference.py 的能力、Invocation 与 Result 是教学子集：
- 调用记录标记 execution_allowed=false，不含可执行入口。
- 协议 ID synthetic-settled-v1 / synthetic-legacy-v1 不是 Pi 发布版本。
- 工具状态机只覆盖夹具中的顺序协议；未完整实现参数/结果语义对照或并发工具。
- usage 示例使用十进制字符串，不是 Pi 真实 cost 字段编码。
- 测试的上下文/transport/环境/时间来自合成输入，不是真实进程事实。
- frame_jsonl 为有上限的小样本收集器，不是生产排空管道或证据落盘模块。
- 已脱敏字节由调用者提供；没有实现生产脱敏、沙箱、鉴权、准入、预算或候选门禁。
- capability_gaps 消费受信清单；source_kind/status 字符串不构成证据认证。
- Schema 只检查本建议五类记录的结构；实际字段归属、资源种类、权限和锁需要域校验。

NOT_RUN：原项目回归、真实 Pi/probe/模型调用、03/05/07 接线、Windows/macOS 实际进程、
G0–G6、生产脱敏、账本与断电恢复。离线测试通过不能替代上述验收。
```

<!-- file: requirements-schema.txt -->
```text
jsonschema==4.26.0
```


## 附录 C：本次验证记录与结果边界

### C.1 验证对象与运行方式

验证对象是**从最终 Markdown 具名代码块重新提取的 26 个文件**，而不是另一个未交付的工作副本。提取后逐文件比较字节，再在独立目录运行 `python verify.py`。没有启动 Pi、访问凭据、发起真实模型请求或执行原项目测试。

本次环境：Linux、Python **3.13.5**、`jsonschema` **4.26.0**。这些是本次离线环境事实，不是宣布 Windows/macOS 或其他 Python 版本已经兼容；需要的平台矩阵仍见第 7.3 节。

| 检查 | 本次结果 | 解释 |
|---|---|---|
| 具名文件提取 | 26/26，字节一致 | 包括源码、样例、Schema 与全部引用 fixture |
| 单元测试 | **95/95 通过**；0 failure / 0 error | 不是生产验收用例数量；子测试不另充通过数 |
| Schema 元验证 | 1 份根 Schema，通过 | 根 Schema 包含五家族及公共 `$defs` |
| 合成结构正例 | 5/5 通过 | 每个记录家族一个；结构通过不表示可执行 |
| Schema 反例 | 14/14 按预期拒绝 | 缺类型、未知批准字段、模型调用/ shell 越界、无证据 verified、冒写 gate verdict |
| 固定序列化黄金向量 | 10/10 通过 | 同时核对独立预期字节和 SHA-256；不只比较两次同函数输出 |
| 去重资源 SHA-256 | 15/15 通过 | 相对路径定位并核验实际完整文件字节 |
| 样例内 ResourceRef 出现位置 | 21/21 通过 | 与“15 个去重资源”是不同统计口径 |
| adapter 模块导入 AST 检查 | 通过 | 未出现检查清单中的进程/网络导入；不是静态安全证明 |
| 原项目/真实平台验收 | **NOT_RUN** | 不包含在上述通过数内 |

验证脚本执行会产出机器可读 `verification-report.json`；交付的外层验证报告另记录文档/来源摘要、提取结果及未运行项。**文档不内嵌自身哈希**，避免自指依赖；外层报告计算的是封存后的最终文档字节。

### C.2 测试覆盖主题

覆盖主题包括：完整和缺失终止、旧/新 synthetic 方言显式分派、每一个字节切分位置的分片不变性、UTF-8 单字节输入、Unicode 分隔符、CRLF、无末尾 LF、重复 JSON 键、非法编码/嵌套/数字、未知字段和事件、厂商伪造本地上下文、报告不取 delta、请求/CLI/provider 字段不回填、前一轮模型失配、工具缺口和非法工具 ID、镜像用量去重、相同内容的不同 cycle、重放观察键稳定、费用未知/零估算、取消/超时、必要能力与固定参数限制。

每个测试的实际断言以 `test_reference.py` 为准。单元测试名称或数量不证明没有其他缺陷；例如顺序工具规则通过不表示完整并发工具支持。第 4 章定义的生产要求中，参考实现未覆盖的内容必须继续补齐，不可因已有演示而豁免。

### C.3 明确未运行项

原项目 `python tests/test_dispatch.py`、真实 Pi 版本/帮助/模型目录/认证 probe、真实 JSONL 对照、03 审核准入接线、05 进程执行与 Windows/macOS、07 预算账本、生产脱敏与证据存储、G0–G6、受保护源文件迁移、断电/崩溃恢复及 11 第二真实 CLI：**全部 NOT_RUN**。

也没有测量延迟、吞吐、峰值内存、节省比例或真实费用；因此本文不提供未经测量的性能提升或预算节约结论。

## 附录 D：原文追溯与实施复核入口

### D.1 原文章节到增强内容的映射

| 原文位置 | 保留的主旨 | 主要增强位置 |
|---|---|---|
| 第 1 章 | Pi 专有职责迁出核心，保留门禁，不代替进程层 | 1.2–1.4：缺口、不变量、信任边界 |
| 第 2 章 | 01/02 依赖，统一输入输出与 03/05 边界 | 2.1–2.3：对象所有权与准入数据流 |
| 第 3 章 | 六类现有复用点，仅按原文记载 | 3.1–3.3：迁移取证表、上游差异、未取证材料 |
| 4.1 | 五个适配器逻辑接口 | 4.1.1–4.1.9：类型、版本、注册、probe、Invocation、错误 |
| 4.2 | 能力与角色匹配，证据等级，readonly 边界 | 4.2.1–4.2.6：能力匹配、逐响应身份、思考、工具 |
| 4.3 | 原流/标准流、异常、身份、用量 | 4.3.1–4.3.10：封存、分帧、状态机、工具、重放与结果 |
| 4.4 | Pi 对照、v1/v2 路由、资源禁用、真实调用授权 | 4.4.1–4.4.6：协议差异、完整资源面、差分与样本来源 |
| 第 5 章 | 预计代码范围，保护路径仍有效 | 5.1–5.3：建议文件与交付物 |
| 第 6 章 | 分步迁移，不先重写所有行为 | 6.1–6.4：阶段退出条件、基线模板与切换 |
| 第 7 章 | 原 A01–A09 验收编号及要求 | 7.1 保留；7.2 建议 A10–A26；7.3 真实 F01–F20 |
| 第 8 章 | 历史证据不改、失败留证、新 run 重试、不能降级绕过 | 8.2–8.4：故障窗口、恢复步骤与升级路径 |
| 第 9 章 | 三个小包，合同仍需独立授权/预算 | 9.1–9.3：11 交接清单与未决项 |

### D.2 与前面增强版的对齐

01 的完整字节 SHA-256、非 JCS 的固定 Python 序列化、半开区间证据引用和 seq 所有权继续有效。02 的“入口定位与固定参数分离”“只消费锁定有效配置”“v1 变量不改写 v2”继续有效。03 的审批/业务授权分离、exact 环境锁、固定字节消费、单次准入和 START_UNKNOWN 继续有效。[^P01E][^P02E][^P03E]

本版没有修改前面三份增强版，也不将其新增建议追认为已部署事实。新增记录类型、资源 kind、事件 payload 和错误码在正式接线时须统一登记；存在跨模块重复定义时以明确版本的公共契约为准，不能由哪个模块最后导入来决定。

### D.3 本次输入文件指纹

以下摘要仅用于定位本次阅读的文件版本，不能证明原作者、真实性或任何运行结果。摘要按当前上传文件的完整字节计算；文件名相同但摘要不同视为不同输入。

| 输入文件 | 完整字节 SHA-256 |
|---|---|
| `01-baseline-and-protocols.md` | `49b880b87c037b00f73de0214c6859d60f268fb10d569f7186d1dae7834b18c2` |
| `01-baseline-and-protocols-审阅增强版.md` | `c5fca7b163d6e8d42f0c79dc0b5d6755c508ec832e8a87f514a294c8a300f42d` |
| `02-config-and-profiles.md` | `9a31440b7002ae0d1acbcb0eff6c1e4b9fd53e380571df5ac52f4e7ee9a0da86` |
| `02-config-and-profiles-审阅增强版.md` | `1cc5adb72e2e6def13897ac259c62717aa39915cffce4be2f1d2136eb346bd1d` |
| `03-approvals-and-locks.md` | `64b6c8cd1bd9fb7a08912b4505c54cd4babd2d7bd3ac4d7b635612499498733e` |
| `03-approvals-and-locks-审阅增强版.md` | `5727ab30e73962fd8a0dfefe3a250885624657bdc91423b0210bbc0b5b26cae2` |
| `04-cli-adapters-and-pi.md` | `0356ca997961c1cb6e78a23e0eb545261fb3634c2848796068e47051deef4320` |

## 附录 E：来源、核验日期与使用限制

### E.1 来源分级

| 来源类别 | 本文如何使用 | 不支持的结论 |
|---|---|---|
| 用户 04 原文 | 保留目标、边界、组织、原验收编号与现状记载 | 不能证明现有函数实际源码或回归结果 |
| 用户 01–03 原文及增强版 | 对齐公共类型、配置、环境锁、审核与准入边界 | 不能证明这些建议已在项目落地 |
| 上游官方文档与源码 | 提醒当前公开接口差异，支撑新增设计注意事项 | 不能证明本机版本、具体 semver 支持范围或真实运行结果 |
| 本文新增算法/Schema/测试 | 明确为建议及离线合成参考，提供可复算字节 | 不能替代真实 Pi 捕获、主机隔离、业务授权与项目验收 |

网页核验日期为 **2026-09-25**。Pi 的链接指向上游 `main` 可变页面；本次**没有取得对应固定 commit ID，也没有封存上游完整内容**。需要可重放的正式支持证明时，应在实施基线中固定实际 commit/package/build、内容摘要与脱敏真实 fixture，不能把本文网页链接当成不可变 ResourceRef。

下列来源说明是简要定位，不是对整篇上游文档的复制。环境字段、选项、事件和类型以实际已锁定版本为准；任何“当前页面存在”都不意味着用户旧版本支持，也不授权升级软件。

### E.2 用户材料

[^P04]: 用户上传 `04-cli-adapters-and-pi.md`，原文第 1–9 章；本版保留 4.1–4.4 组织及 `04-A01`–`04-A09`。
[^P01]: 用户上传 `01-baseline-and-protocols.md`，尤其 4.1–4.4 的 ID、完整字节哈希、标准事件与预算边界。
[^P01E]: 本会话提供 `01-baseline-and-protocols-审阅增强版.md`，用于核对 ResourceRef、严格 JSON、固定序列化、脱敏后字节坐标及 seq 所有权；仍按建议文档对待。
[^P02E]: 本会话提供 `02-config-and-profiles-审阅增强版.md`，用于核对 adapter 描述符与 Schema 注入、固定参数语义锁、配置依赖范围及环境分离；不表示这些模块已经实现。
[^P03E]: 本会话提供 `03-approvals-and-locks-审阅增强版.md`，用于核对 exact 环境锁、当前授权、单次准入、generation 与 START_UNKNOWN；不作为真实进程启动证据。

### E.3 官方技术资料

[^W01]: Pi 官方仓库入口。原 `badlogic/pi-mono` 页面本次重定向到 `earendil-works/pi`；用于找到官方资料，不用于改变用户软件安装或声明最新版。地址：`https://github.com/earendil-works/pi`。
[^W02]: Pi 官方 JSON Event Stream 文档，定位 LF 分帧、agent cycle 与 session settled 的区别，以及完成消息/工具事件的接口说明。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/json.md`。
[^W03]: Pi 官方 Command Line 文档，定位已公开的命令选项、模式、模型匹配、thinking 调整、显式资源和帮助/认证命令副作用。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/cli.md`。
[^W04]: Pi 官方 Shared Message Types 文档，定位 assistant 模型字段、可选提供商元数据、token 包含关系与工具结果中的用量表示。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/message-types.md`。
[^W05]: Pi 官方 Configuration 文档，定位 agent/project 配置、资源、上下文文件和信任作用范围。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/configuration.md`。
[^W06]: Pi 官方 Provider Authentication 文档，定位认证来源、`auth.json`、凭据配置及命令解析的边界。本文不执行其示例。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/providers.md`。
[^W07]: Pi 官方 Environment Variables 文档，定位进程环境对目录、网络及运行行为的影响；实际允许变量仍由项目规则决定。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/environment-variables.md`。
[^W08]: Pi 官方 `print-mode.ts` 源码，定位输出模式/错误处理流程。本次阅读的是可变 main，不用于宣称用户安装版本的退出码已实测。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/src/modes/print-mode.ts`。
[^W09]: Pi 官方 Security 文档，定位 project trust 与外围隔离的保证边界。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/security.md`。
[^W10]: Python 官方 subprocess 文档，定位管道排空、缓冲、超时及 Windows batch/shell 的安全注意事项；不是对 05 的实现测试。地址：`https://docs.python.org/3/library/subprocess.html`。
[^W11]: Pi 官方 agent 类型定义，用于理解 agent/message/tool 事件的字段和生命周期，实际映射仍须版本化。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/agent/src/types.ts`。
[^W12]: Pi 官方 `json-event.ts` 源码，定位流式事件对外表示与 SDK 累计对象的区别。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/src/modes/json-event.ts`。
[^W13]: Python 官方 json 文档，定位默认解析器对重复名称、非有限值等行为；本版严格入口另行拒绝。地址：`https://docs.python.org/3/library/json.html`。
[^W14]: Pi 官方 AI 类型定义，定位 stopReason、模型响应与用量类型。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/ai/src/types.ts`。
[^W15]: Pi 官方 Settings 文档，定位模型、重试、压缩与缓存维护等设置。本文不宣称这些设置已在用户环境启用或关闭。地址：`https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/settings.md`。
[^W16]: JSON Schema Draft 2020-12 Validation 官方规范，定位结构校验与 format 处理。地址：`https://json-schema.org/draft/2020-12/json-schema-validation`。

---

**签收提醒**：本文交付的是“可进入正式实施审定的模块说明 + 可复算的离线合成参考”。真实迁移是否完成，应由实际基线、真实环境、真实协议样本、原回归与 `04-A01`–`04-A26` 中获准采用的检查共同证明；不得从文档篇幅、测试数量或上游链接反推生产就绪。
