# TASK: scheduler-exact-inputs

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: 35c65dcbfcc1b58c61b4b1928b34573a9efc631a
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
在上一轮候选（attempt-001）上定点返工 `sentinelmesh/scheduler.py` 与 `tests/test_scheduler_exact_inputs.py`：
- `observe` 收到不可哈希的 dimension 时报 `E_OBSERVATION_SHAPE`，不再冒出 `TypeError`；
- 改正三处测试，并为表中每个参数补上不可哈希的用例；

并让规划端探针 `tests/test_scheduler_exact_inputs_probes.py` 全部通过。

## Non-goals
- 不改上一轮已经正确的行为：下面 Behavior contract 第 1–6 条中，除第 2 条的“v2 补充”之外，候选都已满足。
- 不处理 `rollover()` 在代际为 2^53−1 时先改账本后报错的问题（另立合同）。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、探针与其他已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `35c65dc` 由下列提交依次组成：
  - master `139fd9d`；
  - 规划端 oracle `1ef2e6f`：`tests/test_scheduler_exact_inputs_oracle.py`，56 个用例；
  - `04c981c`：规划端把 5 处对 `scheduler.recovered` 整个 payload 的断言加上 `"owner": "owner-2"`；
  - 上一轮候选的返工提交 `89d049e`：`sentinelmesh/scheduler.py` 与 `tests/test_scheduler_exact_inputs.py`；
  - 规划端探针 `tests/test_scheduler_exact_inputs_probes.py`，2 个用例。
- 探针在基线上：
  - `test_observe_rejects_an_unhashable_dimension` 失败：`observe` 先执行 `op.usage.get(dimension)`，之后才在 `_check_usage` 里检查 dimension 的类型，list、set、dict 都冒出 `TypeError`；
  - `test_settle_mapping_with_explicit_evidence_encodes_before_recording` 通过，它守护上一轮已经正确的行为：显式传入 evidence_hash 时，只有记录之前的 payload 编码能拦住 `2**53`。
- 规划端用一份草稿修复验证过（草稿未提交）：探针 2/2、oracle 56/56，全量 465 passed。
- 上一轮复审的发现（复审记录不在基线里，需要的内容都写在这里）：
  - F1：即上面 `observe` 的问题，违反第 2 条“不可哈希的值也照常报下表的 code”。独立复审用 2019 个单参数探针确认，这是表内唯一一处。
  - F2：`test_unhashable_values_raise_the_contract_code` 的文档字符串说“不可哈希的输入报表内 code”，测试体却只是把 `CASES` 里已有的列表值重跑一遍。大多数参数没有不可哈希的用例，F1 因此漏过。
  - F3：`test_stop_refuses_an_empty_reason_and_keeps_the_request_running` 只做了一次合法的 `stop("req-1")`，名不副实。
  - F4：`test_stop_with_an_empty_reason_changes_nothing` 用 `idem-1` 的幂等重放证明 request 仍可领取，但重放在检查 `stopped` 之前就返回了，这个断言不成立。同一测试里 `stopped is None` 的断言有效。
  - 上一轮在 `contract_conflicts` 里的解读（多个参数同时不合格时先报新增的格式检查）规划端接受，已写进第 2 条的“v2 补充”。
- oracle 比较账本时，连同每个值的精确类型一起比较：
  - 直接读取 `Scheduler` 的私有属性 `_generation`、`_requests`、`_tasks`、`_operations`、`_idempotency`、`_resource_holds`、`_events`；
  - 用 `vars()` 读取 `_Request`、`_Task`、`_Operation`、`_Usage` 的字段。

  不要给这些属性或类改名，也不要把 dataclass 改成 `slots`。
- `_emit()` 用 `canonical()` 编码 payload：只接受精确的 str 键，以及 str、int、bool、None 值；整数的绝对值不超过 2^53−1，超出时报 `E_JSON_INTEGER`。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - tests/test_scheduler_exact_inputs.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `tests/test_scheduler_exact_inputs_oracle.py`, `tests/test_scheduler_exact_inputs_probes.py`, `tests/test_scheduler_supervised_oracle.py`, `tests/test_scheduler_supervised.py`, `tests/test_supervised_operation_oracle.py`, `tests/test_reference.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
本节是完整的规则：第 1–6 条照录上一轮合同，只在第 2 条末尾加了“v2 补充”。

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
   - **v2 补充**：
     - `observe` 只用精确的 str 去查已记录的用量。dimension 不是 str 时（包括 list、set、dict 这类不可哈希的值），报 `E_OBSERVATION_SHAPE`，账本不变。
     - 多个参数同时不合格时报哪个 code，本合同不作规定；只有一个参数不合格时，必须报该参数在表中的 code。
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
1. 读 AGENTS.md、本合同、oracle、探针，以及基线中的 `sentinelmesh/scheduler.py` 与 `tests/test_scheduler_exact_inputs.py`。基线与 Repository facts 不符就停止并报告。
2. 修改 `sentinelmesh/scheduler.py`：只修 F1（第 2 条的 v2 补充），不重写其他部分。
3. 修改 `tests/test_scheduler_exact_inputs.py`。
   - 下列旧测试是错的，按要求改正：
     - F2：把 `test_unhashable_values_raise_the_contract_code` 改成真正的不可哈希测试。下面每个参数各传一个 list（例如 `["x"]`），只改这一处，断言该行的 code 与账本不变（值与类型），之后同一账本上的合法调用仍然成功：
       - `handoff`、`mark_running`、`renew`、`finish`、`unknown`、`release_proven_unstarted`、`cancel_unstarted`、`observe`、`settle`、`recover` 的 operation：`E_OPERATION_UNKNOWN`；`fixture_verdict` 的 operation：`E_VERDICT_CANDIDATE`；
       - `handoff`、`mark_running`、`renew`、`finish`、`unknown`、`release_proven_unstarted`、`recover` 的 owner：`E_FENCED`；
       - `claim` 的 request、task：`E_SCOPE_UNKNOWN`；key、owner：`E_CLAIM_SHAPE`；resource_keys 中的一项：`E_RESOURCE_KEYS`；
       - `seed_request` 的 request、repo：`E_REQUEST_SHAPE`；`seed_task` 的 request：`E_REQUEST_UNKNOWN`，task：`E_TASK_SHAPE`；
       - `observe` 与 `settle`（单个维度）的 dimension、call_key、evidence_hash：`E_OBSERVATION_SHAPE`；
       - `finish` 的 candidate_hash：`E_CLEANUP_OR_CANDIDATE`；`fixture_verdict` 的 candidate_hash：`E_VERDICT_CANDIDATE`，evidence_hash：`E_VERDICT_SHAPE`；
       - `stop` 的 request：`E_REQUEST_UNKNOWN`，reason：`E_STOP_REASON`。

       预算的键（caps、reservation 与 `settle` 映射的键）本身必须可哈希，不在此列。
     - F3：把 `test_stop_refuses_an_empty_reason_and_keeps_the_request_running` 改成名副其实：要么改名说明它测的是合法基线，要么并入相邻用例。
     - F4：删掉用 `idem-1` 幂等重放证明“仍可领取”的写法，改为对 task-2 用新键 `claim`，断言返回的 `replayed` 为 False。
   - 其余测试保留，不要删除或放宽。
4. 运行 V01–V09。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应通过）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_exact_inputs_probes.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V04（必要，基线应通过）：`python -m pytest -q tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V05（必要，基线应通过）：`python -m pytest -q tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V06（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V07（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V08（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V09（必要）：`python -c "import sys, sentinelmesh.scheduler; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V02 是规划端探针，在基线上失败；V06 包含 V02，在基线上同样失败。V01、V04、V05 在基线上通过，保护上一轮已经正确的行为。V03 在基线上通过，改正与补测之后仍须通过。

## Definition of done
- D01：`observe` 的不可哈希 dimension 报 `E_OBSERVATION_SHAPE`（第 2 条的 v2 补充）；证据：V02、V03。
- D02：第 1–6 条的其余规则不退化，导入无副作用；证据：V01、V04、V05、V06、V09。
- D03：Step 3 的改正与补测（F2–F4），不自行做变异检查；证据：V03、review。
- D04：所有修改在允许集合内，无依赖变更；证据：V07、V08、scope。

## Stop conditions
- 基线、oracle、探针或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总：attempt-001 已用 1 次，本版本是最后一次）
- max_wall_seconds_per_attempt: 1200
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 3 分钟；
  - 修正约 2 分钟；
  - 测试约 8 分钟；
  - 运行检查约 3 分钟（全量约 105 秒）；
  - 执行报告约 3 分钟；
  - 合计约 19 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D04，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、探针、其他已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过；改正 Step 3 列出的三处旧测试是要求，不算弱化。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
