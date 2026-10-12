# 设计说明：任务接受后不再领取（task-done）

状态：用户 2026-10-12 决定第 5 节四项均选推荐项；规划端决定见第 6 节；合同 `task-done` 已集成（2026-10-12），方向完成。
前置：review-verdict（`Scheduler.verdict`、`smctl ledger verdict`、`status.reviews`）已集成（2026-10-11，`c2215ee`）。

## 1. 为什么做

- review-verdict 之后，一个任务从种子到被接受的每一步都有命令、事件与证据，但"被接受"对调度器没有任何后果：`claim` 只拒绝活动操作（`E_ACTIVE_TASK`）与超出 `max_attempts` 的次数（`E_ATTEMPTS`），`ACCEPTED` 与 `REJECTED` 一视同仁。一个已接受的任务在次数用完之前可以再被领取、再跑一次、再进复审——这是浪费预算的空转，也让"任务做完了没有"在产品里无处可读。
- review-verdict 设计说明第 5 节问题 4 明确把它推迟为"另一条调度器规则，另立方向"。它是端到端链路里最后一个缺口：接受之后，任务应当关闭。
- 规模小、事实清楚：规则只从操作状态推导，不加账本记录、不加事件、不改账本格式。

## 2. 现状（2026-10-12 实测核对）

- `Scheduler.claim`（`scheduler.py:738`）的检查顺序：形状（`E_SCOPE_UNKNOWN`/`E_CLAIM_SHAPE`/`E_RESOURCE_KEYS`）→ 锁内幂等重放（同键同指纹直接返回 `replayed: True`，不再检查任何规则）→ `_epoch`（`E_FENCED`）→ 范围（`E_SCOPE_UNKNOWN`）→ `E_STOPPED` → `E_GUARD_STALE` → `E_ACTIVE_TASK`（LIVE ∪ {REVIEW_PENDING}）→ `E_ATTEMPTS`（同任务操作总数 ≥ `max_attempts`，含 NOT_STARTED/CANCELLED_NOT_STARTED 之外的一切——实际是 `sum` 全部同任务操作）→ `E_CONCURRENCY` → 预算 → `E_RESOURCE_BUSY`。领取被拒绝不发事件、不改状态。
- `ACCEPTED` 只由 `verdict` 与 `fixture_verdict` 写入，都要求前态 `REVIEW_PENDING`；而 `REVIEW_PENDING` 阻止同任务领取，所以一个任务在 `ACCEPTED` 出现时不可能同时有活动操作——新规则与 `E_ACTIVE_TASK` 的先后在实践中无关，与 `E_ATTEMPTS` 的先后有关（次数用尽且已接受时报哪个码）。
- 探针（草稿工作树，`c2215ee`）：在 `E_ACTIVE_TASK` 之前加"同任务存在 `ACCEPTED` 操作 → `E_TASK_DONE`"，全量 1 failed / 970 passed / 281 skipped——唯一失败是 `tests/test_review_verdict_oracle.py::test_a_decided_task_may_be_claimed_again_until_its_attempts_run_out`（`max_attempts=2`，先拒后接，第三次领取钉住 `E_ATTEMPTS`）。其它 11 个调用 `fixture_verdict(..., True, ...)` 的测试文件没有一个在接受之后再领取同一任务。
- 调度器与持久账本都没有任务的只读视图：`_tasks` 是私有字典（`(request, task) → _Task(caps, max_attempts, guard_version, fixture_eligible)`），公开读只有 `snapshot(request, task)`（按任务范围的预算）、`operation`/`operation_ids`/`request_ids`/`stop_reasons`。`smctl ledger status` 的请求条目是六个键 `{request_id, stopped, stop_reasons, snapshot, attention, reviews}`，review-verdict 的 oracle（`test_review_verdict_oracle.py:176`）钉住了这个键集；没有任何地方列出请求有哪些任务、各用了几次尝试。
- `smctl run once` 把领取被拒原样上报（`_refuse(exc, None)`：`{"error": 码, "detail", "operation_id": null}`，退出码 3），新码无需改 `run_cli.py`。
- 账本：检查点只存状态，重放只按方法名；规则从操作状态推导，重放、检查点、压缩都自然保持。`LEDGER_FORMAT`、`MUTATING_METHODS` 不变。

## 3. 方案草图

1. `Scheduler.claim`：在 `E_GUARD_STALE` 之后、`E_ACTIVE_TASK` 之前加一条——同请求同任务存在状态为 `ACCEPTED` 的操作 → `ContractError("E_TASK_DONE")`。幂等重放照旧先于一切规则（同键重放一个已接受任务的领取仍返回 `replayed: True`）。被拒不发事件、不改任何东西。`claim` 文档串列出新码及其位置。
2. 任务只读视图：`Scheduler.task_ids(request) -> tuple[str, ...]`（种子顺序，未知请求 `E_REQUEST_UNKNOWN`）与 `Scheduler.task(request, task) -> {"task_id", "max_attempts", "attempts", "done"}`（`attempts` = 同任务操作总数，与 `E_ATTEMPTS` 同一口径；`done` = 存在 `ACCEPTED` 操作；未知任务 `E_SCOPE_UNKNOWN`），锁内、不记事件；`DurableLedger` 同名代理（`_require_available`）。
3. `smctl ledger status` 每个请求多一个 `tasks` 列表：按种子顺序，每项即 `task(request, task)` 的四键；`attention`、`reviews` 不变。
4. oracle（规划端写，Windows 进程内 + 真实 `smctl` 子进程）：`run once` → `verdict --accept` → 再 `run once` 同任务 → stderr `{"error": "E_TASK_DONE", "operation_id": null}`、退出 3、账本不变（seq、事件、快照逐字节相同）；`status.tasks` 该任务 `done: true`、`attempts` 正确，其它任务可照常领取；`--reject` 后 `done: false`、次数照旧计数直到 `E_ATTEMPTS`；`max_attempts=1` 且已接受 → `E_TASK_DONE`（先于 `E_ATTEMPTS`）；`fixture_verdict` 写的 `ACCEPTED` 同样算完成；`rollover`、`compact`、重开之后规则不变；幂等重放不受影响；`task_ids`/`task` 对未知请求与任务的码；review-verdict oracle 那条测试改为期望 `E_TASK_DONE`（在 oracle 提交里改）。
5. 执行器测试短清单：规则位置（接受后拒绝、拒绝后可再领、`E_TASK_DONE` 先于 `E_ATTEMPTS`）、视图四键与顺序、`status.tasks`、重开后一致。

## 4. 证据边界

- 不读凭据、不联网、不启动真实 pi；子进程只有 `python -m sentinelmesh.cli`。
- 不核验证据内容；"接受"只认状态。

## 5. 用户决定（2026-10-12，均选推荐项）

1. **新码的位置**：`E_TASK_DONE` 在 `E_ACTIVE_TASK`/`E_ATTEMPTS` 之前（推荐；"已接受"是关于任务的最终事实，比次数更具体，次数用尽且已接受时报 `E_TASK_DONE`）还是放在 `E_ATTEMPTS` 之后（次数用尽时照旧报 `E_ATTEMPTS`，只有次数未尽的已接受任务才报新码）。
2. **什么算"已接受"**：任何 `ACCEPTED` 状态的操作，不论由 `verdict` 还是 `fixture_verdict` 写入（推荐；状态是唯一口径，重放与检查点天然保持）还是只认 `run.verdict` 事件里带操作员的那种（要扫事件，检查点后不可得）。
3. **`status` 是否加 `tasks` 列表**：加，每项 `{task_id, max_attempts, attempts, done}` 按种子顺序，并加 `task_ids`/`task` 只读视图（推荐；否则操作员只能靠领取被拒才知道任务已完成）还是不改 `status`、不加视图（只加领取规则）。
4. **请求级的"完成"**：不加（推荐；请求是否完成还牵涉停止原因与预算，`tasks` 列表已足够推出"所有任务已接受"）还是请求条目加 `done: bool`（所有任务都有 `ACCEPTED` 操作）。

## 6. 规划端决定

- 一份合同 `task-done`（低—中风险）：领取规则一行、两个只读视图及代理、`status.tasks`、文档串；允许改 `scheduler.py`、`durable_ledger.py`、`ledger_cli.py`，V08 钉住其它模块，复审逐 hunk 核"diff 只在指定位置"。
- 不加事件、不加账本记录、不改 `LEDGER_FORMAT` 与 `MUTATING_METHODS`：规则是推导出来的。
- `REJECTED` 的语义不变：次数照旧计数，任务可再领取。

## 7. 合同拆分

1. `task-done`：第 3 节全部；oracle 在 Windows 上进程内 + 真实子进程。

## 8. 不做

- 不加"任务完成"的新状态或新事件；不改 `max_attempts` 的计数口径。
- 不做 `serve` 循环、不做请求级完成状态（问题 4 选推荐项时）。
- 不改 `attention` 与 `reviews`。

## 9. 进度与遗留

- 2026-10-12：实测核对第 2 节（探针：规则加在 `E_ACTIVE_TASK` 之前，全量只有 1 条 review-verdict oracle 测试失败）；用户决定第 5 节四项均选推荐项。
- 合同 `task-done` 已集成（2026-10-12）：oracle `c7b5b65`（pi-base 上 `7256c8b`；另把 review-verdict oracle 的键集加 `tasks`、接受后的第三次领取改为期望 `E_TASK_DONE`）、合同 `6433938`、a001 `4901eac`、规划端补全 `69d64fb`；复审 `.agent/reviews/task-done/attempt-001.md`（PASS，544 s；`check --fresh` 8/8；变异 27/29 与 17/29，漏掉的 2 条等价：规则放在活动任务检查之后、`done` 只看最后一个操作）；干净工作树全量 988 passed / 281 skipped。
  - 执行器零取舍（报告无 limitations）：合同逐字给出规则两行、视图四键与代理样板，544 s 完成。
  - 复审四个低级发现：规则上方注释四行（合同要一两行）、报告把 oracle 的账本字节检查说成执行器测试的、测试辅助函数多断言一次并带无人用的参数、工作树里有被忽略的 `.pytest_cache/`；前后两处补全已改。
  - oracle 草稿时发现的两处盲区：任务名按字母序种子时“排序”与“种子顺序”不可区分（改成非字母序种子）；只有一个请求时“忽略请求”不可区分（加第二个请求的同名任务）。
