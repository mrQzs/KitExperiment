# 设计说明：操作员驱动的受监督运行（controller-cli）

状态：用户决定见第 5 节（2026-10-11，均选推荐项）；规划端决定见第 6 节；三份合同 `run-cli-once`、`run-cli-recover`、`run-cli-pi` 均已集成（2026-10-11），方向完成；1b 真实 pi 待用户批准凭据与联网。
前置：持久账本与 `smctl ledger`（durable-ledger、ledger-cli、ledger-events、ledger-compaction、ledger-reference）、受监督操作桥接（scheduler-supervisor-wiring）、adapter 受监督运行与流上限（adapter-supervisor-wiring、stream-ceilings）、重启恢复与请求恢复（request-resume）、Pi 原生事件翻译（pi-native-events）、两个原生监督器（posix-runtime-runner、windows-supervisor）都已集成。

## 1. 为什么做

- 整条链路——`claim` → `handoff` → `supervise` → 记录 → `_reduce` → `settle` → `finish`，以及崩溃后的 `recover_after_restart` → `resolve` → `resume`——每一环都有库函数和原生证据，但没有任何一个入口把它们串成一次"操作员能从命令行发起的运行"。今天要让一个 adapter 在账本里跑一次，只能写 Python：建账本、`seed_request`/`seed_task`、`claim`、构造 `Invocation`、调 `run_adapter_operation`、处理返回。`tests/test_request_resume_oracle.py` 的 `CONTROLLER` 脚本（一个被 `kill` 掉的控制器子进程）就是这段 Python 的样子：它证明了"崩溃→恢复→裁定→恢复请求→再跑"的闭环，但产品里没有这个控制器。
- `smctl ledger` 刻意只做"崩溃之后由人接手"的那几步（看状态、补用量、裁定、恢复请求），`open_ledger` 从不创建账本；创建账本、种子请求与任务、发起一次运行、重启后恢复，这四步没有命令。
- 这是产品最后一段没有端到端证据的路径：`cli.py` 的 `main` → `ledger_cli` 只覆盖读与裁定；`run_adapter_operation` 的调用方只有测试。把它做成命令之后，"一个真实的控制器进程被杀掉、重启后从账本恢复"可以在 Windows 上用真实进程原生验证（`TerminateProcess` 控制器，再起一个 `smctl` 进程恢复），而不是靠测试里的 `python -c` 控制器。
- 它也是通往 1b（真实 pi 在监督器下运行）的最后一块：有了命令，1b 只差凭据与联网的批准，不再差代码。本说明不碰凭据与联网。

## 2. 现状

- `smctl`（`sentinelmesh/cli.py`）：`validate-config`、`plugin-lock`、`debug-bundle`、`baseline`、`workflow`、`contract-render`，加上 `ledger_cli.register` 挂上的 `ledger status|reference|compact|events|stop|settle|resolve|resume`。约定：每次调用 stdout 一个 JSON 对象（`sort_keys`，非 ASCII 原样）；账本拒绝 → stderr 一个 JSON 对象（账本自己的 code 与 detail），退出码 3，什么也不改；argparse 拒绝退出码 2；每个命令只做至多一次变更，`finally` 里关账本。
- `durable_ledger`：`create_ledger(path, *, generation=1)`、`open_ledger(path)`（只重放，从不创建）、`DurableScheduler` 把 `Scheduler` 的变更方法逐条写进命令日志；`run_descriptor(op)` 记录交接时的运行描述（规格、证据目录、worktree id）。
- `supervised_operation.run_supervised_operation(scheduler, *, operation_id, owner, generation, spec, environment, evidence_dir, worktree_id, candidate_hash, allowed_secret_names, now, before_finish)`；`adapter_run.run_adapter_operation(scheduler, adapter, invocation, *, cwd, timeout_seconds, environment, owner, generation, evidence_dir, worktree_id, candidate_hash, usage_dimensions, allowed_secret_names, now, max_stream_bytes, max_line_bytes)`；`restart_recovery.recover_after_restart(scheduler, *, owner, candidate_hash)`。
- `adapter_protocol.Invocation(request_id, run_id, role, profile_id, command, capabilities, environment_sha256, adapter_id)`；adapter：`OfflineAdapter`（离线归一化协议，`adapter_id` 由子类定）、`pi_events.PiNativeAdapter`（pi `--mode json` 会话 v3 → 归一化协议，有 31 条真实捕获流的证据）；`AdapterRegistry.resolve(adapter_id, role=...)`。
- 监督器要求显式环境映射（`verified_environment` 对 `environment_sha256`）、显式允许的凭据名；`resolve_executable` 在 Windows 上拒绝 `.cmd`/`.bat` 垫片——`pi` 在本机是 `pi.cmd`，真正的程序是 `node.exe` + `cli.js`（win-identity 已实测）。
- 测试里的控制器（request-resume oracle）：建账本、种子、领取两次、第一次跑完并 `settle`、第二次跑一个等待释放文件的子进程，父进程被 `SIGKILL`；然后 `open_ledger` + `recover_after_restart` + `resolve` + `resume` + 再领取。POSIX 守卫下，Windows 上没跑过。

## 3. 方案草图

新模块 `sentinelmesh/run_cli.py`，由 `cli.py` 挂为 `smctl run`，沿用 `ledger_cli` 的全部输出与退出码约定：

- `smctl run init <ledger> --spec <request.json>`：`create_ledger`（已存在则拒绝，退出码 3），按请求规格文件 `seed_request`/`seed_task`。规格文件是严格校验的 JSON（精确键集合、精确类型；多一个键、少一个键、类型不对都是 `E_RUN_SPEC`，什么也不建）。
- `smctl run once <ledger> --job <job.json> --evidence <dir> --worktree-id <id> --owner <name> [--candidate <64 hex>]`：打开账本，按作业文件 `claim` 一个操作（请求、任务、尝试、幂等键、保留量、资源都来自作业文件），构造 `Invocation`，`run_adapter_operation` 一次，把它返回的 outcome 原样作为 stdout 的 JSON。作业文件里的 `environment` 是显式映射（命令从不读 `os.environ` 给子进程），`allowed_secret_names` 显式列出；adapter 按 `adapter_id` 从一个固定注册表取（第一份合同只有离线桩 `offline`；`pi-session-v3` 第二份合同挂上，但真实 pi 的运行仍 NOT_RUN）。每次调用恰好一次 `claim` 与一次运行：没有循环、没有守护进程。
- `smctl run recover <ledger> --owner <name> [--candidate <64 hex>]`：`recover_after_restart`，报告原样输出。
- `candidate_hash` 的来源见第 5 节问题 2。
- 原生证据：在 Windows 上用真实 `smctl` 子进程跑 `init` → `once`（离线桩，`python -c` 作业）→ 用 `TerminateProcess` 杀掉一个正在 `once` 的控制器 → 另一个 `smctl run recover` → `smctl ledger resolve`/`resume` → 再 `once`。这就是 request-resume oracle 的闭环，第一次以产品命令、在 Windows 上成立。

## 4. 证据边界

- 不读凭据、不联网、不调用真实 pi：`pi-session-v3` 的 adapter 只在作业文件层面可选，真实运行等 1b 批准；离线桩 adapter 的作业用 `python -c` 子进程写归一化协议帧。
- 操作员的权限与裁定依据照旧只记录不核验（request-resume 第 5 节）。
- 断电持久性照旧 NOT_RUN；这里只证明"进程被杀"这一种崩溃。

## 5. 用户决定（2026-10-11，均选推荐项）

1. **命令的范围**：只做 `init`/`once`/`recover` 三个一次性命令，每次调用恰好一次领取与一次运行；不做 `serve` 循环。
2. **`candidate_hash` 的来源**：`once` 与 `recover` 都接受 `--candidate <64 hex>`，缺省用 `evidence_digest(evidence_dir)`。
3. **作业文件的 adapter**：第一份合同只认 `offline`；`pi-session-v3` 第三份合同再挂上，真实 pi 仍 NOT_RUN。

## 6. 规划端决定

- 问题 1 选"三个一次性命令"：每个命令至多一次 `claim`、至多一次运行，与 `smctl ledger` 的"一次调用一次变更"同一口径，被杀掉时账本里至多一个未定案的操作，恢复路径可穷举；`serve` 的循环语义（并发、退避、何时停）是另一个方向。
- 问题 2 选"可选，缺省 `evidence_digest`"：`candidate_hash` 在桥接里是"调用方知道候选是什么"，命令行上调用方是操作员；给缺省值是为了让没有候选树的运行（离线桩、恢复）也能走到复审，而不是停在 `START_UNKNOWN`。
- 问题 3 选"第一份只认 `offline`"：合同范围与证据边界都更干净；`pi-session-v3` 的挂接是一次注册表条目加一组作业文件校验，第二份合同连同 Windows 上 `node.exe` + `cli.js` 的可执行文件解析一起做。
- 规划端自定：作业文件与请求规格的键集合在合同里逐字列出；`once` 对账本的第一次变更是 `claim`，之前的任何校验失败都不碰账本；outcome 的 JSON 形状就是 `run_adapter_operation` 的返回值（`sort_keys`）；超时、流上限、行上限都在作业文件里给，缺省同 `adapter_run` 的缺省。

## 7. 合同拆分（每份一个主要结果；清单 5–8 项，20 分钟计划）

1. `run-cli-once`（中风险）：`run_cli.py` 的 `init` 与 `once`（离线桩），规格/作业文件的严格校验，`cli.py` 挂接；oracle 用真实 `smctl` 子进程在 Windows 上跑 `init` → `once`，核对 stdout JSON、账本状态、证据目录、退出码。
2. `run-cli-recover`（中到高风险）：`recover`；oracle 在 Windows 上用 `TerminateProcess` 杀掉一个正在运行的 `smctl run once`，再用 `smctl run recover` + `smctl ledger resolve`/`resume` + 再一次 `once` 走完闭环；`test_request_resume_oracle.py` 里 POSIX 守卫下的 `CrashToResumeOracle` 得到 Windows 对应物。
3. `run-cli-pi`（中风险，不启动真实 pi）：`pi-session-v3` 挂进注册表，作业文件里 pi 的可执行文件按平台规则解析（Windows 上是 `node.exe` + `cli.js`），用捕获的真实流当桩（`tests/fixtures/pi_native/`）验证从作业文件到用量结算；真实 pi 仍 NOT_RUN。

## 8. 不做

- 不做守护进程、并发领取、退避与调度策略。
- 不读凭据、不联网、不启动真实 pi（1b 另批）。
- 不改 `smctl ledger` 的任何命令，不放宽账本的任何守卫。
- 不做远端账本、多宿主。

## 9. 进度与遗留

- 2026-10-11：用户决定第 5 节三项均选推荐项。
- 第 1 份 `run-cli-once` 已集成（2026-10-11）：oracle `f5f1de5`（pi-base 上）、合同 `c07cce4`、a001 `0106f32`、规划端补全 `5dd47b7`；复审 `.agent/reviews/run-cli-once/attempt-001.md`（PASS，1067 s；`check --fresh` 8/8；变异 36/37 once anchored on the candidate wording (the one left is equivalent: the file ceiling by truncation) 与 11/37）；干净工作树全量 932 passed / 281 skipped。
  - 草稿阶段的事实：一次 `too_large`（用量未结算）的运行让请求因 `USAGE_UNKNOWN` 停止，之后的领取 `E_STOPPED`——这是账本既有规则，oracle 对需要多次运行的用例用新账本；`rollover` 后请求 `RECOVERY_REQUIRED`，`smctl ledger resume` 之后才能在新代际领取（oracle 用它钉住“按账本当前代际领取”）。两条等价/不可观察的变异：文件上限靠读取长度兜底（超限文件被截断后仍以 `E_RUN_SPEC` 拒绝）；成功路径上账本未关闭从进程外看不出来。
  - 复审 F1（中）：嵌套 3000 层的 JSON 让 `json.loads` 抛 `RecursionError`，从加载器逃出去变成退出码 2，补全并进 `except` 并加 oracle 用例；F2/F3（低）文档串与异常策略，补全已改；F4（低，记给第 2 份合同）：环境变量名不合法的作业在领取之后才被 `environment_digest` 拒绝，留下一个 RESERVED 操作——`recover` 要能处理它，或者第 2 份合同给作业文件加名字的预检。
- 第 2 份 `run-cli-recover` 已集成（2026-10-11）：oracle `4d5f484`（pi-base 上 `7f4c00f`）、合同 `68e1592`、a001 `25c3cd8`、规划端补全 `43f4f5a`；复审 `.agent/reviews/run-cli-recover/attempt-001.md`（PASS，605 s；`check --fresh` 8/8；变异 27/28 与 24/28，漏掉的 1 条等价：`RuntimeErrorCode` 是 `ValueError` 的子类）；干净工作树全量 943 passed / 281 skipped。上一份的 F4 由 `load_job` 的环境预检关闭。
  - 实测的事实（Windows，真实 `smctl` 进程）：控制器被 `TerminateProcess` 杀掉后子进程随 Job Object 在 0.02 s 内结束，证据目录只有交接记录与两个流文件，`recover` 报告 `recovered`/`START_UNKNOWN`；`smctl ledger settle` 一次一个维度，第一个维度结算后账本追加 `USAGE_UNKNOWN` 停止理由，`resume` 必须两个理由都点名（库里一次 `settle` 多个维度看不到这条）；按记录恢复到 `REVIEW_PENDING` 的运行用量未结算，`resume` 先 `E_RESUME_USAGE_UNKNOWN`；`recover` 的候选写在 `scheduler.recovered` 事件的 payload 里，不是 `run.finished_for_review`。
  - 复审五个低级发现：F1 `cmd_recover` 文档串把“账本原样”说成了所有拒绝（rollover 之后的 `LedgerError` 会留下已推进的代际），F2 模块文档串的“settles nothing”指用量，F3 执行器测试只比较缺省候选的一个值——补全已改；F4 执行器拒绝用例没有自己的合法基线、F5 报告 JSON 的 `changed_files` 漏列测试文件——记录不改。
- 第 3 份 `run-cli-pi` 已集成（2026-10-11）：oracle `757a7b4`（pi-base 上 `5527374`）、合同 `5732f63`、a001 `c72ee3f`、规划端补全 `5786d0b`；复审 `.agent/reviews/run-cli-pi/attempt-001.md`（PASS，484 s；`check --fresh` 8/8；变异两轮都是 13/15：1 条只插入死代码，1 条是 oracle 盲点——pi 作业背后被加上 `total_tokens` 维度——补全改子集用例后单独验证抓住）；干净工作树全量 953 passed / 281 skipped。
  - 规划端决定（第 6 节的延伸）：注册表按 adapter 自己的 `adapter_id` 键入，所以作业里写 `"pi"`（`pi-session-v3` 是方言/协议版本，不是 id）；命令不解析 pi 的可执行文件——argv0 由监督器按平台规则解析，作业要把真实 pi 写成 `["C:\\Program Files\\nodejs\\node.exe", "<cli.js>", "--mode", "json", …]`，oracle 钉住 `pi.cmd`（按路径或裸名）是 `NOT_STARTED`/`E_RUNTIME_IDENTITY`。
  - 实测的事实：捕获流经 `smctl run once` 结算的五个维度与 `.meta.json` 里 dispatcher 的数字完全一致（Windows 与 macOS 夹具都是），并与 `smctl ledger reference --dialect pi-session-v3` 一致；截断流 `result.status == "UNKNOWN"`、全部维度 skipped、请求 `USAGE_UNKNOWN`；离线事件喂给 pi 是 `E_ADAPTER_SETTLEMENT`，pi 帧喂给 offline 是 `E_ADAPTER_EVENT`；作业只列部分维度就只结算那些。
- 三份合同都已集成：方向完成。1b（真实 pi 在监督器下运行）现在只差批准：一个把 pi 写成 `node.exe` + `cli.js`、`adapter_id: "pi"`、五个 token 维度、`allowed_secret_names` 列出 provider 凭据名的作业文件，经 `smctl run init` → `once` 即可；凭据与联网仍需用户另批。
