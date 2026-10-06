# TASK: adapter-supervised-run

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: fd66ce31a024356fb9272e617d8e382f73211ee9
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
新建 `sentinelmesh/adapter_run.py`，完成下面这条链路：
- 把规范化的 `adapter_protocol.Invocation` 交给原生 POSIX 监督器运行，经调度器桥接完成交接与落定；
- 读取 stdout 中的规范化事件流，交给 adapter 归约；
- 在运行被结束复审之前，把结果中的用量作为最终值记入调度器。

并让规划端 oracle `tests/test_adapter_run_oracle.py` 全部通过。

## Non-goals
- 不修改 `scheduler.py`、`supervised_operation.py`、`posix_supervisor.py`、`supervised_record.py`、`adapter_protocol.py`、`adapter.py` 或其他已有模块。
- 不调用真实 provider CLI，不翻译 Pi 的原生事件格式；不处理换代后 `recover` 路径上的用量。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml` 与 oracle；不联网、不读取凭据。

## Repository facts
- 基线 `fd66ce3` = master `173c1a0` + 规划端 oracle（16 个用例，启动真实的本地 python 进程）。
- 设计说明：`.agent/design/adapter-supervisor-wiring.md` 第 3 节与第 6 节（规划端的决定）。
- 调度器的预算检查决定了用量必须在 `finish` 之前记入：
  - `Scheduler.finish()` 结束时检查预算。只要操作进入 REVIEW_PENDING 时还有预约维度的用量不是最终值，就把 request 停止为 `USAGE_UNKNOWN`；
  - 停止是永久的，之后的领取都报 `E_STOPPED`；
  - 操作处于 RUNNING 时记入的用量不算未结清。
- 桥接 `supervised_operation.run_supervised_operation` 的挂钩时机：
  - 它只在“结束复审”这条路径上调用 `candidate_hash(evidence_dir)`：先 `mark_running`，然后求 `candidate_hash`，紧接着 `finish`。调用时操作还是 RUNNING；
  - `candidate_hash` 抛出时，桥接调用 `unknown(..., reason="START_UNKNOWN")`，再原样重新抛出；
  - 其他路径（NOT_STARTED、START_UNKNOWN）不调用它。
- 其他接口：
  - `Scheduler.settle(operation, 映射, evidence_hash=…)` 一次记录全部条目，或一项都不记；每项的 call_key 为 `local:<operation>:<维度>`，revision 1，final 为 True。
  - 监督器把 stdout 写进证据目录的 `stdout.log`（`O_EXCL` 创建，0600）。子进程与调用方是同一账户，能替换这个路径。
  - `sentinelmesh.core.strict_json_loads(bytes)` 拒绝时抛出带 `.code` 的 `ProtocolError`，例如 `E_JSON_SYNTAX`；空输入是 `E_JSON_LIMIT`。
  - `sentinelmesh.adapter_protocol`：
    - `OfflineAdapter.run(invocation, events)` 返回 `AdapterResult(invocation_sha256, events, settled, status, usage, raw_complete)`；
    - 未结算的结果，usage 是 `{"status": "unknown"}`；
    - `AdapterError(code, message, *, seq=None)`。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 16/16，全量 518 passed；
  - 13 个故意制造的偏差中 12 个被抓住；
  - 漏掉的是“去掉 fstat 的大小预检”：之后的有界读取照样拒绝，属于等价写法。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files: 无
- allowed_new_files:
  - sentinelmesh/adapter_run.py
  - tests/test_adapter_run.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/scheduler.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/adapter.py`, `tests/test_adapter_run_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **常量**：`STREAM_NAME = "stdout.log"`、`DEFAULT_MAX_STREAM_BYTES = 1_048_576`、`MAX_STREAM_BYTES = 16 * 1_048_576`。
2. **`supervised_spec(invocation, *, cwd, timeout_seconds) -> InvocationSpec`**
   - `type(invocation) is not adapter_protocol.Invocation` 时报 `AdapterError("E_ADAPTER_INVOCATION", …)`。
   - 否则返回 `InvocationSpec(invocation.request_id, invocation.run_id, invocation.command, cwd, invocation.environment_sha256, timeout_seconds)`。它的 `RuntimeErrorCode`（例如超时不是正整数时的 `E_RUNTIME_TIMEOUT`）原样冒出。
3. **`read_stream(evidence_dir, *, max_bytes=DEFAULT_MAX_STREAM_BYTES) -> dict`**
   - `max_bytes` 必须是 `1..MAX_STREAM_BYTES` 之间的精确 int，否则报 `AdapterError("E_ADAPTER_SCHEMA", …)`。
   - 用 `os.open(os.path.join(evidence_dir, STREAM_NAME), O_RDONLY | O_NOFOLLOW | O_NONBLOCK)` 打开：
     - `FileNotFoundError`：返回 `{"status": "missing", "sha256": None, "bytes": None, "events": None}`；
     - 其他 `OSError`（包括路径是符号链接）：`status` 为 `"unreadable"`，其余三项同上。
   - 打开之后：
     - `fstat` 显示不是普通文件：`unreadable`；
     - 大小超过 `max_bytes`：`too_large`；
     - 最多读 `max_bytes + 1` 字节，读到的超过 `max_bytes`：`too_large`；
     - 读取时出现 `OSError`：`unreadable`。

     描述符在任何情况下都要关闭。
   - 否则取 `sha256` 为全部字节的 SHA-256（小写十六进制），`bytes` 为字节数，然后解析事件：
     - 按 `b"\n"` 切分；最后一段为空时丢掉；
     - 每一段用 `core.strict_json_loads` 解析。抛出 `ProtocolError` 时，返回 `{"status": "invalid", "sha256": …, "bytes": …, "events": None, "line": 段号（从 1 起）, "code": exc.code}`；
     - 解析结果不是精确的 dict：同上，`code` 为 `"E_ADAPTER_STREAM"`；
     - 全部成功：返回 `{"status": "ok", "sha256": …, "bytes": …, "events": [各段的 dict]}`。0 字节的文件得到 `events: []`。
4. **`run_adapter_operation(scheduler, adapter, invocation, *, cwd, timeout_seconds, environment, owner, generation, evidence_dir, worktree_id, candidate_hash, usage_dimensions=(), allowed_secret_names=frozenset(), now=0, max_stream_bytes=DEFAULT_MAX_STREAM_BYTES) -> dict`**
   - 下列检查按顺序进行，都在交接之前，不改变调度器、不创建文件：
     1. `type(invocation) is not Invocation`：`E_ADAPTER_INVOCATION`；
     2. `not isinstance(adapter, OfflineAdapter)`：`E_ADAPTER_INVOCATION`（允许子类，例如 adapter_id 不同的子类）；
     3. `invocation.adapter_id` 不为 None 且不等于 `adapter.adapter_id`：`E_ADAPTER_IDENTITY`；
     4. `usage_dimensions` 必须满足下列全部条件，否则报 `E_ADAPTER_SCHEMA`：
        - 是精确的 tuple；
        - 每项是非空的精确 str；
        - 互不重复；
        - 不含 `"dispatches"`；
     5. `max_stream_bytes` 同第 3 条：`E_ADAPTER_SCHEMA`；
     6. `spec = supervised_spec(invocation, cwd=cwd, timeout_seconds=timeout_seconds)`，错误原样冒出。
   - 然后调用 `run_supervised_operation(scheduler, operation_id=invocation.run_id, owner=…, generation=…, spec=spec, environment=…, evidence_dir=…, worktree_id=…, candidate_hash=<包装>, allowed_secret_names=…, now=…)`，它的错误原样冒出。
   - **包装的 candidate_hash**：桥接只在结束复审的路径上调用它，此时操作是 RUNNING。它按顺序做三件事：
     1. 归约：`stream = read_stream(evidence_dir, max_bytes=max_stream_bytes)`。`status` 为 `ok` 时，`result = adapter.run(invocation, stream["events"])`；`adapter.run` 抛出 `AdapterError` 时，`result` 为 None，记下 `result_error = exc.code`。
     2. 记用量：只有 `result` 不为 None，且 `result.settled is True`、`result.raw_complete is True` 时才记。
        - 映射 = `{d: result.usage[d]}`，取 `usage_dimensions` 中满足下列条件的每个 d：d 在 `result.usage` 里，且值是精确 int 并且 ≥ 0；
        - 映射非空时，`recorded = scheduler.settle(invocation.run_id, 映射, evidence_hash=stream["sha256"])`；
        - `skipped` 为 `usage_dimensions` 中没有进入映射的维度，保持原顺序。
     3. 返回调用方的 `candidate_hash(evidence_dir)`，它的异常原样冒出（桥接随后把操作置为 START_UNKNOWN）。
   - 包装没有被调用时（其他任何路径），在桥接返回之后做一次第 1 步的归约，只用于报告，不记用量。
   - 返回桥接的 dict，再加上：
     - `"stream"`：`{"status", "sha256", "bytes"}`；只有 `status` 为 `invalid` 时，再加 `"line"` 与 `"code"`；
     - `"result"`：None，或 `{"settled", "status", "raw_complete", "usage": usage 的 dict 副本, "invocation_sha256"}`；
     - `"result_error"`：`adapter.run` 的错误码，或 None；
     - `"usage_recorded"`：`settle` 的返回值；没有记录时为 `{}`；
     - `"usage_skipped"`：没有记录的维度列表（list）。没有任何维度符合条件时，就是 `usage_dimensions` 的全部。
5. **导入无副作用**：导入 `sentinelmesh.adapter_run` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3、6 节、oracle，以及会用到的已有接口（`supervised_operation.py`、`adapter_protocol.py`、`scheduler.py` 的 `settle` 与 `finish`、`core.strict_json_loads`）。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–5 条新建 `sentinelmesh/adapter_run.py`。
3. 新建 `tests/test_adapter_run.py`，覆盖下列 oracle 没有覆盖的用例。
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处；
     - 每个用例都清理自己启动的进程；
     - 冒出的 `KeyboardInterrupt` 要在测试里拦住，不要让它中断 pytest 会话。
   - `read_stream`：
     - CRLF 结尾的行能正常解析；
     - 最后一行没有换行符也能解析；
     - 只含空格的行报 `invalid`，并给出行号；
     - 路径上是 FIFO（`os.mkfifo`）时报 `unreadable`，而且不阻塞；
     - 恰好 `max_bytes` 字节为 `ok`，多 1 字节为 `too_large`；
     - `max_bytes` 不合法时报 `E_ADAPTER_SCHEMA`。
   - 调用方的 `candidate_hash` 抛出异常：
     - 用量已经记入（记录在求候选哈希之前）；
     - 操作为 START_UNKNOWN；
     - 原来的异常冒出。
   - 超时：打印完整事件流之后 `sleep`，`timeout_seconds=1`。清理已确认，所以用量照常记入。
   - `usage_dimensions=()`：不记录任何用量，`usage_skipped` 为 `[]`。
   - 相对路径的 `cwd`：交接之后监督器拒绝，状态为 NOT_STARTED，`error` 为 `E_RUNTIME_INVOCATION`，流为 `missing`。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_adapter_run_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_adapter_run.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_adapter_protocol_reference_oracle.py tests/test_adapter_protocol_differential_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.adapter_run; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为模块还不存在；V02 在基线上失败，是因为测试文件还不存在。V03 保护桥接与 adapter 协议层。

## Definition of done
- D01：`supervised_spec` 与 `read_stream`（Behavior contract 第 1–3 条）；证据：V01、V02。
- D02：`run_adapter_operation` 的前置检查与流程（第 4 条）；证据：V01、V02。
- D03：用量只在结束复审之前、结果已结算且完整时记入，其余情形一律不记（第 4 条）；证据：V01、V02。
- D04：导入无副作用，已有模块不退化（第 5 条）；证据：V03、V04、V07。
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
  - 运行检查约 4 分钟（oracle 约 5 秒，全量约 110 秒）；
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
