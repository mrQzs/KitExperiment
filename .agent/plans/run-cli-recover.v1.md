# TASK: run-cli-recover

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 7f4c00fc099627334c5f6f709ddad0b20288c1a4
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/controller-cli.md` 第 3、5、6 节与第 7 节第 2 份，在 `sentinelmesh/run_cli.py` 里补上第三个一次性命令并修一处上一份复审留下的问题：
- `smctl run recover <ledger> --owner <name> [--candidate <64 hex>]`：打开账本，恰好一次 `restart_recovery.recover_after_restart(ledger, owner=..., candidate_hash=...)`，把它的报告原样作为 stdout 的 JSON；本命令不领取、不运行、不结算、不恢复请求——每个请求照旧停在 `RECOVERY_REQUIRED`，之后由 `smctl ledger settle`/`resolve`/`resume` 接手。
- `load_job` 的环境预检（上一份复审 F4）：作业文件的 `environment` 在任何领取之前先按 `process_identity.environment_digest` 的规则检查，不合法 → `E_RUN_JOB`，不再留下 RESERVED 操作。
- 约定与 `smctl ledger`、`smctl run init/once` 相同：stdout 一行 JSON、拒绝 stderr 一行 JSON（三个键）退出码 3、argparse 用法错误退出码 2、账本在 `finally` 里关闭。
- 让规划端 oracle `tests/test_run_cli_recover_oracle.py` 全部通过；新建执行器测试 `tests/test_run_cli_recover.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不做 `serve` 循环；不挂 `pi-session-v3`（第 3 份）；不启动真实 pi；不联网、不读凭据。
- 不改 `restart_recovery.py`（恢复的规则全在那里：一次 rollover、RESERVED 取消、未定案的交接按记录结算、已定案的不碰、每个请求停在 `RECOVERY_REQUIRED`）；本命令对报告不加、不减、不改、不裁定；不在命令里 `resume`、`settle`、`resolve`、`claim` 或运行任何东西。
- 不改 `init` 与 `once` 的任何已有行为（除 Goal 第 2 条的预检）；不改 `cli.py`（`run_cli.register` 已挂上整个 `run` 组，`recover` 只在 `register` 里加一个子解析器）；不改 `ledger_cli.py` 的任何命令与约定。
- 不放宽账本、调度器、桥接、恢复驱动、adapter 运行的任何守卫；不改 `durable_ledger.py`、`scheduler.py`、`supervised_operation.py`、`supervised_record.py`、`adapter_run.py`、`adapter_protocol.py`、`posix_supervisor.py`、`windows_supervisor.py`、`process_identity.py`、`runtime.py`、`restart_recovery.py`、`ledger_cli.py`、`cli.py`（V08 钉住）。
- 不读 `os.environ`；测试里只启动 `sys.executable -c` 的子进程与 `sys.executable -m sentinelmesh.cli` 的 `smctl` 进程；不修改 `tests/test_run_cli.py`、`tests/test_run_cli_oracle.py`。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试。

## Repository facts
- 基线 = master `70c3a49`（`smctl run init`/`once` 已集成，`run_cli.py` 约 430 行；`cli.py` 已有 `from . import ledger_cli, run_cli` 与 `run_cli.register(sub)`）+ 规划端 oracle 提交 `7f4c00fc099627334c5f6f709ddad0b20288c1a4`，后者只含 `tests/test_run_cli_recover_oracle.py`（6 个用例，两个类 `RecoverOracle`（5 个）与 `CrashToResumeCliOracle`（1 个），任何平台都跑，Windows 上约 4 秒；在基线上整个文件失败——`smctl run recover` 是 argparse 用法错误，环境预检不存在）。oracle 在进程内调 `cli.main(argv)`，另有真实子进程：一个 `python -m sentinelmesh.cli run once ...` 控制器被 `Popen.kill()`（Windows 上是 `TerminateProcess`，返回码 1）杀掉，再一个真实 `python -m sentinelmesh.cli run recover ...`。
- `restart_recovery.recover_after_restart(scheduler, *, owner: str, candidate_hash: Callable[[str], str]) -> dict`：先 `_checked_inputs`（`owner` 不是非空 str 或 `candidate_hash` 不可调用 → `ContractError("E_RECOVERY_INPUT")`，在 rollover 之前，账本不变），然后恰好一次 `scheduler.rollover()`（代际 +1，每个请求停止并记录 `RECOVERY_REQUIRED`，事件 `controller.fenced`），然后按 `operation_ids()` 的领取顺序：`RESERVED` → `cancel_unstarted` → `{"operation_id", "action": "cancelled", "state"}`；`HANDOFF_ACCEPTED`/`RUNNING` 且账本有运行描述 → `recover_supervised_operation` → `{"operation_id", "action": "recovered", "state"}`（`state` 是 `NOT_STARTED`/`START_UNKNOWN`/`REVIEW_PENDING` 之一）；没有描述 → `{"action": "skipped", "state", "reason": "no run descriptor"}`；某个操作抛非 `LedgerError` 的异常 → `{"action": "failed", "state", "error": <码或类型名>}` 并继续；`LedgerError` 原样抛出。返回 `{"generation": <新代际>, "operations": [<上述条目>]}`。已定案的操作（`START_UNKNOWN`、`REVIEW_PENDING`、`ACCEPTED`、`REJECTED`、`NOT_STARTED`、`CANCELLED_NOT_STARTED`、`ABANDONED`）不碰也不报告。它不结算用量、不恢复请求。
- `supervised_operation.evidence_digest(evidence_dir)` 是缺省的 `candidate_hash`；`--candidate` 给的常量用 `run_cli._constant(value)` 包成只返回常量的可调用（与 `once` 相同）。
- `process_identity.environment_digest(mapping)` 对不合法的映射抛 `RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", ...)`：实测三种 `str(exc)`："E_RUNTIME_ENVIRONMENT: environment name must not contain '='"、"E_RUNTIME_ENVIRONMENT: environment name must be non-empty text"、"E_RUNTIME_ENVIRONMENT: environment entry contains a control or non-scalar character"。基线上这些作业能通过 `load_job`，在领取之后才被 `Invocation` 构造时的 `environment_digest` 拒绝，留下一个 `RESERVED` 操作（上一份复审 F4）。
- 规划端草稿实测（Windows，真实 `smctl` 子进程）：
  - 没有在飞的操作：`recover` 输出 `{"command": "recover", "generation": 2, "operations": []}`；之后 `smctl ledger status` 的请求 `stopped: true`、`stop_reasons: ["RECOVERY_REQUIRED"]`；`once` → `E_STOPPED`（`operation_id` null，证据目录空）；再 `recover` → 代际 3、`operations: []`；`smctl ledger resume --reason RECOVERY_REQUIRED` 之后 `once` 在代际 3 得到 `REVIEW_PENDING`。
  - 一个领取后被拒（`usage_dimensions` 含未保留维度 → `E_ADAPTER_SCHEMA`）留下的 `RESERVED` 操作：`recover` → `[{"action": "cancelled", "operation_id": ..., "state": "CANCELLED_NOT_STARTED"}]`，保留量归还（`held` 0），同账本里已 `REVIEW_PENDING` 的运行不被报告也不变。
  - 被杀的控制器：`smctl run once` 的子进程（`python -c`，写下 pid 后等待一个释放文件）启动后，用 `TerminateProcess` 杀掉控制器 → Job Object 在 0.02 秒内结束子进程（Windows）；证据目录只有 `handoff.json`、`stdout.log`、`stderr.log`（没有 `observation.json`）；此时请求未停止、`held` 为保留量。另一个进程 `smctl run recover --owner owner-2` → 代际 2，`[{"action": "recovered", "state": "START_UNKNOWN"}]`；`status` 的 `attention` 里该操作 `reasons: ["start_unknown", "usage_unknown"]`；`once` → `E_STOPPED`；`resume --reason RECOVERY_REQUIRED` → `E_RESUME_UNKNOWN`；`smctl ledger settle` 一次一个维度：第一个维度结算后另一个仍未知，账本追加停止理由 `USAGE_UNKNOWN`（`stop_reasons` 成为 `["RECOVERY_REQUIRED", "USAGE_UNKNOWN"]`）；`resolve --resolution NOT_STARTED` → `E_RESOLVE_CONFLICT`（账本有运行描述），`ABANDONED` 成功；只点名 `RECOVERY_REQUIRED` 的 `resume` → `E_STOP_REASON_CHANGED`，两个理由都点名才成功；之后 `once`（同一任务，新幂等键，`--owner owner-2`）→ `REVIEW_PENDING`、代际 2。
  - 一个交接后记录完整但没定案的操作（`HANDOFF_ACCEPTED`，账本有描述，两份记录都在）：`recover` → `recovered`/`REVIEW_PENDING`，事件 `scheduler.recovered` 的 payload 有 `outcome: "FINISHED"`、`state`、`owner`（命令行的）、`from_generation: 1`、`candidate`（缺省是 `evidence_digest(evidence_dir)`，给了 `--candidate` 就是它）；恢复不结算用量，所以 `resume` 先 `E_RESUME_USAGE_UNKNOWN`，两个维度 `settle` 后点名两个理由才能恢复。记录之后 `stdout.log` 被改写 → `recovered`/`START_UNKNOWN`，payload 没有 `candidate`。
  - 拒绝：账本不存在 → `E_LEDGER_MISSING`；`--owner ""` → `E_RECOVERY_INPUT`，账本字节与代际都不变；缺 `--owner`、`--candidate` 不是 64 个小写十六进制、多余参数 → 退出码 2。
  - 草稿 oracle 6/6；相关套件（两个 run-cli、两个 ledger-cli、restart-recovery、request-resume）全过；全量见下。
- 执行器测试 `tests/test_run_cli.py`（上一份的 6 个用例）在本合同的预检之后仍须全过（V03）。

## Scope
- allowed_modified_files:
  - sentinelmesh/run_cli.py
- allowed_new_files:
  - tests/test_run_cli_recover.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_run_cli_recover_oracle.py`, `tests/test_run_cli_oracle.py`, `tests/test_run_cli.py`, `tests/test_ledger_cli.py`, `tests/test_ledger_cli_oracle.py`, `tests/test_restart_recovery.py`, `tests/test_request_resume_oracle.py`, `sentinelmesh/cli.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定（oracle 只通过 `cli.main` 与真实子进程驱动；执行器测试可以用私有名字）：`run_cli.cmd_recover(args)`；`run_cli.recover_after_restart` 必须是模块级名字（`from .restart_recovery import recover_after_restart`），`cmd_recover` 通过这个名字调用，以便测试打补丁；`load_job`、`RunSpecError`、`_constant`、`_candidate`、`_refuse`、`_emit` 沿用上一份。

1. **挂接与约定**：`register` 在 `once` 之后加子命令 `recover`：位置参数 `ledger`，必填 `--owner`，可选 `--candidate`（类型 `_candidate`，64 个小写十六进制）；没有 `--now`、没有 `--evidence`。缺 `--owner`、`--candidate` 不合法、多余参数都是 argparse 用法错误（退出码 2，stdout 空，账本不打开）。`smctl run --help` 退出码 0 并列出 `init`、`once`、`recover`。成功：stdout 恰好一行 JSON（`_emit`），stderr 空，退出码 0。拒绝：stderr 恰好一行 JSON，键恰好 `{"error", "detail", "operation_id"}`（`_refuse`），`operation_id` 总是 None——本命令不领取；stdout 空；退出码 3。
2. **`recover` 的顺序**：`open_ledger(ledger)`（失败 → 它的码，如 `E_LEDGER_MISSING`）；`candidate_hash` 是 `--candidate` 给的常量（`_constant`），缺省 `supervised_operation.evidence_digest`；然后恰好一次 `recover_after_restart(ledger, owner=<--owner 原样>, candidate_hash=<上述>)`。它抛出的 `ContractError`/`LedgerError` → 拒绝（码与 detail 照抄；`E_RECOVERY_INPUT` 发生在 rollover 之前，账本不变）。它返回 → 输出 `{"command": "recover", "generation": report["generation"], "operations": report["operations"]}`：`operations` 列表的顺序与每个条目的键值原样，不增删改任何条目，不再读、不再写账本。`KeyboardInterrupt` 不捕获。账本在 `finally` 关闭。本命令调用的账本变更方法只有 `recover_after_restart` 内部的那些：不 `claim`、不 `resume`、不 `settle`、不 `resolve_unknown`、不运行任何子进程。
3. **环境预检（F4）**：`load_job` 在现有的"`environment` 是 str→str 对象"检查之后、`adapter_id` 检查之前，恰好一次 `environment_digest(environment)`；`RuntimeErrorCode` → `RunSpecError("E_RUN_JOB", "environment: " + str(exc))`（detail 以 `environment: ` 开头，后接 `str(exc)`）。因此 `once` 对这类作业在打开账本之前就拒绝：`operation_id` None，账本字节不变，证据目录空。合法的环境经预检后 `load_job` 的返回值不变（预检不改映射）。
4. **文档串**：模块文档串把 `recover` 写成第三个命令（一次 rollover、RESERVED 取消、未定案的交接按记录结算、报告原样、每个请求停在 `RECOVERY_REQUIRED`、之后由 `smctl ledger settle`/`resolve`/`resume` 接手），删掉"下一份合同"的说法；`cmd_recover` 的文档串说明第 2 条的顺序、`failed`/`skipped` 条目留给下一次调用或操作员、本命令不领取。导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 1–3、5–7 节、oracle，`run_cli.py` 全文、`restart_recovery.recover_after_restart` 的文档串与实现、`ledger_cli.cmd_resolve/cmd_resume`（约定）、`process_identity.environment_digest`。基线与 Repository facts 不符就停止并报告。
2. 改 `run_cli.py`（第 1–4 条；`cmd_recover` 连文档串约 40 行，预检约 8 行，`register` 约 7 行）。
3. 跑 V01 直到 oracle 全过（约 5 秒）。
4. 跑 `python -m sentinelmesh.cli run --help` 与 `python -m pytest -q tests/test_run_cli_oracle.py tests/test_run_cli.py` 确认 `init`/`once` 未受影响。
5. 新建 `tests/test_run_cli_recover.py`，只覆盖下面这些（oracle 已覆盖空账本的 rollover、RESERVED 的取消、按记录结算到 REVIEW_PENDING/START_UNKNOWN、候选来源、拒绝与用法错误、预检的三种环境、被杀控制器的闭环；可以沿用 oracle 的 `smctl`/`ok`/`refused`/`emit` 写法，但不要照抄 oracle 的用例；子进程只用 `sys.executable -c` 与 `sys.executable -m sentinelmesh.cli`；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - 进程内 `recover`：一个领取后被拒留下的 `RESERVED` 操作加一个 `REVIEW_PENDING` 的运行，报告恰好一条 `cancelled`，之后 `smctl ledger status` 的代际为 2、`stop_reasons == ["RECOVERY_REQUIRED"]`、`held` 为 0，`smctl ledger events --type controller.fenced` 恰好一条；
   - `unittest.mock.patch("sentinelmesh.run_cli.recover_after_restart")` 捕获调用：恰好一次，`owner` 是命令行给的，`candidate_hash(dir)` 在 `--candidate` 下返回命令行的值、缺省下等于 `evidence_digest(dir)`（对一个真实的空目录调用比较）；返回的报告（含一个 `failed` 条目与一个 `skipped` 条目）原样出现在 stdout 的 JSON 里；
   - 同样打补丁让它抛 `LedgerError("E_LEDGER_CLOSED", "x")` 与 `ContractError("E_RECOVERY_INPUT")`：退出码 3、stderr 一行三键、`operation_id` None、码照抄；
   - `load_job` 直接调用：合法作业的返回值与上一份相同；`environment={"A=B": "x"}` → `RunSpecError`，`.code == "E_RUN_JOB"`，`.detail` 以 `environment: ` 开头并含 `E_RUNTIME_ENVIRONMENT`；
   - 真实 `smctl` 子进程：新建账本上 `run recover --owner owner-2` → 退出码 0、stdout 恰好一行、`{"command": "recover", "generation": 2, "operations": []}`、stderr 空；再 `run recover` 缺 `--owner` → 退出码 2。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_recover_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_recover.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py tests/test_restart_recovery.py tests/test_request_resume_oracle.py tests/test_win_wiring_oracle.py tests/test_win_wiring.py tests/test_adapter_run.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -m sentinelmesh.cli run --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`recover`
- V08（必要）：`git diff --quiet 7f4c00fc099627334c5f6f709ddad0b20288c1a4 -- sentinelmesh/cli.py sentinelmesh/ledger_cli.py sentinelmesh/durable_ledger.py sentinelmesh/scheduler.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/restart_recovery.py tests/test_run_cli.py tests/test_run_cli_oracle.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为 `smctl run recover` 还不存在、预检还没有；V02 是因为测试文件还不存在。V03 保护 `init`/`once` 与它们的两个测试文件、`smctl ledger`、重启恢复与请求恢复、Windows 接线、adapter 运行；V08 保护十三个不该动的模块与上一份的两个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 170 秒。

## Definition of done
- D01：`recover` 的挂接、用法错误、成功与拒绝的形状（第 1 条）；证据：V01、V07。
- D02：`recover` 的顺序——先开账本、候选来源、恰好一次 `recover_after_restart`、报告原样、拒绝照抄、不领取不运行不恢复（第 2 条）；证据：V01、V02。
- D03：环境预检在任何领取之前，`E_RUN_JOB`，detail 以 `environment: ` 开头（第 3 条）；证据：V01、V02、V03。
- D04：被杀的控制器 → `recover` → `settle`/`resolve`/`resume` → 再 `once` 的闭环以产品命令成立（oracle 最后一个用例）；证据：V01。
- D05：`init`/`once`/`smctl ledger` 不受影响，十三个模块与两个测试文件不变，导入无副作用，文档串按第 4 条；证据：V03、V04、V08、review。
- D06：Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
- 时间分配（上限 30 分钟，按 19 分钟规划、留 11 分钟余量）：
  - 阅读约 4 分钟；
  - 实现约 5 分钟（`cmd_recover`、预检、`register`、文档串）；
  - 测试约 6 分钟（5 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 5 秒，V03 约 40 秒，全量约 170 秒）；
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
