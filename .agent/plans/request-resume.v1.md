# TASK: request-resume

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 50ef86acf12548e5f7e7be1f44f4e001e32c0651
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
让崩溃之后的系统能接着干活：
- 运维可以裁定一个 `START_UNKNOWN` 的操作：未启动、放弃或确认完成；
- 请求里的一切都定案、用量已知、额度不超之后，运维可以恢复这个请求；
- 持久账本把这两个新的变更方法记进日志。

在 `sentinelmesh/scheduler.py` 新增 `resolve_unknown`、`resume` 与终态 `ABANDONED`，在 `sentinelmesh/durable_ledger.py` 的 `MUTATING_METHODS` 末尾追加两个方法名，并让规划端 oracle `tests/test_request_resume_oracle.py` 与更新过的 `tests/test_durable_ledger_oracle.py` 全部通过。

## Non-goals
- 不改 `Scheduler` 的其他语义与已有私有属性（改动只限 Behavior contract 第 1–3 条）；不改 `restart_recovery.py`、`supervised_operation.py`、`posix_supervisor.py`、`supervised_record.py`、`adapter_run.py`、`runtime.py`、`persistence.py`。
- 不做提额，不改额度。
- 不核验运维的权限与证据：只记录运维名、证据摘要与说明。
- 不自动恢复请求，不自动裁定；裁定不恢复请求，恢复不裁定任何操作。
- 持久账本只追加两个方法名：日志格式名、编码、重放与失败关闭都不改。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `50ef86a` = master `a12b705` + 规划端的两处改动：
  - 新的 oracle `tests/test_request_resume_oracle.py`，11 个用例，其中 1 个启动真实的控制器子进程并用 SIGKILL 杀掉；
  - `tests/test_durable_ledger_oracle.py` 期望的 `MUTATING_METHODS` 末尾追加了 `"resolve_unknown"` 与 `"resume"`。

  两份 oracle 在基线上都失败。
- 设计说明：`.agent/design/request-resume.md`，第 5 节（用户决定）与第 6 节（规划端决定 P1–P7）。本合同的规则都来自那里。
- 请求的停止：
  - `stop`、`rollover`（`RECOVERY_REQUIRED`）、`unknown`（`START_UNKNOWN`）与 `_enforce`（`BUDGET_EXCEEDED`、`USAGE_UNKNOWN`）都只在 `stopped` 为空时写入，所以 `_Request.stopped` 记的是第一个原因；
  - 现在没有任何方法把它清空。
- 已有的状态集合：
  - `LIVE = {"RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "START_UNKNOWN"}`：占并发名额；与 `REVIEW_PENDING` 一起让同一 task 不能再领取（`E_ACTIVE_TASK`）；
  - `UNRESOLVED = {"START_UNKNOWN", "REVIEW_PENDING", "ACCEPTED", "REJECTED"}`：`_snapshot` 里，这些状态的操作用量不完整时算一个未知操作，预留按“已知用量之外仍占着”计；`NOT_STARTED` 只算已知用量；`CANCELLED_NOT_STARTED` 整个跳过。
  - 未知用量让领取报 `E_USAGE_UNKNOWN`、交接报 `E_BUDGET_GUARD`。
- 已有的出路：
  - 迟到计量：`observe` 与 `settle` 对任何状态的操作都能补记用量，不受代际限制；
  - `fixture_verdict` 只裁决 `REVIEW_PENDING`；
  - `recover` 只接受代际比当前旧的操作；
  - `cancel_unstarted` 只接受 `RESERVED`；
  - `_release_resources(op)` 只释放资源占用。
- 写法约定：
  - 事件载荷先用 `_encode_payload` 编码，再改账本；
  - 代际用 `_epoch(generation)` 核对（精确 `int` 且等于当前代际，否则 `E_FENCED`）；
  - 摘要用 `_HEX64`；
  - `scheduler.py` 已经从 `runtime` 导入 `_runtime_text`，它的规则是：非空、无 NUL、无控制字符、无代理字符。
- 持久账本：
  - 按 `MUTATING_METHODS` 为每个名字生成包装方法，签名取自 `Scheduler`，记录绑定后的全部参数；
  - durable-ledger 的 oracle 逐个比较这些签名；
  - 编码用 `ensure_ascii=False`，所以含孤立代理字符的说明必须在内存调用之前就被拒绝，否则追加失败会让实例失效。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 11/11，全量 714 passed；
  - 30 个故意制造的偏差抓住 29 个，漏掉的是 `_runtime_text` 已经重复的精确类型检查（等价写法）。其中包括：放弃不算未解决、放弃仍算在途、裁定不释放资源、裁定顺带恢复请求、恢复不比对原因、当前代际的运行也挡住恢复、只查请求的额度、检查顺序颠倒、恢复清空用量。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - sentinelmesh/durable_ledger.py
- allowed_new_files:
  - tests/test_request_resume.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `sentinelmesh/runtime.py`, `tests/test_request_resume_oracle.py`, `tests/test_durable_ledger_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **终态 `ABANDONED`**：
   - `UNRESOLVED` 加入 `"ABANDONED"`；`LIVE` 不变。
   - 于是放弃的操作不占并发名额，它的 task 可以重领，尝试次数照算；用量不完整时仍是未知用量，预留仍按已知用量之外占着，补记完整之后才放开。
2. **`resolve_unknown(self, operation, generation, *, resolution, operator, evidence_hash, note, candidate_hash=None) -> None`**
   - 按这个顺序检查，先失败的先报，全部在任何改动之前：
     1. `resolution` 是精确 `str`，取值 `"NOT_STARTED"`、`"ABANDONED"`、`"FINISHED"` 之一，否则 `E_RESOLUTION`；
     2. `evidence_hash` 是 64 位小写十六进制的精确 `str`，否则 `E_EVIDENCE_HASH`；
     3. `FINISHED` 的 `candidate_hash` 必须是 64 位小写十六进制的精确 `str`，另外两种必须是 `None`，否则 `E_CLEANUP_OR_CANDIDATE`；
     4. `operator` 是精确 `str`，不超过 200 个字符，并符合 `_runtime_text` 的规则，否则 `E_OPERATOR`；
     5. `note` 同上，但不超过 500 个字符，否则 `E_NOTE`；
     6. `generation` 是当前代际，否则 `E_FENCED`；
     7. `operation` 是精确 `str` 并且是已知的操作，否则 `E_OPERATION_UNKNOWN`；
     8. 操作的状态是 `START_UNKNOWN`，否则 `E_RESOLVE_STATE`。
   - 通过之后：
     - 状态变为 `NOT_STARTED`、`ABANDONED` 或 `REVIEW_PENDING`（分别对应三种结论）；`FINISHED` 同时记下候选摘要，之后可以用 `fixture_verdict` 裁决；
     - 释放这个操作的资源占用；owner、代际、用量与预留都不改；
     - 发出 `scheduler.unknown_resolved`，`operation_id` 是这个操作，载荷恰好是 `{"resolution", "state", "operator", "evidence", "note"}`，`FINISHED` 另有 `"candidate"`；
     - 请求的停止不变。
3. **`resume(self, request, generation, *, expected_reason, operator, note) -> None`**
   - 按这个顺序检查，先失败的先报，全部在任何改动之前：
     1. `operator`（`E_OPERATOR`）与 `note`（`E_NOTE`），规则同第 2 条；
     2. `expected_reason` 是非空的精确 `str`，否则 `E_STOP_REASON`；
     3. `generation` 是当前代际，否则 `E_FENCED`；
     4. `request` 是精确 `str` 并且是已知的请求，否则 `E_REQUEST_UNKNOWN`；
     5. 请求已停止，否则 `E_NOT_STOPPED`；
     6. 停止原因等于 `expected_reason`，否则 `E_STOP_REASON_CHANGED`；
     7. 请求里没有 `START_UNKNOWN` 的操作，否则 `E_RESUME_UNKNOWN`；
     8. 请求里没有代际比当前旧、并且仍在 `RESERVED`、`HANDOFF_ACCEPTED`、`RUNNING` 的操作，否则 `E_RESUME_UNDECIDED`。当前代际里仍在进行的操作不妨碍恢复；
     9. 先对请求本身与它的每一个 task 检查未知用量（`E_RESUME_USAGE_UNKNOWN`），全部没有之后，再检查已用加预留是否超过额度（`E_RESUME_BUDGET`）。
   - 通过之后：
     - 只把停止清空；
     - 发出 `request.resumed`，`operation_id` 为 `None`，载荷恰好是 `{"request_id", "previous_reason", "operator", "note"}`；
     - 尝试次数、用量、预留与历史事件都不动。
4. **持久账本**：`MUTATING_METHODS` 末尾按顺序追加 `"resolve_unknown"`、`"resume"`。其余不变，日志格式名仍是 `sentinelmesh-ledger-v1`。
5. **导入无副作用**：导入 `sentinelmesh.scheduler` 与 `sentinelmesh.durable_ledger` 仍不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 5–6 节、两份 oracle，以及 `scheduler.py`（`LIVE`、`UNRESOLVED`、`_snapshot`、`claim`、`recover`、`cancel_unstarted`、`stop`、`rollover`、`_release_resources`、`_encode_payload`）与 `durable_ledger.py` 的 `MUTATING_METHODS`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改 `sentinelmesh/scheduler.py`，第 4 条修改 `sentinelmesh/durable_ledger.py`。
3. 新建 `tests/test_request_resume.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 三种结论都在持久账本上做一遍，重开之后状态、快照与事件都相同；
   - 用量在裁定之前就已补记完整：放弃之后预留立即放开；
   - 确认完成之后判为 `REJECTED`：请求恢复之后，这个 task 可以作为新的尝试重领；
   - 两个请求都停止：恢复其中一个，另一个仍停止，它的操作不受影响；
   - 恢复之后又记下超额的用量：请求照常因 `BUDGET_EXCEEDED` 再次停止，可以再次在额度满足后恢复；
   - 已经恢复的请求再恢复一次：`E_NOT_STOPPED`，什么都不变。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_request_resume_oracle.py tests/test_durable_ledger_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_request_resume.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_gaps.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_durable_ledger_gaps.py tests/test_durable_ledger.py tests/test_restart_recovery_oracle.py tests/test_restart_recovery.py tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_verified_review_oracle.py tests/test_verified_review.py tests/test_verified_review_gaps.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.scheduler, sentinelmesh.durable_ledger; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04 在基线上失败，是因为两个方法还不存在、方法列表还没追加；V02 是因为测试文件还不存在。V03 保护调度器、持久账本、重启恢复与桥接的已有行为。

## Definition of done
- D01：`ABANDONED` 的计量与并发规则（Behavior contract 第 1 条）；证据：V01、V02。
- D02：`resolve_unknown` 的检查顺序、三种结论与事件（第 2 条）；证据：V01、V02。
- D03：`resume` 的检查顺序、前提与事件，历史不动（第 3 条）；证据：V01、V02。
- D04：持久账本追加两个方法，日志格式不变（第 4 条）；证据：V01、V03。
- D05：导入无副作用，已有模块不退化（第 5 条）；证据：V03、V04、V07。
- D06：Step 3 的测试齐全，不自行做变异检查；证据：V02、review。
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
  - 实现约 7 分钟；
  - 测试约 9 分钟；
  - 运行检查约 5 分钟（oracle 约 10 秒，全量约 170 秒）；
  - 执行报告约 3 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
