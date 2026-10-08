# TASK: restart-recovery

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 4c7307073575a7cfe05a8cdc660bbc873a9cb3e2
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
控制器崩溃重启之后，能从持久账本把旧代际留下、还没有判定的操作恢复出来：
- 交接时由调度器记下运行描述（调用规格的各个字段、证据目录、工作树），可以读回；
- 桥接每次交接都把它运行的那份描述交给调度器；
- 新增 `sentinelmesh/restart_recovery.py`，提供恢复驱动 `recover_after_restart`：
  - 先换代；
  - 取消从未交接的领取；
  - 对交接过但旧控制器还没有判定的操作，按磁盘上的记录用 `recover_supervised_operation` 定案；
  - 逐个报告结果。

并让规划端 oracle `tests/test_restart_recovery_oracle.py` 全部通过。

## Non-goals
- 不改 `Scheduler` 的其他语义：`handoff` 的事件载荷、其余方法、私有属性都不变（改动只限 Behavior contract 第 1 条）。
- 不改 `recover_supervised_operation`、`posix_supervisor.py`、`supervised_record.py`、`adapter_run.py`、`persistence.py`。持久账本只加两个只读访问器（第 2 条），不改它的日志格式、编码、重放与失败关闭。
- 恢复驱动不碰旧控制器已经判定过的操作（尤其是 `START_UNKNOWN`），不重新开放请求（请求保持 `RECOVERY_REQUIRED`），不重跑任何运行，不清理工作树。
- 不做多宿主或分布式恢复；不承诺断电之后的持久性。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `4c73070` = master `b7c2899` + 规划端 oracle（12 个用例，其中 3 个启动真实的控制器子进程并用 SIGKILL 杀掉）。oracle 在基线上收集失败，因为 `restart_recovery` 模块还不存在。
- 设计说明：`.agent/design/durable-ledger.md` 第 5 节第 3 项（用户决定：账本记运行描述，自动恢复）与第 7 节。
- `Scheduler.handoff(self, operation, owner, generation, now=0)`：
  - 检查依次为：`exact_int(now)`、`_owned`（代际、操作、owner）、状态与租约、请求是否已停、guard、预算；
  - 然后改状态、记 `dispatches`、发出 `scheduler.handoff_accepted`，载荷 `{"generation": generation}`。
- 持久账本：
  - 记录一个调用时，按参数名绑定并补上默认值，记下全部参数。所以给 `handoff` 加一个仅限关键字的参数，日志里就多一个 `"run"`（`null` 或描述），重放时原样传回；
  - 编码用 `ensure_ascii=False` 再转 UTF-8。参数里有孤立代理字符时，内存调用已经成功，追加却失败，实例随之失效。所以描述的文本字段必须在交接之前就被拒绝（第 1 条）。
- `InvocationSpec(...)` 的构造只做纯校验，不访问文件系统，重放时构造是安全的。`runtime._runtime_text(value, code, field)` 是运行时的文本规则：非空、无 NUL、无控制字符（< 0x20、0x7F–0x9F）、无代理字符；`posix_supervisor.py` 已经跨模块导入它。
- 桥接：
  - `run_supervised_operation` 先交接再运行；
  - 运行结束、两份记录都写好之后，才补登记 `mark_running`；
  - 在复审路径上，先核对输出流，再调用 `before_finish`、`candidate_hash` 与 `finish`。
  - 所以控制器死在 `mark_running` 处，操作留在 `HANDOFF_ACCEPTED`，记录是完整的；死在 `before_finish` 处，操作留在 `RUNNING`。
- `recover_supervised_operation(scheduler, *, operation_id, owner, generation, spec, evidence_dir, candidate_hash)`：
  - 只凭两份记录定案：`NOT_STARTED`；结束且清理已确认、输出流仍是记录的那两个文件时为 `FINISHED`（进入 `REVIEW_PENDING`）；其他一律 `START_UNKNOWN`；
  - 证据目录不存在，或记录被删掉，也不抛出，得到 `START_UNKNOWN`；证据目录存在却没有任何记录（记录器在启动之前就拒绝，例如可执行文件不存在），说明什么都没有交接出去，得到 `NOT_STARTED`；
  - `candidate_hash` 抛出时账本不变，异常原样冒出。
- `Scheduler.recover` 接受 `HANDOFF_ACCEPTED`、`RUNNING`、`START_UNKNOWN` 三种源状态，要求请求已停、操作的代际小于当前代际。`rollover()` 让每个请求以 `RECOVERY_REQUIRED` 停止。`cancel_unstarted(operation, generation)` 只接受 `RESERVED`。
- 旧控制器已经判成 `START_UNKNOWN` 的操作，当时它手里有监督器在内存中的观察，而证据目录里的记录子进程也能写。只凭记录重判会推翻一个信息更多的判断，所以恢复驱动不碰它。这是规划端的决定，见 Behavior contract 第 4 条。
- `dispatches` 是交接时由调度器记下的本地指标；没有结算 `tokens` 就完成一次运行，请求会以 `USAGE_UNKNOWN` 停止。oracle 的控制器在 `before_finish` 里结算 `tokens`。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 12/12，全量 682 passed；
  - 38 个故意制造的偏差全部抓住。其中包括：描述存进事件、校验晚于状态变化、读出共享对象、按 id 排序、恢复 `START_UNKNOWN`、不恢复 `RUNNING`、吞掉账本错误、吞掉中断、换代两次，以及重建的规格走样。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - sentinelmesh/durable_ledger.py
  - sentinelmesh/supervised_operation.py
- allowed_new_files:
  - sentinelmesh/restart_recovery.py
  - tests/test_restart_recovery.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `sentinelmesh/runtime.py`, `tests/test_restart_recovery_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **调度器的运行描述**（`scheduler.py`）
   - 签名改为 `handoff(self, operation, owner, generation, now=0, *, run=None)`。
   - 已有的检查保持原来的顺序，并且都在描述之前。`run` 不是 `None` 时，再校验描述；不合格报 `ContractError("E_RUN_DESCRIPTOR")`，什么都不改：不发事件，操作仍是 `RESERVED`，不存描述。
   - 合格的描述：
     - 精确 `dict`，键恰好是 `spec`、`evidence_dir`、`worktree_id`，每个键都是精确 `str`。
     - `spec` 是精确 `dict`，键恰好是 `request_id`、`run_id`、`command`、`cwd`、`environment_sha256`、`timeout_seconds`，每个键都是精确 `str`。
     - `command` 是元素都为精确 `str` 的精确 `list`；`request_id`、`run_id`、`cwd`、`environment_sha256` 是精确 `str`；`timeout_seconds` 是精确 `int`。
     - `run_id` 等于这次交接的操作 id，`request_id` 等于该操作所属的请求。
     - `InvocationSpec(request_id, run_id, tuple(command), cwd, environment_sha256, timeout_seconds)` 能够构造；它的 `RuntimeErrorCode` 映射为 `E_RUN_DESCRIPTOR`。
     - `evidence_dir` 与 `worktree_id` 符合运行时的文本规则 `runtime._runtime_text`。
   - 每一层都先复制再检查，只检查并保存这份副本。调用方之后再改自己的对象，账本里的描述不变。
   - 描述存在操作上。`run=None` 的交接不存描述。`scheduler.handoff_accepted` 的载荷仍然只是 `{"generation": generation}`。
   - 新增只读方法，都不发事件、不改账本：
     - `operation_ids(self) -> tuple[str, ...]`：全部操作 id，按领取的顺序。
     - `run_descriptor(self, operation) -> dict | None`：返回该操作记下的描述的一份新的深拷贝；没有描述时返回 `None`。`operation` 不是精确 `str`，或者是未知的操作：`ContractError("E_OPERATION_UNKNOWN")`。
2. **持久账本的读访问器**（`durable_ledger.py`）
   - `DurableScheduler` 新增 `operation_ids()` 与 `run_descriptor(operation)`，行为与已有的 `operation()` 相同：实例不可用时报 `LedgerError("E_LEDGER_UNAVAILABLE")`，否则读内存，不碰日志。
   - 描述随 `handoff` 的参数记入日志，重开之后原样读回。日志格式、编码、重放与失败关闭都不改。
3. **桥接**（`supervised_operation.py`）
   - `run_supervised_operation` 交接时传 `run=`，内容是它这次运行的描述：
     - `spec` 是该 `InvocationSpec` 的六个字段，`command` 转为 `list`；
     - `evidence_dir` 与 `worktree_id` 是调用方传入的值。
   - 桥接的其余行为不变。
4. **恢复驱动**：新模块 `sentinelmesh/restart_recovery.py` 提供 `recover_after_restart(scheduler, *, owner, candidate_hash) -> dict`。`scheduler` 是 `Scheduler` 或 `DurableScheduler`。
   - 入参检查在一切之前：`owner` 必须是非空的精确 `str`，`candidate_hash` 必须可调用；否则报 `ContractError("E_RECOVERY_INPUT")`，这时不换代、不发事件。
   - 恰好调用一次 `generation = scheduler.rollover()`。
   - 按 `scheduler.operation_ids()` 的顺序，逐个读取操作的状态：
     - `RESERVED`：调用 `cancel_unstarted(op, generation)`，报告 `{"operation_id": op, "action": "cancelled", "state": <取消之后的状态>}`；
     - `HANDOFF_ACCEPTED` 或 `RUNNING`：
       - 没有描述：报告 `{"operation_id": op, "action": "skipped", "state": <状态>, "reason": "no run descriptor"}`；
       - 有描述：用描述重建 `InvocationSpec`，调用 `recover_supervised_operation(scheduler, operation_id=op, owner=owner, generation=generation, spec=..., evidence_dir=描述的 evidence_dir, candidate_hash=candidate_hash)`，报告 `{"operation_id": op, "action": "recovered", "state": 返回的 state}`；
     - 其他状态（`START_UNKNOWN`、`REVIEW_PENDING`、已取消等）：不碰，也不报告。
   - 错误：
     - 处理一个操作（取消或恢复）时出现 `Exception`、但不是 `LedgerError`：报告 `{"operation_id": op, "action": "failed", "state": <处理之前的状态>, "error": <code>}`，然后继续处理下一个。异常带有精确 `str` 的 `code` 属性时，`<code>` 用它，否则用异常的类型名；
     - `LedgerError` 立刻原样冒出，不再处理后面的操作；
     - 不是 `Exception` 的异常（例如 `KeyboardInterrupt`）同样原样冒出。
   - 返回 `{"generation": generation, "operations": [按领取顺序的报告]}`。请求保持停止。
5. **导入无副作用**：导入 `sentinelmesh.restart_recovery` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 5、7 节、oracle，以及 `scheduler.py`（`handoff`、`_identity_copy`、`operation`、`recover`、`rollover`、`cancel_unstarted`）、`durable_ledger.py` 的只读访问器、`supervised_operation.py`（`run_supervised_operation`、`recover_supervised_operation`）。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条修改三个已有模块。
3. 按第 4–5 条新建 `sentinelmesh/restart_recovery.py`，模块文档字符串写明只碰旧代际没有判定的操作，以及为什么不碰 `START_UNKNOWN`。
4. 新建 `tests/test_restart_recovery.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 恢复之后再调用一次：代际变成 3；第一次已经定案或取消的操作都不变，也不出现在第二次的报告里；
   - 第一次恢复失败（`failed`）的操作，原因消除之后，第二次调用能恢复它；
   - 描述里的证据目录已经不存在时，操作恢复为 `START_UNKNOWN`；
   - 证据目录存在却没有任何记录（交接之后可执行文件不存在，记录器在启动之前就拒绝）时，操作恢复为 `NOT_STARTED`，占用被释放；
   - 在持久账本上做一次恢复，再重开：状态与事件都相同；
   - 用 `reserve` 领取的操作也按领取顺序出现在 `operation_ids()` 里。
5. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_restart_recovery_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_restart_recovery.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_gaps.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_durable_ledger_oracle.py tests/test_durable_ledger_gaps.py tests/test_durable_ledger.py tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_verified_review_oracle.py tests/test_verified_review.py tests/test_verified_review_gaps.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.restart_recovery; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为模块还不存在；V02 是因为测试文件还不存在。V03 保护调度器、持久账本与桥接的已有行为。

## Definition of done
- D01：调度器的运行描述与两个只读方法（Behavior contract 第 1 条）；证据：V01、V02、V03。
- D02：持久账本的两个读访问器，描述在重开之后保留（第 2 条）；证据：V01、V02、V03。
- D03：桥接传出描述，其余行为不变（第 3 条）；证据：V01、V03。
- D04：恢复驱动：入参、换代、取消、恢复、跳过、失败报告与错误传播（第 4 条）；证据：V01、V02。
- D05：导入无副作用，已有模块不退化（第 5 条）；证据：V03、V04、V07。
- D06：Step 4 的测试齐全，不自行做变异检查；证据：V02、review。
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
  - 阅读约 6 分钟；
  - 实现约 8 分钟；
  - 测试约 8 分钟；
  - 运行检查约 5 分钟（oracle 约 10 秒，全量约 160 秒）；
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
