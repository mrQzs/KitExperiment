# TASK: claude-adapter

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 1350884b227f6e65bd9c69d80b492221f4102fa0
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch, merge
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/claude-adapter.md`（用户决定第 5 节四项均选推荐项；录制见第 9 节）新增 `sentinelmesh/claude_events.py`：

- 把 Claude Code 的 `--output-format stream-json` 流（方言 `claude-stream-json`）翻译成规范事件，原样交给参考归约器结算，做法与 `pi_events.py` 对 pi 的相同；
- 在 `run_cli._ADAPTERS` 里以 id `claude` 注册。

验收标准：
- 规划端 oracle `tests/test_claude_adapter_oracle.py`（12 个用例）全部通过；
- 新建执行器测试 `tests/test_claude_adapter.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改 `pi_events.py`、`adapter_protocol.py`（参考归约器）、`adapter_run.py`、`protocols.py`、账本与监管器：新 adapter 只是又一个 `OfflineAdapter` 子类加一行注册。
- 不翻译工具调用、多轮、重试、压缩、`user`、`stream_event` 这些帧：本机唯一的真实样本是单轮、无工具的，没见过的都失败关闭（第 3 条）。
- 不读 `total_cost_usd`，美元费用不进账本（用户决定第 3 项）。
- 不扩展 `smctl ledger reference`（它只认 `pi-session-v3`），不加生成作业文件的命令（用户决定第 4 项）。
- 不联网、不读凭据、不启动真实的 Claude Code：所有用例都只重放夹具。

## Repository facts

**基线与 oracle**
- 基线 = master `459d598`（录制与夹具）+ 规划端提交 `1350884`。规划端提交包含：
  - 定稿的设计说明；
  - 新文件 `tests/test_claude_adapter_oracle.py`；
  - `tests/test_run_cli_pi.py` 里两处钉死注册表的断言改成三项：`set(_ADAPTERS)` 是 `{"claude", "offline", "pi"}`，`load_job` 的拒绝信息是 `adapter_id must be one of claude, offline, pi`（`run_cli` 用排序后的键拼出这句，所以注册之后不用改信息本身）。
- oracle 有 12 个用例，一个类 `ClaudeOracle`，Windows 上约 5 秒。基线上全部失败，因为 `sentinelmesh.claude_events` 不存在；`test_run_cli_pi.py` 的那两个用例在基线上也失败。
- oracle 的驱动方式：
  - 直接调用 `claude_events.translate` 与 `ClaudeStreamAdapter`；
  - 进程内 `cli.main` 跑 `smctl run init` 与 `smctl run once`，作业的 `adapter_id` 是 `claude`，子进程是 `python -c`，原样写出夹具或改过的流；
  - 读账本的 `operation_usage`；
  - 起一个子进程比较导入 `claude_events` 与导入 `pi_events` 各自拉进哪些模块。

**真实夹具** `tests/fixtures/claude_stream/windows-claude2_1_301-live-a001.jsonl`（`.meta.json` 是来源与 CLI 自己的报告），共 4 帧：
1. `{"type": "system", "subtype": "init", "session_id", "model", "claude_code_version", …二十多个别的键}`；
2. `{"type": "assistant", "message": {"model", "id", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "OK"}], "stop_reason": null, "usage": {…单条消息的用量}, …}, "session_id", "uuid", …}`；
3. `{"type": "rate_limit_event", "rate_limit_info": {…数值已清零}, …}`；
4. `{"type": "result", "subtype": "success", "is_error": false, "num_turns": 1, "total_cost_usd": 0.0188578, "usage": {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 649, "cache_creation_input_tokens": 2330, …还有一些嵌套键}, …}`。

注意：
- 长字符串与像路径的字符串已经换成 `[reduced len=N sha256=…]`，翻译器只看类型与短字段，不受影响；
- 夹具在检出时可能是 CRLF，逐行读时要去掉行尾空白（oracle 用 `line.strip()`）。

**`sentinelmesh/pi_events.py`（421 行）是逐项的样板**
- 模块常量：`DIALECT`、`SOURCE`、`TERMINAL_FRAME`、`UNKNOWN_FRAME`、`INVALID_FRAME`、`USAGE_KEYS`；
- `_frame_digest(frame)` 给异常帧的摘要与字节数，用规范 JSON：`sort_keys`、`ensure_ascii=False`、`separators=(",", ":")`、`default=repr`、`surrogatepass`；
- `_Accumulator` 有 `_emit` / `_odd` / `feed`，异常帧记成事件 `{"seq", "event_id": "L<行号>", "type", "source", "payload": {"frame_type", "line", "reason", "sha256", "bytes"}}`；
- 冻结的数据类 `PiTranslation`；
- `translate(frames)` 遇到文本、字节串或不可迭代的输入，抛 `AdapterError("E_ADAPTER_EVENT")`；
- `PiNativeAdapter(OfflineAdapter)` 有 `adapter_id`、`protocol_version`、`probe`、`translate`、`run`：`run` 核对身份，然后把事件的深拷贝交给 `super().run`。

**参考归约器 `adapter_protocol.OfflineAdapter.run`（不改）**
- 第一个事件必须是唯一的 `run_started`，否则抛 `E_ADAPTER_SETTLEMENT`，run 的结果是 `result_error`；
- 终止事件（`run_finished` 或 `error`）必须唯一、在最后；
- 不在词汇表里的事件类型（`claude.unknown_frame` 之类）保留成 `unknown`，结果是 `UNKNOWN`、不结算；
- 用量取最后一个 `usage` 事件的 payload。`adapter_run` 只结算 payload 里键名等于作业 `usage_dimensions`、值是非负整数的维度，`complete` 这类键不算维度。

**注册**：`run_cli._ADAPTERS` 现在是 `{OfflineAdapter.adapter_id: OfflineAdapter, PiNativeAdapter.adapter_id: PiNativeAdapter}`，`load_job` 只接受这里的键（精确匹配，`Claude`、`claude ` 都是 `E_RUN_JOB`）。

**规划端草稿实测**（Windows，约 230 行）
- oracle 12/12，`test_run_cli_pi.py` 全过；
- 草稿树全量 1116 passed、281 skipped（约 225–275 秒，与基线并行时偏慢）；
- 20 条故意偏差里 19 条被 oracle 抓住：
  - 不忽略 `rate_limit_event`、`init` 不必在第一行、接受工具调用块、接受 `result` 之后的帧；
  - 不算合计、接受负数、把布尔当整数、不看 `is_error`、没有 `assistant` 也算成功；
  - 注册表里没有 `claude`、正文进了事件、接受字节串；
  - `is_error`、`num_turns`、`assistant` 的模型名不查类型；
  - 缓存写取错字段、用量不从 `result` 取；
  - 前面有异常帧时 `usage` 事件仍声称完整、`result` 之后有异常帧时翻译结果仍带用量。
- 1 条预期漏掉：“不核对 adapter 身份”。父类的 `run` 自己再核对一次，抛的是同一个码，是等价变异。

## Scope
- allowed_modified_files:
  - sentinelmesh/run_cli.py
- allowed_new_files:
  - sentinelmesh/claude_events.py
  - tests/test_claude_adapter.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_claude_adapter_oracle.py`, `tests/test_pi_events*.py`, `tests/test_live_pi_fixture.py`, `tests/test_run_cli*.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/protocols.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/cli.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定：模块 `sentinelmesh.claude_events` 的 `DIALECT`、`SOURCE`、`TERMINAL_FRAME`、`UNKNOWN_FRAME`、`INVALID_FRAME`、`USAGE_KEYS`、`TOTAL_DIMENSION`、`ClaudeTranslation`、`ClaudeStreamAdapter`、`translate`。其余（累加器、辅助函数）由你定。

1. **常量**（值全等）：
   - `DIALECT = "claude-stream-json"`；`SOURCE = "claude"`；`TERMINAL_FRAME = "result"`；
   - `UNKNOWN_FRAME = "claude.unknown_frame"`；`INVALID_FRAME = "claude.invalid_frame"`；
   - `USAGE_KEYS = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"), ("cache_read_input_tokens", "cache_read_tokens"), ("cache_creation_input_tokens", "cache_write_tokens"))`，每对是（厂商键，规范维度）；
   - `TOTAL_DIMENSION = "total_tokens"`；
   - 计数后忽略的帧类型只有 `rate_limit_event`；`assistant` 内容块只认 `text` 与 `thinking`。
2. **`ClaudeTranslation`**：冻结数据类，字段恰好是 `dialect`、`events`（元组）、`frames`、`ignored`（帧类型到次数）、`unknown`、`usage`、`usage_complete`、`assistant_messages`、`terminal`、`subtype`、`is_error`、`num_turns`、`models`（排序后的元组）。
3. **`translate(frames)`**：
   - 文本、字节串（`str`、`bytes`、`bytearray`）或不可迭代的输入，抛 `AdapterError("E_ADAPTER_EVENT")`。
   - 对每一帧（行号从 1 起）依次判断：
     - 不是 dict：异常帧，`INVALID_FRAME`，原因 `frame is not an object`；
     - `type` 不是 str：`INVALID_FRAME`；
     - 已经见过终止帧：`UNKNOWN_FRAME`，原因 `frame after result`；
     - `system`：
       - `subtype` 不是 `init`：`UNKNOWN_FRAME`；
       - 已见过 `init`，或者不在第 1 行：`INVALID_FRAME`；
       - `session_id`、`model` 不是 str，或者 `claude_code_version` 既不是 None 也不是 str：`INVALID_FRAME`；
       - 否则发 `run_started`，payload 恰好是 `{"dialect", "session_id", "model", "claude_code_version"}`，并把 model 记进 `models`。
     - `assistant`：
       - `message` 不是 `role == "assistant"` 的 dict，或 `content` 不是 list：`INVALID_FRAME`；
       - 某个块不是带 str `type` 的 dict：`INVALID_FRAME`；
       - 块类型不在 `text`/`thinking` 里：`UNKNOWN_FRAME`；
       - `message.model` 或 `message.id` 不是 str：`INVALID_FRAME`；
       - 否则 `assistant_messages` 加一、model 记进 `models`，发 `assistant`，payload 恰好是 `{"model", "message_id", "text_blocks", "thinking_blocks"}`。正文绝不进事件。
     - `result`：见第 4 条。
     - `rate_limit_event`：计入 `ignored`，不发事件。
     - 其它类型：`UNKNOWN_FRAME`，原因 `frame type outside the dialect`。
   - 每个异常帧发一个事件：类型是对应的 `*_FRAME`，`event_id` 为 `L<行号>`，payload 恰好是 `{"frame_type", "line", "reason", "sha256", "bytes"}`，摘要的算法与 `pi_events._frame_digest` 相同；同时 `unknown` 加一。
   - 正常事件的 `event_id`：`L<行号>`，`usage` 用 `L<行号>.usage`。`seq` 从 0 连续编号，`source` 是 `claude`。
4. **终止帧 `result`**：
   - 先记“已见过终止帧”，再检查；
   - `subtype` 不是 str、`is_error` 不是 bool、`num_turns` 不是非负 int：`INVALID_FRAME`，不发 `usage`，不发终止事件；
   - 否则记下 `terminal = "result"`、`subtype`、`is_error`、`num_turns`，然后：
     - **用量**：`usage` 是 dict，四个厂商键都在，值都是精确的 int、在 `0..MAX_SAFE_INTEGER` 之间（布尔不算），而且四者之和也不超过 `MAX_SAFE_INTEGER`：这时用量就是四个规范维度加 `total_tokens`（四者之和）。否则用量不完整。
     - 发一个 `usage` 事件，payload 是 `{"assistant_messages", "complete"}`。只有用量完整、并且此前没有任何异常帧时，才把五个维度并进 payload，`complete` 为真；否则 `complete` 为假，不带维度。
     - 然后发一个终止事件，payload 是 `{"subtype", "num_turns", "assistant_messages"}`：
       - `subtype == "success"`、`is_error` 为假、`assistant_messages > 0`：发 `run_finished`；
       - 否则发 `error`，payload 另加 `reason`：成功却没有 assistant 时是 `"no_assistant_message"`，其余是 `"subtype:<subtype> is_error:<true|false>"`。
   - 用量只从 `result` 取，不从各条 `assistant` 的用量累加。
5. **翻译结果**：
   - `usage` 只在用量完整且没有异常帧时等于那五个维度，否则是 `{}`；
   - `usage_complete` 同样只在这种情况下为真；
   - `terminal`、`subtype`、`is_error`、`num_turns` 没有合法的 `result` 时是 None。
6. **`ClaudeStreamAdapter(OfflineAdapter)`**：
   - `adapter_id = "claude"`，`protocol_version = DIALECT`；
   - `probe()` 在父类的报告上加 `dialect`；
   - `translate(frames)` 就是模块的 `translate`；
   - `run(invocation, frames)`：invocation 不是 `Invocation` 时抛 `E_ADAPTER_INVOCATION`；adapter 不符时抛 `E_ADAPTER_IDENTITY`；然后把翻译出的事件的深拷贝交给 `super().run`。
7. **注册**：`run_cli` 导入 `ClaudeStreamAdapter`，`_ADAPTERS` 加一项 `ClaudeStreamAdapter.adapter_id: ClaudeStreamAdapter`。模块文档串里“Two adapters are accepted”一段改成三个，`_ADAPTERS` 的注释也一并改。别的不改。
8. **文档串与导入**：
   - 模块文档串照 `pi_events` 写明方言、各帧的规则、失败关闭、用量只取 `result`、文本不进事件；
   - 导入 `claude_events` 拉进的模块，不多于导入 `pi_events` 拉进的（oracle 比较两者）；
   - 导入时不读文件、不起进程、不联网。

## Steps
1. 读 AGENTS.md、本合同、设计说明（第 3.2、5、9 节）、oracle、夹具与它的 meta，以及 `pi_events.py` 全文、`adapter_protocol.OfflineAdapter.run`、`run_cli._ADAPTERS` 与 `load_job`。基线与 Repository facts 不符就停止并报告。
2. 新建 `sentinelmesh/claude_events.py`（第 1–6、8 条；约 230 行连文档串），改 `run_cli.py`（第 7 条；约 6 行）。
3. 跑 V01 直到 oracle 全过（约 5 秒），再跑 V03（pi 与 run 的已有测试）。
4. 新建 `tests/test_claude_adapter.py`，只覆盖下面这些。oracle 已经覆盖夹具、终止、失败关闭、非法用量、注册与跨 adapter；可以沿用 oracle 的写法，但不要照抄 oracle 的用例。
   - 手写一条有两个 `assistant`、各带一个 `thinking` 与一个 `text` 块的成功流：`assistant_messages` 为 2，`assistant` 事件的块计数正确，终止是 `run_finished`；
   - 异常帧事件的 payload：键集合恰好五个，`sha256` 与 `bytes` 等于用第 3 条的规范 JSON 自己算出来的；
   - 四个厂商键都合法、但总和超过 `MAX_SAFE_INTEGER`：用量不完整，`usage` 为 `{}`；
   - `translate(42)`（不可迭代）是 `E_ADAPTER_EVENT`；`ClaudeStreamAdapter().run("x", [])` 是 `E_ADAPTER_INVOCATION`；
   - `ClaudeTranslation` 是冻结的：给字段赋值会抛异常。

   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
5. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
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

- V01（必要，基线应失败）：`python -m pytest -q tests/test_claude_adapter_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_claude_adapter.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要，基线应失败）：`python -m pytest -q tests/test_pi_events.py tests/test_live_pi_fixture.py tests/test_run_cli_pi_oracle.py tests/test_run_cli_pi.py tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_serve_oracle.py tests/test_run_cli_serve_watch_oracle.py` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：1200s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "from sentinelmesh import claude_events as c, run_cli; assert c.DIALECT == 'claude-stream-json' and c.SOURCE == 'claude' and c.TERMINAL_FRAME == 'result' and c.TOTAL_DIMENSION == 'total_tokens'; assert run_cli._ADAPTERS['claude'] is c.ClaudeStreamAdapter and sorted(run_cli._ADAPTERS) == ['claude', 'offline', 'pi']; print('claude ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`claude ok`
- V08（必要）：`git diff --quiet 1350884 -- tests/test_run_cli_pi.py sentinelmesh/pi_events.py sentinelmesh/adapter_protocol.py sentinelmesh/adapter_run.py sentinelmesh/protocols.py sentinelmesh/durable_ledger.py sentinelmesh/scheduler.py sentinelmesh/ledger_cli.py sentinelmesh/cli.py sentinelmesh/windows_supervisor.py sentinelmesh/supervised_operation.py tests/test_claude_adapter_oracle.py tests/fixtures/claude_stream/windows-claude2_1_301-live-a001.jsonl tests/fixtures/claude_stream/windows-claude2_1_301-live-a001.meta.json` · cwd：仓库根 · 超时：60s · 期望退出码：0

在基线上，V01、V04、V07 失败是因为 `claude_events` 还不存在，V02 失败是因为测试文件还不存在，V03 失败是因为 `test_run_cli_pi.py` 已经钉死三项注册表。

V03 保护 pi adapter 与 run 的已有命令；V08 保护十个不该动的模块、`test_run_cli_pi.py`、oracle 与夹具（检查命令按 argv 直接执行，不经 shell）。全量约 230 秒。

## Definition of done
- D01：常量、翻译结果与逐帧规则（第 1–3、5 条）；证据：V01、V02、V07；目的：真实录下的 Claude Code 流被一帧不差地翻成规范事件，正文不进事件，没见过的帧一律失败关闭。
- D02：终止帧与用量（第 4 条）；证据：V01、V02；目的：账本里结算的 token 正是 CLI 自己在 `result` 里报告的会话合计，错误终止与用量不合法时不多记一分。
- D03：adapter 类与注册（第 6–7 条）；证据：V01、V07；目的：作业写 `adapter_id: "claude"` 就能经 `once`/`serve` 结算，名字精确匹配，pi 与离线桩不受影响。
- D04：其它模块与命令不受影响，文档串与导入按第 8 条；证据：V03、V04、V08、review。
- D05：Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、夹具、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实的 Claude Code。
- 本合同对同一行为说法不同。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 3000
- max_wall_seconds_total: 6000
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配（上限 50 分钟，按 30 分钟规划、留 20 分钟余量）：
  - 阅读约 8 分钟；
  - 实现约 10 分钟（新模块约 230 行，注册约 6 行）；
  - 测试约 5 分钟（5 组用例，先跑单个用例）；
  - 运行检查约 6 分钟（oracle 约 5 秒，V03 约 120 秒，全量约 230 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍写进 `interpretations`（合同原文、你的做法、理由）；与合同矛盾的写进 `contract_conflicts`，没法决定的写进 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
