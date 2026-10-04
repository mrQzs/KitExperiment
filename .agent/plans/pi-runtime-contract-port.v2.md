# TASK: pi-runtime-contract-port

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: 89db55bb9eadcf955a0f8ecb67609548ff1d93a2
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
把 `sentinelmesh/runtime.py` 的 canonical 层改成冻结参考 `.agent/design/runtime-reference/runtime.py.txt`（Code-Kit `scripts/codekit/runtime.py`）的逐项移植：三个规划端 oracle 测试文件全部通过，旧 `Invocation/ProcessResult/StaticAdapter/SubprocessRunner/RuntimeErrorSM` 行为不变，`tests/test_runtime_contract_port.py` 改为断言参考语义。

## Non-goals
- 不实现 RuntimeLease、原生进程清理、持久化或 adapter 接线；不修改旧 API 的行为与错误码。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、`sentinelmesh/adapter.py` 和三个 oracle 测试文件；不联网、不读取凭据、不启动真实 CLI。

## Repository facts
- 基线 `89db55b` = master `a14079f` + attempt-001 的候选（提交 `a84fbb1`）+ 规划端 oracle（`4585eee`、`89db55b`）。返工从候选继续，attempt-001 的复审见 `.agent/reviews/pi-runtime-contract-port/attempt-001.md`。
- 唯一的规格来源是 worktree 内的冻结参考 `.agent/design/runtime-reference/runtime.py.txt`（`manifest.json` 记录了 CRLF→LF 后的 SHA-256）。只读，不要复制到别处，也不要修改。
- `sentinelmesh/runtime.py` 现状：文件顶部是 attempt-001 加的 canonical 层，依次为 `RuntimeErrorCode`、`RuntimeErrorSM`（它的子类）、`LIFECYCLE`/`ALLOWED_TRANSITIONS`、`STATUS_*` 常量、`_opaque_text`、`validate_argv`、`InvocationSpec`、`ProcessIdentity`、`Lifecycle`、`_identity_bound`、`classify_start`、`reconcile`；其后是旧 API，attempt-001 没有改动。
- 三个 oracle 都在 `tests/`：`test_runtime_reference_oracle.py` 是参考测试的逐字移植；`test_runtime_fail_closed_oracle.py` 补了参考测试没有覆盖的输入；`test_runtime_differential_oracle.py` 加载冻结参考，在固定输入网格上逐项比较返回值或 `RuntimeErrorCode.code`。三者在参考实现上全部通过，在基线上大量失败。
- 工具链：macOS 主机上 PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；`pyproject.toml` 要求 Python ≥3.10。
- 基线检查（规划端已在基线上运行）：V01 失败（oracle 失败），V02 因同一原因失败，V03、V04、V05 通过。
- 草稿验证（规划端在临时副本上做过，已删除）：按 Behavior contract 第 1–3 条换上参考的 canonical 层后，三个 oracle 全部通过，旧 API 测试全部通过。全量测试只剩 `tests/test_runtime_contract_port.py` 中下列 20 个测试函数失败，它们就是第 5 条要改写的范围：
  - `ValidateArgvTests`：test_shell_carrier_rejected、test_nul_rejected_in_every_slot、test_empty_executable_rejected、test_empty_argv_rejected、test_carriage_return_or_line_feed_in_executable_rejected；
  - `InvocationSpecTests`：test_shell_carrier_and_command_errors_rejected、test_construction_validates_immediately_and_normalises_command；
  - `ProcessIdentityTests`：test_four_field_identity_is_constructed_and_validated；
  - `LifecycleTests`：test_identity_cannot_be_bound_after_start_unknown_without_fence、test_record_construction_is_validated、test_stale_generation_fence_rejected、test_start_unknown_recovery_requires_explicit_generation_fence；
  - `ClassifyStartTests`：test_model_text_and_exit_codes_are_never_consulted、test_no_observation_depends_only_on_handoff、test_signature_and_input_types、test_started_observation_requires_bound_process_identity；
  - `ReconcileTests`：test_empty_observation_record_has_no_start_evidence、test_input_types、test_observed_environment_and_run_fields、test_replay_allowed_only_for_clear_not_started。

## Scope
- allowed_modified_files:
  - sentinelmesh/runtime.py
  - tests/test_runtime_contract_port.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter.py`, `tests/test_runtime_reference_oracle.py`, `tests/test_runtime_fail_closed_oracle.py`, `tests/test_runtime_differential_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **canonical 层与参考逐项一致**：`validate_argv`、`InvocationSpec`（含 `digest()`）、`ProcessIdentity`、`LIFECYCLE`、`ALLOWED_TRANSITIONS`、`Lifecycle`（含 `transition`）、`classify_start`、`reconcile` 对任意输入的返回值、抛出的 `RuntimeErrorCode` 及其 `code` 都与冻结参考相同，包括检查顺序决定的错误码。做法已经确定：把参考中的这些定义及其私有辅助（`_MAX_SAFE_INTEGER`、`_runtime_text`、`_observation_identity`）照搬进 `runtime.py`，替换 attempt-001 的对应实现。
2. **错误类保持 attempt-001 的写法**：`RuntimeErrorCode` 与 `RuntimeErrorSM` 两个类原样保留（`message` 可选，`str()` 为 `CODE: message`，单参数的 `RuntimeErrorSM("E_…")` 文本不变）。canonical 层抛出时一律传入 code 与 message。
3. **删除失去调用方的私有辅助**：`_identity_bound`、`_opaque_text`、`_CONTROL_RE`、`_is_surrogate`、`_TERMINAL_OBSERVED`，以及只被 canonical 层使用的导入 `from .core import SHA256_RE, canonical_json, sha256_bytes`，在改动后没有调用方，删除（旧 API 从 `class Invocation` 开始，不使用它们）。参考没有的公开名称 `STATUS_*`、`InvocationSpec.validate()`、`ProcessIdentity.to_dict()` 可以保留，但不得改变上面第 1 条的任何行为。
4. **旧 API 不变**：`Invocation`、`ProcessResult`、`StaticAdapter`、`SubprocessRunner` 及其错误码与文本保持基线行为，既有测试照常通过。
5. **测试文件改为参考语义**：`tests/test_runtime_contract_port.py` 中断言了与参考相反行为的用例，就地改为对同一输入断言参考的结果，不删除测试函数，也不放宽与之无关的断言。至少包括：
   - `{}`/None 的观察可以重放；
   - `{}` 当作“没有观察”；
   - 六键 identity 判为 STARTED；
   - 不带 identity 进入 STARTED；
   - `handoff=False` 时 FAILED 保持 FAILED；
   - 非字符串的 observed_environment_sha256 被转成 None；
   - 错误码 `E_ARGV_*`、`E_SPEC_*`、`E_IDENTITY_*`、`E_LIFECYCLE_*`、`E_EXPECTED_TYPE`、`E_HANDOFF_TYPE`、`E_OBSERVATION_TYPE`。
   oracle 已覆盖的输入可以重复断言，但不要把 oracle 的内容复制进来凑数。

## Steps
1. 读 AGENTS.md、本合同、冻结参考 `.agent/design/runtime-reference/runtime.py.txt`、三个 oracle 测试文件，以及当前的 `sentinelmesh/runtime.py` 与 `tests/test_runtime_contract_port.py`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条替换 canonical 层，旧 API 一行不改。
3. 按第 5 条改写 `tests/test_runtime_contract_port.py`。
4. 运行 V01–V05。oracle 失败时以参考为准修改实现，不要改 oracle。
5. 写执行报告。遇到合同没有定义、参考也无法回答的行为，停止并在 `unresolved_questions` 中说明，不要自行选择。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_runtime_reference_oracle.py tests/test_runtime_fail_closed_oracle.py tests/test_runtime_differential_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V03（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V04（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V05（必要）：`python -m pytest -q tests/test_runtime_contract_port.py tests/test_reference.py tests/test_hardening.py` · cwd：仓库根 · 超时：300s · 期望退出码：0

## Definition of done
- D01：canonical 层与冻结参考逐项一致（Behavior contract 第 1–3 条）；证据：V01。
- D02：旧 API 与全量测试不退化（第 4 条）；证据：V02、V05。
- D03：`tests/test_runtime_contract_port.py` 断言参考语义，不再固定失败开放的行为（第 5 条）；证据：V05、review。
- D04：所有修改都在允许集合内，oracle 与参考未被改动；证据：scope。
- D05：无依赖变更、无联网与凭据访问，代码可编译、无空白错误；证据：V03、V04、scope。

## Stop conditions
- 基线、冻结参考或 oracle 与 Repository facts 不符。
- 必须修改允许范围之外的文件（包括 oracle）才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 冻结参考与本合同对同一行为说法不同，或出现两者都没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总：attempt-001 已用 1 次，本版本为用户追加的 1 次返工）
- max_wall_seconds_per_attempt: 1500
- max_wall_seconds_total: 2400
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：阅读参考与 oracle 约 5 分钟；替换 canonical 层约 5 分钟；改写测试约 8 分钟；运行检查约 2 分钟；执行报告约 3 分钟；合计约 23 分钟

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、改写过的测试函数清单、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、oracle 测试、冻结参考、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
