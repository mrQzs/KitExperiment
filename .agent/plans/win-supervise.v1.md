# TASK: win-supervise

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 7d3a3027dc8f91c927aac85744325be50f3f3a90
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/windows-supervisor.md` 第 3.5 节与第 6 节 W1–W5（用户决定第 5 节第 1、4 条）给 `sentinelmesh/windows_supervisor.py` 加上 Job Object 监督：`supervise(spec, *, environment, evidence_dir, worktree_id, allowed_secret_names, before_start)`，签名、检查顺序、钩子、证据文件、观察记录与返回的 `SupervisedRun` 都与 `posix_supervisor.supervise` 相同，机制换成：`CREATE_SUSPENDED` 创建 → 入带 `KILL_ON_JOB_CLOSE` 的 Job → `NtResumeProcess`；超时或取消直接 `TerminateJobObject`（退出码 `TERMINATED_EXIT_CODE = 0xC000013A`）；按 Job 的 `ActiveProcesses` 回收。
- 只修改 `sentinelmesh/windows_supervisor.py`（在第 2 份合同的证据一半之上追加）；
- 让规划端 oracle `tests/test_win_supervise_oracle.py` 全部通过；
- 新建执行器测试 `tests/test_win_supervise.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改平台选择：`supervised_record` 仍 `from .posix_supervisor import SupervisedRun, supervise`，`supervised_operation` 仍 `from .posix_supervisor import streams_match`（第 4 份合同再换）；`run_supervised_operation`、`run_adapter_operation` 不动。
- 不改 `windows_supervisor.py` 已有的证据一半（`_plain_regular`、`_create_evidence`、`_open_pinned`、`_open_evidence_files`、`_abandon_evidence`、`_remove_own_evidence`、`_fact_of`、`_streams_of`、`_stream_matches`、`streams_match`）的行为；只改模块文档串、导入、`__all__`，并追加新代码。
- 不改 `posix_supervisor.py`、`supervised_record.py`、`supervised_operation.py`、`process_identity.py`、`runtime.py`、`adapter_run.py`、`scheduler.py`、`core.py`、`cli.py`（V08 钉住前六个）。
- 不做 Ctrl-Break 或任何"优雅停止"；不设 `_TERM_GRACE_SECONDS`；不改 `SupervisedRun` 的字段与顺序；不改观察记录与 `Lifecycle` 的转移。
- 不读 `os.environ` 给子进程（`env=snapshot` 原样）；测试里只启动 `sys.executable -c` 的进程树；不启动真实 pi；不联网、不读取凭据。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试。

## Repository facts
- 基线 = master `349d87c`（第 2 份合同 `win-evidence` 已集成：`windows_supervisor.py` 的证据一半、`.agent/design/windows-supervisor.md` 第 9 节记录了给本合同的两条事实）+ 规划端 oracle 提交 `7d3a302`，后者只含 `tests/test_win_supervise_oracle.py`（21 个用例；原生用例带 `@unittest.skipIf(os.name != "nt", ...)`，三个表面用例在任何平台都跑；在基线上整个文件因 `from sentinelmesh.windows_supervisor import TERMINATED_EXIT_CODE, ..., supervise` 失败）。oracle 直接用这些私有名字：`windows_supervisor._win32()`、`windows_supervisor._Job(api)`（`mock.patch.object(windows_supervisor._Job, "assign_and_resume", ...)`、`mock.patch.object(windows_supervisor._Job, "active_processes", ...)`）、`windows_supervisor._start_in_job(job, spec, executable, snapshot, stdout_fd, stderr_fd)`，并用 `mock.patch.object(windows_supervisor.time, "sleep", ...)`、`mock.patch.object(windows_supervisor.os, "name", "posix")`、`mock.patch.object(subprocess.Popen, "wait", ...)` 注入——所以模块要 `import time`、`import subprocess`、`import os` 后用 `time.sleep`、`subprocess.Popen`、`os.name` 调用，不要 `from time import sleep`。
- `sentinelmesh/windows_supervisor.py`（基线，约 390 行）：模块文档串首行是 `"""Native Windows evidence handling for SentinelMesh: the half without Job Objects.`，第二段说明"不含 Job Object、不含 supervise"（本合同改写这两处）；导入 `hashlib, os, stat`、`from typing import Any, Mapping, Optional`、`from . import posix_supervisor as _posix` 与 `from .posix_supervisor import (SupervisedRun, _BLOCK_BYTES, _EVIDENCE_MODE, _STDERR_NAME, _STDOUT_NAME, _STREAM_NAMES, _close_fd, _digest_blocks, _identity_of, _valid_facts)`、`from .runtime import RuntimeErrorCode`；`__all__ = ["SupervisedRun", "streams_match"]`；`_open_evidence_files(evidence_dir)` 返回 `(stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, stdout_identity, stderr_identity)`；`_abandon_evidence(created, descriptors)` 先关后删；`_streams_of(stdout_pin, stderr_pin)`。
- `sentinelmesh/posix_supervisor.py`（基线，不改）可复用的名字：`_KILL_WAIT_SECONDS`（2.0）、`_REAP_SECONDS`（2.0）、`_REAP_POLL_SECONDS`（0.05）、`_validated_allowlist(allowed_secret_names)`、`_reject_credential_names(snapshot, allowed)`、`_validated_worktree_id(worktree_id)`、`_validated_evidence_dir(evidence_dir)`、`_observation_of(spec, state, identity, cleanup_confirmed)`、`_wait_for_leader(proc, seconds)`（等待组长、吸收 `KeyboardInterrupt`、返回是否退出）、`_close_fd`。`posix_supervisor.supervise`（约第 660–849 行）是 `supervise` 的逐行模板：检查顺序、`before_start` 的处理、`Lifecycle().transition("STARTING")` → `transition("STARTED", identity=identity)` → `transition("STOPPING")` → `transition(state)` 或 `transition("START_UNKNOWN")`、`SupervisedRun` 的 11 个位置参数顺序 `(stop_reason, exit_code, lifecycle, observation, identity, stdout_path, stderr_path, stragglers_killed, environment_names, duration_seconds, streams)`、启动失败时 10 个参数（`streams` 默认 None）。`sentinelmesh/process_identity.py`：`boot_id()`、`file_sha256(path)`、`resolve_executable(argv0, *, cwd, environment)`、`verified_environment(spec, environment)`。`sentinelmesh/runtime.py`：`InvocationSpec`（`command`、`cwd`、`timeout_seconds`、`environment_sha256`）、`Lifecycle`、`ProcessIdentity(pid, boot_id, executable_sha256, worktree_id)`、`RuntimeErrorCode`。
- Code-Kit 调度脚本里的 `_WinJob` 类已在这台机器上用同一套做法派发了九轮，其 ctypes 绑定可以照抄：`k32 = ctypes.WinDLL("kernel32", use_last_error=True)`，`CreateJobObjectW`（restype `wintypes.HANDLE`，argtypes `[c_void_p, wintypes.LPCWSTR]`）、`SetInformationJobObject`（`BOOL`；`[HANDLE, c_int, c_void_p, DWORD]`）、`QueryInformationJobObject`（`BOOL`；`[HANDLE, c_int, c_void_p, DWORD, c_void_p]`）、`AssignProcessToJobObject`（`BOOL`；`[HANDLE, HANDLE]`）、`TerminateJobObject`（`BOOL`；`[HANDLE, UINT]`）、`TerminateProcess`（`BOOL`；`[HANDLE, UINT]`）、`CloseHandle`（`BOOL`；`[HANDLE]`）；`ntdll = ctypes.WinDLL("ntdll")`，`NtResumeProcess`（restype `c_long`，argtypes `[HANDLE]`，0 表示成功）。结构体：`IoCounters` 六个 `c_ulonglong`（ReadOperationCount、WriteOperationCount、OtherOperationCount、ReadTransferCount、WriteTransferCount、OtherTransferCount）；`BasicLimit`（PerProcessUserTimeLimit `c_longlong`、PerJobUserTimeLimit `c_longlong`、LimitFlags `DWORD`、MinimumWorkingSetSize `c_size_t`、MaximumWorkingSetSize `c_size_t`、ActiveProcessLimit `DWORD`、Affinity `c_size_t`、PriorityClass `DWORD`、SchedulingClass `DWORD`）；`ExtendedLimit`（BasicLimitInformation `BasicLimit`、IoInfo `IoCounters`、ProcessMemoryLimit/JobMemoryLimit/PeakProcessMemoryUsed/PeakJobMemoryUsed 四个 `c_size_t`）；`BasicAccounting`（TotalUserTime、TotalKernelTime、ThisPeriodTotalUserTime、ThisPeriodTotalKernelTime 四个 `c_longlong`，TotalPageFaultCount、TotalProcesses、ActiveProcesses、TotalTerminatedProcesses 四个 `DWORD`）。常量：`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000`，`JobObjectExtendedLimitInformation = 9`，`JobObjectBasicAccountingInformation = 1`，`CREATE_SUSPENDED = 0x4`（`subprocess` 模块没有这个名字，自己定义）。
- Win32 事实（规划端 2026-10-11 用探针实测，Windows Server 2025、Python 3.12.10）：`subprocess.Popen(..., stdout=fd, stderr=fd, creationflags=0x4, close_fds=True)` 可用，`proc._handle` 可 `int()`；挂起状态下 `AssignProcessToJobObject` 成功、`ActiveProcesses` 为 1、`proc.poll()` 为 None；`NtResumeProcess` 返回 0；`TerminateJobObject(job, 0xC000013A)` 后 `proc.wait()` 的 `returncode` 是 `3221225786`（即 `0xC000013A`，无符号），整棵树（组长 + 孙进程）8 ms 内 `ActiveProcesses` 归 0；组长正常退出后留下的孙进程让 `ActiveProcesses` 保持 1，`TerminateJobObject` 后立即归 0；对一个只有 `[0, 1, 2]` 字节的 `.exe` 做 `Popen` 抛 `OSError`（errno 22，winerror 216）；对挂起的进程 `TerminateProcess(h, 1)` 成功、`returncode` 1、它没有写过一个字节；关闭带 `KILL_ON_JOB_CLOSE` 的 Job 句柄会结束成员；`GetProcessHandleCount(GetCurrentProcess())` 在一个完整周期（建 Job、Popen、等待、关 Job、释放 Popen）之后回到原值（Popen 的进程句柄在对象释放时关闭，所以 `supervise` 返回后不能保留对 `Popen` 的引用）；子进程的环境块恰好是传入的映射（Windows 把键名转成大写，子进程里 `os.environ` 看到 `SYSTEMROOT` 而不是 `SystemRoot`），不含 `SystemRoot` 时 Python 子进程也能启动；子进程 `print` 写出的是 CR LF。
- 设计说明第 9 节给本合同的两条事实（第 2 份合同复审 F4、F5）：`_abandon_evidence` 关掉的描述符号会被下一次 `os.open` 复用，所以 `supervise` 调用它之后必须把四个描述符变量置 None，`finally` 里才不会把别人的新描述符关掉；子进程还持有继承来的写句柄时文件删不掉，按身份删除只在钩子失败（尚无子进程）时发生。
- 规划端用一份草稿实现验证过（草稿未提交，追加约 330 行含文档串）：oracle 22/22；全量 865 passed / 289 skipped；32 个故意偏差全部被 oracle 抓住（“先恢复再入 Job”这条靠记录调用顺序的假 API 用例，真实进程树的时序分不出来；“Job 句柄不设 KILL_ON_JOB_CLOSE”这条靠直接关 Job 句柄看成员是否死掉）。oracle 提交同时把 `tests/test_win_evidence_oracle.py` 里对 `__all__` 的精确比较放宽为子集检查。

## Scope
- allowed_modified_files:
  - sentinelmesh/windows_supervisor.py
- allowed_new_files:
  - tests/test_win_supervise.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_win_supervise_oracle.py`, `tests/test_win_evidence_oracle.py`, `tests/test_win_evidence.py`, `tests/test_win_identity_oracle.py`, `tests/test_win_identity.py`, `tests/test_supervised_record.py`, `tests/test_supervised_record_gaps.py`, `tests/test_supervised_record_oracle.py`, `tests/test_supervised_operation.py`, `tests/test_supervised_operation_gaps.py`, `tests/test_supervised_operation_oracle.py`, `tests/test_supervisor_stream_facts.py`, `tests/test_supervisor_stream_facts_gaps.py`, `tests/test_supervisor_stream_facts_oracle.py`, `tests/test_posix_supervisor.py`, `tests/test_posix_supervisor_gaps.py`, `tests/test_posix_supervisor_oracle.py`, `tests/test_posix_supervisor_probes.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/core.py`, `sentinelmesh/cli.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有"是 int""是 str"都是精确类型。拒绝都是 `RuntimeErrorCode(<code>, ...)`，code 按条目给出。下列私有名字与签名由本合同固定（oracle 直接使用；第 4 份合同要调用）：`_win32()`、类 `_Job`（`__init__(self, api)`、属性 `handle`、`assign_and_resume(self, process_handle) -> bool`、`active_processes(self) -> Optional[int]`、`terminate(self, exit_code) -> None`、`terminate_process(self, process_handle) -> None`、`close(self) -> None`）、`_start_in_job(job, spec, executable, snapshot, stdout_fd, stderr_fd) -> subprocess.Popen`、`_stop_job(job, proc, stop_reason) -> str`、`_reap_job(job) -> tuple[bool, bool]`；其它私有名字自定。

1. **表面**：`__all__ == ["SupervisedRun", "TERMINATED_EXIT_CODE", "streams_match", "supervise"]`；模块常量 `TERMINATED_EXIT_CODE = 0xC000013A`；`_KILL_WAIT_SECONDS`、`_REAP_SECONDS`、`_REAP_POLL_SECONDS` 从 `posix_supervisor` 导入（oracle 比较 `windows_supervisor._REAP_SECONDS == posix_supervisor._REAP_SECONDS`），不定义 `_TERM_GRACE_SECONDS`；模块文档串改写为"证据一半 + Job Object 监督"。导入无副作用：`ctypes` 只在 `_win32()` 里导入，`_win32()` 只在 `supervise` 里（检查都通过之后、创建证据文件之前）首次调用，结果缓存在模块变量里。
2. **`_win32()`**：按 Repository facts 建绑定并返回一个对象（属性自定，例如 `k32`、`ntdll`、结构体类型、`byref`、`sizeof`、`last_error`）；第二次调用返回同一对象。
3. **`_Job(api)`**：`CreateJobObjectW(None, None)` 返回 0 → `OSError`；`ExtendedLimit().BasicLimitInformation.LimitFlags = 0x2000`，`SetInformationJobObject(handle, 9, byref(info), sizeof(info))` 失败 → `CloseHandle` 后 `OSError`；成功后 `self.handle` 是句柄。`assign_and_resume(process_handle)`：先 `AssignProcessToJobObject(self.handle, process_handle)`，失败返回 False（不恢复）；再 `NtResumeProcess(process_handle)`，返回值 `== 0` 即结果。顺序固定：**先入 Job 再恢复**。`active_processes()`：`QueryInformationJobObject(self.handle, 1, byref(acct), sizeof(acct), None)` 失败 → None，否则 `int(acct.ActiveProcesses)`。`terminate(exit_code)`：`TerminateJobObject(self.handle, exit_code)`，失败忽略。`terminate_process(process_handle)`：`TerminateProcess(process_handle, 1)`。`close()`：句柄非空时 `CloseHandle` 并置 None，可重复调用。
4. **`_start_in_job`**：`subprocess.Popen(list(spec.command), executable=executable, cwd=spec.cwd, env=snapshot, stdin=subprocess.DEVNULL, stdout=stdout_fd, stderr=stderr_fd, creationflags=0x4, close_fds=True)`；`OSError` 原样抛出；`job.assign_and_resume(int(proc._handle))` 为 False 时 `job.terminate_process(int(proc._handle))`、`_wait_for_leader(proc, _KILL_WAIT_SECONDS)`，然后抛 `OSError`。返回 `proc`。
5. **`_stop_job(job, proc, stop_reason)`**：`job.terminate(TERMINATED_EXIT_CODE)`；`"timeout"` 变成 `"timeout_killed"`，`"cancelled"` 不变；`_wait_for_leader(proc, _KILL_WAIT_SECONDS)`；返回 stop_reason。没有宽限期。
6. **`_reap_job(job)`**：`active = job.active_processes()`；`0` → `(False, True)`；None → `(False, False)`；否则 `job.terminate(TERMINATED_EXIT_CODE)`，`deadline = time.monotonic() + _REAP_SECONDS`，循环：读 `active_processes()`，`0` → `(True, True)`；None 或已过 deadline → `(True, False)`；否则 `time.sleep(min(_REAP_POLL_SECONDS, max(0.0, deadline - time.monotonic())))`；循环体里的 `KeyboardInterrupt` 吸收后 `continue`（deadline 不动）。
7. **`supervise` 的检查**：`started_at = time.monotonic()`；`os.name != "nt"` → `E_RUNTIME_PLATFORM`（第一条，在任何其它检查之前）；然后与 POSIX 相同、顺序相同、错误码相同：`type(spec) is not InvocationSpec` → `E_RUNTIME_INVOCATION`；`spec.cwd` 非绝对或不是目录 → `E_RUNTIME_INVOCATION`；`snapshot = verified_environment(spec, environment)`；`allowed = _validated_allowlist(allowed_secret_names)`；`_reject_credential_names(snapshot, allowed)`；`_validated_worktree_id(worktree_id)`；`executable = resolve_executable(spec.command[0], cwd=spec.cwd, environment=snapshot)`；`executable_sha256 = file_sha256(executable)`；`boot = boot_id()`；`evidence = _validated_evidence_dir(evidence_dir)`；`before_start` 非 None 且不可调用 → `E_RUNTIME_INVOCATION`；`api = _win32()`。这些检查之前不创建任何文件、不起任何进程。
8. **证据与钩子**：`_open_evidence_files(evidence)` 得到 8 元组；`before_start` 非 None 时调用一次；它抛出任何 `BaseException` → `_abandon_evidence(((stdout_path, stdout_identity), (stderr_path, stderr_identity)), (stderr_pin, stderr_fd, stdout_pin, stdout_fd))`，把四个描述符变量置 None，同一个异常原样 raise（外层 `finally` 对 None 不做任何事）。
9. **启动**：`environment_names = tuple(sorted(snapshot))`；`lifecycle = Lifecycle().transition("STARTING")`；`job = _Job(api)` 与 `proc = _start_in_job(...)` 在同一个 `try` 里，`OSError` → `lifecycle.transition("FAILED")`，返回 `SupervisedRun("start_failed", None, lifecycle, _observation_of(spec, "NOT_STARTED", None, False), None, stdout_path, stderr_path, False, environment_names, max(0.0, time.monotonic() - started_at))`（`streams` 为默认 None）；`finally` 里关掉两个写描述符并把变量置 None。`identity = ProcessIdentity(proc.pid, boot, executable_sha256, worktree_id)`；`lifecycle.transition("STARTED", identity=identity)`。
10. **等待与停止**：`proc.wait(timeout=spec.timeout_seconds)` → `"completed"`；`subprocess.TimeoutExpired` → `"timeout"`；`KeyboardInterrupt` → `"cancelled"`（报告，不再抛）。`lifecycle.transition("STOPPING")`；不是 `"completed"` 时 `stop_reason = _stop_job(job, proc, stop_reason)`；`stragglers_killed, cleanup_confirmed = _reap_job(job)`。
11. **收尾**：`streams = _streams_of(stdout_pin, stderr_pin)`；`exit_code = proc.returncode`；`state = "STOPPED" if stop_reason == "completed" and exit_code == 0 else "FAILED"`；`cleanup_confirmed` 为真 → `lifecycle.transition(state)`，否则 `transition("START_UNKNOWN")`；返回 `SupervisedRun(stop_reason, exit_code, lifecycle, _observation_of(spec, state, identity, cleanup_confirmed), identity, stdout_path, stderr_path, stragglers_killed, environment_names, max(0.0, time.monotonic() - started_at), streams)`。外层 `finally`：关两个 pin（`_close_fd`），`job` 非 None 时 `job.close()`。函数返回后不保留对 `proc` 的引用（局部变量即可）。
12. **兼容性**：证据一半的函数行为不变（第 2 份合同的 oracle `tests/test_win_evidence_oracle.py` 与 `tests/test_win_evidence.py` 继续通过）；不改任何公开签名；V08 列出的六个文件一字不改。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3.5、5、6、9 节、oracle，`windows_supervisor.py` 全文，`posix_supervisor.supervise` 与它用到的帮助函数，Code-Kit 事实段落里的 ctypes 绑定。基线与 Repository facts 不符就停止并报告。
2. 改 `windows_supervisor.py`：文档串、导入、`__all__`、常量、`_win32`、`_Job`、`_start_in_job`、`_stop_job`、`_reap_job`、`supervise`（第 1–11 条；追加约 300 行，文档串每个函数 2–5 行）。
3. 跑 V01 直到 oracle 全过（原生用例约 10 秒）。
4. 新建 `tests/test_win_supervise.py`，只覆盖下面这些（oracle 已经覆盖了规则的各个边界；可以沿用 oracle 的 `pid_alive`/`pid_gone_within`/`kill_pid`/`handle_count` 写法，但不要照抄 oracle 的用例；原生用例加 `@unittest.skipIf(os.name != "nt", ...)`；子进程只用 `sys.executable -c`；每个用例结束时杀掉自己启动的进程；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - 一次正常运行，子进程写 100 KB stdout（含 CR LF 与 0x1A 字节）与 10 字节 stderr → `completed`/0/`STOPPED`、`streams` 的两个 fact 与测试独立算出的四键一致、`streams_match` 为 True；先做一次热身运行，再比较 `GetProcessHandleCount` 在这次运行前后相等；
   - 三层进程树（组长 → 子 → 孙，各自 `time.sleep(60)`，组长把两个 pid 写到 stdout）超时 → `timeout_killed`、`exit_code == TERMINATED_EXIT_CODE`、`cleanup_confirmed` True、三个进程 3 秒内都不在了、`duration_seconds < timeout + 1.5`；
   - 组长留下一个孙进程后以退出码 2 结束 → `completed`/2/`FAILED`、`stragglers_killed` True、`cleanup_confirmed` True、孙进程不在了；
   - 取消（`mock.patch.object(subprocess.Popen, "wait", ...)` 第一次调用等组长写出标记文件后抛 `KeyboardInterrupt`）且组长带一个孙进程 → `cancelled`、`exit_code == TERMINATED_EXIT_CODE`、两个进程都不在了、`observation["state"] == "FAILED"`；
   - 取消 + `mock.patch.object(windows_supervisor._Job, "active_processes", return_value=1)` → `cleanup_confirmed` False、`lifecycle.state == "START_UNKNOWN"`、`reconcile(...)["replay_allowed"] is False`、`duration_seconds >= _REAP_SECONDS`；
   - 钩子先在证据目录写一个自己的文件再抛 `KeyboardInterrupt` → 同一个 `KeyboardInterrupt` 传出、`stdout.log`/`stderr.log` 不在了、钩子自己的文件原样在、句柄数不变；
   - 同一个证据目录连续两次 `supervise` → 第二次 `E_RUNTIME_EVIDENCE`，第一次的两个文件字节不变，目录里没有多出文件。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
5. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_win_supervise_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_win_supervise.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_win_evidence_oracle.py tests/test_win_evidence.py tests/test_win_identity_oracle.py tests/test_win_identity.py tests/test_supervised_record.py tests/test_supervised_record_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_oracle.py tests/test_supervisor_stream_facts_oracle.py tests/test_posix_supervisor.py tests/test_posix_supervisor_oracle.py tests/test_adapter_run.py tests/test_scheduler_supervised.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.windows_supervisor; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要）：`git diff --quiet 7d3a3027dc8f91c927aac85744325be50f3f3a90 -- sentinelmesh/posix_supervisor.py sentinelmesh/supervised_record.py sentinelmesh/supervised_operation.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/adapter_run.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04 在基线上失败，是因为 `supervise` 与 `TERMINATED_EXIT_CODE` 还不存在；V02 是因为测试文件还不存在。V03 保护证据一半、身份、记录模块与流事实的现有规则；V07 保护导入无副作用（`ctypes` 要到第一次 `supervise` 才加载）；V08 保护六个不该动的文件（`--quiet` 在有差异时退出码 1；检查命令按 argv 直接执行，不经 shell）。全量约 130 秒。

## Definition of done
- D01：表面、`_win32` 的惰性加载与缓存（第 1–2 条）；证据：V01、V07。
- D02：`_Job` 的创建、先入 Job 再恢复、成员计数、终止与关闭（第 3 条）；证据：V01、V02。
- D03：`_start_in_job`、`_stop_job`、`_reap_job`（第 4–6 条）；证据：V01、V02。
- D04：`supervise` 的检查顺序、钩子回滚、启动失败、等待与停止、收尾与返回值（第 7–11 条）；证据：V01、V02。
- D05：第 2 份合同交付的那一半行为不变、六个文件不变、导入行不变（第 12 条）；证据：V03、V04、V08、review。
- D06：Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
  - 实现约 8 分钟（ctypes 绑定按事实段落抄写，`supervise` 按 POSIX 模板改写）；
  - 测试约 6 分钟（7 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 10 秒，V03 约 15 秒，全量约 130 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
