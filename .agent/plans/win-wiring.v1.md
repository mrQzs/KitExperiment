# TASK: win-wiring

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 17503e9afc4d4b360393e311f9d8748095f6c6e7
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/windows-supervisor.md` 第 3.1 节、第 6 节 W11–W12 与第 7 节第 4 份，把 Windows 监督器接进上层：
- `sentinelmesh/supervised_record.py` 按平台选监督器——新公开函数 `native_supervisor()`（`os.name == "nt"` 返回 `windows_supervisor` 模块，`"posix"` 返回 `posix_supervisor` 模块，其它平台 `E_RUNTIME_PLATFORM`），模块级 `supervise(...)` 原样委托给它选中的模块；`record_supervised_run` 照旧通过模块级名字 `supervise` 调用。
- `sentinelmesh/supervised_operation.py` 的 `streams_match` 同样按平台委托（模块级函数，`_stream_reason` 与 `_recorded_streams_match` 照旧按名字调用它）。
- 观察记录里的文件身份（`device`、`inode`）超出 canonical JSON 的整数范围（`core.MAX_SAFE_INTEGER = 2**53 - 1`）时按 W11 写成十进制文本、读回成整数；范围内的记录字节一个不变。
- 让规划端 oracle `tests/test_win_wiring_oracle.py` 全部通过；新建执行器测试 `tests/test_win_wiring.py`（清单见 Step 5，刻意不长）；去掉两处在 Windows 上已实测通过的平台守卫（第 5 条）。

## Non-goals
- 不改 `posix_supervisor.py`、`windows_supervisor.py`、`adapter_run.py`、`process_identity.py`、`runtime.py`、`scheduler.py`、`restart_recovery.py`、`core.py`（V08 钉住这八个）；`run_supervised_operation`、`run_adapter_operation`、`recover_after_restart` 一行不改。
- 不改记录的 schema 标签（`sentinelmesh-handoff-v1`、`sentinelmesh-observation-v2`）、键集合、`_MAX_RECORD_BYTES`、`_RECORD_MODE`、写入与发布方式；不改 `SupervisedRun`；不改 `supervised_operation.__all__`。
- 不改第 5 条之外的任何平台守卫，不改其它已有测试、oracle、夹具；不动旧 `SubprocessRunner`。
- 不读 `os.environ` 给子进程；测试里只启动 `sys.executable -c` 的进程树；不启动真实 pi；不联网、不读取凭据。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试。

## Repository facts
- 基线 = master `37b71ef`（第 3 份合同 `win-supervise` 已集成，`windows_supervisor.supervise` 可用，设计说明第 9 节记录了给本合同的事实）+ 规划端 oracle 提交 `17503e9afc4d4b360393e311f9d8748095f6c6e7`，后者只含 `tests/test_win_wiring_oracle.py`（20 个用例：`SelectionOracle` 6 个与 `RecordIdentityOracle` 4 个在任何平台都跑，`WindowsWiringOracle` 10 个带 `@unittest.skipIf(os.name != "nt", ...)`；在基线上整个文件因 `from sentinelmesh.supervised_record import native_supervisor` 失败）。oracle 直接用这些私有名字：`supervised_record._record_identity`、`_recorded_identity`、`_write_record`、`_handoff_content`、`_observation_content`；`supervised_operation._stream_reason`、`_recorded_streams_match`；并用 `mock.patch.object(supervised_record.os, "name", ...)`、`mock.patch.object(windows_supervisor, "supervise", ...)`、`mock.patch.object(posix_supervisor, "streams_match", ...)`、`mock.patch.object(supervised_record, "supervise", ...)`、`mock.patch.object(supervised_operation, "streams_match", ...)`、`mock.patch.object(windows_supervisor._Job, "active_processes", ...)` 注入——所以两个模块要 `import os` 后在调用时读 `os.name`，`record_supervised_run` 要通过模块级名字 `supervise` 调用，`_stream_reason`/`_recorded_streams_match` 要通过模块级名字 `streams_match` 调用，`native_supervisor()` 要在每次调用时查 `os.name`、不缓存结果。
- `sentinelmesh/supervised_record.py`（基线）：第 59 行 `from .posix_supervisor import SupervisedRun, supervise`；第 58 行 `from .core import canonical_json, strict_json_loads`；模块文档串首行 `"""Durable start records and the replay gate for the native POSIX supervisor.`，其中一句 `Linux and Windows Job Object supervision stay NOT_RUN.`；`replay_decision` 与 `recorded_streams` 的文档串各提到一次 `sentinelmesh.posix_supervisor.streams_match`；`_observation_content` 用 `_copy_streams(streams)` 填 `"streams"`；`_validated_streams(value, observation)` 逐项检查后 `return value`；`_read_record` 里 `_validated_streams(parsed["streams"], observation)` 的返回值没有被使用；`recorded_streams` 末尾 `return _copy_streams(observation["streams"])`。没有 `__all__`。
- `sentinelmesh/supervised_operation.py`（基线）：第 70 行 `from .posix_supervisor import streams_match`；`from .supervised_record import (HANDOFF_NAME, OBSERVATION_NAME, record_supervised_run, recorded_streams, replay_decision)`；`__all__ = ["evidence_digest", "run_supervised_operation", "recover_supervised_operation"]`；`_stream_reason` 里 `if streams_match(evidence_dir, streams):`，`_recorded_streams_match` 里 `return streams_match(evidence_dir, recorded)`；模块文档串首行 `"""Bridge between the native POSIX supervisor and the ``Scheduler`` ledger.`。
- `sentinelmesh/core.py`：`MAX_SAFE_INTEGER = 2**53 - 1`；`canonical_json` 对绝对值更大的整数抛 `ProtocolError("E_INTEGER_RANGE")`（`_write_record` 把它转成 `E_RUNTIME_EVIDENCE`）；`strict_json_loads` 对超过 16 位或超出范围的整数同样拒绝，所以这样的数既写不进记录也读不出来。
- 实测（规划端 2026-10-11，Windows Server 2025、Python 3.12.10）：这台机器临时目录所在卷的 `st_dev` 是 7562699644344144196、一个新文件的 `st_ino` 是 22517998137671556，都大于 `2**53`（NTFS 文件引用号高 16 位是序列号；Python 3.12 的 `st_dev` 是 64 位卷序列号），而系统盘上 `python.exe` 的 `st_ino` 是 281474977126949（在范围内）。因此基线上 `record_supervised_run` 在 Windows 上跑完一个真实子进程后写 `observation.json` 失败（`E_RUNTIME_EVIDENCE: observation.json is not canonical JSON: E_INTEGER_RANGE`）——这就是 W11 的来由。两个监督器各自在对方平台上第一条就 `E_RUNTIME_PLATFORM`；`windows_supervisor` 导入不加载 `ctypes`。
- 守卫实测（规划端在草稿工作副本上把 14 个 POSIX 守卫文件的守卫临时去掉后逐个运行）：`tests/test_restart_recovery.py::RecoverAfterRestartRuns`（3 个用例，经 `record_supervised_run`/`run_supervised_operation` 真实运行）与 `tests/test_verified_review_gaps.py::VerifiedReviewGaps`（2 个用例）在 Windows 上不改一字就全过；其余守卫类都依赖 POSIX 夹具（非 `.exe` 的坏程序让 `resolve_executable` 先以 `E_RUNTIME_IDENTITY` 拒绝、`os.killpg`/`SIGKILL` 清理、`-signal.SIGKILL` 退出码、FIFO、`os.fork`/`setsid` 逃逸）——保留守卫（W12），它们的 Windows 对应用例在 oracle 与本合同的执行器测试里。
- 规划端草稿实测（接线 + W11 之后）：`record_supervised_run` 用 `python -c` 子进程在 Windows 上写出两份记录，`recorded_streams` 等于 `run.streams`、`streams_match` 为 True；`run_adapter_operation` 用桩 adapter 记录用量（`{"tokens": "APPLIED", "cost": "APPLIED"}`，`evidence_hash` 等于 `stdout.log` 的 sha256）；`max_stream_bytes` 取流长度时 `ok`、少一字节时 `too_large`，`max_line_bytes=40` 时 `invalid`（第 1 行，`E_JSON_LIMIT`）；三层进程树超时 → `timeout_killed`、退出码 3221225786、`REVIEW_PENDING`、孙进程 3 秒内消失；坏 `.exe` → `NOT_STARTED`/`start_failed`；取消 → `cancelled`、3221225786、`REVIEW_PENDING`；`_Job.active_processes` 注入为 1 → `START_UNKNOWN`（`CLEANUP_UNCONFIRMED`）；同账户进程在 `mark_running` 里追加 `stderr.log` → `CLEANUP_UNCONFIRMED`，换成另一个文件 → `START_UNKNOWN`；持久账本上的 `recover_after_restart` 把记录完整的运行恢复成 `REVIEW_PENDING`、`stderr.log` 被换过的恢复成 `START_UNKNOWN`，重开账本后状态不变；一次热身之后 `GetProcessHandleCount` 在一次 `run_adapter_operation` 前后相等。草稿 oracle 20/20；全量 894 passed / 289 skipped（含 oracle 的 20 个）；25 条故意偏差全部被 oracle 抓住。

## Scope
- allowed_modified_files:
  - sentinelmesh/supervised_record.py
  - sentinelmesh/supervised_operation.py
  - tests/test_restart_recovery.py
  - tests/test_verified_review_gaps.py
- allowed_new_files:
  - tests/test_win_wiring.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_win_wiring_oracle.py`, `tests/test_win_supervise_oracle.py`, `tests/test_win_supervise.py`, `tests/test_win_evidence_oracle.py`, `tests/test_win_evidence.py`, `tests/test_win_identity_oracle.py`, `tests/test_win_identity.py`, `tests/test_supervised_record.py`, `tests/test_supervised_record_gaps.py`, `tests/test_supervised_record_oracle.py`, `tests/test_supervised_operation.py`, `tests/test_supervised_operation_gaps.py`, `tests/test_supervised_operation_oracle.py`, `tests/test_supervisor_stream_facts.py`, `tests/test_supervisor_stream_facts_gaps.py`, `tests/test_supervisor_stream_facts_oracle.py`, `tests/test_adapter_run.py`, `tests/test_adapter_run_oracle.py`, `tests/test_verified_review.py`, `tests/test_verified_review_oracle.py`, `tests/test_restart_recovery_oracle.py`, `tests/test_request_resume_oracle.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/core.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/cli.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有"是 int""是 str"都是精确类型（`bool` 不是 int）。拒绝都是 `RuntimeErrorCode(<code>, ...)`，code 按条目给出。下列名字与签名由本合同固定（oracle 直接使用）：`supervised_record.native_supervisor()`、`supervised_record.supervise(...)`、`supervised_record._record_identity(value)`、`supervised_record._recorded_identity(value)`、`supervised_record._record_streams(streams)`、`supervised_operation.streams_match(evidence_dir, streams)`、`supervised_operation.native_supervisor`；其它私有名字自定。

1. **`supervised_record` 的表面**：导入改成 `from . import posix_supervisor, windows_supervisor`、`from .posix_supervisor import SupervisedRun`（不再从它导入 `supervise`）、`from .core import MAX_SAFE_INTEGER, canonical_json, strict_json_loads`、`from types import ModuleType`；导入无副作用（`ctypes` 不加载）。`native_supervisor() -> ModuleType`：`os.name == "nt"` 返回 `windows_supervisor` 模块对象，`os.name == "posix"` 返回 `posix_supervisor` 模块对象，其它值 `E_RUNTIME_PLATFORM`；每次调用都读 `os.name`，不缓存。模块级 `supervise(spec, *, environment, evidence_dir, worktree_id, allowed_secret_names=frozenset(), before_start=None)`：`return native_supervisor().supervise(spec, environment=environment, evidence_dir=evidence_dir, worktree_id=worktree_id, allowed_secret_names=allowed_secret_names, before_start=before_start)`——同一批对象原样传过去（不复制 `environment`，不替换钩子或允许集合），自己不做任何检查、不捕获任何异常。`record_supervised_run` 的 `run = supervise(...)` 这一行不改（它必须经过模块级名字）。
2. **`supervised_operation` 的表面**：删除 `from .posix_supervisor import streams_match`；从 `.supervised_record` 多导入一个 `native_supervisor`；新增模块级 `streams_match(evidence_dir, streams)`：`return native_supervisor().streams_match(evidence_dir, streams)`（原样传参）；`_stream_reason` 与 `_recorded_streams_match` 的调用行不改；`__all__` 不改；导入无副作用。
3. **记录里的文件身份（W11）**：模块常量 `_DECIMAL_DIGITS = frozenset("0123456789")`、`_IDENTITY_LIMIT = 2**64 - 1`。`_record_identity(value)`：`abs(value) <= MAX_SAFE_INTEGER` 时返回 `value` 本身，否则返回 `str(value)`。`_recorded_identity(value)`：`type(value) is int` → 返回 `value`；`type(value) is str` 且 16 ≤ 长度 ≤ 21、可选的一个前导 `-`、其余全是 `_DECIMAL_DIGITS` 里的字符（`str.isdigit` 不算数，全角数字要拒绝）、首位数字不是 `0`、且 `MAX_SAFE_INTEGER < abs(int(value)) <= _IDENTITY_LIMIT` → 返回 `int(value)`；其它一切（含 `bool`、`float`、`None`、带 `+`、带空格、带小数点、范围内整数的文本、超过 64 位的文本）→ `None`。即每个值恰好一种写法：范围内的数永远是 JSON 数字，范围外的数永远是最短十进制文本。
4. **写与读**：`_record_streams(streams)`：`None` → `None`；否则返回新字典，`stdout`/`stderr` 各自 `None` 保持 `None`，事实复制成新字典，`device`、`inode` 经 `_record_identity`，`bytes`、`sha256` 原样；`_observation_content` 的 `"streams"` 改用 `_record_streams(streams)`（`_copy_streams` 保留给 `recorded_streams`）。`_validated_streams(value, observation)` 的前两段检查不变（未启动只接受 `None`；启动了必须覆盖两个流），逐流改成：`type(fact) is not dict or frozenset(fact) != _FACT_KEYS` → `E_RUNTIME_EVIDENCE`；`device = _recorded_identity(fact["device"])` 为 `None` → `E_RUNTIME_EVIDENCE`（设备号可以为负）；`inode = _recorded_identity(fact["inode"])` 为 `None` 或 `< 0` → `E_RUNTIME_EVIDENCE`；`fact["bytes"]` 不是 int 或 `< 0` → `E_RUNTIME_EVIDENCE`（字节数不走文本形式）；`sha256` 检查不变；返回新字典 `{name: None | {"device": device, "inode": inode, "bytes": ..., "sha256": ...}}`。`_read_record` 改成 `parsed["streams"] = _validated_streams(parsed["streams"], observation)`（先按原样比较完 canonical 字节再解码），于是 `recorded_streams` 返回的 `device`/`inode` 永远是 int，和监督器自己的事实、`os.lstat` 直接可比。范围内身份的记录字节与基线完全相同。
5. **去守卫**：删除 `tests/test_restart_recovery.py` 里 `class RecoverAfterRestartRuns(RecoveryCase):` 上方那一行 `@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")`，以及 `tests/test_verified_review_gaps.py` 里 `class VerifiedReviewGaps(unittest.TestCase):` 上方的同一行装饰器；这两个文件除此之外一个字符不改（V09 核对 `git diff --numstat` 恰好各为删 1 行、加 0 行）。
6. **文档串**：`supervised_record` 的模块文档串首行改为 `"""Durable start records and the replay gate for the native supervisor.`，`Linux and Windows Job Object supervision stay NOT_RUN.` 这句改成只说 Linux 仍 NOT_RUN，并加一段说明平台选择（`native_supervisor`：nt 是 Job Object、posix 是进程组、其它平台 `E_RUNTIME_PLATFORM`，两边同一套检查、证据文件与 `SupervisedRun`）；`replay_decision`、`recorded_streams` 文档串里的 `sentinelmesh.posix_supervisor.streams_match` 改为"所选平台监督器的 `streams_match`"之类的措辞；`_copy_streams` 的文档串说明它的输入是 `_validated_streams` 已解码的映射；`supervised_operation` 模块文档串首行改为 `"""Bridge between the native supervisor and the ``Scheduler`` ledger.`。措辞自定，但不得再说 Windows Job Object 是 NOT_RUN。
7. **兼容性**：V08 列出的八个文件一字不改；POSIX 守卫下的所有测试不改；`tests/test_win_*` 的既有测试继续通过；`record_supervised_run`、`replay_decision`、`recorded_streams`、`run_supervised_operation`、`recover_supervised_operation`、`run_adapter_operation`、`recover_after_restart` 的签名与返回形状不变。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3.1、6、9 节、oracle，`supervised_record.py` 与 `supervised_operation.py` 全文，`windows_supervisor.supervise`/`streams_match` 与 `posix_supervisor.supervise`/`streams_match` 的签名。基线与 Repository facts 不符就停止并报告。
2. 改 `supervised_record.py`（第 1、3、4、6 条）与 `supervised_operation.py`（第 2、6 条）；新增代码连文档串约 110 行。
3. 跑 V01 直到 oracle 全过（原生用例约 5 秒）。
4. 按第 5 条删两行装饰器；跑 `python -m pytest -q tests/test_restart_recovery.py tests/test_verified_review_gaps.py` 确认 Windows 上全过。
5. 新建 `tests/test_win_wiring.py`，只覆盖下面这些（oracle 已经覆盖了选择规则、身份编码的各个边界与主要的端到端路径；可以沿用 oracle 的 `kill_pid`/`pid_gone_within`/`emit`/`stream_bytes` 写法，但不要照抄 oracle 的用例；原生用例加 `@unittest.skipIf(os.name != "nt", ...)`；子进程只用 `sys.executable -c`；每个用例结束时杀掉自己启动的进程；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - `record_supervised_run`：证据目录里事先放一个 `handoff.json` → 抛 `E_RUNTIME_EVIDENCE`、没有 `run.started`（没有进程启动）、`stdout.log`/`stderr.log` 已被回滚删除、目录里只剩那个预先放的文件且字节不变；
   - `run_supervised_operation`：子进程写两个流后 `sys.exit(3)` → `REVIEW_PENDING`、`stop_reason == "completed"`、`exit_code == 3`、`decision` 是 `("FAILED", True)`、`run.finished_for_review` 恰好一次、观察记录里的 `process_identity` 与 `run.started` 事件一致；
   - `run_adapter_operation` + 拒绝流的 adapter（`AdapterError("E_ADAPTER_SOURCE", ...)`）→ `REVIEW_PENDING`、`result is None`、`result_error == "E_ADAPTER_SOURCE"`、`usage_recorded == {}`、`usage_skipped == ["tokens", "cost"]`，账本里没有 `usage.observed` 事件；
   - `run_adapter_operation` + `mock.patch.object(windows_supervisor._Job, "active_processes", lambda self: 1)` 且流完整 → `START_UNKNOWN`（`scheduler.start_unknown` 的 reason 是 `CLEANUP_UNCONFIRMED`）、`stream["status"] == "ok"` 但 `usage_recorded == {}`、`usage_skipped` 两个维度、持有的资源仍在、请求已 stopped；
   - `run_supervised_operation` 的环境里有 `SERVICE_API_TOKEN` 且不在允许集合 → `error == "E_RUNTIME_ENVIRONMENT"`、`NOT_STARTED`、证据目录为空、没有 `run.started`；同一规格把该名字放进 `allowed_secret_names` 后 `REVIEW_PENDING`；
   - `run_adapter_operation`：子进程写完完整的流后 `time.sleep(30)`，`timeout_seconds=1` → `stop_reason == "timeout_killed"`、`exit_code == TERMINATED_EXIT_CODE`（3221225786）、`REVIEW_PENDING`、`usage_recorded == {"tokens": "APPLIED", "cost": "APPLIED"}`、子进程 3 秒内消失、整个调用用时小于 8 秒。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V09。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_win_wiring_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_win_wiring.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_win_supervise_oracle.py tests/test_win_supervise.py tests/test_win_evidence_oracle.py tests/test_win_evidence.py tests/test_win_identity.py tests/test_restart_recovery.py tests/test_verified_review_gaps.py tests/test_supervised_record_oracle.py tests/test_supervised_operation_oracle.py tests/test_adapter_run.py tests/test_scheduler_supervised.py tests/test_ledger_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.supervised_record, sentinelmesh.supervised_operation, sentinelmesh.adapter_run, sentinelmesh.restart_recovery; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要）：`git diff --quiet 17503e9afc4d4b360393e311f9d8748095f6c6e7 -- sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/adapter_run.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/scheduler.py sentinelmesh/restart_recovery.py sentinelmesh/core.py` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V09（必要，基线应失败）：`python -c "import subprocess; rows = [line.split()[:2] for line in subprocess.run(['git', 'diff', '--numstat', '17503e9afc4d4b360393e311f9d8748095f6c6e7', '--', 'tests/test_restart_recovery.py', 'tests/test_verified_review_gaps.py'], capture_output=True, text=True, check=True).stdout.splitlines()]; assert rows == [['0', '1'], ['0', '1']], rows; print('GUARDS_ONLY_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`GUARDS_ONLY_OK`

V01、V04 在基线上失败，是因为 `native_supervisor` 还不存在；V02 是因为测试文件还不存在；V09 是因为两行守卫还在。V03 保护三份已集成合同的规则、两个去守卫文件在 Windows 上的通过、记录与桥接模块的既有 oracle（在 Windows 上被跳过，但要能导入）、流的上限与账本引用；V07 保护导入无副作用；V08 保护八个不该动的文件；V09 保护去守卫只删那两行（`--numstat` 每个文件一行 `加 删 路径`；检查命令按 argv 直接执行，不经 shell）。全量约 150 秒。

## Definition of done
- D01：`native_supervisor` 与模块级 `supervise` 的平台选择、原样委托、不缓存（第 1 条）；证据：V01、V07。
- D02：`supervised_operation.streams_match` 的委托与按名字调用（第 2 条）；证据：V01。
- D03：文件身份的编码与解码——范围内是数字、范围外是最短十进制文本、每个值恰好一种写法、读回是整数（第 3–4 条）；证据：V01。
- D04：Windows 原生端到端——记录、桥接、adapter 路径、流上限、恢复（oracle 的 `WindowsWiringOracle` 与 Step 5 的测试）；证据：V01、V02。
- D05：两行守卫去掉后两个文件在 Windows 上通过且只改这两行（第 5 条）；证据：V03、V09。
- D06：文档串不再把 Windows Job Object 说成 NOT_RUN，八个文件不变，既有测试不变（第 6–7 条）；证据：V04、V08、review。
- D07：Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
- 时间分配（上限 30 分钟，按 22 分钟规划、留 8 分钟余量）：
  - 阅读约 4 分钟；
  - 实现约 6 分钟（两个模块的委托与身份编码按第 1–4 条逐字实现，文档串按第 6 条）；
  - 去守卫与验证约 1 分钟；
  - 测试约 7 分钟（6 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 5 秒，V03 约 30 秒，全量约 150 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
