# TASK: durable-ledger

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: 2ee04c06c73c48bbaab6fe2e4ca8f97eb3ec15ed
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
返工 attempt-001 的候选：持久调度账本（命令日志加重放）。v2 在 v1 之上修正复审发现的问题：
- 内存调用抛出 Scheduler 拒绝（`ContractError`）之外的任何异常时，实例失效；
- 打开失败时一律关闭连接，只报账本自己的错误；打开绝不新建文件；
- 创建用 `O_EXCL` 原子地新建文件，失败时只删除自己建的那个文件；
- 重放时拒绝重复的或不合格式的操作 id。

并让规划端测试 `tests/test_durable_ledger_gaps.py` 与 v1 的 oracle 全部通过。

## Non-goals
- 不改内存 `Scheduler`（v1 已提出的 `_new_operation_id` 保持不变），不改其他已有模块。
- 不记录运行描述，不做恢复驱动（下一份合同）；不做快照与压缩；不用 WAL；不做多宿主；不承诺断电之后的持久性。
- 调用方在调用期间并发修改自己传入的映射或列表、实例打开期间路径上的文件被换掉：都不在本合同范围内。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、规划端测试与其他已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `2ee04c0` 由下列提交依次组成：
  - `bb93f04`：master `35426a2` + v1 oracle `tests/test_durable_ledger_oracle.py`（16 个用例）；
  - `9063615`：上一轮候选，即 attempt-001 的 `sentinelmesh/durable_ledger.py`、`sentinelmesh/scheduler.py`（只提出了 `_new_operation_id`）与 `tests/test_durable_ledger.py`；
  - `2ee04c0`：规划端测试 `tests/test_durable_ledger_gaps.py`，10 个测试函数。
- 规划端测试在基线上：8 个测试函数失败（直接失败或有子用例失败）；`test_an_entry_rehashed_over_a_wrong_previous_is_refused` 与 `test_an_entry_removed_and_the_rest_renumbered_is_refused` 通过，它们守护上一轮已经正确的哈希链前后链接。
- 上一轮复审的发现（复审记录不在基线里，需要的内容都写在这里）：
  - R1：内存调用（`_apply` 里的 `target(*args, **kwargs)`）在负责失效处理的 `try` 之外。在 claim 已经写入操作之后注入 `KeyboardInterrupt`，实例仍然可用；接着的 `handoff` 被记入日志，重放时找不到操作，账本从此打不开。v1 合同第 6 条第 2 步写的是“实例照常可用”，这是合同的缺口，v2 修订。
  - R2：`open_ledger` 在读取之后做哈希链校验、构造 Scheduler 与重放，这些都在关闭连接的保护之外；每次打开损坏的账本都漏一个描述符。
  - R3：
    - 嵌套很深的命令冒出 `RecursionError`；
    - 5000 位的 generation 让 `int()` 抛出 `ValueError`；
    - generation 为 `"0"` 或 2^53 时冒出 Scheduler 的 `ContractError`；
    - 重放的调用抛出 `ContractError`、`TypeError` 之外的异常时原样冒出。
  - R4：路径在 `lstat` 与 `sqlite3.connect` 之间消失时，connect 会新建一个空文件。
  - R5：创建失败时 `_discard` 按“0 字节”删除路径上的文件；`lstat` 与 connect 之间不是原子的，另一个创建者的账本可能被删掉，也可能在别人刚放上的空文件上建表。
  - R6：v1 的 M05（只按每行自己的 `previous` 核对摘要）没有测试守住；规划端测试已补。
  - R7：伪造的日志可以让两次 claim 复用同一个操作 id。
- 规划端测试的写法约束（不要破坏）：
  - 用 `mock.patch.object(os, "lstat", …)` 制造“检查之后、连接之前”的竞态；
  - 用 `mock.patch.object(durable_ledger.sqlite3, "connect", …)` 包装连接，在事务开始时换掉文件并让它失败，所以模块要通过 `sqlite3.connect` 连接；
  - 用 `mock.patch.object(Scheduler, "_emit", …)` 在内存调用中途抛出异常；
  - 用 `/dev/fd` 的条目数检查描述符泄漏。
- 规划端用一份草稿实现验证过（草稿未提交）：规划端测试 10/10、v1 oracle 16/16、执行器的测试 10/10，全量 658 passed；为 v2 新规则故意制造的 11 个偏差全部被抓住。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/durable_ledger.py
  - tests/test_durable_ledger.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/scheduler.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_durable_ledger_gaps.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
v1 的第 1–7 条照旧有效（附在本节末尾，原文不变），下面的 v2 规则修改或补充其中对应的条目；两处说法不同时以 v2 规则为准。

- **v2-1（修改 v1 第 6 条第 2 步）内存调用的异常**：
  - 参数绑定（`inspect.signature(...).bind`）在调用内存 Scheduler 之前进行。绑定失败的 `TypeError` 原样冒出，什么都不记录，实例照常可用。
  - 调用内存 Scheduler 的同名方法：
    - 它抛出 `ContractError`（Scheduler 的拒绝，账本不变）：原样冒出，什么都不记录，实例照常可用；
    - 它抛出任何其他异常（包括 `TypeError`、`RuntimeError`、`KeyboardInterrupt`、`MemoryError`）：实例失效，原样冒出。内存状态可能已经变了，而日志里没有。
- **v2-2（补充 v1 第 5 条）打开**：
  - 连接用 `sqlite3.connect(<路径的 file: URI>?mode=rw, uri=True, ...)`，绝不新建文件。路径要先转成绝对路径，再用 `pathlib.Path.as_uri()` 编码（路径里可能有空格）。
  - 连接成功之后的每一步——pragma、读取、哈希链校验、构造 Scheduler、重放——失败时都关闭连接，再报错。
  - `generation`：规范的十进制整数文本，最多 16 位（2^53−1 是 16 位），并且 `Scheduler(generation=...)` 接受它；否则报 `E_LEDGER_FORMAT`。Scheduler 的 `ContractError` 映射成 `E_LEDGER_FORMAT`，不原样冒出。
  - 解析命令时的任何失败，包括 `RecursionError`：`E_LEDGER_REPLAY`。
  - 重放的调用抛出的任何 `Exception`：`E_LEDGER_REPLAY`。
  - `claim` 记录的 `operation_id` 必须是 `op-` 加 32 位小写十六进制，并且还没有被别的操作用过；否则 `E_LEDGER_REPLAY`。
  - `open_ledger` 只抛出 `LedgerError`；`KeyboardInterrupt` 之类不是 `Exception` 的异常除外，它们照样冒出，连接照样关闭。
- **v2-3（修改 v1 第 4 条）创建**：
  - 路径检查与构造内存 Scheduler 同 v1，仍然在创建任何文件之前。
  - 用 `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)` 原子地新建文件，并立即 `fstat` 记下它的 `(st_dev, st_ino)`，然后关闭这个描述符：
    - 文件已存在（`FileExistsError`）：`E_LEDGER_EXISTS`，不改动它；
    - 其他 `OSError`：`E_LEDGER_OPEN`。
  - 然后按 v2-2 的方式（`mode=rw`）连接，设 pragma，在一个 `BEGIN IMMEDIATE` 事务里建表、写 `meta`。
  - 新建文件之后的任何失败：关闭连接；只在 `os.lstat(path)` 显示的 `(st_dev, st_ino)` 等于记下的身份时才删除路径上的文件，不看文件大小；报 `E_LEDGER_OPEN`（`KeyboardInterrupt` 之类原样冒出）。
- **v2-4 其余不变**：命令编码（不排序键、保留映射的键序）、哈希链、过期检测、失效状态与只读调用都按 v1。

### v1 的第 1–7 条（原文）

1. **`scheduler.py` 只做一处改动**：新增方法 `Scheduler._new_operation_id(self) -> str`，返回 `"op-" + uuid.uuid4().hex`；`claim` 改为调用它生成操作 id。其余代码、语义与私有属性一律不变。
2. **常量与错误**（`durable_ledger`）
   - `LEDGER_FORMAT = "sentinelmesh-ledger-v1"`。
   - `MUTATING_METHODS`：按这个顺序的 tuple：`seed_request`、`seed_task`、`claim`、`handoff`、`mark_running`、`renew`、`observe`、`settle`、`finish`、`fixture_verdict`、`unknown`、`release_proven_unstarted`、`recover`、`cancel_unstarted`、`stop`、`rollover`。
   - `LedgerError(Exception)`，带 `code` 与 `detail` 属性。
3. **日志文件**
   - 一个 SQLite 文件。每次连接都设 `PRAGMA journal_mode=DELETE`（返回值不是 `delete` 时报 `E_LEDGER_OPEN`）、`PRAGMA synchronous=FULL`、`PRAGMA fullfsync=ON`。
   - 两张表：
     - `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`，恰好两行：`format` = `LEDGER_FORMAT`，`generation` = 初始代际的十进制文本；
     - `commands(seq INTEGER PRIMARY KEY, command TEXT NOT NULL, sha256 TEXT NOT NULL, previous TEXT NOT NULL)`。
   - 哈希链：
     - 第 1 条的 `previous` 是空字符串，第 k 条的 `previous` 是第 k−1 条的 `sha256`；
     - `sha256` 是 SHA-256(`previous` 的 ASCII 字节 + `b"\n"` + `command` 的 UTF-8 字节) 的小写十六进制。
   - 一条命令是 JSON 对象：`{"method": 名称, "arguments": {...}}`，`claim` 另有 `"operation_id"`。
     - `arguments`：把调用绑定到 `Scheduler` 方法的参数名，补上默认值，包含全部参数。`Mapping` 记成 JSON 对象，保留它的迭代顺序；list 记成数组；str、int、bool、None 原样。
     - 编码：`json.dumps(command, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` 的 UTF-8 字节。**不排序键**。
4. **`create_ledger(path, *, generation=1) -> DurableScheduler`**
   - `path` 必须是不含 NUL 的非空精确 str，否则报 `LedgerError("E_LEDGER_PATH")`。
   - 先用 `generation` 构造内存 `Scheduler`。它的 `ContractError`（例如 `E_INTEGER`）原样冒出，这时不创建任何文件。
   - `path` 上已有任何东西（`os.lstat` 成功）：`E_LEDGER_EXISTS`，不改动它。
   - 在一个 `BEGIN IMMEDIATE` 事务里建表并写入 `meta`。表已存在（另一个创建者抢先）报 `E_LEDGER_EXISTS`，其他 SQLite 错误报 `E_LEDGER_OPEN`。
   - 返回 `seq` 为 0 的实例。
5. **`open_ledger(path) -> DurableScheduler`**
   - `path` 检查同第 4 条（`E_LEDGER_PATH`）。`path` 上什么都没有：`E_LEDGER_MISSING`，绝不新建文件。
   - 连接与 pragma 出错（例如不是数据库文件）：`E_LEDGER_OPEN`。
   - 在一个读事务里读出 `meta` 与按 `seq` 排序的全部命令：
     - 缺表：`E_LEDGER_FORMAT`；
     - `meta` 的键不恰好是 `format` 与 `generation`，`format` 不等于 `LEDGER_FORMAT`，或 `generation` 不是规范的十进制整数文本：`E_LEDGER_FORMAT`。
   - 逐条核对：`seq` 从 1 起连续，`previous` 与 `sha256` 符合第 3 条；不符：`E_LEDGER_CHAIN`。
   - 逐条重放，在用 `meta` 的代际新建的内存 Scheduler 上：
     - 命令按严格 JSON 解析：重复键、NaN 与 Infinity 都拒绝；
     - 必须是对象，键恰好是 `method` 与 `arguments`（`claim` 另有 `operation_id`，是 str）；`method` 属于 `MUTATING_METHODS`；`arguments` 是对象；
     - 调用 `method(**arguments)`。`claim` 时，Scheduler 下一次生成的操作 id 就是记录的那个，并且返回的 `operation_id` 必须等于它；
     - 以上任何一处不符，或者 Scheduler 拒绝这次调用：`E_LEDGER_REPLAY`。
   - 打开只读，不写入文件；失败时关闭连接。返回的实例 `seq` 为命令条数。
6. **`DurableScheduler`**
   - 有 `MUTATING_METHODS` 中的每个方法，参数的名称、种类与默认值都与 `Scheduler` 相同；`reserve` 与 `claim` 相同。
   - 只读：`generation`（属性）、`events`（属性）、`snapshot(request, task=None)`、`operation(operation)`。另有 `path`、`seq`（已应用的命令条数）、`close()`，并且可以当作上下文管理器（退出时关闭）。
   - 一次变更调用，在一把锁内按顺序进行：
     1. 实例已关闭或已失效时报 `E_LEDGER_UNAVAILABLE`；
     2. 用同样的参数调用内存 Scheduler 的同名方法。它的异常（包括 `ContractError` 与参数不对时的 `TypeError`）原样冒出，什么都不记录，实例照常可用；
     3. 成功之后按第 3 条构造并编码命令；
     4. 在一个 `BEGIN IMMEDIATE` 事务里追加：日志的最后一条必须正是本实例上次应用的那条（`seq` 与 `sha256` 都相同），否则报 `E_LEDGER_STALE`；插入第 `seq + 1` 条，提交；
     5. 第 3–4 步的任何失败都让实例失效：日志被推进报 `E_LEDGER_STALE`，其他情况报 `E_LEDGER_WRITE`；不是 `Exception` 的异常（例如 `KeyboardInterrupt`）同样让实例失效，并原样冒出；
     6. 返回 Scheduler 的结果。
   - 每个成功的变更调用都记录，包括按幂等键重放的 `claim`。
   - 只读调用：实例不可用时报 `E_LEDGER_UNAVAILABLE`，否则读内存，不碰日志。
   - `close()` 关闭连接，之后实例不可用。
7. **导入无副作用**：导入 `sentinelmesh.durable_ledger` 不导入 ctypes，不读文件、不读环境变量、不启动进程。

## Steps
1. 读 AGENTS.md、本合同、你上一轮写的 `sentinelmesh/durable_ledger.py` 与 `tests/test_durable_ledger.py`、规划端测试 `tests/test_durable_ledger_gaps.py` 与 v1 oracle。基线与 Repository facts 不符就停止并报告。
2. 按 v2-1 到 v2-3 修改 `sentinelmesh/durable_ledger.py`，更新文档字符串（内存调用的失效、打开不新建、创建的 `O_EXCL` 与身份核对）。
3. 更新 `tests/test_durable_ledger.py`：上一轮的用例照旧通过（需要时只做与 v2 规则一致的修正），并补下列规划端测试没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 参数绑定失败的 `TypeError` 之后，实例照常可用；
   - 内存调用抛出 `MemoryError`（用 `mock.patch.object(Scheduler, "_emit", …)` 注入）之后实例失效，重开之后这次调用不在日志里；
   - 打开时 pragma 失败（例如用一个包装连接让 `PRAGMA synchronous` 抛出 `sqlite3.OperationalError`）：报 `E_LEDGER_OPEN`，描述符不泄漏；
   - 创建时 `os.open` 抛出 `PermissionError`：报 `E_LEDGER_OPEN`，不留下文件；
   - 路径里有空格的账本能创建、写入、重开。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_durable_ledger_gaps.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要）：`python -m pytest -q tests/test_durable_ledger_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_durable_ledger.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要）：`python -c "import sys, sentinelmesh.durable_ledger; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01 与 V05 在基线上失败，是因为规划端测试里有 8 个测试函数守护 v2 的新规则。V02–V04 保护上一轮已经正确的行为与 Scheduler 的已有行为；V03 是你上一轮写的测试。

## Definition of done
- D01：内存调用的异常与实例失效（v2-1）；证据：V01、V03。
- D02：打开的 v2 规则：不新建、关闭连接、只报账本错误、代际与操作 id 的核对（v2-2）；证据：V01、V02、V03。
- D03：创建的 v2 规则：`O_EXCL`、身份核对的清理（v2-3）；证据：V01、V03。
- D04：v1 的其余规则不退化（v2-4）；证据：V02、V03、V04。
- D05：导入无副作用，已有模块不退化；证据：V04、V05、V08。
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
  - 实现约 7 分钟；
  - 测试约 8 分钟；
  - 运行检查约 5 分钟（规划端测试约 3 秒，全量约 150 秒）；
  - 执行报告约 3 分钟；
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
