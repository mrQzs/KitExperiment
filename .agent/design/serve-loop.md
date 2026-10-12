# serve 循环：`smctl run serve` 按任务顺序把一个请求跑空

状态：草案（2026-10-12）。等用户决定第 5 节，再拆合同。

## 1. 为什么做

`controller-cli` 的三个一次性命令（`init` / `once` / `recover`）已经把 `claim → handoff → supervise → 记录 → _reduce → settle → finish` 的整条链变成操作员能从命令行启动的东西，但每次调用恰好一次领取与一次运行：一个有八个任务的请求要操作员手敲八次 `once`，每次自己算幂等键、自己挑任务。用户 2026-10-12 选了“serve 循环”作为下一方向，之后转去改 Code-Kit（2.5.0、2.6.0），现在回来。

目标：一条命令把一个请求里能领的任务按种子顺序逐个领取、逐个运行，直到没有可领的为止；每一次运行仍然是 `once` 的那条链，不多一个守卫，不少一个守卫。它不是守护进程：没有轮询等待、没有重试、没有并发。

## 2. 现状（2026-10-12 核对）

- `run_cli.cmd_once`：读作业文件（`load_job`，精确键集合 `_JOB_REQUIRED` 加两个可选上限）、打开账本、`claim` 一次（请求、任务、plan、幂等键、保留量、资源都来自作业文件；`replayed` 的领取报 `E_RUN_CLAIM_REPLAYED` 并退出 3）、构造 `Invocation`、`run_adapter_operation` 一次，stdout 一行 JSON `{"command": "once", "operation_id", "outcome", "operation"}`；领取之后的拒绝带 `operation_id`（操作留在账本里 `RESERVED`）。`KeyboardInterrupt` 不捕获；账本在 `finally` 里关闭。
- `Scheduler.claim` 的拒绝顺序：`E_SCOPE_UNKNOWN` → `E_STOPPED` → `E_GUARD_STALE` → `E_TASK_DONE`（task-done，2026-10-12）→ `E_ACTIVE_TASK`（同任务有 LIVE 或 `REVIEW_PENDING` 的操作）→ `E_ATTEMPTS` → `E_CONCURRENCY` → 资源持有。幂等键以 `(request, key)` 索引，同键同指纹是重放，同键不同指纹是 `E_IDEMPOTENCY_CONFLICT`。
- 只读视图：`task_ids(request)` 按种子顺序；`task(request, task)` 给 `max_attempts`、`attempts`（含取消与未启动的领取）与 `done`。两者都不记事件、不改状态。
- 测试：`tests/test_run_cli*.py` 三组 16 个执行器用例加三份 oracle；没有测试枚举 `run` 命令组的子命令集合，所以加一个子命令不碰钉住面；`load_job` 的精确键集合被测试钉住（多一个键、少一个键都是 `E_RUN_JOB`），本方案照用它、不改它。
- `recover_after_restart` 由 `run recover` 调用，serve 不碰它：一个被杀的 serve 与一个被杀的 once 留下的东西相同（最多一个未决操作）。

## 3. 方案草图

新命令 `smctl run serve <ledger> --request <id> --jobs <dir> --evidence <root> --worktree-id <id> --owner <name> [--max-runs N] [--max-seconds S] [--candidate <64 hex>] [--now <n>]`，挂在 `run_cli.register`，全部逻辑在 `run_cli.py` 的 `cmd_serve` 与两三个辅助函数里。

- **作业来源**：`--jobs <dir>` 下每个任务一份作业文件 `<task_id>.json`，用 `load_job` 原样读（不改键集合）。打开账本之前先读完目录里的全部作业：任何一份 `E_RUN_JOB`、`request_id` 不等于 `--request`、`task_id` 不等于文件名，都是 `E_RUN_JOBS`，什么也不跑。账本里有任务而目录里没有作业的，跳过并报告（`no_job`）；目录里有作业而账本里没有任务的，开跑前就拒绝（`E_SCOPE_UNKNOWN` 由第一次 `claim` 报出来会太晚：这时别的任务可能已经跑过了）。
- **幂等键**：作业文件里的 `idempotency_key` 是基键；第 n 次尝试的键是 `"<基键>#<n>"`，n 取自 `task()` 的 `attempts + 1`。这样同一目录的作业文件可以重复用于整个请求的生命周期，而 `once` 的语义不变（`once` 仍按作业文件里的键原样领取）。被重放的领取（`replayed`）在 serve 里是 `E_RUN_CLAIM_REPLAYED`，停止循环——它说明有人用同一个键跑过了本应由 serve 跑的尝试。
- **循环**：按 `task_ids(request)` 的顺序走一遍任务：`task()` 说 `done` 或 `attempts >= max_attempts` 的跳过并记原因；否则 `claim`。`E_ACTIVE_TASK`（等复审裁决，或上一个控制器留下的未决操作）跳过并记原因；`E_STOPPED`、`E_GUARD_STALE`、`E_GENERATION`/账本的任何拒绝、`E_IDEMPOTENCY_CONFLICT` 停止循环（它们不是“这个任务现在不能领”，而是“这个账本现在不该再动”）。领到之后与 `once` 逐字相同地运行一次；结果记入报告，继续下一个任务。一整遍没有领到任何任务时结束（`drained`）；`--max-runs` 到数、或下一次领取前墙钟已超过 `--max-seconds` 时结束（上限只在领取之前检查，不在运行中途杀进程：杀进程是 supervise 的事，有它自己的 `timeout_seconds`）。
- **顺序执行**：一次只有一个 LIVE 操作；请求的 `concurrency` 不用。它不是“并发度 1”的一个特例实现，而是第一版明确不做并发：并发要解决证据目录、worktree 与 `KeyboardInterrupt` 的归属，另起设计。
- **证据与 worktree**：`--evidence <root>`，每次运行用 `<root>/<task_id>/<n>`（serve 创建，已存在则在开跑前拒绝 `E_RUN_EVIDENCE`，因为证据目录是一次性的）；`--worktree-id` 原样记在每次运行上（作业的 `cwd` 才是真正的工作目录；一个请求的任务各有 worktree 时，操作员给每个作业不同的 `cwd`，id 只是记录）。`--now` 是基准逻辑时钟，第 i 次领取用 `now + i`。
- **输出与退出码**：stdout 永远一行 JSON：`{"command": "serve", "request", "runs": [{"task_id", "attempt", "operation_id", "state", "evidence_dir"}…], "skipped": [{"task_id", "reason"}…], "stopped": "drained | max_runs | max_seconds | interrupted | refused", "refusal": null | {"error", "detail", "operation_id"}}`。`drained` / `max_*` / `interrupted` 退出 0；`refused` 退出 3，stderr 另有 `once` 式的一行拒绝（带已领取的 `operation_id`）。领取之间的 `KeyboardInterrupt` 捕获为 `interrupted`（已完成的运行在账本里，报告里也有）；运行当中的 `KeyboardInterrupt` 与 `once` 一样不捕获。
- **两个 adapter 都认**（`offline`、`pi`），与 `once` 同一张注册表；真实 pi 的运行仍 NOT_RUN。
- 原生证据：Windows 上用真实 `smctl` 子进程 `init`（三个任务）→ `serve`（离线桩，第二个任务的作业让子进程退出 2 进入复审、第三个正常）→ 报告里一次 `REVIEW_PENDING`、两次运行、`drained` → `ledger verdict` 接受第一个 → 再 `serve` → 第一个任务 `E_TASK_DONE` 跳过、第二个 `E_ACTIVE_TASK` 跳过、第三个 `attempts` 用尽跳过 → `drained` 且零运行。再一条：serve 跑到第二个任务时被 `TerminateProcess` 杀掉 → `recover` → 报告里恰好一个未决操作，与 `once` 被杀相同。

## 4. 证据边界

- 全部离线（`offline` adapter 与 `python -c` 作业）；pi adapter 只走已有的夹具路径；真实 pi、凭据、联网 NOT_RUN。
- 不改 `load_job`、`load_spec`、`cmd_once`、`cmd_recover`、`Scheduler`、`durable_ledger`：serve 只是组合它们。
- 不新增账本事件或记录：serve 的报告只在 stdout；账本里看到的与手敲 n 次 `once` 完全相同。

## 5. 用户决定（2026-10-12，均选推荐项）

1. **作业来源**：`--jobs <dir>` 下每任务一份 `<task_id>.json`，照用 `load_job`；开跑前读完全部，任何一份不对就什么也不跑。
2. **幂等键**：serve 派生 `<基键>#<尝试序号>`，序号取 `task()` 的 `attempts + 1`；`once` 语义不变。
3. **不可领的任务**：跳过并继续，一整遍没领到就结束；“账本不该再动”的拒绝停止循环；不等复审。
4. **报告与退出码**：stdout 永远一行报告；只有拒绝停止时退出 3，并在 stderr 另给 once 式的一行拒绝。

## 6. 规划端决定

- 逻辑时钟按领取递增（`now + i`），不按运行结束时间：账本的 `now` 是逻辑的，serve 不读墙钟给它。
- 一整遍没有领到任何任务就结束，不等复审裁决：等待是守护进程的事，本命令不是。
- `--max-seconds` 只在领取之前检查。

## 7. 合同拆分

一份合同 `serve-loop.v1`：`run_cli.py` 的 `cmd_serve` 与 `register`、执行器测试 `tests/test_run_cli_serve.py`；规划端先写 oracle `tests/test_run_cli_serve_oracle.py`（含第 3 节末尾的两条真实子进程用例）与草稿变异清单，按 2.5.0 的流程在草稿树上 `mutate --tree` 验收 oracle 后派发。决策模式（Code-Kit 2.6.0）在本轮仍为 off：没有 key。

## 8. 不做

- 守护进程、轮询等待复审、重试失败的运行、并发运行。
- 作业模板与变量替换：每个任务一份作业文件就是“模板”。
- serve 自己做 `recover`、`verdict`、`resume`：这些是操作员的决定。
- 改 `once` 的任何语义（幂等键派生只在 serve 里）。

## 9. 进度与遗留

- 2026-10-12：核对 `run_cli` 与 `Scheduler.claim` 的现状，写成本说明；用户决定第 5 节四项。
