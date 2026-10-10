# 设计说明：运维读事件 `smctl ledger events`（单宿主）

状态：用户已决定第 5 节（2026-10-09，均选推荐项）；规划端决定见第 6 节。
前置：`smctl ledger`（`.agent/design/ledger-cli.md`）与账本检查点与压缩（`.agent/design/ledger-compaction.md`）都已集成。

## 1. 为什么做

- 压缩（ledger-compaction）删除检查点之前的命令，用户决定“历史留在事件里”：事件现在是账本唯一完整的历史。可是运维没有读事件的入口——`status` 只给请求的停止原因、预算快照和需要处理的操作；要看“这个操作经历了什么”“这个请求什么时候停的、谁恢复的”，只能写 Python 调 `ledger.events`。
- 07 第 8.3 节的恢复流程（R3 核对账本、R4 核对运行、R5 核对预算）每一步都要“可审计”；事件就是审计材料，运维应当能按请求、操作、类型、序号区间读到它们。
- 压缩之后账本里有没有检查点、在哪个序号，运维也看不到（ledger-compaction 的遗留）。

## 2. 现状

- 事件由内存 Scheduler 的 `_emit` 记录，每条恰好五个键：`seq`（从 1 连续）、`operation_id`（str 或 None）、`type`、`generation`、`payload`（规范化 JSON）。`DurableScheduler.events` 返回全部事件的深拷贝（JSON 往返，6001 条约 14 ms）；没有按条件读取的接口。
- 事件类型共 16 种。带操作 id 的 11 种：`scheduler.claimed`、`scheduler.handoff_accepted`、`run.started`、`usage.observed`、`run.finished_for_review`、`run.verdict`、`scheduler.start_unknown`、`scheduler.not_started`、`scheduler.recovered`、`scheduler.unknown_resolved`、`scheduler.cancelled_before_handoff`。请求级的 5 种，`operation_id` 为 None：`request.created`、`request.stopped`、`request.stop_observed`、`request.resumed`（payload 都带 `request_id`）与 `controller.fenced`（payload 只有 `generation`，不属于任何请求）。
- `smctl ledger` 现有七个命令；`status` 的输出是 `{"ledger", "generation", "seq", "requests"}`，ledger-compaction 的 oracle 与执行器测试都断言压缩前后 `status` 逐字相同。
- 账本实例不暴露检查点的位置：`open_ledger` 知道它（`start_seq`），`compact()` 也知道，但没有读访问器。
- 一个长账本的事件可能有几万条；一行 JSON 输出几万条事件是几 MB，运维命令的输出要有界。

## 3. 拟议

### 3.1 命令

`smctl ledger events LEDGER [--request R] [--operation OP] [--type T ...] [--after SEQ] [--limit N]`

1. 打开账本（重放）；
2. `--request R` 必须是 `request_ids()` 里的请求，否则 `E_REQUEST_UNKNOWN`；`--operation OP` 必须是已知操作（`operation(OP)`，否则 `E_OPERATION_UNKNOWN`）；`--type` 可重复，取值限于 16 种类型（argparse `choices`，错的在打开账本之前以退出码 2 拒绝）；
3. 取 `ledger.events`，按序号升序过滤：
   - `--request R`：带操作 id 的事件按该操作的 `request_id` 归属；请求级事件按 payload 的 `request_id` 归属；`controller.fenced` 不属于任何请求；
   - `--operation OP`：`operation_id == OP`；
   - `--type`：类型属于给定集合；
   - 几个条件同时给出时取交集；
4. 分页：跳过 `seq <= --after` 的事件，最多返回 `--limit` 条（默认 100，最多 10000，必须是正整数；`--after` 默认 0，非负整数）；
5. 打印一行 JSON，`finally` 里关闭账本。全程只读，不调用任何变更方法。

### 3.2 输出

```
{"command": "events", "ledger", "generation", "seq",
 "checkpoint": 检查点的序号或 null,
 "total": 账本里事件的总条数,
 "matched": 过滤后（分页前）的条数,
 "events": [事件原样（五个键、payload 原样），按 seq 升序],
 "next": 还有匹配事件时是本页最后一条的 seq，否则 null}
```

运维接着用 `--after <next>` 翻下一页。事件不加派生字段（第 5 节问题 4）：事件是账本记录的原文，操作与请求的关系用 `status` 或 `--request` 过滤去对。

### 3.3 检查点的读访问器

`DurableScheduler.checkpoint_seq`（只读属性）：打开时等于检查点的 `seq`，没有检查点为 None；`compact()` 成功后等于本实例的 `seq`。与其他只读访问器一样在锁内、先 `_require_available`，不碰日志。`status` 的输出不变（第 5 节问题 1）。

### 3.4 边界

- 读取是整份 `events` 的深拷贝再过滤：几万条事件几十毫秒，与 `status` 同一量级；不给 Scheduler 加按条件读取的接口。
- 输出有界：一页最多 10000 条；`matched` 与 `next` 告诉运维还有多少。
- 不改事件的形状，不改 `status`、`reference`、`compact` 等现有命令的输出。

## 4. 参考设计

07 第 8.3 节：恢复的每一步要可审计、不改写历史、无法证明的保持未知。本命令只读账本已经记录的事件，不推断、不汇总、不改写；检查点的位置是“历史从哪一条命令起只剩事件”的事实，一并报告。

## 5. 用户决定（2026-10-09，均选推荐项）

1. **检查点的位置报告在 `events` 的输出头部**；`status` 的输出不变。
2. **分页**：`--after SEQ` 加 `--limit N`（默认 100、最多 10000），按 `seq` 升序，还有匹配时输出 `next`。
3. **按请求过滤的归属**：带操作 id 的事件按操作所属请求，请求级事件按 payload 的 `request_id`，`controller.fenced` 不属于任何请求；未知请求 `E_REQUEST_UNKNOWN`。
4. **事件原样输出五个键**，不加派生字段。

下面是当时的选项，留作记录：

1. 检查点报告在 `events` 头部（`status` 不变，现有 oracle 与测试断言压缩前后 `status` 逐字相同）；或者 `status` 顶层加 `checkpoint` 键（改现有命令的输出与两处测试）。
2. 分页（几万条事件不会一次打几 MB）；或者不分页，一次全部输出。
3. `--request` 按上述归属；或者不提供 `--request`，运维按操作逐个查。
4. 原样五个键；或者每条加派生的 `request_id`、`task_id`（输出不再是原文，要定义请求级事件上的值）。

## 6. 规划端的决定（2026-10-09）

- E1：命令名 `events`，经 `_with_ledger` 打开、一次工作、`finally` 关闭；只读。
- E2：检查顺序：参数（argparse，退出码 2）→ 打开账本 → `--request`（`E_REQUEST_UNKNOWN`）→ `--operation`（`E_OPERATION_UNKNOWN`）→ 读事件、过滤、分页。拒绝走 `_refuse`（退出码 3，stderr 一行 JSON）。
- E3：`--type` 用 argparse `choices`，表在 `ledger_cli` 里按 Scheduler 的 16 种类型写死（`_EVENT_TYPES`）；Scheduler 加新类型时同步更新，否则运维选不到它——失败关闭的命名，而不是接受任意字符串静默匹配不到。
- E4：`--request` 的归属用 `operation(op)["request_id"]`（每个操作查一次，结果缓存在命令内），请求级事件用 `payload["request_id"]`；`controller.fenced` 永远不匹配 `--request`。
- E5：`--after` 非负整数、`--limit` 1–10000，argparse `type` 校验；`next` 是本页最后一条事件的 `seq`，仅当分页后还有匹配事件。
- E6：`checkpoint_seq` 作为 `DurableScheduler` 的只读属性；`durable_ledger.py` 只加这一处（`__init__` 多一个参数、`open_ledger` 传入、`compact()` 更新）。
- E7：输出键恰好 9 个；事件原样（`ledger.events` 已是深拷贝）。

## 7. 合同拆分

一份合同 `ledger-events`：
- `sentinelmesh/ledger_cli.py`：`events` 命令、`_EVENT_TYPES`、过滤与分页；
- `sentinelmesh/durable_ledger.py`：`checkpoint_seq` 读访问器；
- 执行器测试 `tests/test_ledger_events.py`；
- 规划端 oracle `tests/test_ledger_events_oracle.py`：在 ledger-compaction 的 33 步场景（含换代、裁定、恢复）上，`events` 的全量输出等于 `ledger.events`；每种过滤与交集；分页的 `next` 翻页能拼回全量；压缩前后输出相同且 `checkpoint` 从 null 变成序号；拒绝与退出码；从不写；命令行入口。

## 8. 不做

- 不改 `status` 的输出（除非用户在第 5 节另选）；不改事件的形状；不给 Scheduler 加按条件读取的接口。
- 不做文本/表格格式，不做时间过滤（事件没有时间戳）。
- 不联网、不读凭据、不调用 pi。

## 9. 进度与遗留

- 已集成：oracle `cdb1e62`、a001 `054631a`、规划端补全 `028d882`；复审 `.agent/reviews/ledger-events/attempt-001.md`（PASS，独立复审 PASS）；干净工作树全量 755 passed / 289 skipped。
- a001 的验收：571 s；`check --fresh` 8/8；变异 24/24（oracle 加执行器测试）与 19/24（只用执行器测试；漏掉的五条是合同刻意留给 oracle 的规则）。
- 草稿阶段：24 个偏差抓住 23 个，漏掉的“接受 `--limit 0`”靠空页上的 `IndexError` 碰巧以退出码 2 退出；oracle 改为要求 argparse 的 `usage:` 行后抓住。
- 遗留：
  - 读取是整份事件的深拷贝再过滤，几万条事件几十毫秒；事件再多一个数量级时要给 Scheduler 加按序号区间读取的接口；
  - 没有时间过滤：事件没有时间戳；
  - `_EVENT_TYPES` 在 `ledger_cli` 里写死，Scheduler 加新类型要同步（oracle 用源码里的 `_emit` 字面量核对）。
