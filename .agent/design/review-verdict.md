# 设计说明：复审裁决的操作员入口（review-verdict）

状态：用户 2026-10-11 决定第 5 节四项均选推荐项；规划端决定见第 6 节；第 1 份合同 `review-verdict` 待派发。
前置：durable-ledger、ledger-cli、request-resume、controller-cli（`smctl run init/once/recover`）、live-pi-run 均已集成（2026-10-11）。

## 1. 为什么做

- 端到端链路今天停在 `REVIEW_PENDING`：`smctl run once` 把一次运行跑到"等复审"，账本里记着候选哈希（`run.finished_for_review`），但产品里没有任何命令能把它判成 `ACCEPTED` 或 `REJECTED`。调度器唯一的裁决方法叫 `fixture_verdict(operation, candidate_hash, accepted, evidence_hash)`——名字说明它是给夹具与测试用的：不要求当前代际、不记操作员、不记说明，事件 `run.verdict` 的 payload 只有 `{state, evidence}`。`smctl ledger` 的八个命令（status/reference/compact/events/stop/settle/resolve/resume）没有一个碰它。
- 对照 `resolve_unknown`（运维裁定 `START_UNKNOWN`）：要求当前代际（`E_FENCED`）、操作员名与说明按精确文本规则（`E_OPERATOR`/`E_NOTE`）、证据哈希、事件里记全。复审裁决是同一类"人下的决定"，应当同一口径记录。
- 这是 live-pi-run 之后链路里最后一个没有操作员入口的状态迁移：`claim → handoff → run → finish(REVIEW_PENDING) → ?`。补上它，一个任务从种子到被接受/拒绝的每一步都有命令、有事件、有证据。

## 2. 现状（2026-10-11 实测核对）

- `Scheduler.fixture_verdict`（`scheduler.py:1016`）：形状检查 `E_VERDICT_SHAPE`（`accepted` 是 bool、`evidence_hash` 64 hex）；操作不存在、不是 `REVIEW_PENDING`、候选不等于记录的 → `E_VERDICT_CANDIDATE`；改状态、发 `run.verdict` `{state, evidence}`；不释放资源（`finish` 到 `REVIEW_PENDING` 时已释放）。12 个测试文件 30 处调用它。
- `durable_ledger.MUTATING_METHODS` 按名字记录与重放命令（未知名字 → `E_LEDGER_REPLAY`），新方法要加进去；检查点只存状态，不受方法名影响。
- `ledger_cli.cmd_resolve` 的形状：`--operation --resolution --operator --evidence --note [--candidate]`，用 `ledger.generation`，输出 `{"command": "resolve", "operation": ledger.operation(op)}`。
- `smctl ledger status` 的 `attention` 只列三类（`start_unknown`、`undecided`、`usage_unknown`）；`REVIEW_PENDING` 且用量完整的操作不出现在任何地方——操作员要靠 `events --type run.finished_for_review` 才能找到待复审的运行。run-cli 的 oracle 断言 `attention == []`，所以 `attention` 不能变。
- 领取规则：`claim` 对同任务只拒绝活动操作（`E_ACTIVE_TASK`：RESERVED/HANDOFF_ACCEPTED/RUNNING/START_UNKNOWN）与超出 `max_attempts` 的次数（`E_ATTEMPTS`）；`ACCEPTED`/`REJECTED`/`REVIEW_PENDING` 都不阻止同一任务的下一次尝试。没有"任务已完成"的概念。
- `_decision_text(operator, "E_OPERATOR", _MAX_OPERATOR)` / `(note, "E_NOTE", _MAX_NOTE)` 与 `_epoch(generation)`（`E_FENCED`）是 `resolve_unknown` 已有的检查，可直接复用。

## 3. 方案草图

1. `Scheduler.verdict(operation, generation, *, candidate_hash, accepted, evidence_hash, operator, note)`：先 `_decision_text` 两项，再形状（`E_VERDICT_SHAPE`），锁内先 `_epoch(generation)`（`E_FENCED`），再操作与候选（`E_VERDICT_CANDIDATE`），改状态为 `ACCEPTED`/`REJECTED`，发 `run.verdict`，payload `{state, evidence, candidate, operator, note}`。`fixture_verdict` 原样保留（测试用），文档串注明。
2. `durable_ledger.MUTATING_METHODS` 末尾加 `"verdict"`。
3. `smctl ledger verdict <ledger> --operation OP --candidate HASH (--accept | --reject) --evidence HASH --operator NAME --note TEXT`：`_with_ledger`，用 `ledger.generation`，输出 `{"command": "verdict", "operation": ledger.operation(OP)}`；`--accept`/`--reject` 互斥且必选其一（argparse 用法错误退出码 2）。
4. `smctl ledger status` 每个请求多一个 `reviews` 列表：`REVIEW_PENDING` 的操作，按领取顺序，`{operation_id, task_id, generation, candidate}`（候选从 `run.finished_for_review`/`scheduler.recovered` 事件或操作视图取，以合同为准）；`attention` 不变。
5. oracle：进程内 `cli.main` + 一个真实 `smctl` 子进程：`smctl run once`（离线桩）→ `status.reviews` 一条 → `verdict --accept` → `ACCEPTED`、`run.verdict` payload 五键、`reviews` 空；`--reject` 同；错候选、再裁一次、裁 `ACCEPTED`/`START_UNKNOWN`/不存在的操作 → `E_VERDICT_CANDIDATE`，账本不变；`rollover` 后旧代际不可裁（命令用当前代际所以总能裁——oracle 改为验证"裁决记录的代际是当前的"）；证据非 hex → `E_VERDICT_SHAPE`；操作员名/说明违反文本规则 → `E_OPERATOR`/`E_NOTE`；裁决后同一任务可再领取为新尝试，直到 `E_ATTEMPTS`；`events --type run.verdict` 能列出；`resolve --resolution FINISHED` 进来的复审同样可裁。

## 4. 证据边界

- 不读凭据、不联网、不启动真实 pi；子进程只有 `python -c` 与 `python -m sentinelmesh.cli`。
- 操作员的权限与证据哈希照旧只记录不核验（request-resume 第 5 节）。

## 5. 用户决定（2026-10-11，均选推荐项）

1. **命令放哪**：`smctl ledger verdict`（推荐；它是操作员对账本的决定，与 `resolve`/`resume` 同组）还是 `smctl run verdict`。
2. **记录方式**：新加 `Scheduler.verdict`（当前代际、操作员、说明都进事件；`fixture_verdict` 保留给测试）（推荐）还是直接包一层现有的 `fixture_verdict`（事件里没有操作员与说明）。
3. **`status` 是否加 `reviews` 列表**：加（推荐；否则操作员只能翻事件找待复审的运行）还是不改 `status`。
4. **裁决后的任务**：领取规则不变——`ACCEPTED`/`REJECTED` 都不阻止同一任务的下一次尝试，`max_attempts` 照旧计数（推荐；"任务已完成"是另一条调度器规则，另立方向）还是本方向顺带加"任务已接受后不再领取"。

## 6. 规划端决定

- 一份合同 `review-verdict`（中风险）：调度器方法、账本方法表、CLI 命令、`status.reviews`、文档串；执行器测试短清单；oracle 规划端写。`scheduler.py` 与 `durable_ledger.py` 是允许修改的文件，但合同用 V08 钉住它们之外的模块，并要求两个文件的 diff 只在新方法与方法表（复审逐行核）。
- `run.verdict` 的 payload 加键不删键（`state`、`evidence` 照旧），`events` 命令与既有 oracle 不受影响。
- 不改 `attention` 的三类理由。

## 7. 合同拆分

1. `review-verdict`（中风险）：第 3 节全部；oracle 在 Windows 上进程内 + 真实子进程。

## 8. 不做

- 不改领取规则、不加"任务完成"状态（问题 4 选推荐项时）。
- 不自动裁决、不按证据内容判断。
- 不删 `fixture_verdict`，不改既有 30 处测试调用。

## 9. 进度与遗留

- 2026-10-11：实测核对第 2 节；用户决定第 5 节四项均选推荐项。
