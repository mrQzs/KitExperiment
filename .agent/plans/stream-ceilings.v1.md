# TASK: stream-ceilings

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 35e7f05f63e3c48947db51e70ae7e0894714dd3a
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
把受监督运行的输出流的两道上限按设计说明 `.agent/design/stream-ceilings.md` 改好：整流最大 64 MiB；单行上限成为 `read_stream` 与 `run_adapter_operation` 的参数 `max_line_bytes`（默认 1 MiB、最大 8 MiB）；`smctl ledger reference` 与控制器路径共用同一组常量。
- 修改 `sentinelmesh/adapter_run.py`（常量、参数、校验、传参）与 `sentinelmesh/ledger_cli.py`（`cmd_reference` 传两个最大值）；
- 修改 `tests/test_adapter_run.py` 里钉住旧数字的一行；
- 让规划端 oracle `tests/test_stream_ceilings_oracle.py` 全部通过；
- 新建执行器测试 `tests/test_stream_ceilings.py`（清单见 Step 4，刻意不长）。

## Non-goals
- 不改 `read_stream` 答案的形状（`too_large` 仍是 `sha256`、`bytes` 为 None；`invalid` 仍带 `line` 与 `code`），不改检查顺序（`expected` 的身份与大小比较先于整流上限）。
- 不改协议常量（`core.DEFAULT_MAX_JSON_BYTES`、`DEFAULT_MAX_JSON_DEPTH`）、规范化事件的 payload 上限、翻译器、监督器；不做流式逐行解析。
- 不改 `DEFAULT_MAX_STREAM_BYTES`（用户决定：默认保持 1 MiB）。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试；不联网、不读取凭据、不调用 pi。

## Repository facts
- 基线 = master `66ef144`（设计说明 `.agent/design/stream-ceilings.md`：第 2 节现状、第 3 节方案、第 5 节用户决定、第 6 节规划端决定 S1–S8）+ 规划端 oracle 提交 `35e7f05`，后者含 `tests/test_stream_ceilings_oracle.py`（17 个用例：四个常量与两个签名、源码里不再有 16 MiB 字面量；单行超过协议上限时默认 `invalid`、给了 `max_line_bytes` 后读出且翻译结果与短对话相同、8 MiB 含边界、行长不含换行、两道上限互相独立、深度仍是 64；16 MiB 与 64 MiB 之间的流可读、恰好 64 MiB 读完、多一字节 `too_large`、`changed` 先于 `too_large`、默认值不变；`max_line_bytes` 的校验在碰文件之前、顺序为 `max_bytes` → `max_line_bytes` → `expected`；`run_adapter_operation` 在移交之前拒绝、顺序在 `max_stream_bytes` 之后、`supervised_spec` 与预约检查之前；`reference` 读超过 1 MiB 的一行与超过 16 MiB 的流，进程内与子进程入口）；同一提交把 `tests/test_adapter_run_oracle.py` 的越界值与 `tests/test_pi_events_captured.py` 的措辞改成用常量。在基线上 oracle 在导入时失败（`DEFAULT_MAX_LINE_BYTES`、`MAX_LINE_BYTES` 还不存在）。
- `sentinelmesh/adapter_run.py`：`from .core import ProtocolError, strict_json_loads`；`__all__ = ["DEFAULT_MAX_STREAM_BYTES", "MAX_STREAM_BYTES", "STREAM_NAME", "read_stream", "run_adapter_operation", "supervised_spec"]`；`DEFAULT_MAX_STREAM_BYTES = 1_048_576`、`MAX_STREAM_BYTES = 16 * 1_048_576`；`_check_max_bytes(max_bytes)` 要求 `type(max_bytes) is int` 且 `1 <= max_bytes <= MAX_STREAM_BYTES`，否则 `AdapterError("E_ADAPTER_SCHEMA", "max_stream_bytes must be an integer in 1..%d" % MAX_STREAM_BYTES)`；`read_stream(evidence_dir, *, max_bytes=DEFAULT_MAX_STREAM_BYTES, expected=None)` 开头依次 `_check_max_bytes(max_bytes)`、`expected` 非 None 时 `_checked_expected(expected)`，然后才拼路径、`lstat`/`open`；读完 `return _parse_stream(raw)`；`_parse_stream(raw)` 按 LF 切分（末尾空段去掉），每段 `strict_json_loads(segment)`，`ProtocolError` → `{"status": "invalid", ..., "line", "code": exc.code}`；`_reduce(adapter, invocation, evidence_dir, max_bytes, expected=None)` 调 `read_stream(evidence_dir, max_bytes=max_bytes, expected=expected)`，在 `run_adapter_operation` 里被调两次（`before_finish` 里带 `expected=fact`，末尾不带）；`run_adapter_operation` 的关键字参数以 `max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES` 结尾，函数体开头依次检查 invocation 类型、adapter、adapter id、`_check_dimensions`、`_check_max_bytes(max_stream_bytes)`、`supervised_spec(...)`、`_check_reserved_dimensions(...)`。
- `sentinelmesh/core.py`：`DEFAULT_MAX_JSON_BYTES = 1_048_576`、`DEFAULT_MAX_JSON_DEPTH = 64`；`strict_json_loads(raw, *, max_bytes=DEFAULT_MAX_JSON_BYTES, max_depth=DEFAULT_MAX_JSON_DEPTH)`，超长报 `ProtocolError("E_JSON_LIMIT")`。
- `sentinelmesh/ledger_cli.py`：`from .adapter_run import MAX_STREAM_BYTES, read_stream`；`cmd_reference` 里 `stream = read_stream(run["evidence_dir"], max_bytes=MAX_STREAM_BYTES)`；`_stream_summary` 只报 `status`、`sha256`、`bytes`（`invalid` 再加 `line`、`code`）。
- `tests/test_adapter_run.py` 第 262 行：`for limit in (0, -1, True, 1.5, "64", MAX_STREAM_BYTES + 1, 16 * 1_048_576 * 2):`（所在类在 Windows 上整体跳过，但 32 MiB 在新上限内，这一行在 macOS 上会失败）。
- 翻译器 `pi_events.py` 不复制帧内容（`_message_end` 只数块、`_agent_end` 忽略），所以更长的一行只让更长的对话被翻译，规范化事件的 payload 不变大。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 17/17；全量 772 passed / 289 skipped；28 个故意制造的偏差全部被 oracle 抓住（整流上限 32/128 MiB、单行上限 4/16 MiB、两个默认值改成最大值、接受 0、接受 bool、拒绝恰好 8 MiB、错误码或消息改掉、`read_stream` 不校验或顺序颠倒、解析器忽略参数或永远用最大值、上限变成开区间、放宽深度、两个签名的默认值改成最大值、`run_adapter_operation` 不校验或顺序错到规格之后、预约之后、`__all__` 少一个、`reference` 不传行上限或仍用 16 MiB、测试里的旧字面量回来）。
- 工具链：Windows Server 2025，`python` 3.12.10（系统安装，没有 venv）与 pytest 9.1.1；基线上 `python -m pytest -q` 因 oracle 失败而失败，其余通过（原生 POSIX 用例在 Windows 上跳过，这是预期；`tests/test_pi_events_captured.py` 在没有 `.agent/runs` 的工作副本里跳过）。

## Scope
- allowed_modified_files:
  - sentinelmesh/adapter_run.py
  - sentinelmesh/ledger_cli.py
  - tests/test_adapter_run.py
- allowed_new_files:
  - tests/test_stream_ceilings.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_stream_ceilings_oracle.py`, `tests/test_adapter_run_oracle.py`, `tests/test_adapter_run_gaps.py`, `tests/test_ledger_reference.py`, `tests/test_ledger_reference_oracle.py`, `tests/test_pi_events.py`, `tests/test_pi_events_oracle.py`, `tests/test_pi_events_captured.py`, `tests/test_ledger_cli.py`, `tests/test_ledger_cli_oracle.py`, `sentinelmesh/core.py`, `sentinelmesh/protocols.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/cli.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有“是 int”都是精确类型（bool 不算）。私有名字可自定，公开名字不可。

1. **常量**（`adapter_run.py`）：`MAX_STREAM_BYTES = 64 * 1_048_576`；新增 `DEFAULT_MAX_LINE_BYTES`，等于 `core.DEFAULT_MAX_JSON_BYTES`（从 `.core` 导入，值 1_048_576）；新增 `MAX_LINE_BYTES = 8 * 1_048_576`；`DEFAULT_MAX_STREAM_BYTES` 不变；`__all__` 加入 `"DEFAULT_MAX_LINE_BYTES"` 与 `"MAX_LINE_BYTES"`，原有名字保留。`adapter_run.py`、`ledger_cli.py`、`tests/test_adapter_run.py` 里不再出现 `16 * 1_048_576` 或 `16777216`。
2. **`read_stream` 的签名与校验**：`read_stream(evidence_dir, *, max_bytes=DEFAULT_MAX_STREAM_BYTES, expected=None, max_line_bytes=DEFAULT_MAX_LINE_BYTES)`——参数顺序恰好如此，三个都是仅关键字。在碰文件之前依次校验 `max_bytes`（原样）、`max_line_bytes`、`expected`（原样）。`max_line_bytes` 必须是 int 且 `1 <= max_line_bytes <= MAX_LINE_BYTES`（含两端），否则 `AdapterError("E_ADAPTER_SCHEMA", "max_line_bytes must be an integer in 1..%d" % MAX_LINE_BYTES)`（消息里必须含 `max_line_bytes`）。两道上限互相独立：`max_line_bytes` 可以大于 `max_bytes`。
3. **解析**：每一行（按 LF 切分后的段，不含换行）用 `strict_json_loads(segment, max_bytes=max_line_bytes)` 解析，深度仍是解析器的默认；超长的行与以前一样是 `invalid`，`line` 是它的 1 起行号、`code` 是 `E_JSON_LIMIT`；其他答案（`missing`、`unreadable`、`changed`、`too_large`、`ok`）的条件与形状不变。
4. **`run_adapter_operation`**：新增仅关键字参数 `max_line_bytes: int = DEFAULT_MAX_LINE_BYTES`，位于 `max_stream_bytes` 之后；在 `_check_max_bytes(max_stream_bytes)` 之后、`supervised_spec(...)` 之前用与第 2 条相同的规则校验（所以 `max_stream_bytes` 与 `max_line_bytes` 都坏时报 `max_stream_bytes`；`max_line_bytes` 坏而 `timeout_seconds` 或用量维度的预约也坏时报 `max_line_bytes`；拒绝发生在移交之前：Scheduler 不变、操作仍 RESERVED、证据目录为空）。两处 `_reduce` 读流都传 `max_line_bytes`。
5. **`cmd_reference`**（`ledger_cli.py`）：`read_stream(run["evidence_dir"], max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES)`；输出形状不变。
6. **`tests/test_adapter_run.py`**：只把第 262 行的 `16 * 1_048_576 * 2` 改成 `MAX_STREAM_BYTES * 2`，其余不动。
7. **兼容性**：不改任何其他公开签名；`adapter_run` 与 `ledger_cli` 导入无副作用（V07）。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 2、3、5、6 节、oracle，以及 `adapter_run.py`（常量、`_check_max_bytes`、`read_stream`、`_parse_stream`、`_reduce`、`run_adapter_operation` 的开头与两处 `_reduce`）、`ledger_cli.py` 的 `cmd_reference`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–5 条改 `adapter_run.py` 与 `ledger_cli.py`（合计约 50 行）。
3. 按第 6 条改 `tests/test_adapter_run.py` 的一行。
4. 新建 `tests/test_stream_ceilings.py`，只覆盖下面这些（oracle 已经覆盖常量与签名、单行规则的各个边界、整流规则、检查顺序、`reference` 与入口；**可以沿用 oracle 的辅助函数写法，不必重复 oracle 已有的用例**；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言；流放在临时目录里、场景尽量小）：
   - 一行恰好 N 字节（N 取 1500 左右）：`max_line_bytes=N` 读出，`N - 1` 时 `invalid`、`line` 是它的行号、`code` 为 `E_JSON_LIMIT`；几行里第一条超长的行报自己的行号；
   - `max_line_bytes` 大于 `max_bytes` 合法，整流上限仍由 `max_bytes` 决定（`len(raw)` 读出、`len(raw) - 1` 是 `too_large`）；
   - `max_line_bytes` 的拒绝表（`0`、`-1`、`True`、`1.5`、`"8"`、`MAX_LINE_BYTES + 1`）：`E_ADAPTER_SCHEMA`、消息含 `max_line_bytes`，用不存在的证据目录证明拒绝发生在碰文件之前（合法值在同一目录上得到 `missing`）；
   - `run_adapter_operation` 以 `max_line_bytes=0` 在移交之前被拒绝（照 `tests/test_adapter_run_gaps.py` 的写法：精确的 `Invocation`、`OfflineAdapter()`、Scheduler 的 `events` 不变、操作仍 `RESERVED`、证据目录为空），并且 `max_stream_bytes=0` 同时给出时消息报的是 `max_stream_bytes`；
   - `smctl ledger reference`：一个崩溃后的运行（照 oracle 的 `crashed` 写法建账本），`agent_end` 一行 `DEFAULT_MAX_LINE_BYTES + 1` 字节时 `stream.status` 为 `ok`、`reference.complete` 为 True、`reference.usage` 等于那条助手消息的用量；把文件截断到 `MAX_STREAM_BYTES + 1` 字节后 `stream` 为 `too_large`、`reference` 为 None。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
5. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_stream_ceilings_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_stream_ceilings.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_adapter_run.py tests/test_adapter_run_gaps.py tests/test_adapter_run_oracle.py tests/test_ledger_reference.py tests/test_ledger_reference_oracle.py tests/test_pi_events.py tests/test_pi_events_oracle.py tests/test_pi_events_captured.py tests/test_verified_review.py tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.adapter_run, sentinelmesh.ledger_cli; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要，基线应失败）：`python -c "from sentinelmesh.adapter_run import DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES, MAX_LINE_BYTES, MAX_STREAM_BYTES; print(MAX_STREAM_BYTES, MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES, DEFAULT_MAX_LINE_BYTES)"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`67108864 8388608 1048576 1048576`

V01、V04 在基线上失败，是因为两个新常量还不存在（oracle 导入失败）；V02 是因为测试文件还不存在；V08 是因为导入失败。V03 保护 `read_stream` 的其他答案、`reference`、翻译器与 CLI 的全部现有规则；V07 保护导入无副作用。

## Definition of done
- D01：常量与 `__all__`、旧字面量清除（Behavior contract 第 1 条）；证据：V01、V08。
- D02：`read_stream` 的签名、校验顺序与错误（第 2 条）；证据：V01、V02。
- D03：按行上限解析、其他答案不变（第 3 条）；证据：V01、V02、V03。
- D04：`run_adapter_operation` 的参数、校验位置与两处传参（第 4 条）；证据：V01、V02、review。
- D05：`cmd_reference` 传两个最大值（第 5 条）与 `tests/test_adapter_run.py` 的一行（第 6 条）；证据：V01、V03。
- D06：兼容性与导入无副作用（第 7 条）；Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V04、V05、V06、V07、scope。

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
  - 阅读约 3 分钟；
  - 实现约 4 分钟（两处改动合计约 50 行，外加测试里的一行）；
  - 测试约 5 分钟（5 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 6 秒，V03 约 20 秒，全量约 110 秒）；
  - 执行报告约 2 分钟。

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
