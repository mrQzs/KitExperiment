# TASK: scheduler-supervised-transitions

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: befbba9b95f874d39cc0df6a30a8e4abbcfb761c
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
给 `sentinelmesh/scheduler.py` 的 `Scheduler` 增加接入监督器所需的迁移：
- 新终态 NOT_STARTED 与 `release_proven_unstarted()`；
- `unknown()` 增加原因参数，`mark_running()` 可记录进程身份；
- controller 换代后按证据恢复在途操作的 `recover()`；
- 预算快照对 NOT_STARTED 的口径；

并让规划端 oracle `tests/test_scheduler_supervised_oracle.py` 全部通过。

## Non-goals
- 不接入监督器、记录模块或 adapter（下一份合同 `supervised-operation-bridge` 做）；不启动任何进程。
- 不改 `Scheduler` 已有迁移的行为，只做本合同列出的增补；不实现 `resume`，不做持久化。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml` 与 oracle；不联网、不读取凭据。

## Repository facts
- 基线 `befbba9` = master `b454938` + 规划端 oracle（10 个用例，纯内存）。规划端用一份草稿验证过（草稿未提交）：
  - oracle 全部通过，全量 326 passed；
  - oracle 在基线上全部失败；
  - 11 个草稿变异都被抓住。
- 设计与用户决定：`.agent/design/scheduler-supervisor-wiring.md` 第 5、6 节。
- `Scheduler` 的现状：
  - 状态：RESERVED → HANDOFF_ACCEPTED → RUNNING → REVIEW_PENDING → ACCEPTED / REJECTED；另有 START_UNKNOWN（`unknown()`）与 CANCELLED_NOT_STARTED（`cancel_unstarted()`，只接受 RESERVED）；
  - `LIVE = {"RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "START_UNKNOWN"}`，`UNRESOLVED = {"START_UNKNOWN", "REVIEW_PENDING", "ACCEPTED", "REJECTED"}`；
  - `handoff()` 会把 dispatch 用量记为 1（final）；
  - `_snapshot()` 整个跳过 CANCELLED_NOT_STARTED 的操作；
  - `claim()` 按该 task 的全部操作计尝试次数；
  - `rollover()` 把代际加一，并停止全部 request（原因 RECOVERY_REQUIRED）；
  - `_owned()` 要求当前代际，且 owner 与操作的 generation 都匹配，否则报 `E_FENCED`；
  - 事件经 `_emit()` 追加，包含当时的 generation。
- `sentinelmesh.runtime.ProcessIdentity(pid, boot_id, executable_sha256, worktree_id)` 在构造时校验四个字段，错误为 `RuntimeErrorCode`（`ValueError` 的子类）。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
- allowed_new_files:
  - tests/test_scheduler_supervised.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `tests/test_scheduler_supervised_oracle.py`, `tests/test_reference.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下文的“哈希”指恰好 64 个小写十六进制字符的 str。所有参数检查都在任何改动之前完成：被拒绝的调用不改变任何操作的 state、owner、generation、candidate_hash，不改变资源占用与 request 的停止状态，也不追加事件。

1. **NOT_STARTED 与 `release_proven_unstarted(operation, owner, generation, *, evidence_hash)`**
   - `evidence_hash` 不是哈希时报 `E_EVIDENCE_HASH`。
   - 按 `_owned()` 检查代际与 owner（不符报 `E_FENCED`，操作不存在报 `E_OPERATION_UNKNOWN`）。
   - 操作必须处于 HANDOFF_ACCEPTED，否则报 `E_NOT_STARTED_STATE`。
   - 成功时：state 变为 NOT_STARTED；释放该操作的资源占用；追加事件 `scheduler.not_started`，payload 为 `{"evidence": evidence_hash}`。request 不停止。
   - NOT_STARTED 不属于 LIVE，也不属于 UNRESOLVED（这两个集合不变）。
2. **预算快照的口径**（`_snapshot()`）
   - 对 NOT_STARTED 的操作，每个维度只把已观察到的用量计入 `known`（没有观察或 amount 为 None 时计 0），不计 `held`，也不计 `unknown_operations`。
   - 其余状态的计算不变；CANCELLED_NOT_STARTED 仍整个跳过。
3. **`mark_running(operation, owner, generation, *, process_identity=None)`**
   - 为 None 时行为不变，事件 `run.started` 的 payload 仍是 `{}`。
   - 不为 None 时，它必须是 dict，键恰好是 `pid`、`boot_id`、`executable_sha256`、`worktree_id`，并且 `ProcessIdentity(**process_identity)` 能构造成功，否则报 `E_IDENTITY_SHAPE`。
   - 通过时，`run.started` 的 payload 为 `{"process_identity": <该 dict 的副本>}`。
4. **`unknown(operation, owner, generation, *, reason="START_UNKNOWN")`**
   - `reason` 必须是 str，且是 `"START_UNKNOWN"` 或 `"CLEANUP_UNCONFIRMED"`，否则报 `E_UNKNOWN_REASON`。
   - 其余行为不变（只接受 HANDOFF_ACCEPTED 与 RUNNING，进入 START_UNKNOWN 并停止 request），事件 `scheduler.start_unknown` 的 payload 改为 `{"reason": reason}`。
5. **`recover(operation, owner, generation, *, outcome, evidence_hash, candidate_hash=None)`**
   - 按下列顺序检查，第一条不满足即报错：
     1. `outcome` 必须是 str，且是 `"NOT_STARTED"`、`"START_UNKNOWN"`、`"FINISHED"` 之一 → `E_RECOVERY_OUTCOME`；
     2. `evidence_hash` 必须是哈希 → `E_EVIDENCE_HASH`；
     3. `outcome == "FINISHED"` 时 `candidate_hash` 必须是哈希；其他 outcome 时它必须是 None → 都报 `E_CLEANUP_OR_CANDIDATE`；
     4. `generation` 必须等于当前代际 → `E_FENCED`；
     5. 操作必须存在 → `E_OPERATION_UNKNOWN`；
     6. 操作的 generation 必须小于 `generation`（已被换代隔离）→ `E_RECOVERY_NOT_FENCED`；
     7. 操作的 state 必须是 HANDOFF_ACCEPTED、RUNNING 或 START_UNKNOWN → `E_RECOVERY_STATE`；
     8. 操作所属的 request 必须已停止 → `E_RECOVERY_NOT_STOPPED`；
     9. `outcome == "NOT_STARTED"` 而操作是 RUNNING（已登记启动）→ `E_RECOVERY_CONFLICT`。
   - 通过后接管：操作的 owner 与 generation 改为参数值；然后：
     - NOT_STARTED：state 变为 NOT_STARTED，释放资源占用；
     - START_UNKNOWN：state 变为 START_UNKNOWN，保留资源占用；
     - FINISHED：state 变为 REVIEW_PENDING，`candidate_hash` 记为参数值，释放资源占用。
   - 追加事件 `scheduler.recovered`，payload 为 `{"from_generation": <接管前的 generation>, "outcome": outcome, "evidence": evidence_hash, "state": <新 state>}`；FINISHED 时再加 `"candidate": candidate_hash`。
   - request 的停止状态不变。
6. **其余不变**：`claim`、`handoff`、`renew`、`observe`、`settle`、`finish`、`fixture_verdict`、`cancel_unstarted`、`stop`、`rollover` 的行为不变；已有测试照常通过。导入 `sentinelmesh.scheduler` 不导入 ctypes、不读文件、不读环境变量。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 5、6 节、oracle 与 `scheduler.py`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–6 条修改 `sentinelmesh/scheduler.py`。
3. 新建 `tests/test_scheduler_supervised.py`，覆盖下列 oracle 没有覆盖的用例。每个拒绝用例都先验证合法基线，再只改一处，并断言具体的 code 以及账本没有变化：
   - `recover` 在 request 未停止时报 `E_RECOVERY_NOT_STOPPED`（换代后直接把该 request 的停止状态清空来构造前提）；
   - 当前代际先 `unknown()`、再换代：从 START_UNKNOWN 恢复为 FINISHED 进 REVIEW_PENDING，或恢复为 NOT_STARTED；
   - `release_proven_unstarted` 传入旧代际报 `E_FENCED`；对 START_UNKNOWN 报 `E_NOT_STARTED_STATE`；
   - `unknown(reason=None)` 报 `E_UNKNOWN_REASON`；
   - NOT_STARTED 之后补记一条 tokens 用量（final，数量 5）：快照中 tokens 的 known 为 5、held 为 0；
   - NOT_STARTED 释放并发槽：request 并发为 1 时，释放后另一个 task 可以领取；
   - `scheduler.recovered` 事件记录的 generation 是新代际；
   - `mark_running` 记录的是身份副本：调用之后修改传入的 dict，事件 payload 不变。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_supervised_oracle.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_supervised.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.scheduler; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01 与 V04 在基线上失败，是因为新迁移还不存在；V02 在基线上失败，是因为测试文件还不存在。V03、V07 保护已有行为。

## Definition of done
- D01：NOT_STARTED、`release_proven_unstarted` 与快照口径（Behavior contract 第 1、2 条）；证据：V01、V02。
- D02：`mark_running` 的身份与 `unknown` 的原因（第 3、4 条）；证据：V01、V02。
- D03：`recover` 的检查顺序、接管与三种结果（第 5 条）；证据：V01、V02。
- D04：已有行为不变（第 6 条）；证据：V03、V04、V07。
- D05：Step 3 的测试齐全，不自行做变异检查；证据：V02、review。
- D06：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle 或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1500
- max_wall_seconds_total: 3000
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 4 分钟；
  - 实现约 6 分钟；
  - 测试约 8 分钟；
  - 运行检查约 3 分钟（oracle 约 1 秒，全量约 95 秒）；
  - 执行报告约 4 分钟；
  - 合计约 25 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍（包括收紧），写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
