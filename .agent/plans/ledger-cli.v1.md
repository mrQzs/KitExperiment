# TASK: ledger-cli

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 5c4bd85eed0277f147e4c5f471a29645447bbfbd
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
给运维一个命令行入口，崩溃之后不写代码也能把请求接回来：
- `smctl ledger status|stop|settle|resolve|resume LEDGER ...`；
- 调度器与持久账本各加两个只读方法：`request_ids()` 与 `operation_usage(operation)`。

新建 `sentinelmesh/ledger_cli.py`，在 `sentinelmesh/cli.py` 里挂上，修改 `sentinelmesh/scheduler.py` 与 `sentinelmesh/durable_ledger.py`，并让规划端 oracle `tests/test_ledger_cli_oracle.py` 全部通过。

## Non-goals
- 不改实现语言（用户决定）。
- 不做重启恢复命令（第一版只做五个命令，用户决定）；不做提额；不核验运维的权限与证据。
- 恢复时没有“照抄全部原因”的捷径（用户决定）；CLI 写入与开着账本的控制器冲突时，沿用账本的失败关闭，不加持有标记（用户决定）。
- 不改调度器的任何已有方法与语义；不改 `restart_recovery.py`、`supervised_operation.py`、`posix_supervisor.py`、`supervised_record.py`、`adapter_run.py`、`runtime.py`、`persistence.py`；`cli.py` 只加一处挂接。
- 不做交互式界面；不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `5c4bd85` = master `9e8aa98` + 规划端 oracle `tests/test_ledger_cli_oracle.py`（12 个用例，其中 1 个用子进程跑 `python -m sentinelmesh.cli`）。oracle 在基线上收集失败，因为 `ledger_cli` 还不存在。
- 设计说明：`.agent/design/ledger-cli.md`，第 5 节（用户决定）与第 6 节（规划端决定 C1–C5）。本合同的规则都来自那里。
- `smctl`：
  - 是 `sentinelmesh/cli.py`（`[project.scripts]` 的 `smctl = "sentinelmesh.cli:main"`），用 argparse 子命令；
  - `build_parser()` 里 `sub = parser.add_subparsers(dest="command", required=True)`；
  - `main()` 调用 `args.func(args)` 并返回它的整数，任何其他异常打印 `error: ...` 到 stderr、返回 2；
  - argparse 的用法错误以 `SystemExit(2)` 退出。
- 持久账本：
  - `open_ledger(path)` 重放、只读；路径上没有文件报 `E_LEDGER_MISSING`，绝不新建；
  - 实例有 `generation`、`seq`、`events`、`snapshot`、`operation`、`operation_ids`、`run_descriptor`、`stop_reasons` 与全部变更方法；
  - `close()` 之后实例不可用；读访问器都先 `_require_available()`。
- 调度器的相关事实：
  - `_requests` 是按建立顺序的 dict；`_Operation.usage` 是 `{维度: _Usage(call_key, revision, amount, final, evidence_hash)}`；
  - `UNRESOLVED` 是模块常量；`_snapshot` 的口径：状态属于 `UNRESOLVED` 且某个预留维度的用量不是“已知数额的最终值”，就算一个未知操作。
- `settle(operation, dimension, call_key=None, revision=1, amount=None, final=True, evidence_hash=None, correction=False)` 的单维度形式，`call_key` 默认为 `local:<op>:<dimension>`。
- `ContractError` 与 `LedgerError` 都有 `code` 与 `detail` 属性。
- 导入 `sentinelmesh.cli` 现在不导入 ctypes。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 12/12，全量 798 passed；18 个故意制造的偏差全部抓住，其中包括：缺账本就新建、拒绝写到 stdout 或退出码 2、恢复照抄当前原因、`--reason` 不是必填、`status` 漏报或多报、补记不是最终值、裁定用旧代际、不关账本、请求 id 排序、用量读出共享对象、成功输出多行。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
  - sentinelmesh/durable_ledger.py
  - sentinelmesh/cli.py
- allowed_new_files:
  - sentinelmesh/ledger_cli.py
  - tests/test_ledger_cli.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `sentinelmesh/runtime.py`, `tests/test_ledger_cli_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **两个只读方法**（调度器）：都不发事件、不改账本。
   - `request_ids(self) -> tuple[str, ...]`：全部请求 id，按建立的顺序。
   - `operation_usage(self, operation) -> dict[str, dict]`：这个操作记下的每一项用量，`{维度: {"call_key", "revision", "amount", "final", "evidence_hash"}}`，是一份新的拷贝，调用方修改它不影响账本；没有记下用量时是空 dict。`operation` 不是精确 `str` 或是未知的操作：`ContractError("E_OPERATION_UNKNOWN")`。
2. **持久账本的读访问器**：`DurableScheduler.request_ids()` 与 `operation_usage(operation)`，与 `operation()` 一样，实例不可用时报 `LedgerError("E_LEDGER_UNAVAILABLE")`，否则读内存、不碰日志。
3. **命令组**：
   - 新模块 `sentinelmesh/ledger_cli.py` 提供 `register(subparsers)`，在 `smctl` 下加 `ledger` 命令组（子命令必填）；`cli.py` 的 `build_parser()` 在返回之前调用它。
   - 每个命令的第一个位置参数是账本路径。命令打开账本（`open_ledger`），最多做一次变更，并在 `finally` 里关闭，拒绝时也一样。
   - 变更一律用账本的当前代际，从不换代。
4. **输出与退出码**：
   - 成功：退出码 0；stdout 恰好一行，`json.dumps(结果, ensure_ascii=False, sort_keys=True)` 加换行；stderr 为空。
   - 账本拒绝（`ContractError` 或 `LedgerError`，包括打不开账本）：退出码 3；stderr 恰好一行 JSON `{"error": code, "detail": detail}`；stdout 为空；账本不变。
   - 参数错误：沿用 argparse，退出码 2。
5. **`status LEDGER`**：输出 `{"ledger": 给定的路径, "generation", "seq", "requests": [...]}`。
   - `requests` 按 `request_ids()` 的顺序，每项 `{"request_id", "stopped", "stop_reasons", "snapshot", "attention"}`：
     - `stopped` 是布尔值；
     - `stop_reasons` 是列表；
     - `snapshot` 是 `snapshot(request)` 的结果。
   - `attention` 按 `operation_ids()` 的顺序，只列至少有一条理由的操作，每项 `{"operation_id", "task_id", "state", "generation", "supervised", "usage", "reasons"}`：
     - `supervised` 表示有没有运行描述；
     - `usage` 是 `operation_usage` 的结果；
     - `reasons` 取下面三个值中成立的那些，按这个顺序：
       - `start_unknown`：状态是 `START_UNKNOWN`；
       - `undecided`：状态是 `RESERVED`、`HANDOFF_ACCEPTED` 或 `RUNNING`，并且代际比当前旧；
       - `usage_unknown`：状态属于 `UNRESOLVED`，并且预留里有一个维度的用量不是“已知数额的最终值”。
   - `status` 不写账本文件。
6. **变更命令**：
   - `stop LEDGER --request R --reason TEXT`：
     - 调 `stop(R, TEXT)`；
     - 输出 `{"command": "stop", "request_id", "stop_reasons"}`（停止之后的列表）。
   - `settle LEDGER --operation OP --dimension D --amount N --evidence HASH [--revision N] [--correction]`：
     - `--amount` 与 `--revision` 是整数，`--revision` 默认 1；
     - 调单维度的 `settle`：`final=True`，`call_key` 用默认值，`--correction` 对应 `correction=True`；
     - 输出 `{"command": "settle", "operation_id", "dimension", "result", "usage"}`，其中 `result` 是调度器返回的结果，`usage` 是补记之后的 `operation_usage`。
   - `resolve LEDGER --operation OP --resolution {NOT_STARTED,ABANDONED,FINISHED} --operator NAME --evidence HASH --note TEXT [--candidate HASH]`：
     - 调 `resolve_unknown`；
     - 输出 `{"command": "resolve", "operation"}`，`operation` 是裁定之后的操作视图；
     - `--resolution` 取值以外的值是参数错误。
   - `resume LEDGER --request R --reason A [--reason B ...] --operator NAME --note TEXT`：
     - `--reason` 必填、可重复，按给出的顺序作为 `expected_reasons`，不读当前原因来补；
     - 输出 `{"command": "resume", "request_id", "previous_reasons"}`。
7. **导入无副作用**：导入 `sentinelmesh.cli` 与 `sentinelmesh.ledger_cli` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 5–6 节、oracle，以及 `cli.py`、`durable_ledger.py` 的读访问器、`scheduler.py` 的 `_Usage`、`UNRESOLVED`、`_snapshot`、`settle`、`resolve_unknown`、`resume`、`stop_reasons`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–2 条修改调度器与持久账本，第 3–6 条新建 `sentinelmesh/ledger_cli.py` 并在 `cli.py` 挂上。
3. 新建 `tests/test_ledger_cli.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - `status` 在几种状态下的 `attention`：
     - 放弃但用量不完整、复审中但用量不完整：都是 `usage_unknown`；
     - 当前代际里正在运行的：不出现；
     - 多个请求：各自独立；
   - `settle` 的 `--revision` 与 `--correction`：更正之后结果与用量正确；没有 `--correction` 的更小数额报 `E_USAGE_CORRECTION_REQUIRED`；
   - `resolve FINISHED --candidate`：进入复审；不带 `--candidate` 报 `E_CLEANUP_OR_CANDIDATE`；
   - 非 ASCII 的停止原因（例如 `安全暂停`）在输出里原样保留；
   - `smctl ledger` 不带子命令是参数错误。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_ledger_cli_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_ledger_cli.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_durable_ledger_oracle.py tests/test_durable_ledger_gaps.py tests/test_durable_ledger.py tests/test_restart_recovery_oracle.py tests/test_restart_recovery.py tests/test_request_resume_oracle.py tests/test_request_resume.py tests/test_resume_holds_oracle.py tests/test_resume_holds.py tests/test_explicit_holds_oracle.py tests/test_explicit_holds.py tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_reference.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.cli, sentinelmesh.ledger_cli; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为 `ledger_cli` 还不存在；V02 是因为测试文件还不存在。V03 保护调度器、持久账本、重启恢复、请求恢复与已有的参考用例。

## Definition of done
- D01：两个只读方法与持久账本的读访问器（Behavior contract 第 1–2 条）；证据：V01、V02、V03。
- D02：`ledger` 命令组的挂接、打开与关闭、当前代际（第 3 条）；证据：V01、V02。
- D03：输出与退出码（第 4 条）；证据：V01、V02。
- D04：`status` 的内容与只读（第 5 条）；证据：V01、V02。
- D05：四个变更命令（第 6 条）；证据：V01、V02。
- D06：导入无副作用，已有模块不退化；Step 3 的测试齐全，不自行做变异检查（第 7 条）；证据：V03、V04、V07、review。
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
  - 阅读约 5 分钟；
  - 实现约 8 分钟；
  - 测试约 9 分钟；
  - 运行检查约 5 分钟（oracle 约 12 秒，全量约 185 秒）；
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
