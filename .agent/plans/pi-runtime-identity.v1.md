# TASK: pi-runtime-identity

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 5f43cdf32ba029c3bc045871d8b753c024159366
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch (planner will cherry-pick only after independent review)
- executor: bb/step-5-preview · thinking high

## Goal
由 Pi `step-5-preview` high 将 Code-Kit 已验证的纯运行时身份/生命周期契约移植到 KitExperiment，保持现有运行 API 兼容并建立可独立复核的离线证据。

## Non-goals
- 不实现进程启动/终止的原生平台适配、RuntimeLease、持久化或 adapter 改造；这些另有合同。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、设计材料或 pyproject.toml。
- 不联网、不读取凭据、不启动真实 Pi/Claude/provider，不添加依赖。

## Scope
- allowed_modified_files: `sentinelmesh/runtime.py`
- allowed_new_files: `tests/test_runtime_identity_contract.py`
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter.py`
- dependency_changes: forbidden

## Repository facts
- 当前 `sentinelmesh/runtime.py` 已有 `Invocation`、`ProcessResult`、`SubprocessRunner`，KitExperiment 全量基线为 65 passed。
- Code-Kit 对应实现位于 `scripts/codekit/runtime.py`，它是本合同的参考 API 和语义来源；Pi 需阅读并移植兼容语义，不要复制与 KitExperiment 不相容的依赖。
- 当前平台为 Windows；本合同只验纯函数和离线 fixture，不宣称跨平台 native cleanup 已验证。

## Behavior contract
- 新增 `validate_argv`、`InvocationSpec`、`ProcessIdentity`、`Lifecycle`、`classify_start`、`reconcile`，公共字段和错误语义应与 Code-Kit runtime 对齐，且不删除/重命名现有类。
- InvocationSpec 至少绑定 request_id、run_id、command、cwd、environment_sha256、timeout_seconds、shell；ProcessIdentity 至少绑定 pid、boot_id、executable_sha256、worktree_id；Lifecycle 必须有严格状态转移、generation fence 和 identity fence。
- START_UNKNOWN、IDENTITY_MISMATCH、NOT_STARTED、FAILED、STOPPED 必须可区分；reconcile 默认保守，只有明确“未启动”或终止身份/cleanup 完整证据时才允许 replay。
- 非法 argv、shell=true、空/NUL/control 字符、错误 SHA、缺 identity、代际不符必须 fail closed；现有 SubprocessRunner 行为不扩大权限。
- 测试必须覆盖正例与最小反例，不把模型文本、退出码或 synthetic 记录当作 runtime 事实。

## Steps
1. 阅读 AGENTS.md、`sentinelmesh/runtime.py` 和 Code-Kit 参考实现，锁定兼容 API。
2. 只在 scope 内实现新纯契约和测试，保持旧 65 项测试通过。
3. 运行 V01-V05，报告每项 D01-D04、实际命令和未运行平台能力。

## Validation commands
- V01（基线应失败）：`python -c "from pathlib import Path; p=Path(r'tests/test_runtime_identity_contract.py'); assert p.exists() and p.stat().st_size>0; print('PI_RUNTIME_IDENTITY_ARTIFACT_OK')"`
- V02（必要）：`python -m pytest -q`
- V03（必要）：`python -m compileall -q sentinelmesh tests`
- V04（必要）：`git diff --check`
- V05（必要，基线应失败）：`python -c "from sentinelmesh.runtime import InvocationSpec, ProcessIdentity, Lifecycle, classify_start, reconcile, validate_argv; print('PI_RUNTIME_IDENTITY_API_OK')"`

## Definition of done
- D01：Code-Kit runtime 纯契约移植；证据 V02、V05。
- D02：START_UNKNOWN/replay 和旧 API 兼容；证据 V02。
- D03：反例和 identity/generation fence 测试；证据 V02、V03。
- D04：无外部副作用，平台边界明确；证据 V02、V04。

## Stop conditions
- 需要越界修改、依赖、凭据、联网或真实 provider。
- 破坏现有公开 API/测试，或只能削弱测试来通过。

## Report back
最后报告 D01-D04、命令退出码、变更文件、未运行的 Windows/macOS/native 能力。

## Budget
- max_attempts_total: 1
- max_wall_seconds_per_attempt: 900
- max_wall_seconds_total: 900
- cost_budget: tracking-only

## Forbidden
- 不修改合同、AGENTS.md、`.agent/`、设计材料。
- 不删除、跳过或弱化既有测试；不提交、不推送、不合并、不部署。

