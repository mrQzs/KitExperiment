# TASK: posix-supervisor

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 897809bc14a0b842fdd2161f1749683267bd1a55
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
新增 `sentinelmesh/posix_supervisor.py`，提供 macOS（POSIX）原生进程监督器 `supervise()`：
- 只用调用方显式给出、并与 `InvocationSpec` 摘要一致的环境启动进程；凭据类变量名只有在白名单里才放行；
- 输出写入独占创建的证据文件；
- 超时与取消时对整个进程组升级到 SIGKILL，正常结束后回收残留组员；
- 返回生命周期记录和 `reconcile` 能直接消费的观察记录；
- 让规划端 oracle `tests/test_posix_supervisor_oracle.py` 全部通过。

## Non-goals
- 不修改 `sentinelmesh/runtime.py`、`sentinelmesh/process_identity.py` 或其他已有模块；不接入 adapter、scheduler 或旧的 `SubprocessRunner`。
- 不实现 Linux、Windows 原生验证（保持 NOT_RUN）；不检测用 `setsid` 逃出进程组的后代（已知边界，见 Repository facts）。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml` 和 oracle 测试；不联网、不读取凭据、不启动真实 provider CLI。

## Repository facts
- 基线 `897809b` = master `cba305f` + 规划端 oracle（11 个真实进程用例）。规划端用一份草稿实现验证过 oracle 全部通过、全量 256 passed；草稿未提交。
- 设计与用户决定见 `.agent/design/posix-runtime-runner.md`：
  - 第 2 节：方案；
  - 第 5 节：macOS 先行；不继承环境；凭据类变量默认拒绝、白名单放行，证据只记名称。
- 可用的已有接口：
  - `sentinelmesh.process_identity`：`verified_environment`、`resolve_executable`、`file_sha256`、`boot_id`，以及字符规则辅助函数 `_scalar_text`；
  - `sentinelmesh.runtime`：`InvocationSpec`、`ProcessIdentity`、`Lifecycle`（`transition` 每次 generation+1）、`RuntimeErrorCode`、`reconcile`。
- 规划端实测（macOS 15.7.9）：
  - `/usr/bin/env` 输出的正是 execve 收到的环境；
  - Python 子进程会自己加上 `LC_CTYPE`（PEP 538）与 `__CF_USER_TEXT_ENCODING`；
  - `/usr/bin/python3` 是 Xcode 转发程序，还会加 `SDKROOT` 等变量。

  监督器只保证传给 execve 的环境。
- 已知边界：调用 `os.setsid()` 的后代离开了进程组，`killpg` 看不到它；输出写到文件而不是管道，所以它不会让监督器挂起，但也不会被回收。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files: 无
- allowed_new_files:
  - sentinelmesh/posix_supervisor.py
  - tests/test_posix_supervisor.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `sentinelmesh/process_identity.py`, `tests/test_posix_supervisor_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **公开接口**
   - frozen dataclass `SupervisedRun`，字段及顺序为：`stop_reason`、`exit_code`、`lifecycle`、`observation`、`identity`、`stdout_path`、`stderr_path`、`stragglers_killed`、`environment_names`、`duration_seconds`。
   - `supervise(spec, *, environment, evidence_dir, worktree_id, allowed_secret_names=frozenset()) -> SupervisedRun`。
   - 模块级常量：`_TERM_GRACE_SECONDS = 1.0`、`_KILL_WAIT_SECONDS = 2.0`、`_REAP_SECONDS = 2.0`、`_CREDENTIAL_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH")`。
2. **校验顺序**：按下列顺序逐条检查，第一条不满足即抛出，此时不创建任何文件、不启动任何进程。
   1. `os.name != "posix"` → `E_RUNTIME_PLATFORM`；
   2. `type(spec) is not InvocationSpec` → `E_RUNTIME_INVOCATION`；
   3. `spec.cwd` 不是绝对路径，或不是已存在的目录 → `E_RUNTIME_INVOCATION`；
   4. `snapshot = verified_environment(spec, environment)`，它的错误原样冒出；
   5. `allowed_secret_names` 的类型必须恰好是 `frozenset` 或 `set`，每个元素的类型恰好是 `str`，否则 `E_RUNTIME_ENVIRONMENT`；
   6. `snapshot` 中任一名称的 `upper()` 含有 `_CREDENTIAL_MARKERS` 中的子串、且不在 `allowed_secret_names` 里 → `E_RUNTIME_ENVIRONMENT`；
   7. `worktree_id` 的类型必须恰好是 `str`、非空，并满足 `_scalar_text`，否则 `E_RUNTIME_IDENTITY`；
   8. `executable = resolve_executable(spec.command[0], cwd=spec.cwd, environment=snapshot)`，然后 `executable_sha256 = file_sha256(executable)`、`boot = boot_id()`，它们的错误原样冒出；
   9. `evidence_dir` 的类型必须恰好是 `str`，且是绝对路径、已存在的目录，否则 `E_RUNTIME_EVIDENCE`。
3. **证据文件**
   - 在 `evidence_dir` 中先后用 `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)` 创建 `stdout.log` 与 `stderr.log`。任一失败都抛 `E_RUNTIME_EVIDENCE`；创建 `stderr.log` 失败时，先关闭并删除刚创建的 `stdout.log`。
   - 已存在的文件永不被覆盖或删除。
   - 路径以 `os.path.join(evidence_dir, 名称)` 返回。
4. **启动**
   - 生命周期从 `Lifecycle()` 转到 `STARTING`（generation 1）。
   - 调用 `subprocess.Popen(list(spec.command), executable=executable, cwd=spec.cwd, env=snapshot, stdin=subprocess.DEVNULL, stdout=<stdout 描述符>, stderr=<stderr 描述符>, start_new_session=True, close_fds=True)`。
   - 无论 Popen 返回还是抛出，监督器都关闭自己持有的两个描述符。
   - Popen 抛出 `OSError` 时返回：
     - `stop_reason="start_failed"`、`exit_code=None`、`identity=None`、`stragglers_killed=False`；
     - 生命周期 `STARTING → FAILED`（generation 2）；
     - 观察记录恰好是 `{"run_id", "environment_sha256", "state": "NOT_STARTED"}` 三键。
5. **身份**：启动成功后 `identity = ProcessIdentity(proc.pid, boot, executable_sha256, worktree_id)`，生命周期带 identity 转到 `STARTED`（generation 2）。
6. **等待与停止**
   - `proc.wait(timeout=spec.timeout_seconds)`：正常返回为 `"completed"`；`TimeoutExpired` 为 `"timeout"`；`KeyboardInterrupt` 为 `"cancelled"`，不重新抛出。
   - 随后生命周期转到 `STOPPING`（generation 3）。
   - 非 completed 时按下面的顺序停止：
     1. 对进程组发 SIGTERM（ProcessLookupError 忽略），最多等组长 `_TERM_GRACE_SECONDS`；
     2. 组长仍未退出时对进程组发 SIGKILL，`"timeout"` 改为 `"timeout_killed"`（`"cancelled"` 保持不变），再最多等组长 `_KILL_WAIT_SECONDS`。
7. **残留回收**（所有启动成功的路径）：
   - 组长结束后若 `os.killpg(pgid, 0)` 显示组仍存在（PermissionError 视为存在），对进程组发 SIGKILL，`stragglers_killed=True`；
   - 然后每隔不超过 0.05 秒轮询一次，最多 `_REAP_SECONDS`，直到 `os.killpg(pgid, 0)` 抛出 ProcessLookupError，此时 `cleanup_confirmed=True`，否则为 False。
8. **结果**
   - `state = "STOPPED"`：当 `stop_reason == "completed"` 且 `exit_code == 0` 时；其余启动成功的情形 `state = "FAILED"`。
   - 生命周期在 cleanup 确认时转到 `state`，否则转到 `START_UNKNOWN`（generation 4）。
   - 观察记录恰好五键：`{"run_id": spec.run_id, "environment_sha256": spec.environment_sha256, "state": state, "process_identity": {"pid", "boot_id", "executable_sha256", "worktree_id"}, "cleanup_confirmed": 布尔值}`。
   - `exit_code` 为 `proc.returncode`；`environment_names` 为 `tuple(sorted(snapshot))`；`duration_seconds` 为非负浮点数。
9. **不泄露值**：环境变量的值不出现在 `SupervisedRun` 的任何字段或 repr 中，只记名称。
10. **导入无副作用**：导入模块不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 2 与第 5 节、oracle 测试，以及 `process_identity.py` 与 `runtime.py` 中会用到的接口。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 新建 `sentinelmesh/posix_supervisor.py`。
3. 新建 `tests/test_posix_supervisor.py`，覆盖下列 oracle 没有覆盖的用例。每个拒绝用例都先验证合法基线，再只改一处并断言具体 code；每个用例都清理自己启动的进程组。
   - 校验顺序：相邻两条规则同时违反时取前一条的 code，至少覆盖下列三组：
     - spec 类型与环境不符；
     - 相对 cwd 与缺失的证据目录；
     - 凭据名称与空 worktree_id。
   - 用补丁把 `os.name` 改成 `"nt"` 时抛 `E_RUNTIME_PLATFORM`；
   - `stderr.log` 已存在而 `stdout.log` 不存在时抛 `E_RUNTIME_EVIDENCE`，并且没有留下 `stdout.log`；
   - `allowed_secret_names` 含非 str 元素时抛 `E_RUNTIME_ENVIRONMENT`；白名单里的名称不在环境中时照常运行；
   - 凭据名称按大写子串判定：`my_Token` 与 `MONKEY` 都被拒绝；
   - 不泄漏描述符：在若干次运行（含 start_failed）前后，`os.listdir("/dev/fd")` 的数量相同；
   - cleanup 未确认的白盒路径：用补丁让 `os.killpg(pgid, 0)` 一直成功，其他信号照常发送。结果是：`cleanup_confirmed` 为 False、生命周期为 `START_UNKNOWN`（generation 4）、`reconcile` 的 `replay_allowed` 为 False；
   - 每条路径的生命周期 generation：completed 为 4，start_failed 为 2；
   - 允许的凭据值在 `repr(run)` 与 `run.observation` 中都不出现。
4. 运行 V01–V06。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_posix_supervisor_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_posix_supervisor.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V03（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V04（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V05（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V06（必要，基线应失败）：`python -c "import sys, sentinelmesh.posix_supervisor; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V03、V06 在基线上失败，是因为模块还不存在；V02 在基线上失败，是因为测试文件还不存在。

## Definition of done
- D01：校验顺序、证据文件与启动（Behavior contract 第 1–5 条）；证据：V01、V02。
- D02：等待、停止、残留回收与结果（第 6–8 条）；证据：V01、V02。
- D03：不泄露环境值、导入无副作用（第 9、10 条）；证据：V01、V02、V06。
- D04：Step 3 的测试齐全，不自行做变异检查；证据：V02、review。
- D05：已有测试不退化，所有修改在允许集合内，无依赖变更；证据：V03、V04、V05、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 中的实测事实与本机不符。
- 必须修改允许范围之外的文件（包括 oracle、`process_identity.py`、`runtime.py`）才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 4 分钟；
  - 实现约 8 分钟；
  - 测试约 10 分钟；
  - 运行检查约 2 分钟（oracle 约 6 秒，全量约 45 秒）；
  - 执行报告约 4 分钟；
  - 合计约 28 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、oracle 测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
