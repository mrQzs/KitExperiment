# TASK: durable-ledger

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: bb93f04d2fec66f8f71097a153f43499072db2fa
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
新建 `sentinelmesh/durable_ledger.py`，把内存 `Scheduler` 包成持久账本：
- 每个成功的变更调用，先作为一条命令追加到 SQLite 命令日志（哈希链），追加成功才返回；
- 打开账本时按序重放日志，重建出与之前完全相同的状态，连操作 id 与事件序号都相同；
- 追加失败、或日志已被别的实例推进时，失败关闭。

`scheduler.py` 只把生成操作 id 的那一行提成一个方法。

并让规划端 oracle `tests/test_durable_ledger_oracle.py` 全部通过。

## Non-goals
- 不改内存 `Scheduler` 的语义与私有属性（唯一的改动见 Behavior contract 第 1 条）。不改 `supervised_operation.py`、`adapter_run.py`、`posix_supervisor.py`、`supervised_record.py`、`persistence.py` 或其他已有模块。
- 不记录运行描述，不做恢复驱动：那是下一份合同 `restart-recovery`。
- 不做快照与压缩；不用 WAL；不做多宿主、分布式锁或网络文件系统上的账本；不承诺断电之后的持久性。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle 与已有测试；不联网、不读取凭据。

## Repository facts
- 基线 `bb93f04` = master `35426a2` + 规划端 oracle（16 个用例，部分启动真实的本地 python 子进程）。oracle 在基线上收集失败，因为模块还不存在。
- 设计说明：`.agent/design/durable-ledger.md`，第 3.1、4 节，第 5 节（用户决定）与第 6 节（规划端决定 L1–L7）。
- 现状：
  - `Scheduler` 有 16 个变更方法，由一把 `RLock` 串行；被拒绝的调用不改变账本（scheduler-exact-inputs 的 oracle 穷举守护）。
  - 时间都由调用方显式传入（`now`）；唯一的非确定性来源是 `claim` 里的 `operation_id = "op-" + uuid.uuid4().hex`。
  - 只读方法：`generation`（属性）、`events`（属性，返回副本）、`snapshot(request, task=None)`、`operation(operation)`。`reserve = claim` 是别名。
- `_settle_mapping` 按映射自己的顺序逐项记录用量并发出事件。所以命令里的映射必须保留键序：按键排序编码，重放出的事件顺序就会不同。oracle 有专门的用例。
- oracle 的写法约束：
  - 子进程以仓库根为 cwd，`import sentinelmesh.durable_ledger`；
  - 损坏日志的用例直接用 `sqlite3` 读写 `commands` 表，并按第 3 条的规则自己计算哈希链；
  - 写入失败的用例把账本所在目录改成只读，让回滚日志建不出来。
- 规划端用一份草稿实现验证过（草稿未提交）：
  - oracle 16/16，全量 638 passed；
  - 22 个故意制造的偏差抓住 19 个，漏掉的 3 个都是等价写法：只核对日志头的序号、解析时接受浮点数（Scheduler 自己会拒绝浮点参数）、一个实际走不到的防御分支。
- SQLite：venv 的 Python 3.12 链接 3.53.1。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9。

## Scope
- allowed_modified_files:
  - sentinelmesh/scheduler.py
- allowed_new_files:
  - sentinelmesh/durable_ledger.py
  - tests/test_durable_ledger.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/persistence.py`, `tests/test_durable_ledger_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
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
1. 读 AGENTS.md、本合同、设计说明第 3–6 节、oracle，以及 `scheduler.py`（重点是 `claim`、`_settle_mapping` 与各个变更方法的签名）。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1 条修改 `sentinelmesh/scheduler.py`。
3. 按第 2–7 条新建 `sentinelmesh/durable_ledger.py`，模块文档字符串写明命令日志、重放与失败关闭。
4. 新建 `tests/test_durable_ledger.py`，覆盖下列 oracle 没有覆盖的用例：
   - 每个拒绝用例都先验证合法基线，再只改一处；
   - 几个线程在同一个实例上同时领取不同的 task：全部成功，重开之后事件相同；
   - 用 `reserve` 领取，重开之后操作仍在；
   - `meta` 的 generation 是 `"007"` 时报 `E_LEDGER_FORMAT`；
   - `create_ledger(path, generation=2**53)` 报 `E_JSON_INTEGER`，不留下文件；
   - 实例当作上下文管理器使用，退出之后不可用。
5. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_durable_ledger_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_durable_ledger.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -m pytest -q tests/test_scheduler_exact_inputs_oracle.py tests/test_scheduler_exact_inputs_probes.py tests/test_scheduler_exact_inputs.py tests/test_scheduler_supervised_oracle.py tests/test_scheduler_supervised_probes.py tests/test_scheduler_supervised.py tests/test_scheduler_supervised_gaps.py tests/test_scheduler_operation_view_oracle.py tests/test_scheduler_operation_view.py tests/test_reference.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.durable_ledger; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`

V01、V04、V07 在基线上失败，是因为模块还不存在；V02 是因为测试文件还不存在。V03 保护 Scheduler 的已有行为。

## Definition of done
- D01：`scheduler.py` 只提出 `_new_operation_id`（Behavior contract 第 1 条）；证据：V01、V03。
- D02：日志文件、命令编码与哈希链（第 2–3 条）；证据：V01、V02。
- D03：`create_ledger` 与 `open_ledger`，包括重放与各种拒绝（第 4–5 条）；证据：V01、V02。
- D04：持久账本的变更、过期检测、失效与只读调用（第 6 条）；证据：V01、V02。
- D05：导入无副作用，已有模块不退化（第 7 条）；证据：V03、V04、V07。
- D06：Step 4 的测试齐全，不自行做变异检查；证据：V02、review。
- D07：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
  - 实现约 10 分钟；
  - 测试约 8 分钟；
  - 运行检查约 4 分钟（oracle 约 10 秒，全量约 145 秒）；
  - 执行报告约 3 分钟；
  - 合计约 30 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
