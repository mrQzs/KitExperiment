# TASK: serve-watch

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 1816f1c5f85338f4417d1eeaf2434e53d1970537
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch, merge
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/serve-watch.md`（第 3.2 节；用户决定第 5 节第 2–4 项；规划端决定第 6 节）给 `smctl run serve` 加 `--watch` 与 `--poll-seconds`：在 `serve --parallel` 的走查外面加“遍”的循环。
- 每遍开始与结束时补读账本（`refresh()`，上一份合同 ledger-catch-up 已经提供），所以操作员从另一个进程下的裁决会被看见；
- 被复审拒绝的任务下一遍再领；没进复审就结束的任务留给操作员；
- 请求里没有任务能再推进时，报告 `finished`。

让规划端 oracle `tests/test_run_cli_serve_watch_oracle.py`（14 个用例）全部通过；新建执行器测试 `tests/test_run_cli_serve_watch.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改不加 `--watch`、`--parallel` 时的 serve：`cmd_serve` 只改开头的分派（第 2 条），原有循环一字不改；`tests/test_run_cli_serve_oracle.py`、`tests/test_run_cli_serve.py` 原样通过。
- 不加 `--watch` 时 `_serve_parallel` 的行为不变：
  - 不补读；
  - 报告里没有 `passes` 与 `watch` 两个键；
  - `tests/test_run_cli_serve_parallel_oracle.py`、`tests/test_run_cli_serve_parallel.py` 原样通过。
- 不改 `durable_ledger.py`、`scheduler.py` 与其它模块。不加账本事件。
- 不做系统服务、不脱离终端、不跨请求、不自动重试没进复审的运行（`needs_operator`）。
- 纯 Python 吸收中断有残留窗口，与 serve-parallel 相同，不追求完全吸收。
- 不联网、不读凭据、不启动真实 pi。

## Repository facts
- 基线 = master `5929586`（含 ledger-catch-up 的候选 `43fbd73` 与补全 `5929586`，以及 serve-parallel 的等待修复 `c41d557`）+ 规划端提交 `1816f1c5f85338f4417d1eeaf2434e53d1970537`。规划端提交包含定稿的设计说明与新文件 `tests/test_run_cli_serve_watch_oracle.py`（14 个用例，一个类 `WatchOracle`，Windows 上约 8 秒）。基线上 13 个失败；`--poll-seconds` 的用法错误用例在基线上也是退出 2（argparse 不认识这个参数），所以通过。
- 规划端草稿实测（Windows，约 120 行）：
  - oracle 14/14，serve 的另外两种用法照旧；
  - 草稿树全量 1090 passed / 281 skipped（草稿树没有运行目录，3 个规划端测试跳过）；
  - 20 条故意偏差里 16 条被 oracle 抓住：遍开始不补读、不跳过 `needs_operator`、`NOT_STARTED` 不算、等复审不算等、被拒绝的不算能推进、次数用尽先于等复审、skipped 跨遍累计、遍数多算、没有 `watch` 键、缺省停顿不是 30、停顿不分段、停顿中的中断不吸收、`--poll-seconds` 单独放行、宽度缺省不是 1、`--watch` 不进并发走查、领取数每遍清零；
  - 4 条预期漏掉，都是观察不到的时序：
    - 遍末不补读：运行期间工作线程自己的写入已经补读了操作员的裁决，只差在最后一次写入之后才来的裁决，晚一遍发现能结束；
    - 没试过的任务不算能推进：一遍走完时，没试过、有作业的任务必定已被领取，或者已经因为上限停了；
    - 停顿前不查 `max_seconds`：用替身时钟时，只有停顿推进时钟，下一遍第一次领取之前的检查一样会停；
    - 遍末补读的拒绝不处理：换代总是在停顿里发生，被下一遍开头的补读接住。

  这四条仍是合同规则，复审读代码核对。
- oracle 的驱动方式：
  - 进程内 `cli.main(argv)`，用 `mock.patch.object(run_cli, "_sleep", …)` 替换遍与遍之间的停顿：第 k 次停顿时，另一个账本实例（`open_ledger` 打开、写完关闭）扮演操作员下裁决、停止请求或换代。所以遍数是确定的，`_sleep` 必须经模块级名字调用。
  - `mock.patch.object(run_cli, "_clock", …)` 让时钟随停顿前进。
  - `_thread.interrupt_main()` 在真实停顿里送 Ctrl-C：要求 30 秒内返回，所以 `_sleep` 必须由不超过 `_POLL_SECONDS` 的短等待组成。
  - 一个真实 `smctl run serve --watch --poll-seconds 1` 子进程，由另外的 `smctl ledger verdict` 子进程下裁决。
- `sentinelmesh/run_cli.py`：
  - `cmd_serve` 开头只有一行分派，条件是 `getattr(args, "parallel", None) is not None`；
  - `_serve_parallel(args)` 是并发走查：开跑前检查 → 走查（领取只在主线程）→ 收尾 → 报告。走查的拒绝与第一次 Ctrl-C 都先收尾再报告。等待码只在本次调用自己有运行在跑时等；在跑数只读一次。
  - `_runs_in_flight`、`_await_run`、`_ended_badly`、`_serve_run`、`_shared_directories` 照用。
- 账本：
  - `DurableScheduler.refresh()` 在读事务里补读别的实例追加的命令，返回 `{"seq", "applied"}`。补读遇到换代是 `E_LEDGER_STALE`，实例随之失效。
  - `ledger.operation(id)` 给 `request_id`、`task_id`、`state`；`ledger.operation_ids()` 按领取顺序给出全部操作。
  - LIVE 是 `RESERVED`、`HANDOFF_ACCEPTED`、`RUNNING`、`START_UNKNOWN`。
  - 请求被停止后，领取的第一个拒绝是 `E_STOPPED`（在 `E_ACTIVE_TASK` 之前）。

## Scope
- allowed_modified_files:
  - sentinelmesh/run_cli.py
- allowed_new_files:
  - tests/test_run_cli_serve_watch.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_run_cli_serve_watch_oracle.py`, `tests/test_run_cli_serve_parallel_oracle.py`, `tests/test_run_cli_serve_parallel.py`, `tests/test_run_cli_serve_oracle.py`, `tests/test_run_cli_serve.py`, `tests/test_run_cli_oracle.py`, `tests/test_run_cli.py`, `tests/test_ledger_catch_up_oracle.py`, `tests/test_ledger_catch_up.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/cli.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定：`run_cli._sleep(seconds)`、`run_cli._last_operation(ledger, request, task)`、`run_cli._watch_finished(ledger, request, task_ids, jobs)`、`run_cli._NEEDS_OPERATOR`、`run_cli._WAITING`、`run_cli._DEFAULT_POLL_SECONDS`。其余由你定。

1. **命令行**：`register` 在 serve 的 `--parallel` 之前加两项：
   - `--watch`（`store_true`）；
   - `--poll-seconds`（类型 `_count`，缺省 `None`）。

   `--poll-seconds` 写成 `0`、负数或不是数字，都是 argparse 的用法错误（退出 2）。
2. **分派**：`cmd_serve` 文档串之后，先后两条：
   - （i）给了 `--poll-seconds` 却没有 `--watch`：用 `_write_line(sys.stderr, {"error": "E_RUN_USAGE", "detail": "--poll-seconds needs --watch", "operation_id": None})` 写一行，返回 2。什么也不读，账本不动。
   - （ii）给了 `--parallel` 或 `--watch`：`return _serve_parallel(args)`。

   都用 `getattr(…, None/False)` 读：已有测试直接构造的 `Namespace` 没有这两个属性。其余不变。
3. **常量与辅助函数**：
   - `_NEEDS_OPERATOR = frozenset({"NOT_STARTED", "START_UNKNOWN", "CANCELLED_NOT_STARTED", "ABANDONED"})`；
   - `_WAITING = frozenset({"RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "REVIEW_PENDING"})`；
   - `_DEFAULT_POLL_SECONDS = 30`；
   - `_sleep(seconds)`：用若干次不超过 `_POLL_SECONDS` 的 `time.sleep`，等满 `seconds` 秒（以 `time.monotonic()` 计）；
   - `_last_operation(ledger, request, task)`：按 `ledger.operation_ids()` 的顺序，返回 `request_id` 与 `task_id` 都相符的最后一个 `ledger.operation(...)`；没有就返回 `None`；
   - `_watch_finished(ledger, request, task_ids, jobs)`：对 `task_ids` 里每个**有作业**的任务依次判断，全部通过才返回 `True`：
     - `task()` 说 `done`：通过；
     - 否则最后一个操作在 `_WAITING` 里：返回 `False`；
     - 否则次数用尽（`attempts >= max_attempts`）：通过；
     - 否则没有操作，或者最后一个操作是 `REJECTED`：返回 `False`；
     - 否则通过。

     没有作业的任务不看。
4. **宽度与报告的 `parallel`**：宽度是 `--parallel` 的值，没给时为 1。报告的 `"parallel"` 是这个宽度。不加 `--watch` 时与今天相同。
5. **遍**（只在 `--watch` 时；不加 `--watch` 时恰好一遍，没有下面任何多出来的步骤）：
   - 遍数从 1 开始计。
   - 每遍开始时把 `skipped` 清空：报告只给最后一遍的。
   - 在走查的 `try` 里、第一次领取之前，调用 `ledger.refresh()`。它的拒绝按走查的拒绝处理：先收尾，再报告 `refused`。
   - 走查与 serve-parallel 相同，只多一步：在 `done` / `attempts` 的跳过之后、证据目录检查之前，如果 `_last_operation(...)` 不是 `None` 且其状态在 `_NEEDS_OPERATOR` 里，记 skipped `needs_operator`，处理下一个任务。
   - 收尾与 serve-parallel 相同。
   - 这一遍有了停止原因（拒绝、`max_runs`、`max_seconds`）、被中断，或者有运行以拒绝或意外异常结束，就结束整个循环。否则依次：
     - `ledger.refresh()`；
     - `_watch_finished(...)` 为真：`stopped = "finished"`，结束；
     - `--max-seconds` 给了且从第一遍开始算已经用完：`max_seconds`，结束；
     - 否则 `_sleep(--poll-seconds 或 _DEFAULT_POLL_SECONDS)`，进入下一遍。

     这一段里的 `ContractError` / `LedgerError` 是主线程的拒绝（`operation_id` 为 null），`refused`，结束。这一段里的 `KeyboardInterrupt` 是第一次中断：`interrupted`，结束。这时没有运行在跑。
6. **跨遍的计数**：
   - 领取次数（`--max-runs` 与逻辑时钟 `--now + 已领取数`）、领取记录与线程，都在整个调用里累计；
   - `--max-seconds` 从第一遍开始计；
   - 中断与拒绝的规则与 serve-parallel 相同：第一次中断吸收，收尾中的第二次冒出；拒绝按领取顺序列出，主线程的排最后。
7. **报告**：`--watch` 时多两个键，`"passes": 遍数`、`"watch": {"poll_seconds": 用的停顿秒数}`。`runs` 是所有遍的、按领取顺序；`stopped` 多一个值 `finished`；退出码规则不变。不加 `--watch` 时没有这两个键。
8. **文档串**：
   - 模块文档串 serve 一段加一句 `--watch`；
   - `cmd_serve` 文档串末尾一句；
   - `_serve_parallel` 文档串写明第 4–7 条；
   - `_sleep`、`_last_operation`、`_watch_finished` 各有文档串；
   - 导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3.2、5、6 节、oracle 文件，`run_cli.py` 的 `cmd_serve` 开头、`_serve_parallel` 与 `register`，以及 `durable_ledger.py` 的 `refresh`。基线与 Repository facts 不符就停止并报告。
2. 改 `run_cli.py`（第 1–8 条；约 120 行连文档串）。把走查与收尾包进遍的循环时注意缩进，不要复制出第二份 `_serve_parallel`。
3. 跑 V01 直到 oracle 全过（约 8 秒），再跑 V03（serve 的三种用法与 run 的其它命令）。
4. 新建 `tests/test_run_cli_serve_watch.py`，只覆盖下面这些。oracle 已覆盖遍、裁决、`needs_operator`、`finished`、上限、中断、停止、换代、并发与真实进程；可以沿用 oracle 的写法，但不要照抄 oracle 的用例。
   - `_last_operation` 直接调用：没有操作、按领取顺序取最后一个、别的请求的操作不算；
   - `_watch_finished` 直接调用：第 3 条的五种情形各一个，加一个没有作业的任务；
   - `_sleep`：替换 `time.sleep` 记录每次等待，断言每次都不超过 `_POLL_SECONDS`，合计不少于要求的秒数；
   - 真实 `python -m sentinelmesh.cli run serve … --poll-seconds 5`（不加 `--watch`）子进程：退出 2，stdout 为空，stderr 一行 `E_RUN_USAGE`。

   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
5. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告。合同没有写死、需要你自己定的地方，按最合理的读法继续，写进 `interpretations`；与合同矛盾、或没法继续时，停止并写进 `contract_conflicts` 或 `unresolved_questions`。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。

需要截短输出时：
- 照抄执行说明“机器合同摘要”里每条检查的“自检照抄”写法（bash 的写法以 `set -o pipefail;` 开头，管道的退出码就是被测程序自己的）；
- 自己临时写的测试命令要接 `| tail` 之类的管道时，开头同样加 `set -o pipefail;`；
- 不要把输出写到 `/tmp/v01.txt` 这类固定路径。

另外：
- 探针里不要用 `tempfile`；
- 不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改），验收端会在固定候选上做；
- 只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_serve_watch_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_serve_watch.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_run_cli_serve_parallel_oracle.py tests/test_run_cli_serve_parallel.py tests/test_run_cli_serve_oracle.py tests/test_run_cli_serve.py tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_ledger_catch_up_oracle.py` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：1200s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "from sentinelmesh import run_cli as r; assert callable(r._sleep) and callable(r._last_operation) and callable(r._watch_finished); assert r._NEEDS_OPERATOR == frozenset({'NOT_STARTED', 'START_UNKNOWN', 'CANCELLED_NOT_STARTED', 'ABANDONED'}); assert r._WAITING == frozenset({'RESERVED', 'HANDOFF_ACCEPTED', 'RUNNING', 'REVIEW_PENDING'}); assert r._DEFAULT_POLL_SECONDS == 30; print('watch ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`watch ok`
- V08（必要）：`git diff --quiet 1816f1c5f85338f4417d1eeaf2434e53d1970537 -- sentinelmesh/durable_ledger.py sentinelmesh/scheduler.py sentinelmesh/ledger_cli.py sentinelmesh/cli.py sentinelmesh/restart_recovery.py sentinelmesh/adapter_run.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/pi_events.py tests/test_run_cli_serve_watch_oracle.py tests/test_run_cli_serve_parallel_oracle.py tests/test_run_cli_serve_parallel.py tests/test_run_cli_serve_oracle.py tests/test_run_cli_serve.py tests/test_ledger_catch_up_oracle.py tests/test_ledger_catch_up.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

在基线上，V01、V04、V07 失败是因为 `--watch` 还不存在，V02 失败是因为测试文件还不存在。V03 保护 serve 的另外两种用法、run 的其它命令与账本补读，V08 保护十三个不该动的模块与七个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 300 秒。

## Definition of done
- D01：命令行、分派、常量与辅助函数（第 1–3 条）；证据：V01、V02、V07；目的：不加 `--watch` 时一切照旧，`--poll-seconds` 单独出现时什么都不跑。
- D02：遍的循环——补读、`needs_operator`、`finished`、停顿、跨遍的计数（第 4–6 条）；证据：V01、V02；目的：操作员从另一个进程下的裁决会被下一遍看见，被拒绝的任务自动再领，环境坏掉的任务不会每 30 秒烧掉一次尝试，没有能推进的任务时命令自己结束。
- D03：报告与退出码（第 7 条）；证据：V01；目的：一行报告说清走了几遍、跑了什么、为什么结束。
- D04：serve 的另外两种用法与其它命令不受影响，`run_cli.py` 的 diff 只在合同指定的位置，文档串按第 8 条，导入无副作用；证据：V03、V04、V08、review。
- D05：Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 pi。
- 本合同对同一行为说法不同。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 2400
- max_wall_seconds_total: 4800
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配（上限 40 分钟，按 28 分钟规划、留 12 分钟余量）：
  - 阅读约 7 分钟；
  - 实现约 8 分钟（一个文件，约 120 行）；
  - 测试约 5 分钟（4 组用例，先跑单个用例）；
  - 运行检查约 7 分钟（oracle 约 8 秒，V03 约 120 秒，全量约 300 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍写进 `interpretations`（合同原文、你的做法、理由）；与合同矛盾的写进 `contract_conflicts`，没法决定的写进 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
