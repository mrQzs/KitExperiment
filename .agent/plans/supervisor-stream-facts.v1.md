# TASK: supervisor-stream-facts

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 626063340b5bc99ce422ff3d5529f7f09207c42f
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
让监督器在进程组回收之后，记下两个输出流的事实（device、inode、字节数、SHA-256），并让之后对流的任何改动都能被发现：
- 事实取自启动之前钉住的只读描述符，不取自路径；
- 观察记录升为 schema v2，携带同样的事实；
- 提供核对函数 `streams_match` 与读取函数 `recorded_streams`。

并让规划端 oracle `tests/test_supervisor_stream_facts_oracle.py` 全部通过。

## Non-goals
- 不修改 `supervised_operation.py`、`adapter_run.py`、`scheduler.py`、`runtime.py`、`process_identity.py` 或其他已有模块；桥接与 adapter 的核对是下一份合同。
- 不改清理规则：用 `setsid()` 干净逃逸的后代仍然看不见，`cleanup_confirmed` 的判定不变；不做进程组之外的逃逸检测。
- 不改 `reconcile`、五键（或三键）观察 `observation` 与 `replay_decision` 的判定；不给流设字节上限。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `6260633` = master `02314c4` + 规划端 oracle。
  - 新增 `tests/test_supervisor_stream_facts_oracle.py`：16 个用例，启动真实的本地 python 进程。
  - 规划端同时改了三个已有测试，以跟上本合同有意的改动：
    - `tests/test_posix_supervisor.py`：`SupervisedRun` 的字段表末尾加 `streams`；
    - `tests/test_supervised_record_oracle.py`：观察记录的精确字节改为 v2；
    - `tests/test_supervised_operation_gaps.py`：子进程伪造的 NOT_STARTED 记录改为 v2，这样新读取方仍会读到它。
- 设计说明：`.agent/design/stream-trust-boundary.md`，第 3 节第 1–4 条与第 6 节（决定 E1–E8）。
- 现状：
  - `posix_supervisor._open_evidence_files` 用 `O_WRONLY | O_CREAT | O_EXCL`（0600）创建 `stdout.log` 与 `stderr.log`；第二个失败时关闭并删除第一个。`Popen` 之后关闭这两个写描述符。
  - `supervise` 依次是：输入检查 → 创建证据文件 → `before_start` 钩子（失败时关闭并删除两个文件，原样抛出）→ `Popen`（`OSError` 时返回 `start_failed`，观察为三键 NOT_STARTED）→ 等待 → 停止进程组 → `_reap_group` → 返回 `SupervisedRun`。
  - `supervised_record` 的观察记录是 `{"schema": "sentinelmesh-observation-v1", "spec_sha256", "observation"}`，读取时校验闭合键集；`record_supervised_run` 在 `supervise` 返回后写入。
  - 桥接 `run_supervised_operation` 把“`record_supervised_run` 抛出 `RuntimeErrorCode` 且没有附带运行”当作启动前被拒，释放为 NOT_STARTED。所以进程启动之后，监督器不能抛出 `RuntimeErrorCode`。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 16/16，全量 554 passed；
  - 34 个故意制造的偏差全部被抓住。只有一组例外：sha256 的“格式检查”和“比较”是两道互相遮蔽的守卫，单独去掉任意一道都等价，两道一起去掉会被抓住。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/posix_supervisor.py
  - sentinelmesh/supervised_record.py
- allowed_new_files:
  - tests/test_supervisor_stream_facts.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/runtime.py`, `sentinelmesh/process_identity.py`, `tests/test_supervisor_stream_facts_oracle.py`, `tests/test_posix_supervisor.py`, `tests/test_supervised_record_oracle.py`, `tests/test_supervised_operation_gaps.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
1. **钉住证据文件**（`posix_supervisor`）
   - 每个证据文件照旧用 `O_WRONLY | O_CREAT | O_EXCL`（0600）创建；创建之后，用 `os.open(同一路径, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)` 再开一个只读描述符（下称“钉住的描述符”）。
   - 对钉住的描述符和写描述符各做一次 `os.fstat`：钉住的必须是普通文件，且 `(st_dev, st_ino)` 与写描述符相同。
   - 两个文件都在 `before_start` 钩子之前创建并钉住。
   - 任何一步失败（打开或 `fstat` 出现 `OSError`、不是普通文件、身份不同）时：
     - 关闭本次调用已经打开的全部描述符；
     - 删除本次调用创建的证据文件；
     - 抛出 `RuntimeErrorCode("E_RUNTIME_EVIDENCE", …)`。不调用钩子，不启动进程。
   - 子进程拿到的仍是两个只写的描述符。
   - 钉住的描述符打开之后，`supervise` 的每一条出口都要关闭它们：钩子失败、exec 失败、正常返回，以及任何冒出的异常（包括 `KeyboardInterrupt`）。
2. **流的事实**（`posix_supervisor`）
   - 有进程启动的每一次运行（正常结束、超时、取消；清理确认与否），都在 `_reap_group` 返回之后计算：

     `streams = {"stdout": 事实(钉住的 stdout), "stderr": 事实(钉住的 stderr)}`
   - 一个流的事实：
     - 先 `info = os.fstat(钉住的描述符)`；
     - 再用 `os.pread(描述符, n, offset)` 从偏移 0 起分块读取，读满 `info.st_size` 字节或 `pread` 返回空为止；
     - 返回新的 dict：`{"device": info.st_dev, "inode": info.st_ino, "bytes": 读到的字节数, "sha256": 这些字节的 SHA-256（小写十六进制）}`，前三项是精确 int。
   - 这一步出现 `OSError` 时，这个流的事实为 `None`，不抛出。其他异常（例如 `KeyboardInterrupt`）不吸收，照样从 `supervise` 冒出，钉住的描述符照样关闭。
   - `SupervisedRun` 末尾新增字段 `streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None`：
     - 有进程启动时为上面的 dict；
     - `start_failed` 时为 `None`。

     其余字段、观察记录、生命周期、停止原因与清理规则都不变。
3. **`streams_match(evidence_dir, streams) -> bool`**（`posix_supervisor` 的公开函数）
   - 下列条件全部满足时返回 True，否则返回 False：
     - `type(evidence_dir) is str`；
     - `type(streams) is dict`，键恰好是 `"stdout"` 与 `"stderr"`；
     - 每个值都是精确的 dict，键恰好是 `device`、`inode`、`bytes`、`sha256`：
       - `device`、`inode`、`bytes` 是精确 int（bool 不算），`inode` 与 `bytes` 不小于 0；
       - `sha256` 是 64 个小写十六进制字符的精确 str；
     - 两个文件（`evidence_dir` 下的 `stdout.log` 与 `stderr.log`）都与各自的事实一致：
       - 用 `os.open(路径, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)` 打开；
       - `fstat` 显示是普通文件，`st_dev`、`st_ino`、`st_size` 分别等于 `device`、`inode`、`bytes`；
       - 最多读 `bytes + 1` 字节，读到的恰好是 `bytes` 字节，它们的 SHA-256 等于 `sha256`。
   - 对任何参数值或文件状态都不抛出：`OSError` 一律算不一致；打开的描述符在任何情况下都要关闭。
4. **观察记录 v2**（`supervised_record`）
   - schema 改为 `"sentinelmesh-observation-v2"`，记录的键恰好是 `schema`、`spec_sha256`、`observation`、`streams`。
   - `record_supervised_run` 把 `run.streams` 写进 `streams`（`None` 写成 `null`）；`observation` 不变。
   - 读取时（`replay_decision` 与第 5 条共用同一套校验），在已有校验之外还要求：
     - 内层 `observation` 是三键（未启动）的形状时，`streams` 必须是 `null`；
     - 是五键（已启动）的形状时，`streams` 必须是对象，键恰好是 `stdout` 与 `stderr`；
     - 每个值是 `null`，或键恰好是 `device`、`inode`、`bytes`、`sha256` 的对象：
       - `device` 是整数（可以为负）；
       - `inode` 与 `bytes` 是不小于 0 的整数；JSON 的 `true`、`false` 不算整数；
       - `sha256` 是 64 个小写十六进制字符的字符串。

     不满足时报 `E_RUNTIME_EVIDENCE`。schema 为 v1 的旧记录同样被拒绝。
   - `replay_decision` 的判定不变：`streams` 不参与判定。
5. **`recorded_streams(spec, evidence_dir) -> Optional[dict]`**（`supervised_record` 的公开函数）
   - 参数检查与 `replay_decision` 相同：
     - `type(spec) is not InvocationSpec`：`E_RUNTIME_INVOCATION`；
     - `evidence_dir` 不是 str、不是绝对路径、或不是已存在的目录：`E_RUNTIME_EVIDENCE`。
   - 用第 4 条的同一套校验读取两份记录；损坏时报 `E_RUNTIME_EVIDENCE`。
   - 下列情形依次判断：
     - 没有交接记录：有观察记录时报 `E_RUNTIME_EVIDENCE`，否则返回 None；
     - 交接记录不等于 `spec` 的交接内容：None；
     - 没有观察记录：None；
     - 观察记录的 `spec_sha256` 不等于 `spec.digest()`：None；
     - 否则返回记录里的 `streams`，作为新的 dict（未启动的记录返回 None）。
   - 只读：不创建、不修改、不删除任何文件。
6. **导入无副作用**：导入 `sentinelmesh.posix_supervisor` 与 `sentinelmesh.supervised_record` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3、6 节、oracle、`posix_supervisor.py` 与 `supervised_record.py` 的全文，以及 `tests/test_posix_supervisor.py` 中已有的描述符泄漏用例。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–2 条与第 6 条修改 `sentinelmesh/posix_supervisor.py`，并新增第 3 条的 `streams_match`；更新模块文档字符串，写明钉住、流的事实与核对。
3. 按第 4–5 条修改 `sentinelmesh/supervised_record.py`；更新模块文档字符串。
4. 新建 `tests/test_supervisor_stream_facts.py`，覆盖下列 oracle 没有覆盖的用例。
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处；
     - 每个用例都清理自己启动的进程；
     - 冒出的 `KeyboardInterrupt` 要在测试里拦住，不要让它中断 pytest 会话。
   - 用例：
     - 超过一个读取块的流（例如 200 000 字节）：事实与文件一致，`streams_match` 为 True；
     - 取消的运行（等待时收到 `KeyboardInterrupt`，可以参照 `tests/test_posix_supervisor_oracle.py` 的做法）：`stop_reason` 为 `cancelled`，`streams` 与文件一致；
     - `before_start` 钩子抛出时，钉住的描述符被关闭（`/dev/fd` 的数量不变），证据目录为空；
     - 读取有上界：`fstat` 报告的大小等于记录的字节数，而文件实际更长时，`streams_match` 返回 False，且读取的字节数不超过 `bytes + 1`（可以包装 `os.fstat` 与 `os.read` 来观察）；
     - 只给十个位置参数构造的 `SupervisedRun`，`streams` 为 None。
5. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_supervisor_stream_facts_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_supervisor_stream_facts.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要，基线应失败）：`python -m pytest -q tests/test_posix_supervisor.py tests/test_supervised_record_oracle.py tests/test_supervised_operation_gaps.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要）：`python -m pytest -q tests/test_posix_supervisor_oracle.py tests/test_posix_supervisor_gaps.py tests/test_posix_supervisor_probes.py tests/test_supervised_record.py tests/test_supervised_record_gaps.py tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要）：`python -c "import sys, sentinelmesh.posix_supervisor, sentinelmesh.supervised_record; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01 在基线上失败，是因为 `streams_match` 与 `recorded_streams` 还不存在；V02 是因为测试文件还不存在；V03 是因为三个已有测试已经按 v2 与新字段更新。V04 保护监督器、记录、桥接与 adapter 的已有行为。

## Definition of done
- D01：钉住证据文件与失败时的回滚（Behavior contract 第 1 条）；证据：V01、V02。
- D02：流的事实取自钉住的描述符，在回收之后计算，读不了时为 None，中断照样冒出（第 2 条）；证据：V01、V02、V03。
- D03：`streams_match` 的核对与“从不抛出”（第 3 条）；证据：V01、V02。
- D04：观察记录 v2 的写入与闭合校验，`replay_decision` 的判定不变（第 4 条）；证据：V01、V03、V04。
- D05：`recorded_streams`（第 5 条）；证据：V01。
- D06：导入无副作用，已有模块不退化（第 6 条）；证据：V04、V05、V08。
- D07：Step 4 的测试齐全，不自行做变异检查；证据：V02、review。
- D08：所有修改在允许集合内，无依赖变更；证据：V06、V07、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
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
  - 阅读约 5 分钟；
  - 实现约 8 分钟；
  - 测试约 8 分钟；
  - 运行检查约 5 分钟（oracle 约 8 秒，全量约 120 秒）；
  - 执行报告约 4 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D08，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
