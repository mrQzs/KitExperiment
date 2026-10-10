# TASK: ledger-compaction

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: ef3e46c4428a624d1f12f0f9b7f65bc39b05b922
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
给持久账本加**检查点与压缩**：`DurableScheduler.compact()` 在一个事务里把内存 Scheduler 的完整状态（含全部事件）写成检查点并删除它覆盖的命令；`open_ledger` 载入检查点之后只重放它之后的命令，结果与全量重放完全相同；账本格式升到 `sentinelmesh-ledger-v2`，v1 文件拒绝打开；`smctl ledger compact LEDGER` 调用它。
- 修改 `sentinelmesh/durable_ledger.py`（格式、`checkpoint` 表、状态编解码、`compact()`、日志头、打开路径）与 `sentinelmesh/ledger_cli.py`（`compact` 命令）；
- 让规划端 oracle `tests/test_ledger_compaction_oracle.py` 全部通过（它在 33 步场景的每一个前缀处压缩，再与从不压缩的账本逐项比对）；基线里 `tests/test_durable_ledger_oracle.py` 已改为钉住 v2，也要通过；
- 新建执行器测试 `tests/test_ledger_compaction.py`。

## Non-goals
- 不自动压缩，不做阈值；控制器的追加路径不看日志长度（规划端决定 K2）。
- 不做 `VACUUM`、归档表、迁移工具；不兼容读取 v1 文件（用户决定 4）。
- 不改 `sentinelmesh/scheduler.py`：状态的编码与恢复都在 `durable_ledger.py` 里，它导入 `scheduler` 的四个私有 dataclass（`_Operation`、`_Request`、`_Task`、`_Usage`）。
- 不改命令条目的哈希链（`entry_sha256`、`previous` 的规则）、`MUTATING_METHODS`、`_apply` 的规则（durable-ledger v1 第 6 条与 v2-1）、`create_ledger` 的 `O_EXCL` 与清理规则；`compact` 不是变更方法，不记命令、不发事件、不换代。
- 不改 `status`、`reference`、`stop`、`settle`、`resolve`、`resume` 的行为与输出；不改 `cli.py`。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与已有测试；不联网、不读取凭据、不调用 pi。

## Repository facts
- 基线 = master `cac3014`（设计说明 `.agent/design/ledger-compaction.md`：第 3 节方案、第 5 节用户决定、第 6 节规划端决定 K1–K10）+ 规划端 oracle 提交 `c9aa8aa`、`ef3e46c`，后者含：
  - `tests/test_ledger_compaction_oracle.py`：14 个用例（格式与表面、每个前缀处压缩后与从不压缩的账本比对全部只读视图、事件与内部状态、另一个进程重开、压缩不改变账本内容、空账本与重复压缩、别的进程压缩不让本实例失效而追加让它失效、过期实例不得压缩、压缩之后被杀、损坏与伪造的检查点、压缩失败失效、关闭的实例、命令行、不留日志文件且读不写、导入无副作用）；在基线上全部失败，因为 `compact` 与 `checkpoint` 表都还不存在；
  - `tests/test_durable_ledger_oracle.py` 的两处改动：`LEDGER_FORMAT` 钉为 `"sentinelmesh-ledger-v2"`；“另一种格式”的用例改成把 meta 的 format 写成 `sentinelmesh-ledger-v1` 并期望 `E_LEDGER_FORMAT`。在基线上这两个用例失败，其余通过。
- `sentinelmesh/durable_ledger.py`（约 730 行）的现状：
  - `LEDGER_FORMAT = "sentinelmesh-ledger-v1"`；`_SCHEMA` 是两条 `CREATE TABLE`（`meta`、`commands`）的 tuple，`create_ledger` 在 `BEGIN IMMEDIATE` 里逐条执行它再写两行 meta；
  - `entry_sha256(previous, command)` = SHA-256(`previous` 的 ASCII 字节 + 一个换行字节 + `command` 的 UTF-8 字节)；`_OPERATION_ID` 是 `op-` 加 32 位小写十六进制的正则；`_CANONICAL_INTEGER`、`_MAX_GENERATION_DIGITS`、`_stored_generation`、`_read_meta`（缺表 → `E_LEDGER_FORMAT`，键不是恰好 `format`、`generation` 或 format 不等于 `LEDGER_FORMAT` → `E_LEDGER_FORMAT`）、`_missing_table`、`_jsonable`（Mapping 保序转 dict、list/tuple 转 list、str/bool/int/None 原样、其他 `TypeError`）、`_rollback`、`_connect`；
  - `_strict_loads(text)`：重复键、NaN、Infinity、`RecursionError`、`ValueError`、其他异常一律 `E_LEDGER_REPLAY`；
  - `_verify_chain(rows)`：`seq` 从 1 连续、`previous` 从空串起、每行四列且是文本，`digest == entry_sha256(previous, command)`，否则 `E_LEDGER_CHAIN`；返回 `(commands, last_sha)`；
  - `_ReplayScheduler(Scheduler)`：`_recorded_operation_id` 注入；`_check_claim(result, operation_id, arguments, used)`：`used` 是 `{operation_id: (request, key)}`，同一个 id 的第二次 claim 必须是同样的 `(request, key)`，否则 `E_LEDGER_REPLAY`；`_replay(scheduler, command_text, used)`；
  - `DurableScheduler.__init__(path, scheduler, db, seq, last_sha)`；只读属性与方法都在 `self._lock` 内先 `_require_available()`（`self._db is None` → `E_LEDGER_UNAVAILABLE`）；`_invalidate()` 关闭连接并把 `_db` 置 None；`_apply` 先绑定参数、再调用内存方法（`ContractError` 原样冒出且实例可用，其他 `BaseException` 失效并原样冒出）、再 `_encode_command` + `_append`（`LedgerError` → 失效并原样冒出；其他 `Exception` → 失效并 `E_LEDGER_WRITE`；其他 `BaseException` → 失效并原样冒出）；
  - `_append(raw)`：`BEGIN IMMEDIATE`（`sqlite3.Error` → `E_LEDGER_WRITE`），然后读日志头 `SELECT seq, sha256 FROM commands ORDER BY seq DESC LIMIT 1`：`self._seq == 0` 时头必须为 None，否则头必须是 `(self._seq, self._last_sha)`，不符 `E_LEDGER_STALE`；`INSERT` 第 `seq + 1` 条；`COMMIT`；任何 `BaseException` 都 `_rollback` 再冒出；成功后 `_seq += 1`、`_last_sha = digest`；
  - `open_ledger(path)`：`_check_path`、`os.lstat`（缺失 `E_LEDGER_MISSING`）、`_connect`，然后在一个 `try` 里：`BEGIN`、`_read_meta`、`SELECT seq, command, sha256, previous FROM commands ORDER BY seq`（`sqlite3.Error` 缺表 → `E_LEDGER_FORMAT`，其他 → `E_LEDGER_OPEN`），`finally` 里 `_rollback`；`_stored_generation(meta)`；`_ReplayScheduler(generation=…)`（`ContractError` → `E_LEDGER_FORMAT`）；`_verify_chain(rows)`；逐条 `_replay`；任何 `BaseException` 都先关闭连接再冒出；返回 `DurableScheduler(location, scheduler, db, len(commands), last_sha)`；
  - `__all__ = ["LEDGER_FORMAT", "MUTATING_METHODS", "DurableScheduler", "LedgerError", "create_ledger", "open_ledger"]`；模块导入无副作用。
- `sentinelmesh/scheduler.py` 的私有状态（本合同只读它，不改它）：`_generation`（int）；`_requests: dict[str, _Request]`，`_Request` 的字段依次为 `repo_id: str`、`caps: dict[str, int]`、`max_concurrency: int`、`stopped: str | None`、`stop_reasons: list[str]`；`_tasks: dict[tuple[str, str], _Task]`（键 `(request, task)`），`_Task` 的字段 `caps: dict[str, int]`、`max_attempts: int`、`guard_version: int`、`fixture_eligible: bool`；`_operations: dict[str, _Operation]`（按领取顺序），`_Operation` 的 17 个字段依次为 `operation_id: str`、`request_id: str`、`task_id: str`、`plan_version: int`、`guard_version: int`、`idempotency_key: str`、`request_digest: str`、`owner: str`、`generation: int`、`lease_until: int`、`state: str`、`reservation: dict[str, int]`、`resource_keys: tuple[str, ...]`、`candidate_hash: str | None`、`usage: dict[str, _Usage]`、`run: dict | None`、`started: bool`；`_Usage` 的字段 `call_key: str`、`revision: int`、`amount: int | None`、`final: bool`、`evidence_hash: str`；`_idempotency: dict[tuple[str, str], str]`（键 `(request, key)`，值操作 id；每个操作恰好被一个键指向）；`_resource_holds: dict[tuple[str, str], str]`（键 `(repo_id, resource)`，值操作 id，`resource` 必在该操作的 `resource_keys` 里）；`_events: list[dict]`，每个事件恰好五个键 `seq`（从 1 连续）、`operation_id`（str 或 None）、`type`（str）、`generation`（int）、`payload`（任意 JSON）；`_lock`。操作的状态只会是这十个之一：`RESERVED`、`HANDOFF_ACCEPTED`、`RUNNING`、`START_UNKNOWN`、`REVIEW_PENDING`、`ACCEPTED`、`REJECTED`、`ABANDONED`、`NOT_STARTED`、`CANCELLED_NOT_STARTED`。`Scheduler(generation=g)` 要求 `g` 是精确的正整数且不超过 2**53−1。`dataclasses.asdict` 能把这四个 dataclass 转成嵌套 dict（tuple 保持为 tuple）。
- `sentinelmesh/ledger_cli.py`（约 400 行）：`_with_ledger(args, work)` 打开账本、`work(ledger)`、`finally` 关闭、成功时 `_emit(result)`，`ContractError` 与 `LedgerError` 都 `_refuse`（stderr 一行 `{"error", "detail"}`，返回 3）；`register(subparsers)` 在 `ledger` 组下注册 `status`、`reference`、`stop`、`settle`、`resolve`、`resume`，每个子命令的第一个位置参数是 `ledger`；`cli.main` 对 argparse 错误 `SystemExit(2)`。
- SQLite 事实（本机 Python 3.12.10 自带）：`cursor.rowcount` 在 `DELETE` 之后是删掉的行数；`INTEGER PRIMARY KEY` 列接受负数与 0；`sqlite3.Error` 的文本含 `no such table` 时 `_missing_table` 为真；`BEGIN IMMEDIATE` 之内可以读写多张表。
- 本机实测（设计说明第 1 节）：1000 个任务、7001 条命令的账本打开要 1.16 s，而载入同样状态的 JSON 要 0.014 s；状态 JSON 的大小与日志相当（2.9 MB 对 3.0 MB），其中事件占一半。
- 规划端用一份草稿实现验证过（草稿未提交）：oracle 14/14（93 个子用例）；全量 699 passed / 289 skipped；46 个故意制造的偏差全部被 oracle 抓住（格式仍为 v1、摘要漏掉 `seq`、压缩不删命令或少删一条、压缩不核对日志头、日志头忽略检查点或优先取检查点、链只按每行自己的 `previous` 核对、尾部从 1 编号、打开后的 `seq` 只数尾部、不核对检查点摘要、接受两行检查点、检查点里的操作 id 不登记为已用、接受低于 meta 的代际、bool 当 int、多字段或少字段、任意状态、键与 `operation_id` 不一致、任意形状的操作 id、幂等条目不指向自己的操作、操作没有幂等键、资源占用不在 `resource_keys` 里、事件不从 1 编号、事件多键、状态多键、检查点宽松解析、编码时排序键、压缩不替换旧检查点、压缩失败不失效、`removed` 报 0、链接不合序号、负序号、建表少一张、可选字段接受任意类型、caps 与列表元素不检查、任务指向未知请求、任务重复、三元组任意长度、事件/幂等键/`started`/运行描述不恢复、命令行少一个键或改名）。
- 工具链：Windows Server 2025，`python` 3.12.10（系统安装，没有 venv）与 pytest 9.1.1；基线上 `python -m pytest -q` 因 oracle 失败而失败，其余通过（原生 POSIX 用例在 Windows 上跳过，这是预期）。

## Scope
- allowed_modified_files:
  - sentinelmesh/durable_ledger.py
  - sentinelmesh/ledger_cli.py
- allowed_new_files:
  - tests/test_ledger_compaction.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_ledger_compaction_oracle.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_durable_ledger.py`, `tests/test_durable_ledger_gaps.py`, `tests/test_ledger_cli.py`, `tests/test_ledger_cli_oracle.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/cli.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/persistence.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有“是 str / int / bool / dict / list”都是精确类型（`type(x) is str` 等；bool 不是 int）。“失效”指 `_invalidate()`：连接关闭、之后的一切调用（含只读）报 `E_LEDGER_UNAVAILABLE`。下面的私有函数名是草稿的名字，可以自定，公开名字不可。

1. **格式与表**：`LEDGER_FORMAT = "sentinelmesh-ledger-v2"`。`_SCHEMA` 增加第三条 `CREATE TABLE checkpoint(seq INTEGER PRIMARY KEY, last_sha TEXT NOT NULL, state TEXT NOT NULL, sha256 TEXT NOT NULL)`；`create_ledger` 的其余规则不变（新账本有三张表、meta 的 format 是 v2、`checkpoint` 表为空）。`open_ledger` 对 meta 的规则不变（format 不等于 v2 → `E_LEDGER_FORMAT`，所以 v1 文件被拒绝）；`checkpoint` 表缺失 → `E_LEDGER_FORMAT`。不迁移。
2. **检查点的摘要**：新增公开函数 `checkpoint_sha256(seq: int, last_sha: str, state: str) -> str`：SHA-256(`"%d\n%s\n" % (seq, last_sha)` 的 ASCII 字节 + `state` 的 UTF-8 字节) 的小写十六进制。`__all__` 加入 `checkpoint_sha256` 与 `entry_sha256`。命令条目的 `entry_sha256` 与链的规则不变。
3. **状态的编码**（`_encode_state(scheduler) -> str`）：一个 JSON 对象，恰好七个键，`json.dumps(_jsonable(state), separators=(",", ":"), ensure_ascii=False, allow_nan=False)`，**不排序键**：
   - `generation`：`_generation`；
   - `requests`：对象，`{request_id: dataclasses.asdict(_Request)}`，按 `_requests` 的顺序；
   - `tasks`：数组，每项 `[request_id, task_id, dataclasses.asdict(_Task)]`，按 `_tasks` 的顺序；
   - `operations`：对象，`{operation_id: dataclasses.asdict(_Operation)}`，按 `_operations` 的顺序（`usage` 的每项是 `_Usage` 的 dict，`resource_keys` 成为数组，`run` 是描述或 null）；
   - `idempotency`：数组，每项 `[request_id, key, operation_id]`，按 `_idempotency` 的顺序；
   - `resource_holds`：数组，每项 `[repo_id, resource, operation_id]`，按 `_resource_holds` 的顺序；
   - `events`：`_events` 原样。
   编码失败（`TypeError`、`ValueError`、`UnicodeError`）→ `E_LEDGER_WRITE`。
4. **状态的恢复**（`_decode_state(text, stored_generation) -> _ReplayScheduler`）。先 `_strict_loads(text, "E_LEDGER_CHECKPOINT")`（`_strict_loads` 增加一个错误码参数，默认仍是 `E_LEDGER_REPLAY`；重复键、NaN、Infinity、`RecursionError`、`ValueError`、其他异常都报给定的码）。然后依次检查，任何一处不符都是 `E_LEDGER_CHECKPOINT`，不重算语义（预算、迁移都不再证明）：
   1. 顶层是 dict 且键恰好是那七个；
   2. `generation` 是 int，且不小于 `stored_generation`（meta 的代际）；`_ReplayScheduler(generation=…)` 的 `ContractError` 也是 `E_LEDGER_CHECKPOINT`；
   3. `requests` 是 dict；每个值是 dict 且键恰好是 `_Request` 的字段名（用 `dataclasses.fields`）；`repo_id` str、`caps` 是 dict 且每个值是 int、`max_concurrency` int、`stopped` str 或 None、`stop_reasons` 是 list 且每项 str；
   4. `tasks` 是 list；每项是长度 3 的 list `[request, task, fields]`，`request`、`task` 都是 str，`request` 必须在 `requests` 里，`(request, task)` 不得重复；`fields` 的键恰好是 `_Task` 的字段名；`caps` 同上、`max_attempts` int、`guard_version` int、`fixture_eligible` bool；
   5. `operations` 是 dict；每个键必须匹配 `_OPERATION_ID`；值的键恰好是 `_Operation` 的字段名，且 `operation_id` 等于键；`request_id`、`task_id` 都是 str 且 `(request_id, task_id)` 必须在 `tasks` 里；`state` 是 str 且属于第 Repository facts 列出的十个状态；`usage` 是 dict，每项的键恰好是 `_Usage` 的字段名，`call_key` str、`revision` int、`amount` int 或 None、`final` bool、`evidence_hash` str；`run` 是 dict 或 None；`plan_version`、`guard_version`、`generation`、`lease_until` int；`idempotency_key`、`request_digest`、`owner` str；`reservation` 是 dict 且每个值 int；`resource_keys` 是 list 且每项 str；`candidate_hash` str 或 None；`started` bool；
   6. `idempotency` 是 list；每项是长度 3 的 list 且三项都是 str；指向的操作必须存在，且该操作的 `request_id` 等于第一项、`idempotency_key` 等于第二项；`(request, key)` 不得重复；条目数必须等于操作数（每个操作恰好被一个键指向）；
   7. `resource_holds` 是 list；每项是长度 3 的 list 且三项都是 str；指向的操作必须存在，该操作所属请求的 `repo_id` 等于第一项，第二项必须在该操作的 `resource_keys` 里；`(repo_id, resource)` 不得重复；
   8. `events` 是 list；每项是 dict 且键恰好是 `seq`、`operation_id`、`type`、`generation`、`payload`；第 i 项的 `seq` 是 int 且等于 i（从 1 起）；`operation_id` str 或 None；`type` str；`generation` int；`payload` 不检查。
   通过之后，直接设置 `_ReplayScheduler` 的私有属性：`_requests`（`_Request(**…)`，`caps` 为新 dict、`stop_reasons` 为新 list）、`_tasks`（键还原成元组，`_Task(**…)`）、`_operations`（`_Operation(**…)`，`usage` 的每项 `_Usage(**…)`，`resource_keys` 还原成 tuple，`reservation` 为新 dict，`run` 为深拷贝或 None）、`_idempotency`、`_resource_holds`（键还原成元组）、`_events`（list 原样）。映射的顺序都保持 JSON 里的顺序。
5. **读检查点行**（`_read_checkpoint(db)`）：`SELECT seq, last_sha, state, sha256 FROM checkpoint`（`sqlite3.Error`：缺表 → `E_LEDGER_FORMAT`，其他 → `E_LEDGER_OPEN`）。没有行 → None。多于一行 → `E_LEDGER_CHECKPOINT`。恰好一行时：不是四列 → `E_LEDGER_CHECKPOINT`；`seq` 不是 int 或小于 0 → `E_LEDGER_CHECKPOINT`；`last_sha`、`state`、`sha256` 有不是 str 的 → `E_LEDGER_CHECKPOINT`；`seq == 0` 时 `last_sha` 必须是空串，`seq > 0` 时 `last_sha` 必须是 64 位小写十六进制，否则 `E_LEDGER_CHECKPOINT`；`sha256 != checkpoint_sha256(seq, last_sha, state)` → `E_LEDGER_CHECKPOINT`。返回 `(seq, last_sha, state)`。
6. **打开**（`open_ledger`，其余不变）：读事务里依次 `_read_meta`、`_read_checkpoint`、读全部命令（`sqlite3.Error` 的映射同现状），`finally` 回滚；`_stored_generation`；**总是**先 `_ReplayScheduler(generation=stored)`（`ContractError` → `E_LEDGER_FORMAT`，这条守住 meta 代际不变）；`used = {}`、`start_seq, previous = 0, ""`；有检查点时：`start_seq, previous, state = checkpoint`，`scheduler = _decode_state(state, stored)`，并把检查点里的每个操作登记进 `used`：`used[operation_id] = (op.request_id, op.idempotency_key)`；然后 `_verify_chain(rows, start_seq, previous)`：`_verify_chain` 增加两个参数，`seq` 从 `start_seq + 1` 起连续、第一条的 `previous` 必须等于给定的 `previous`，其余规则不变（不符 `E_LEDGER_CHAIN`）；逐条 `_replay(scheduler, …, used)`（检查点里的操作 id 被尾部的 claim 以别的 `(request, key)` 复用 → `E_LEDGER_REPLAY`，这是 `_check_claim` 现有的规则）；返回 `DurableScheduler(location, scheduler, db, start_seq + len(commands), last_sha)`，没有尾部命令时 `last_sha` 就是检查点的 `last_sha`。失败时关闭连接的规则不变。`seq` 属性的含义改为“状态包含的最后一条命令的序号”（未压缩的账本数值不变），文档字符串照此改。
7. **日志头**（`_head(db) -> (int, str)`）：`commands` 的最后一条 `(seq, sha256)`；没有命令时 `checkpoint` 的 `(seq, last_sha)`；都没有时 `(0, "")`。`_append` 的头核对改为 `_head(db) != (self._seq, self._last_sha)` → `E_LEDGER_STALE`（原来的两分支逻辑由它涵盖）。于是别的实例压缩之后，本实例的下一次追加照常成功（用户决定 3），而别的实例追加之后本实例失效（现状）。
8. **`DurableScheduler.compact(self) -> dict`**（不加入 `MUTATING_METHODS`，不用 `_apply`，不记命令、不发事件）。在 `self._lock` 内：
   1. `_require_available()`；
   2. `state = _encode_state(self._scheduler)`；`digest = checkpoint_sha256(self._seq, self._last_sha, state)`；
   3. `BEGIN IMMEDIATE`（`sqlite3.Error` → `E_LEDGER_WRITE`）；
   4. 事务内：`_head(db) != (self._seq, self._last_sha)` → `E_LEDGER_STALE`；`DELETE FROM checkpoint`；`INSERT INTO checkpoint(seq, last_sha, state, sha256) VALUES (self._seq, self._last_sha, state, digest)`；`removed = DELETE FROM commands WHERE seq <= self._seq` 的 `rowcount`；`COMMIT`；事务内的任何 `BaseException` 都先 `_rollback(db)` 再冒出；
   5. 第 2–4 步的失败：`LedgerError` → 失效并原样冒出；其他 `Exception` → 失效并 `E_LEDGER_WRITE`；其他 `BaseException` → 失效并原样冒出；
   6. 成功返回 `{"seq": self._seq, "last_sha": self._last_sha, "sha256": digest, "removed": removed}`；`_seq` 与 `_last_sha` 不变，实例仍可用。
   `seq` 为 0 的账本也可以压缩（检查点 `(0, "")`）；重复压缩允许，`removed` 为 0，摘要相同。
9. **命令行**：`smctl ledger compact LEDGER`，由 `ledger_cli.register` 注册，只有位置参数 `ledger`。经 `_with_ledger`：`result = ledger.compact()`，输出恰好 7 个键：`command`（`"compact"`）、`ledger`（命令行给的路径原样）、`generation`（`ledger.generation`）、`seq`、`last_sha`、`sha256`、`removed`（取自 `result`）。账本拒绝（含 `E_LEDGER_MISSING`、`E_LEDGER_STALE`）退出码 3，stderr 一行 `{"error", "detail"}`，stdout 不输出；参数错误退出码 2。
10. **兼容性**：`status`、`reference`、`stop`、`settle`、`resolve`、`resume` 的行为与输出不变（`status` 在压缩前后的输出逐字相同）；durable-ledger 的全部现有规则不变（v1 第 1–7 条、v2-1 到 v2-4，除本合同明确修改的 `LEDGER_FORMAT`、`_SCHEMA`、`_verify_chain` 的参数、`_append` 的头核对、`open_ledger` 的流程与 `seq` 的含义）；导入 `durable_ledger` 与 `ledger_cli` 无副作用（V07）。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 2、3、5、6 节、oracle，以及 `durable_ledger.py` 全文、`scheduler.py` 的四个 dataclass 与 `__init__`、`ledger_cli.py` 的 `_with_ledger` 与 `register`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–8 条改 `durable_ledger.py`（更新模块文档字符串：检查点、v2、不迁移），按第 9 条在 `ledger_cli.py` 里加 `compact`。
3. 新建 `tests/test_ledger_compaction.py`，覆盖下列 oracle 没有逐个覆盖的用例（每个拒绝用例先验证合法基线，再只改一处；错误码全等断言；账本用 `create_ledger` 在临时目录里建；需要改文件时直接用 `sqlite3` 改，摘要用自己算的 `hashlib`，不调用被测模块的 `checkpoint_sha256` 来生成期望值）：
   - 表面：`LEDGER_FORMAT`；`compact` 不在 `MUTATING_METHODS`；`DurableScheduler.compact` 的签名只有 `self`；新账本的三张表与 `checkpoint` 的四列；`checkpoint_sha256` 对一组固定输入的值等于自己用 `hashlib` 算的；
   - 状态的形状：压缩后读出 `state`，断言恰好七个键；`requests` 按播种顺序、`operations` 按领取顺序（至少三个请求、三个操作，顺序不是字母序）；`operations` 每项恰好 17 个字段、`usage` 每项恰好 5 个字段；`tasks`、`idempotency`、`resource_holds` 是三元组数组；`events` 等于 `ledger.events`；一个带运行描述的操作的 `run` 原样在内；
   - 顺序经检查点保持：预留按非排序顺序给出（例如 `{"dispatches": 1, "tokens": …, "cost": …}`），`settle` 一个映射，压缩、重开之后 `operation()["reservation"]`、`operation_usage()` 的键序、`request_ids()`、`operation_ids()` 都与压缩前相同；
   - `compact` 的返回值：四个键；`removed` 在“压缩、再追加 3 条、再压缩”时是 3；`seq` 为 0 时的检查点；压缩之后 `seq`、`events`、`generation`、`stop_reasons` 不变；
   - 第 4 条的每一项至少一个拒绝用例（各自独立的文件副本，重算摘要让它通过第 5 条）：顶层不是对象、多一个键、少一个键、重复键、`generation` 小于 meta 代际、`generation` 是 bool、`generation` 超过 2**53−1；每个 dataclass 各一个多字段与一个少字段；`final` 为 1、`started` 为 1、`fixture_eligible` 为 1（bool 字段给 int）、`max_attempts` 为 True（int 字段给 bool）、`amount` 为 "7"、`stopped` 为 0、`candidate_hash` 为 0、`run` 为 []、`caps` 的值为 1.0、`resource_keys` 含 int、`stop_reasons` 含 None；未知状态；操作 id 形状不对、键与 `operation_id` 不一致；操作指向未知任务；任务指向未知请求、任务重复；幂等条目指向未知操作、key 不符、重复、少一条、不是三元组；资源占用指向未知操作、资源不在 `resource_keys`、repo 不符、重复；事件序号错、事件少键、事件多键；
   - 第 5 条：两行、摘要不符、`seq` 为 −1、`seq` 为 0 带链接、`seq` 大于 0 链接为空、链接不是 64 位十六进制、`state` 不是文本（用 `UPDATE checkpoint SET state=1`）；
   - 第 6 条：尾部第一条 `seq` 不是检查点 `seq + 1`（多一、少一）、尾部第一条 `previous` 不等于 `last_sha`、尾部中间断链；有检查点但 meta 代际是 0 → `E_LEDGER_FORMAT`（不是 `E_LEDGER_CHECKPOINT`）；尾部 claim 复用检查点里的操作 id → `E_LEDGER_REPLAY`；
   - 第 7 条：实例 A 开着，实例 B（同进程）压缩，A 追加成功且 `seq` 加一、新行的 `previous` 是检查点的 `last_sha`；B 压缩后再追加一条，A 的下一次追加 `E_LEDGER_STALE` 并失效；过期实例的 `compact` 报 `E_LEDGER_STALE`、失效、文件没有检查点；
   - 第 8 条：`compact` 在关闭的实例上 `E_LEDGER_UNAVAILABLE`；用包装 `sqlite3.connect` 的办法让 `INSERT INTO checkpoint`、`DELETE FROM commands`、`COMMIT` 之前的某条语句抛出 `sqlite3.OperationalError`（参照 `tests/test_durable_ledger_gaps.py` 的写法）→ `E_LEDGER_WRITE`、实例失效、重开后没有检查点且命令齐全；让 `_encode_state` 抛出 `ValueError`（`mock.patch.object`）→ 同上；
   - 第 9 条：输出的 7 个键与值；`status` 在压缩前后逐字相同；`E_LEDGER_MISSING` 退出码 3；缺 `LEDGER` 退出码 2；一次经 `subprocess` 跑 `python -m sentinelmesh.cli ledger compact …`；
   - 一次在另一个进程里重开压缩过的账本并打印 `operation_ids()`、`events` 的条数与 `seq`，与本进程一致。
4. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_ledger_compaction_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_ledger_compaction.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要，基线应失败）：`python -m pytest -q tests/test_durable_ledger_oracle.py tests/test_durable_ledger.py tests/test_durable_ledger_gaps.py tests/test_ledger_cli.py tests/test_ledger_cli_oracle.py tests/test_ledger_reference.py tests/test_ledger_reference_oracle.py tests/test_restart_recovery.py tests/test_restart_recovery_oracle.py tests/test_request_resume.py tests/test_request_resume_oracle.py tests/test_resume_holds.py tests/test_resume_holds_oracle.py tests/test_explicit_holds.py tests/test_explicit_holds_oracle.py` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.durable_ledger, sentinelmesh.ledger_cli; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要，基线应失败）：`python -m sentinelmesh.cli ledger compact --help` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`compact`

V01、V03、V04 在基线上失败，是因为 `compact` 与 `checkpoint` 表还不存在、`LEDGER_FORMAT` 还是 v1（durable-ledger oracle 已钉住 v2）；V02 是因为测试文件还不存在；V08 是因为 argparse 不认识这个子命令。V03 保护账本的全部现有规则、六个已有命令、重启恢复与运维裁定；V07 保护导入无副作用。

## Definition of done
- D01：格式 v2、`checkpoint` 表、`checkpoint_sha256`（Behavior contract 第 1–2 条）；证据：V01、V02、V03。
- D02：状态的编码与恢复的全部校验（第 3–4 条）；证据：V01、V02。
- D03：检查点行的读取与打开的流程，`seq` 的含义（第 5–6 条）；证据：V01、V02、V03。
- D04：日志头与 `compact()` 的事务、失败与返回值（第 7–8 条）；证据：V01、V02。
- D05：命令行 `compact`（第 9 条）；证据：V01、V02、V08。
- D06：兼容性与导入无副作用（第 10 条）；Step 3 的测试齐全，不自行做变异检查；证据：V03、V04、V07、review。
- D07：所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 pi。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 6 分钟；
  - 实现约 8 分钟（两处改动合计约 260 行，其中状态校验约 120 行）；
  - 测试约 10 分钟；
  - 运行检查约 4 分钟（oracle 约 30 秒，全量约 70 秒）；
  - 执行报告约 3 分钟；
  - 合计约 31 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
