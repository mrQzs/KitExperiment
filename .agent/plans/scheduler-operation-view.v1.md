# TASK: scheduler-operation-view

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: ce6d0655ae6da91c6ccfc66e4647cc103a9be829
- risk_level: low
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
给 `Scheduler` 加一个只读的操作视图 `operation(operation_id)`，并让 `rollover()` 与构造函数在代际无法编码时什么都不改就拒绝。

并让规划端 oracle `tests/test_scheduler_operation_view_oracle.py` 全部通过。

## Non-goals
- 不修改 `scheduler.py` 以外的任何模块；不改已有方法的语义，`rollover()` 与构造函数只在下面第 2、3 条说的边界上变化。
- 视图不含用量、候选哈希与资源键；不新增事件类型。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `ce6d065` = master `3cc13a3` + 规划端 oracle（8 个用例，纯内存，不启动进程）。
- 设计说明：`.agent/design/stream-trust-boundary.md` 第 3 节第 6 条。下一份合同里，桥接与 adapter 要在交接之前用这个视图核对调用属于哪个 request、预约了哪些维度。
- 现状：
  - `Scheduler` 没有公开的单个操作读取方式，测试只能读私有的 `_operations`；
  - `snapshot(request, task)` 返回预算汇总，不是某个操作的预约；
  - `rollover()` 先把代际加一、把所有 request 设为 `RECOVERY_REQUIRED`，然后才编码 `controller.fenced` 事件。代际为 2**53-1 时编码报 `E_JSON_INTEGER`，账本已经被改了（scheduler-exact-inputs 复审 F7）；
  - 构造函数只用 `exact_int(generation, positive=True)` 检查，接受 2**53 及以上的代际，之后任何事件都编码不了；
  - `canonical()` 对超出 JSON 安全范围的整数报 `ContractError("E_JSON_INTEGER")`；`_encode_payload` 在改动账本之前编码事件载荷。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 8/8，全量 546 passed；8 个故意制造的偏差全部被抓住。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
- allowed_new_files:
  - tests/test_scheduler_operation_view.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/test_scheduler_operation_view_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **`Scheduler.operation(operation) -> dict`**
   - 在锁内执行。
   - `type(operation) is not str` 时报 `ContractError("E_OPERATION_UNKNOWN")`。这个检查在查字典之前：不可哈希的值（list、dict）也报这个码，不冒出 `TypeError`；与已有 id 相等的 str 子类同样被拒绝。
   - 没有这个操作：`E_OPERATION_UNKNOWN`。
   - 否则返回一个新的 dict，键恰好是：
     - `operation_id`、`request_id`、`task_id`、`state`、`owner`：精确 str；
     - `generation`：精确 int；
     - `reservation`：该操作自己的预约，作为新的 dict（不是任务或 request 的预算）。
   - 不记录事件，不改变账本的任何部分；调用方修改返回值也不会影响账本。
   - 读取不受代际约束：换代之后仍能读到旧代际的 owner 与 generation。
2. **`rollover()`**
   - 先算出新代际，并编码 `controller.fenced` 的载荷 `{"generation": 新代际}`，然后才改动账本。
   - 编码失败（代际已是 2**53-1 时报 `E_JSON_INTEGER`）时原样抛出，代际、每个 request 的停止标记与事件都不变。
   - 成功时的行为不变：代际加一，尚未停止的 request 设为 `RECOVERY_REQUIRED`，追加一条 `controller.fenced` 事件，返回新代际。
3. **构造函数** `Scheduler(*, generation=1)`
   - 先做已有的 `exact_int(generation, positive=True)`（`E_INTEGER`）；
   - 再要求 `generation <= 2**53 - 1`，否则报 `ContractError("E_JSON_INTEGER")`。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3 节、oracle，以及 `scheduler.py` 中的 `__init__`、`_emit`、`_encode_payload`、`snapshot`、`rollover`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改 `sentinelmesh/scheduler.py`。
3. 新建 `tests/test_scheduler_operation_view.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 用量记录之后，视图不变（视图不含用量）；
   - 同一 request 下两个任务的操作，视图各自独立；
   - `rollover()` 连续两次成功时，事件与返回值正确；
   - `release_proven_unstarted` 之后，视图里的预约不变：释放的是预算汇总里的保留，不是操作自己的预约。
4. 运行 V01–V06。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_operation_view_oracle.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_operation_view.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01 与 V04 在基线上失败，是因为视图与边界规则还不存在；V02 是因为测试文件还不存在。V03 保护调度器的已有行为。

## Definition of done
- D01：`operation()` 的视图、拒绝与只读（Behavior contract 第 1 条）；证据：V01、V02。
- D02：`rollover()` 在编码失败时不改账本（第 2 条）；证据：V01、V02。
- D03：构造函数拒绝无法编码的代际（第 3 条）；证据：V01。
- D04：已有行为不退化；证据：V03、V04。
- D05：Step 3 的测试齐全，不自行做变异检查；证据：V02、review。
- D06：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1200
- max_wall_seconds_total: 2400
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 3 分钟；
  - 实现约 3 分钟；
  - 测试约 5 分钟；
  - 运行检查约 4 分钟（oracle 约 1 秒，全量约 115 秒）；
  - 执行报告约 3 分钟；
  - 合计约 18 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
