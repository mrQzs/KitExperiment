# TASK: supervisor-stream-facts

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: ad58ee8c83baa434e0f6d2951b96d173fc5cb869
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
返工 attempt-001 的候选：让监督器记下两个输出流的事实（device、inode、字节数、SHA-256），并让之后对流的任何改动都能被发现。v2 在 v1 之上修正复审发现的问题：
- 摘要设上限，并且逐块计算，监督器在有界时间、常数内存内报告；
- `streams_match` 对任何参数都不抛出；
- 回滚与钩子失败时只删除路径上仍是本次创建的那个文件。

并让规划端测试 `tests/test_supervisor_stream_facts_gaps.py` 与 v1 的 oracle 全部通过。

## Non-goals
- 不修改 `supervised_record.py`、`supervised_operation.py`、`adapter_run.py`、`scheduler.py`、`runtime.py`、`process_identity.py` 或其他已有模块；桥接与 adapter 的核对是下一份合同。
- 不改清理规则：用 `setsid()` 干净逃逸的后代仍然看不见，`cleanup_confirmed` 的判定不变；不做进程组之外的逃逸检测。
- 不改 `reconcile`、观察记录的格式与 `replay_decision` 的判定。
- 不限制子进程的输出：不截断、不拒绝；只是超过上限的流不做摘要。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、规划端测试、oracle 与其他已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `ad58ee8` 由下列提交依次组成：
  - `6260633`：master `02314c4` + v1 oracle `tests/test_supervisor_stream_facts_oracle.py`（16 个用例），以及规划端对三个已有测试的更新（`SupervisedRun` 字段表、v2 记录字节、v2 伪造记录）；
  - `91a0927`：上一轮候选，即 attempt-001 对 `sentinelmesh/posix_supervisor.py`、`sentinelmesh/supervised_record.py` 的修改与 `tests/test_supervisor_stream_facts.py`；
  - `ad58ee8`：规划端测试 `tests/test_supervisor_stream_facts_gaps.py`，10 个用例。
- 规划端测试在基线上：
  - 7 个失败：逐块摘要、摘要上限（两个）、`streams_match` 的上限与无法编码的路径、钉住被替换时保留替换上来的文件、钩子失败时保留替换上来的文件；
  - 3 个通过：事实在回收之后取、摘要以 `fstat` 的大小为界、字节数是实际读到的数。它们守护上一轮已经正确的行为。
- 上一轮复审的发现（复审记录不在基线里，需要的内容都写在这里）：
  - R1：`_fact_of` 按 `st_size` 循环读取，把每块追加进列表、最后 `b"".join` 再求哈希；`_stream_matches` 也先把至多 `bytes + 1` 字节读进内存。子进程拿到的写描述符不带 `O_APPEND`，一次 `os.ftruncate(1, N)` 就能把文件变成大小为 N 的稀疏文件，什么都不写；监督器随后要读满 N 字节，内存峰值约 2N，N 足够大时 `supervise` 永远返回不了。
  - R2：`streams_match("/nonexistent\x00x", 合法事实)` 抛出 `ValueError: embedded null character`，`streams_match("/nonexistent/\ud800", 合法事实)` 抛出 `UnicodeEncodeError`，违反“对任何参数值都不抛出”。
  - R3：钉住时身份不符（另一个进程在创建与钉住之间把文件改名走、在原路径放了自己的文件），回滚按路径删除，删掉的是别人的文件；本次创建、被改名走的文件反而留下。钩子失败时同样按路径删除。
  - R4：几条规则没有测试把守。规划端已经放进 `tests/test_supervisor_stream_facts_gaps.py`。
- 规划端测试的写法约束（不要破坏）：
  - 用 `mock.patch.object(posix_supervisor, "_reap_group", …)` 控制回收的时机，所以 `supervise` 要通过模块里的名字调用 `_reap_group`（现在就是这样）；
  - 用 `mock.patch.object(posix_supervisor, "hashlib", …)` 记录每次交给哈希的字节数，所以摘要要通过模块级的 `import hashlib` 调用 `hashlib.sha256()`；
  - 用 `mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 1000)` 改小上限，所以上限要在计算事实与核对时从模块全局读取，不能在导入时绑定到默认参数或别的名字上；
  - 包装 `os.pread`、`os.read`、`os.open` 来观察读取与制造替换：事实用 `os.pread` 读，核对用 `os.read` 读，钉住用 `os.open` 打开。
- 规划端用一份草稿实现验证过（草稿未提交）：规划端测试 10/10，v1 oracle 16/16，全量 575 passed；为 v2 新规则故意制造的 8 个偏差全部被抓住。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/posix_supervisor.py
  - tests/test_supervisor_stream_facts.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/supervised_record.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/runtime.py`, `sentinelmesh/process_identity.py`, `tests/test_supervisor_stream_facts_oracle.py`, `tests/test_supervisor_stream_facts_gaps.py`, `tests/test_posix_supervisor.py`, `tests/test_supervised_record_oracle.py`, `tests/test_supervised_operation_gaps.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
v1 的第 1–6 条照旧有效，下面完整列出；标“v2”的是本版本的修改或补充。

1. **钉住证据文件**（`posix_supervisor`）
   - 每个证据文件照旧用 `O_WRONLY | O_CREAT | O_EXCL`（0600）创建；创建之后，用 `os.open(同一路径, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)` 再开一个只读描述符（下称“钉住的描述符”）。
   - 对钉住的描述符和写描述符各做一次 `os.fstat`：钉住的必须是普通文件，且 `(st_dev, st_ino)` 与**写描述符**的相同（v2 明确：不与路径的 `os.stat` 比较）。
   - 两个文件都在 `before_start` 钩子之前创建并钉住。
   - 任何一步失败（打开或 `fstat` 出现 `OSError`、不是普通文件、身份不同）时：
     - 关闭本次调用已经打开的全部描述符；
     - 删除本次调用创建、并且路径上仍是它的证据文件（见下）；
     - 抛出 `RuntimeErrorCode("E_RUNTIME_EVIDENCE", …)`。不调用钩子，不启动进程。
   - **v2：只删除自己的文件。** 回滚，以及 `before_start` 抛出时的清理，都只在下面条件成立时 `unlink` 一个证据文件：
     - `os.lstat(路径)` 显示它是普通文件；
     - 它的 `(st_dev, st_ino)` 等于创建时写描述符的身份（创建之后立即 `fstat` 写描述符得到）。

     路径上已是别的文件、不存在、或 `lstat` 出现 `OSError` 时，什么都不删。本次创建、已被改名走的文件留在被移到的地方。
   - 子进程拿到的仍是两个只写的描述符。
   - 钉住的描述符打开之后，`supervise` 的每一条出口都要关闭它们：钩子失败、exec 失败、正常返回，以及任何冒出的异常（包括 `KeyboardInterrupt`）。
2. **流的事实**（`posix_supervisor`）
   - 有进程启动的每一次运行（正常结束、超时、取消；清理确认与否），都在 `_reap_group` 返回之后计算：

     `streams = {"stdout": 事实(钉住的 stdout), "stderr": 事实(钉住的 stderr)}`
   - **v2：摘要上限。** 新增公开常量 `MAX_STREAM_DIGEST_BYTES = 256 * 1024 * 1024`。计算事实与第 3 条核对时，都在调用的时候从模块全局读取它。
   - 一个流的事实：
     - 先 `info = os.fstat(钉住的描述符)`；
     - v2：`info.st_size > MAX_STREAM_DIGEST_BYTES` 时，事实为 None，一个字节都不读；
     - 否则用 `os.pread(描述符, n, offset)` 从偏移 0 起分块读取，读满 `info.st_size` 字节或 `pread` 返回空为止；
     - v2：逐块计算摘要：`hashlib.sha256()` 之后每读一块就 `update` 一次，每次交给哈希的数据不超过 65536 字节，不把多块拼在一起；
     - 返回新的 dict：`{"device": info.st_dev, "inode": info.st_ino, "bytes": 读到的字节数, "sha256": 这些字节的 SHA-256（小写十六进制）}`，前三项是精确 int。
   - 这一步出现 `OSError` 时，这个流的事实为 `None`，不抛出。其他异常（例如 `KeyboardInterrupt`）不吸收，照样从 `supervise` 冒出，钉住的描述符照样关闭。
   - `SupervisedRun` 末尾的字段 `streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None`：
     - 有进程启动时为上面的 dict；
     - `start_failed` 时为 `None`。

     其余字段、观察记录、生命周期、停止原因与清理规则都不变。
3. **`streams_match(evidence_dir, streams) -> bool`**（`posix_supervisor` 的公开函数）
   - 下列条件全部满足时返回 True，否则返回 False：
     - `type(evidence_dir) is str`；
     - `type(streams) is dict`，键恰好是 `"stdout"` 与 `"stderr"`；
     - 每个值都是精确的 dict，键恰好是 `device`、`inode`、`bytes`、`sha256`：
       - `device`、`inode`、`bytes` 是精确 int（bool 不算），`inode` 与 `bytes` 不小于 0；
       - v2：`bytes` 不大于 `MAX_STREAM_DIGEST_BYTES`；超过时直接返回 False，不打开、不读任何文件；
       - `sha256` 是 64 个小写十六进制字符的精确 str；
     - 两个文件（`evidence_dir` 下的 `stdout.log` 与 `stderr.log`）都与各自的事实一致：
       - 用 `os.open(路径, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)` 打开；
       - `fstat` 显示是普通文件，`st_dev`、`st_ino`、`st_size` 分别等于 `device`、`inode`、`bytes`；
       - 用 `os.read` 最多读 `bytes + 1` 字节，读到的恰好是 `bytes` 字节，它们的 SHA-256 等于 `sha256`；v2：同第 2 条，逐块计算摘要。
   - 对任何参数值或文件状态都不抛出：`OSError` 与 `ValueError`（含 `UnicodeEncodeError`：路径含 NUL 或无法编码）一律算不一致（v2）；打开的描述符在任何情况下都要关闭。
4. **观察记录 v2**（`supervised_record`，上一轮已完成，本版本不改）
   - schema 为 `"sentinelmesh-observation-v2"`，记录的键恰好是 `schema`、`spec_sha256`、`observation`、`streams`；`record_supervised_run` 把 `run.streams` 写进 `streams`；读取时按 v1 第 4 条闭合校验；`replay_decision` 的判定不变。
5. **`recorded_streams(spec, evidence_dir) -> Optional[dict]`**（`supervised_record`，上一轮已完成，本版本不改）。
6. **导入无副作用**：导入 `sentinelmesh.posix_supervisor` 与 `sentinelmesh.supervised_record` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、`posix_supervisor.py` 的全文、规划端测试 `tests/test_supervisor_stream_facts_gaps.py`、v1 oracle，以及你上一轮写的 `tests/test_supervisor_stream_facts.py`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–3 条中标“v2”的部分修改 `sentinelmesh/posix_supervisor.py`，更新模块与函数的文档字符串（摘要上限、逐块摘要、只删除自己的文件）。
3. 更新 `tests/test_supervisor_stream_facts.py`：上一轮的用例照旧通过（需要时只做与 v2 规则一致的修正），并补下列规划端测试没有覆盖的用例：
   - 通用要求：
     - 每个拒绝用例都先验证合法基线，再只改一处；
     - 每个用例都清理自己启动的进程；
     - 冒出的 `KeyboardInterrupt` 要在测试里拦住，不要让它中断 pytest 会话。
   - 用例：
     - 钉住 stderr 时身份不符：本次创建的 stdout 被删除，替换到 stderr 路径上的文件保留；
     - `before_start` 抛出前替换了 stdout：替换上来的 stdout 保留，本次创建的 stderr 被删除；
     - 上限改小之后，`bytes` 恰好等于上限的事实可以核对通过，多一字节返回 False 且不读文件；
     - 摘要期间 `os.fstat` 出现 `OSError` 时，这个流的事实为 None，运行照常返回。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_supervisor_stream_facts_gaps.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要）：`python -m pytest -q tests/test_supervisor_stream_facts_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_supervisor_stream_facts.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要）：`python -m pytest -q tests/test_posix_supervisor.py tests/test_posix_supervisor_oracle.py tests/test_posix_supervisor_gaps.py tests/test_posix_supervisor_probes.py tests/test_supervised_record_oracle.py tests/test_supervised_record.py tests/test_supervised_record_gaps.py tests/test_supervised_operation_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_adapter_run_oracle.py tests/test_adapter_run.py tests/test_adapter_run_gaps.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要）：`python -c "import sys, sentinelmesh.posix_supervisor, sentinelmesh.supervised_record; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01 与 V05 在基线上失败，是因为规划端测试里有 7 个守护 v2 的新规则。V02–V04 保护上一轮已经正确的行为，以及监督器、记录、桥接与 adapter 的已有行为；V03 是你上一轮写的测试。

## Definition of done
- D01：钉住与只删除自己的文件（Behavior contract 第 1 条）；证据：V01、V02、V03。
- D02：流的事实，含摘要上限与逐块摘要（第 2 条）；证据：V01、V02、V03。
- D03：`streams_match` 的核对、上限与“从不抛出”（第 3 条）；证据：V01、V02、V03。
- D04：记录与 `recorded_streams` 不退化（第 4、5 条）；证据：V02、V04。
- D05：导入无副作用，已有模块不退化（第 6 条）；证据：V04、V05、V08。
- D06：Step 3 的测试齐全，不自行做变异检查；证据：V03、review。
- D07：所有修改在允许集合内，无依赖变更；证据：V06、V07、scope。

## Stop conditions
- 基线、规划端测试、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总：attempt-001 已用 1 次，本版本是最后一次）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 5 分钟；
  - 实现约 6 分钟；
  - 测试约 8 分钟；
  - 运行检查约 5 分钟（规划端测试约 2 秒，全量约 125 秒）；
  - 执行报告约 4 分钟；
  - 合计约 28 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、规划端测试、oracle、已有模块（允许集合之外）与其他已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
