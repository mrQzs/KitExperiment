# TASK: ledger-reference

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 7cdb039c386c418982df2a2e19d5e67e992cb0c3
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
给 `smctl ledger` 加一个只读命令 `reference`：打开账本，读某个受监督运行的输出流（运行描述里的证据目录下的 `stdout.log`），按运维指定的方言算出**参考用量**，连同预留与已记用量一起打印；它从不写账本。同时让 `adapter_run.read_stream` 在没有 `os.O_NOFOLLOW` 的平台（Windows）上也能失败关闭地读流。
- 修改 `sentinelmesh/ledger_cli.py`（新命令）与 `sentinelmesh/adapter_run.py`（`read_stream` 的非 POSIX 分支）；
- 让规划端 oracle `tests/test_ledger_reference_oracle.py` 全部通过（它用 5 份真实录得的缩减流与调度器的独立汇总对照）；
- 新建执行器测试 `tests/test_ledger_reference.py`。

## Non-goals
- 不自动补记用量，不给 `settle` 加从参考值取数的捷径（用户决定：只作参考值）。
- 不改 `status`、`stop`、`settle`、`resolve`、`resume` 的行为与输出；`status` 不读证据目录。
- 不做 `--dialect` 的自动识别，不做 `pi-session-v3` 之外的方言；不加 `--stream PATH` 一类读任意路径的参数。
- 不改 `read_stream` 的 POSIX 分支（`O_NOFOLLOW | O_NONBLOCK` 那段逐字保留）、签名、返回形状与上限常量；不改 `run_adapter_operation`。
- 不改 `pi_events.py`、`scheduler.py`、`durable_ledger.py`、`cli.py`、`adapter_protocol.py`、监督器与账本格式；不记录方言到运行描述。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具、规划端专用测试与已有测试；不联网、不读取凭据、不调用 pi。

## Repository facts
- 基线 = master `fb53e9a`（设计说明 `.agent/design/ledger-reference.md`：第 3 节方案、第 5 节用户决定、第 6 节规划端决定 R1–R9）+ 规划端 oracle 提交，后者含：
  - `tests/test_ledger_reference_oracle.py`：28 个用例（夹具对照、运维流程、不完整流、流的状态、无 `O_NOFOLLOW` 平台的分支、拒绝与退出码、账本关闭、`python -m sentinelmesh.cli` 入口），在基线上全部失败，因为 `reference` 子命令还不存在；
  - `tests/test_pi_events_captured.py` 新增一例（规划端专用，`.agent/runs` 不存在时跳过；执行器的工作副本里没有它，跳过是预期）。
- `sentinelmesh/ledger_cli.py`（约 290 行）：
  - `_with_ledger(args, work)`：`open_ledger(args.ledger)` 打不开报 `_refuse`；`work(ledger)` 抛 `ContractError` 或 `LedgerError` 时 `_refuse`（stderr 一行 `{"error": code, "detail": detail}`，返回 3）；`finally` 里 `ledger.close()`；成功时 `_emit(result)`（stdout 一行 `json.dumps(..., ensure_ascii=False, sort_keys=True)`）返回 0；
  - `_usage_incomplete(reservation, usage)`：预留里某一维没有记录、`amount` 为 None 或 `final` 不是 True 就返回 True（`status` 的 `usage_unknown` 口径）；
  - `register(subparsers)` 在 `ledger` 组下注册五个子命令，每个子命令第一个位置参数是 `ledger`；`cli.main` 对 `args.func` 之外的任何异常打印 `error: ...` 返回 2，argparse 错误 `SystemExit(2)`。
- 账本的只读方法（`durable_ledger.DurableScheduler` 与 `scheduler.Scheduler` 同名）：
  - `operation(op)` → `{"operation_id", "request_id", "task_id", "state", "owner", "generation", "reservation": dict}`，未知或非精确 str 报 `ContractError("E_OPERATION_UNKNOWN")`；`reservation` 保持领取时映射的顺序；
  - `run_descriptor(op)` → None 或深拷贝的 `{"spec": {...}, "evidence_dir": str, "worktree_id": str}`；`evidence_dir` 只保证是非空、无控制字符的文本，可能是相对路径；
  - `operation_usage(op)` → `{维度: {"call_key", "revision", "amount", "final", "evidence_hash"}}`，交接自己记的 `dispatches` 也在里面。
- `sentinelmesh/adapter_run.py`（约 430 行）：
  - `DEFAULT_MAX_STREAM_BYTES = 1 MiB`，`MAX_STREAM_BYTES = 16 MiB`，`STREAM_NAME = "stdout.log"`；
  - `read_stream(evidence_dir, *, max_bytes=DEFAULT_MAX_STREAM_BYTES, expected=None)` 返回 `{"status", "sha256", "bytes", "events"}`，`invalid` 时另有 `line` 与 `code`；非 `ok` 时 `sha256`、`bytes`、`events` 为 None（`invalid` 的 `sha256` 与 `bytes` 有值）；目前 `os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)`，`FileNotFoundError` → `missing`，其他 `OSError` → `unreadable`，`fstat` 不是普通文件 → `unreadable`，之后比对 `expected`、上限、读取、摘要、解析（`_parse_stream`：每行 `strict_json_loads`，不是 dict 的行 `invalid` 且 `code` 为 `E_ADAPTER_STREAM`）；
  - 模块导入无副作用（不导入 ctypes）。
- Windows 事实（本机 Windows Server 2025、Python 3.12.10，2026-10-09 实测，见设计说明第 2 节）：`os` 没有 `O_NOFOLLOW` 与 `O_NONBLOCK`，有 `O_BINARY`；`os.open` 不带 `O_BINARY` 按文本模式读（CR LF 折成 LF、遇 0x1A 停止）；普通文件的 `os.lstat`/`os.fstat` 的 `(st_dev, st_ino)` 相同；符号链接的 `lstat` 是 `S_ISLNK` 且 `st_file_attributes` 含 `stat.FILE_ATTRIBUTE_REPARSE_POINT`，打开它得到目标的身份；硬链接与原文件身份相同；打开目录报 `PermissionError`；`stat.FILE_ATTRIBUTE_REPARSE_POINT` 在所有平台的 `stat` 模块里都有，`st_file_attributes` 只有 Windows 的 stat 结果才有。
- `sentinelmesh/pi_events.py`：`DIALECT = "pi-session-v3"`；`translate(frames) -> PiTranslation`，字段 `dialect, events, frames, ignored, unknown, usage, usage_complete, assistant_messages, tool_calls, tool_errors, agent_cycles, terminal, last_stop_reason, models`；`usage` 是五个维度（`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`、`total_tokens`）的累计，不完整时也给；`terminal` 是 `"agent_settled"` 或 None；`unknown` 是未知与畸形帧的个数；`models` 是排序去重的 `(provider, model)` 元组的元组；`assistant` 事件的 payload 里这条消息的 `usage` 合法时是五维 dict、不合法时是 None。
- 夹具 `tests/fixtures/pi_native/<名>.jsonl` + `.meta.json`：三份完整（`complete: true`、`terminal_event: "agent_settled"`）、两份截断（`complete: false`、`terminal_event: null`）；`dispatcher_summary.usage` 的键是 vendor 名（`input`、`output`、`cacheRead`、`cacheWrite`、`totalTokens`）。每份不超过 400 KB；真实完整流 2–11 MB。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 28/28；全量通过；34 个故意制造的偏差里除注明等价的之外全部被 oracle 抓住（原因顺序、只看终止判完整、漏掉某个原因、不完整时不给数、`missing` 的三条口径与顺序、没有运行描述不拒绝、用默认上限、`invalid` 少 `line`/`code`、流不 `ok` 也算参考值、自动补记、不关账本、规范化证据目录、`--dialect` 有默认值或接受任意值、`models` 不排序、`frames` 按事件数、`tool_calls` 按结束计、取第一条 stopReason、`agent_end` 当终止、丢 `unknown`、丢 `recorded`、用 `usage_complete` 代替非法用量、`missing` 只看已记维度、文本模式打开、打开后不比对身份、不拒绝重解析点、`missing` 报成 `unreadable`）。
- 工具链：Windows Server 2025，`python` 3.12.10（系统安装，没有 venv）与 pytest 9.1.1；基线上 `python -m pytest -q` 因 oracle 失败而失败，其余 603 passed / 288 skipped（原生 POSIX 用例在 Windows 上跳过，这是预期）。

## Scope
- allowed_modified_files:
  - sentinelmesh/ledger_cli.py
  - sentinelmesh/adapter_run.py
- allowed_new_files:
  - tests/test_ledger_reference.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_ledger_reference_oracle.py`, `tests/test_pi_events_captured.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/cli.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有“是 str / int / bool / dict / list”都是精确类型（`type(x) is str` 等；bool 不是 int）。

1. **命令与参数**：`smctl ledger reference LEDGER --operation OP --dialect D`，由 `ledger_cli.register` 注册在 `ledger` 组下（`cli.py` 不改）。`--operation` 与 `--dialect` 都是必填；`--dialect` 用 argparse `choices`，第一版恰好只有 `"pi-session-v3"`（取 `pi_events.DIALECT`）。参数错误（缺 `LEDGER`、缺 `--operation`、缺 `--dialect`、`--dialect` 不在 choices 里，包括空串）由 argparse 以退出码 2 拒绝，账本不打开。
2. **流程与检查顺序**：经 `_with_ledger` 打开账本（打不开、`ContractError`、`LedgerError` 都是退出码 3，stderr 一行 `{"error", "detail"}`，stdout 不输出，账本文件不变，实例在 `finally` 里关闭）。`work` 里依次：
   1. `ledger.operation(OP)`（未知报 `E_OPERATION_UNKNOWN`）；
   2. `ledger.run_descriptor(OP)` 为 None → `raise ContractError("E_NOT_SUPERVISED", <一句说明>)`；
   3. `ledger.operation_usage(OP)`；
   4. `read_stream(run["evidence_dir"], max_bytes=MAX_STREAM_BYTES)`，不给 `expected`；`evidence_dir` 按记录原样使用，不规范化、不解析相对路径（相对路径相对运维的当前目录）；
   5. 流的 `status` 是 `"ok"` 时按方言算参考值（第 6 条），否则参考值为 None。

   操作处于任何状态都接受。全程不调用账本的任何变更方法，不在证据目录里写任何东西。流不是 `ok` 不是失败：退出码 0。
3. **输出**：stdout 一行 JSON（`_emit`），恰好 13 个键：`command`（`"reference"`）、`operation_id`、`request_id`、`task_id`、`state`、`generation`（都取自 `operation(OP)` 的视图）、`evidence_dir`（运行描述里记录的原字符串）、`dialect`（命令行给的值）、`reservation`（视图的 `reservation`）、`recorded`（`operation_usage(OP)` 的结果）、`missing`（第 4 条）、`stream`（第 5 条）、`reference`（第 6 条的 dict，或 None）。
4. **`missing`**：预留的维度里，按预留的顺序，列出“没有记录、或 `amount` 为 None、或 `final` 不是 True”的维度——每一维按 `_usage_incomplete` 对单个维度的口径判断。全部已知时是 `[]`。
5. **`stream`**：`{"status": …, "sha256": …, "bytes": …}` 取自 `read_stream` 的返回；`status` 为 `"invalid"` 时再加 `"line"` 与 `"code"`（同样取自返回）。不放 `events`。
6. **`pi-session-v3` 的参考值**：只用 `pi_events.translate(events)` 的结果，不重算。恰好 12 个键：
   - `complete`：bool，当且仅当 `reasons` 为空；
   - `reasons`：list，按这个顺序列出成立的项：`"no_terminal"`（`terminal` 为 None）、`"illegal_usage"`（至少一个 `assistant` 事件的 payload `usage` 为 None）、`"unknown_frames"`（`unknown > 0`）；
   - `terminal`、`last_stop_reason`、`assistant_messages`、`tool_calls`、`tool_errors`、`agent_cycles`、`frames`、`unknown`：逐项照抄 `PiTranslation` 的同名字段；
   - `usage`：`dict(translation.usage)`，五个维度，不完整时也给；
   - `models`：`[[provider, model], ...]`，顺序与 `translation.models` 相同。
7. **`read_stream` 的非 POSIX 分支**：`os` 没有 `O_NOFOLLOW` 时（判断可以在模块加载时做，`hasattr(os, "O_NOFOLLOW")`），把现有的 `os.open(..., O_NOFOLLOW | O_NONBLOCK)` 换成下面的序列，其余代码（`fstat` 后的普通文件检查、`expected` 比对、上限、读取、摘要、解析、`finally` 关闭描述符）两个分支共用、不改：
   1. `before = os.lstat(path)`：`FileNotFoundError` → `missing`；其他 `OSError` → `unreadable`；
   2. `before` 不是 `stat.S_ISREG`，或 `getattr(before, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT` 非零 → `unreadable`（不打开）；
   3. `descriptor = os.open(path, os.O_RDONLY | os.O_BINARY)`（`OSError` → `unreadable`；写成 `getattr(os, "O_BINARY", 0)` 也可以）；
   4. `fstat` 之后，除了现有的普通文件检查，再要求 `(info.st_dev, info.st_ino) == (before.st_dev, before.st_ino)`，否则 `unreadable`（描述符照常在 `finally` 里关闭）。

   有 `O_NOFOLLOW` 的平台（macOS、Linux）走原来的代码，逐字不变；`read_stream` 的签名、返回形状、`_check_max_bytes`、`MAX_STREAM_BYTES`、`DEFAULT_MAX_STREAM_BYTES`、`STREAM_NAME` 都不变；模块导入仍无副作用。
8. **兼容性**：`status`、`stop`、`settle`、`resolve`、`resume` 的行为与输出不变；`ledger_cli` 导入 `adapter_run` 与 `pi_events` 不得引入副作用（V07）；不改任何公开签名。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 2、3、5、6 节、oracle，以及 `ledger_cli.py`（`_with_ledger`、`_usage_incomplete`、`register`）、`adapter_run.py`（`read_stream`、`_parse_stream`、`_empty`）、`pi_events.py`（`PiTranslation`、`translate`）。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 7 条改 `adapter_run.read_stream`；按第 1–6 条在 `ledger_cli.py` 里加 `reference`（命令函数、参考值函数、`missing` 与 `stream` 摘要的辅助函数；私有名字自定）。
3. 新建 `tests/test_ledger_reference.py`，覆盖下列 oracle 没有逐个覆盖的用例（每个拒绝用例先验证合法基线，再只改一处；错误码与 reason 文字全等断言；账本用 `create_ledger` 在临时目录里建，流用字节写进临时证据目录，不读 `.agent/runs`）：
   - 输出恰好 13 个键，各键的值与账本视图、运行描述、`operation_usage` 一致；`generation` 是操作自己的代际（换代之后仍是旧代际）；操作处于 `ABANDONED`（`resolve_unknown` 之后）与 `REVIEW_PENDING` 时命令同样工作；
   - `missing`：没有记录、`amount` 为 None（`observe(..., None, False, ...)`）、非最终（`observe(..., 5, False, ...)`）、已知最终，各一个用例；预留按非排序的顺序给出（例如 `{"dispatches": 1, "total_tokens": …, "cache_read_tokens": …}`），断言 `missing` 按预留顺序而不是字母序；
   - `reasons`：三种原因各自单独出现的用例；两两同时出现的三对，断言顺序；三种同时出现；每种不完整情况下 `usage` 仍是合法 assistant 消息之和、`complete` 为 False；
   - `models` 去重排序成列表的列表；`agent_cycles` 数 `agent_start`；`tool_errors` 数 `isError` 为 True；`frames` 等于帧数而不是事件数（一条 assistant 消息产生两个事件）；
   - 流的每种状态各一个用例，断言 `reference` 为 None、退出码 0 与 `stream` 的形状：缺失（目录在、文件不在；目录也不在）、`unreadable`（`stdout.log` 是目录）、`too_large`（`MAX_STREAM_BYTES + 1` 字节的稀疏文件）、`invalid`（坏 JSON 行，断言 `line` 与 `code`）、非对象行（`code` 为 `E_ADAPTER_STREAM`）；超过 `DEFAULT_MAX_STREAM_BYTES` 但不超过 `MAX_STREAM_BYTES` 的合法流是 `ok`；
   - 参数：缺 `--dialect`、`--dialect` 为未知值、为空串、缺 `--operation`、缺 `LEDGER`，各退出码 2、stdout 为空；
   - 拒绝：`E_OPERATION_UNKNOWN`、`E_NOT_SUPERVISED`（一个交接时没有运行描述的操作，和一个从未交接的 `RESERVED` 操作）、`E_LEDGER_MISSING`，各退出码 3、stdout 为空、stderr 恰好 `{"error", "detail"}`、账本文件摘要不变；
   - 从不写：成功与流缺失两种情况下账本文件摘要、`seq` 与 `events` 都不变，证据目录里没有新文件；
   - 每次打开的账本都关闭了（包装 `ledger_cli.open_ledger` 留住实例，成功与拒绝各至少一次）；
   - `evidence_dir` 相对路径按记录原样输出，并相对当前目录解析（`os.chdir` 之后，`addCleanup` 恢复）；
   - `read_stream` 的非 POSIX 分支（有 `O_NOFOLLOW` 时整类跳过，理由写明）：普通文件 `ok` 且 `sha256` 是磁盘字节的摘要（CR LF 结尾）；含 0x1A 字节的流读到末尾（该行 `invalid`）；目录 `unreadable`；符号链接 `unreadable`（建不了链接时 `skipTest`）；硬链接 `ok`（建不了时 `skipTest`）；用 `mock.patch.object(adapter_run.os, "lstat", ...)` 让 `lstat` 返回带 `FILE_ATTRIBUTE_REPARSE_POINT` 的普通文件 stat → `unreadable`；返回另一个 `st_ino` 的 stat → `unreadable`；
   - 一次经 `subprocess` 跑 `python -m sentinelmesh.cli ledger reference ...`（成功一次、`E_NOT_SUPERVISED` 一次）。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_ledger_reference_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_ledger_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_adapter_run_oracle.py tests/test_pi_events.py tests/test_pi_events_oracle.py tests/test_verified_review.py tests/test_verified_review_gaps.py tests/test_verified_review_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.ledger_cli, sentinelmesh.adapter_run; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要，基线应失败）：`python -m sentinelmesh.cli ledger reference --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`--dialect`

V01、V04 在基线上失败，是因为 `reference` 子命令还不存在（argparse 退出码 2）；V02 是因为测试文件还不存在；V08 是因为 argparse 不认识这个子命令。V03 保护五个已有命令、`read_stream` 的 POSIX 分支（Windows 上其中的原生用例跳过，这是预期）、Pi 翻译与接线；V07 保护导入无副作用。

## Definition of done
- D01：命令、参数与参数错误的退出码（Behavior contract 第 1 条）；证据：V01、V02、V08。
- D02：流程、检查顺序、拒绝码与“从不写”（第 2 条）；证据：V01、V02。
- D03：输出的 13 个键、`missing` 与 `stream` 摘要（第 3–5 条）；证据：V01、V02。
- D04：`pi-session-v3` 的参考值、`reasons` 的顺序与不完整时仍给累计（第 6 条）；证据：V01、V02。
- D05：`read_stream` 的非 POSIX 分支，POSIX 分支逐字不变（第 7 条）；证据：V01、V02、V03。
- D06：兼容性与导入无副作用（第 8 条）；Step 3 的测试齐全，不自行做变异检查；证据：V03、V04、V07、review。
- D07：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
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
  - 实现约 7 分钟（两处改动合计约 120 行）；
  - 测试约 10 分钟；
  - 运行检查约 3 分钟（oracle 约 6 秒，全量约 40 秒）；
  - 执行报告约 3 分钟；
  - 合计约 29 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、规划端专用测试、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
