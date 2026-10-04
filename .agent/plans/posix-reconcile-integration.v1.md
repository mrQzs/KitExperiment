# TASK: posix-reconcile-integration

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: f0668df6de8ecd501db51e73575200f62824affa
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
把监督器的启动结果落盘，并只凭落盘的记录做重放判定：
- `supervise()` 增加钩子 `before_start`；
- 新增 `sentinelmesh/supervised_record.py`：
  - `record_supervised_run()` 在启动前写交接记录，结束后写观察记录；
  - `replay_decision()` 只读这两份记录，返回与 `reconcile` 相同的五键记录；
- 让规划端 oracle `tests/test_supervised_record_oracle.py` 全部通过。

## Non-goals
- 不接入 `Scheduler`、adapter 或旧的 `SubprocessRunner`；不修改 `runtime.py`、`process_identity.py`、`core.py`。
- 不改监督器已有的行为，只加钩子。监督器的行为由 `.agent/plans/posix-supervisor.v1.md`、`v2.md` 与已集成的实现确定。
- 不验证断电后的持久性：不对目录做 fsync，不用 F_FULLFSYNC。Linux 与 Windows 保持 NOT_RUN。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`，也不修改已有的监督器测试文件与本合同的 oracle；不联网、不读取凭据、不启动真实 provider CLI。

## Repository facts
- 基线 `f0668df` = master `de0123c` + 规划端 oracle（11 个真实进程用例）。规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 全部通过，全量 297 passed；
  - 14 个草稿变异都被 oracle 抓住。
- 设计见 `.agent/design/posix-runtime-runner.md` 第 6 节。监督器的合同与复审在 `.agent/plans/posix-supervisor.v1.md`、`v2.md` 与 `.agent/reviews/posix-supervisor/`。
- 可用的已有接口：
  - `sentinelmesh.posix_supervisor`：`supervise`、`SupervisedRun`；
  - `sentinelmesh.runtime`：`InvocationSpec`（`digest()` 返回规格摘要）、`RuntimeErrorCode`、`classify_start`、`reconcile`；
  - `sentinelmesh.core`：
    - `canonical_json(value)`：排序键、紧凑、UTF-8、末尾换行，根必须是 dict；
    - `strict_json_loads(raw)`：拒绝重复键、NaN 与 BOM，错误类型为 `ProtocolError`。
- `reconcile(spec, observation)` 总是按“已交接”分类。没有观察记录时改用 `classify_start(handoff_accepted=..., started_observation=None, expected=spec)`：已交接得到 START_UNKNOWN，未交接得到 NOT_STARTED。
- 规划端在 macOS 15.7.9 上实测：APFS 支持硬链接；用 `os.open(..., O_NOFOLLOW)` 打开符号链接会报 ELOOP（`OSError`）。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files:
  - sentinelmesh/posix_supervisor.py
- allowed_new_files:
  - sentinelmesh/supervised_record.py
  - tests/test_supervised_record.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/core.py`, `tests/test_supervised_record_oracle.py`, `tests/test_posix_supervisor.py`, `tests/test_posix_supervisor_oracle.py`, `tests/test_posix_supervisor_probes.py`, `tests/test_posix_supervisor_gaps.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **监督器钩子**（修改 `supervise`）
   - 签名增加仅关键字参数 `before_start: Optional[Callable[[], None]] = None`，放在 `allowed_secret_names` 之后。
   - 校验顺序：在证据目录校验之后增加一步。`before_start` 不是 None、又不可调用（`callable()` 为 False）时报 `E_RUNTIME_INVOCATION`；此时不创建文件、不启动进程。
   - 两个证据文件建好之后、调用 `subprocess.Popen` 之前，无参数调用 `before_start()` 恰好一次；为 None 时跳过。
   - `before_start()` 抛出任何异常（包括 `KeyboardInterrupt`）时：
     - 关闭两个描述符；
     - 删除本次创建的两个证据文件；
     - 原样重新抛出同一个异常，不启动进程。
   - 监督器的其余行为都不变，已有的 oracle、探针、补测与执行器测试照常通过。
2. **记录文件**（`supervised_record.py`）
   - 常量：`HANDOFF_NAME = "handoff.json"`、`OBSERVATION_NAME = "observation.json"`。
   - 交接记录的内容恰好是 `{"schema": "sentinelmesh-handoff-v1", "request_id": spec.request_id, "run_id": spec.run_id, "environment_sha256": spec.environment_sha256, "spec_sha256": spec.digest()}`。
   - 观察记录的内容恰好是 `{"schema": "sentinelmesh-observation-v1", "spec_sha256": spec.digest(), "observation": dict(run.observation)}`。
   - 写入：
     - 字节为 `core.canonical_json(内容)`；
     - 先用 `os.open(<证据目录>/.<名称>.partial, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)` 写入全部字节并 `os.fsync`，关闭后用 `os.link` 发布为 `<证据目录>/<名称>`；
     - 无论成败都删除 partial 文件；
     - 任一步的 `OSError` 都报 `E_RUNTIME_EVIDENCE`；已存在的记录永不覆盖，也不删除。
   - 读取：
     - 用 `os.open(路径, O_RDONLY | O_NOFOLLOW)` 打开。文件不存在，视为没有这份记录；其他 `OSError`（包括符号链接）报 `E_RUNTIME_EVIDENCE`；
     - 必须是普通文件，且不超过 65536 字节，否则报 `E_RUNTIME_EVIDENCE`；
     - `strict_json_loads` 失败、根不是 dict，或 `canonical_json(解析结果)` 与原字节不同，都报 `E_RUNTIME_EVIDENCE`；
     - 交接记录：键集合恰好是上述五个，`schema` 等于 `"sentinelmesh-handoff-v1"`，五个值都是 str；
     - 观察记录：
       - 键集合恰好是上述三个；
       - `schema` 等于 `"sentinelmesh-observation-v1"`；
       - `spec_sha256` 是 str；
       - `observation` 是 dict，键集合恰好是监督器观察记录的五键（启动成功）或三键（未启动）；
     - 不满足这些形状要求，报 `E_RUNTIME_EVIDENCE`。
3. **`record_supervised_run(spec, *, environment, evidence_dir, worktree_id, allowed_secret_names=frozenset()) -> SupervisedRun`**
   - 调用 `supervise(...)` 并透传参数，同时传入 `before_start`：按第 2 条写交接记录。
   - `supervise()` 返回后，按第 2 条写观察记录，再返回同一个 `SupervisedRun`。
   - `supervise()` 与钩子抛出的异常原样冒出；钩子写入失败时，冒出的是 `E_RUNTIME_EVIDENCE`。
   - 观察记录写入失败时报 `E_RUNTIME_EVIDENCE`。此时进程已经结束、交接记录已在，判定为 START_UNKNOWN，属于失败关闭。
4. **`replay_decision(spec, evidence_dir) -> dict`**
   - `type(spec) is not InvocationSpec` 时报 `E_RUNTIME_INVOCATION`。`evidence_dir` 不是 str、不是绝对路径，或不是已存在的目录时，报 `E_RUNTIME_EVIDENCE`。
   - 先读两份记录（规则见第 2 条，任一份损坏或形状不符即报错），再按顺序判定：
     1. 没有交接记录：若有观察记录，报 `E_RUNTIME_EVIDENCE`；否则 status 为 `classify_start(handoff_accepted=False, started_observation=None, expected=spec)`，即 NOT_STARTED；
     2. 交接记录与本 spec 应有的交接记录不完全相同：status 为 `IDENTITY_MISMATCH`，observed_environment_sha256 取交接记录中的值；
     3. 没有观察记录：status 为 `classify_start(handoff_accepted=True, started_observation=None, expected=spec)`，即 START_UNKNOWN；
     4. 观察记录的 `spec_sha256` 不等于 `spec.digest()`：status 为 `IDENTITY_MISMATCH`，observed_environment_sha256 取观察记录内 `observation` 的 `environment_sha256`；
     5. 其余情形返回 `dict(reconcile(spec, 观察记录["observation"]))`。
   - 前四种情形返回五键记录：`{"status", "run_id": spec.run_id, "expected_environment_sha256": spec.environment_sha256, "observed_environment_sha256", "replay_allowed": status == "NOT_STARTED"}`。第 1、3 种情形的 observed_environment_sha256 为 None。
   - 判定只读：不创建、不修改、不删除任何文件。
5. **导入无副作用**：导入 `sentinelmesh.supervised_record` 时不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 6 节、oracle、`posix_supervisor.py`，以及 `runtime.py` 与 `core.py` 中会用到的接口。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1 条修改 `supervise`。只加钩子，不改动其他代码路径。
3. 按第 2–5 条新建 `sentinelmesh/supervised_record.py`。
4. 新建 `tests/test_supervised_record.py`，覆盖下列 oracle 没有覆盖的用例。
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处，并断言具体的 code；
     - 每个用例都清理自己启动的进程组；
     - 数值界限与合同中的数字比较（例如 65536），不与被测模块的常量比较。
   - 钩子：
     - 抛出 `KeyboardInterrupt` 时同样回滚证据文件，并原样冒出（测试里拦住它，不要让它中断 pytest 会话）；
     - 一次运行中恰好调用一次；
     - 传入 `before_start=None` 时，结果与不传相同；
     - 校验顺序：`before_start` 不可调用、证据目录又缺失时，报 `E_RUNTIME_EVIDENCE`。
   - 写入失败：
     - 用补丁让 `os.link` 抛出 `OSError`：报 `E_RUNTIME_EVIDENCE`，partial 文件被删除，没有启动进程；
     - 用补丁让观察记录写入失败：`record_supervised_run` 报 `E_RUNTIME_EVIDENCE`，之后 `replay_decision` 判为 START_UNKNOWN；
     - 交接记录已存在而失败时，不留下 partial 文件。
   - 读取：下列情形都报 `E_RUNTIME_EVIDENCE`：
     - 观察记录内层缺少 `cleanup_confirmed`；
     - `schema` 不符；
     - 交接记录的某个值不是 str；
     - 记录的根是 list；
     - 记录路径是目录；
     - 恰好 65537 字节的规范记录（65536 字节的合法记录照常读取）。
   - 判定：
     - 只读：调用前后，证据目录的文件列表与各文件字节都相同；
     - 未启动的三键观察记录与交接记录一致时，结果与 `reconcile` 相同（NOT_STARTED，可以重放）。
5. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_supervised_record_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_supervised_record.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_posix_supervisor_oracle.py tests/test_posix_supervisor_probes.py tests/test_posix_supervisor_gaps.py tests/test_posix_supervisor.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.supervised_record; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为模块还不存在；V02 在基线上失败，是因为测试文件还不存在。V03 保护监督器已有的行为。

## Definition of done
- D01：监督器钩子（Behavior contract 第 1 条），已有监督器行为不变；证据：V01、V02、V03。
- D02：记录文件的写入与读取（第 2 条）；证据：V01、V02。
- D03：`record_supervised_run` 与 `replay_decision`（第 3、4 条）；证据：V01、V02。
- D04：导入无副作用（第 5 条）；证据：V07。
- D05：Step 4 的测试齐全，不自行做变异检查；证据：V02、review。
- D06：全量测试不退化，所有修改在允许集合内，无依赖变更；证据：V04、V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 中的实测事实与本机不符。
- 必须修改允许范围之外的文件（包括 oracle、已有的监督器测试、`runtime.py`、`core.py`）才能继续。
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
  - 阅读约 4 分钟；
  - 实现约 10 分钟；
  - 测试约 10 分钟；
  - 运行检查约 3 分钟（oracle 约 7 秒，全量约 90 秒）；
  - 执行报告约 4 分钟；
  - 合计约 31 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、oracle、已有的监督器测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
