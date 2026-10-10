# TASK: ledger-events

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 1981a368b8174a7a0b8455ff1dcc1ead0f4ebb53
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
给 `smctl ledger` 加一个只读命令 `events`：按请求、操作、类型过滤，按序号分页，把账本记录的事件原样打印出来，头部报告检查点的位置；给持久账本加一个只读属性 `checkpoint_seq`。
- 修改 `sentinelmesh/ledger_cli.py`（`events` 命令）与 `sentinelmesh/durable_ledger.py`（`checkpoint_seq`）；
- 让规划端 oracle `tests/test_ledger_events_oracle.py` 全部通过；
- 新建执行器测试 `tests/test_ledger_events.py`（清单见 Step 3，刻意不长）。

## Non-goals
- 不改 `status`、`reference`、`compact`、`stop`、`settle`、`resolve`、`resume` 的行为与输出（用户决定：检查点只报告在 `events` 的头部）。
- 不改事件的形状，不加派生字段（用户决定）；不给 Scheduler 加按条件读取的接口；不改 `scheduler.py`。
- 不做文本或表格格式；不做按时间过滤（事件没有时间戳）。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与已有测试；不联网、不读取凭据、不调用 pi。

## Repository facts
- 基线 = master `7ac6b2b`（设计说明 `.agent/design/ledger-events.md`：第 3 节方案、第 5 节用户决定、第 6 节规划端决定 E1–E7）+ 规划端 oracle 提交，后者含 `tests/test_ledger_events_oracle.py`（11 个用例：全量原样读回、三种过滤与交集、分页拼回全量、默认值与参数边界、拒绝、压缩前后与检查点、`checkpoint_seq` 访问器、从不写、类型表等于 Scheduler 发出的类型、入口、导入无副作用）；在基线上 oracle 全部失败，因为 `events` 子命令与 `checkpoint_seq` 都还不存在。
- 事件：内存 Scheduler 的 `_emit` 记录，每条恰好五个键 `seq`（从 1 连续）、`operation_id`（str 或 None）、`type`（str）、`generation`（int）、`payload`（JSON）。`DurableScheduler.events` 返回全部事件的深拷贝（tuple of dict）。事件类型恰好 16 种：带操作 id 的 `scheduler.claimed`、`scheduler.handoff_accepted`、`run.started`、`usage.observed`、`run.finished_for_review`、`run.verdict`、`scheduler.start_unknown`、`scheduler.not_started`、`scheduler.recovered`、`scheduler.unknown_resolved`、`scheduler.cancelled_before_handoff`；`operation_id` 为 None 的 `request.created`、`request.stopped`、`request.stop_observed`、`request.resumed`（payload 都有 `request_id`）与 `controller.fenced`（payload 只有 `generation`）。
- 账本的只读方法：`request_ids()`（按播种顺序的 tuple）、`operation(op)`（未知或非精确 str 报 `ContractError("E_OPERATION_UNKNOWN")`，视图含 `request_id`）、`operation_ids()`、`seq`、`generation`、`events`；`compact()` 返回 `{"seq", "last_sha", "sha256", "removed"}`。
- `sentinelmesh/durable_ledger.py`：`DurableScheduler.__init__(self, path, scheduler, db, seq, last_sha)`，属性在 `self._lock` 内先 `_require_available()`（`self._db is None` → `LedgerError("E_LEDGER_UNAVAILABLE")`）；`create_ledger` 末尾 `return DurableScheduler(location, scheduler, db, 0, "")`；`open_ledger` 末尾 `return DurableScheduler(location, scheduler, db, start_seq + len(commands), last_sha)`，其中 `checkpoint` 是 `_read_checkpoint` 的结果（None 或 `(seq, last_sha, state)`），`start_seq` 在有检查点时是它的 `seq`；`compact()` 在 `COMMIT` 之后、`return {...}` 之前没有别的语句。
- `sentinelmesh/ledger_cli.py`：`_with_ledger(args, work)` 打开账本、`work(ledger)`、`finally` 关闭、成功 `_emit(result)`（stdout 一行 `json.dumps(..., ensure_ascii=False, sort_keys=True)`），`ContractError` 与 `LedgerError` 都 `_refuse`（stderr 一行 `{"error", "detail"}`，返回 3）；`register(subparsers)` 在 `ledger` 组下注册七个子命令，每个子命令第一个位置参数是 `ledger`；`cli.main` 对 argparse 错误 `SystemExit(2)`（stderr 带 `usage:`），对 `args.func` 之外的任何异常打印 `error: ...` 返回 2（没有 `usage:`）。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 11/11；全量 741 passed / 289 skipped；24 个故意制造的偏差全部被 oracle 抓住（请求级事件不归属、操作事件按任务归属、忽略操作过滤、多个类型只取第一个、`--after` 含等于、页多一条、`next` 永远为空或最后一页也给、`matched` 数页、`total` 数匹配、未知请求或操作不拒绝、先查操作后查请求、默认 50、上限 10001、接受带符号、接受 0、不报检查点、压缩不更新访问器、打开不报检查点、降序、加派生字段、类型表少一个、命令改名）。
- 工具链：Windows Server 2025，`python` 3.12.10（系统安装，没有 venv）与 pytest 9.1.1；基线上 `python -m pytest -q` 因 oracle 失败而失败，其余通过（原生 POSIX 用例在 Windows 上跳过，这是预期）。

## Scope
- allowed_modified_files:
  - sentinelmesh/ledger_cli.py
  - sentinelmesh/durable_ledger.py
- allowed_new_files:
  - tests/test_ledger_events.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_ledger_events_oracle.py`, `tests/test_ledger_compaction_oracle.py`, `tests/test_ledger_compaction.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_durable_ledger.py`, `tests/test_durable_ledger_gaps.py`, `tests/test_ledger_cli.py`, `tests/test_ledger_cli_oracle.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/cli.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有“是 str / int”都是精确类型。私有名字可自定，公开名字不可。

1. **`DurableScheduler.checkpoint_seq`**（只读属性，`int | None`）：`__init__` 末尾新增参数 `checkpoint_seq: int | None = None` 存为 `self._checkpoint_seq`；`open_ledger` 传入检查点的 `seq`（没有检查点传 None）；`create_ledger` 不传（None）；`compact()` 在 `COMMIT` 成功之后把它设为 `self._seq`（失败路径不改）。读取与其他只读属性相同：锁内、先 `_require_available()`（关闭或失效 → `E_LEDGER_UNAVAILABLE`），不碰日志。
2. **常量与参数类型**（`ledger_cli.py`）：`_EVENT_TYPES` 是 Repository facts 列出的 16 个类型名的 tuple（顺序自定）；`_DEFAULT_EVENTS = 100`；`_MAX_EVENTS = 10000`。`--after` 与 `--limit` 的值必须只由十进制数字组成（没有正负号、下划线、空白、小数点；用 `re.fullmatch("[0-9]+", text)` 一类的判断），`--limit` 还必须在 1 到 10000 之间；不符的值用 `argparse.ArgumentTypeError` 拒绝，于是 argparse 以退出码 2 退出、stderr 带 `usage:`、stdout 为空、账本不打开。
3. **命令与参数**：`smctl ledger events LEDGER [--request R] [--operation OP] [--type T ...] [--after SEQ] [--limit N]`，由 `register` 注册；`--type` 用 `action="append"` 与 `choices=_EVENT_TYPES`（不在表里的类型，含空串，退出码 2）；`--after` 默认 0；`--limit` 默认 `_DEFAULT_EVENTS`。
4. **流程**：经 `_with_ledger`。`work` 里依次：
   1. 给了 `--request` 且它不在 `ledger.request_ids()` 里 → `raise ContractError("E_REQUEST_UNKNOWN", <一句说明>)`；
   2. 给了 `--operation` → 调 `ledger.operation(OP)`（未知报 `E_OPERATION_UNKNOWN`）；请求的检查在操作之前；
   3. 取 `ledger.events`，按原顺序（`seq` 升序）过滤，几个条件取交集：
      - `--request R`：事件的 `operation_id` 不是 None 时，按 `ledger.operation(operation_id)["request_id"] == R`（每个操作只查一次）；是 None 时，按 payload 是 dict 且 `payload.get("request_id") == R`（所以 `controller.fenced` 不属于任何请求）；
      - `--operation OP`：`operation_id == OP`；
      - `--type`：`type` 属于给出的类型集合（多个 `--type` 是并集）；
   4. 分页：`remaining` = 匹配事件里 `seq > after` 的那些（升序）；`page = remaining[:limit]`。
   全程不调用账本的任何变更方法，不写任何文件；账本拒绝（含打不开）退出码 3，stderr 一行 `{"error", "detail"}`，stdout 不输出。
5. **输出**：stdout 一行 JSON（`_emit`），恰好 9 个键：`command`（`"events"`）、`ledger`（命令行给的路径原样）、`generation`、`seq`（账本的）、`checkpoint`（`ledger.checkpoint_seq`，int 或 None）、`total`（账本事件总条数）、`matched`（过滤后、分页前的条数）、`events`（`page`，每条事件原样五个键，不增不减不改）、`next`（`remaining` 比 `page` 长时是 `page` 最后一条的 `seq`，否则 None）。
6. **兼容性**：其他命令的行为与输出不变（`status` 仍是 `{"ledger", "generation", "seq", "requests"}`）；`ledger_cli` 与 `durable_ledger` 导入无副作用（V07）；不改任何公开签名（`__init__` 只加带默认值的末尾参数）。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 2、3、5、6 节、oracle，以及 `ledger_cli.py`（`_with_ledger`、`register`、`cmd_compact`）、`durable_ledger.py`（`DurableScheduler.__init__`、只读属性、`compact()` 的结尾、`open_ledger` 的结尾）。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1 条改 `durable_ledger.py`，按第 2–5 条在 `ledger_cli.py` 里加 `events`（参数类型函数、归属函数、命令函数、注册）。
3. 新建 `tests/test_ledger_events.py`，只覆盖下面这些（oracle 已经覆盖全量读回、三种过滤与交集、分页拼回、默认值与边界、拒绝、压缩与检查点、类型表、入口；**可以复用 oracle 的辅助函数写法，不必重复 oracle 已有的用例**；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言；账本用 `create_ledger` 在临时目录里建，场景自定、尽量小）：
   - `checkpoint_seq`：新建、重开（无检查点）为 None；`compact()` 之后等于 `seq`；再追加命令后不变、`seq` 加一；重开后相同；关闭后 `E_LEDGER_UNAVAILABLE`；
   - `--request`：请求级四种类型（`request.created`、`request.stopped`、`request.stop_observed` 用预算守卫触发或跳过并写明、`request.resumed`）按 payload 归属，操作事件按操作的请求归属，`controller.fenced` 在任何 `--request` 下都不出现；
   - 过滤的交集为空：`matched` 0、`events` 空、`next` None；
   - 分页：`--limit 1` 翻完一个小账本的全部事件并拼回；`--after` 等于最后一条的 `seq` → 空页、`next` None、`matched` 与 `total` 不变；
   - 参数错误（退出码 2、stdout 空、stderr 含 `usage:`）：`--limit 0`、`10001`、`+5`、`1_0`、`x`、`--after -1`、`1.5`、`--type` 未知与空串；
   - 默认页：101 条事件的账本不带 `--limit` 返回 100 条、`next` 为 100；
   - 从不写：成功一次、拒绝一次之后账本文件摘要与命令表不变；
   - 一次经 `subprocess` 跑 `python -m sentinelmesh.cli ledger events ...`。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_ledger_events_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_ledger_events.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py tests/test_ledger_compaction.py tests/test_ledger_compaction_oracle.py tests/test_ledger_reference.py tests/test_ledger_reference_oracle.py tests/test_durable_ledger.py tests/test_durable_ledger_oracle.py tests/test_durable_ledger_gaps.py` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.ledger_cli, sentinelmesh.durable_ledger; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要，基线应失败）：`python -m sentinelmesh.cli ledger events --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`--after`

V01、V04 在基线上失败，是因为 `events` 子命令与 `checkpoint_seq` 还不存在；V02 是因为测试文件还不存在；V08 是因为 argparse 不认识这个子命令。V03 保护七个已有命令、压缩与账本的全部现有规则；V07 保护导入无副作用。

## Definition of done
- D01：`checkpoint_seq`（Behavior contract 第 1 条）；证据：V01、V02、V03。
- D02：参数类型与常量、命令注册与参数错误（第 2–3 条）；证据：V01、V02、V08。
- D03：流程、检查顺序、过滤与归属、分页、从不写（第 4 条）；证据：V01、V02。
- D04：输出的 9 个键与 `next` 规则、事件原样（第 5 条）；证据：V01、V02。
- D05：兼容性与导入无副作用（第 6 条）；Step 3 的测试齐全，不自行做变异检查；证据：V03、V04、V07、review。
- D06：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 pi。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配（上限 30 分钟，按 20 分钟规划、留 10 分钟余量）：
  - 阅读约 4 分钟；
  - 实现约 5 分钟（两处改动合计约 90 行）；
  - 测试约 6 分钟（约 10 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 7 秒，V03 约 60 秒，全量约 95 秒）；
  - 执行报告约 2 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
