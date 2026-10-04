# TASK: posix-supervisor

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: c5ac19477131971f4e256f22281e9d2e596c8032
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
在上一轮候选（attempt-001）上定点返工 `sentinelmesh/posix_supervisor.py` 与 `tests/test_posix_supervisor.py`：
- 观察记录按 v1 第 8 条保留组长的结局；
- 发信号时的 `PermissionError` 不中断流程；
- 停止与回收阶段的三处等待吸收 `KeyboardInterrupt`；
- 改正与补齐测试，让规划端探针 `tests/test_posix_supervisor_probes.py` 全部通过。

## Non-goals
- 不改 v1 已经正确的行为：校验顺序、证据文件、启动、身份、第一次等待（completed / timeout / cancelled）、残留回收的探测规则、不泄露值、导入无副作用。
- 不修改 `sentinelmesh/runtime.py`、`sentinelmesh/process_identity.py` 或其他已有模块；不接入 adapter、scheduler 或旧的 `SubprocessRunner`；Linux 与 Windows 不做原生验证。
- 进程启动后、第一次等待开始之前那段极短窗口里的中断，不在本合同范围内。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与探针测试；不联网、不读取凭据、不启动真实 provider CLI。

## Repository facts
- 基线 `c5ac194` 由下列提交依次组成：
  - master `f458fb4`；
  - 规划端 oracle `063547b`（即 v1 基线的 `897809b`）；
  - 上一轮候选的返工提交 `7ce6ff9`（即 `665b862`）；
  - 规划端探针 `tests/test_posix_supervisor_probes.py`，7 个用例。
- 探针在基线上：5 个失败（对应下面 Behavior contract 第 1–3 条）；另外 2 个守护已有的正确行为（第 4 条的信号顺序、存在的相对 cwd 被拒绝）。
- 规划端用一份草稿修复验证过（草稿未提交）：
  - 7 个探针、11 个 oracle 用例全部通过；
  - 全量只剩执行器测试 `test_unconfirmed_cleanup_is_start_unknown` 失败，它断言的正是第 1 条要改掉的偏离。
- 上一轮复审：`.agent/reviews/posix-supervisor/attempt-001.md`。
- v1 合同（`.agent/plans/posix-supervisor.v1.md`）的 Behavior contract 第 1–10 条继续有效。本合同的 Behavior contract 只写修订与新增，两者冲突时以本合同为准。
- 规划端在 macOS 上实测（2026-10-04、10-05）：
  - 进程组只剩未回收的僵尸进程时，`os.killpg` 对信号 0 与 SIGKILL 都抛 `PermissionError`（EPERM），不是 `ProcessLookupError`。已集成的旧 `SubprocessRunner` 有同一缺陷，已由规划端在 `74b4d62` 修正；
  - 对未回收的僵尸进程，`os.kill(pid, 0)` 成功；回收后抛 `ProcessLookupError`；
  - 本机 Python 没有 `os.waitid`。
- CPython 的 `subprocess.Popen.wait` 第一次收到 `KeyboardInterrupt` 时，会再等子进程最多 0.25 秒，然后重新抛出；之后再收到的中断立即重新抛出。
- 探针用真实 SIGINT 测试第 3 条：信号只发给测试进程自己；发送前先握手，确认监督器已进入对应的窗口；`supervise()` 一返回，处理器就解除。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9（Darwin 24.6.0）x86_64。

## Scope
- allowed_modified_files:
  - sentinelmesh/posix_supervisor.py
  - tests/test_posix_supervisor.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `sentinelmesh/process_identity.py`, `tests/test_posix_supervisor_oracle.py`, `tests/test_posix_supervisor_probes.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **观察记录保留组长的结局**（重申 v1 第 8 条，上一轮候选与此相反）
   - `state` 只由组长决定：`stop_reason == "completed"` 且 `exit_code == 0` 时为 `"STOPPED"`，其余启动成功的情形为 `"FAILED"`。
   - 观察记录的 `"state"` 永远等于这个 `state`；cleanup 未确认时也不改写。
   - 生命周期在 cleanup 确认时转到 `state`，未确认时转到 `START_UNKNOWN`（generation 4）。
   - 因此 cleanup 未确认时，`reconcile` 返回的 `status` 是 `STOPPED` 或 `FAILED`，`replay_allowed` 为 False。
2. **发信号时的 PermissionError**（修订 v1 第 6、7 条）
   - 停止阶段的 SIGTERM、SIGKILL，以及残留回收的 SIGKILL，都通过 `os.killpg(pgid, 信号)` 发送，`pgid` 是组长的 pid。
   - 发送时的 `ProcessLookupError` 与 `PermissionError` 都忽略，不冒出 `supervise()`。
   - 发送是否报错不影响后续步骤：
     - 停止阶段照常等待组长；只要发出了 SIGKILL，`"timeout"` 就改为 `"timeout_killed"`；
     - 残留回收照常把 `stragglers_killed` 设为 True，并照常轮询。
   - 组是否已清空，只由 v1 第 7 条的信号 0 探测决定。
3. **停止与回收阶段吸收中断**（新增）
   - 有三处等待：
     - 宽限等待（`_TERM_GRACE_SECONDS`）；
     - 强杀后的等待（`_KILL_WAIT_SECONDS`）；
     - 回收轮询（`_REAP_SECONDS`），包括每一轮的探测与间隔。
   - 其中任一处被 `KeyboardInterrupt` 打断时，吸收该中断，继续这一处等待，直到原来的结束条件成立（组长退出，或组被确认清空）或到达原来的截止时间。
   - 截止时间、结束条件与后续步骤都不变。例如宽限等待中途被打断，组长到截止时间仍未退出，就照常对进程组发 SIGKILL。
   - 这类中断不改变 `stop_reason`（它仍只由 v1 第 6 条的第一次等待决定）；结果与观察记录照常返回。
4. **停止阶段的信号顺序**（明确 v1 第 6 条）
   - 停止阶段只调用 `os.killpg` 发 SIGTERM；组长在宽限内未退出时，再发 SIGKILL。
   - 信号 0 探测只出现在残留回收阶段。
   - 例：组长忽略 SIGTERM、没有其他组员的超时。`os.killpg` 依次恰好被调用三次：`(pgid, SIGTERM)`、`(pgid, SIGKILL)`、`(pgid, 0)`，且 `stragglers_killed` 为 False。

## Steps
1. 读 AGENTS.md、本合同、v1 合同、上一轮复审、oracle 与探针测试，以及基线中的两个文件。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–4 条修改 `sentinelmesh/posix_supervisor.py`。保留 v1 已经正确的部分，不重写整个模块。
3. 修改 `tests/test_posix_supervisor.py`：
   - 改正 `test_unconfirmed_cleanup_is_start_unknown`（可以改名）：
     - 观察记录的 `state` 为 `"STOPPED"`；
     - `reconcile` 返回 `("STOPPED", False)`；
     - 生命周期仍为 `START_UNKNOWN`（generation 4）。
   - 补下列用例。白盒注入方式自定，每个用例都清理自己启动的进程组：
     - cleanup 未确认的另外两种结局：取消、超时强杀。两者的观察记录 `state` 都是 `"FAILED"`，生命周期都是 `START_UNKNOWN`；
     - 强杀后的等待被 `KeyboardInterrupt` 打断：中断被吸收，结果照常返回，`exit_code` 为 `-SIGKILL`；
     - 回收轮询的间隔不超过 0.05 秒，例如记录回收阶段每次休眠的时长；
     - 残留回收的 SIGKILL 抛 `PermissionError`：`stragglers_killed` 为 True，结果照常返回。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。

- V01（必要）：`python -m pytest -q tests/test_posix_supervisor_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_posix_supervisor_probes.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_posix_supervisor.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.posix_supervisor; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V02 是规划端探针，在基线上失败；V04 因包含 V02 的探针，在基线上同样失败。V03 在基线上通过，是因为上一轮的执行器测试断言了第 1 条要改掉的偏离；改正后它保护新行为。其余检查保护 v1 已经正确的行为。

## Definition of done
- D01：观察记录保留组长的结局（Behavior contract 第 1 条）；证据：V02、V03。
- D02：发信号时的 `PermissionError` 不中断流程，停止阶段的信号顺序不变（第 2、4 条）；证据：V01、V02、V03。
- D03：停止与回收阶段吸收中断（第 3 条）；证据：V02、V03。
- D04：完成 Step 3 的测试改正与补齐，不自行做变异检查；证据：V03、review。
- D05：v1 行为与已有测试不退化，所有修改在允许集合内，无依赖变更；证据：V01、V04、V05、V06、V07、scope。

## Stop conditions
- 基线、oracle、探针或 Repository facts 中的实测事实与本机不符。
- 必须修改允许范围之外的文件（包括 oracle、探针、`process_identity.py`、`runtime.py`）才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 本合同与 v1 合同对同一行为说法不同（本合同已写明优先的除外），或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总：attempt-001 已用 1 次，本版本是最后一次）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 4 分钟；
  - 实现修订约 5 分钟；
  - 测试约 10 分钟；
  - 运行检查约 3 分钟（探针约 15 秒，oracle 约 6 秒，全量约 60 秒）；
  - 执行报告约 4 分钟；
  - 合计约 26 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、oracle 与探针测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过；改正 `test_unconfirmed_cleanup_is_start_unknown` 是 Step 3 的要求，不算弱化。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
