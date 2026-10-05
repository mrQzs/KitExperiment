# 设计说明：把原生监督器与重放判定接入 Scheduler（macOS）

状态：草稿（2026-10-05），第 5 节的语义待用户决定
前置：POSIX 监督第 0–3 份合同已集成（`.agent/design/posix-runtime-runner.md`）：`supervise()`、`record_supervised_run()`、`replay_decision()`

## 1. 为什么做

监督器和重放判定都已有 macOS 原生证据，但还没有调用方：`Scheduler` 的操作状态（领取、交接、运行、完成、启动未知）与真实进程之间没有连接。`IMPLEMENTATION_STATUS.md` 把“接入 adapter 与调度器”列为 NOT_RUN。

SentinelMesh 的设计已经规定了接法：
- `07-workflows-and-scheduling` 4.3.6：已领取 → handoff 再核验 → 持久记录 HANDOFF_ACCEPTED → 05 创建并监督进程 → STARTED（带身份），或 NOT_STARTED 的可靠证据，或 START_UNKNOWN；
- 4.3.7：运行并发槽与文件锁，只有在 05 证明受控范围已退出、必要清理完成后才释放；主进程退出 0 不够；
- 4.3.10 与 4.4.7：START_UNKNOWN 先恢复，不默认重试；保留调用占用、预留与资源；
- `05-cross-platform-runtime` 4.3.10：handoff 已记而创建结果未知时判为 START_UNKNOWN，调查而不重发；controller 换代后先冻结新派遣，再核验旧对象。

## 2. 现状与缺口

`sentinelmesh/scheduler.py` 的 `Scheduler` 是 07 的单机原型，状态为：RESERVED → HANDOFF_ACCEPTED → RUNNING → REVIEW_PENDING → ACCEPTED / REJECTED；另有 START_UNKNOWN（`unknown()`，同时停止 request）、CANCELLED_NOT_STARTED（`cancel_unstarted()`，只能从 RESERVED 进入）。

接入监督器时缺少四样东西：
1. **交接之后已证明未启动的出口**。可执行文件格式错误导致 exec 失败（观察记录为 NOT_STARTED），或者监督器在启动前拒绝了输入，这两种情形都能可靠证明没有进程，但 `cancel_unstarted()` 只接受 RESERVED。Code-Kit 参考账本的做法是 `cancel_unstarted(not_started_proven=True)` 与 `reconcile_not_started(...)`。
2. **运行状态的时机**。`finish()` 只接受 RUNNING；而监督器在进程结束后才返回，没有“刚启动”的回调。
3. **清理未确认的落点**。`finish()` 要求 `cleanup_verified=True`；清理未确认时，目前唯一的出口是 `unknown()`（START_UNKNOWN）。
4. **controller 换代后的恢复**。`rollover()` 会停止所有 request，但没有按证据处理旧代际在途操作的入口；`_owned()` 要求 owner 与 generation 都匹配，新代际无法接管。

## 3. 方案草图

新增桥接模块 `sentinelmesh/supervised_operation.py`，`Scheduler` 只加必要的迁移，不改已有迁移的语义。

- `run_supervised_operation(scheduler, *, operation_id, owner, generation, spec, environment, evidence_dir, worktree_id, candidate_hash_of, now)`：
  1. `scheduler.handoff(...)`：再核验守卫，记录 dispatch；
  2. `record_supervised_run(...)`：启动前写交接记录，结束后写观察记录；
  3. 按结果迁移 `Scheduler`：
     - 启动前被拒、或 exec 失败（NOT_STARTED）：走新增的“交接后已证明未启动”出口；
     - 已启动、清理已确认：补记 RUNNING（事后登记 STARTED 事实），再 `finish(candidate_hash_of(run), cleanup_verified=True)`；
     - 已启动、清理未确认：见第 5 节问题 1；
     - 观察记录写不进去，或桥接中途出现意外异常：以 `replay_decision()` 为准分类；判不出时一律 START_UNKNOWN。
- `recover_operation(scheduler, *, operation_id, owner, generation, spec, evidence_dir, ...)`：controller 换代后，新代际凭 `replay_decision()` 处理旧代际的在途操作（见第 5 节问题 4）。
- `spec.run_id` 与 `operation_id` 一一对应（每次尝试一个新 run）；`spec.request_id` 等于 Scheduler 的 request。
- 不改监督器与记录模块的行为。

## 4. 证据边界

- 只在 macOS 上取得原生证据；Linux、Windows Job Object 仍为 NOT_RUN。
- `Scheduler` 是内存原型（07 附录原型），不是持久账本；跨进程的 controller 崩溃用“新建 Scheduler 并重放事件”之类的方式模拟，不声称持久恢复。
- 不调用真实 provider CLI，测试只启动本地 `python -c` 进程。

## 5. 待用户决定的语义

1. **清理未确认的落点**：
   - 复用 START_UNKNOWN（与 Code-Kit 参考账本的 `mark_start_unknown` 一致：保留资源与预留，停止 request，原因 RECOVERY_REQUIRED），在事件里写明原因是清理未确认；
   - 或新增 CLEANUP_UNKNOWN 状态，与“不知道是否启动”分开。
2. **交接后已证明未启动**（exec 失败、启动前被拒）：
   - 释放资源和预算预留，尝试次数照计（07 原型的保守做法），dispatch 计数保留；
   - 或不计这次尝试。
3. **失败的运行**（非零退出、超时强杀）且清理已确认：
   - 与成功的运行一样进 REVIEW_PENDING，由复审裁决（“运行结束不直接接受”）；
   - 或直接进 REJECTED。
4. **controller 换代后的恢复**：
   - 自动按 `replay_decision()` 处理：未启动 → 释放；启动未知或清理未确认 → 保持 START_UNKNOWN；已结束且清理确认 → 进 REVIEW_PENDING；
   - 或只自动处理“未启动”和“未知”，已结束的留给人工。
