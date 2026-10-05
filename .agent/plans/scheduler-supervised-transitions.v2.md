# TASK: scheduler-supervised-transitions

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: 63b4ed928a76f03d1ccdbbceb5c4d6e6fc072a47
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
在上一轮候选（attempt-001）上定点返工 `sentinelmesh/scheduler.py` 与 `tests/test_scheduler_supervised.py`：
- 新方法的类型检查改为精确类型，使被拒绝的调用真正不改变账本；
- `recover()` 只接受精确的 int 代际；
- 补齐测试；

并让规划端探针 `tests/test_scheduler_supervised_probes.py` 全部通过。

## Non-goals
- 不改 v1 已经正确的行为：NOT_STARTED 与快照口径、`unknown` 的原因、`mark_running` 的身份、`recover` 的九步顺序与三种结果。
- 不改 `finish()`、`fixture_verdict()` 等已有方法（它们也用 `isinstance`，属于已有行为）。不接入监督器，不启动进程。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与探针；不联网、不读取凭据。

## Repository facts
- 基线 `63b4ed9` 由下列提交依次组成：
  - master `82d29a0`（含 v1 合同与上一轮复审）；
  - 规划端 oracle `76b7043`（即 v1 基线的 `befbba9`）；
  - 上一轮候选的返工提交 `e9a7333`（即 `d72f923`）；
  - 规划端探针 `tests/test_scheduler_supervised_probes.py`，8 个用例。
- 探针在基线上：2 个失败（下面第 1、2 条），另外 6 个守护上一轮已经实现、但没有测试把守的规则。规划端用一份草稿修复验证过（草稿未提交）：8 个探针、10 个 oracle 用例与已有测试全部通过，全量 352 passed。
- 上一轮复审：`.agent/reviews/scheduler-supervised-transitions/attempt-001.md`。规划端在固定候选上复现过：
  - str 子类（包括 `StrEnum` 的成员）能通过 `isinstance(x, str)`；`release_proven_unstarted`、`unknown`、`recover`，以及身份键为 str 子类的 `mark_running`，都会先改账本，再在 `_emit()` 里报 `E_JSON_TYPE`/`E_JSON_KEY`；
  - `recover(..., generation=2.0)` 通过检查，并把 `2.0` 写进操作的 generation。
- `_emit()` 会对 payload 做规范 JSON 编码，只接受精确的 str 键与 str/int/bool 值。
- v1 合同（`.agent/plans/scheduler-supervised-transitions.v1.md`）的 Behavior contract 第 1–6 条继续有效；本合同只写修订与新增，两者冲突时以本合同为准。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - tests/test_scheduler_supervised.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `tests/test_scheduler_supervised_oracle.py`, `tests/test_scheduler_supervised_probes.py`, `tests/test_reference.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **精确类型**（修订 v1 第 1、3、4、5 条）
   - “是 str”一律指 `type(x) is str`：`release_proven_unstarted` 与 `recover` 的 `evidence_hash`、`recover` 的 `outcome` 与 `candidate_hash`、`unknown` 的 `reason`。str 子类（包括 `StrEnum` 的成员）按“不是 str”处理，报原来的 code。
   - `mark_running` 的 `process_identity`（不为 None 时）：
     - `type(process_identity) is dict`，否则报 `E_IDENTITY_SHAPE`；
     - 先做副本，之后只检查副本：每个键都是精确的 str、键集合恰好是四个字段，并且 `ProcessIdentity(**副本)` 能构造成功，否则报 `E_IDENTITY_SHAPE`；
     - 事件 payload 用这份副本。
   - 身份检查先于代际、owner 与状态检查（上一轮执行器的解读，规划端接受）。
2. **`recover` 的代际**（修订 v1 第 5 条第 4 步）：`type(generation) is not int`，或不等于当前代际，都报 `E_FENCED`。其余八步的顺序与 code 不变。
3. **总规则不变，并对上面这些输入成立**：被拒绝的调用不改变任何操作的 state、owner、generation、candidate_hash，不改变资源占用与 request 的停止状态，也不追加事件。

## Steps
1. 读 AGENTS.md、本合同、v1 合同、上一轮复审、oracle 与探针，以及基线中的两个文件。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改 `sentinelmesh/scheduler.py`，只改这些检查，不重写其他部分。
3. 修改 `tests/test_scheduler_supervised.py`：
   - 下列旧测试是错的，按合同改正：
     - Step 3 第 5 项那条测试先 `settle` 再释放。改为先 `release_proven_unstarted`，再补记 tokens 用量（final，数量 5），断言返回 APPLIED、known 为 5、held 为 0、request 未停止；
     - 注释写“已处于 NOT_STARTED”的那条断言，实际传入的是 RUNNING 的操作。改为对一个已经释放的操作再释放一次，断言 `E_NOT_STARTED_STATE`，且账本不变。
   - 补下列用例，每个都断言具体的 code，并断言账本不变：
     - 每个新方法传入 `class S(str)` 的实例，以及一个 `StrEnum` 成员；
     - 身份是 dict 子类，或某个键是 str 子类；
     - `recover` 的 `generation` 为 `2.0` 与 `True`。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要）：`python -m pytest -q tests/test_scheduler_supervised_oracle.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_scheduler_supervised_probes.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_supervised.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V04（必要）：`python -m pytest -q tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要）：`python -c "import sys, sentinelmesh.scheduler; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V02 是规划端探针，在基线上失败；V05 因包含 V02，在基线上同样失败。V03 在基线上通过，改正与补测后保护新行为。

## Definition of done
- D01：精确类型与身份副本（Behavior contract 第 1 条）；证据：V02、V03。
- D02：`recover` 的 int 代际（第 2 条）；证据：V02、V03。
- D03：总规则对这些输入成立，v1 行为不退化（第 3 条）；证据：V01、V02、V04、V05、V08。
- D04：Step 3 的改正与补测，不自行做变异检查；证据：V03、review。
- D05：所有修改在允许集合内，无依赖变更；证据：V06、V07、scope。

## Stop conditions
- 基线、探针或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据。
- 本合同与 v1 合同对同一行为说法不同（本合同已写明优先的除外），或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总：attempt-001 已用 1 次，本版本是最后一次）
- max_wall_seconds_per_attempt: 1200
- max_wall_seconds_total: 3000
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 3 分钟；
  - 实现修订约 3 分钟；
  - 测试约 7 分钟；
  - 运行检查约 3 分钟；
  - 执行报告约 3 分钟；
  - 合计约 19 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、探针、已有测试 `tests/test_reference.py`、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过；改正 Step 3 列出的两处旧测试是要求，不算弱化。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
