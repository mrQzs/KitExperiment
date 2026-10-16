# serve 并发：`smctl run serve --parallel N` 同时跑一个请求里的几个任务

状态：设计中（2026-10-15），等用户决定第 5 节。

## 1. 为什么做

serve-loop（2026-10-12）让一条命令按种子顺序把一个请求跑空，但一次只跑一个。它的第 3 节写明：第一版不做并发，因为并发要先说清楚证据目录、worktree 和 `KeyboardInterrupt` 各归谁管，要另起设计。

顺序跑的代价很直接：一个请求有八个任务、每个真实任务要 10 分钟，就是 80 分钟。账本从一开始就给并发留了位置：
- 请求带 `max_concurrency`；
- `Scheduler.claim` 有 `E_CONCURRENCY`、`E_RESOURCE_BUSY`、`E_BUDGET_INSUFFICIENT` 三个拒绝；
- `DurableLedger` 用 `check_same_thread=False` 加实例锁，注释写明一个实例会被多个线程写。

2026-10-15，Code-Kit 2.10.0 做完后，用户说“推进下一轮”。候选有两个：serve 并发，或者给第二个 CLI 写 adapter。选 serve 并发，理由有两条：
- 本机没有任何第二个 CLI 的真实输出流可以照着写（真实调用 NOT_RUN），只看文档写出来的 adapter 是猜的；
- serve 并发可以全部离线测试，而且是 serve-loop 明确留下的缺口。

这一轮也是 Code-Kit 2.10.0 第一次实测，要看三件事：执行器会不会照抄每条检查的自检写法、报告里有没有填 `interpretations`、`integrate` 能不能沿用 `check --fresh` 的结果。合同按 2.8.0 起的约定写 `local_branch, merge`，由 `integrate` 落地。

## 2. 现状（2026-10-15 核对）

**`run_cli.cmd_serve` 现在怎么跑**（`run_cli.py` 第 540 行起）
- 开跑前：先读 `--jobs` 下的全部作业，再打开账本、核对任务。
- 然后按 `task_ids` 的顺序逐个任务处理，每个任务依次经过：上限检查 → `no_job` → `done` / `attempts` → 证据目录必须不存在 → 用派生键 `claim` → 建证据目录，跑 `once` 的那条链（`run_adapter_operation`）。
- 拒绝码分两类：`_SERVE_SKIP`（`E_ACTIVE_TASK`、`E_TASK_DONE`、`E_ATTEMPTS`）只跳过这个任务，其余拒绝让整个循环停在 `refused`。
- `KeyboardInterrupt`：落在两次领取之间的，吸收为 `interrupted`；落在运行当中的不捕获，靠 `running` 标志区分。

**`Scheduler.claim` 的检查顺序**
- 顺序是：…→ `E_ATTEMPTS` → `E_CONCURRENCY` → 预算 → `E_RESOURCE_BUSY`。
- `E_CONCURRENCY`：本请求处在 LIVE 状态的操作数已经达到 `max_concurrency`。
- 预算有三个码：`E_BUDGET_DIMENSION`、`E_USAGE_UNKNOWN`、`E_BUDGET_INSUFFICIENT`，按请求和任务两级检查。暴露量 = 已用 + 还没结算的保留量。
- `E_RESOURCE_BUSY`：同一个 `repo_id` 下，这个资源键正被别的操作持有。
- 资源在操作离开 LIVE 时释放（`_release_resources`），所以进入 `REVIEW_PENDING` 的运行不再占资源。
- 预算在运行结算后，从保留量变成实际用量。

**线程安全**
- `Scheduler` 的每个变更都在 `self._lock` 下。
- `DurableLedger` 的每次读写都在实例自己的 `RLock` 下，SQLite 连接开着 `check_same_thread=False`。
- `run_adapter_operation` 和 `run_supervised_operation` 的状态都放在调用自己的局部变量里。
- `windows_supervisor._win32()` 第一次被调用时才构建 ctypes 绑定，并缓存在模块变量里。两个线程同时第一次进来，会各建一份，后写的那份留下；两份内容相同，所以无害。
- POSIX 监管器只发信号、不装信号处理器（`signal.signal` 只能在主线程装）。不过 POSIX 原生运行在本机测不了。

**Windows 子进程**
- 子进程以 `CREATE_SUSPENDED` 创建，和控制器在同一个控制台、同一个进程组。操作员在控制台按 Ctrl-C，子进程也会收到。
- Job Object 带 `KILL_ON_JOB_CLOSE`：控制器进程一结束，系统关闭句柄，子进程树跟着结束。serve-loop 的 `TerminateProcess` 用例已经证明了这一点。

**已有测试**
- `tests/test_run_cli_serve.py` 是执行器写的测试。
- `tests/test_run_cli_serve_oracle.py` 是规划端写的 oracle，其中两条运行中中断的用例：在 `run_adapter_operation` 里或建目录时抛 `KeyboardInterrupt`，异常必须冒出 serve，而且不写报告。
- 这些用例钉的都是不加新参数时的行为，本方案不改它们。

## 3. 方案草图

新参数 `--parallel N`，N 是不小于 1 的整数。
- 不加这个参数：`serve` 与今天完全一样，同一段代码、同一份报告、同一套中断规则。
- 加了它：进入下面描述的并发模式。

**领取仍在主线程，按种子顺序**
- 主线程按 `task_ids` 的顺序走，每个任务经过的检查和今天一样：上限检查，`no_job` / `done` / `attempts` 跳过，证据目录检查，用派生键领取，逻辑时钟取 `now + 已领取数`。
- 领到之后，把“建证据目录 + 跑 `once` 的链”交给一个工作线程，主线程接着领下一个，直到在跑的运行达到 N 个。
- 每个工作线程只跑一次运行，把结果（或拒绝）交回主线程。
- 整个过程只有一个账本实例。

**领不到时**
- `E_CONCURRENCY`、`E_RESOURCE_BUSY`、`E_BUDGET_INSUFFICIENT` 出现时，如果本次 serve 还有运行在跑，意思就是名额、资源或预算正被自己的运行占着。这时等其中一个结束，再重新领同一个任务（第 5 节第 2 项）。
- 如果本次 serve 一个运行都没在跑，这三个码的意思和今天一样：别人占着，或者预算真的不够。照今天的做法停在 `refused`。
- `_SERVE_SKIP` 的三个码仍然只跳过这个任务。

**两个作业的工作目录相同**
- 检查时机：打开账本之后、第一次领取之前。
- 判断方法：两个作业的 `cwd` 经 `os.path.normcase(os.path.realpath(...))` 处理后相等，而且 `resources` 里没有共同的键。
- 结果：`E_RUN_JOBS`，什么也不跑。
- 原因：两个执行器同时在一个工作目录里改文件，没有哪一层会拦住（第 5 节第 4 项）。

**主线程怎么等**
- 主线程只做带短超时的等待（例如每轮 0.1 秒），从不无限期阻塞。原因是在 Windows 的 Python 3.12 上，没有超时的锁等待可能收不到 Ctrl-C。
- 工作线程设成守护线程：进程结束时不等它们（见下面“第二次 Ctrl-C”）。

**Ctrl-C**
- 第一次 `KeyboardInterrupt`：无论主线程这时在领取、在等待还是在检查上限，都停止领取，然后等所有在跑的运行结束，报告 `interrupted`，退出码 0。这一步不会无限等：每个运行都有自己的 `timeout_seconds`，而且控制台的 Ctrl-C 通常已经让子进程退出了。
- 等待期间的第二次 `KeyboardInterrupt` 不吸收：异常冒出 serve，不写报告，和 `once` 被中断时一样。进程结束时 Job Object 会结束子进程，还在跑的操作留在账本里，由 `smctl run recover` 接管（第 5 节第 3 项）。

**一个运行被拒绝**
- 这里指领取之后的拒绝，例如 `E_RUNTIME_IDENTITY`。
- 处理：停止领取，等其余在跑的运行结束，停在 `refused`，退出码 3。
- 报告的 `refusal` 写第一个拒绝（按主线程收到的先后）；stderr 每个拒绝写一行，格式与 `once` 相同，各自带 `operation_id`。
- 主线程领取时被拒绝，也一样先等在跑的运行结束，再报告。

**报告**
- 键集合不变，只在并发模式下多一个 `"parallel": N`。
- `runs` 按领取顺序排列，不按结束顺序。所以同一份账本、同一组作业，报告是确定的。
- `--max-runs` 数的是已领取的运行（并发模式下领取就等于开跑）。
- `--max-seconds` 仍然只在领取之前检查。
- 到了上限，同样先等在跑的运行结束再报告。

**不变的东西**
- 不改：`once`、`recover`、`load_job`、`load_spec`、`Scheduler`、`DurableLedger`、两个监管器、`run_adapter_operation`。
- 账本里看到的，和 N 个操作员同时各敲一次 `once`、共用一个账本实例的结果相同。
- 不新增账本事件。

**原生证据**（Windows，真实 `smctl` 子进程）
1. **重叠**：用 `init` 建一个 `concurrency` 为 2 的请求，带两个任务。每个作业的子进程先写一个“我开始了”文件，再等对方的文件出现，最多等 20 秒，等不到就退出 2。
   - `--parallel 2` 下两个都正常结束，这只有真正同时运行才可能。
   - 不加 `--parallel` 时，第一个等不到，退出 2，进入复审。
2. **资源**：两个作业共用一个资源键。`--parallel 2` 下，第二个在第一个结束之后才开始（看开始和结束文件的时间顺序）；报告里的顺序仍是种子顺序。
3. **杀进程**：两个运行在跑时，用 `TerminateProcess` 杀掉 serve。两个子进程随 Job Object 结束；之后跑 `recover`，报告里恰好有两个未决操作。
4. **同目录**：两个作业的 `cwd` 相同，又没有共同的资源键，结果是 `E_RUN_JOBS`，账本里没有任何领取。

## 4. 证据边界

- 全部离线：只用 `offline` adapter 和 `python -c` 作业；pi 只走已有的夹具。真实 pi、凭据、联网都是 NOT_RUN。要并发跑真实 pi，得先有一次单独批准的实跑。
- 只在 Windows 上实测。POSIX 监管器在线程里运行的原生行为是 NOT_RUN（本机跳过）。
- Ctrl-C：
  - 进程内用例用替身，让等待或领取抛 `KeyboardInterrupt`。
  - 真实控制台的 Ctrl-C 不放进自动测试，因为在 Windows 上向另一个进程组发 `CTRL_C_EVENT` 不可靠。
  - 第二次 Ctrl-C 的效果等同于杀进程，用第 3 节原生证据第 3 条的 `TerminateProcess` 用例证明。

## 5. 待用户决定

1. **并发宽度从哪里来**
   - 用 `--parallel N`，不加就是今天的顺序 serve（推荐）；
   - 或者直接用请求自己的 `concurrency`：serve 默认就并发，已有的 serve 测试和报告都要跟着改。
2. **排在最前的任务领不到**（名额、资源或预算正被本次 serve 自己的运行占着）
   - 按种子顺序等：等自己的某个运行结束，再重领同一个任务，领取顺序因此是确定的（推荐）；
   - 或者先领后面能领的任务：吞吐高一些，但顺序不确定。
3. **运行当中按 Ctrl-C**
   - 第一次停止领取、等在跑的运行结束、报告 `interrupted`；第二次才放弃它们，交给 `recover`（推荐）；
   - 或者和 `once` 一样，第一次就结束进程，在跑的全部交给 `recover`。
4. **两个作业的工作目录相同**
   - 并发模式下，没有共同资源键就在开跑前拒绝（推荐）；
   - 或者不检查，由操作员自己在作业的 `resources` 里写。

## 6. 规划端决定

- **用线程，不用子进程**：`DurableLedger` 把整份状态放在一个实例里。几个进程各开一份，就看不到彼此的写入。它本来也为多线程写准备了锁。
- **领取全部在主线程**：领取顺序、逻辑时钟和 `--max-runs` 的计数只在一个线程里变化，所以结果确定。工作线程只碰自己那个操作。
- **被拒绝或到上限时，先收尾再报告**：报告里每个开跑的运行都有结果，不会有“开跑了、报告里却没有”的操作。例外是第二次 Ctrl-C，那是操作员明确要放弃。

## 7. 合同拆分

一份合同 `serve-parallel.v1`：
- 执行器改 `run_cli.py`：`cmd_serve` 的并发分支、`register` 里的 `--parallel`，加几个辅助函数。
- 执行器写测试 `tests/test_run_cli_serve_parallel.py`。
- 第 3 节“不变的东西”里列的文件，都列为受保护。

规划端的准备：
- 先写 oracle `tests/test_run_cli_serve_parallel_oracle.py`，包括进程内的替身用例和第 3 节的四条真实子进程用例。
- 再写草稿变异清单，在草稿树上用 `mutate --tree` 验收 oracle，然后派发。

合同的 `integration` 写 `local_branch, merge`。决策模式仍为 off。

## 8. 不做

- 守护进程、轮询等待复审、重试、跨请求并发：一次 serve 仍只管一个请求。
- serve 自己建 worktree：工作目录仍由作业文件给出。
- 改动 `once`、`recover`，或者不加 `--parallel` 时 serve 的任何语义。
- 按 CPU 或内存自动决定宽度。

## 9. 进度与遗留

- 2026-10-15：核对 `cmd_serve`、`Scheduler.claim`、账本和监管器的线程安全，写成本说明。
