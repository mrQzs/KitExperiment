# TASK: pi-runtime

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: a297b6ebe08410ade88e5fc4fe09fff489cbf064
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch (planner will cherry-pick only after independent review)
- executor: bb/step-5-preview · thinking high

## Goal
由 Pi `step-5-preview` high 完成 SentinelMesh 运行时的身份、生命周期、代际 fencing 和租约契约，补齐 Code-Kit 对应的 runtime/runtime_lease 能力，同时保持当前公开 API 兼容。

## Non-goals
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、设计材料、pyproject.toml。
- 不添加第三方依赖，不联网，不读取凭据，不启动真实 Pi 或 Claude CLI。
- 不宣称真实 Windows/macOS 原生能力已经验证；未能证明的能力必须保持 PARTIAL/UNKNOWN 或 NOT_RUN/BLOCKED。

## Scope
- allowed_modified_files: `sentinelmesh/runtime.py`, `sentinelmesh/adapter.py`
- allowed_new_files: `sentinelmesh/runtime_lease.py`, `tests/test_runtime_contract.py`
- allowed_deleted_files: none
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`
- dependency_changes: forbidden

## Repository facts
- 当前运行时在 `sentinelmesh/runtime.py`，已有 `Invocation`、`ProcessResult`、`SubprocessRunner`；`sentinelmesh/adapter.py` 已有 Pi/Claude descriptor、build_invocation 和 normalize。
- 当前基线为 65 项离线测试通过，现有公开 API 和测试必须保持通过。
- 运行环境是本地 Windows；生产供应商和跨平台 native 能力不可在本合同中伪造。

## Behavior contract
- 增加兼容的新 API：`InvocationSpec`（request_id/run_id/environment_sha256/worktree_id/argv 等）、`ProcessIdentity`（pid、启动标识、executable_sha256、worktree_id）、`Lifecycle`、`validate_argv`、`classify_start`、`reconcile`；具体字段可与现有模型兼容，但必须严格拒绝空值、NUL、shell 拼接和非法代际。
- START_UNKNOWN 必须是独立结果，禁止在没有 reconcile 证据时自动重发；identity mismatch、generation stale、STOPPING/FAILED/terminal 状态必须 fail closed。
- 增加 `runtime_lease.py` 的 `RuntimeLease`、`issue_lease`、`assess_lease`、`release_lease`；租约和 generation 操作必须可重复判断，不能凭 exit code 或模型文本提升状态。
- 保持 `SubprocessRunner` 的兼容性和证据边界：argv 不得通过 shell 解释；超时/取消 stop_reason 粘性；POSIX 进程组和 Windows 可用的原生边界要显式建模；不能证明的清理返回 `PARTIAL`/`UNKNOWN`，不得声称 PASS。
- PiAdapter/ClaudeCodeAdapter 的能力探测必须只读、显式 source，缺能力时拒绝构造 invocation；不得把 synthetic descriptor 当生产证据。
- 新测试只使用标准库和本地临时 Python 子进程，覆盖身份不匹配、START_UNKNOWN 禁止重发、STOPPING/FAILED 不得替换 identity、generation fence、terminal cleanup 后只允许真实未启动或完整清理的 replay，以及旧 API 回归。

## Steps
1. 阅读 AGENTS.md、现有 runtime/adapter 实现和测试，确认兼容边界。
2. 在允许范围内实现新 API 和严格验证，必要时增加显式兼容层；不要重命名或删除旧 API。
3. 编写确定性的离线测试，测试结果必须区分 START_UNKNOWN、PARTIAL、UNKNOWN、BLOCKED。
4. 运行全部 validation commands，保留真实结果；不执行真实供应商和生产连接器。
5. 最后一条消息逐项报告 D01-D04、真实命令结果、变更文件、未运行平台边界。

## Validation commands
- V01（基线应失败）：`python -c "from pathlib import Path; p=Path(r'tests/test_runtime_contract.py'); assert p.exists() and p.stat().st_size>0; print('PI_RUNTIME_ARTIFACT_OK')"`
- V02（必要）：`python -m pytest -q`
- V03（必要）：`python -m compileall -q sentinelmesh tests`
- V04（必要）：`git diff --check`
- V05（必要，基线应失败）：`python -c "from sentinelmesh.runtime import InvocationSpec, ProcessIdentity, Lifecycle, classify_start, reconcile, validate_argv; from sentinelmesh.runtime_lease import RuntimeLease, issue_lease, assess_lease, release_lease; print('PI_RUNTIME_API_OK')"`

## Definition of done
- D01：运行身份、生命周期和 START_UNKNOWN 对账 API，兼容旧 API；证据 V02、V05。
- D02：RuntimeLease、generation fencing、adapter fail-closed 探测；证据 V02、V05。
- D03：进程终止与清理边界可证明，未知能力不伪造 PASS；证据 V02、V03、V04。
- D04：无依赖、无凭据/联网/真实 CLI，范围和证据完整；证据 V02、V04。

## Stop conditions
- 需要修改 scope 外文件、安装依赖、访问凭据、联网或启动真实供应商。
- 只能用合成文字把 UNKNOWN/PARTIAL/START_UNKNOWN 伪造成 PASS。
- 破坏既有公开 API 或现有测试。

## Report back
最后报告每项 D01-D04、命令退出码和输出摘要、证据路径，以及未运行的 Windows/macOS/生产连接器能力。

## Budget
- max_attempts_total: 1
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 1800
- cost_budget: tracking-only

## Forbidden
- 不修改合同、AGENTS.md、`.agent/`、设计材料。
- 不删除、跳过或弱化既有测试；不提交、不推送、不合并、不部署。

