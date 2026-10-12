# TASK: serve-loop

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 88e7f14134bc493de72bbbfbc1b0ba20758b5aed
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/serve-loop.md`（用户决定第 5 节四项均选推荐项；规划端决定第 6 节）给 `smctl run` 加第四个子命令 `serve`：按种子顺序走一遍请求的任务，每个能领的任务用它自己的作业文件领取一次、运行一次（与 `once` 的链逐字相同），stdout 永远一行报告；让规划端 oracle `tests/test_run_cli_serve_oracle.py`（13 个用例）全部通过；新建执行器测试 `tests/test_run_cli_serve.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改 `load_spec`、`load_job`（精确键集合被测试钉住）、`cmd_init`、`cmd_once`、`cmd_recover`、`_refuse`、`_emit`、`_ADAPTERS`、`_JOB_*`：serve 只是组合它们。幂等键的派生只在 serve 里，`once` 仍按作业文件里的键原样领取。
- 不改 `Scheduler`、`durable_ledger`、`ledger_cli`、`adapter_run`、`supervised_*`、`restart_recovery`、`cli.py`（`cli.main` 已经调用 `run_cli.register`，不用动）。不加账本事件或记录：账本里看到的与手敲 n 次 `once` 完全相同。
- 不做守护进程、轮询等待复审、重试、并发（一次只有一个 LIVE 操作，请求的 `concurrency` 不用）；serve 不调用 `recover`、`verdict`、`resume`。
- 不联网、不读凭据、不启动真实 pi（`pi` adapter 与 `once` 同一张注册表照常可选，oracle 只用 `offline`）。

## Repository facts
- 基线 = master `e5b2d17`（设计说明）+ 规划端 oracle 提交 `88e7f14134bc493de72bbbfbc1b0ba20758b5aed`，后者含新文件 `tests/test_run_cli_serve_oracle.py`（13 个用例，一个类 `ServeOracle`，Windows 上约 7 秒；基线上 13 个全失败——没有 `serve` 子命令，argparse 退出 2）。oracle 在进程内调 `cli.main(argv)`，另有真实子进程 `python -m sentinelmesh.cli run serve …` / `run recover`，以及一个被 `kill()` 的 serve 控制器；用 `smctl run init`（三个任务，`max_attempts: 2`）、`run once`、`ledger stop/resume/verdict` 布置账本。oracle 以 `mock.patch.object(run_cli, "_clock", …)` 在领取之间抛 `KeyboardInterrupt`，以 `mock.patch.object(run_cli, "open_ledger", …)` 换一个 `claim` 会重放的账本替身：`cmd_serve` 必须经模块级名字 `_clock()` 读时钟、经模块级名字 `open_ledger` 开账本（不要在函数里 `from … import` 或提前绑定）。
- `sentinelmesh/run_cli.py`（523 行）：`cmd_once` 约第 345 行是整条链的样板（`load_job` → `open_ledger` → `ledger.claim(request, task, plan, key, owner, ledger.generation, guard_version, reservation, list(resources), now)` → `replayed` 检查 → `Invocation(request, operation_id, role, profile_id, tuple(command), tuple(capabilities), environment_digest(environment), adapter_id)` → `run_adapter_operation(ledger, _ADAPTERS[adapter_id](), invocation, cwd=…, timeout_seconds=…, environment=…, owner=…, generation=ledger.generation, evidence_dir=…, worktree_id=…, candidate_hash=…, usage_dimensions=tuple(…), allowed_secret_names=frozenset(…), now=…, max_stream_bytes=…, max_line_bytes=…)` → `finally: ledger.close()`）；`_refuse(exc, operation_id)` 约第 279 行（`ContractError`/`LedgerError`/`RunSpecError` 带 `.code`/`.detail`，其它取 `str(exc)`）；`_constant` 约第 468 行，`_digits` 约第 473 行（非负整数的 argparse 类型），`_candidate` 约第 485 行；`register` 约第 492 行把 `init`/`once`/`recover` 挂到 `run` 命令组。模块只 `import argparse, json, re, sys`：serve 还需要 `os` 与 `time`。
- `Scheduler.claim` 的拒绝顺序（`scheduler.py` 约第 780 行）：幂等重放（同键同指纹直接 `return {..., "replayed": True}`，同键不同指纹 `E_IDEMPOTENCY_CONFLICT`，都在一切规则之前）→ `E_FENCED` → `E_SCOPE_UNKNOWN` → `E_STOPPED` → `E_GUARD_STALE` → `E_TASK_DONE` → `E_ACTIVE_TASK`（同任务有 LIVE 或 `REVIEW_PENDING` 的操作）→ `E_ATTEMPTS` → `E_CONCURRENCY` → 预算 → `E_RESOURCE_BUSY`。幂等索引以 `(request, key)` 为键，跨任务共用。只读视图 `ledger.task_ids(request)`（种子顺序；未知请求 `E_REQUEST_UNKNOWN`）与 `ledger.task(request, task)`（四键 `task_id`、`max_attempts`、`attempts`、`done`；`attempts` 含取消与未启动的领取）。`ledger.operation(id)` 给 `task_id`、`state`、`owner`、`generation`。
- 运行的 outcome（`run_adapter_operation` 的返回）有 `state`（`REVIEW_PENDING`、`NOT_STARTED`、`START_UNKNOWN` 等）；运行时拒绝（例如相对的 `cwd`）是 `NOT_STARTED` 的 outcome 而不是拒绝，证据目录为空；`usage_dimensions` 含未知维度（`gpu`）时 `run_adapter_operation` 在领取之后抛 `AdapterError("E_ADAPTER_SCHEMA")`，操作留在 `RESERVED`。
- 规划端草稿实测（Windows，本合同的 serve 原型约 130 行）：oracle 13/13；28 条故意偏差（done 跳过改名、去掉次数检查、序号多算一、键分隔符、证据目录不分尝试、不查目录已存在、`E_ACTIVE_TASK` 不跳过、重放不停止、`drained` 写错、`max_runs` 多跑一次、不查 `max_seconds`、中断不捕获、拒绝不写 stderr、拒绝退出 0、不核对作业 `request_id`、不拒绝账本没有的任务、任务顺序反转、忽略 `--candidate`、`--max-runs` 收 0、`--max-seconds` 拒 0、记录的 attempt 写死、领取后的拒绝不写操作 id、空目录不拒绝、没有作业的任务也领、记录的状态写死、其它码也跳过、skipped 不记录、逻辑时钟不递增）里 27 条被 oracle 抓住，1 条等价（`--now` 的递增外部观察不到）。草稿阶段发现“一遍领到了就再走一遍”会让 `NOT_STARTED` 的任务在一次调用里烧光次数，所以定为每个任务最多运行一次（第 6 节）。草稿树上全量 1001 passed / 281 skipped（基线 988 加 oracle 13）：没有现有测试钉住旧表面。

## Scope
- allowed_modified_files:
  - sentinelmesh/run_cli.py
- allowed_new_files:
  - tests/test_run_cli_serve.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_run_cli_serve_oracle.py`, `tests/test_run_cli_oracle.py`, `tests/test_run_cli.py`, `tests/test_run_cli_recover_oracle.py`, `tests/test_run_cli_recover.py`, `tests/test_run_cli_pi_oracle.py`, `tests/test_run_cli_pi.py`, `tests/test_task_done_oracle.py`, `tests/test_review_verdict_oracle.py`, `tests/test_ledger_cli_oracle.py`, `sentinelmesh/cli.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定：`run_cli.load_jobs(directory, request_id)`、`run_cli.cmd_serve(args)`、`run_cli._clock()`、`run_cli._count(text)`、`run_cli._SERVE_SKIP`；码 `E_RUN_JOBS`、`E_RUN_EVIDENCE`、`E_RUN_CLAIM_REPLAYED`（后者与 `once` 的同名）。执行器测试可以用私有名字；oracle 只通过 `cli.main`、真实子进程与两个 `mock.patch.object` 驱动。

1. **命令行**：`register` 在 `once` 之后、`recover` 之前加 `serve`：位置参数 `ledger`；必填 `--request`、`--jobs`（目录）、`--evidence`（根目录）、`--worktree-id`、`--owner`；可选 `--max-runs`（类型 `_count`：`_digits` 再要求 ≥ 1，否则 `argparse.ArgumentTypeError`）、`--max-seconds`（类型 `_digits`，0 合法）、`--candidate`（类型 `_candidate`）、`--now`（类型 `_digits`，缺省 0）。缺必填或类型不对是 argparse 的用法错误（退出 2）。
2. **作业目录** `load_jobs(directory, request_id) -> dict[task_id, job]`：`directory` 不是目录 → `RunSpecError("E_RUN_JOBS")`；按文件名排序读其中每个 `*.json`（其它文件不读）；一个都没有 → `E_RUN_JOBS`；每个文件用 `load_job` 原样读，它的 `E_RUN_JOB` 变成 `E_RUN_JOBS`，detail 以文件名开头（`"<文件名>: <原 detail>"`）；`job["request_id"] != request_id` → `E_RUN_JOBS`（detail 含文件名）；`job["task_id"]` 不等于去掉 `.json` 的文件名 → `E_RUN_JOBS`（detail 含文件名）。任何一份不对就什么也不返回。
3. **报告与退出码**：stdout 永远恰好一行 canonical JSON（`_emit`），键恰好 `{"command": "serve", "request": <--request>, "runs": [...], "skipped": [...], "stopped": <…>, "refusal": null | {...}}`。`runs` 每项恰好五键 `{"task_id", "attempt", "operation_id", "state", "evidence_dir"}`（`state` 是 outcome 的 `state`，`evidence_dir` 是第 5 条的路径原样）；`skipped` 每项恰好两键 `{"task_id", "reason"}`，按发生顺序。`stopped` 是 `drained | max_runs | max_seconds | interrupted | refused` 之一。退出码：`refused` 为 3，其余 0。`refused` 时 `refusal` 是 `{"error", "detail", "operation_id"}`（形状同 `_refuse` 的 stderr 行：`ContractError`/`LedgerError`/`RunSpecError` 用 `.code`/`.detail`，其它用 `.code`/`str(exc)`；`operation_id` 是本次调用已领取且被这次拒绝留在账本里的操作，领取之前的拒绝为 null），**同一个对象**再以 `_write_line(sys.stderr, …)` 写一行到 stderr；非 `refused` 时 stderr 为空。
4. **开跑前的检查**（任何一条拒绝都是 `refused`，`runs`、`skipped` 为空，账本不变）：先 `load_jobs`（在打开账本之前）；再 `open_ledger`（它的拒绝按原码）；再 `ledger.task_ids(--request)`（未知请求 `E_REQUEST_UNKNOWN`）；目录里有作业而请求没有的任务 → `RunSpecError("E_RUN_JOBS")`，detail 列出这些任务 id（排序）。`candidate_hash` 与 `once` 相同：`--candidate` 给了就是 `_constant(args.candidate)`，否则 `evidence_digest`。
5. **一遍循环**：按 `task_ids` 的顺序每个任务考虑一次（一次调用里一个任务最多运行一次，运行以什么状态结束都不再领）。对每个任务依次：（a）`--max-runs` 给了且 `len(runs) >= max_runs` → `stopped = "max_runs"`，结束循环；（b）`--max-seconds` 给了且 `_clock() - started >= max_seconds`（`started` 是打开账本之后、循环之前读的一次 `_clock()`）→ `stopped = "max_seconds"`，结束；（c）没有作业 → skipped `no_job`，下一个；（d）`view = ledger.task(request, task)`：`view["done"]` → skipped `done`；否则 `view["attempts"] >= view["max_attempts"]` → skipped `attempts`；（e）`attempt = view["attempts"] + 1`，`evidence = os.path.join(--evidence, task_id, str(attempt))`，已存在（`os.path.exists`）→ `refused`，`RunSpecError("E_RUN_EVIDENCE")`，detail 含该路径，`operation_id` null；（f）`key = "%s#%d" % (job["idempotency_key"], attempt)`，`ledger.claim(request, task_id, job["plan"], key, --owner, ledger.generation, job["guard_version"], job["reservation"], list(job["resources"]), --now + 本次调用已成功领取的次数)`：拒绝码在 `_SERVE_SKIP = frozenset({"E_ACTIVE_TASK", "E_TASK_DONE", "E_ATTEMPTS"})` 里 → skipped，reason 就是该码；其它 `ContractError`/`LedgerError` → `refused`，`operation_id` null；`claim["replayed"]` 为真 → `refused`，`RunSpecError("E_RUN_CLAIM_REPLAYED", "operation <id> is already <state>")`，`operation_id` 是被重放的操作；（g）领到后 `os.makedirs(evidence)`，按 `cmd_once` 的写法构造 `Invocation` 并 `run_adapter_operation(... evidence_dir=evidence, worktree_id=--worktree-id, candidate_hash=…, now=该次领取用的 now, …)` 恰好一次；它抛的 `AdapterError`/`ContractError`/`LedgerError`/`RuntimeErrorCode` → `refused`，`operation_id` 是刚领取的操作；返回的 outcome → `runs` 追加一项，继续下一个任务。循环走完而 `stopped` 仍为空 → `drained`。
6. **中断**：（a）–（f）期间（领取之前，含 `_clock()`、视图读取与 `claim`）的 `KeyboardInterrupt` 捕获为 `stopped = "interrupted"`，结束循环，报告照常输出、退出 0；（g）运行当中的 `KeyboardInterrupt` 不捕获，与 `once` 相同。账本在 `finally` 里关闭，无论怎样结束。
7. **顺序执行与时钟**：一次只有一个操作在运行；`--max-runs`/`--max-seconds` 只在领取之前检查，不在运行中途杀进程；`--now` 从给定值起每次成功领取加一（只影响租约，外部观察不到）。
8. **文档串**：`load_jobs`、`cmd_serve` 的文档串按 `cmd_once`/`cmd_recover` 的风格写明以上规则（一遍、每任务一次、跳过与停止的区别、报告与退出码、中断的两种处理）；模块文档串的命令列表加 `serve` 一段；`register` 的 `help` 一句话。导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明、oracle 文件，`run_cli.py` 的 `cmd_once`、`cmd_recover`、`_refuse`、`_emit`、`load_job`、`register`，`scheduler.py` 的 `claim`、`task_ids`、`task`。基线与 Repository facts 不符就停止并报告。
2. 改 `run_cli.py`（第 1–8 条；约 130 行连文档串：`load_jobs` 约 25 行、`cmd_serve` 约 95 行、`_count` 5 行、`register` 约 15 行）。
3. 跑 V01 直到 oracle 全过（约 7 秒）。
4. 跑 V03 确认 `once`/`recover`/`init`、pi adapter 与 `smctl ledger` 未受影响。
5. 新建 `tests/test_run_cli_serve.py`，只覆盖下面这些（oracle 已覆盖产品命令的完整流程、真实进程、被杀的控制器、替身账本、中断、上限、证据目录；可以沿用 oracle 的 harness 写法，但不要照抄 oracle 的用例；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - `load_jobs` 直接调用：三份合法作业的往返（返回按任务 id 索引、内容含 `load_job` 填入的两个上限）；不是目录、空目录、一份缺 `cwd`（detail 以 `task-2.json:` 开头）、一份 `request_id` 不对、一份文件名与 `task_id` 不符，各自 `E_RUN_JOBS`，且目录里的非 `.json` 文件被忽略；
   - 进程内 `cli.main`：两个任务一份作业 → `runs` 一项、`skipped` 一项 `no_job`、`stopped` `drained`、退出 0、stderr 空；同一账本再加 `--max-runs 1` 的第二个请求布置（或新账本两份作业）→ 恰好一次运行、`stopped` `max_runs`、账本里操作数相符；
   - 报告形状：`runs` 项恰好五键、`skipped` 项恰好两键、顶层恰好六键；`refused` 时 stdout 的 `refusal` 与 stderr 的那一行相等（用不存在的作业目录触发）；
   - 真实 `python -m sentinelmesh.cli run serve` 子进程：作业目录不存在 → 退出 3，stdout 一行报告（`stopped` `refused`），stderr 一行。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_serve_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_serve.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_run_cli_recover.py tests/test_run_cli_pi_oracle.py tests/test_run_cli_pi.py tests/test_task_done_oracle.py tests/test_review_verdict_oracle.py tests/test_ledger_cli_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "from sentinelmesh import run_cli; assert callable(run_cli.cmd_serve) and callable(run_cli.load_jobs) and callable(run_cli._clock) and callable(run_cli._count); assert run_cli._SERVE_SKIP == frozenset({'E_ACTIVE_TASK', 'E_TASK_DONE', 'E_ATTEMPTS'}); print('serve ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`serve ok`
- V08（必要）：`git diff --quiet 88e7f14134bc493de72bbbfbc1b0ba20758b5aed -- sentinelmesh/cli.py sentinelmesh/scheduler.py sentinelmesh/durable_ledger.py sentinelmesh/ledger_cli.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/restart_recovery.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/pi_events.py tests/test_run_cli_serve_oracle.py tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_run_cli_recover.py tests/test_run_cli_pi_oracle.py tests/test_run_cli_pi.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为 `serve` 还不存在；V02 是因为测试文件还不存在。V03 保护 `run` 的其它三个命令、pi adapter、task-done、复审裁决与 `smctl ledger`；V08 保护十四个不该动的模块与七个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 170 秒。

## Definition of done
- D01：命令行、作业目录的读取与拒绝（第 1–2 条）；证据：V01、V02；目的：操作员一条命令跑一个请求，而任何一份作业有问题时什么都不跑、账本不动。
- D02：报告与退出码的形状，拒绝时 stdout 与 stderr 的一致（第 3 条）；证据：V01、V02；目的：被脚本调用时一行就能读到跑了什么、跳过了什么、为什么停。
- D03：开跑前的检查与一遍循环的每一步——跳过的原因、停止的原因、派生键、证据目录、上限只在领取前检查（第 4–5、7 条）；证据：V01、V02；目的：每次运行与手敲 `once` 留下的账本完全相同，serve 不多一个守卫也不少一个，一个任务一次调用里只跑一次。
- D04：中断的两种处理与账本的关闭（第 6 条）；证据：V01；目的：领取之间按 Ctrl-C 得到报告而不是堆栈，运行当中的中断仍交给 `once` 的规则。
- D05：其它命令与模块不受影响，`run_cli.py` 的 diff 只在合同指定的位置，文档串按第 8 条，导入无副作用；证据：V03、V04、V07、V08、review。
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
- 时间分配（上限 30 分钟，按 20 分钟规划、留 10 分钟余量）：
  - 阅读约 5 分钟；
  - 实现约 6 分钟（一个文件，约 130 行）；
  - 测试约 5 分钟（4 组用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 7 秒，V03 约 40 秒，全量约 170 秒）；
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
