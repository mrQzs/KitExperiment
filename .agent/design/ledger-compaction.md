# 设计说明：账本检查点与压缩（单宿主）

状态：用户已决定第 5 节（2026-10-09，均选推荐项）；规划端决定见第 6 节。
前置：持久账本（`.agent/design/durable-ledger.md`）、重启恢复、`smctl ledger`（`.agent/design/ledger-cli.md`）都已集成。

## 1. 为什么做

- 用户的候选清单第 3 项：“账本快照与压缩：当初推迟了，目前不急。”durable-ledger 的规划端决定 L3 写的是“不做快照与压缩。重放的代价与命令数成正比，以后需要时再做。”
- 现在每次打开账本——控制器重启、每一条 `smctl ledger` 命令——都从第 1 条命令重放到最后一条。`claim` 的重放要扫描全部操作（预算汇总、活动任务、尝试次数、并发），所以重放时间随命令数超线性增长。本机实测（Windows Server 2025，Python 3.12.10；每个任务 7 条命令：`seed_task`、`claim`、带运行描述的 `handoff`、`mark_running`、`settle`、`finish`、`fixture_verdict`）：

| 任务数 | 命令数 | 日志文件 | 打开（重放） | 状态 JSON | 载入状态 JSON |
| --- | --- | --- | --- | --- | --- |
| 100 | 701 | 320 KB | 0.03 s | 286 KB | 0.002 s |
| 300 | 2101 | 920 KB | 0.13 s | 863 KB | 0.004 s |
| 1000 | 7001 | 3.0 MB | 1.16 s | 2.9 MB | 0.014 s |

  按这个趋势，一万个任务约七万条命令，打开要一两分钟，而载入状态约 0.15 s。
- 账本是运维对账的入口（`status`、`reference` 每次都要打开），重放变慢直接拖慢每一条命令。

## 2. 现状

- 命令日志：一个 SQLite 文件，表 `meta`（`format`、`generation`）与 `commands`（`seq`、`command`、`sha256`、`previous`）；哈希链 `sha256` = SHA-256(`previous`、一个换行符、`command`)，第 1 条的 `previous` 是空串。
- 打开：读 `meta`、读全部命令、核对 `seq` 从 1 连续与链、用 `meta` 的代际构造 `_ReplayScheduler`、逐条重放。追加：`BEGIN IMMEDIATE`，日志头（最后一条的 `seq` 与 `sha256`）必须正是本实例的，否则 `E_LEDGER_STALE` 并失效。
- 内存 Scheduler 的全部状态：`_generation`；`_requests`（dataclass `_Request`：`repo_id`、`caps`、`max_concurrency`、`stopped`、`stop_reasons`）；`_tasks`（键 `(request, task)`，`_Task`：`caps`、`max_attempts`、`guard_version`、`fixture_eligible`）；`_operations`（`_Operation`，17 个字段，含 `usage` 的 `_Usage`、运行描述 `run`、`started`）；`_idempotency`（键 `(request, key)` → 操作 id）；`_resource_holds`（键 `(repo_id, resource)` → 操作 id）；`_events`（`seq` 从 1 连续）。全部是 JSON 原样还原的类型（str、int、bool、None、dict、list/tuple）；三个映射的键是元组。关闭的操作永远留在 `_operations` 里：尝试次数、幂等键与 `E_ACTIVE_TASK` 都要用它们。
- 状态的大小与日志相当：1000 个任务时状态 2.9 MB 对日志 3.0 MB，其中事件占 51%、操作占 43%。`Scheduler.events` 每次调用做一次 JSON 往返拷贝（6001 条事件约 14 ms）。
- 现有测试：`tests/test_durable_ledger_oracle.py` 第 168 行钉住 `LEDGER_FORMAT == "sentinelmesh-ledger-v1"`；18 个测试文件直接读 Scheduler 的私有属性，所以私有属性的名字与类型实际上是被测试钉住的。
- 07 第 8.3 节：R3 核对账本时发现矛盾进入人工恢复，“不自动删除多余行”；R7 补偿时“原证据未改写”。第 8 章末：数据库备份要用 SQLite 官方备份接口或一致快照，不要只复制正在写入的主文件。
- 名字冲突：`Scheduler.snapshot(request, task)` 已经是“预算快照”。本说明把用户说的“快照”叫**检查点**（checkpoint）：日志某个序号处的完整状态；“压缩”（compact）指写入检查点并删除它之前的命令。

## 3. 拟议

### 3.1 检查点与压缩

- 新表 `checkpoint(seq INTEGER PRIMARY KEY, last_sha TEXT NOT NULL, state TEXT NOT NULL, sha256 TEXT NOT NULL)`，至多一行：
  - `seq`：检查点覆盖到的最后一条命令的序号（0 表示创建时的空状态）；
  - `last_sha`：第 `seq` 条命令的 `sha256`（`seq` 为 0 时是空串）。命令链不变：第 `seq + 1` 条的 `previous` 仍然是它，命令的摘要从不重算；
  - `state`：内存 Scheduler 在第 `seq` 条命令之后的完整状态（第 3.2 节）；
  - `sha256` = SHA-256(`seq` 的十进制文本、一个换行符、`last_sha`、一个换行符、`state`)：行里除摘要自己之外的每一列都在摘要里，改动序号、链接或状态都能查出来。
- `DurableScheduler.compact()`：在实例自己的锁内、一个 `BEGIN IMMEDIATE` 事务里：核对日志头是本实例的 `(seq, sha)`（否则 `E_LEDGER_STALE` 并失效）；把内存状态编码成 `state`；替换 `checkpoint` 行；`DELETE FROM commands WHERE seq <= ?`；提交。失败按追加的规则：实例失效，`E_LEDGER_WRITE`（日志被推进时 `E_LEDGER_STALE`）。返回 `{"seq", "last_sha", "sha256", "removed"}`（`removed` 是删掉的命令条数）。不记命令、不发事件、不换代；实例之后照常可用，它的 `(seq, sha)` 没有变。
- 打开：读 `meta`、读 `checkpoint`（0 或 1 行，多于 1 行拒绝）、读全部命令。有检查点时：核对 `sha256`、`seq` 与 `last_sha` 的形状；命令必须从 `seq + 1` 起连续，第一条的 `previous` 等于 `last_sha`，链照旧核对；用 `state` 恢复一个 `_ReplayScheduler`（第 3.3 节），把检查点里的操作 id 登记为已用，再重放尾部。没有检查点时与今天相同。实例的 `seq` 是绝对序号：检查点的 `seq` 加尾部条数。
- 日志头的定义改成：最后一条命令的 `(seq, sha256)`；没有命令时是检查点的 `(seq, last_sha)`；两者都没有时是 `(0, "")`。追加与压缩都用它核对。于是别的进程压缩账本不改变状态身份，开着账本的控制器下一次追加照常成功（第 5 节问题 3）。

### 3.2 状态的编码

JSON 对象，键序固定，`json.dumps(separators=(",", ":"), ensure_ascii=False, allow_nan=False)`，**不排序键**：映射保持迭代顺序（请求按播种顺序、操作按领取顺序、用量按记录顺序），重放后的只读视图才会与写入方相同。

```
{"generation": int,
 "requests": {request_id: {"repo_id", "caps", "max_concurrency", "stopped", "stop_reasons"}},
 "tasks": [[request_id, task_id, {"caps", "max_attempts", "guard_version", "fixture_eligible"}], ...],
 "operations": {operation_id: {_Operation 的全部字段；"usage": {维度: {"call_key", "revision", "amount", "final", "evidence_hash"}}；
                               "resource_keys" 是列表；"run" 是描述或 null}},
 "idempotency": [[request_id, key, operation_id], ...],
 "resource_holds": [[repo_id, resource, operation_id], ...],
 "events": [事件, ...]}
```

恢复时的校验，任何一处不符都是 `E_LEDGER_CHECKPOINT`：严格 JSON（重复键、NaN、Infinity 拒绝）；顶层恰好这七个键；每个对象恰好对应 dataclass 的字段；每个字段的精确类型（bool 不是 int；None 只出现在 `stopped`、`candidate_hash`、`run`、`amount`）；`generation` 是 Scheduler 构造函数接受的整数；操作 id 符合 `op-` 加 32 位小写十六进制且与键一致；`tasks` 的请求、操作的请求与任务、`idempotency` 与 `resource_holds` 指向的操作都存在，`idempotency` 的 `(request, key)` 与操作的 `request_id`、`idempotency_key` 一致；`events` 的 `seq` 从 1 连续。深层语义不重算（例如预算是否超限）：检查点由本模块从一个已经通过全部守卫的 Scheduler 写出，校验防的是损坏与伪造的形状，不是重新证明账本的历史。

### 3.3 恢复到内存

`_ReplayScheduler(generation=状态的代际)` 之后直接设置私有属性：`_requests` 用 `_Request(**fields)`；`_tasks` 的键还原成元组；`_operations` 用 `_Operation(**fields)`，其中 `usage` 的每项是 `_Usage(**u)`、`resource_keys` 还原成元组、`run` 深拷贝；`_idempotency`、`_resource_holds` 的键还原成元组；`_events` 原样。`scheduler.py` 不改：编码与恢复都在 `durable_ledger.py`，它导入四个私有 dataclass，字段集合取 `dataclasses.fields()`，所以 Scheduler 加一个字段，旧检查点就因字段不符而拒绝打开——失败关闭。这条耦合写进标准遗留：改动 Scheduler 的私有状态（字段增减、类型）之前先给账本格式升版本，与 `_runtime_text`/`InvocationSpec` 那条同类。

### 3.4 格式版本

`LEDGER_FORMAT = "sentinelmesh-ledger-v2"`：`create_ledger` 建三张表；`open_ledger` 只接受 v2，缺 `checkpoint` 表是 `E_LEDGER_FORMAT`。v1 文件拒绝打开（第 5 节问题 4），没有迁移工具。升版本的目的是让旧代码失败关闭：旧代码打开一个压缩过的文件，看到的是空的（或从中间开始的）`commands` 表，空表会被它当成一个空账本重放出来。

### 3.5 命令行

`smctl ledger compact LEDGER`：打开（重放）、`compact()`、`finally` 关闭；输出一行 `{"command": "compact", "ledger", "generation", "seq", "last_sha", "sha256", "removed"}`；账本拒绝退出码 3，参数错误退出码 2。`status` 等已有命令的输出不变。

### 3.6 不会变小的东西

- 文件大小：SQLite 删除行不归还空间，后续追加复用；不做 `VACUUM`（不能在事务里做，而且要重写整个文件）。压缩缩短的是打开时间与链核对的工作量，不是文件。
- 状态大小：事件占状态的一半，而且永远增长（第 5 节问题 1 的推荐项保留全部事件）。要让状态有界，得把事件归档、让 Scheduler 忘掉关闭的请求，那是改语义，另立方向。

## 4. 参考设计

07 第 8.3 节 R3/R7：核对时发现矛盾不自动删行，补偿不改写原证据。压缩删除的是重放用的命令，不是证据：事件（投影器的输入）全部留在检查点里，检查点自己带摘要并通过 `last_sha` 与命令链相连；压缩只由运维或控制器显式调用。07 第 8 章末关于备份：要保留全部命令的历史，就在压缩前用 SQLite 备份接口（`sqlite3.Connection.backup`）备份，或在没有写者时复制文件。

## 5. 用户决定（2026-10-09，均选推荐项）

1. **检查点的内容**：完整状态，含全部事件。检查点加尾部重放与全量重放在所有只读视图、事件与内部状态上完全相同；Scheduler 的语义一处不改。
2. **压缩删除检查点之前的命令**：日志只留检查点之后的命令；历史留在事件里；要保留全部命令就在压缩前用 SQLite 备份接口备份文件。
3. **压缩不让开着账本的别的实例失效**：状态身份 `(seq, sha)` 没有变，控制器下一次追加照常成功；只有别的进程追加了命令才失效。
4. **格式升到 v2，拒绝 v1 文件，不做迁移**：`LEDGER_FORMAT = "sentinelmesh-ledger-v2"`，v1 文件报 `E_LEDGER_FORMAT`；旧代码打开压缩过的文件同样失败关闭。

下面是当时的选项，留作记录：

1. **检查点的内容**：
   - 完整状态含全部事件：oracle 能逐项比对，语义不改；代价是事件占状态一半且永远增长，压缩只缩短打开时间，不缩小状态；
   - 或者丢弃检查点之前的事件：状态有界，但 `events` 从此只含检查点之后的事件（序号延续），投影器要先归档；这是改 Scheduler 的语义。
2. **压缩是否删除命令**：
   - 删除：日志只留尾部，历史在事件里；
   - 或者只写检查点不删：文件继续增长，两份真相并存，要互相核对。
3. **对开着账本的别的实例的影响**：
   - 照常追加，不失效：状态身份没变，日志头核对照样通过；
   - 或者一律失效：任何别的进程写入都让已开实例在下一次追加时报 `E_LEDGER_STALE`，压缩前得先停控制器。
4. **格式版本**：
   - 拒绝打开 v1，不做迁移：本仓库没有生产账本；
   - 或者兼容读取 v1：没有 `checkpoint` 表的文件当作没有检查点打开，压缩时就地升版本；多一条格式规则。

## 6. 规划端的决定（2026-10-09）

- K1：名字。检查点（checkpoint）与压缩（compact），避开已被预算快照占用的 `snapshot`：表 `checkpoint`，方法 `DurableScheduler.compact()`，命令 `smctl ledger compact`，错误码 `E_LEDGER_CHECKPOINT`（检查点损坏或与状态不一致）。
- K2：只显式触发：`compact()` 与命令行。控制器不自动压缩，追加路径不看日志长度。阈值是策略，不进账本。
- K3：`seq` 为 0 时也允许压缩（检查点 `(0, "")`）；重复压缩允许，`removed` 为 0。不加特例。
- K4：检查点的摘要覆盖 `seq`、`last_sha` 与 `state`（第 3.1 节的公式），`last_sha` 把它和命令链连起来；命令的摘要从不重算，所以压缩后尾部命令的行一字不改。
- K5：状态校验只到形状、精确类型与引用完整，不重算语义。
- K6：打开时检查点与尾部命令都存在的话，尾部第一条的 `seq` 必须是检查点 `seq + 1`，`previous` 必须等于 `last_sha`；不符是 `E_LEDGER_CHAIN`。
- K7：`scheduler.py` 不改；编码与恢复都在 `durable_ledger.py`。
- K8：`seq` 的含义改为“状态包含的最后一条命令的序号”；对未压缩的账本数值不变。
- K9：不做 `VACUUM`、不做归档表；压缩前的备份是运维的事（第 4 节）。
- K10：`compact` 不是 `MUTATING_METHODS` 的成员，不记命令、不发事件。

## 7. 合同拆分

一份合同 `ledger-compaction`：
- `sentinelmesh/durable_ledger.py`：格式 v2、`checkpoint` 表、状态编解码、`compact()`、日志头的新定义、打开路径；
- `sentinelmesh/ledger_cli.py`：`compact` 命令；
- 执行器测试 `tests/test_ledger_compaction.py`。
- 规划端 oracle `tests/test_ledger_compaction_oracle.py`：在 durable-ledger oracle 的 28 步场景上，在每一个前缀处压缩、继续、重开，与从不压缩的账本比较全部只读视图、事件与内部状态；真实子进程压缩之后父进程照常追加；子进程压缩之后被杀，父进程重开；损坏与伪造（多行检查点、摘要不符、尾部 `seq`/`previous` 不符、未知或缺失字段、类型、引用、检查点里的操作 id 被尾部的 `claim` 复用、v1 格式）；压缩失败失效；命令行。
- 规划端同时在 oracle 提交里把 `tests/test_durable_ledger_oracle.py` 第 168 行的格式字符串改成 v2。

## 8. 不做

- 不自动压缩，不做阈值。
- 不改 `Scheduler` 的语义与私有属性；不丢事件（除非用户在第 5 节另选）。
- 不做 `VACUUM`、归档表、迁移工具；不做多宿主。
- 不改 `status`、`reference` 等已有命令的输出。
- 不联网、不读凭据、不调用 pi。

## 9. 进度与遗留

- 已集成：oracle `dfb56a3`、`38f0c4e`，a001 返工提交 `76252ed`，规划端补全 `c7c280d`；复审 `.agent/reviews/ledger-compaction/attempt-001.md`（REVISE，class TIMEOUT：候选完整、缺执行报告，按 review-pi 技能的规则由规划端逐项复审代替报告后直接集成，attempt 标 SUPERSEDED）；干净工作树全量 730 passed / 289 skipped。
- a001 的验收：`check --fresh` 8/8；两轮变异各 46/46（oracle 加执行器测试）与 44/46（只用执行器测试；漏掉的两个是被别的检查遮蔽的用例，补全改成已结束的操作与无人领取的任务之后，执行器测试单独也能抓住）。
- 规划端草稿阶段：46 个故意偏差全部被 oracle 抓住，其中 6 个是修正 oracle 之后才抓住的（`with_state` 曾把 `pop` 的返回值当作新状态，四个用例因此替换了整个状态而不是改一个字段；伪造 claim 的用例落在了换代之后的位置，被代际围栏而不是被操作 id 登记拒绝，改到压缩在第 20 步、之后没有命令的账本上并加对照才成立；bool 给 int 字段的用例原来没有）。
- 遗留：
  - 文件不变小：SQLite 删行不归还空间，没有 `VACUUM`；压缩缩短的是打开时间；
  - 状态随事件增长（事件占状态一半）；要让状态有界得归档事件并让 Scheduler 忘掉关闭的请求，是另一个方向；
  - `status` 不显示有没有检查点、在哪个序号（输出未改）；要看就用 `compact` 的输出或直接查 `checkpoint` 表；
  - 改动 Scheduler 的私有状态（字段增减、类型）之前先给账本格式升版本：检查点的字段集合取自 dataclass，旧检查点会因字段不符而拒绝打开；
  - 合同的 Step 3 测试清单约 50 项对 1800 s 没有余量，执行器写完实现与测试后来不及写报告。
