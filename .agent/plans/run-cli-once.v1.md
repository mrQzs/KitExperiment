# TASK: run-cli-once

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: f5f1de551d79c907f2d382c0780e27ae24829a1b
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/controller-cli.md` 第 3、5、6 节与第 7 节第 1 份，新建 `sentinelmesh/run_cli.py` 并挂为 `smctl run`：
- `smctl run init <ledger> --spec <request.json>`：按严格校验的请求规格文件创建账本并种子请求与任务；
- `smctl run once <ledger> --job <job.json> --evidence <dir> --worktree-id <id> --owner <name> [--candidate <64 hex>] [--now <n>]`：恰好一次 `claim`、恰好一次 `run_adapter_operation`（第一份只认 `offline` adapter），把 outcome 作为 stdout 的 JSON；
- 约定与 `smctl ledger` 相同：stdout 一行 JSON、拒绝 stderr 一行 JSON 退出码 3、argparse 用法错误退出码 2、账本在 `finally` 里关闭。
- 让规划端 oracle `tests/test_run_cli_oracle.py` 全部通过；新建执行器测试 `tests/test_run_cli.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不做 `recover`（第 2 份合同）；不做 `serve` 循环；不挂 `pi-session-v3`（第 3 份）；不启动真实 pi；不联网、不读凭据。
- 不改 `ledger_cli.py` 的任何命令与约定（只复用它的 `_write_line`）；不放宽账本、调度器、桥接、adapter 运行的任何守卫；不改 `durable_ledger.py`、`scheduler.py`、`supervised_operation.py`、`supervised_record.py`、`adapter_run.py`、`adapter_protocol.py`、`posix_supervisor.py`、`windows_supervisor.py`、`process_identity.py`、`runtime.py`、`restart_recovery.py`、`ledger_cli.py`（V08 钉住）。
- `cli.py` 只加一行导入与一行 `run_cli.register(sub)`；`main` 的异常处理不改。
- 不读 `os.environ` 给子进程；测试里只启动 `sys.executable -c` 的子进程与 `sys.executable -m sentinelmesh.cli` 的 `smctl` 进程。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试。

## Repository facts
- 基线 = master `91f2b49`（Windows 监督器四份合同已集成；`smctl ledger` 八个命令在 `ledger_cli.py`）+ 规划端 oracle 提交 `f5f1de551d79c907f2d382c0780e27ae24829a1b`，后者只含 `tests/test_run_cli_oracle.py`（17 个用例，两个类 `InitOracle`、`OnceOracle`，任何平台都跑：子进程是 `python -c`，在 Windows 上经 Job Object 监督器、在 POSIX 上经进程组监督器；在基线上整个文件因 `smctl run` 不存在而失败——`cli.main(["run", ...])` 是 argparse 用法错误）。oracle 在进程内调用 `cli.main(argv)`（重定向 stdout/stderr，捕获 `SystemExit`），另有一个用例用真实子进程 `python -m sentinelmesh.cli run once ...`（cwd 仓库根）。
- `sentinelmesh/cli.py`（基线）：`from . import ledger_cli`；`build_parser()` 末尾 `ledger_cli.register(sub)` 然后 `return parser`；`main(argv)` 调 `args.func(args)`，任何 `Exception` 打印 `error: ...` 退出码 2。
- `sentinelmesh/ledger_cli.py` 可复用：`_write_line(stream, payload)`（一行 `json.dumps(payload, ensure_ascii=False, sort_keys=True)`，写 UTF-8 字节到 `stream.buffer`，没有 buffer 时写文本）。它的 `_refuse` 只写 `{"error", "detail"}`；本合同的拒绝多一个 `operation_id` 键（第 2 条）。
- `durable_ledger`：`create_ledger(path, *, generation=1)` 对已存在的路径（文件或目录）抛 `LedgerError("E_LEDGER_EXISTS", path)`；`open_ledger(path)` 对不存在的路径抛 `LedgerError("E_LEDGER_MISSING", ...)`，只重放从不创建；`DurableScheduler` 有 `claim`、`seed_request`、`seed_task`、`operation(op)`、`operation_ids()`、`operation_usage(op)`、`run_descriptor(op)`、`events`、`generation`、`seq`、`close()`。`ContractError`/`LedgerError` 都有 `.code` 与 `.detail`（detail 可能是 None）；`AdapterError` 有 `.code`，`RuntimeErrorCode` 有 `.code`，两者没有 `.detail`。
- `Scheduler.claim(request, task, plan, key, owner, generation, guard_version, reservation, resource_keys, now=0, lease=30)` 返回 `{"operation_id", "state", "replayed"}`；同一请求同一幂等键、同一意图再领取 → `replayed: True`（并不报错），意图不同 → `ContractError("E_IDEMPOTENCY_CONFLICT")`；未知请求或任务 → `E_SCOPE_UNKNOWN`；`guard_version` 不等于任务的 → `E_GUARD_STALE`；`reservation` 没有 `dispatches: 1` → `E_CLAIM_SHAPE`；预算维度名不匹配 `[a-z][a-z0-9_-]{0,63}` → `E_BUDGET_DIMENSION`；同一任务已有活动操作 → `E_ACTIVE_TASK`；请求已停止 → `E_STOPPED`。
- `adapter_run.run_adapter_operation(scheduler, adapter, invocation, *, cwd, timeout_seconds, environment, owner, generation, evidence_dir, worktree_id, candidate_hash, usage_dimensions=(), allowed_secret_names=frozenset(), now=0, max_stream_bytes=DEFAULT_MAX_STREAM_BYTES, max_line_bytes=DEFAULT_MAX_LINE_BYTES)` 返回 11 键的 outcome：`operation_id, state, decision, error, stop_reason, exit_code, stream, result, result_error, usage_recorded, usage_skipped`（全部可 JSON 序列化）。它在交接之前就拒绝的情况直接抛出：`AdapterError`（如 `usage_dimensions` 含未保留的维度 → `E_ADAPTER_SCHEMA`）、`ContractError`、`RuntimeErrorCode`。运行时拒绝（相对 cwd、凭据名、坏程序）不是异常而是 outcome（`state == "NOT_STARTED"`，`error` 给码）。`DEFAULT_MAX_STREAM_BYTES = 1_048_576`、`MAX_STREAM_BYTES = 64 MiB`、`DEFAULT_MAX_LINE_BYTES = 1_048_576`、`MAX_LINE_BYTES = 8 MiB`。
- `adapter_protocol.Invocation(request_id, run_id, role, profile_id, command, capabilities, environment_sha256, adapter_id)`；`OfflineAdapter.adapter_id == "offline"`；`process_identity.environment_digest(mapping)`；`supervised_operation.evidence_digest(evidence_dir)`。
- 规划端草稿实测（Windows，真实 `smctl` 子进程）：`init` 输出 `{"command": "init", "generation": 1, "ledger": ..., "request_id": ..., "seq": 1 + 任务数, "tasks": [...]}`；再 `init` 同路径 → `E_LEDGER_EXISTS` 退出码 3；`once` 一次 → `REVIEW_PENDING`、用量 `{"tokens": "APPLIED", "cost": "APPLIED"}`、证据目录四个文件；同一作业再 `once` → `E_RUN_CLAIM_REPLAYED`（stderr 带 `operation_id`）。一次 `too_large` 的运行让请求因用量未知而停止（之后的领取 `E_STOPPED`），这是账本既有规则。草稿 oracle 16/16；全量 925 passed / 281 skipped（含 oracle 当时的 16 个用例）；29 条故意偏差里 27 条被 oracle 抓住，2 条等价或不可观察（文件上限由读取长度兜底，同样以 E_RUN_SPEC 拒绝；账本在成功路径上未关闭从进程外看不出来，复审核对）。

## Scope
- allowed_modified_files:
  - sentinelmesh/cli.py
- allowed_new_files:
  - sentinelmesh/run_cli.py
  - tests/test_run_cli.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_run_cli_oracle.py`, `tests/test_ledger_cli.py`, `tests/test_ledger_cli_oracle.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有"是 int""是 str"都是精确类型（`bool` 不是 int，`float` 不是 int）。下列名字由本合同固定（oracle 只通过 `cli.main` 与真实子进程驱动，不直接用私有名字；执行器测试可以用）：`run_cli.register(subparsers)`、`run_cli.cmd_init`、`run_cli.cmd_once`、`run_cli.load_spec(path)`、`run_cli.load_job(path)`、异常类 `run_cli.RunSpecError(code, detail)`（`.code`、`.detail`）；其它私有名字自定。

1. **挂接与约定**：`cli.py` 加 `from . import ledger_cli, run_cli` 与 `run_cli.register(sub)`（在 `ledger_cli.register(sub)` 之后、`return parser` 之前）。`smctl run` 是一个命令组，子命令 `init` 与 `once`；缺子命令、缺必填选项、多余参数、`--candidate` 不是 64 个小写十六进制、`--now` 不是十进制数字串都是 argparse 用法错误（退出码 2，stdout 空），账本不打开、文件不创建。`smctl run --help` 退出码 0 并列出两个子命令。成功：stdout 恰好一行 JSON（用 `ledger_cli._write_line` 的格式：`sort_keys`、`ensure_ascii=False`），stderr 空，退出码 0。
2. **拒绝的形状**：stderr 恰好一行 JSON，键恰好是 `{"error", "detail", "operation_id"}`：`error` 是稳定的码，`detail` 是 str 或 None（账本/调度器异常给什么就写什么；`AdapterError`/`RuntimeErrorCode` 没有 detail 时用 `str(exc)`），`operation_id` 是本次调用已经领取到的操作 id，没有领取则 None；stdout 空；退出码 3。
3. **规格文件（`init --spec`）**：读取最多 65536 字节，超过 → `E_RUN_SPEC`；不是 UTF-8 JSON → `E_RUN_SPEC`；必须是对象，键恰好 `{"request_id", "repo_id", "caps", "concurrency", "tasks"}`：`request_id`、`repo_id` 非空 str；`caps` 非空对象，值全是 int 且 ≥ 0；`concurrency` int ≥ 1；`tasks` 非空列表，每项键恰好 `{"task_id", "caps", "max_attempts"}`（同样的规则，`max_attempts` ≥ 1），`task_id` 不重复。任何违反 → `E_RUN_SPEC`，`detail` 说明是哪一项。文件不存在或读不了 → `E_RUN_SPEC`。恰好 65536 字节的文件要能读。
4. **`init` 的顺序**：先完整校验规格；再在一个内存 `Scheduler()` 上 `seed_request`/`seed_task` 一遍（调度器的拒绝如 `E_BUDGET_DIMENSION` 在任何文件存在之前报出，码照抄）；然后 `create_ledger(ledger)`（已存在 → `E_LEDGER_EXISTS`）；再在账本上同样种子；输出 `{"command": "init", "ledger": <命令行给的路径原样>, "request_id", "tasks": [task_id 按文件顺序], "generation": ledger.generation, "seq": ledger.seq}`。账本在 `finally` 关闭。任何拒绝都不留下文件。
5. **作业文件（`once --job`）**：同样的读取规则，码 `E_RUN_JOB`；必填键恰好 `{"request_id", "task_id", "plan", "idempotency_key", "guard_version", "reservation", "resources", "adapter_id", "role", "profile_id", "command", "capabilities", "cwd", "environment", "allowed_secret_names", "timeout_seconds", "usage_dimensions"}`，可选键 `max_stream_bytes`、`max_line_bytes`（缺省 `DEFAULT_MAX_STREAM_BYTES`/`DEFAULT_MAX_LINE_BYTES`）；文本键非空 str；`plan`、`guard_version`、`timeout_seconds` int ≥ 1；`reservation` 非空对象、值 int ≥ 0；`resources`、`capabilities`、`allowed_secret_names`、`usage_dimensions` 是 str 列表且不重复；`command` 非空 str 列表（可重复）；`environment` 是 str→str 对象（可以为空）；`adapter_id` 必须在 `{"offline"}` 里；两个上限 int ≥ 1 且分别 ≤ `MAX_STREAM_BYTES`、`MAX_LINE_BYTES`（等于时接受）。违反 → `E_RUN_JOB`。
6. **`once` 的顺序**：先校验作业（任何 `E_RUN_JOB` 在打开账本之前）；`open_ledger`（失败 → 它的码，如 `E_LEDGER_MISSING`）；`candidate_hash` 是 `--candidate` 给的常量，缺省 `supervised_operation.evidence_digest`；然后恰好一次 `ledger.claim(request_id, task_id, plan, idempotency_key, owner, ledger.generation, guard_version, reservation, list(resources), now)`：调度器拒绝 → 它的码，`operation_id` None，不运行；`replayed` 为 True → `E_RUN_CLAIM_REPLAYED`，`operation_id` 是被重放的那个，`detail` 含它的 state，不运行；否则构造 `Invocation(request_id, operation_id, role, profile_id, tuple(command), tuple(capabilities), environment_digest(environment), adapter_id)`，调 `run_adapter_operation(ledger, OfflineAdapter(), invocation, cwd=cwd, timeout_seconds=..., environment=environment, owner=owner, generation=ledger.generation, evidence_dir=--evidence 原样, worktree_id=--worktree-id, candidate_hash=..., usage_dimensions=tuple(usage_dimensions), allowed_secret_names=frozenset(allowed_secret_names), now=now, max_stream_bytes=..., max_line_bytes=...)`。它抛出的 `AdapterError`/`ContractError`/`LedgerError`/`RuntimeErrorCode` → 拒绝，`operation_id` 是已领取的 id（操作留在账本里，通常 `RESERVED`）。它返回 → 成功输出 `{"command": "once", "operation_id", "outcome": <原样>, "operation": ledger.operation(operation_id)}`，退出码 0，哪怕 outcome 的 `state` 是 `NOT_STARTED`/`START_UNKNOWN`。`KeyboardInterrupt` 不捕获（`finally` 仍关账本）。账本在 `finally` 关闭。
7. **子进程看到的东西**：子进程的环境恰好是作业的 `environment`（Windows 会把键名转成大写，这是平台行为）、cwd 是作业的 `cwd`、argv 是 `command[1:]`；`os.environ` 一个键都不传。凭据形状的名字不在 `allowed_secret_names` 里 → 监督器拒绝，outcome `NOT_STARTED` + `E_RUNTIME_ENVIRONMENT`（命令本身成功）。
8. **模块文档串**：说明三个一次性命令（`recover` 下一份）、每次调用至多一次领取一次运行、约定来自 `smctl ledger`、子进程看到的一切都在作业文件里。导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 1–3、5、6 节、oracle，`ledger_cli.py`（约定与 `_write_line`、`_with_ledger`）、`cli.py`、`adapter_run.run_adapter_operation` 的签名与文档串、`durable_ledger.create_ledger/open_ledger`、`Scheduler.claim`。基线与 Repository facts 不符就停止并报告。
2. 新建 `run_cli.py`（第 1–8 条；连文档串约 300 行）；改 `cli.py` 两行。
3. 跑 V01 直到 oracle 全过（约 10 秒）。
4. 跑 `python -m sentinelmesh.cli run --help` 与 `python -m pytest -q tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py` 确认 `smctl ledger` 未受影响。
5. 新建 `tests/test_run_cli.py`，只覆盖下面这些（oracle 已覆盖两个文件的逐项校验、顺序、重放、交接后的拒绝、上限与超时、默认上限；可以沿用 oracle 的 `smctl`/`ok`/`refused`/`emit` 写法，但不要照抄 oracle 的用例；子进程只用 `sys.executable -c` 与 `sys.executable -m sentinelmesh.cli`；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - `load_spec`/`load_job` 直接调用：合法文件返回与文件内容相同的字典（可选键缺省补上 `DEFAULT_MAX_STREAM_BYTES`/`DEFAULT_MAX_LINE_BYTES`），`RunSpecError` 的 `.code`/`.detail` 形状，`detail` 提到出错的键名；
   - `init` 两个请求规格文件先后 `init` 到两个账本，`smctl ledger status` 各自只看到自己的请求；
   - `once` 之后 `smctl ledger status` 的 `attention` 为空、`known` 用量等于流里的数；`smctl ledger events` 能按操作 id 列出 `run.started` 与 `run.finished_for_review`；
   - `once --owner someone-else`：账本里该操作的 `owner` 是命令行给的名字，`run.started` 事件的 `process_identity` 与 `observation.json` 里的一致，`worktree_id` 是 `--worktree-id` 给的；
   - 作业的 `command` 指向一个会以退出码 2 结束但流完整的子进程 → outcome `REVIEW_PENDING`、`exit_code == 2`、用量 APPLIED、`operation` 的 state 与账本一致；
   - 真实 `smctl` 子进程：`init` 拒绝（规格文件缺键）时 stdout 空、stderr 一行 JSON、退出码 3、账本文件不存在。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_run_cli.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py tests/test_ledger_reference.py tests/test_ledger_events.py tests/test_win_wiring_oracle.py tests/test_win_wiring.py tests/test_restart_recovery.py tests/test_request_resume_oracle.py tests/test_adapter_run.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -m sentinelmesh.cli run --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`once`
- V08（必要）：`git diff --quiet f5f1de551d79c907f2d382c0780e27ae24829a1b -- sentinelmesh/ledger_cli.py sentinelmesh/durable_ledger.py sentinelmesh/scheduler.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/restart_recovery.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为 `smctl run` 还不存在；V02 是因为测试文件还不存在。V03 保护 `smctl ledger` 与它的两个读方法、账本引用与事件命令、Windows 接线、重启恢复与请求恢复、adapter 运行；V08 保护十二个不该动的文件（检查命令按 argv 直接执行，不经 shell）。全量约 160 秒。

## Definition of done
- D01：挂接、用法错误、成功与拒绝的形状（第 1–2 条）；证据：V01、V07。
- D02：规格文件的校验与 `init` 的顺序——先校验、先在内存调度器上排练、再创建、任何拒绝不留文件（第 3–4 条）；证据：V01、V02。
- D03：作业文件的校验与 `once` 的顺序——先校验、再开账本、恰好一次领取、重放不运行、交接后的拒绝带操作 id、outcome 原样输出（第 5–6 条）；证据：V01、V02。
- D04：子进程看到的恰好是作业文件给的（第 7 条）；证据：V01。
- D05：`smctl ledger` 不受影响，十二个文件不变，导入无副作用（第 8 条与 Non-goals）；证据：V03、V04、V08、review。
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
- 时间分配（上限 30 分钟，按 22 分钟规划、留 8 分钟余量）：
  - 阅读约 4 分钟；
  - 实现约 8 分钟（两个文件的校验按第 3、5 条逐字实现，两个命令按第 4、6 条）；
  - 测试约 6 分钟（6 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 10 秒，V03 约 40 秒，全量约 160 秒）；
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
