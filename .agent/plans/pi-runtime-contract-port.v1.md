# TASK: pi-runtime-contract-port

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 60f2d159f30a36f956f542f363e72cd98a879f96
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch (planner will cherry-pick only after independent review)
- executor: bb/step-5-preview · thinking high

## Goal
由 Pi `step-5-preview` high 逐项移植 Code-Kit runtime 的 canonical identity/lifecycle contract，保留 KitExperiment 旧 `Invocation/ProcessResult/SubprocessRunner` API。

## Non-goals
- 不发明 flattened observation 或替代返回值，不实现 RuntimeLease/native cleanup/persistence/adapter。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、设计材料、pyproject.toml；不联网、不读取凭据、不启动真实 CLI。

## Scope
- allowed_modified_files: `sentinelmesh/runtime.py`
- allowed_new_files: `tests/test_runtime_contract_port.py`
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter.py`
- dependency_changes: forbidden

## Canonical API (must be copied exactly)
Implement these public shapes in `sentinelmesh.runtime`:

1. `LIFECYCLE = ('NEW','STARTING','STARTED','STOPPING','STOPPED','FAILED','START_UNKNOWN')`.
   `ALLOWED_TRANSITIONS = {'NEW': {'STARTING','FAILED'}, 'STARTING': {'STARTED','START_UNKNOWN','FAILED'}, 'STARTED': {'STOPPING','FAILED'}, 'STOPPING': {'STOPPED','FAILED','START_UNKNOWN'}, 'STOPPED': set(), 'FAILED': set(), 'START_UNKNOWN': {'STARTED','STOPPING','FAILED'}}`.
2. `RuntimeErrorCode(ValueError)` has `code` and renders `CODE: message`; historical `RuntimeErrorSM` remains catchable and exposes the same `code`/message fields.
3. `validate_argv(command, *, shell=False)` rejects shell=true, non-list/tuple, empty argv, NUL, surrogate, and empty executable/CR/LF in argv[0]; ordinary argument empty strings and newlines are accepted.
4. Frozen `InvocationSpec(request_id, run_id, command, cwd, environment_sha256, timeout_seconds, shell=False)`: constructor validates immediately; command is canonical tuple; environment SHA is lowercase 64 hex; timeout is positive integer; `.digest()` returns canonical SHA-256.
5. Frozen `ProcessIdentity(pid, boot_id, executable_sha256, worktree_id)`: constructor validates positive pid, nonempty opaque strings, lowercase SHA-256 executable digest.
6. Frozen `Lifecycle(state='NEW', generation=0, identity=None)`: `transition(state, *, identity=None, expected_generation=None)` checks allowed transition, generation fence, START_UNKNOWN requires explicit fence, immutable identity binding, and returns a new record with generation+1. START_UNKNOWN is recoverable; it is not terminal.
7. `classify_start(*, handoff_accepted, started_observation, expected)` exact behavior: if no observation, `START_UNKNOWN` iff handoff accepted else `NOT_STARTED`; mismatched observation run/environment → `IDENTITY_MISMATCH`; observation state NOT_STARTED → NOT_STARTED (unless contradictory process identity then START_UNKNOWN); state STARTED requires process_identity and matching expected run/environment, otherwise START_UNKNOWN; FAILED/STOPPED pass through; no model text/exit code guessing.
8. `reconcile(expected: InvocationSpec, observed: Mapping)` returns exactly a mapping with `status`, `run_id`, `expected_environment_sha256`, `observed_environment_sha256`, `replay_allowed`. `status` comes from classify_start. `replay_allowed` is true only for status NOT_STARTED or for FAILED/STOPPED with matching process_identity and `cleanup_confirmed is True`; START_UNKNOWN, mismatch, running/partial cleanup are false.

## Repository facts
- 当前基线 65 项测试通过；上一候选均未集成，当前运行时只有旧 Invocation/ProcessResult/SubprocessRunner。
- 以上 Canonical API 是本合同的唯一参考，执行器不得读取 worktree 之外的路径来猜测接口。

## Steps
1. 先阅读 AGENTS.md、当前 runtime.py 和本合同 Canonical API。
2. 只在 scope 内实现兼容层和直接调用 canonical shapes 的测试；禁止保留上一候选的替代 reconcile/classify API。
3. 运行 V01-V05，逐项报告 D01-D05、命令结果和未运行平台能力。

## Behavior contract
- `LIFECYCLE` 与 `ALLOWED_TRANSITIONS` 必须逐字匹配 Canonical API；`START_UNKNOWN` 可恢复，不是终态。
- `InvocationSpec` 构造立即校验并提供 `digest()`；`ProcessIdentity` 只有四个必填字段；`Lifecycle.transition` 返回 generation 加一的新记录并执行 expected_generation fence。
- `classify_start` 必须读取 observation 的 `run_id`、`environment_sha256`、`state`、`process_identity`；`reconcile` 必须接受 InvocationSpec 和 Mapping，并返回五键 mapping：`status`、`run_id`、`expected_environment_sha256`、`observed_environment_sha256`、`replay_allowed`。
- 参考 argv 语义允许普通参数为空或含换行；只拒绝 shell carrier、空 executable、executable CR/LF、NUL、surrogate；未知启动和不完整 cleanup 必须 fail closed。

## Validation commands
- V01（基线应失败）：`python -c "from pathlib import Path; p=Path(r'tests/test_runtime_contract_port.py'); assert p.exists() and p.stat().st_size>0; print('PI_RUNTIME_PORT_ARTIFACT_OK')"`
- V02（必要）：`python -m pytest -q`
- V03（必要）：`python -m compileall -q sentinelmesh tests`
- V04（必要）：`git diff --check`
- V05（必要，基线应失败）：`python -c "from sentinelmesh.runtime import ALLOWED_TRANSITIONS, LIFECYCLE, InvocationSpec, ProcessIdentity, Lifecycle, classify_start, reconcile, validate_argv; assert LIFECYCLE == ('NEW','STARTING','STARTED','STOPPING','STOPPED','FAILED','START_UNKNOWN'); assert ALLOWED_TRANSITIONS['START_UNKNOWN'] == {'STARTED','STOPPING','FAILED'}; assert tuple(validate_argv(['python',''])) == ('python',''); print('PI_RUNTIME_PORT_API_OK')"`

## Definition of done
- D01：canonical 常量、错误、输入和 Lifecycle API；证据 V02/V05。
- D02：classify_start/reconcile 参数、字段和 mapping；证据 V02/V05。
- D03：START_UNKNOWN/generation/identity/cleanup replay gate；证据 V02/V03。
- D04：canonical 直接调用测试与旧 API 回归；证据 V02。
- D05：无外部副作用，native 能力边界明确；证据 V02/V04。

## Stop conditions
- 需要越界、依赖、凭据、联网或真实 provider；或 canonical API 与合同矛盾。
- 只能通过删除/跳过测试、改变 canonical 形状或弱化断言让检查通过。

## Report back
最后报告 D01-D05、实际命令/退出码、变更文件、未运行能力。

## Budget
- max_attempts_total: 1
- max_wall_seconds_per_attempt: 1500
- max_wall_seconds_total: 1500
- cost_budget: tracking-only

## Forbidden
- 不修改合同、AGENTS.md、`.agent/`、设计材料。
- 不删除、跳过或弱化既有测试；不提交、不推送、不合并、不部署。

