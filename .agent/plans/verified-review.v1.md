# TASK: verified-review

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: c4c8083ca2c8de2ff0b48ba3bbb42a8801bed84a
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
让桥接与 adapter 在复审之前，用监督器在内存里记下的流事实核对磁盘上的输出：
- 桥接：复审路径上两个流必须仍是监督器记下的样子，否则不进入复审；换代恢复同样核对记录里的流；
- 桥接：把内存里的 `SupervisedRun` 交给新的 `before_finish` 钩子；交接之前核对规格属于操作所在的 request；
- adapter：在钩子里结算用量，只在读到的字节正是监督器摘要过的那些时才记，证据哈希取监督器的摘要；交接之前核对用量维度都在操作的预约里。

并让规划端 oracle `tests/test_verified_review_oracle.py` 全部通过。

## Non-goals
- 不修改 `posix_supervisor.py`、`supervised_record.py`、`scheduler.py`、`runtime.py`、`adapter_protocol.py`、`adapter.py` 或其他已有模块。
- 不改监督器的清理规则，不做进程组之外的逃逸检测；不改 `replay_decision` 的判定。
- 不处理换代恢复路径上的用量；不翻译 Pi 的原生事件格式；不调用真实 provider CLI。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `c4c8083` = master `a115764` + 规划端 oracle `tests/test_verified_review_oracle.py`（16 个用例，启动真实的本地 python 进程）。
- oracle 在基线上：15 个失败；`test_usage_is_settled_against_the_supervisors_digest` 通过，它守护已有的行为：用量的证据哈希等于监督器记下的 stdout 摘要。
- 设计说明：`.agent/design/stream-trust-boundary.md` 第 3 节第 5 条与第 6 节（决定 E1–E10）。
- 已经集成的前两份合同（`supervisor-stream-facts` 到 `d69d45d`，`scheduler-operation-view` 到 `88dff4b`）：
  - `posix_supervisor.streams_match(evidence_dir, streams) -> bool`：两个文件（`stdout.log`、`stderr.log`）都与事实一致才返回 True，从不抛出；事实为 `None`、或字节数超过 `MAX_STREAM_DIGEST_BYTES`（256 MiB）的映射返回 False；
  - `SupervisedRun.streams`：有进程启动时为 `{"stdout": 事实或 None, "stderr": 事实或 None}`，事实是 `{"device", "inode", "bytes", "sha256"}`；流读不了或超过摘要上限时，它的事实为 None；没有进程启动时为 None；
  - `supervised_record.recorded_streams(spec, evidence_dir)`：返回观察记录里的 `streams`（或 None），记录损坏时报 `E_RUNTIME_EVIDENCE`；
  - `Scheduler.operation(operation_id)`：返回 `{"operation_id", "request_id", "task_id", "state", "owner", "generation", "reservation"}` 的新副本；id 不是精确 str 或不存在时报 `ContractError("E_OPERATION_UNKNOWN")`。
- 现状：
  - `supervised_operation._settle` 的复审路径：`mark_running` → 核对记录与内存一致（`agreed`、`replay_allowed`、`cleanup_confirmed`）→ `candidate_hash(evidence_dir)` → `finish`；`candidate_hash` 或 `finish` 抛出时，`unknown(reason="START_UNKNOWN")` 后原样抛出；
  - `recover_supervised_operation`：STOPPED 或 FAILED 且 `replay_allowed` 时判 FINISHED（先求候选哈希），NOT_STARTED 时释放，其余为 START_UNKNOWN；
  - `adapter_run.run_adapter_operation` 现在包装 `candidate_hash`，在包装里归约并结算用量，证据哈希是自己读到的 stdout 的 SHA-256。
- oracle 的时序控制：
  - 一个 `Scheduler` 子类在 `mark_running` 里打开逃逸进程的闸门，并等它写完，所以流的核对必须在 `mark_running` 之后；
  - 有用例用 `mock.patch.object(supervised_operation, "streams_match", …)` 观察核对，所以桥接必须在模块级导入 `streams_match`，并通过这个名字调用（与 `record_supervised_run` 相同）。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 16/16，全量 613 passed；22 个故意制造的偏差抓住 21 个。漏掉的一个是等价写法：在候选哈希的包装里结算，用的是钩子记下的 run。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/supervised_operation.py
  - sentinelmesh/adapter_run.py
- allowed_new_files:
  - tests/test_verified_review.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/runtime.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/adapter.py`, `tests/test_verified_review_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **桥接：交接之前的检查**
   - `run_supervised_operation` 新增关键字参数 `before_finish=None`。检查按顺序进行，都在 `scheduler.handoff` 之前：
     1. 已有的 `_checked_spec`（`E_RUNTIME_INVOCATION`）；
     2. 已有的 `_checked_evidence_dir`（`E_RUNTIME_EVIDENCE`）；
     3. `before_finish` 不是 None 且不可调用：`RuntimeErrorCode("E_RUNTIME_INVOCATION", …)`；
     4. `scheduler.operation(operation_id)`，它的 `ContractError` 原样冒出；`spec.request_id` 不等于视图的 `request_id`：`RuntimeErrorCode("E_RUNTIME_INVOCATION", …)`。
   - `recover_supervised_operation` 在已有的两项检查之后做第 4 项，在读取记录之前。
   - 被拒绝的调用不改变调度器，不写证据。
2. **桥接：复审路径上的流核对**（`_settle`）
   - 进入复审路径的条件不变（有进程、记录一致、`replay_allowed`、清理确认）。在 `mark_running` 之后、做任何其他事之前：
     - `run.streams` 不是 dict，或其中任一事实为 None：`unknown(reason="START_UNKNOWN")`，返回 START_UNKNOWN；
     - 否则调用 `streams_match(evidence_dir, run.streams)`（通过本模块的名字 `streams_match`）。返回 False 时：
       - 两个路径用 `os.lstat` 查看，都是普通文件，且 `(st_dev, st_ino)` 都等于各自的事实：`unknown(reason="CLEANUP_UNCONFIRMED")`。文件还是监督器钉住的那个，却在回收之后变了，说明组外还有写入者；
       - 其他情形（缺失、不是普通文件、身份不同、`lstat` 出现 `OSError` 或 `ValueError`）：`unknown(reason="START_UNKNOWN")`；

       都返回 START_UNKNOWN，不调用钩子与 `candidate_hash`。
   - 核对通过之后：
     - `before_finish` 不是 None 时，先调用 `before_finish(run)`（内存里的 `SupervisedRun`）；
     - 然后 `candidate_hash(evidence_dir)` 与 `finish`，与现在相同。
   - 钩子、`candidate_hash` 或 `finish` 抛出时：`unknown(reason="START_UNKNOWN")`，然后原样抛出。
   - `before_finish` 只在这条路径上调用，至多一次；换代恢复不调用它。
3. **恢复路径上的流核对**（`recover_supervised_operation`）
   - 只有已有条件（STOPPED 或 FAILED 且 `replay_allowed`）成立，并且 `streams_match(evidence_dir, recorded_streams(spec, evidence_dir))` 为 True 时，才判 FINISHED 并求候选哈希；
   - `recorded_streams` 抛出 `RuntimeErrorCode` 时算不一致；
   - 不一致时判 START_UNKNOWN，不求候选哈希。其余分支不变。
4. **adapter：`read_stream(evidence_dir, *, max_bytes=…, expected=None)`**
   - `expected` 不是 None 时，必须是精确的 dict，键恰好是 `device`、`inode`、`bytes`、`sha256`：
     - `device`、`inode`、`bytes` 是精确 int，`inode` 与 `bytes` 不小于 0；
     - `sha256` 是 64 个小写十六进制字符的精确 str。

     否则报 `AdapterError("E_ADAPTER_SCHEMA", …)`。这项检查在 `max_bytes` 检查之后、打开文件之前。
   - 给了 `expected` 时，在“不是普通文件”的判断之后：
     - `(st_dev, st_ino, st_size)` 与 `expected` 的 `device`、`inode`、`bytes` 不同：返回 `{"status": "changed", "sha256": None, "bytes": None, "events": None}`；
     - 然后才是已有的上限检查（`too_large`）与有界读取；
     - 读到的字节数不等于 `bytes`，或它们的 SHA-256 不等于 `sha256`：同样返回 `changed`。
   - `expected` 为 None 时行为不变。`missing`、`unreadable` 的判断不变。
5. **adapter：`run_adapter_operation`**
   - 交接之前的检查在已有的第 1–6 项之后加第 7 项：`reserved = scheduler.operation(invocation.run_id)["reservation"]`（`ContractError` 原样冒出）；`usage_dimensions` 中每一项都必须是 `reserved` 的键，否则报 `AdapterError("E_ADAPTER_SCHEMA", …)`。
   - 不再包装 `candidate_hash`：把调用方的 `candidate_hash` 原样交给桥接，并传 `before_finish=<钩子>`。钩子收到 `run`，按顺序：
     1. `read_stream(evidence_dir, max_bytes=max_stream_bytes, expected=run.streams["stdout"])`，状态为 `ok` 时交给 `adapter.run` 归约（`AdapterError` 的处理不变）；
     2. 记用量的规则不变（结果已结算且完整；值为精确非负 int 的已预约维度），`evidence_hash` 取 `run.streams["stdout"]["sha256"]`。
   - 钩子没有被调用时（其他任何路径），照旧在桥接返回之后归约一次，只用于报告，不带 `expected`，不记用量。
   - 返回值的键不变；`stream.status` 可以是 `changed`。
6. **导入无副作用**：导入 `sentinelmesh.supervised_operation` 与 `sentinelmesh.adapter_run` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3、6 节、oracle、`supervised_operation.py` 与 `adapter_run.py` 的全文，以及第 Repository facts 列出的四个接口。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改 `sentinelmesh/supervised_operation.py`，更新模块与函数的文档字符串。
3. 按第 4–5 条修改 `sentinelmesh/adapter_run.py`，更新文档字符串。
4. 新建 `tests/test_verified_review.py`，覆盖下列 oracle 没有覆盖的用例。
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处；
     - 每个用例都清理自己启动的进程；
     - 冒出的 `KeyboardInterrupt` 要在测试里拦住，不要让它中断 pytest 会话。
   - 用例：
     - `before_finish` 恰好调用一次；
     - `before_finish` 通过、`finish` 抛出时（例如用一个让 `finish` 抛错的 `Scheduler` 子类），操作为 START_UNKNOWN，异常冒出；
     - 只有 stderr 被换成另一个文件（身份不同）时，原因为 START_UNKNOWN；只有 stderr 在原文件上追加时，原因为 CLEANUP_UNCONFIRMED；
     - 换代恢复时观察记录损坏到 `recorded_streams` 抛错：判 START_UNKNOWN，不抛出；
     - `read_stream` 给了 `expected` 但文件不存在：`missing`；
     - `usage_dimensions=()` 时第 7 项检查通过，什么都不记。
5. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_verified_review_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_verified_review.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_supervisor_stream_facts_oracle.py tests/test_scheduler_operation_view_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.supervised_operation, sentinelmesh.adapter_run; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01 与 V04 在基线上失败，是因为核对与钩子还不存在；V02 是因为测试文件还不存在。V03 保护桥接、adapter 与前两份合同的行为。

## Definition of done
- D01：交接之前的检查（Behavior contract 第 1 条）；证据：V01、V02。
- D02：复审路径上的流核对与 `before_finish`（第 2 条）；证据：V01、V02。
- D03：恢复路径上的流核对（第 3 条）；证据：V01、V02。
- D04：`read_stream` 的 `expected`（第 4 条）；证据：V01、V02。
- D05：adapter 的维度对账与钩子里的结算（第 5 条）；证据：V01、V02、V03。
- D06：导入无副作用，已有模块不退化（第 6 条）；证据：V03、V04、V07。
- D07：Step 4 的测试齐全，不自行做变异检查；证据：V02、review。
- D08：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
  - 实现约 8 分钟；
  - 测试约 8 分钟；
  - 运行检查约 5 分钟（oracle 约 5 秒，全量约 125 秒）；
  - 执行报告约 4 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D08，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
