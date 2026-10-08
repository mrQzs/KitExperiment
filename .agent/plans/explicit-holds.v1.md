# TASK: explicit-holds

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 43b0e7c166d63a91829683548bf77ed102ba08eb
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
补上 resume-holds 复审留下的两处覆盖面缺口：
- 交给过受监督运行（交接时记有运行描述）的操作，运维不能再判为未启动：桥接要等运行结束才登记开始，控制器在运行中途死掉时账本没见过开始，现有的冲突检查拦不住；
- 运维显式的每一次 `stop()` 单独记一项，同名的恢复时点名两次；自动原因（预算、换代、未知启动）仍只记一次。

修改 `sentinelmesh/scheduler.py`，把 `tests/test_resume_holds.py` 里钉住旧规则的三个用例改到新规则，并让规划端的四份 oracle 全部通过：新的 `tests/test_explicit_holds_oracle.py`，更新过的 `tests/test_resume_holds_oracle.py` 与 `tests/test_request_resume_oracle.py`，以及不变的 `tests/test_durable_ledger_oracle.py`。

## Non-goals
- 不改 `recover`：它是按记录接管的路径，重启恢复靠它把两份记录都为空的交接判为未启动。
- 不改 `Scheduler` 的其他语义；不改 `durable_ledger.py`、`restart_recovery.py`、`supervised_operation.py`、`posix_supervisor.py`、`supervised_record.py`、`adapter_run.py`、`runtime.py`、`persistence.py`。
- 不改桥接登记开始的时机。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与其他已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `43b0e7c` = master `f836ac9` + 规划端的三处 oracle 改动：
  - 新的 `tests/test_explicit_holds_oracle.py`，9 个用例；
  - `tests/test_resume_holds_oracle.py` 改为同名的显式暂停记两项、期望原因可以重复；
  - `tests/test_request_resume_oracle.py` 的真实进程用例加了一条断言：被监督的崩溃运行判未启动报 `E_RESOLVE_CONFLICT`。

  这三份 oracle 在基线上失败。
- 设计说明：`.agent/design/request-resume.md` 第 10 节（用户决定与规划端决定 P13–P15）。
- 停止原因：
  - 模块函数 `_record_stop_reason(root, reason)` 只在列表里还没有这个原因时追加；
  - `stop`、`rollover`、`unknown` 与 `_enforce` 现在都调用它。
- `resume`：先复制 `expected_reasons`，在副本上逐个检查元素是非空精确 `str` 且互不重复（用一个 `named` 集合），不合格报 `E_STOP_REASON`；比对 `expected != list(root.stop_reasons)`。
- `resolve_unknown`：
  - 在状态检查（`E_RESOLVE_STATE`）之后，`resolution == "NOT_STARTED" and op.started` 报 `E_RESOLVE_CONFLICT`；
  - 交接时记下的运行描述存在 `op.run` 里，没有描述时是 `None`；桥接的每次交接都记有描述。
- `recover` 的冲突条件是“结论是 `NOT_STARTED`，而操作当前是 `RUNNING` 或开始过”，本合同不改。
- `tests/test_resume_holds.py` 里有三个用例钉住了旧规则，在新规则下会失败（规划端草稿实测）：
  - `test_the_reasons_are_kept_in_first_seen_order_when_they_interleave`：同名的显式暂停记一项；
  - `test_a_second_stop_after_the_resume_starts_a_new_list`：同上；
  - `test_the_expected_reasons_must_be_a_list_of_distinct_reasons`：重复的期望原因报 `E_STOP_REASON`。

  其余用例与 `tests/test_request_resume.py` 在新规则下照常通过。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - 四份 oracle 45/45；把上面三个用例改到新规则之后，全量 771 passed；
  - 9 个故意制造的偏差全部抓住：显式暂停又去重、自动原因也重复记、期望原因又不许重复、冲突不看运行描述、放弃也被挡、`recover` 也挡受监督的运行、先查冲突再查状态、按集合比对原因、只合并相邻的同名暂停。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - tests/test_resume_holds.py
- allowed_new_files:
  - tests/test_explicit_holds.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `sentinelmesh/runtime.py`, `tests/test_explicit_holds_oracle.py`, `tests/test_resume_holds_oracle.py`, `tests/test_request_resume_oracle.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_request_resume.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **显式暂停**：`stop()` 每次都把原因追加进停止原因列表，不去重。`rollover`、`unknown` 与 `_enforce` 不变：只在列表里还没有这个原因时追加。`stopped` 仍是第一个原因。
2. **期望原因**：`resume` 的 `expected_reasons` 不再要求互不重复，只要求是不空的精确 `list`、元素是非空精确 `str`（仍然先复制，再在副本上检查），不合格报 `E_STOP_REASON`。比对仍是按顺序与停止原因列表完全相等（`E_STOP_REASON_CHANGED`）。`resume` 的其余检查、顺序与事件都不变。
3. **受监督的运行**：`resolve_unknown` 的冲突条件扩展为“结论是 `NOT_STARTED`，而操作开始过，或者记有运行描述”，仍报 `E_RESOLVE_CONFLICT`，仍在状态检查之后。`ABANDONED` 与 `FINISHED` 不受影响；没有运行描述、也没开始过的操作仍可判为 `NOT_STARTED`。`recover` 不改。
4. **旧测试**：把 `tests/test_resume_holds.py` 里上面列出的三个用例改到新规则：同名的显式暂停记两项并点名两次；重复的期望原因是合法的形状，不再列在报 `E_STOP_REASON` 的表里。用例名里写着旧规则的（`..._distinct_reasons`）按新规则改名。其余用例不改，不得删除或跳过用例。
5. **导入无副作用**：导入 `sentinelmesh.scheduler` 仍不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 10 节、四份 oracle，以及 `scheduler.py` 的 `_record_stop_reason`、`stop`、`resume`、`resolve_unknown` 与 `tests/test_resume_holds.py`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改 `sentinelmesh/scheduler.py`，第 4 条修改 `tests/test_resume_holds.py`。
3. 新建 `tests/test_explicit_holds.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 同名的显式暂停与自动原因交错出现：显式的每次都记，自动的只记第一次；
   - 恢复、再用同一个原因暂停两次、再恢复：第二轮的列表只有那两项；
   - 两个请求：在一个请求上重复暂停，另一个请求的原因列表不变；
   - 既开始过、又记有运行描述的操作：判未启动报 `E_RESOLVE_CONFLICT`，判放弃之后补记用量即可恢复；
   - 记有运行描述的交接，在新代际里 `recover(outcome="NOT_STARTED")` 照常成功（`recover` 不改）。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_explicit_holds_oracle.py tests/test_resume_holds_oracle.py tests/test_request_resume_oracle.py tests/test_durable_ledger_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_explicit_holds.py tests/test_resume_holds.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_request_resume.py tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_gaps.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_durable_ledger_gaps.py tests/test_durable_ledger.py tests/test_restart_recovery_oracle.py tests/test_restart_recovery.py tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_verified_review_oracle.py tests/test_verified_review.py tests/test_verified_review_gaps.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.scheduler, sentinelmesh.durable_ledger; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04 在基线上失败，是因为新规则还没有实现；V02 是因为新测试文件还不存在。V03 保护调度器、持久账本、重启恢复、请求恢复与桥接的已有行为。

## Definition of done
- D01：显式暂停每次都记，自动原因只记一次（Behavior contract 第 1 条）；证据：V01、V02。
- D02：期望原因可以重复，比对不变（第 2 条）；证据：V01、V02。
- D03：记有运行描述的操作不能判为未启动，`recover` 不变（第 3 条）；证据：V01、V02、V03。
- D04：三个旧用例改到新规则，其余不动（第 4 条）；证据：V02、review。
- D05：导入无副作用，已有模块不退化；Step 3 的测试齐全，不自行做变异检查（第 5 条）；证据：V03、V04、V07、review。
- D06：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
  - 实现约 3 分钟；
  - 改旧测试与写新测试约 10 分钟；
  - 运行检查约 5 分钟（oracle 约 15 秒，全量约 175 秒）；
  - 执行报告约 3 分钟；
  - 合计约 26 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试（`tests/test_resume_holds.py` 除外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
