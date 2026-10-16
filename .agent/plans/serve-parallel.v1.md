# TASK: serve-parallel

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: e4098921a0491788ef48886255297debab99d6c8
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch, merge
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/serve-parallel.md`（用户决定第 5 节四项均选推荐项；规划端决定第 6 节）给 `smctl run serve` 加 `--parallel N`：主线程按种子顺序逐个领取，每个领到的运行交给自己的守护线程，最多 N 个同时在跑；名额、资源或预算被本次调用自己的运行占着时等一个结束再重领同一个任务；任何停止都先等在跑的运行结束再报告；第一次 Ctrl-C 收尾、第二次放弃；同一工作目录的作业必须有共同资源键。让规划端 oracle `tests/test_run_cli_serve_parallel_oracle.py`（24 个用例）全部通过；新建执行器测试 `tests/test_run_cli_serve_parallel.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改不加 `--parallel` 时的 serve：`cmd_serve` 只在文档串之后加第 2 条的那一行分派，原有循环一字不改；`tests/test_run_cli_serve_oracle.py` 与 `tests/test_run_cli_serve.py` 原样通过。不要把原有循环拆成与并发分支共用的辅助函数。
- 不改 `load_spec`、`load_job`、`load_jobs`、`cmd_init`、`cmd_once`、`cmd_recover`、`_refuse`、`_refusal`、`_emit`、`_ADAPTERS`、`_JOB_*`、`_SERVE_SKIP`、`_clock`、`_count`、`_digits`、`_candidate`、`_constant`：并发分支只是组合它们。
- 不改 `Scheduler`、`durable_ledger`、`ledger_cli`、`adapter_run`、`adapter_protocol`、`supervised_*`、`restart_recovery`、两个监管器、`process_identity`、`runtime`、`pi_events`、`cli.py`。不加账本事件或记录。
- 不做守护进程、重试、跨请求并发、按 CPU 自动定宽度；serve 不建 worktree，工作目录仍由作业给出。
- 纯 Python 吸收中断有残留窗口，不要追求完全吸收：主线程正在账本调用里（领取、读任务视图）时收到 Ctrl-C，账本实例按 durable-ledger 的既有规则失效，之后各运行的账本调用以拒绝结束；领取已经返回、领取记录还没登记时收到 Ctrl-C，那个操作留在 `RESERVED`、不在报告里（与不加 `--parallel` 时领取当中被中断相同）。
- 不联网、不读凭据、不启动真实 pi（`pi` adapter 照常可选，oracle 只用 `offline`）。

## Repository facts
- 基线 = master `2d0f22e`（设计说明初稿）+ 规划端提交 `e4098921a0491788ef48886255297debab99d6c8`，后者含定稿的设计说明（第 5、6 节）与新文件 `tests/test_run_cli_serve_parallel_oracle.py`（24 个用例，一个类 `ParallelOracle`，Windows 上约 20 秒；基线上 23 个失败（另有同目录用例的 3 个子测试）——没有 `--parallel`，argparse 退出 2；`--parallel` 取值的用法错误用例在基线上也是退出 2，所以通过）。oracle 的驱动方式：
  - 进程内 `cli.main(argv)`；真实子进程 `python -m sentinelmesh.cli run serve … --parallel 2` 与 `run recover`；一个被 `kill()`（Windows 上即 `TerminateProcess`）的 serve 控制器；
  - `_thread.interrupt_main()` 从另一个线程向主线程送 `KeyboardInterrupt`：主线程每次等待都必须带不超过 0.1 秒的超时（第 7 条），否则在 Windows 上收不到；
  - `mock.patch.object(run_cli, "run_adapter_operation", …)`：工作线程必须在调用时经模块级名字 `run_adapter_operation` 调用（不要在函数里 `from … import`、不要提前绑定到局部或默认参数）；`mock.patch.object(run_cli, "open_ledger", …)`：同理经模块级名字开账本；
  - 子进程是 `python -c` 程序，在一个标记目录里写 `<任务>.start`、`.met`、`.saw`、`.end`，用来证明两个运行同时在跑，或者一个运行在另一个结束之后才开始。
- `cli.main` 把任何 `Exception` 变成 stderr 上的一行 `error: <异常>` 并返回 2；`KeyboardInterrupt` 不是 `Exception`，原样冒出。
- `sentinelmesh/run_cli.py`（797 行）：
  - `cmd_serve` 约第 537 行，是不加 `--parallel` 时的走查；本合同的每一步都与它的（a）–（g）对应，读它的文档串与代码再动手。`once` 的链在它的（g）里：`Invocation(args.request, operation_id, job["role"], job["profile_id"], tuple(job["command"]), tuple(job["capabilities"]), environment_digest(job["environment"]), job["adapter_id"])` → `run_adapter_operation(ledger, _ADAPTERS[job["adapter_id"]](), invocation, cwd=…, timeout_seconds=…, environment=…, owner=…, generation=ledger.generation, evidence_dir=…, worktree_id=…, candidate_hash=…, usage_dimensions=tuple(…), allowed_secret_names=frozenset(…), now=…, max_stream_bytes=…, max_line_bytes=…)`；
  - `_refusal(exc, operation_id)` 约第 349 行给出 `{"error", "detail", "operation_id"}`；`_write_line`（来自 `ledger_cli`）写一行；`_emit` 写 stdout 的一行；
  - `_count` 约第 727 行（`_digits` 再要求 ≥ 1）；`register` 约第 749 行，serve 的参数到 `--now` 为止；
  - 模块只导入了 `argparse, json, os, re, sys, time`：并发分支还需要 `threading`。
- `Scheduler.claim`（`scheduler.py` 约第 780 行）的拒绝顺序：幂等重放 → … → `E_TASK_DONE` → `E_ACTIVE_TASK` → `E_ATTEMPTS` → `E_CONCURRENCY`（本请求处于 LIVE 的操作数 ≥ `max_concurrency`）→ 预算（`E_BUDGET_DIMENSION`、`E_USAGE_UNKNOWN`、`E_BUDGET_INSUFFICIENT`：请求与任务两级，暴露量 + 本次保留量 > 上限）→ `E_RESOURCE_BUSY`（同一 `repo_id` 下资源键被持有）。LIVE = `RESERVED`、`HANDOFF_ACCEPTED`、`RUNNING`、`START_UNKNOWN`；操作离开 LIVE 时释放资源；运行结束时结算用量，暴露量随之从保留量变成实际用量（oracle 里保留 40、实际 12）。
- 线程：`DurableLedger` 的每次读写都在实例的 `RLock` 下（SQLite 连接 `check_same_thread=False`），`Scheduler` 的变更在 `self._lock` 下，`run_adapter_operation` 的状态都在调用自己的局部变量里，所以一个账本实例可以由主线程领取、由几个工作线程各跑一个操作。桥接在运行结束之后才补登记开始（`mark_running`），所以一个在跑的运行在账本里是 `HANDOFF_ACCEPTED`。`os.makedirs` 自己处理两个线程同时建同一个中间目录。Windows 的子进程与控制器同一控制台（真实的 Ctrl-C 子进程也收到），Job Object 带 `KILL_ON_JOB_CLOSE`（控制器进程结束，子进程树随之结束）。
- 规划端草稿实测（Windows，草稿约 200 行连文档串）：oracle 24/24，连跑四次稳定；43 条故意偏差（宽度多一、不等空位、三种等待码各自不等、等待码改成跳过或丢掉任务、没有自己的运行也等、报告逆序、拒绝不停止领取、停止后不收尾、第一次中断不吸收、第二次中断也吸收、不报 `interrupted`、stderr 只写一个拒绝、拒绝逆序、主线程的拒绝排前、没有 `parallel` 键、意外异常被吞、不查同目录、开账本之后才查、不解析 `..`、共同资源也拒绝、不加 `--parallel` 也查目录、重放不停止、`max_runs` 多一、不查 `max_seconds`、不查证据目录、跳过码不跳过、`done`/`attempts` 不跳过、没有作业也领、键分隔符、序号多一、目录不分尝试、忽略 `--candidate`、运行的拒绝不带操作、状态写死、等待不带超时、等完空位不再看拒绝）里 40 条被 oracle 抓住；3 条预期漏掉：不折叠大小写与逻辑时钟不递增是等价的（Windows 的 `realpath` 对存在的目录已经返回真实大小写；逻辑时钟外部观察不到），工作线程不是守护线程 oracle 观察不到（第二次 Ctrl-C 之后进程能否退出只能用真实控制台验证），由复审核对，所以第 5 条的 `daemon=True` 是硬性要求；草稿树上全量 1037 passed / 281 skipped（基线 1013 加 oracle 24）：没有现有测试钉住旧表面。

## Scope
- allowed_modified_files:
  - sentinelmesh/run_cli.py
- allowed_new_files:
  - tests/test_run_cli_serve_parallel.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_run_cli_serve_parallel_oracle.py`, `tests/test_run_cli_serve_oracle.py`, `tests/test_run_cli_serve.py`, `tests/test_run_cli_oracle.py`, `tests/test_run_cli.py`, `tests/test_run_cli_recover_oracle.py`, `tests/test_run_cli_recover.py`, `tests/test_run_cli_pi_oracle.py`, `tests/test_run_cli_pi.py`, `tests/test_task_done_oracle.py`, `tests/test_review_verdict_oracle.py`, `tests/test_ledger_cli_oracle.py`, `sentinelmesh/cli.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/durable_ledger.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/adapter_protocol.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定：`run_cli._serve_parallel(args)`、`run_cli._shared_directories(jobs)`、`run_cli._SERVE_WAIT`、`run_cli._POLL_SECONDS`。其余辅助函数的名字由你定。执行器测试可以用私有名字；oracle 只通过 `cli.main`、真实子进程、`_thread.interrupt_main()` 与两个 `mock.patch.object` 驱动。

1. **命令行**：`register` 在 serve 的 `--now` 之后加 `--parallel`（类型 `_count`，缺省 `None`；`help` 一句话）。`0`、负数、小数、空串、带空白都是 argparse 的用法错误（退出 2）。
2. **分派**：`cmd_serve` 文档串之后的第一条语句是 `if getattr(args, "parallel", None) is not None: return _serve_parallel(args)`（用 `getattr`：已有测试直接构造的 `Namespace` 没有这个属性）。`cmd_serve` 的其余部分一字不改；文档串末尾可加一句指向 `--parallel`。
3. **常量**：`_SERVE_WAIT = frozenset({"E_CONCURRENCY", "E_RESOURCE_BUSY", "E_BUDGET_INSUFFICIENT"})`；`_POLL_SECONDS = 0.1`。各带一段 `#:` 注释，写明含义。
4. **开跑前的检查**（`_serve_parallel` 依次做；任何一条拒绝都按第 10 条报告为 `refused`，`runs`、`skipped` 为空，`operation_id` 为 null，什么也没领）：`load_jobs(args.jobs, args.request)` → `_shared_directories(jobs)`（在打开账本之前）→ `open_ledger(args.ledger)` → `ledger.task_ids(args.request)` → 目录里有作业而请求没有的任务 → `RunSpecError("E_RUN_JOBS")`，detail 与不加 `--parallel` 时相同 → `candidate_hash` 与不加 `--parallel` 时相同。`_shared_directories(jobs)`：按任务 id 排序后逐对比较（先按第一个任务、再按第二个任务的顺序），两个作业的 `os.path.normcase(os.path.realpath(job["cwd"]))` 相等、而 `set(resources)` 没有交集 → `RunSpecError("E_RUN_JOBS", detail)`，detail 含两个任务 id 与第一个作业写的 `cwd`；只报第一对；没有这样的一对就返回 `None`。
5. **走查（只在主线程）**：按 `task_ids` 的顺序，对每个任务重复下面的步骤，直到这个任务被跳过、被领取或走查停止：
   - （s）**空位**：本次调用在跑的运行（工作线程还活着的）有 N 个时，等其中一个结束（第 7 条）；
   - （h）**有运行失败就停**：本次调用已经结束的运行里有拒绝或意外异常（第 6 条）→ 停止领取（不再考虑任何任务）；
   - （a）`--max-runs` 给了且本次调用已经领取的次数 ≥ max_runs → `max_runs`，停止；（b）`--max-seconds` 给了且 `_clock() - started >= max_seconds`（`started` 是走查开始前读的一次 `_clock()`）→ `max_seconds`，停止；
   - （c）没有作业 → skipped `no_job`；（d）`ledger.task(...)`：`done` → skipped `done`，否则 `attempts >= max_attempts` → skipped `attempts`；（e）`attempt`、证据目录、目录已存在 → `RunSpecError("E_RUN_EVIDENCE")`，与不加 `--parallel` 时逐字相同，停止；
   - （f）派生键 `"%s#%d" % (job["idempotency_key"], attempt)`，`now = --now + 本次调用已经成功领取的次数`，`ledger.claim(...)` 的参数与不加 `--parallel` 时相同：拒绝码在 `_SERVE_SKIP` 里 → skipped，reason 是该码；拒绝码在 `_SERVE_WAIT` 里**且**本次调用有运行在跑 → 等其中一个结束（第 7 条），然后对同一个任务从（s）重来；其它拒绝（包括没有运行在跑时的 `_SERVE_WAIT`）→ 主线程的拒绝，`operation_id` 为 null，停止；`claim["replayed"]` → `RunSpecError("E_RUN_CLAIM_REPLAYED", "operation <id> is already <state>")`，`operation_id` 是被重放的操作，停止，**不运行**那个操作；
   - （g）领到之后：先把领取记录（任务、`attempt`、`operation_id`、证据目录、这次领取的 `now`、结果占位）按领取顺序登记，再启动一个守护线程（`daemon=True`）跑第 6 条；接着处理下一个任务。
   “停止”就是不再领取任何任务；之后一律进入第 8 条的收尾。
6. **一次运行（工作线程）**：`os.makedirs(证据目录)`，按不加 `--parallel` 时（g）的写法构造 `Invocation` 并经模块级名字调用 `run_adapter_operation` 恰好一次（`generation=ledger.generation`，`now=` 这次领取的 `now`）。结果在线程结束之前写回它自己的领取记录：返回的 outcome → 记它的 `state`；`AdapterError`、`ContractError`、`LedgerError`、`RuntimeErrorCode` → 记 `_refusal(exc, 这次的 operation_id)`；其它任何异常（`BaseException`）→ 原样记下，交给主线程。工作线程不写 stdout、stderr。主线程判断“结束了”只看线程是否还活着，从不从队列里取结果。
7. **等待**：主线程每次阻塞不超过 `_POLL_SECONDS` 秒（等空位、等 `_SERVE_WAIT` 的重领、收尾都一样），例如 `threading.Event.wait(_POLL_SECONDS)` 或 `Thread.join(_POLL_SECONDS)` 之后重新检查；“等其中一个结束”就是等到本次调用在跑的运行数比开始等的时候少。
8. **收尾与中断**：走查停止（任何原因，包括主线程的拒绝与第一次 Ctrl-C）或走完所有任务之后，等本次调用的所有运行结束，再在 `finally` 里关闭账本。主线程在走查或收尾里收到的**第一个** `KeyboardInterrupt` 被吸收：停止领取，继续收尾，记下“被中断”；收尾当中再收到一个 `KeyboardInterrupt`（第二个）不吸收：它冒出 `_serve_parallel`（账本照常在 `finally` 里关闭），不写 stdout、stderr，在跑的运行留给 `smctl run recover`（守护线程不拖住进程退出，进程结束时 Job Object 结束子进程）。开跑前的检查（第 4 条）里的 `KeyboardInterrupt` 不吸收，与不加 `--parallel` 时相同。
9. **意外异常**：收尾结束、账本关闭之后，若有运行记下了意外异常，按领取顺序把第一个抛出去，不写报告（`cli.main` 照常写 `error: …`、退出 2）。主线程自己的意外异常照常冒出，不收尾。
10. **报告与退出码**（没有意外异常时）：
    - 拒绝列表 = 各运行的拒绝按领取顺序，再加主线程自己的拒绝（若有，排在最后）；
    - `stopped`：拒绝列表不空 → `refused`；否则被中断过 → `interrupted`；否则走查以 `max_runs` / `max_seconds` 停止的 → 该值；否则 `drained`；
    - stdout 恰好一行 `_emit`：`{"command": "serve", "request", "runs", "skipped", "stopped", "refusal", "parallel": N}`；`runs` 按领取顺序，只列有 outcome 的运行，每项恰好五键 `{"task_id", "attempt", "operation_id", "state", "evidence_dir"}`；`skipped` 与不加 `--parallel` 时相同；`refusal` 是拒绝列表的第一项或 null；
    - stderr：拒绝列表的每一项一行（`_write_line(sys.stderr, …)`），按同一顺序；没有拒绝时为空；
    - 退出码：有拒绝为 3，否则 0。
11. **文档串**：模块文档串 serve 一段加一句 `--parallel`；`_serve_parallel` 的文档串写明第 4–10 条（领取只在主线程、等待码只在自己有运行在跑时等、先收尾再报告、两次中断、拒绝的顺序、报告的 `parallel`）；`_shared_directories` 与工作线程函数各有文档串；导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明、oracle 文件，`run_cli.py` 的 `cmd_serve`（文档串与（a）–（g））、`_refusal`、`register`，`scheduler.py` 的 `claim`。基线与 Repository facts 不符就停止并报告。
2. 改 `run_cli.py`（第 1–11 条；约 200 行连文档串：常量约 12 行、`_shared_directories` 约 15 行、工作线程函数约 30 行、`_serve_parallel` 约 130 行、`register` 3 行、分派 2 行）。
3. 跑 V01 直到 oracle 全过（约 20 秒）；某个用例挂住时先看第 7 条（每次等待都要带超时）。
4. 跑 V03 确认不加 `--parallel` 的 serve 与 `run` 的其它命令未受影响。
5. 新建 `tests/test_run_cli_serve_parallel.py`，只覆盖下面这些（oracle 已经覆盖重叠、宽度、三种等待码、同目录、各种停止、两次中断、被杀的控制器、意外异常；可以沿用 oracle 的 harness 写法，但不要照抄 oracle 的用例；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - `_shared_directories` 直接调用：不同目录的三份作业返回 `None`；两份同目录、有共同资源键返回 `None`；两份同目录、资源不相交 → `E_RUN_JOBS`，detail 含两个任务 id；两对都违反时报按排序的第一对（例如 `task-1`/`task-3` 与 `task-2`/`task-4` 各在一个目录：detail 含 `task-1` 而不含 `task-2`）；
   - 进程内 `cli.main`：两个任务、各自目录、`--parallel 2` → 退出 0、stderr 空、报告恰好七键、`parallel` 为 2、`runs` 按任务顺序、`drained`；`--parallel 2 --max-runs 1` → 恰好一次运行、`max_runs`、账本里一个操作；
   - 拒绝时的形状：`--parallel 2` 而作业目录不存在 → 退出 3，stdout 的 `refusal` 等于 stderr 唯一的那一行，报告有 `parallel`；
   - 真实 `python -m sentinelmesh.cli run serve … --parallel 2` 子进程：作业目录不存在 → 退出 3，stdout 一行、stderr 一行；`--parallel 0` → 退出 2。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告。合同没有写死、需要你自己定的地方，按最合理的读法继续，写进 `interpretations`；与合同矛盾、或没法继续时，停止并写进 `contract_conflicts` 或 `unresolved_questions`。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出时，照抄执行说明“机器合同摘要”里每条检查的“自检照抄”写法（bash 的写法以 `set -o pipefail;` 开头，管道的退出码就是被测程序自己的）；自己临时写的测试命令要接 `| tail` 之类的管道时，开头同样加 `set -o pipefail;`。不要把输出写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_serve_parallel_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_run_cli_serve_parallel.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_run_cli_serve_oracle.py tests/test_run_cli_serve.py tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_run_cli_recover.py tests/test_run_cli_pi_oracle.py tests/test_run_cli_pi.py tests/test_task_done_oracle.py tests/test_review_verdict_oracle.py tests/test_ledger_cli_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "from sentinelmesh import run_cli; assert callable(run_cli._serve_parallel) and callable(run_cli._shared_directories); assert run_cli._SERVE_WAIT == frozenset({'E_CONCURRENCY', 'E_RESOURCE_BUSY', 'E_BUDGET_INSUFFICIENT'}); assert run_cli._POLL_SECONDS == 0.1; assert run_cli._SERVE_SKIP == frozenset({'E_ACTIVE_TASK', 'E_TASK_DONE', 'E_ATTEMPTS'}); print('parallel ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`parallel ok`
- V08（必要）：`git diff --quiet e4098921a0491788ef48886255297debab99d6c8 -- sentinelmesh/cli.py sentinelmesh/scheduler.py sentinelmesh/durable_ledger.py sentinelmesh/ledger_cli.py sentinelmesh/adapter_run.py sentinelmesh/adapter_protocol.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/restart_recovery.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/pi_events.py tests/test_run_cli_serve_parallel_oracle.py tests/test_run_cli_serve_oracle.py tests/test_run_cli_serve.py tests/test_run_cli_oracle.py tests/test_run_cli.py tests/test_run_cli_recover_oracle.py tests/test_run_cli_recover.py tests/test_run_cli_pi_oracle.py tests/test_run_cli_pi.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为 `--parallel` 还不存在；V02 是因为测试文件还不存在。V03 保护不加 `--parallel` 的 serve、`run` 的其它三个命令、pi adapter、task-done、复审裁决与 `smctl ledger`；V08 保护十四个不该动的模块与九个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 190 秒。

## Definition of done
- D01：命令行、分派与常量（第 1–3 条）；证据：V01、V02、V07；目的：不加 `--parallel` 时一切照旧，加了才进入并发走查。
- D02：开跑前的检查与同一工作目录的规则（第 4 条）；证据：V01、V02；目的：两个执行器不会在没有共同资源键的情况下同时改同一棵树，任何作业有问题时什么都不跑、账本不动。
- D03：主线程的走查——空位、跳过、停止、派生键、证据目录、三种等待码只在自己有运行在跑时等、重放不运行（第 5 条）；证据：V01、V02；目的：账本里看到的与按种子顺序手敲 `once` 相同，只是最多 N 个同时在跑，领取顺序与报告确定。
- D04：工作线程、等待、收尾、两次中断与意外异常（第 6–9 条）；证据：V01；目的：报告里每个开跑的运行都有结果，第一次 Ctrl-C 得到报告，第二次放弃时在跑的操作留给 `recover`，任何情况下都不挂住、不丢结果。
- D05：报告、拒绝的顺序与退出码（第 10 条）；证据：V01、V02；目的：被脚本调用时一行就能读到跑了什么、为什么停，每个被留在账本里的操作都在 stderr 上被点名。
- D06：不加 `--parallel` 的 serve 与其它命令、模块不受影响，`run_cli.py` 的 diff 只在合同指定的位置，文档串按第 11 条，导入无副作用；证据：V03、V04、V07、V08、review。
- D07：Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
  - 阅读约 6 分钟；
  - 实现约 9 分钟（一个文件，约 200 行）；
  - 测试约 6 分钟（4 组用例，先跑单个用例）；
  - 运行检查约 5 分钟（oracle 约 20 秒，V03 约 60 秒，全量约 190 秒）；
  - 执行报告约 2 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D07，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍写进 `interpretations`（合同原文、你的做法、理由）；与合同矛盾的写进 `contract_conflicts`，没法决定的写进 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
