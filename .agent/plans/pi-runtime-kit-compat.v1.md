# TASK: pi-runtime-kit-compat

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 9c97134396cce23e0e47694030b53d3c728e9d41
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch (planner will cherry-pick only after independent review)
- executor: bb/step-5-preview · thinking high

## Goal
由 Pi `step-5-preview` high 将 Code-Kit 的 runtime 纯身份/生命周期 API **按原构造器、签名、状态和返回形状逐项兼容**移植到 KitExperiment，保留现有旧运行时 API。

## Non-goals
- 不添加新的替代 API 设计，不改名或收紧 Code-Kit 参考字段。
- 不实现 RuntimeLease、原生 Job Object/POSIX cleanup、持久化或 adapter 改造；另有合同。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、设计材料或 pyproject.toml；不联网、不读取凭据、不启动真实 CLI。

## Scope
- allowed_modified_files: `sentinelmesh/runtime.py`
- allowed_new_files: `tests/test_runtime_identity_kit_contract.py`
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter.py`
- dependency_changes: forbidden

## Repository facts
- 当前基线没有上一候选的实现：上一 attempt 已 REVISE、未集成；当前全量基线为 65 passed。
- Code-Kit 参考文件是规划端只读输入：`D:\Code\Code-Kit\scripts\codekit\runtime.py`。逐项保留它的公开签名和构造字段。
- KitExperiment 现有 `Invocation`、`ProcessResult`、`SubprocessRunner` 必须保持兼容。

## Behavior contract
- `validate_argv(command, *, shell=False)` 的调用形状和 fail-closed 语义保持参考：shell carrier 拒绝；argv 必须非空；NUL 和 surrogate 拒绝；保持 Code-Kit 允许普通参数中的空字符串/换行，只拒绝 executable 的空值和 CR/LF。
- `InvocationSpec` 必须使用 Code-Kit 的字段 `request_id, run_id, command, cwd, environment_sha256, timeout_seconds, shell=False`；在 `__post_init__` 立即校验；提供 `.digest()`，不要以 `.validate()` 才校验替代。
- `ProcessIdentity` 构造器必须接受且只要求 `pid, boot_id, executable_sha256, worktree_id`，不能强制新增 request/run/creation 字段；在构造时拒绝坏值。
- `Lifecycle` 默认必须是 `state='NEW', generation=0, identity=None`，提供 `transition(state, *, identity=None, expected_generation=None)`；每次有效 transition generation+1，START_UNKNOWN 不是不可恢复 terminal，旧 identity 不得在 STOPPING/FAILED 中替换。
- `classify_start(*, handoff_accepted, started_observation, expected)` 与 `reconcile(expected, observed)` 参数签名必须和参考一致；返回状态/键保持可被参考调用方读取。未知启动不得被标成 NOT_STARTED，replay 仅允许明确 NOT_STARTED 或终止身份和 cleanup 完整证据。
- 所有新增测试必须直接调用这些参考形状；不得只测候选自定义 API。测试还必须证明普通参数的空尾参数与换行保持兼容。

## Steps
1. 阅读 AGENTS.md、当前 runtime.py 和 Code-Kit 参考实现，先写兼容测试。
2. 只在 scope 内实现精确兼容层，保持旧 65 项测试通过；不要复用或保留上一候选的替代接口。
3. 运行 V01-V05，报告每项 D01-D05 和平台未运行边界。

## Validation commands
- V01（基线应失败）：`python -c "from pathlib import Path; p=Path(r'tests/test_runtime_identity_kit_contract.py'); assert p.exists() and p.stat().st_size>0; print('PI_RUNTIME_KIT_ARTIFACT_OK')"`
- V02（必要）：`python -m pytest -q`
- V03（必要）：`python -m compileall -q sentinelmesh tests`
- V04（必要）：`git diff --check`
- V05（必要，基线应失败）：`python -c "import inspect; from sentinelmesh.runtime import InvocationSpec, ProcessIdentity, Lifecycle, classify_start, reconcile, validate_argv; assert list(inspect.signature(validate_argv).parameters)==['command','shell']; assert list(inspect.signature(classify_start).parameters)==['handoff_accepted','started_observation','expected']; assert list(inspect.signature(reconcile).parameters)==['expected','observed']; ProcessIdentity(1,'boot','a'*64,'wt'); print('PI_RUNTIME_KIT_API_OK')"`

## Definition of done
- D01：构造器、签名、状态和 digest 与 Code-Kit 对齐；证据 V02、V05。
- D02：START_UNKNOWN 对账和 replay gate 保守且可恢复；证据 V02、V05。
- D03：generation/identity/输入边界 fail closed，旧 API 回归；证据 V02-V04。
- D04：测试直接保护参考形状和最小反例；证据 V02。
- D05：无外部副作用，平台边界明确；证据 V02、V04。

## Stop conditions
- 需要越界、依赖、凭据、联网或真实 provider；或参考实现与本合同不一致。
- 只能删除/跳过测试或添加替代签名来通过。

## Report back
最后报告 D01-D05、实际命令、变更文件和未运行平台能力。

## Budget
- max_attempts_total: 1
- max_wall_seconds_per_attempt: 1200
- max_wall_seconds_total: 1200
- cost_budget: tracking-only

## Forbidden
- 不修改合同、AGENTS.md、`.agent/`、设计材料。
- 不删除、跳过或弱化既有测试；不提交、不推送、不合并、不部署。




