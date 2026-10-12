# TASK: task-done

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 7256c8b0934bcf5f87365f1f1cf341f624d5239a
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/task-done.md`（用户决定第 5 节四项均选推荐项）让"被接受"对调度器有后果，并让任务的状态可读：
- `Scheduler.claim`：同请求同任务存在状态为 `ACCEPTED` 的操作 → `E_TASK_DONE`，位置在 `E_GUARD_STALE` 之后、`E_ACTIVE_TASK` 之前（因此先于 `E_ATTEMPTS`）；幂等重放照旧先于一切规则。
- 两个只读视图：`Scheduler.task_ids(request) -> tuple[str, ...]`（种子顺序）与 `Scheduler.task(request, task) -> dict`（恰好四键 `task_id`、`max_attempts`、`attempts`、`done`），`DurableScheduler` 同名代理。
- `smctl ledger status` 每个请求多一个 `tasks` 列表（种子顺序，每项即 `task` 视图），`attention`、`reviews` 不变。
- 让规划端 oracle `tests/test_task_done_oracle.py` 与更新后的 `tests/test_review_verdict_oracle.py` 全部通过；新建执行器测试 `tests/test_task_done.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不加新状态、不发新事件、不加账本记录：规则与视图都从操作状态推导。`LEDGER_FORMAT`、`MUTATING_METHODS` 一字不改（`task_ids`/`task` 是读，不进方法表）。
- 不改 `max_attempts` 的计数口径（`E_ATTEMPTS` 仍按同任务操作总数，含取消与未启动的）；不改 `REJECTED` 的语义（任务仍可再领取）。
- 不改 `E_ACTIVE_TASK`、`E_ATTEMPTS`、`E_FENCED`、`E_SCOPE_UNKNOWN`、`E_STOPPED`、`E_GUARD_STALE` 的判断与相对顺序；不改幂等重放。
- 不改 `attention`、`reviews` 与 `status` 的其它键；不改 `smctl run`（领取被拒已由 `_refuse` 原样上报，`operation_id` 为 null）。
- 不改 `scheduler.py`、`durable_ledger.py`、`ledger_cli.py` 里与本合同无关的任何行（复审逐 hunk 核对：调度器只加规则两行加注释与两个方法，账本只加两个代理方法，CLI 只加 `_tasks` 与 `_status` 的一个键）。
- 不改 `cli.py`、`run_cli.py`、`restart_recovery.py`、`supervised_operation.py`、`adapter_run.py` 等其它模块（V08 钉住）；不改 oracle、夹具、允许集合之外的已有测试；不联网、不读凭据、不启动真实 pi。

## Repository facts
- 基线 = master `fdb2d1c`（review-verdict 之后；`smctl ledger` 九个命令，`status` 每个请求六键 `{request_id, stopped, stop_reasons, snapshot, attention, reviews}`）+ 规划端 oracle 提交 `7256c8b0934bcf5f87365f1f1cf341f624d5239a`，后者含新文件 `tests/test_task_done_oracle.py`（8 个用例，一个类 `TaskDoneOracle`，任何平台都跑，Windows 上约 3 秒；基线上 8 个全失败——`E_TASK_DONE` 不存在、`task_ids`/`task` 不存在、`status` 没有 `tasks`）与 `tests/test_review_verdict_oracle.py` 的两处改动（`status` 请求条目的键集加 `tasks`；`test_a_rejected_task_takes_a_new_attempt_and_an_accepted_one_is_closed` 里接受之后的第三次领取期望 `E_TASK_DONE` 而不是 `E_ATTEMPTS`，基线上这两个用例失败）。oracle 在进程内调 `cli.main(argv)`，另有真实子进程 `python -m sentinelmesh.cli run once ...` 与 `ledger status`；运行用 `smctl run init/once`（离线桩）制造 `REVIEW_PENDING`，用 `smctl ledger verdict` 裁决。
- `Scheduler.claim`（`scheduler.py` 约第 738 行）锁内的顺序：幂等重放（同键同指纹直接 `return {..., "replayed": True}`）→ `self._epoch(generation)`（`E_FENCED`）→ `root`/`item` 查找（`E_SCOPE_UNKNOWN`）→ `root.stopped`（`E_STOPPED`）→ `guard_version`/`fixture_eligible`（`E_GUARD_STALE`）→ 活动操作（`E_ACTIVE_TASK`：`LIVE | {"REVIEW_PENDING"}`）→ 次数（`E_ATTEMPTS`）→ `E_CONCURRENCY` → 预算 → `E_RESOURCE_BUSY`。领取被拒不发事件、不改状态。`claim` 没有文档串，只有注释。
- `ACCEPTED` 只由 `verdict` 与 `fixture_verdict` 写入，前态都是 `REVIEW_PENDING`；`REVIEW_PENDING` 阻止同任务领取，所以已接受的任务不可能同时有活动操作。
- `self._tasks` 是 `dict[(request, task) -> _Task(caps, max_attempts, guard_version, fixture_eligible)]`，插入顺序 = 种子顺序，检查点（`durable_ledger.py` 约第 332 与 536 行）按这个顺序存取；`self._requests` 是 `dict[request -> _Request]`。`request_ids()`（`scheduler.py` 约第 562 行）与 `operation_ids()` 是现有只读视图的样板：锁内、不发事件；`snapshot`/`stop_reasons` 对未知请求 `E_REQUEST_UNKNOWN`，`claim` 对未知请求或任务 `E_SCOPE_UNKNOWN`（都要求精确 `str`：`str` 子类被拒）。
- `DurableScheduler`（`durable_ledger.py`）的只读代理样板是 `request_ids`（约第 828 行）：`with self._lock: self._require_available(); return self._scheduler.request_ids()`；关闭的实例 → `E_LEDGER_UNAVAILABLE`。
- `ledger_cli._status`（约第 322 行）：每个请求 `{request_id, stopped, stop_reasons, snapshot, attention, reviews}`；`_reviews`（约第 300 行）是列表辅助函数的样板。输出 `sort_keys`，键的插入顺序不影响输出。
- 规划端草稿实测（Windows）：接受后 `smctl run once` 同任务 → stderr `{"error": "E_TASK_DONE", "detail": null, "operation_id": null}`、退出 3、账本字节不变；`status.tasks == [{task_id: "task-1", max_attempts: 3, attempts: 1, done: true}, {task-2 …, attempts: 0, done: false}, …]`；其它任务照常领取；同名任务在另一请求下不受影响；拒绝后 `done: false`，取消的领取也算一次 `attempts`，三次用尽 → `E_ATTEMPTS`；`max_attempts=1` 且已接受 → `E_TASK_DONE`；旧代际 → `E_FENCED`、未知任务 → `E_SCOPE_UNKNOWN`、错 guard → `E_GUARD_STALE`、停止的请求 → `E_STOPPED` 都先于它；`fixture_verdict` 写的 `ACCEPTED` 同样关闭任务；同键同指纹重放 → `replayed: True` 与 `ACCEPTED`；`smctl run recover`（代际 2）+ `resume` 后规则仍在，`compact` 与重开后 `task_ids` 顺序与视图不变；视图对未知请求/任务、`str` 子类、`None`、`int` 的码；关闭的实例 → `E_LEDGER_UNAVAILABLE`。草稿 oracle 8/8；全量 979 passed / 281 skipped。24 条故意偏差（去掉规则、规则放到次数之后、拒绝也关闭、报 `E_ATTEMPTS`、忽略请求、规则放到重放/代际/停止/guard 之前、`attempts` 不算取消、`done` 算拒绝、视图少键、视图收 `str` 子类、未知请求报别的码、`task_ids` 排序/未知请求返回空/收 `str` 子类、代理跳过可用性检查、`status` 只列有尝试的任务/排序/没有 `tasks`）里 22 条被 oracle 抓住，2 条等价（规则放在 `E_ACTIVE_TASK` 之后：已接受的任务没有活动操作；`done` 只看最后一个操作：接受之后不会再有操作）。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - sentinelmesh/durable_ledger.py
  - sentinelmesh/ledger_cli.py
- allowed_new_files:
  - tests/test_task_done.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_task_done_oracle.py`, `tests/test_review_verdict_oracle.py`, `tests/test_review_verdict.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_ledger_cli_oracle.py`, `tests/test_ledger_cli.py`, `tests/test_scheduler_exact_inputs_oracle.py`, `tests/test_request_resume_oracle.py`, `tests/test_request_resume.py`, `sentinelmesh/cli.py`, `sentinelmesh/run_cli.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有"是 int""是 str"都是精确类型。下列名字由本合同固定：`E_TASK_DONE`、`Scheduler.task_ids(request)`、`Scheduler.task(request, task)`、`DurableScheduler.task_ids`/`task`、`ledger_cli._tasks(ledger, request_id)`（执行器测试可以用私有名字；oracle 只通过 `cli.main`、`Scheduler`、`open_ledger` 驱动）。

1. **领取规则**：`claim` 锁内，在 `E_GUARD_STALE` 那一行之后、`E_ACTIVE_TASK` 那两行之前，加上（措辞照抄，上面一两行注释说明"接受的运行关闭任务"）：
   ```
               if any(op.request_id == request and op.task_id == task and op.state == "ACCEPTED"
                      for op in self._operations.values()): raise ContractError("E_TASK_DONE")
   ```
   其它检查与顺序一行不动；被拒不发事件、不改任何东西；幂等重放照旧在所有规则之前。
2. **`Scheduler.task_ids(request)`**：放在 `request_ids` 之后。锁内：`request` 不是 str 或不在 `self._requests` → `E_REQUEST_UNKNOWN`；返回 `tuple(task for name, task in self._tasks if name == request)`（种子顺序，不排序）。不发事件、不改状态。
3. **`Scheduler.task(request, task)`**：放在 `task_ids` 之后。锁内：`request` 或 `task` 不是 str → `E_SCOPE_UNKNOWN`；`self._tasks.get((request, task))` 为 None → `E_SCOPE_UNKNOWN`；`ops` = 同请求同任务的全部操作（任何状态）；返回恰好四键 `{"task_id": task, "max_attempts": item.max_attempts, "attempts": len(ops), "done": any(op.state == "ACCEPTED" for op in ops)}`。不发事件、不改状态。
4. **账本代理**：`DurableScheduler.task_ids(self, request: str)` 与 `task(self, request: str, task: str)`，放在 `request_ids` 之后，形式与 `request_ids` 相同（`with self._lock: self._require_available(); return self._scheduler.…`），签名与 `Scheduler` 的相同（`inspect.signature` 去掉 `self` 后相等）。`MUTATING_METHODS`、`LEDGER_FORMAT`、检查点与重放一字不改。
5. **`status.tasks`**：`ledger_cli._tasks(ledger, request_id)` 返回 `[ledger.task(request_id, task_id) for task_id in ledger.task_ids(request_id)]`（种子顺序、每个任务都列，包括没有尝试的）；`_status` 每个请求的字典在 `reviews` 之后多一个键 `"tasks": _tasks(ledger, request_id)`；其它键与 `attention`、`reviews` 的内容不变。
6. **文档串**：规则上方的注释；`task_ids`、`task` 的文档串按 `request_ids`/`operation` 的风格（顺序是种子顺序、未知的码、`attempts` 是 `claim` 对 `max_attempts` 计数的同一口径、`done` 是关闭任务的事实而不论由哪个裁决写入、读不记事件）；两个代理的文档串按 `request_ids` 代理的风格；`_tasks` 的文档串说明它给操作员的是什么（哪些任务已完成、还开着、次数用尽）。导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明、两个 oracle 文件，`scheduler.py` 的 `claim`、`request_ids`、`operation`、`snapshot`、`_tasks` 的定义与 `seed_task`，`durable_ledger.py` 的 `request_ids`/`operation_ids` 代理与 `_require_available`，`ledger_cli.py` 的 `_reviews`、`_status`。基线与 Repository facts 不符就停止并报告。
2. 改三个模块（第 1–6 条；调度器约 45 行连文档串，账本约 25 行，CLI 约 15 行）。
3. 跑 V01 直到两个 oracle 全过（约 10 秒）。
4. 跑 V03 确认 `smctl ledger` 其它命令、账本重放与公开表面、调度器精确输入、请求恢复、run-cli 未受影响。
5. 新建 `tests/test_task_done.py`，只覆盖下面这些（oracle 已覆盖产品命令、真实进程、顺序、重放、恢复/压缩/重开、未知范围的码；可以沿用 oracle 的 `Scheduler` 写法，但不要照抄 oracle 的用例；不用子进程；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - 进程内 `Scheduler`：一个任务经 `claim → handoff → mark_running → settle → finish → fixture_verdict(accepted=True)` 后，新键的 `claim` → `E_TASK_DONE`，且 `events`、`snapshot` 在拒绝前后相同；同一流程 `accepted=False` 后 `claim` → `RESERVED`；`max_attempts=1` 且已接受 → `E_TASK_DONE`（不是 `E_ATTEMPTS`）；
   - `task_ids` 对两个请求各自只列自己的任务、顺序是种子顺序（种子时故意不按字母序）；`task` 的 `attempts` 把一次 `cancel_unstarted` 的领取也算进去，`done` 在 `fixture_verdict(accepted=True)` 之后为 True、之前为 False；
   - `DurableScheduler.task_ids`/`task` 的签名等于 `Scheduler` 的（`inspect.signature`，去掉 `self`），`"task_ids"`、`"task"` 不在 `MUTATING_METHODS` 里，`LEDGER_FORMAT` 仍是 `"sentinelmesh-ledger-v2"`；账本上 `task` 的视图与关闭、`open_ledger` 重开后相同；
   - `_tasks` 直接调用：一个请求两个任务，一个已接受一个没尝试，得到两项、顺序与键集正确；`cmd_status` 经 `cli.main` 的输出里 `tasks` 与 `_tasks` 的结果相同。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_task_done_oracle.py tests/test_review_verdict_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_task_done.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_ledger_cli_oracle.py tests/test_ledger_cli.py tests/test_durable_ledger_oracle.py tests/test_durable_ledger.py tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs.py tests/test_request_resume_oracle.py tests/test_request_resume.py tests/test_ledger_compaction_oracle.py tests/test_run_cli_oracle.py tests/test_run_cli_recover_oracle.py tests/test_scheduler_supervised_oracle.py tests/test_review_verdict.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "from sentinelmesh.scheduler import Scheduler; from sentinelmesh.durable_ledger import DurableScheduler, MUTATING_METHODS; assert callable(Scheduler.task_ids) and callable(Scheduler.task) and callable(DurableScheduler.task_ids) and callable(DurableScheduler.task); assert 'task' not in MUTATING_METHODS and 'task_ids' not in MUTATING_METHODS; print('views ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`views ok`
- V08（必要）：`git diff --quiet 7256c8b0934bcf5f87365f1f1cf341f624d5239a -- sentinelmesh/cli.py sentinelmesh/run_cli.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/restart_recovery.py sentinelmesh/pi_events.py tests/test_task_done_oracle.py tests/test_review_verdict_oracle.py tests/test_review_verdict.py tests/test_durable_ledger_oracle.py tests/test_ledger_cli_oracle.py tests/test_ledger_cli.py tests/test_scheduler_exact_inputs_oracle.py tests/test_request_resume_oracle.py tests/test_request_resume.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为规则与视图还不存在；V02 是因为测试文件还不存在。V03 保护 `smctl ledger` 的其它命令、账本的公开表面与重放、调度器精确输入、请求恢复、压缩、run-cli、受监督调度与复审裁决；V08 保护十二个不该动的模块与九个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 160 秒。

## Definition of done
- D01：领取规则的位置、码与"被拒不改"（第 1 条）；证据：V01、V02。
- D02：`task_ids`/`task` 的键、顺序、口径与未知范围的码（第 2–3 条）；证据：V01、V02。
- D03：账本代理的签名与可用性检查，方法表与格式不变（第 4 条）；证据：V01、V02、V03、V07。
- D04：`status.tasks` 的内容与顺序，其它键不变（第 5 条）；证据：V01、V02、V03。
- D05：其它命令与模块不受影响，三个文件的 diff 只在合同指定的位置，文档串按第 6 条，导入无副作用；证据：V03、V04、V08、review。
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
  - 实现约 5 分钟（三个文件，约 85 行）；
  - 测试约 6 分钟（4 组用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 10 秒，V03 约 60 秒，全量约 160 秒）；
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
