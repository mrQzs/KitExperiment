# TASK: resume-holds

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 5e28d9515e25d8d9be7d8edbc413556f9add494a
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
修好 request-resume 复审留下的两个合同缺口：
- 请求已经停止之后再叠加的停止（例如运维的安全暂停）不能被一次恢复悄悄清掉：记下自上次恢复以来的全部停止原因，恢复时必须原样点名这个列表；新增只读方法查看它。
- 账本见过开始运行（`run.started`）的操作，不能再判为未启动：`resolve_unknown` 与 `recover` 都拒绝。

修改 `sentinelmesh/scheduler.py` 与 `sentinelmesh/durable_ledger.py`，把 `tests/test_request_resume.py` 改到新的参数，并让规划端 oracle `tests/test_resume_holds_oracle.py`、更新过的 `tests/test_request_resume_oracle.py` 与 `tests/test_durable_ledger_oracle.py` 全部通过。

## Non-goals
- 不改 `Scheduler` 的其他语义（改动只限 Behavior contract 第 1–3 条）；不改 `restart_recovery.py`、`supervised_operation.py`、`posix_supervisor.py`、`supervised_record.py`、`adapter_run.py`、`runtime.py`、`persistence.py`。
- 不做提额；不核验运维的权限与证据。
- 持久账本只加一个读访问器：`MUTATING_METHODS`、日志格式名、编码、重放与失败关闭都不改。
- 不为旧版 `resume` 命令做兼容：在 request-resume v1 下记过 `resume` 的账本，重放时会以 `E_LEDGER_REPLAY` 失败关闭（这样的账本只存在于测试里），这是规划端的决定 P12。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与其他已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `5e28d95` = master `8cd6860` + 规划端的两处改动：
  - 新的 oracle `tests/test_resume_holds_oracle.py`，9 个用例；
  - `tests/test_request_resume_oracle.py` 改用 `expected_reasons`，原来叠加原因会让比对先失败的那几个用例改为点名全部原因，事件载荷改为 `previous_reasons`。

  这两份 oracle 在基线上失败；`tests/test_durable_ledger_oracle.py` 不变，但在 V01 里一起跑。
- 设计说明：`.agent/design/request-resume.md` 第 9 节（用户决定与规划端决定 P8–P12）。本合同的规则都来自那里；第 5–6 节是 request-resume 的原有规则。
- 停止的写入点：
  - `stop`：`root.stopped = root.stopped or reason`；
  - `rollover`：每个请求 `stopped or "RECOVERY_REQUIRED"`；
  - `unknown`：`stopped or "START_UNKNOWN"`；
  - `_enforce`：`stopped` 为空时写入 `BUDGET_EXCEEDED` 或 `USAGE_UNKNOWN`，并且只要有原因就发出 `request.stop_observed`。

  请求已经停止时，这四处都不再改 `stopped`。
- `resume`（request-resume v1）的签名是 `resume(self, request, generation, *, expected_reason, operator, note)`：
  - 检查顺序：运维名、说明、期望原因、代际、请求、已停止、`stopped == expected_reason`、未知启动、旧代际未判定、未知用量、额度；
  - 事件载荷是 `{"request_id", "previous_reason", "operator", "note"}`。
- `mark_running` 把 `HANDOFF_ACCEPTED` 改为 `RUNNING` 并发出 `run.started`。`recover` 现在只在操作当前是 `RUNNING` 时拒绝 `NOT_STARTED`（`E_RECOVERY_CONFLICT`），一个 `RUNNING` 之后变成 `START_UNKNOWN` 的操作仍可被判为 `NOT_STARTED`。
- `resolve_unknown` 的检查顺序：结论、证据、候选、运维名、说明、代际、操作、状态（`E_RESOLVE_STATE`）。
- 执行器的旧测试 `tests/test_request_resume.py` 用的是 `expected_reason`，本合同里要改。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - 三份 oracle 36/36；把 `tests/test_request_resume.py` 改到新参数之后，全量 748 passed（不改它时有 42 处失败，都是旧参数名）；
  - 23 个故意制造的偏差全部抓住。其中包括：只记第一次停止、重复的原因也记、预算守卫或换代或 `stop` 不记原因、只比对第一个原因、不管顺序、恢复不清原因、接受元组或空列表或重复、事件只记第一个原因、不记开始过、`recover` 只看当前状态、先查冲突再查状态、账本没有读原因。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - sentinelmesh/durable_ledger.py
  - tests/test_request_resume.py
- allowed_new_files:
  - tests/test_resume_holds.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `sentinelmesh/runtime.py`, `tests/test_resume_holds_oracle.py`, `tests/test_request_resume_oracle.py`, `tests/test_durable_ledger_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **停止原因**
   - `_Request` 另记自上次恢复以来的停止原因：按第一次出现的顺序排列，同一个原因只记一次。
   - `stop`、`rollover`、`unknown` 与 `_enforce` 每次停止请求，都把原因加进去；请求已经停止时也加。`stopped` 仍是第一个原因，其余检查照旧。
   - 新增只读方法 `stop_reasons(self, request) -> tuple[str, ...]`：
     - 返回这个列表的元组，请求在运行时是空元组；
     - `request` 不是精确 `str` 或是未知的请求，报 `ContractError("E_REQUEST_UNKNOWN")`；
     - 不发事件，不改账本。
2. **`resume(self, request, generation, *, expected_reasons, operator, note) -> None`**
   - `expected_reason` 改为 `expected_reasons`，其余参数不变。
   - 第 3 项检查改为：`expected_reasons` 是精确 `list`，不为空，元素是互不重复的非空精确 `str`，否则报 `E_STOP_REASON`。先复制这个列表，只检查与比对副本。
   - 第 7 项检查改为：按顺序与 `stop_reasons` 完全相等，否则报 `E_STOP_REASON_CHANGED`。
   - 其余检查与它们的顺序都不变。
   - 通过之后：同时清空 `stopped` 与停止原因；事件 `request.resumed` 的载荷恰好是 `{"request_id", "previous_reasons", "operator", "note"}`，`previous_reasons` 是恢复之前的原因列表。
3. **见过运行的操作**
   - 操作另记它是否开始过：`mark_running` 把它置为真。
   - `resolve_unknown`：在状态检查（`E_RESOLVE_STATE`）之后，如果结论是 `NOT_STARTED` 而操作开始过，报 `ContractError("E_RESOLVE_CONFLICT")`；`ABANDONED` 与 `FINISHED` 不受影响。
   - `recover`：原来“结论是 `NOT_STARTED` 而操作当前是 `RUNNING`”报 `E_RECOVERY_CONFLICT`，扩展为“结论是 `NOT_STARTED` 而操作当前是 `RUNNING` 或开始过”，检查位置不变。
4. **持久账本**：`DurableScheduler` 新增读访问器 `stop_reasons(request)`，与已有的 `operation()` 一样：实例不可用时报 `LedgerError("E_LEDGER_UNAVAILABLE")`，否则读内存，不碰日志。其余不变。
5. **旧测试**：把 `tests/test_request_resume.py` 改用 `expected_reasons`，事件载荷改为 `previous_reasons`。
   - 每个用例原来要守的规则不变；原来会因为叠加的原因先在比对处失败的用例（规划端草稿里有两个：放弃之后部分补记用量、两个请求），改为点名全部原因。
   - `test_the_stop_reason_is_compared_against_the_first_one_recorded` 钉住的正是要改掉的旧规则（只比对第一个原因）：改为断言新规则——只点名其中一个原因报 `E_STOP_REASON_CHANGED`，点名全部原因才能往下检查——并按新规则改名。
   - 不得删除或跳过用例。
6. **导入无副作用**：导入 `sentinelmesh.scheduler` 与 `sentinelmesh.durable_ledger` 仍不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 9 节、三份 oracle，以及 `scheduler.py`（`_Request`、`_Operation`、`stop`、`rollover`、`unknown`、`_enforce`、`mark_running`、`recover`、`resolve_unknown`、`resume`）、`durable_ledger.py` 的读访问器与 `tests/test_request_resume.py`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改 `sentinelmesh/scheduler.py`，第 4 条修改 `sentinelmesh/durable_ledger.py`，第 5 条修改 `tests/test_request_resume.py`。
3. 新建 `tests/test_resume_holds.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 几种原因交错出现、有的出现多次：列表按第一次出现的顺序；
   - 恢复、再停止、再恢复：两次的列表互不影响，事件里各自的 `previous_reasons` 正确；
   - 两个请求：换代给两者都加上 `RECOVERY_REQUIRED`，恢复其中一个，另一个的原因列表不变；
   - 开始过的操作经 `recover(outcome="START_UNKNOWN")` 被新代际接管之后，仍不能判为未启动；
   - 开始过的操作判为 `ABANDONED`，补记用量之后，请求点名全部原因即可恢复。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_resume_holds_oracle.py tests/test_request_resume_oracle.py tests/test_durable_ledger_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_resume_holds.py tests/test_request_resume.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_gaps.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_durable_ledger_gaps.py tests/test_durable_ledger.py tests/test_restart_recovery_oracle.py tests/test_restart_recovery.py tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_verified_review_oracle.py tests/test_verified_review.py tests/test_verified_review_gaps.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.scheduler, sentinelmesh.durable_ledger; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04 在基线上失败，是因为新的规则还没有实现；V02 是因为新测试文件还不存在。V03 保护调度器、持久账本、重启恢复与桥接的已有行为。

## Definition of done
- D01：停止原因的记录与只读方法（Behavior contract 第 1 条）；证据：V01、V02。
- D02：`resume` 的新参数、比对与事件（第 2 条）；证据：V01、V02。
- D03：开始过的操作不能判为未启动（第 3 条）；证据：V01、V02、V03。
- D04：持久账本的读访问器（第 4 条）；证据：V01、V02。
- D05：旧测试改到新参数，用例一个不少（第 5 条）；证据：V02、review。
- D06：导入无副作用，已有模块不退化；Step 3 的测试齐全，不自行做变异检查（第 6 条）；证据：V03、V04、V07、review。
- D07：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
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
  - 阅读约 5 分钟；
  - 实现约 6 分钟；
  - 改旧测试与写新测试约 10 分钟；
  - 运行检查约 5 分钟（oracle 约 15 秒，全量约 175 秒）；
  - 执行报告约 3 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试（`tests/test_request_resume.py` 除外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
