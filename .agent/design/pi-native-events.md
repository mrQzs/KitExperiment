# 设计说明：Pi 原生事件转换（离线，录得证据）

状态：方向已由用户选定（2026-10-08，三个候选中选 1a，“全按你的推荐来”）；第 5 节的语义已由用户决定（2026-10-09，均选推荐项），第 6 节是规划端的决定；按第 4 节拆分合同。
前置：adapter 接入监督运行（`.agent/design/adapter-supervisor-wiring.md`）与输出流信任边界（`.agent/design/stream-trust-boundary.md`）都已集成。

## 1. 为什么做

- IMPLEMENTATION_STATUS 把“把厂商原生事件翻译成规范化 adapter 协议”列为 NOT_RUN。受监督的 adapter 路径（`adapter_run.py`）只接受规范化事件 JSONL；真实 Pi 的 `--mode json` 输出不是这种格式，所以真实 Pi 至今没有接到调度器的用量结算，adapter-supervisor-wiring 第 4 节第 3 条留下的正是这一项。
- 本机现在是 Windows，没有监督器，真实的受监督运行做不了；但翻译层本身只是解析，不需要进程、凭据或网络，Windows 可做。
- 调度器 pi_dispatch 把 Pi 的 stdout 原样写进 `.agent/runs/<task>/attempt-NNN/events.jsonl`（`run` 以 `xb` 打开它作为子进程的 stdout）。本机有 30 次真实 Step 运行的事件流：pi 1.0.1（旧 Windows 账户）、1.0.2 与 1.0.4（macOS）。这是不调用 provider 就能拿到的录得证据（captured），按 AGENTS.md 与合成、离线、原生证据分开记。

## 2. 现状

- 规范化协议 `sentinelmesh/adapter_protocol.py`（Code-Kit adapter 边界的逐字移植，adapter-protocol-port）：
  - 事件词表 `run_started`、`assistant`、`tool_started`、`tool_finished`、`usage`、`run_finished`、`error`；事件是 `{"seq", "event_id", "type", "source", "payload"}` 五个键，seq 从 0 连续递增，event_id 不重复，source 必须等于 adapter_id，payload 是有界（1 MiB、深度 64）、无环、有限的 JSON 对象；
  - `OfflineAdapter.run(invocation, events)` 的结算规则：`run_started` 唯一且在最前；终止事件（`run_finished` 或 `error`）唯一且在最后；`tool_started` / `tool_finished` 按 `tool_id` 配对，终止时不许有未闭合的工具；词表之外的类型保留为 `unknown`，一出现就不结算（`settled=False`、status `UNKNOWN`、usage `{"status": "unknown"}`）；用量取最后一条 `usage` 事件的 payload；终止是 `error` 时 settled、status `ERROR`、`raw_complete=True`。
- `adapter_run.read_stream` 把证据目录的 `stdout.log` 逐行用 `strict_json_loads` 解析（单行上限 1 MiB、深度 64），整流上限默认 1 MiB、最大 16 MiB，交给 `adapter.run(invocation, events)`；用量只在 `settled` 且 `raw_complete` 时记入调度器，而且只记 payload 里与预约维度同名、值为精确非负 int 的项。
- 真实 Pi 流的事实（30 次运行全部核对）：
  - 每行一个 JSON 对象，没有解析失败的行；第一行是 `{"type": "session", "version": 3, "id", "timestamp", "cwd"}`，30 次的 version 都是 3；
  - 帧类型 12 种：`session`、`agent_start`、`turn_start`、`message_start`、`message_update`、`message_end`、`tool_execution_start`、`tool_execution_update`、`tool_execution_end`、`turn_end`、`agent_end`、`agent_settled`；
  - assistant 的 `message_end` 带 `usage {input, output, cacheRead, cacheWrite, totalTokens, cost{…}}`、`stopReason`（实测只有 `toolUse` 1243 次与 `stop` 28 次）、`provider`、`model`、`responseId`、`rawStopReason`、`thinkingLevel`、`content`（text / thinking / toolCall 块）；
  - `message_update` 带同一条消息的累计 usage 快照（1271 条消息全部单调不减，且不超过 `message_end` 的值）；`turn_end` 与 `agent_end` 带消息镜像（含 usage）；toolResult 消息没有 usage；
  - `tool_execution_start` / `tool_execution_end` 按 `toolCallId` 严格 1:1 配对（30/30），每个 id 都在前一条 assistant 消息的 toolCall 里声明过，每个都有对应的 toolResult 消息；`tool_execution_end` 带 `isError`；
  - 28 次以 `agent_settled` 结束（`agent_end` 的 `willRetry` 全是 false）；2 次被时限截断：一次停在 `message_update` 中间，一次停在 `turn_start` 之后；
  - 没见过：stopReason 为 `error` / `aborted` / `length`、`willRetry` 为 true、compaction、retry、queue、`entry_appended` 等帧（pi_dispatch 的汇总器知道 `compaction_end` 与 `entry_appended` 可以带 usage）；
  - 单行最长 719 KB（`agent_end` 的镜像随对话增长），整流 2.1–9.2 MB，都在协议的单行上限与 `read_stream` 的最大整流上限之内；
  - pi_dispatch 的 `summarize_events` 独立地把 assistant `message_end` 的 usage 累加成 `events-summary.json` 的 `usage`（它还会加 toolResult、`entry_appended`、`compaction_end` 的 usage，本批数据里没有）。这是另一个作者、另一份代码算出的数，用作本合同 oracle 的独立对照；
  - 本机 pi 0.99.2 的 smoke 通过，但 smoke 不留事件流；第一次派遣会证实它也以 `agent_settled` 结束（check-pi-env 记 pi ≥ 0.80.4 都有）。
- Code-Kit 04 文档第 4.3 节给了规则：映射注册表（4.3.3）、生命周期与终止（4.3.4：低层 `agent_end` 与会话级 `agent_settled` 不同）、工具审计（4.3.5：按调用 id 配对，不用“最近打开的工具”）、用量（4.3.7：完成消息是最终观察，镜像不重复累计，未知不补零，CLI 估算的费用不是账单）、未知记录保留且使完整性不足。附录 B 的参考实现只针对两个合成方言，不是 Pi。

## 3. 方案

### 3.1 模块与接口

新模块 `sentinelmesh/pi_events.py`，导入无副作用：

- `DIALECT = "pi-session-v3"`。
- `translate(frames) -> PiTranslation`：输入是已解析的帧（每行一个 dict，正是 `read_stream` 返回的 `events`，也是 `OfflineAdapter.run` 的第二个参数）；输出规范化事件（dict 列表，可直接交给 `normalize_events` 与 `OfflineAdapter.run`）与翻译事实。翻译器对流的内容从不抛出：帧不是 dict、类型未知、形状不对，都变成一个未知事件；只有 `frames` 不可迭代才报 `AdapterError("E_ADAPTER_EVENT")`，与参考的 `normalize_events` 相同。
- `PiTranslation`（frozen dataclass）：`events`（元组）、`dialect`、`frames`（帧数）、`ignored`（没有产生事件的已知帧按类型计数）、`unknown`（未知或畸形帧数）、`usage`（五个 token 的累计，dict）、`usage_complete`（布尔）、`assistant_messages`、`tool_calls`、`tool_errors`、`agent_cycles`、`terminal`（`"agent_settled"` 或 None）、`last_stop_reason`、`models`（`(provider, model)` 去重排序的元组）。
- `PiNativeAdapter(OfflineAdapter)`：`adapter_id = "pi"`、`protocol_version = "pi-session-v3"`；`translate(frames)`；`run(invocation, frames)` 先翻译，再用参考归约器（`OfflineAdapter.run` 的规则，`expected_source` 为 `"pi"`）结算；`probe()` 在父类的基础上加 `dialect`。这样 `run_adapter_operation(scheduler, PiNativeAdapter(), …)` 不改一行就能消费真实 Pi 的 stdout。

### 3.2 映射表

| 帧 | 规范化事件 | payload |
|---|---|---|
| `session`（必须是第一帧，`version` 是精确 int 3） | `run_started` | `dialect`、`session_id`、`session_version`、`timestamp` |
| `message_end`，role `assistant` | `assistant`，紧接一条 `usage` | assistant：`stop_reason`、`provider`、`model`、`response_id`、`thinking_level`、`text_blocks`、`thinking_blocks`、`tool_calls`（toolCall 的 id 列表）、`usage`（这条消息的五个整数，合法时才有）；usage：累计 `input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`、`total_tokens`（见 3.3）、`assistant_messages`、`tool_calls`、`complete` |
| `tool_execution_start` | `tool_started` | `tool_id`（toolCallId）、`tool`（toolName） |
| `tool_execution_end` | `tool_finished` | `tool_id`、`tool`、`is_error` |
| `agent_settled` | `run_finished` 或 `error`（见 3.4） | `stop_reason`、`assistant_messages`、`tool_calls`、`agent_cycles` |
| `agent_start`、`agent_end`、`turn_start`、`turn_end`、`message_start`、`message_update`、`tool_execution_update`、role 为 `system` / `user` / `toolResult` 的 `message_end` | 不产生事件，只计数 | — |
| 其他类型；不是 dict 的帧；已知类型但形状不对（见 N10–N13） | `pi.unknown_frame` / `pi.invalid_frame`（归约时成为 `unknown`） | `frame_type`、`line`、`reason`、`sha256`（帧的规范 JSON 摘要）、`bytes` |

- 事件 id：`L<行号>`；一帧产生两个事件时加后缀 `.assistant` 与 `.usage`。seq 按产出顺序从 0 起，source 一律 `"pi"`。
- 文本内容（系统提示、文件内容、助手正文、thinking）与路径（`cwd`）都不进事件：事件只记结构事实。

### 3.3 用量

- 只累加 assistant `message_end` 的 usage 的五个整数；`message_update` 的快照、`turn_end` / `agent_end` 的镜像不计；toolResult 没有 usage。
- 某条 assistant 消息没有 usage，或五个值中任一个不是精确的非负安全整数（bool、float、负数、超过 2**53-1、缺键都算）：整个翻译 `usage_complete=False`。此后每条 `usage` 事件都不含五个 token 键，只含计数与 `complete: false`，于是 `run_adapter_operation` 什么都记不到，request 停在用量未知。未知不补零。
- 费用不作维度：Step 回报的 cost 全是 0，是 CLI 的估算不是账单（04 第 4.3.7 节）。

### 3.4 终止

- 只有 `agent_settled` 是终止。它之后再出现任何帧，都翻译成未知事件：终止就不再是最后一个事件，归约器据此拒绝整条流（`E_ADAPTER_SETTLEMENT`）。
- `agent_settled` 时，最后一条 assistant 消息的 stopReason 是 `stop` → `run_finished`；否则（`error`、`aborted`、`length`、`toolUse`，或根本没有 assistant 消息）→ `error`，payload 的 `stop_reason` 写明原因。两种都是结算完成，用量照记。
- `agent_end` 只计数（`agent_cycles`）并要求 `willRetry` 是布尔值；`willRetry` 为 true 之后的新周期照常累加用量。
- 没有 `agent_settled` 的流（被截断、只有 `agent_end`）没有终止事件：不结算，用量未知；`PiTranslation.usage` 仍给出到截断为止的累计，供运维参考（候选 2 的参考值就从这里来）。

### 3.5 上限与读取

- 单行 1 MiB 与深度 64 是协议的；整流上限由 `read_stream` 的调用方给，Pi 路径应取 `MAX_STREAM_BYTES`（16 MiB）。实测最大整流 9.2 MB、最长一行 719 KB。超过上限的流是 `too_large` / `invalid`，用量未知，与现在一样失败关闭。本合同不改 `read_stream` 与 `adapter_run`。

### 3.6 夹具与 oracle

- `tests/fixtures/pi_native/reduce.py`：规划端工具，从一个 attempt 目录生成脱敏缩减流 `<名>.jsonl` 与 `<名>.meta.json`。规则：
  - 保留每一帧与全部结构字段；流式更新帧例外：每条 assistant 消息的 `message_update`、每次工具调用的 `tool_execution_update` 只保留第一条与最后一条，其余省略并计数（这些帧只有文本增量、部分结果与累计 usage 快照，翻译器不读）；
  - 超过 64 个字符的字符串值截成前 48 个字符加 `…[reduced len=N sha256=前 12 位]`；数字、布尔、null、键名与嵌套结构不变；
  - meta 记来源（attempt、pi 版本、平台、工具版本、原文件 sha256、行数与字节数、省略条数）与调度器的独立汇总（`usage`、`assistant_messages`、`tool_calls`、`tool_errors`、`final_stop_reason`、`terminal_event`、`complete`、`event_counts`、`actual_models`）。
- 5 份夹具：三个 pi 版本各一份完整流（都含工具错误），两份被截断的流。每份不超过 400 KB（原拟 300 KB；Windows 上 pi 1.0.1 的三次完整运行都更长，缩减后的体积主要是结构而不是文本，最大的一份 356 KiB，五份合计约 1.1 MB）。
- 夹具里保留了约 30 行含本机路径（旧 macOS 与旧 Windows 账户的用户名）的字符串前缀；不含凭据。
- oracle `tests/test_pi_events_oracle.py`：对每份夹具核对翻译事实与 meta 里调度器的数（用量相等、消息数相等、工具调用数相等、工具错误数相等、模型集合相等、终止与 complete 一致）；三份完整流经 `PiNativeAdapter.run` 结算为 `SUCCEEDED`、usage 等于调度器的数且 `complete` 为真；两份截断流不结算、usage unknown、翻译的累计等于调度器的数；另有合成反例（标明合成）：未知帧、第二个 session 头、`agent_settled` 之后还有帧、五种不合法的 usage 值、工具配对违规、非 `stop` 的终止、`willRetry` 为 true 的重试周期、空流、第一帧不是 session、不是 dict 的帧、`adapter_id` 不符、导入无副作用、确定性。
- 规划端专用 `tests/test_pi_events_captured.py`：仓库里有 `.agent/runs` 时，对全部 30 次完整流复核同样的事实，并断言 translate(完整流) == translate(缩减流)（证明缩减没有改变任何结构事实）；没有 `.agent/runs` 时跳过。执行器的 worktree 看不到 `.agent/runs`，这份测试在那里只会跳过，它是规划端的证据，不进 DoD。

## 4. 合同拆分

一份合同 `pi-native-events`：新建 `sentinelmesh/pi_events.py` 与执行器测试 `tests/test_pi_events.py`。oracle、夹具与缩减脚本由规划端先提交进基线并列入 forbidden。

之后另议：
- 真实 Pi 的受监督运行（候选 1b）：需要 macOS 上的监督器与凭据批准；本合同之后它只差 `adapter=PiNativeAdapter()` 与 `max_stream_bytes=MAX_STREAM_BYTES`；
- 崩溃运行的参考用量（候选 2）：`PiNativeAdapter.translate(frames).usage` 就是参考值，再由 `smctl ledger status` 展示。

## 5. 用户决定（2026-10-09，均选推荐项）

1. **未知帧失败关闭**：词表之外的帧类型、已知类型但形状不对的帧，都保留为未知事件，整条流不结算、用量未知，由运维补记；新版 pi 加了帧类型时另立方言版本。没有选“白名单之外的帧忽略”：`compaction_end` 这类帧带 usage，忽略就是少记。
2. **只有 `agent_settled` 算结束**：pi ≥ 0.80.4 都有，30 次完整流都以它结束；只有 `agent_end` 的流算未结束。没有选“`agent_end` 且 `willRetry` 为 false 也算结束”：04 第 4.3.4 节明确反对对新版只见 `agent_end` 就提前结束。
3. **只累加 assistant `message_end` 的五个整数**，与调度器的汇总器一致，有 30 次运行的对照；任一值不合法就整条流用量不完整，不记入账本；费用不作维度。没有选“截断的最后一条消息用 `message_update` 的快照补上”：快照不是最终观察，调度器也不这样算，没有对照。
4. **非 `stop` 的终止记为 `error`**：`agent_settled` 时最后一条 assistant 的 stopReason 不是 `stop`（`error`、`aborted`、`length`、`toolUse`，或没有 assistant 消息）→ 终止事件是 `error`：结算完成、status `ERROR`、用量照记，由复审决定候选是否可用。没有选“一律 `run_finished`”：会让一个报错结束的运行看起来和正常结束一样。

## 6. 规划端的决定（2026-10-09）

- N1：模块名 `pi_events`，方言名 `pi-session-v3`，adapter_id `pi`，protocol_version `pi-session-v3`。
- N2：事件 id `L<行号>`（两个事件时加 `.assistant` / `.usage`），seq 按产出顺序，source `pi`。
- N3：payload 只放结构事实：文本内容、thinking、工具参数与结果、`cwd` 都不进事件。
- N4：未知与畸形帧的 payload 只放 `frame_type`、`line`、`reason`、`sha256`、`bytes`，不放原帧，所以永远有界。
- N5：toolResult 消息不翻译也不核对；工具配对由归约器按 `tool_execution_start` / `tool_execution_end` 把守。
- N6：翻译器保持薄：不做配对、不做 seq 之外的校验，不重复参考归约器的规则；它负责的是“把 Pi 的帧如实变成词表里的事件”。
- N7：夹具缩减规则见 3.6；缩减流与完整流的翻译结果除事件 id（它带帧的行号，省略会改变行号）外必须相同，由规划端专用测试断言。
- N8：用量维度名用小写下划线：`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`、`total_tokens`；计数 `assistant_messages`、`tool_calls`。
- N9：`agent_cycles` 数 `agent_start`；`agent_end` 的 `willRetry` 不是布尔值 → 畸形帧。
- N10：`session` 必须是第一帧（第 1 行，不是“第一个产生事件的帧”）且 `version` 是精确 int 3，`id` 与 `timestamp` 是 str；其他版本、不在第 1 行或第二个 `session` → 畸形帧（方言版本化）。
- N11：`tool_execution_start` / `tool_execution_end` 的 `toolCallId` 与 `toolName` 必须是非空 str，`tool_execution_end` 的 `isError` 必须是布尔值；否则畸形帧。
- N12：assistant `message_end` 的 `message` 必须是 dict，`content` 必须是 list（块是 dict，按 `type` 计数，toolCall 块的 `id` 必须是非空 str），`stopReason`、`provider`、`model` 必须是 str；否则畸形帧。`responseId` 与 `thinkingLevel` 缺失时为 None。
- N13：`message_end` 的 role 不是 `system` / `user` / `assistant` / `toolResult` → 畸形帧。
- N14：空流、第一帧不是 `session`：不是畸形的“错”，而是没有 `run_started`，由归约器报 `E_ADAPTER_SETTLEMENT`；翻译器只如实产出。

以上决定都可以在后续合同里修改；改动时更新本节。

## 7. 不做

- 不调用真实 pi，不读取凭据，不联网；不做受监督的真实运行（1b）。
- 不改 `read_stream`、`adapter_run`、`adapter_protocol`、监督器与调度器。
- 不做 Windows 监督器。
- 不翻译没见过的帧（compaction、retry、queue、`entry_appended` 等）：按未知处理，留给下一个方言版本。
- 不计费用，不改 pi_dispatch。

## 8. 进度

- 2026-10-09：设计说明写成，待用户决定第 5 节。
