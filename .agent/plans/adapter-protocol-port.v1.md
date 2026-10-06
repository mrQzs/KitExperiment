# TASK: adapter-protocol-port

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 67f216b31510a87627e9d814559145bebb924396
- risk_level: low
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
新建 `sentinelmesh/adapter_protocol.py`：把 Code-Kit 的 adapter 边界（`adapters/protocol.py` 与 `adapters/offline.py`）逐字移植过来，只改 import；并让规划端的两份 oracle 全部通过。

## Non-goals
- 不修改 `sentinelmesh/adapter.py` 或其他已有模块，不接入监督器或调度器。
- 不移植 Code-Kit 的 `capability.py`、`heterogeneous.py`、`second_cli.py`。
- 不修改合同、`.agent/`（包括冻结的参考）、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml` 与 oracle；不联网、不读取凭据。

## Repository facts
- 基线 `67f216b` 由下列提交依次组成：
  - master `fc909da`：冻结的参考与设计说明 `.agent/design/adapter-supervisor-wiring.md`；
  - 规划端 oracle：`tests/test_adapter_protocol_reference_oracle.py` 与 `tests/test_adapter_protocol_differential_oracle.py`。
- 冻结的参考在 `.agent/design/adapter-reference/`：
  - `protocol.py.txt`、`offline.py.txt`：要移植的源码；
  - `test_adapters.py.txt`：Code-Kit 的测试；
  - `manifest.json`：来源提交与 SHA-256。
- 参考依赖的两个模块，在本仓库有行为相同的版本（规划端已逐字比较）：
  - `sentinelmesh.protocols` 的 `DEFAULT_MAX_BYTES`、`MAX_SAFE_INTEGER`、`ProtocolError`、`check_json_value`；
  - `sentinelmesh.runtime` 的 `RuntimeErrorCode`、`validate_argv`。
- 两份 oracle：
  - 参考 oracle：Code-Kit 的 18 个测试，只把 import 从 `codekit.adapters` 改为 `sentinelmesh.adapter_protocol`；
  - 差分 oracle：加载冻结的参考（按 manifest 核对哈希，只改写包内相对 import），与 `sentinelmesh.adapter_protocol` 逐项比较返回值，以及 `AdapterError` 的 code、seq 与消息。
- 规划端用一份草稿移植验证过（草稿未提交）：
  - 两份 oracle 共 27 个用例全部通过；
  - 8 个故意制造的偏差（改消息、放宽 C1 控制字符、改摘要的序列化方式、取第一条 usage、不复制 payload、接受 dict 子类、去掉大小上限、未知事件不判为不完整）各自都被抓住。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files: 无
- allowed_new_files:
  - sentinelmesh/adapter_protocol.py
  - tests/test_adapter_protocol.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter.py`, `sentinelmesh/protocols.py`, `sentinelmesh/runtime.py`, `tests/test_adapter_protocol_reference_oracle.py`, `tests/test_adapter_protocol_differential_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **逐字移植**：`sentinelmesh/adapter_protocol.py` 的内容依次是：
   - `protocol.py.txt` 的全文；
   - `offline.py.txt` 中的 `OfflineAdapter` 类。

   只允许下列改动：
   - `from ..protocols import …` 改为 `from .protocols import …`，`from ..runtime import …` 改为 `from .runtime import …`；
   - 去掉 `offline.py.txt` 中的 `from .protocol import …` 与它的模块文档字符串、重复的 `from __future__`/`typing` import（这些名字在同一个模块里已经有了）；
   - 模块文档字符串可以改写为说明这是移植；
   - 两个 `__all__` 合成一个，恰好列出：`AdapterDescriptor`、`AdapterError`、`AdapterRegistry`、`AdapterResult`、`EVENT_TYPES`、`Invocation`、`NormalizedEvent`、`OfflineAdapter`、`normalize_events`。

   除此之外不做任何改动：类名、函数名、签名、默认值、检查及其顺序、错误码、错误消息、注释都保持原样。
2. **导入无副作用**：导入 `sentinelmesh.adapter_protocol` 不导入任何 `codekit` 模块，也不导入 ctypes；只依赖标准库与 `sentinelmesh.protocols`、`sentinelmesh.runtime`。

## Steps
1. 读 AGENTS.md、本合同、两份 oracle、`.agent/design/adapter-reference/` 下的参考文件，以及 `sentinelmesh/protocols.py` 与 `sentinelmesh/runtime.py` 中被引用的名字。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1 条新建 `sentinelmesh/adapter_protocol.py`。从参考文件逐字复制，不要凭记忆重写。
3. 新建 `tests/test_adapter_protocol.py`，覆盖下列用例：
   - 用新的解释器进程（`subprocess` 运行 `sys.executable -c …`）导入 `sentinelmesh.adapter_protocol`，断言 `sys.modules` 中既没有 `ctypes`，也没有名为 `codekit` 或以 `codekit.` 开头的模块；
   - `__all__` 恰好等于第 1 条列出的九个名字。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_adapter_protocol_reference_oracle.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_adapter_protocol_differential_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要，基线应失败）：`python -m pytest -q tests/test_adapter_protocol.py` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.adapter_protocol; assert 'ctypes' not in sys.modules; assert not any(n == 'codekit' or n.startswith('codekit.') for n in sys.modules); print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V02、V04、V07 在基线上失败，是因为模块还不存在；V03 在基线上失败，是因为测试文件还不存在。

## Definition of done
- D01：逐字移植（Behavior contract 第 1 条）；证据：V01、V02、review。
- D02：导入无副作用（第 2 条）；证据：V03、V07。
- D03：Step 3 的测试齐全，已有测试不退化，不自行做变异检查；证据：V03、V04、review。
- D04：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、冻结的参考或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1200
- max_wall_seconds_total: 2400
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 3 分钟；
  - 移植约 3 分钟；
  - 测试约 3 分钟；
  - 运行检查约 3 分钟（两份 oracle 约 3 秒，全量约 105 秒）；
  - 执行报告约 3 分钟；
  - 合计约 15 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D04，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、冻结的参考、已有模块与测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 Code-Kit 仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
