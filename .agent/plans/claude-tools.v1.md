# TASK: claude-tools

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: dc03ae037b15897b3d93e501d78e8d1b47829940
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch, merge
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/claude-tools.md`（第 3.2 节是定稿的规则，第 5 节是用户的两次决定，第 9 节是录制）扩展 `sentinelmesh/claude_events.py`，并让 `smctl ledger reference` 认 `claude-stream-json`：

- Claude Code 的工具调用翻成 `tool_started` / `tool_finished`，交给参考归约器配对；
- 录制里见到的权限拒绝、通知、思考估计、合成的用户消息只计数；模型回退接受并计数；
- 结算改用 `result.modelUsage` 各模型之和（计费的全额）；
- 翻译结果多出逐消息用量等字段，`reference` 用它们给出参考值。

验收标准：
- 规划端 oracle `tests/test_claude_tools_oracle.py` 全部通过；
- 规划端改钉过的 `tests/test_claude_adapter_oracle.py` 与 `tests/test_claude_adapter.py` 全部通过；
- 新建执行器测试 `tests/test_claude_tools.py`（清单见 Step 4，刻意不长）。

## Non-goals
- 不改 `adapter_protocol.py`（参考归约器）、`pi_events.py`、`adapter_run.py`、`run_cli.py`、`protocols.py`、账本与监管器。
- 子代理、压缩、hook、API 重试、`stream_event`、图片块仍按未知帧失败关闭（没有样本）。
- 被撤回的帧不从事件里删掉，回退之后不重算任何东西。
- `pi-session-v3` 的参考值一字不改；账本仍不记方言，`--dialect` 仍由操作员点名。
- 不联网、不读凭据、不启动真实的 Claude Code：所有用例都只重放夹具。

## Repository facts

**基线与 oracle**
- 基线是 master 上的规划端提交 `dc03ae0`（在设计说明 `4074012` 与录制 `eee3661` 之后）。这个提交包含：
  - 新文件 `tests/test_claude_tools_oracle.py`，17 个用例，一个类 `ToolStreamOracle`；
  - `tests/test_claude_adapter_oracle.py` 改钉了两处：
    - 非法用量的用例改在 `modelUsage` 上；
    - 失败关闭矩阵里的工具块改成“没有 id 的工具块”；
  - 上一轮执行器写的 `tests/test_claude_adapter.py` 改钉了五处：
    - 两条消息用不同的 id；
    - `result()` 带 `modelUsage`；
    - `assistant` 载荷与 `ClaudeTranslation` 的字段集合加了新字段；
    - 异常帧用例改用 `stream_event`；
    - 总和越界的用例改在 `modelUsage` 上。
- 基线上的失败情况：
  - 新 oracle 有 14 个用例失败；另外 3 个在基线上也通过：夹具 meta 的核对、截断的流进复审、失败关闭矩阵。这三项现有代码本来就满足，它们守的是改动之后不能退步；
  - 两个改钉的文件失败 4 个用例（其中一个有 6 个子用例），原因是现有代码仍读 `result.usage`、按帧计消息数。
- oracle 的驱动方式与上一轮相同：
  - 直接调用 `claude_events.translate`；
  - 进程内 `cli.main` 跑 `smctl run init`、`smctl run once`（作业的 `adapter_id` 是 `claude`，子进程是 `python -c`，原样写出夹具或改过的流）和 `smctl ledger reference`；
  - 读账本的 `operation_usage`。

**新夹具** `tests/fixtures/claude_stream/windows-claude2_1_301-tools-a001.jsonl`：22 帧，行号 L1–L22。`.meta.json` 的 `cli_report` 有 `usage`（即 `result.usage`）、`model_usage`（按模型）与 `total_cost_usd`。
- L1 `system/init`。
- L2 `assistant`，消息 1，块 `tool_use` T1（Read）。
- L3 `system/permission_denied`，`tool_use_id` 是 T1。
- L4 `user`，`tool_result` T1，`is_error: true`。
- L5 `rate_limit_event`。
- L6 `system/informational`。
- L7 `user`，`isSynthetic: true`，一个 `text` 块，带 `refused_message_id`。
- L8、L9 `assistant`，消息 2 的两帧：`thinking`，然后 `tool_use` T2。
- L10 `user`，`tool_result` T2，`is_error: true`。
- L11 `system/model_refusal_fallback`，带 `original_model`、`fallback_model`、`retracted_message_uuids`（撤回 L8–L10）。
- L12、L13、L15 `assistant`，消息 3 的三帧：`thinking`，`tool_use` T3，`tool_use` T4。
- L14 `user`，`tool_result` T3，没有 `is_error` 键。
- L16 `user`，`tool_result` T4，`is_error: true`。
- L17–L19 `system/thinking_tokens`。
- L20、L21 `assistant`，消息 4：`thinking`，然后 `text`。
- L22 `result`：`success`、`is_error: false`、`num_turns: 6`。
  - `usage` 是 6 / 627 / 8253 / 3060，不含被撤回的消息 2。
  - `modelUsage` 有 `claude-opus-5-5` 与 `claude-opus-4-8` 两项，合计 10 / 670 / 11602 / 3401，合计 15683。

另外：
- 所有 `assistant`、`user` 帧都带 `parent_tool_use_id: null`。
- 同一消息的各帧 `message.usage` 相同；逐消息（每个 id 一次）之和是 10 / 27 / 11602 / 3401，合计 15040。
- 长字符串与像路径的字符串已经换成 `[reduced len=N sha256=…]`；工具 id（`toolu_…`）与消息 id（`msg_…`）不超过 32 个字符，原样保留。
- 夹具检出时可能是 CRLF，逐行读要去掉行尾空白。
- 上一轮的单轮夹具 `windows-claude2_1_301-live-a001` 只有一个模型、没有撤回，`modelUsage` 与 `result.usage` 相等（2 / 4 / 649 / 2330，合计 2985），所以它的结算不变。

**现有的 `claude_events.py`（405 行，上一轮的候选与收尾）**
- `_Accumulator` 有 `_emit`、`_ignore`、`_odd`、`feed`、`_system`、`_assistant`、`_result`、`_dimensions`、`result`；
- `_odd` 清空 `usage` 并把 `usage_complete` 置假；
- `feed` 分派 `system`、`assistant`、`result` 和忽略的 `rate_limit_event`，其他一律 `frame type outside the dialect`；
- `_assistant` 遇到 `text` / `thinking` 以外的块就记为未知帧；
- `_result` 从 `result.usage` 用 `_dimensions` 取用量。

**参考归约器 `adapter_protocol.OfflineAdapter.run`（不改）**
- `tool_started` 要有合法的 `tool_id`，同一 id 未关闭前不能再开；`tool_finished` 必须关闭一个已开的 id；违反时抛 `E_ADAPTER_TOOL`。
- 终止时还有没关闭的工具，同样抛 `E_ADAPTER_TOOL`。这条检查在“有未知事件就返回 UNKNOWN”之前，所以一个异常帧留下的未关闭工具，`once` 报告的是 `result_error`，而不是 `UNKNOWN`。oracle 两种都接受。

**`ledger_cli.py` 的 `reference`**
- `_DIALECTS = (DIALECT,)`（pi 的），`--dialect` 的 `choices` 就是它；
- `_REFERENCES = {DIALECT: _pi_reference}`；`cmd_reference` 用 `_REFERENCES[args.dialect](stream["events"])` 算出参考值，`stream["events"]` 是 `read_stream` 逐行解析出的帧；
- `_pi_reference` 是样板：只调用翻译器，不自己重算。

**规划端草稿实测**（Windows；`claude_events.py` 约 400 行，`ledger_cli.py` 加约 30 行）
- 新 oracle 与两个改钉的文件：38 个用例全过，约 12 秒；
- 草稿树全量 1142 passed、281 skipped，约 325 秒（与变异、基线检查并行，偏慢）；
- 40 条故意偏差，oracle 加两个改钉的文件抓住 39 条：
  - 工具块仍算未知帧；跨帧或同帧重复的 `tool_use` id、空工具名也接受；
  - `is_error` 缺省当真、不查类型；每个结果都算工具错误；
  - 带工具结果的合成消息也忽略；子代理的 `user` / `assistant` 帧也接受；
  - 权限拒绝不查 `tool_use_id`、不另计；`thinking_tokens` 不忽略；
  - 回退不查原模型、回退模型不进 `models`、回退仍算未知帧；其他 `system` subtype 也忽略；
  - 按帧计消息数；消息用量取第一帧；
  - 只算第一个模型；空的 `modelUsage` 算零；布尔算整数；总和不查上限；
  - 前面有异常帧也结算；异常帧也清掉 `reported_usage`；不计不合法的消息；不计工具调用；
  - 终止载荷少了回退次数；`tool_finished` 的工具名取错；
  - 同帧两个结果关同一个工具；结果不必关闭已开的工具；
  - `reference`：`reasons` 顺序颠倒、不标 `partial_output`、不报未知帧、总用逐消息之和、result 用量不合法不算 `illegal_usage`、方言不认 `claude`、pi 也走 claude 的参考值、少了 `fallbacks`。
- 1 条预期漏掉：去掉“各维之和不超过上限”的检查。四个维度都非负，任何一维越界，总和一定越界，所以这是等价变异。

## Scope
- allowed_modified_files:
  - sentinelmesh/claude_events.py
  - sentinelmesh/ledger_cli.py
- allowed_new_files:
  - tests/test_claude_tools.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_claude_tools_oracle.py`, `tests/test_claude_adapter_oracle.py`, `tests/test_claude_adapter.py`, `tests/test_ledger_reference*.py`, `tests/test_pi_events*.py`, `tests/test_live_pi_fixture.py`, `tests/test_run_cli*.py`, `sentinelmesh/run_cli.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/protocols.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/cli.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定：`claude_events` 的全部现有公开名，新常量 `MODEL_USAGE_KEYS`，`ClaudeTranslation` 的新字段，`ledger_cli._DIALECTS`、`ledger_cli._REFERENCES`。其余（辅助函数、私有方法、新的原因文本）由你定。现有的原因文本 `frame is not an object`、`frame after result`、`frame type outside the dialect` 不改。

1. **常量**
   - 现有常量的值不变，`USAGE_KEYS` 改作逐消息用量的键。
   - 新增 `MODEL_USAGE_KEYS = (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("cacheReadInputTokens", "cache_read_tokens"), ("cacheCreationInputTokens", "cache_write_tokens"))`，并加进 `__all__`。
2. **`ClaudeTranslation`**：原有 13 个字段之后，依次追加 `reported_usage`、`message_usage`、`illegal_message_usage`、`tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`。仍是冻结数据类。
3. **`system` 帧**，按 `subtype`：
   - `init`：规则不变。
   - `permission_denied`：`tool_use_id` 必须是非空 str，否则 `INVALID_FRAME`。合法时不发事件，`ignored["system/permission_denied"]` 加一，`permission_denials` 加一。
   - `informational`、`thinking_tokens`：不发事件，`ignored["system/<subtype>"]` 加一。
   - `model_refusal_fallback`：`original_model` 与 `fallback_model` 都必须是非空 str，否则 `INVALID_FRAME`。合法时不发事件，`fallbacks` 加一，`fallback_model` 记进 `models`。
   - 其他 subtype（包括不是 str 的）：`UNKNOWN_FRAME`。
4. **`assistant` 帧**
   - 帧上 `parent_tool_use_id` 不是 None（键缺失等于 None）：`UNKNOWN_FRAME`。
   - `message`、`content`、块的形状检查，以及 `message.model`、`message.id` 的检查，都与现在相同。
   - 块类型：
     - `text`、`thinking`：计数；
     - `tool_use`：`id` 与 `name` 都必须是非空 str，否则 `INVALID_FRAME`；`id` 在本流里已经出现过（在之前接受的帧里，或本帧前面的块里），也是 `INVALID_FRAME`；
     - 其他：`UNKNOWN_FRAME`。
   - 一帧要么整帧接受，要么只记一个异常事件，不发这一帧的任何其他事件。
   - 接受时：
     - 先发 `assistant`，`event_id` 是 `L<n>`，payload 恰好是 `{"model", "message_id", "text_blocks", "thinking_blocks", "tool_use_blocks"}`；
     - 再按块的顺序，每个 `tool_use` 发一个 `tool_started`，`event_id` 是 `L<n>.tool<k>`（k 从 1 起），payload 恰好是 `{"tool_id", "tool"}`，`tool` 是 `name`；
     - `tool_calls` 加上这些块的个数，model 记进 `models`。
   - `assistant_messages` 是被接受的帧里不同 `message.id` 的个数。
   - **逐消息用量**：`message.usage` 是 dict，`USAGE_KEYS` 的四个厂商键都在，值都是 `0..MAX_SAFE_INTEGER` 的精确 int（布尔不算）时，这条消息的用量就是这四个维度；否则这条消息的用量不合法。同一 `message.id` 以最后一个被接受的帧为准。用量不合法不让帧变成异常帧。
5. **`user` 帧**
   - `parent_tool_use_id` 不是 None：`UNKNOWN_FRAME`。
   - `message` 不是 `role == "user"` 的 dict：`INVALID_FRAME`。
   - `content` 不是 list：`UNKNOWN_FRAME`。
   - 帧的 `isSynthetic` 是 `True`：块全是 `type == "text"` 的 dict 时，不发事件，`ignored["user/synthetic"]` 加一；否则 `UNKNOWN_FRAME`。
   - 其余情况：
     - `content` 为空，或有块不是 `type == "tool_result"` 的 dict：`UNKNOWN_FRAME`；
     - 每块的 `tool_use_id` 必须是一个已开启、尚未关闭的工具 id，同一帧里也不能出现两次，否则 `INVALID_FRAME`；
     - 每块的 `is_error`：键缺失算 `False`，出现时必须是 bool（`None` 也不行），否则 `INVALID_FRAME`；
     - 接受时，按块的顺序各发一个 `tool_finished`，`event_id` 是 `L<n>.tool<k>`，payload 恰好是 `{"tool_id", "tool", "is_error"}`，`tool` 是开启时的名字；关闭这些工具；`is_error` 为真的块计入 `tool_errors`。
6. **`result` 帧**
   - 形状检查与现在相同（`subtype`、`is_error`、`num_turns`）。
   - **用量来自 `result.modelUsage`**：
     - 它必须是非空 dict；每个值都必须是 dict，`MODEL_USAGE_KEYS` 的四个键都在，值都是 `0..MAX_SAFE_INTEGER` 的精确 int（布尔不算）；其他键（`costUSD` 等）不看；
     - 四个维度各自跨模型相加，每个和与四者的总和都不超过 `MAX_SAFE_INTEGER`；
     - 满足时 `reported_usage` 是这四个维度加 `total_tokens`，否则是 None；
     - `result.usage` 不再读。
   - `usage` 事件与结算规则不变：只有 `reported_usage` 不是 None 且此前没有任何异常帧时，`usage` 才等于它、`complete` 为真。`usage` 事件的 `assistant_messages` 用第 4 条的计数。
   - 终止事件的 payload 在 `{"subtype", "num_turns", "assistant_messages"}` 之外，加 `tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`；`error` 另带 `reason`，规则不变。
7. **翻译结果的新字段**
   - `reported_usage`：第 6 条的值，不因异常帧清空；没有合法的 `result` 时是 None；是新的 dict。
   - `message_usage`：每个 `message.id` 的合法用量之和，四个维度加 `total_tokens`。
     - 没有消息时，五项都是 0；
     - 任何一个和超过 `MAX_SAFE_INTEGER` 时，`message_usage` 是 `{}`，所有消息都算不合法。
   - `illegal_message_usage`：用量不合法的消息个数（按 id）。
   - `tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`：上面各条的计数。
8. **`smctl ledger reference`**
   - `_DIALECTS = (pi 的 DIALECT, claude_events.DIALECT)`；`_REFERENCES` 给 `claude-stream-json` 加一个函数，只调用 `claude_events.translate`，不自己重算。
   - 返回的 dict 的键恰好是 `complete`、`reasons`、`terminal`、`subtype`、`is_error`、`num_turns`、`usage`、`assistant_messages`、`tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`、`frames`、`unknown`、`models`（list）。
   - `usage` 的取法：
     - `reported_usage` 不是 None：取它；
     - 否则取 `message_usage`。
   - `reasons` 按下面的顺序：
     1. `no_terminal`：取 `message_usage`、且 `terminal` 是 None；
     2. `illegal_usage`：取 `message_usage`，且 `terminal` 不是 None（说明 result 的用量不合法），或 `illegal_message_usage > 0`；
     3. `partial_output`：取 `message_usage` 时总有；
     4. `unknown_frames`：`unknown > 0`。
   - `complete` 当且仅当 `reasons` 为空。
   - pi 的参考值、`cmd_reference` 的其余部分与输出的外层字段都不改。
9. **文档串**
   - `claude_events` 的模块文档串改写成现在的规则：工具配对、计数与忽略的帧、回退、`modelUsage`、新字段、两份录制、仍失败关闭的帧；
   - `ledger_cli` 中 `_DIALECTS`、`_REFERENCES` 的注释和 `reference` 相关的文档串，改成两个方言；
   - 导入 `claude_events` 拉进的模块仍不多于导入 `pi_events` 拉进的（上一轮的 oracle 在比较）。

## Steps
1. 读 AGENTS.md、本合同、设计说明（第 3.2、5、9 节）、新 oracle、两个改钉的测试文件、新夹具及其 meta、现有的 `claude_events.py` 全文、`ledger_cli.py` 的 `_pi_reference` 与 `cmd_reference`、`adapter_protocol.OfflineAdapter.run`。基线与 Repository facts 不符就停止并报告。
2. 改 `sentinelmesh/claude_events.py`（第 1–7、9 条）与 `sentinelmesh/ledger_cli.py`（第 8–9 条）。
3. 先跑 V02（两个改钉的文件，约 6 秒），再跑 V01（新 oracle，约 8 秒），直到都通过；然后跑 V04。
4. 新建 `tests/test_claude_tools.py`，只覆盖下面这些。可以沿用 oracle 的写法，但不要照抄 oracle 的用例。
   - 手写一条流：一帧 `assistant` 带两个 `tool_use` 块，下一帧 `user` 带两个 `tool_result` 块，一个 `is_error: true`，一个不带 `is_error`。核对：
     - 事件顺序与 `event_id`（`L<n>`、`L<n>.tool1`、`L<n>.tool2`、`L<m>.tool1`、`L<m>.tool2`）；
     - `tool_errors` 为 1；
     - 终止事件的 payload。
   - `message_usage`：两条不同 id 的消息，加上一条拆成两帧的消息，算出的和与 `assistant_messages`。
   - 一条截断、有一条消息用量不合法、还有一个未知帧的流，`reference` 的四个 `reasons` 按顺序都出现。用进程内的 `smctl run once` 加 `smctl ledger reference`，或直接调用 `_REFERENCES["claude-stream-json"]` 都可以。
   - 同一个 `fallback_model` 回退两次：`fallbacks` 为 2，`models` 里只出现一次。

   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
5. 运行 V01–V09。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告。合同没有写死、需要你自己定的地方，按最合理的读法继续，写进 `interpretations`；与合同矛盾、或没法继续时，停止并写进 `contract_conflicts` 或 `unresolved_questions`。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。

需要截短输出时：
- 照抄执行说明“机器合同摘要”里每条检查的“自检照抄”写法（bash 的写法以 `set -o pipefail;` 开头，管道的退出码就是被测程序自己的）；
- 自己临时写的测试命令要接 `| tail` 之类的管道时，开头同样加 `set -o pipefail;`；
- 不要把输出写到 `/tmp/v01.txt` 这类固定路径。

另外：
- 探针里不要用 `tempfile`；
- 不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改），验收端会在固定候选上做；
- 只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_claude_tools_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_claude_adapter_oracle.py tests/test_claude_adapter.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要，基线应失败）：`python -m pytest -q tests/test_claude_tools.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要）：`python -m pytest -q tests/test_ledger_reference.py tests/test_ledger_reference_oracle.py tests/test_pi_events.py tests/test_live_pi_fixture.py tests/test_run_cli_pi_oracle.py tests/test_run_cli_pi.py tests/test_stream_ceilings.py tests/test_stream_ceilings_oracle.py` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：1200s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要）：`git diff --quiet dc03ae0 -- tests/test_claude_tools_oracle.py tests/test_claude_adapter_oracle.py tests/test_claude_adapter.py tests/fixtures/claude_stream sentinelmesh/run_cli.py sentinelmesh/pi_events.py sentinelmesh/adapter_protocol.py sentinelmesh/adapter_run.py sentinelmesh/protocols.py sentinelmesh/durable_ledger.py sentinelmesh/scheduler.py sentinelmesh/cli.py tests/test_ledger_reference.py tests/test_ledger_reference_oracle.py` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V09（必要，基线应失败）：`python -c "from sentinelmesh import claude_events as c, ledger_cli as l; import dataclasses; f = [x.name for x in dataclasses.fields(c.ClaudeTranslation)]; assert f[13:] == ['reported_usage', 'message_usage', 'illegal_message_usage', 'tool_calls', 'tool_errors', 'permission_denials', 'fallbacks']; assert c.MODEL_USAGE_KEYS[0] == ('inputTokens', 'input_tokens') and 'MODEL_USAGE_KEYS' in c.__all__; assert l._DIALECTS == ('pi-session-v3', 'claude-stream-json') and set(l._REFERENCES) == set(l._DIALECTS); print('tools ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`tools ok`

在基线上：V01 失败是因为翻译器还不认工具帧；V02 失败是因为它还读 `result.usage`、按帧计消息数；V03 失败是因为测试文件还不存在；V05 同时包含这三项；V09 失败是因为新字段与新方言还不存在。

V04 保护 pi 的参考值与已有的 run 命令。V08 保护三个规划端测试文件、两份夹具和不该动的模块（检查命令按 argv 直接执行，不经 shell）。全量约 300 秒。

## Definition of done
- D01：工具帧、`user` 帧与 `system` 帧的规则（第 3–5 条）；证据：V01、V02、V03；目的：录下的带工具的流被一帧不差地翻成配对的工具事件，录制里出现过的权限拒绝、通知、思考估计、合成消息与模型回退都被认出并计数，没见过的仍然失败关闭。
- D02：结算改用 `modelUsage`（第 6–7 条）；证据：V01、V02、V09；目的：账本里结算的 token 是计费的全额，包括被撤回的轮次和回退模型；单轮录制的结算不变。
- D03：`reference` 认 `claude-stream-json`（第 8 条）；证据：V01、V03、V04、V09；目的：一次被杀或截断的 Claude Code 运行，操作员也能拿到按消息累加的参考值，并且知道输出只是下限；pi 的参考值不变。
- D04：其他模块与命令不受影响，文档串按第 9 条；证据：V04、V05、V08、review。
- D05：Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V06、V07、scope。

## Stop conditions
- 基线、oracle、夹具、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续，包括三个规划端测试文件。
- 需要新增或升级依赖、联网、访问凭据或启动真实的 Claude Code。
- 本合同对同一行为说法不同。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 3000
- max_wall_seconds_total: 6000
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配（上限 50 分钟，按 34 分钟规划、留 16 分钟余量）：
  - 阅读约 10 分钟；
  - 实现约 10 分钟（翻译器约 150 行改动，`ledger_cli.py` 约 30 行）；
  - 测试约 5 分钟（4 组用例，先跑单个用例）；
  - 运行检查约 8 分钟（V01、V02 约 20 秒，V04 约 120 秒，全量约 300 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍写进 `interpretations`（合同原文、你的做法、理由）；与合同矛盾的写进 `contract_conflicts`，没法决定的写进 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、两个改钉的测试文件、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
