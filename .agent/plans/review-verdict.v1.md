# TASK: review-verdict

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 717e0f3cb43259ebdfde455b238ef5c9c021330a
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/review-verdict.md`（用户决定第 5 节四项均选推荐项）给复审裁决一个操作员入口：
- `Scheduler.verdict(operation, generation, *, candidate_hash, accepted, evidence_hash, operator, note)`：按 `resolve_unknown` 的口径记录操作员对 `REVIEW_PENDING` 运行的裁决（当前代际、候选、证据、操作员、说明），改状态为 `ACCEPTED`/`REJECTED`，发 `run.verdict`；`fixture_verdict` 原样保留。
- `durable_ledger.MUTATING_METHODS` 在 `"fixture_verdict"` 之后插入 `"verdict"`（末尾的 `"resolve_unknown", "resume"` 不动——request-resume 的测试钉住了它们）。
- `smctl ledger verdict <ledger> --operation OP --candidate HASH (--accept | --reject) --operator NAME --evidence HASH --note TEXT`，约定与其它 `smctl ledger` 命令相同。
- `smctl ledger status` 每个请求多一个 `reviews` 列表（`REVIEW_PENDING` 的运行与各自的候选），`attention` 不变。
- 让规划端 oracle `tests/test_review_verdict_oracle.py` 与更新后的 `tests/test_durable_ledger_oracle.py` 全部通过；新建执行器测试 `tests/test_review_verdict.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改 `tests/test_request_resume.py`、`tests/test_request_resume_oracle.py`（它们钉住方法表末尾两项）。
- 不改领取规则（`ACCEPTED`/`REJECTED` 都不阻止同一任务的下一次尝试，`max_attempts` 照旧）；不加"任务已完成"状态。
- 不删、不改 `fixture_verdict` 的行为与签名（12 个测试文件 30 处调用）；不改 `run.verdict` 既有的两个键（`state`、`evidence`），只加键。
- 不改 `attention` 的三类理由；不改 `status` 的其它键；不改 `events` 命令（`run.verdict` 已在 `_EVENT_TYPES`）。
- 不改 `scheduler.py`、`durable_ledger.py`、`ledger_cli.py` 里与本合同无关的任何行（复审逐 hunk 核对：调度器只加一个方法，账本只加一个名字，CLI 只加 `_candidate_of`/`_reviews`/`cmd_verdict`/一个子解析器/`_status` 的一个键）；不改 `LEDGER_FORMAT`。
- 不改 `cli.py`、`run_cli.py`、`restart_recovery.py`、`supervised_operation.py`、`adapter_run.py` 等其它模块（V08 钉住）；不改 oracle、夹具、允许集合之外的已有测试；不联网、不读凭据、不启动真实 pi。

## Repository facts
- 基线 = master `186a99d`（live-pi-run 之后；`smctl ledger` 八个命令，`fixture_verdict` 是唯一的裁决方法）+ 规划端 oracle 提交 `717e0f3cb43259ebdfde455b238ef5c9c021330a`，后者含新文件 `tests/test_review_verdict_oracle.py`（8 个用例，一个类 `VerdictOracle`，任何平台都跑，Windows 上约 3.5 秒；基线上 7 个失败——`smctl ledger verdict` 是用法错误、`status` 没有 `reviews`、`Scheduler.verdict` 不存在；`test_the_scheduler_validates_in_order_before_mutating` 在基线上因 `AttributeError` 失败）与 `tests/test_durable_ledger_oracle.py` 的一行改动（`MUTATING_METHODS` 的期望元组在 `"fixture_verdict"` 之后多一个 `"verdict"`，基线上该用例因此失败）。oracle 在进程内调 `cli.main(argv)`，另有真实子进程 `python -m sentinelmesh.cli ledger verdict ...`；运行用 `smctl run init/once`（离线桩，`--candidate` 给定候选）制造 `REVIEW_PENDING`。
- `Scheduler.fixture_verdict(operation, candidate_hash, accepted, evidence_hash)`（`scheduler.py` 约第 1016 行）：`E_VERDICT_SHAPE`（`accepted` 非 bool、`evidence_hash` 非 64 hex）、`E_VERDICT_CANDIDATE`（操作不是 str、不存在、不是 `REVIEW_PENDING`、候选不是 str 或不等于记录的）、发 `run.verdict` `{state, evidence}`；不释放资源（`finish` 已释放）。
- `Scheduler.resolve_unknown`（约第 1136 行）是记录口径的样板：`self._decision_text(operator, "E_OPERATOR", _MAX_OPERATOR)`、`self._decision_text(note, "E_NOTE", _MAX_NOTE)`（精确 str、非空、≤200/≤500 字符、通过运行时文本规则——NUL、孤立代理项被拒）在锁外先做；锁内 `self._epoch(generation)`（非精确 int 或不等于当前代际 → `E_FENCED`；`True`、`2.0`、`str` 子类都被拒）。`_HEX64`、`_MAX_OPERATOR = 200`、`_MAX_NOTE = 500` 都是模块级名字。
- `durable_ledger.MUTATING_METHODS` 是按名字记录与重放的方法元组（未知名字 → `E_LEDGER_REPLAY`）；`DurableScheduler` 对元组里的每个名字自动生成同签名的包装方法（`_make_method`）；检查点只存状态。`tests/test_durable_ledger_oracle.py::test_the_public_surface_matches_the_scheduler` 钉住元组的精确内容与每个方法的签名等于 `Scheduler` 的。
- `ledger_cli.py`：`_with_ledger(args, work)` 打开账本、跑一次、`finally` 关闭、`_refuse` 写 `{"error", "detail"}` 退出码 3；`cmd_resolve` 用 `ledger.generation`，输出 `{"command": "resolve", "operation": ledger.operation(op)}`；`_status` 每个请求 `{request_id, stopped, stop_reasons, snapshot, attention}`；`_attention` 只列 `start_unknown`/`undecided`/`usage_unknown`；`ledger.operation(op)` 的视图没有候选，候选只在事件 payload 里：`run.finished_for_review`（`finish`）、`scheduler.recovered`（`recover` 结论 `FINISHED`）、`scheduler.unknown_resolved`（`resolve_unknown` 结论 `FINISHED`）各带 `candidate`。
- 规划端草稿实测（Windows）：`smctl run once --candidate <c>` 后 `status.reviews == [{operation_id, task_id, generation: 1, candidate: <c>}]`；`verdict --accept` → `{"command": "verdict", "operation": {... "state": "ACCEPTED" ...}}`，`run.verdict` payload 恰好 `{state, evidence, candidate, operator, note}`，`reviews` 变空，`attention` 仍空，请求未停止，`known`/`held` 不变；`--reject` 同；错候选/未知操作/已裁决/`RESERVED` 的操作 → `E_VERDICT_CANDIDATE`；证据非 hex → `E_VERDICT_SHAPE`；操作员空或 201 字符 → `E_OPERATOR`；说明空或含 NUL → `E_NOTE`；`rollover` 后命令用当前代际仍能裁，库调用给旧代际/`True`/`2.0`/`str` 子类 → `E_FENCED`；校验顺序：形状 → 操作员 → 说明 → 代际 → 操作与候选；`resolve --resolution FINISHED` 进来的复审带操作员的候选并可裁；`max_attempts=2` 的任务裁决后可再领取一次，第三次 `E_ATTEMPTS`；`compact` 后状态与事件仍在；`--accept` 与 `--reject` 同给、都不给、缺 `--candidate` → 退出码 2。草稿 oracle 8/8；相关套件（ledger-cli、durable-ledger、scheduler-exact-inputs、ledger-events、ledger-compaction、run-cli 两份 oracle）全过；全量 963 passed / 281 skipped。22 条故意偏差（跳过代际/操作员/说明/证据/accepted 的校验、代际在操作之后检查、忽略候选、裁任何状态、总是接受、事件少键或退回夹具形状、不改状态、方法表没有 verdict、命令固定代际/总是接受/丢说明、reviews 列全部操作/丢候选、status 没有 reviews、选项不互斥）里 21 条被 oracle 抓住，1 条等价（`_candidate_of` 取第一个还是最后一个候选事件：一个操作最多只有一个带候选的事件）。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - sentinelmesh/durable_ledger.py
  - sentinelmesh/ledger_cli.py
- allowed_new_files:
  - tests/test_review_verdict.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_review_verdict_oracle.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_ledger_cli_oracle.py`, `tests/test_ledger_cli.py`, `tests/test_scheduler_exact_inputs_oracle.py`, `tests/test_request_resume_oracle.py`, `sentinelmesh/cli.py`, `sentinelmesh/run_cli.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有"是 int""是 str"都是精确类型。下列名字由本合同固定：`Scheduler.verdict`（签名如 Goal，`generation` 位置参数，其余关键字参数）、`durable_ledger.MUTATING_METHODS` 里紧跟 `"fixture_verdict"` 的 `"verdict"`、`ledger_cli.cmd_verdict`、`ledger_cli._reviews(ledger, request_id)`、`ledger_cli._candidate_of(ledger, operation_id)`（执行器测试可以用私有名字；oracle 只通过 `cli.main`、`Scheduler`、`open_ledger` 驱动）。

1. **`Scheduler.verdict` 的校验顺序与码**：锁外先 `accepted` 是 bool 且 `evidence_hash` 是 64 个小写十六进制的 str，否则 `E_VERDICT_SHAPE`；再 `_decision_text(operator, "E_OPERATOR", _MAX_OPERATOR)`，再 `_decision_text(note, "E_NOTE", _MAX_NOTE)`；锁内先 `_epoch(generation)`（`E_FENCED`）；再 `operation` 是 str 且存在且 `state == "REVIEW_PENDING"` 且 `candidate_hash` 是 str 且等于 `op.candidate_hash`，否则 `E_VERDICT_CANDIDATE`。任何拒绝都不改状态、不发事件。
2. **`verdict` 的效果**：`op.state` 变为 `"ACCEPTED"`（`accepted` 为 True）或 `"REJECTED"`；发 `run.verdict`，payload 恰好五键 `{"state", "evidence", "candidate", "operator", "note"}`（经 `_encode_payload`）；不释放、不记用量、不改 owner/generation/reservation、不碰请求的停止与理由、不改 `_tasks`。`fixture_verdict` 一字不改。
3. **账本**：`MUTATING_METHODS` 在 `"fixture_verdict"` 之后插入 `"verdict"`（其它顺序不变，末尾仍是 `"resolve_unknown", "resume"`），由此 `DurableScheduler.verdict` 自动生成且签名与 `Scheduler.verdict` 相同；`LEDGER_FORMAT` 不变；重放与检查点照旧。
4. **`smctl ledger verdict`**：参数 `ledger`、必填 `--operation`、必填 `--candidate`（原样传，不在 argparse 层校验 hex——调度器会以 `E_VERDICT_CANDIDATE` 拒）、互斥且必选其一的 `--accept`/`--reject`（`dest="accept"`，`store_true`/`store_false`）、必填 `--operator`、`--evidence`、`--note`。`cmd_verdict` 用 `_with_ledger`：`ledger.verdict(args.operation, ledger.generation, candidate_hash=args.candidate, accepted=args.accept, evidence_hash=args.evidence, operator=args.operator, note=args.note)`，输出 `{"command": "verdict", "operation": ledger.operation(args.operation)}`。拒绝是 `_refuse` 的两键、退出码 3；用法错误退出码 2。子解析器加在 `resolve` 之后、`resume` 之前。
5. **`status.reviews`**：`_status` 每个请求的字典多一个键 `reviews`（在 `attention` 之后），值是 `_reviews(ledger, request_id)`：按 `ledger.operation_ids()` 的顺序，取 `request_id` 相同且 `state == "REVIEW_PENDING"` 的操作，每项恰好 `{"operation_id", "task_id", "generation", "candidate"}`；`candidate` 是 `_candidate_of`：遍历 `ledger.events`，该操作的、payload 含 `"candidate"` 键的事件中最后一个的 `payload["candidate"]`，没有则 None。`attention` 与其它键不变。
6. **文档串**：`verdict` 的文档串说明它是 `resolve_unknown` 口径的裁决、与 `fixture_verdict` 的区别（当前代际、操作员、说明）、校验顺序、只改状态；`fixture_verdict` 的文档串（若无则加一行）注明是测试用钩子；`cmd_verdict` 与 `_reviews`/`_candidate_of` 的文档串按实写；`ledger_cli` 模块文档串的命令清单加 `verdict`。导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明、两个 oracle 文件，`scheduler.py` 的 `fixture_verdict`、`resolve_unknown`、`_decision_text`、`_epoch`、`_emit`/`_encode_payload`，`durable_ledger.py` 的 `MUTATING_METHODS` 与 `_make_method`，`ledger_cli.py` 的 `_with_ledger`、`cmd_resolve`、`_attention`、`_status`、`register`。基线与 Repository facts 不符就停止并报告。
2. 改三个模块（第 1–6 条；调度器约 40 行连文档串，账本 1 行，CLI 约 60 行）。
3. 跑 V01 直到两个 oracle 全过（约 10 秒）。
4. 跑 V03 确认 `smctl ledger` 其它命令、账本重放、调度器精确输入、run-cli 未受影响。
5. 新建 `tests/test_review_verdict.py`，只覆盖下面这些（oracle 已覆盖命令的成功与拒绝、`reviews`、顺序、代际、再领取、compact、真实进程；可以沿用 oracle 的 `smctl`/`ok`/`refused` 写法，但不要照抄 oracle 的用例；子进程只用 `sys.executable -c` 与 `sys.executable -m sentinelmesh.cli`；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - `DurableScheduler.verdict` 的签名等于 `Scheduler.verdict` 的（`inspect.signature`），`"verdict"` 紧跟在 `MUTATING_METHODS` 的 `"fixture_verdict"` 之后且末尾仍是 `("resolve_unknown", "resume")`，`LEDGER_FORMAT` 仍是 `"sentinelmesh-ledger-v2"`；
   - 账本上 `verdict` 一次、关闭、`open_ledger` 重放后状态 `ACCEPTED` 且 `run.verdict` 事件 payload 五键与写入时相同；`seq` 加 1；
   - `_candidate_of` 直接调用：一个经 `finish` 的操作给 `finish` 的候选；一个经 `recover` 结论 `FINISHED` 的操作（用 `rollover` + `recover(op, owner, 2, outcome="FINISHED", evidence_hash=..., candidate_hash=...)`）给 `recover` 的候选；没有候选事件的操作给 None；
   - `_reviews` 直接调用：两个请求各一个 `REVIEW_PENDING`，各自只列自己的；
   - `cmd_verdict` 经 `cli.main`：`--reject` 之后 `ledger.operation(op)["state"] == "REJECTED"`，stdout 一行且 `operation` 等于 `open_ledger(...).operation(op)`；`fixture_verdict` 仍可在进程内 `Scheduler` 上调用且事件 payload 仍是两键（钉住"原样保留"）。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_review_verdict_oracle.py tests/test_durable_ledger_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_review_verdict.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_ledger_cli_oracle.py tests/test_ledger_cli.py tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs.py tests/test_ledger_events_oracle.py tests/test_ledger_compaction_oracle.py tests/test_request_resume_oracle.py tests/test_run_cli_oracle.py tests/test_run_cli_recover_oracle.py tests/test_scheduler_supervised_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -m sentinelmesh.cli ledger verdict --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`--accept`
- V08（必要）：`git diff --quiet 717e0f3cb43259ebdfde455b238ef5c9c021330a -- sentinelmesh/cli.py sentinelmesh/run_cli.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/restart_recovery.py sentinelmesh/pi_events.py tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py tests/test_scheduler_exact_inputs_oracle.py tests/test_request_resume_oracle.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为 `verdict` 还不存在；V02 是因为测试文件还不存在。V03 保护 `smctl ledger` 的其它命令、调度器精确输入、账本事件与压缩、请求恢复、run-cli、受监督调度；V08 保护十二个不该动的模块与四个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 170 秒。

## Definition of done
- D01：`Scheduler.verdict` 的校验顺序、码与效果，`fixture_verdict` 原样（第 1–2 条）；证据：V01、V02。
- D02：账本方法表与自动包装、签名一致、`LEDGER_FORMAT` 不变（第 3 条）；证据：V01、V02。
- D03：`smctl ledger verdict` 的参数、顺序、输出与拒绝（第 4 条）；证据：V01、V07。
- D04：`status.reviews` 的内容、顺序与候选来源，`attention` 不变（第 5 条）；证据：V01、V02、V03。
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
  - 实现约 6 分钟（三个文件）；
  - 测试约 5 分钟（5 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 10 秒，V03 约 60 秒，全量约 170 秒）；
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
