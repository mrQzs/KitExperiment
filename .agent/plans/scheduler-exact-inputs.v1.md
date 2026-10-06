# TASK: scheduler-exact-inputs

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 04c981c79c392bdc38d3d5763f3c4dbb6e4c402c
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
修改 `sentinelmesh/scheduler.py`，让 `Scheduler` 已有方法被拒绝的调用不改变账本：
- 按精确类型检查输入；
- 所有检查（包括事件 payload 的规范 JSON 编码）在第一次修改之前完成；
- `settle()` 传入映射时，全部条目一起记录或一项都不记录；
- `stop()` 的原因不能为空；
- `scheduler.recovered` 写明接管后的 owner。

并让规划端 oracle `tests/test_scheduler_exact_inputs_oracle.py` 全部通过。

## Non-goals
- 不改合法调用的结果：状态迁移、返回值、用量、事件与快照都不变，第 5 条新增的 `"owner"` 除外。
- 不改 `snapshot()`、`validate_workflow()` 等只读函数，不做持久化，不接入监督器或 adapter，不启动进程。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `04c981c` 由下列提交依次组成：
  - master `139fd9d`；
  - 规划端 oracle `1ef2e6f`：`tests/test_scheduler_exact_inputs_oracle.py`，56 个用例；
  - `04c981c`：规划端把 5 处对 `scheduler.recovered` 整个 payload 的断言加上 `"owner": "owner-2"`：
    - `tests/test_scheduler_supervised_oracle.py` 2 处；
    - `tests/test_scheduler_supervised.py` 2 处；
    - `tests/test_supervised_operation_oracle.py` 1 处。
- 基线上：
  - oracle 55 个失败；`test_settle_mapping_success_is_observe_in_order` 通过，它守护成功时的事件顺序；
  - 上面 5 处断言失败。
- 规划端用一份草稿修复验证过（草稿未提交）：
  - oracle 56/56，全量 453 passed；
  - 17 份只修一部分的草稿，各自只让对应规则的用例失败。
- oracle 比较账本时，连同每个值的精确类型一起比较：
  - 直接读取 `Scheduler` 的私有属性 `_generation`、`_requests`、`_tasks`、`_operations`、`_idempotency`、`_resource_holds`、`_events`；
  - 用 `vars()` 读取 `_Request`、`_Task`、`_Operation`、`_Usage` 的字段。

  不要给这些属性或类改名，也不要把 dataclass 改成 `slots`。
- `_emit()` 用 `canonical()` 编码 payload：
  - 只接受精确的 str 键，以及 str、int、bool、None 值；
  - 整数的绝对值不超过 2^53−1，超出时报 `E_JSON_INTEGER`。
- 已有方法目前在 `canonical()` 之前就改了账本，例如：
  - `finish()` 先改状态、释放资源，再在 `_emit()` 里报 `E_JSON_TYPE`；
  - `observe()` 先写入用量，再报错。
- 参考实现（`code-kit-v2/07-workflows-and-scheduling-审阅增强版.md` 中的 `Ledger`）的每个修改都在 SQLite 事务里，出错就回滚。内存账本没有事务，只能先检查、后修改。
- 设计说明：`.agent/design/scheduler-supervisor-wiring.md` 第 7 节列出了这些残留。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
- allowed_new_files:
  - tests/test_scheduler_exact_inputs.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `tests/test_scheduler_exact_inputs_oracle.py`, `tests/test_scheduler_supervised_oracle.py`, `tests/test_scheduler_supervised.py`, `tests/test_supervised_operation_oracle.py`, `tests/test_reference.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **被拒绝的调用不改变账本**
   - 适用于以下方法：`seed_request`、`seed_task`、`claim`（即 `reserve`）、`handoff`、`mark_running`、`renew`、`observe`、`settle`、`finish`、`fixture_verdict`、`unknown`、`release_proven_unstarted`、`recover`、`cancel_unstarted`、`stop`。
   - 这些方法抛出 `ContractError` 时，以下各项的值与类型都与调用前相同：
     - 代际；
     - 每个 request（含 `stopped`）与每个 task；
     - 每个操作的全部字段（含 `usage`）；
     - 幂等键与资源占用；
     - `_events`。
   - 做法：所有检查在第一次修改之前完成，其中包括要写进事件的 payload 的 `canonical()` 编码。
     - 编码失败时报 `canonical()` 的 code（例如超出范围的整数报 `E_JSON_INTEGER`），同样不改变账本；
     - 不要写成“先改、出错再回滚”。
2. **精确类型**
   - “是 str”指 `type(x) is str`，“是 int”指 `type(x) is int`。
   - str 子类（包括 `StrEnum` 成员）一律按“不是 str”处理；`bool`、int 子类与 float 一律按“不是 int”处理。
   - 不可哈希的值（例如 list）也照常报下表的 code，不能冒出 `TypeError`。
   - 下表的检查先于规范 JSON 编码，所以这些参数不会再报 `E_JSON_TYPE` 或 `E_JSON_KEY`：

   | 方法 | 参数与要求 | 不符时的 code |
   |---|---|---|
   | `handoff`、`mark_running`、`renew`、`finish`、`unknown`、`release_proven_unstarted`、`cancel_unstarted` | generation 是 int，并等于当前代际 | `E_FENCED` |
   | 同上，以及 `observe`、`settle`、`recover` | operation 是 str | `E_OPERATION_UNKNOWN` |
   | `fixture_verdict` | operation 是 str | `E_VERDICT_CANDIDATE` |
   | `handoff`、`mark_running`、`renew`、`finish`、`unknown`、`release_proven_unstarted`、`recover` | owner 是 str | `E_FENCED` |
   | `claim` | request、task 是 str | `E_SCOPE_UNKNOWN` |
   | `claim` | key、owner 是 str | `E_CLAIM_SHAPE` |
   | `claim` | resource_keys 的每一项是 str | `E_RESOURCE_KEYS` |
   | `seed_request` | request、repo 是 str | `E_REQUEST_SHAPE` |
   | `seed_task` | request 是 str | `E_REQUEST_UNKNOWN` |
   | `seed_task` | task 是 str | `E_TASK_SHAPE` |
   | `seed_request`、`seed_task`（caps）与 `claim`（reservation） | 每个键是 str | `E_BUDGET_DIMENSION` |
   | `observe`、`settle` | dimension（含映射的每个键）、call_key、evidence_hash 是 str | `E_OBSERVATION_SHAPE` |
   | `finish` | candidate_hash 是 str | `E_CLEANUP_OR_CANDIDATE` |
   | `fixture_verdict` | candidate_hash 是 str | `E_VERDICT_CANDIDATE` |
   | `fixture_verdict` | evidence_hash 是 str（`None`、bytes 同样报这个 code） | `E_VERDICT_SHAPE` |
   | `stop` | request 是 str | `E_REQUEST_UNKNOWN` |
   | `stop` | reason 是非空的 str | `E_STOP_REASON` |

   - `handoff`、`mark_running`、`renew`、`finish`、`unknown`、`release_proven_unstarted` 按 generation、operation、owner 的顺序检查这三项。
     - 各方法原有的其他前置检查位置不变，例如 `finish` 先查候选，`mark_running` 先查身份。
     - `recover` 的顺序见第 5 条。
   - 表中的格式要求只是新增的前置检查：值合格之后，原有的检查与 code 不变，例如 operation 不存在仍报 `E_OPERATION_UNKNOWN`，owner 不符仍报 `E_FENCED`。
   - `settle` 先检查 operation 与 dimension（或映射的全部键），再取 call_key 与 evidence_hash 的缺省值（写法不变，见第 3 条），取得的值再按上表检查。
3. **`settle` 传入映射时，全部记录或一项都不记录**
   - 先把条目复制成列表，之后只用这份副本。
   - 每一项的 call_key 是 `local:<operation>:<键>`；没有传 evidence_hash 时，evidence 是 `digest({"operation": …, "dimension": 键, "value": 值})`。两者都与现在相同。
   - 按下面的顺序检查，报最先发现的问题的 code：
     1. operation 是 str；
     2. 每个键是 str；
     3. operation 存在；
     4. 按映射顺序逐项做与 `observe` 相同的全部检查，包括与已记录用量的冲突、缺省 evidence 的计算，以及事件 payload 的编码。
   - 发现任何问题：一项都不记录。
   - 没有问题：按顺序逐项记录。每一项的用量、事件，以及记录之后的预算检查（`_enforce`），都与按同样顺序、用同样参数逐项调用 `observe` 相同；返回值也不变。
   - 映射为空：operation 仍须存在，否则报 `E_OPERATION_UNKNOWN`；返回 `{}`，账本不变。
   - 传入单个维度时，缺省值的写法不变：
     - call_key 为 `call_key or "local:<operation>:<dimension>"`；
     - evidence 为 `evidence_hash or digest({"operation": …, "dimension": …, "amount": …})`。
4. **`stop` 的原因**：reason 必须是非空的 str。原来空字符串会把 `stopped` 设成 `""`，request 照样继续运行。
5. **`scheduler.recovered` 写明新 owner**
   - payload 增加 `"owner"`，值为接管后的 owner。
   - `recover` 现有的检查顺序：outcome → evidence_hash → candidate_hash → 代际 → 操作存在 → 操作已被隔离（`op.generation < generation`）→ 状态 → request 已停止 → 结局与状态冲突。新增的两项放在：
     - owner 与代际一起检查：owner 不是 str，与代际不合格一样报 `E_FENCED`；
     - operation 在“操作存在”这一步检查：不是 str，报 `E_OPERATION_UNKNOWN`。
   - 其余步骤与 code 不变。
6. **导入无副作用**：导入 `sentinelmesh.scheduler` 不导入 ctypes。

## Steps
1. 读 AGENTS.md、本合同、oracle、设计说明第 7 节与 `sentinelmesh/scheduler.py`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–6 条修改 `sentinelmesh/scheduler.py`。
   - 只调整检查的位置与必要的结构，例如把用量检查拆成“判定”与“记录”两步，或者先编码 payload 再修改。
   - 不改合法调用的结果。
3. 新建 `tests/test_scheduler_exact_inputs.py`，覆盖下列 oracle 没有覆盖的用例。
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处；
     - 断言具体的 code，并断言账本不变；
     - 比较账本的函数自己写，值与类型都要比较；不要导入 oracle 文件。
   - 非 str 的值（oracle 主要用 str 子类）：
     - `handoff` 与 `finish` 的 owner 为 `None`：`E_FENCED`；
     - `claim` 的 reservation 键为 `5`：`E_BUDGET_DIMENSION`；resource 键为 `5`：`E_RESOURCE_KEYS`；
     - `seed_task` 的 caps 键为 `None`：`E_BUDGET_DIMENSION`；
     - `observe` 的 call_key 为 `5`、evidence_hash 为 `None`：`E_OBSERVATION_SHAPE`；
     - `settle` 映射的键为 `5`：`E_OBSERVATION_SHAPE`；
     - `finish` 的 candidate_hash 为 `None`：`E_CLEANUP_OR_CANDIDATE`；`fixture_verdict` 的 candidate_hash 为 `None`：`E_VERDICT_CANDIDATE`。
   - `settle` 映射：
     - 第一项与已记录的观察完全相同、第二项是新观察：返回 `{"tokens": "REPLAY", "cost": "APPLIED"}`；
     - dict 子类的映射照常处理；
     - 空映射：对已存在的操作返回 `{}` 且账本不变；对不存在的操作报 `E_OPERATION_UNKNOWN`；
     - 第二项的值为 `2**53`：整体拒绝，报 `E_JSON_INTEGER`，第一项不记录。
   - `stop`：
     - 空原因被拒绝后，request 的 `stopped` 仍为 `None`；
     - 随后合法的 `stop` 让 request 停止：`stopped` 为该原因，之后对这个 request 的 `claim` 报 `E_STOPPED`。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_exact_inputs.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V03（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要）：`python -c "import sys, sentinelmesh.scheduler; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01 是规划端 oracle，在基线上失败；V02 在基线上失败，是因为测试文件还不存在。V03、V04 在基线上失败，是因为规划端改过的 5 处断言要求 payload 带 owner。V05 包含以上各项。

## Definition of done
- D01：被拒绝的调用不改变账本，先检查后修改（Behavior contract 第 1 条）；证据：V01、V02。
- D02：精确类型表（第 2 条）；证据：V01、V02。
- D03：`settle` 映射全部记录或一项都不记录（第 3 条）；证据：V01、V02。
- D04：`stop` 的原因与 `scheduler.recovered` 的 owner（第 4、5 条）；证据：V01、V03、V04。
- D05：合法调用的行为不退化，导入无副作用（第 6 条与 Non-goals）；证据：V03、V04、V05、V08。
- D06：Step 3 的测试齐全，不自行做变异检查；证据：V02、review。
- D07：所有修改在允许集合内，无依赖变更；证据：V06、V07、scope。

## Stop conditions
- 基线、oracle 或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 4 分钟；
  - 实现约 8 分钟；
  - 测试约 10 分钟；
  - 运行检查约 4 分钟（oracle 不到 1 秒，V04 约 10 秒，全量约 105 秒）；
  - 执行报告约 4 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
