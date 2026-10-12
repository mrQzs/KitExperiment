# TASK: run-cli-pi

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 5527374449e2900b1a1a3cb621a89ca6d5063291
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/controller-cli.md` 第 3、5、6 节与第 7 节第 3 份，让 `smctl run once` 的作业文件可以指名第二个 adapter：
- `sentinelmesh/run_cli.py` 的 `_ADAPTERS` 从一个只含 `"offline"` 的集合改成按 adapter 自己的 `adapter_id` 键入的注册表：`{"offline": OfflineAdapter, "pi": PiNativeAdapter}`（`pi_events.PiNativeAdapter`，`adapter_id == "pi"`，`protocol_version == "pi-session-v3"`）；`load_job` 照旧按键集合精确匹配 `adapter_id`；`cmd_once` 按作业的 `adapter_id` 从注册表取类并实例化，交给 `run_adapter_operation`。
- 命令不启动任何 provider、不解析 pi 的可执行文件：子进程仍恰好是作业的 `command`，由监督器按平台规则解析 argv0（Windows 上 `pi.cmd` 垫片被拒，真实 pi 要以 `node.exe` + `cli.js` 指名）；真实 pi 仍 NOT_RUN，oracle 用 `python -c` 子进程把 `tests/fixtures/pi_native/` 的捕获流原样写到 stdout。
- 文档串按实改（模块、`_ADAPTERS` 的注释、`load_job`、`cmd_once`）。
- 让规划端 oracle `tests/test_run_cli_pi_oracle.py` 全部通过；新建执行器测试 `tests/test_run_cli_pi.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不启动真实 pi、不联网、不读凭据；不给命令加任何"找 pi"的逻辑（不读 `PATHEXT`、不读 `os.environ`、不猜 `node.exe` 的位置）。
- 不改 `pi_events.py`、`adapter_protocol.py`、`adapter_run.py`、`process_identity.py`、监督器、账本、调度器、`restart_recovery.py`、`ledger_cli.py`、`cli.py`（V08 钉住）；不改 `init`、`recover` 与 `once` 的其它行为；不改作业文件的键集合与其它校验。
- 不修改 `tests/test_run_cli.py`、`tests/test_run_cli_oracle.py`、`tests/test_run_cli_recover.py`、`tests/test_run_cli_recover_oracle.py`、夹具 `tests/fixtures/**`。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、允许集合之外的已有测试。

## Repository facts
- 基线 = master `1a5de1a`（`smctl run init/once/recover` 已集成，`run_cli.py` 约 500 行，`_ADAPTERS = frozenset({"offline"})`，`cmd_once` 写死 `OfflineAdapter()`）+ 规划端 oracle 提交 `5527374449e2900b1a1a3cb621a89ca6d5063291`，后者只含 `tests/test_run_cli_pi_oracle.py`（5 个用例，一个类 `PiOracle`，任何平台都跑，Windows 分支多两个子用例；Windows 上约 3 秒；在基线上 4 个用例失败——`adapter_id: "pi"` 被 `load_job` 拒绝）。oracle 在进程内调 `cli.main(argv)`；子进程只有 `python -c`。
- `pi_events.PiNativeAdapter(OfflineAdapter)`：`adapter_id = "pi"`，`protocol_version = DIALECT = "pi-session-v3"`；`run(invocation, frames)` 先 `translate(frames)` 再按离线桩归约；`invocation.adapter_id` 不等于 `"pi"` → `AdapterError("E_ADAPTER_IDENTITY")`（`run_adapter_operation` 在交接前也比较 `invocation.adapter_id` 与 `adapter.adapter_id`）。翻译器的用量维度是 `pi_events.USAGE_KEYS` 的五个：`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`、`total_tokens`（没有 `tokens`/`cost`）。
- `adapter_run.read_stream` 把 stdout.log 每行按 `strict_json_loads` 解析成对象列表（不检查归一化事件的形状），`_reduce` 再 `adapter.run(invocation, events)`——所以 pi 的原始帧流能直接走 `run_adapter_operation`；`AdapterError` 成为 outcome 的 `result_error`，用量不记录。
- 规划端草稿实测（Windows，真实 `smctl` 子进程，子进程 `python -c` 把夹具字节原样写到 stdout）：
  - `windows-pi101-kit-compat-a001.jsonl`（680 帧，完整）经 `adapter_id: "pi"`：`REVIEW_PENDING`、`stop_reason: "completed"`、`exit_code: 0`、`result_error: null`、`result = {"status": "SUCCEEDED", "settled": true, "raw_complete": true, "invocation_sha256": ..., "usage": {五个维度 + assistant_messages 50 + tool_calls 72 + complete true}}`，五个维度的数值与 `.meta.json` 里 dispatcher 的 `usage`（`input`→`input_tokens` 等，按 `USAGE_KEYS` 映射）完全一致，`usage_recorded` 五个 APPLIED、`usage_skipped: []`；账本 `operation_usage` 的 amount 同样；`smctl ledger reference --dialect pi-session-v3` 给出同样的 `usage`，`frames` 等于文件行数。macOS 夹具 `macos-pi102-adapter-protocol-port-a001.jsonl` 同样成立。
  - `windows-pi101-runtime-a001-truncated.jsonl`（没有终结帧）：`REVIEW_PENDING`、`result = {"status": "UNKNOWN", "settled": false, "raw_complete": false, "usage": {"status": "unknown"}}`、`usage_recorded: {}`、`usage_skipped` 是作业的五个维度（按作业顺序）；请求停止 `USAGE_UNKNOWN`，`attention` 里该操作 `reasons: ["usage_unknown"]`；之后的领取 `E_STOPPED`。
  - 离线事件喂给 `pi` → `result_error: "E_ADAPTER_SETTLEMENT"`、`result: null`、用量全 skipped；pi 帧喂给 `offline` → `result_error: "E_ADAPTER_EVENT"`；作业只列两个维度 → 只结算这两个，`usage_skipped: []`。
  - `adapter_id` 为 `"pi-session-v3"`、`"PI"`、`"Pi"`、`" pi"`、`"pi "`、含 NUL、`""`、`null`、`["pi"]` → `E_RUN_JOB`，detail 是 `adapter_id must be one of offline, pi`（文本形式时）；`""`/`null`/列表在更早的"文本键非空 str"检查就被拒（同码）。
  - Windows：`command` 的 argv0 是 `pi.cmd`（绝对路径）或裸名 `pi`（PATH 只含放着 `pi.cmd` 的目录）→ `NOT_STARTED`、`error: "E_RUNTIME_IDENTITY"`、`stream.status: "missing"`，证据目录没有 stdout.log；裸名 `python` 在作业 PATH 上解析成 `python.exe` 并正常运行到 `REVIEW_PENDING`。
  - 草稿 oracle 5/5；四个 run-cli 测试文件（oracle + 执行器）连同本 oracle 39 passed；全量见 V04。10 条故意偏差（注册表少 pi、pi 映射到离线桩、离线映射到 pi、按方言键入、大小写别名、`cmd_once` 写死任一 adapter、大小写/去空白匹配、拒绝信息少列一个）全部被 oracle 抓住。

## Scope
- allowed_modified_files:
  - sentinelmesh/run_cli.py
- allowed_new_files:
  - tests/test_run_cli_pi.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_run_cli_pi_oracle.py`, `tests/test_run_cli_oracle.py`, `tests/test_run_cli.py`, `tests/test_run_cli_recover_oracle.py`, `tests/test_run_cli_recover.py`, `tests/test_pi_events*.py`, `sentinelmesh/cli.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定（oracle 只通过 `cli.main` 驱动；执行器测试可以用私有名字）：`run_cli._ADAPTERS`（模块级 `dict[str, type]`，键恰好 `{"offline", "pi"}`，值分别是 `adapter_protocol.OfflineAdapter` 与 `pi_events.PiNativeAdapter` 这两个类本身，键必须等于各自类的 `adapter_id`）；`load_job`、`cmd_once`、`RunSpecError` 沿用。

1. **注册表**：`from .pi_events import PiNativeAdapter`；`_ADAPTERS = {OfflineAdapter.adapter_id: OfflineAdapter, PiNativeAdapter.adapter_id: PiNativeAdapter}`（或等价的字面量，键值如上）。没有别名、不做大小写折叠、不去空白：`load_job` 的检查仍是 `value["adapter_id"] not in _ADAPTERS` → `RunSpecError("E_RUN_JOB", "adapter_id must be one of " + ", ".join(sorted(_ADAPTERS)))`，即 detail 为 `adapter_id must be one of offline, pi`。`"pi-session-v3"` 不是 adapter id。
2. **`cmd_once` 取 adapter**：`run_adapter_operation(ledger, _ADAPTERS[job["adapter_id"]](), invocation, ...)`——每次调用新建一个实例，其它实参不变；`Invocation` 的 `adapter_id` 仍是作业的 `adapter_id`。命令里没有任何按 adapter 分支的逻辑：维度、上限、超时、环境、cwd、argv 照旧全来自作业文件。
3. **不解析程序**：不新增任何读 `PATHEXT`、`os.environ`、`shutil.which` 或猜测 `node.exe`/`cli.js` 的代码；argv0 的解析仍由监督器做（`process_identity.resolve_executable`）。
4. **文档串**：模块文档串把"第一份只认 offline"改为两个 adapter（`offline` 归约归一化事件，`pi` 翻译 pi `--mode json` 会话 v3 帧再归约），并说明命令不启动 provider、真实 pi 要在作业里以真实程序指名（Windows 上是 `node.exe` + `cli.js`，监督器拒绝 `pi.cmd` 垫片）、凭据与联网不在范围；`_ADAPTERS` 的注释同样；`load_job` 文档串的"`adapter_id` is one this contract accepts"改成指向注册表；`cmd_once` 文档串提到 adapter 由作业的 `adapter_id` 从注册表取。导入无副作用（`pi_events` 导入无副作用）。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3、5–7、9 节、oracle，`run_cli.py` 全文（重点 `_ADAPTERS`、`load_job`、`cmd_once`）、`pi_events.PiNativeAdapter` 与 `USAGE_KEYS`、`adapter_run._reduce`/`run_adapter_operation` 的 adapter 身份检查、`tests/fixtures/pi_native/*.meta.json` 的 `dispatcher_summary.usage`。基线与 Repository facts 不符就停止并报告。
2. 改 `run_cli.py`（第 1–4 条；约 15 行代码加文档串）。
3. 跑 V01 直到 oracle 全过（约 3 秒）。
4. 跑 `python -m pytest -q tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_run_cli_recover.py` 确认 `init`/`once`/`recover` 未受影响。
5. 新建 `tests/test_run_cli_pi.py`，只覆盖下面这些（oracle 已覆盖完整流与截断流的结算、两种错配、精确匹配、Windows 垫片；可以沿用 oracle 的 `smctl`/`ok`/`refused` 写法，但不要照抄 oracle 的用例；子进程只用 `sys.executable -c` 与 `sys.executable -m sentinelmesh.cli`；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - `run_cli._ADAPTERS` 直接检查：键集合恰好 `{"offline", "pi"}`，`_ADAPTERS["offline"] is OfflineAdapter`、`_ADAPTERS["pi"] is PiNativeAdapter`，每个键等于其类的 `adapter_id`，每个值的 `protocol_version`（`pi` 为 `"pi-session-v3"`）；
   - `unittest.mock.patch("sentinelmesh.run_cli.run_adapter_operation")` 捕获 `cmd_once` 的调用：作业 `adapter_id: "pi"` 时第二个位置实参 `type(...) is PiNativeAdapter`，`"offline"` 时 `is OfflineAdapter`，两次调用的实例不是同一个对象，`invocation.adapter_id` 等于作业的；返回一个假 outcome 字典时 stdout 原样含它；
   - `load_job` 直接调用：`adapter_id: "pi"` 的作业返回与文件相同（加两个缺省上限），`"pi-session-v3"` → `RunSpecError`，`.code == "E_RUN_JOB"`，`.detail == "adapter_id must be one of offline, pi"`；
   - 真实 `smctl` 子进程：macOS 夹具 `macos-pi102-adapter-protocol-port-a001.jsonl` 经 `python -c` 子进程写到 stdout、`adapter_id: "pi"`、五个维度 → 退出码 0、一行 JSON、`outcome.state == "REVIEW_PENDING"`、`usage_recorded` 五个 APPLIED；随后 `smctl ledger status` 的 `snapshot["total_tokens"]["known"]` 等于该夹具 `.meta.json` 的 `dispatcher_summary.usage.totalTokens`；
   - 同一次运行上 `smctl ledger events --operation <op> --type run.finished_for_review` 恰好一条，`--type usage.observed` 恰好五条（每个维度一条），账本 `operation_usage(op)` 里五个维度的 `evidence_hash` 都等于 stdout.log 字节的 sha256（夹具文件的 sha256）。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_pi_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_pi.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_run_cli_recover.py tests/test_pi_events.py tests/test_pi_events_oracle.py tests/test_pi_events_captured.py tests/test_adapter_run.py tests/test_ledger_reference.py tests/test_ledger_cli.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -m sentinelmesh.cli run --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`recover`
- V08（必要）：`git diff --quiet 5527374449e2900b1a1a3cb621a89ca6d5063291 -- sentinelmesh/cli.py sentinelmesh/ledger_cli.py sentinelmesh/pi_events.py sentinelmesh/durable_ledger.py sentinelmesh/scheduler.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/restart_recovery.py tests/test_run_cli.py tests/test_run_cli_oracle.py tests/test_run_cli_recover.py tests/test_run_cli_recover_oracle.py tests/fixtures` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04 在基线上失败，是因为 `adapter_id: "pi"` 还被拒绝；V02 是因为测试文件还不存在。V03 保护前两份的四个测试文件、pi 翻译器的三个测试文件、adapter 运行、账本引用与 `smctl ledger`；V08 保护十四个模块、前两份的四个测试文件与夹具（检查命令按 argv 直接执行，不经 shell）。全量约 170 秒。

## Definition of done
- D01：注册表与精确匹配（第 1 条）；证据：V01、V02。
- D02：`cmd_once` 按作业取 adapter，其它实参不变（第 2 条）；证据：V01、V02。
- D03：捕获的 pi 流经产品命令结算到 dispatcher 的数值、截断流用量未知并停止请求、两种错配是 `result_error`、Windows 上垫片 NOT_STARTED（oracle）；证据：V01。
- D04：不解析程序、不读环境（第 3 条）；证据：review、V08。
- D05：`init`/`once`/`recover`、pi 翻译器、账本不受影响，十四个模块与夹具不变，文档串按第 4 条，导入无副作用；证据：V03、V04、V07、V08、review。
- D06：Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
- 时间分配（上限 30 分钟，按 17 分钟规划、留 13 分钟余量）：
  - 阅读约 4 分钟；
  - 实现约 3 分钟（注册表、取 adapter、文档串）；
  - 测试约 6 分钟（5 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 3 秒，V03 约 40 秒，全量约 170 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
