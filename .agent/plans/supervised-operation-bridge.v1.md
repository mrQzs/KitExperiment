# TASK: supervised-operation-bridge

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 806d9ffbcff88794f2f88fd11a22045d586a046d
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
新增桥接模块 `sentinelmesh/supervised_operation.py`，把监督运行接入 `Scheduler`：
- `run_supervised_operation()`：交接 → 带记录的监督运行 → 按落盘证据迁移调度状态；
- `recover_supervised_operation()`：controller 换代后，按证据恢复旧代际的在途操作；
- `evidence_digest()`：两份记录的摘要，作为调度事件的证据；

并让规划端 oracle `tests/test_supervised_operation_oracle.py` 全部通过。

## Non-goals
- 不修改 `scheduler.py`、`posix_supervisor.py`、`supervised_record.py`、`runtime.py` 或其他已有模块。
- 不接入 adapter，不实现 `resume`，不做持久化；Linux 与 Windows 保持 NOT_RUN。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml` 与 oracle；不联网、不读取凭据、不启动真实 provider CLI。

## Repository facts
- 基线 `806d9ff` = master `68b6e05` + 规划端 oracle（13 个真实进程用例）。规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 全部通过，全量 372 passed；
  - 9 个草稿变异都被抓住。
- 设计与用户决定：`.agent/design/scheduler-supervisor-wiring.md` 第 5、6 节。
- 可用的已有接口：
  - `Scheduler`（`sentinelmesh/scheduler.py`）：`handoff`、`mark_running(..., process_identity=...)`、`finish(operation, owner, generation, candidate_hash, cleanup_verified)`、`unknown(..., reason=...)`、`release_proven_unstarted(..., evidence_hash=...)`、`recover(..., outcome=..., evidence_hash=..., candidate_hash=...)`；
  - `record_supervised_run`、`replay_decision`、`HANDOFF_NAME`、`OBSERVATION_NAME`（`sentinelmesh/supervised_record.py`）：
    - 交接记录在启动之前写；
    - 没有交接记录就没有启动；
    - 判定结果是五键记录；
  - `InvocationSpec`、`RuntimeErrorCode`（`sentinelmesh/runtime.py`）；`SupervisedRun.identity.to_dict()` 是四键的进程身份。
- `Scheduler` 当前代际的 START_UNKNOWN 只能在换代之后由 `recover()` 处理。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files: 无
- allowed_new_files:
  - sentinelmesh/supervised_operation.py
  - tests/test_supervised_operation.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/scheduler.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/runtime.py`, `tests/test_supervised_operation_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **`evidence_digest(evidence_dir) -> str`**
   - 对 `HANDOFF_NAME` 与 `OBSERVATION_NAME` 各取一个值：
     - 文件不存在：`None`；
     - 用 `os.open(路径, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)` 打开失败（包括符号链接）、不是普通文件、或超过 65536 字节：字符串 `"unreadable"`；
     - 否则：文件全部字节的 sha256（小写十六进制）。
   - 返回 `hashlib.sha256(json.dumps({"handoff": 值, "observation": 值}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()`。
   - 对上述各种文件状态都不抛出。
2. **`run_supervised_operation(scheduler, *, operation_id, owner, generation, spec, environment, evidence_dir, worktree_id, candidate_hash, allowed_secret_names=frozenset(), now=0) -> dict`**
   - `candidate_hash` 是可调用对象，以 `evidence_dir` 为参数，返回候选的哈希。
   - 按顺序：
     1. `type(spec) is not InvocationSpec`，或 `spec.run_id != operation_id`：报 `RuntimeErrorCode("E_RUNTIME_INVOCATION")`。这时不交接、不建文件。
     2. `scheduler.handoff(operation_id, owner, generation, now)`；它的错误原样冒出（不启动、不留记录）。
     3. 调用 `record_supervised_run(spec, environment=..., evidence_dir=..., worktree_id=..., allowed_secret_names=...)`：
        - 抛出 `RuntimeErrorCode` 时，记下它的 code，`run` 为 None，继续第 4 步；
        - 抛出其他任何异常（包括 `KeyboardInterrupt`）时，按第 4、5 步（`run` 为 None）落定调度状态，然后原样重新抛出。
     4. `decision = replay_decision(spec, evidence_dir)`；它抛出 `RuntimeErrorCode` 时，`decision` 为 None。
     5. 落定调度状态，恰好走下列一条：
        - `decision` 的 status 为 NOT_STARTED：`release_proven_unstarted(..., evidence_hash=evidence_digest(evidence_dir))`，状态为 NOT_STARTED；
        - `run` 不为 None 且有 identity，`decision` 的 status 为 STOPPED 或 FAILED：先 `mark_running(..., process_identity=run.identity.to_dict())`，然后：
          - `replay_allowed` 为 True：`finish(..., candidate_hash(evidence_dir), True)`，状态为 REVIEW_PENDING；
          - 否则：`unknown(..., reason="CLEANUP_UNCONFIRMED")`，状态为 START_UNKNOWN；
        - 其余情形：`unknown(...)`（原因 START_UNKNOWN），状态为 START_UNKNOWN。
        - `candidate_hash(...)` 或 `finish(...)` 抛出异常时：调用 `unknown(..., reason="START_UNKNOWN")`，再原样重新抛出原来的异常。操作不会停在 RUNNING。
   - 返回 `{"operation_id", "state", "decision", "error", "stop_reason", "exit_code"}`：
     - `decision` 是判定记录的 dict 副本或 None；
     - `error` 是第 3 步记下的 code 或 None；
     - `stop_reason` 与 `exit_code` 取自 `run`，`run` 为 None 时都是 None。
3. **`recover_supervised_operation(scheduler, *, operation_id, owner, generation, spec, evidence_dir, candidate_hash) -> dict`**
   - 与第 2 条第 1 步相同的检查，不符时报 `E_RUNTIME_INVOCATION`，不改变调度器。
   - `decision` 按第 2 条第 4 步取得，`digest = evidence_digest(evidence_dir)`：
     - status 为 NOT_STARTED：`recover(..., outcome="NOT_STARTED", evidence_hash=digest)`，状态为 NOT_STARTED；
     - status 为 STOPPED 或 FAILED，且 `replay_allowed` 为 True：`recover(..., outcome="FINISHED", evidence_hash=digest, candidate_hash=candidate_hash(evidence_dir))`，状态为 REVIEW_PENDING；
     - 其余情形（包括 `decision` 为 None）：`recover(..., outcome="START_UNKNOWN", evidence_hash=digest)`，状态为 START_UNKNOWN。
   - `candidate_hash` 在调用 `recover()` 之前求值；它抛出时原样冒出，调度器不变。`recover()` 的错误原样冒出。
   - 返回 `{"operation_id", "state", "decision"}`。
4. **导入无副作用**：导入 `sentinelmesh.supervised_operation` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 5、6 节、oracle，以及会用到的已有接口。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–4 条新建 `sentinelmesh/supervised_operation.py`。
3. 新建 `tests/test_supervised_operation.py`，覆盖下列 oracle 没有覆盖的用例。
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处；
     - 每个用例都清理自己启动的进程组；
     - 冒出的 `KeyboardInterrupt` 要在测试里拦住，不要让它中断 pytest 会话。
   - `candidate_hash` 抛出异常、或返回非哈希：操作进入 START_UNKNOWN，`run.started` 已记录，原来的异常冒出；
   - `record_supervised_run` 抛出 `KeyboardInterrupt`（用补丁替换桥接模块里的引用）：
     - 什么都没写时，进入 NOT_STARTED；
     - 交接记录已写时，进入 START_UNKNOWN；
     - 两种情形都原样冒出 `KeyboardInterrupt`；
   - `evidence_digest`：记录位置是目录、符号链接、FIFO，或文件恰好 65537 字节时，都按 `"unreadable"` 计算且不抛出；恰好 65536 字节时，按文件的 sha256 计算。
   - `recover_supervised_operation`：
     - `spec.run_id` 不符时报 `E_RUNTIME_INVOCATION`，调度器不变；
     - `candidate_hash` 抛出时调度器不变，之后仍可恢复；
     - 对没有换代的操作，`E_RECOVERY_NOT_FENCED` 原样冒出。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_supervised_operation_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_supervised_operation.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised.py tests/test_supervised_record_oracle.py tests/test_supervised_record.py tests/test_posix_supervisor_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.supervised_operation; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为模块还不存在；V02 在基线上失败，是因为测试文件还不存在。V03 保护已集成的调度器、记录模块与监督器。

## Definition of done
- D01：`evidence_digest`（Behavior contract 第 1 条）；证据：V01、V02。
- D02：`run_supervised_operation` 的检查、交接、落定与异常处理（第 2 条）；证据：V01、V02。
- D03：`recover_supervised_operation`（第 3 条）；证据：V01、V02。
- D04：导入无副作用，已有模块不退化（第 4 条）；证据：V03、V04、V07。
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
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 4 分钟；
  - 实现约 8 分钟；
  - 测试约 10 分钟；
  - 运行检查约 4 分钟（oracle 约 7 秒，全量约 100 秒）；
  - 执行报告约 4 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块与测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
