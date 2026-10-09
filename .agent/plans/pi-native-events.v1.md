# TASK: pi-native-events

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 396e0a9f8d4ff5b227a9c7a41567b9ee31100e97
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
把 Pi `--mode json` 的原生事件流（session v3 帧，每行一个 JSON 对象）翻译成规范化 adapter 协议的事件，并用现有的参考归约器结算：
- 新建 `sentinelmesh/pi_events.py`：`translate(frames) -> PiTranslation`、`PiNativeAdapter(OfflineAdapter)` 以及第 3 节列出的常量；
- 让规划端 oracle `tests/test_pi_events_oracle.py` 全部通过（它用 5 份真实录得的缩减流与调度器的独立汇总对照）；
- 新建执行器测试 `tests/test_pi_events.py`。

## Non-goals
- 不改 `sentinelmesh/adapter_protocol.py`（参考归约器逐字移植，规则原样复用，不重写配对、序号或终止规则）、`adapter_run.py`、`adapter.py`、`__init__.py`、监督器、调度器与 CLI。
- 不调用 pi，不读取 `.agent/runs`（执行器的工作副本里没有它；`tests/test_pi_events_captured.py` 在那里只会跳过，这是预期）。
- 不翻译没见过的帧（compaction、retry、queue、`entry_appended` 等）：它们按未知帧处理（用户决定），不做白名单。
- 不计费用，不从 `message_update` 快照补用量（用户决定）。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具、缩减脚本、规划端专用测试与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `396e0a9` = master `7dda4ef`（设计说明 `.agent/design/pi-native-events.md`）+ 规划端 oracle 提交，后者含：
  - `tests/test_pi_events_oracle.py`：26 个用例（三类：录得夹具、合成映射规则、失败关闭/终止/归约器/adapter 表面），在基线上收集失败，因为 `sentinelmesh.pi_events` 还不存在；
  - `tests/fixtures/pi_native/`：5 份缩减流 `<名>.jsonl` 与 `<名>.meta.json`（三份完整：pi 1.0.4 macOS、1.0.2 macOS、1.0.1 Windows；两份被时限截断）、缩减脚本 `reduce.py`（规划端工具，不要运行）；
  - `tests/test_pi_events_captured.py`：规划端专用，`.agent/runs` 不存在时跳过。
- 设计说明的第 3 节是方案，第 5 节是用户决定，第 6 节 N1–N14 是规划端决定；本合同的 Behavior contract 就是它们的合同写法，两处不一致以本合同为准并写进 `contract_conflicts`。
- 参考归约器 `sentinelmesh/adapter_protocol.py`：
  - `EVENT_TYPES = {run_started, assistant, tool_started, tool_finished, usage, run_finished, error}`；
  - `normalize_events(events, *, expected_source, preserve_unknown)` 要求每个事件是恰好五个键 `seq`、`event_id`、`type`、`source`、`payload` 的 dict，seq 从 0 连续，event_id 不重复且是非空无控制字符的 str，source 等于 `expected_source`，payload 是有界（1 MiB、深度 64）、无环、有限的 JSON 对象；`type` 不在词表里时（`preserve_unknown=True`）包成 `unknown`；
  - `OfflineAdapter.run(invocation, events)`：`invocation` 必须是 `Invocation` 且 `adapter_id` 为 None 或等于 `self.adapter_id`；`run_started` 唯一且在最前；终止事件（`run_finished`/`error`）唯一且在最后；`tool_started`/`tool_finished` 按 payload 的 `tool_id` 配对，终止时不许有未闭合的工具；出现 `unknown` 事件 → `AdapterResult(settled=False, status="UNKNOWN", usage={"status": "unknown"}, raw_complete=False)`；用量取最后一条 `usage` 事件的 payload；终止是 `error` → status `ERROR`、settled、raw_complete；没有 usage 事件时 usage 是 `{"status": "unknown"}`；
  - `OfflineAdapter.probe()` 返回 `{"adapter_id", "protocol_version", "offline": True, "process_started": False, "network_used": False}`，取自实例的类属性；
  - `AdapterError(code, message)`，`code` 属性。
- `sentinelmesh.protocols.MAX_SAFE_INTEGER = 2**53 - 1`。
- `adapter_run.run_adapter_operation` 只把 `AdapterResult.usage` 里与预约维度同名、值为精确非负 int 的项记入调度器：用量不完整时 payload 里不能有 token 键，这就是“不补零”的实现方式。
- Pi 帧的事实（30 次真实运行全部核对，夹具就是其中 5 次）：
  - 第一行 `{"type": "session", "version": 3, "id", "timestamp", "cwd"}`；
  - 帧类型 12 种：`session`、`agent_start`、`turn_start`、`message_start`、`message_update`、`message_end`、`tool_execution_start`、`tool_execution_update`、`tool_execution_end`、`turn_end`、`agent_end`、`agent_settled`；
  - `message_end` 的 `message.role` 是 `system`、`user`、`assistant` 或 `toolResult`；assistant 消息有 `content`（`text`、`thinking`、`toolCall` 块，toolCall 块有 `id`、`name`、`arguments`）、`usage {input, output, cacheRead, cacheWrite, totalTokens, cost}`、`stopReason`（见过 `toolUse`、`stop`）、`provider`、`model`、`responseId`、`rawStopReason`、`thinkingLevel`；
  - `message_update` 带同一条消息的累计 usage 快照；`turn_end` 与 `agent_end` 带消息镜像（含 usage）；toolResult 消息没有 usage；
  - `tool_execution_start` / `tool_execution_end` 有 `toolCallId`、`toolName`，后者还有 `isError`；
  - `agent_end` 有 `willRetry`（布尔）；完整的流以 `agent_settled` 结束。
- 夹具的 meta：`dispatcher_summary` 是调度器算出的 `usage`（`input`、`output`、`cacheRead`、`cacheWrite`、`totalTokens`）、`assistant_messages`、`tool_calls`（按工具名计数的 dict）、`tool_errors`、`final_stop_reason`、`terminal_event`、`complete`、`event_counts`、`actual_models`；`reduction.kept_lines` 是夹具的行数。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 26/26；30 次完整流的翻译与调度器一致；全量 577 passed；32 个故意制造的偏差全部被 oracle 抓住，其中包括：把快照或镜像的 usage 算进去、`agent_end` 当终止、未知帧忽略、畸形帧抛异常、接受 bool/float/负数/不安全整数/缺键的用量、非 `stop` 也记 `run_finished`、第二个 session、session 不在第 1 行、终止之后的帧忽略、用量不完整仍输出 token 键、两个事件共用 id、seq 从 1 起、source 写成 offline、累计改成每条重置、未知帧 payload 带原帧、接受 str 作为 frames、不核对 adapter_id、模型不去重、按第一条消息的 stopReason 判终止。
- 工具链：Windows Server 2025，`python` 3.12.10（系统安装，没有 venv）与 pytest 9.1.1；基线上 `python -m pytest -q` 因 oracle 收集失败而失败，其余 551 passed / 286 skipped（原生 POSIX 用例在 Windows 上跳过，这是预期）。

## Scope
- allowed_modified_files: 无
- allowed_new_files:
  - sentinelmesh/pi_events.py
  - tests/test_pi_events.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_pi_events_oracle.py`, `tests/test_pi_events_captured.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter.py`, `sentinelmesh/__init__.py`, `sentinelmesh/protocols.py`, `sentinelmesh/runtime.py`, `sentinelmesh/scheduler.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有“是 str / int / bool / dict / list”都是精确类型（`type(x) is str` 等；bool 不是 int）。

1. **模块与常量**：`sentinelmesh/pi_events.py` 导入无副作用（不导入 ctypes、不读文件、不读环境变量、不启动进程）。导出 `DIALECT = "pi-session-v3"`、`SOURCE = "pi"`、`SESSION_VERSION = 3`、`TERMINAL_FRAME = "agent_settled"`、`UNKNOWN_FRAME = "pi.unknown_frame"`、`INVALID_FRAME = "pi.invalid_frame"`、`USAGE_KEYS = (("input", "input_tokens"), ("output", "output_tokens"), ("cacheRead", "cache_read_tokens"), ("cacheWrite", "cache_write_tokens"), ("totalTokens", "total_tokens"))`、`PiTranslation`、`PiNativeAdapter`、`translate`，并列入 `__all__`。
2. **`translate(frames) -> PiTranslation`**：
   - `frames` 是 `str`、`bytes` 或 `bytearray`，或不可迭代：`AdapterError("E_ADAPTER_EVENT", …)`；其他任何可迭代对象（列表、元组、生成器）都接受，只迭代一次，第 1 个元素是第 1 行。
   - 对流的内容从不抛出：每一帧要么产生事件，要么被计数，要么变成一个未知/畸形事件（第 8–9 条）。
   - `PiTranslation` 是 frozen dataclass，字段依次为：`dialect`（`DIALECT`）、`events`（事件 dict 的元组，顺序即产出顺序）、`frames`（帧数）、`ignored`（dict：没有产生事件的已知帧按类型计数，非 assistant 的 `message_end` 统一计在 `"message_end"` 下）、`unknown`（未知与畸形帧的总数）、`usage`（dict：五个维度名 → 合法 assistant 消息的累计和，即使不完整也给出）、`usage_complete`（bool）、`assistant_messages`、`tool_calls`（`tool_execution_start` 的个数）、`tool_errors`（`isError` 为 True 的 `tool_execution_end` 个数）、`agent_cycles`（`agent_start` 的个数）、`terminal`（`"agent_settled"` 或 None）、`last_stop_reason`（最后一条合法 assistant 消息的 `stopReason`，没有时 None）、`models`（`(provider, model)` 二元组去重后排序的元组）。
   - 同一输入两次翻译结果相等；返回的事件是新对象，修改它们不影响下一次翻译。
3. **事件的形状**：每个事件是恰好五个键的 dict：`seq` 从 0 按产出顺序连续；`event_id` 为 `"L<行号>"`，一帧产生两个事件时分别是 `"L<行号>.assistant"` 与 `"L<行号>.usage"`；`type`；`source` 一律 `"pi"`；`payload` 是 dict。payload 只放结构事实：文本内容、thinking、工具参数与结果、`cwd` 都不进事件。
4. **映射**（帧类型 → 事件；未列出的字段不检查）：
   - `session`：必须在第 1 行、此前没有 `session`、`version` 是 int 且等于 3、`id` 与 `timestamp` 是 str → `run_started`，payload `{"dialect": DIALECT, "session_id": id, "session_version": 3, "timestamp": timestamp}`。
   - `message_end`：`message` 必须是 dict，`role` 必须是 str 且属于 `system`/`user`/`assistant`/`toolResult`；非 assistant 的只计入 `ignored["message_end"]`。assistant 的：`content` 必须是 list，每个块必须是 dict 且 `type` 是 str；按块的 `type` 数 `text` 与 `thinking`；`toolCall` 块的 `id` 必须是非空 str，依次收集；`stopReason`、`provider`、`model` 必须是 str；`responseId` 与 `thinkingLevel` 缺失或为 None 时记 None，存在时必须是 str。合法时先产生 `assistant`，payload `{"stop_reason", "provider", "model", "response_id", "thinking_level", "text_blocks", "thinking_blocks", "tool_calls": [toolCall 的 id 列表], "usage": 这条消息的五个维度 dict，或 None}`，紧接一条 `usage` 事件（第 6 条）。
   - `tool_execution_start`：`toolCallId` 与 `toolName` 必须是非空 str → `tool_started`，payload `{"tool_id": toolCallId, "tool": toolName}`。
   - `tool_execution_end`：同上，另加 `isError` 必须是 bool → `tool_finished`，payload `{"tool_id", "tool", "is_error"}`。
   - `agent_settled` → 终止事件（第 7 条）。
   - `agent_start`（`agent_cycles` 加一）、`agent_end`（`willRetry` 必须是 bool，否则畸形帧）、`turn_start`、`turn_end`、`message_start`、`message_update`、`tool_execution_update`：不产生事件，按类型计入 `ignored`。
   - 其他类型：未知帧（第 8 条）。
5. **工具配对不在翻译器里做**：翻译器如实产出 `tool_started`/`tool_finished`，配对、`run_started` 的位置与终止的唯一性由参考归约器把守（`PiNativeAdapter.run` 时由它报 `E_ADAPTER_TOOL`/`E_ADAPTER_SETTLEMENT`）。
6. **用量**：只累加 assistant `message_end` 的 usage：`usage` 必须是 dict，`USAGE_KEYS` 的五个 vendor 键都必须存在且每个值是 int、≥ 0、≤ `MAX_SAFE_INTEGER`；全部合法时这条消息的用量按维度名记入 `assistant` 事件的 `usage` 并加进累计；否则这条消息的 `usage` 记 None、累计不变，并且 `usage_complete` 从此为 False。`message_update` 的快照、`turn_end`/`agent_end` 的镜像、toolResult 都不计。每条 `usage` 事件的 payload 是 `{"assistant_messages": 到此为止的 assistant 消息数, "tool_calls": 到此为止的 tool_execution_start 数, "complete": 当时的 usage_complete}`，`complete` 为 True 时再加五个维度名 → 累计值；为 False 时没有任何 token 键。费用不作维度。
7. **终止**：只有 `agent_settled` 是终止。`terminal` 记 `"agent_settled"`；`last_stop_reason == "stop"` → `run_finished`，否则 → `error`；payload 都是 `{"stop_reason": last_stop_reason（可为 None）, "assistant_messages", "tool_calls", "agent_cycles"}`，`error` 再加 `"reason"`：没有 assistant 消息时 `"no_assistant_message"`，否则 `"stop_reason:<last_stop_reason>"`。`agent_settled` 之后的任何帧都是未知帧（reason `frame after agent_settled`），让终止不再是最后一个事件。没有 `agent_settled` 的流（被截断、只有 `agent_end`、`willRetry` 为 True 之后没有下文）没有终止事件。
8. **未知帧**：帧是 dict、`type` 是 str、但类型不在第 4 条之内 → 事件 `type` 为 `UNKNOWN_FRAME`，`event_id` 为 `"L<行号>"`，payload 恰好五个键 `{"frame_type": 该 type, "line": 行号, "reason": "frame type outside the dialect", "sha256": 帧的规范 JSON（`json.dumps(frame, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)`，UTF-8 以 surrogatepass 编码；编码失败时退回 `repr(frame)`）的 SHA-256 小写十六进制, "bytes": 该编码的字节数}`；不放原帧。`agent_settled` 之后的帧同样是 `UNKNOWN_FRAME`，reason `frame after agent_settled`。
9. **畸形帧**：帧不是 dict（reason `frame is not an object`，`frame_type` 为 None）、`type` 不是 str（`type is not text`，`frame_type` 为 None），或第 4 条里某个必须的形状不满足 → 事件 `type` 为 `INVALID_FRAME`，payload 与第 8 条同形，`frame_type` 为该帧的 type，reason 用下列文字：`second session header`、`session header is not the first frame`、`session version is not 3`、`session id or timestamp is not text`、`message is not an object`、`message role outside the dialect`、`assistant content is not a list`、`assistant content block is malformed`、`toolCall block without an id`、`stopReason is not text`、`provider is not text`、`model is not text`、`responseId is not text`、`thinkingLevel is not text`、`toolCallId or toolName is not text`、`isError is not a boolean`、`willRetry is not a boolean`。检查顺序：第二个 session 先于“不在第 1 行”，先于版本，先于 id/timestamp；message 的 dict、role、content、各块、`stopReason`、`provider`、`model`、`responseId`、`thinkingLevel` 依次。每个未知或畸形帧使 `unknown` 加一、`usage_complete` 为 False；它们不计入 `ignored`。
10. **`PiNativeAdapter(OfflineAdapter)`**：类属性 `adapter_id = "pi"`、`protocol_version = "pi-session-v3"`。`probe()` 返回父类的报告加 `"dialect": DIALECT`。`translate(frames)` 等于模块级 `translate`。`run(invocation, frames)`：`invocation` 不是 `Invocation` → `AdapterError("E_ADAPTER_INVOCATION", …)`；`invocation.adapter_id` 不是 None 且不等于 `"pi"` → `AdapterError("E_ADAPTER_IDENTITY", …)`；否则翻译，再用父类的 `run` 对翻译出的事件（每个事件的拷贝）结算，父类的异常原样冒出。
11. **结果**：三份完整夹具经 `run` 得到 `settled=True`、`status="SUCCEEDED"`、`raw_complete=True`、`usage` 等于最后一条 usage 事件的 payload（五个维度等于 meta 的 `dispatcher_summary.usage`，`complete` 为 True）；两份截断夹具得到 `settled=False`、`status="UNKNOWN"`、`usage={"status": "unknown"}`。
12. **兼容性**：不改任何已有模块与公开接口；`OfflineAdapter` 的行为不变。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3、5、6 节、oracle 与夹具的 meta，以及 `adapter_protocol.py` 的 `normalize_events`、`OfflineAdapter.run`、`OfflineAdapter.probe`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 新建 `sentinelmesh/pi_events.py`。
3. 新建 `tests/test_pi_events.py`，覆盖下列 oracle 没有逐个覆盖的用例（每个拒绝用例先验证合法基线，再只改一处，断言恰好一个 `INVALID_FRAME` 事件及其 `line`、`frame_type`、`reason`）：
   - 第 9 条的每一种畸形形状各一个用例，逐个字段：session 的 `id` 不是 str、`timestamp` 不是 str、`version` 为 `"3"`；`message` 不是 dict；`role` 不是 str、`role` 不在四种之内；`content` 不是 list；块不是 dict；块没有 str 的 `type`；toolCall 块的 `id` 缺失、为空、不是 str；`stopReason`/`provider`/`model` 各自缺失与不是 str；`responseId`/`thinkingLevel` 存在但不是 str；`toolCallId` 缺失/为空/不是 str；`toolName` 缺失/为空/不是 str；`isError` 缺失/不是 bool；`willRetry` 缺失/不是 bool；
   - 第 9 条的检查顺序：两个错误同时出现的用例至少三对（第二个 session 且版本错；`role` 错且 `content` 错；`stopReason` 错且 `provider` 错），断言报的是先检查的那一条；
   - `ignored` 的逐项计数：一条流里每种被忽略的类型各出现至少两次，`message_end` 计的是 system、user、toolResult 之和；未知与畸形帧不计入 `ignored`；
   - 事件 id 与 seq：多帧流里 id 为 `L<行号>`、两个事件的后缀、seq 连续；
   - 未知帧 payload 的 `sha256` 与 `bytes` 等于合同第 8 条的编码；含非 ASCII 与嵌套对象的帧各一例；
   - `usage_complete` 为 False 之后每条 `usage` 事件都没有 token 键，而 `PiTranslation.usage` 仍是合法消息之和；`assistant` 事件里这条消息的 `usage` 为 None；
   - `translate` 接受生成器且只消费一次（翻译后生成器已耗尽）；`frames` 为 str/bytes/bytearray/int/None 时报 `E_ADAPTER_EVENT`；
   - `PiTranslation` 是 frozen dataclass（赋值报 `FrozenInstanceError`），`events` 是元组；
   - `models` 去重排序、`agent_cycles`、`tool_errors`、`last_stop_reason` 取最后一条；
   - `PiNativeAdapter.probe()` 的键与值；`run` 接受 `adapter_id` 为 None 的 invocation，拒绝其他 adapter_id；`run` 传给父类的是事件的拷贝（翻译结果不被父类改动）。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_pi_events_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_pi_events.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_adapter_protocol.py tests/test_adapter_protocol_reference_oracle.py tests/test_adapter_protocol_differential_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_adapter_run_oracle.py tests/test_verified_review.py tests/test_verified_review_gaps.py tests/test_verified_review_oracle.py tests/test_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.pi_events; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为 `pi_events` 还不存在；V02 是因为测试文件还不存在。V03 保护参考归约器、adapter 接线与已有的参考用例（Windows 上其中的原生用例跳过，这是预期）。

## Definition of done
- D01：模块、常量、`translate` 的入口规则与 `PiTranslation` 的字段（Behavior contract 第 1–3 条）；证据：V01、V02。
- D02：映射表（第 4–5 条）；证据：V01、V02。
- D03：用量规则（第 6 条）；证据：V01、V02。
- D04：终止规则（第 7 条）；证据：V01、V02。
- D05：未知与畸形帧（第 8–9 条）；证据：V01、V02。
- D06：`PiNativeAdapter` 与夹具的结算结果（第 10–11 条）；证据：V01、V02。
- D07：导入无副作用，已有模块不退化；Step 3 的测试齐全，不自行做变异检查（第 1、12 条）；证据：V03、V04、V07、review。
- D08：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、夹具、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 pi。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 6 分钟；
  - 实现约 8 分钟；
  - 测试约 9 分钟；
  - 运行检查约 3 分钟（oracle 约 1 秒，全量约 30 秒）；
  - 执行报告约 3 分钟；
  - 合计约 29 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D08，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、缩减脚本、规划端专用测试、已有模块与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
