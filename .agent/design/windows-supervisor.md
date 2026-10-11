# 设计说明：Windows 原生进程监督（windows-supervisor）

状态：用户决定见第 5 节（2026-10-10）；规划端决定见第 6 节；四份合同 `win-identity`、`win-evidence`、`win-supervise`、`win-wiring` 已全部集成（2026-10-11）。
前置：POSIX 监督器三份合同（posix-runtime-runner）、监督记录（supervised_record）、受监督操作（supervised_operation）、adapter 受监督运行（adapter_run）、`read_stream` 的非 POSIX 分支（ledger-reference）、输出流上限（stream-ceilings）都已集成。
本说明重新审视 posix-runtime-runner 第 5 节第 1 条（2026-10-04：“先只做 macOS，Linux 与 Windows Job Object 保持 NOT_RUN”）：当时规划端与执行端都在 macOS 上；2026-10-08 起两端都在这台 Windows Server 2025 上，情况变了，所以再问一次。

## 1. 为什么做

- 实测（2026-10-10，干净工作树 `python -m pytest -q -rs`，781 passed / 289 skipped）：289 个跳过里 271 个（94%）是同一个缺口——“Windows 上没有原生监督器”：

  | 跳过原因（测试里的原话） | 个数 |
  | --- | --- |
  | native POSIX supervisor; Windows Job Object stays NOT_RUN | 228 |
  | POSIX process groups only; Windows Job Object cleanup stays NOT_RUN（旧 `SubprocessRunner`，本说明不碰） | 18 |
  | resolve_executable follows POSIX PATH rules; Windows stays NOT_RUN | 14 |
  | descriptor census through /dev/fd; Windows stays NOT_RUN | 7 |
  | capture_process_identity needs a native boot id; Windows stays NOT_RUN | 5 |
  | 其余（O_NOFOLLOW、SIGKILL、inode 身份等单条） | 4 |
  | 与监督无关的文件系统事实（只读目录、chmod 0、代理对、设备节点等） | 13 |

  产品的核心链路——`supervise` → `record_supervised_run` → `run_supervised_operation` → `run_adapter_operation` 的 `_reduce`——在这台机器上零证据；stream-ceilings 一轮里两处 `_reduce` 的传参只能靠复审的 stub 探针。
- 另一条候选“账本状态有界”先量了再放下：基准脚本（规划端证据，`scratchpad/bench_ledger_growth.py`，一个请求一个任务跑到 verdict 的 9 条命令为一个生命周期）在 1000 个生命周期（9000 条命令）时，状态 3.9 MB，事件占 54%，从检查点打开 58 ms，全量重放 0.41 s，压缩 0.24 s；每个生命周期增加约 3.9 KB。按本仓库现在的节奏（每天一到几次派发）要数年才到 10 MB。它是真实的，但不紧迫，而且要推翻 ledger-compaction 第 5 节第 1 条（检查点含全部事件）。
- 本机探针（2026-10-10，`scratchpad/probe_win32.py`，只启动本机 `python -c` 进程，结束时全部清理）：
  - Job Object：`CreateJobObjectW` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，`Popen` 之后 `AssignProcessToJobObject` 成功；之后子进程再起的孙进程在 Job 内（`IsProcessInJob` 为真，`ActiveProcesses` 为 2）；`TerminateJobObject` 0.2 秒内组长与孙进程都结束（退出码为传入的值），之后 `ActiveProcesses` 为 0；`CREATE_BREAKAWAY_FROM_JOB` 标志下结果相同。规划端的 Python 自己不在任何 Job 里。
  - 身份来源：`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters\BootId` 是 REG_DWORD（本机当前 13，每次启动加一）；`HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` 是 36 字符的 UUID 文本；`GetTickCount64` 可以估算开机时刻（毫秒级，不稳定）；`GetProcessTimes` 能给进程创建时刻（100 ns 单位）。
  - `pi` 在本机是 npm 的 `pi.cmd` 垫片（`shutil.which("pi")` 给 `pi.CMD`），真正的程序是 `C:\Program Files\nodejs\node.exe` 加 `.../pi-coding-agent/dist/bundle/cli.js`；`os.access(X_OK)` 在 Windows 上对任何存在的文件都为真；`shutil.which` 在 Windows 上会读 `os.environ["PATHEXT"]`。
- Code-Kit 的派发器 `scripts/pi_dispatch.py`（`_JobObject`，2.4.2）已经用同一套 Job Object 监督每一次 pi 派发：`CREATE_SUSPENDED` 创建、先 `AssignProcessToJobObject` 再 `NtResumeProcess`（“保证进程的任何子进程都在 Job 内创建，没有监督空窗”）、`QueryInformationJobObject` 的 `ActiveProcesses` 判断残留、`TerminateJobObject` 终止。ledger-compaction a001 在 1800 s 被切断就是它干的。本说明照搬这套已验证的做法。

## 2. 现状

- `posix_supervisor.supervise` 第一行就在非 POSIX 平台上报 `E_RUNTIME_PLATFORM`；`supervised_record` 从它导入 `SupervisedRun` 与 `supervise`，`supervised_operation` 从它导入 `streams_match`。
- `process_identity.boot_id` 只认 darwin 与 linux；`resolve_executable` 的 PATH 规则是 `:` 分隔、`/` 开头、`os.access(X_OK)`。
- 四处 `os.open(..., O_NOFOLLOW | O_NONBLOCK)`（`posix_supervisor` 两处、`supervised_operation` 一处、`supervised_record` 两处）在 Windows 上没有这两个标志；`read_stream` 已经有一个非 POSIX 分支可以参照（lstat 先行、拒绝链接与重解析点、`O_BINARY` 打开、fstat 身份须等于 lstat 身份）。
- Windows 文件事实（2026-10-09 已验证，见记忆与 `.agent/environment.md`）：lstat/fstat/stat 对普通文件给同一 `(st_dev, st_ino)`；符号链接与硬链接能建；打开目录报 `PermissionError`；没有 `O_NOFOLLOW`/`O_NONBLOCK`，有 `O_BINARY`。另一个本轮要用到的事实：CPython 的 `os.open` 不带 `FILE_SHARE_DELETE`，所以文件被本进程开着时既删不掉也改不了名。

## 3. 方案

### 3.1 布局

新模块 `sentinelmesh/windows_supervisor.py`，与 `posix_supervisor` 并列，不改后者一行。同一个 `SupervisedRun`（从 `posix_supervisor` 导入这个纯 dataclass）、同一个 `supervise(spec, *, environment, evidence_dir, worktree_id, allowed_secret_names, before_start)` 签名、同一套检查顺序（平台、规格、cwd、环境摘要、凭据名、worktree id、可执行文件身份、证据目录、钩子）、同一个观察记录（启动成功五键，未启动三键）。在非 Windows 上第一行报 `E_RUNTIME_PLATFORM`。导入无副作用：`ctypes` 在首次 `supervise` 时才加载。

`supervised_record` 按平台选监督器（`os.name == "nt"` 用 Windows 的，`posix` 用 POSIX 的，其它平台照旧 `E_RUNTIME_PLATFORM`）；`supervised_operation` 的 `streams_match` 同样按平台。上层（`run_supervised_operation`、`run_adapter_operation`）一行不改。

### 3.2 身份（第 5 节问题 2）

`boot_id()` 增加 `win32` 分支：读 `MachineGuid` 与 `BootId` 两个注册表值，原样拼成 `<machineguid 小写>:<bootid 十进制>`，例如 `7718b16c-…:13`；`_validated_boot_id` 在 Windows 上按这个形状校验。它每次开机都变（BootId 加一）、换机器就变（MachineGuid）；两个值都是平台原样报告的，不是哈希。读不到任一值（权限、键不存在、类型不对）报 `E_RUNTIME_IDENTITY`，不猜。

`ProcessIdentity` 的四个字段与 Scheduler 的 `_IDENTITY_FIELDS` 不变：pid 复用的护栏（进程创建时刻）不进身份，与 POSIX 一致，记为遗留。

### 3.3 可执行文件（第 5 节问题 3）

`resolve_executable` 增加 Windows 规则，仍然只看调用方给的环境映射：
- PATH 用 `;` 分隔，每一项必须带盘符或 UNC 根（`os.path.splitdrive` 非空且 `isabs`）；
- 含 `\` 或 `/` 的 argv0 是路径，相对路径接在 cwd 之后；
- 裸名只在 PATH 里查，候选是 `<entry>\<argv0>`，以及 argv0 没有扩展名时的 `<argv0>.exe`、`<argv0>.com`；**从不读 `PATHEXT`**，`.cmd`/`.bat` 一律不是可执行文件（它们由 `cmd.exe` 解释，被哈希的文件不是被执行的程序）；
- 解析结果必须是存在的普通文件，扩展名（不分大小写）为 `.exe` 或 `.com`；`realpath` 解析符号链接与 junction；`os.access(X_OK)` 不作为依据。
- 后果：真实 pi 在 Windows 上要以 `["C:\\Program Files\\nodejs\\node.exe", "<cli.js>", "--mode", "json", …]` 的形式给 `InvocationSpec`；身份里的 `executable_sha256` 是 node.exe 的摘要。候选 1b 的接线说明随之更新。

### 3.4 证据文件与流事实

与 POSIX 相同：`stdout.log`/`stderr.log` 在证据目录里 `O_EXCL` 创建（加 `O_BINARY`），各自再按路径开一个只读 pin，pin 的 `fstat` 身份必须等于写描述符的 `fstat` 身份；流事实（device、inode、bytes、sha256）在组被回收之后通过 pin 分块摘要，上限 `MAX_STREAM_DIGEST_BYTES` 不变；`streams_match` 的非 POSIX 分支按 `read_stream` 的做法（lstat 先行、拒绝重解析点、`O_BINARY`、fstat 身份须等于 lstat 身份）。

两点 Windows 差别：
- 钩子抛出时的回滚：先关掉两个 pin 与两个写描述符，再按身份删文件（开着的文件删不掉）；身份比较照旧用删除前的 `lstat`。
- “文件被改名后留在原处”这条 POSIX 性质在 Windows 上不成立（开着就改不了名），测试换成“路径上换了别的文件则不删”。

### 3.5 Job Object 监督（第 5 节问题 4）

- 启动：`Popen(list(spec.command), executable=resolved, cwd=spec.cwd, env=snapshot, stdin=DEVNULL, stdout=fd, stderr=fd, creationflags=CREATE_SUSPENDED, close_fds=True)`；`AssignProcessToJobObject(job, proc._handle)`；`NtResumeProcess`。任一步失败：终止该进程、关 Job，报 `start_failed`/`NOT_STARTED`（与 POSIX 的 `OSError` 分支同一个出口）。Job 带 `KILL_ON_JOB_CLOSE`：监督器自己死了，内核收尾。
- 等待：`proc.wait(timeout=spec.timeout_seconds)`；`TimeoutExpired` → `timeout`；`KeyboardInterrupt` → `cancelled`（报告，不再抛）。
- 停止：`timeout`/`cancelled` 时直接 `TerminateJobObject(job, STATUS_CONTROL_C_EXIT)`（没有 SIGTERM 的对应物；Ctrl-Break 只对带控制台的进程有效且不可靠），stop_reason 为 `killed`；组长的退出码就是 `0xC000013A`（Windows 自己给被 Ctrl-C 杀掉的控制台进程的值），与程序自己的退出 1 可区分。
- 回收：组长结束后轮询 `ActiveProcesses`（`_REAP_POLL_SECONDS` 0.05，上限 `_REAP_SECONDS` 2.0）；正常结束后仍有成员则 `TerminateJobObject`（`stragglers_killed=True`）再轮询；窗口内到 0 则 `cleanup_confirmed=True`，否则 False 且生命周期 `START_UNKNOWN`（与 POSIX 同）。Job 句柄在返回前关闭。
- 中断吸收：停止与回收阶段的 `KeyboardInterrupt` 吸收到窗口结束，与 POSIX 的说明一致（纯 Python 不能密不透风，记为同一条遗留）。Windows 特有的一处窗口（a001 复审 F2）：`Popen` 返回与入 Job 之间落下的中断会留下一个不在任何 Job 里、永远挂起的进程（POSIX 的同一窗口留下的是会跑完的进程）；合同未定义，oracle 钉不住，记为遗留。
- 描述符普查：POSIX 测试用 `/dev/fd` 数描述符；Windows 的 oracle 用 `GetProcessHandleCount` 比较一次运行前后的句柄数（Job、进程、文件句柄都该归还）。

### 3.6 不变的部分

`SupervisedRun`、观察记录、`Lifecycle`、`reconcile`、`replay_decision`、`record_supervised_run` 的记录格式、`run_supervised_operation`、`run_adapter_operation`、`read_stream`、Scheduler、账本格式：一处不改。POSIX 监督器与它的测试一字不改。

## 4. 参考设计

posix-runtime-runner 第 2 节（监督器的保证）与第 3 节（三份合同的切法与 macOS 实测）；Code-Kit `pi_dispatch.py` 的 `_JobObject`（已在本机每次派发中验证）；stream-trust-boundary（输出是证据不是输入）；ledger-reference 的 `read_stream` 非 POSIX 分支（本轮四处 `O_NOFOLLOW` 的样板）。

## 5. 用户决定（2026-10-10，均选推荐项）

1. **范围**：推翻 2026-10-04 的“只做 macOS”，在 Windows 上做 Job Object 监督，分四份合同（身份、证据文件、监督、接线）；Linux 仍 NOT_RUN。
2. **boot id**：`MachineGuid` 与 `PrefetchParameters\BootId` 原样拼接成 `<uuid 小写>:<十进制>`；Windows 上按这个形状校验，其它平台的模式不变。
3. **可执行文件**：只认 `.exe`/`.com`，`.cmd`/`.bat` 拒绝；PATH 用 `;` 分隔且每项带盘符或 UNC 根；裸名只补 `.exe`/`.com`；从不读 `PATHEXT`。真实 pi 要以 node.exe 加 cli.js 的形式给 `InvocationSpec`。
4. **停止语义**：超时或取消直接 `TerminateJobObject`，无宽限期；退出码固定 `0xC000013A`；之后轮询 `ActiveProcesses` 到 0 才 `cleanup_confirmed`，窗口 2 秒与 POSIX 相同。

当时的备选，留作记录：方向——保持 macOS 限定改做账本有界，或只做第 1、2 份；boot id——`uuid5(MachineGuid, BootId)` 派生，或 `GetTickCount64` 估算的开机时刻；可执行文件——从环境快照取 `PATHEXT` 接受 `.cmd`/`.bat`；停止——先 Ctrl-Break 宽限 1 秒再终止。

## 6. 规划端决定

- W1：新模块，不改 `posix_supervisor`；`SupervisedRun` 从它导入，签名与检查顺序照抄；非 Windows 上 `E_RUNTIME_PLATFORM`。
- W2：`ctypes` 只在 `supervise` 里首次加载；导入模块无副作用。
- W3：`CREATE_SUSPENDED` → 入 Job → `NtResumeProcess`，照搬 Code-Kit；入 Job 或恢复失败按 `start_failed` 出口（进程先 `TerminateProcess`）。
- W4：被杀组长的退出码固定为 `0xC000013A`；模块常量 `TERMINATED_EXIT_CODE`。
- W5：回收窗口常量与 POSIX 相同（0.05 / 2.0）；`_TERM_GRACE_SECONDS` 在 Windows 上没有对应物，不设。
- W6：`boot_id` 的 Windows 形状 `<uuid>:<十进制>`（guid 原样，模式对十六进制不分大小写），由 Windows 分支自己的模式校验；`_validated_boot_id` 与 `_BOOT_ID_PATTERN` 不动（darwin/linux 分支在任何平台上都要能用假 libc 测，所以不能按运行平台选模式；2026-10-10 据 a001 执行器指出的分歧修订措辞，合同本来就是这样写的）。
- W7：`resolve_executable` 的 Windows 分支自己做查找，不调 `shutil.which`（它读 `os.environ["PATHEXT"]`）。
- W8：四处 `O_NOFOLLOW | O_NONBLOCK` 各加非 POSIX 分支，做法与 `read_stream` 相同；POSIX 分支文本不动。
- W9：测试：平台守卫改成“posix 跑 POSIX 的、nt 跑 Windows 的”，只对平台中立的用例去掉守卫；需要真实进程树的用例用 `python -c` 起子进程与孙进程；没有 `SIGTERM` 可忽略，Windows 的“不听话的孙进程”就是一个 `time.sleep(60)` 的孙进程。
- W10：证据边界：Windows Server 2025 原生测试结果记为 Windows 原生证据；macOS 的 POSIX 证据不受影响；Linux 仍 NOT_RUN。
- W11（2026-10-11，第 4 份合同前实测）：Windows 的文件身份是 64 位（Python 3.12 的 `st_dev` 是卷序列号，`st_ino` 是 NTFS 文件引用号，高 16 位是序列号），这台机器临时卷上两者都大于 `2**53`，而记录用的 canonical JSON 只收 `core.MAX_SAFE_INTEGER = 2**53 - 1` 以内的整数——接线之前 `record_supervised_run` 在 Windows 上根本写不出 `observation.json`（`E_INTEGER_RANGE`）。规则：记录里 `device`/`inode` 在范围内仍是 JSON 数字（已有记录字节不变），超出范围写成最短十进制文本（可选 `-`，无前导零，ASCII 数字，64 位以内），读回时解码成整数，每个值恰好一种写法（范围内的数写成文本、带符号、前导零、全角数字、超过 64 位都是损坏证据）；`bytes` 永远是数字。内存里的事实（`SupervisedRun.streams`、`recorded_streams`、`read_stream` 的 `expected`）在任何平台都是整数。备选方案都否决了：改 canonical JSON 的整数范围（协议级公开 profile）、把身份截短（丢掉序列号就分不出复用的 MFT 项）、拆成两个键或全部改成文本（改变 POSIX 记录的字节与 schema）。
- W12（2026-10-11）：W9 的“只给平台中立的用例去掉守卫”按实测落地：规划端在草稿上把 14 个 POSIX 守卫文件的守卫临时去掉逐个运行，只有 `test_restart_recovery.py::RecoverAfterRestartRuns` 与 `test_verified_review_gaps.py` 不改一字全过，第 4 份合同去掉这两处守卫；其余守卫类都依赖 POSIX 夹具（非 `.exe` 的坏程序让 `resolve_executable` 先以 `E_RUNTIME_IDENTITY` 拒绝、`os.killpg`/`SIGKILL` 清理、`-signal.SIGKILL` 退出码、FIFO、`fork`/`setsid` 逃逸），保留守卫，它们的 Windows 对应用例在 `tests/test_win_wiring_oracle.py` 与 `tests/test_win_wiring.py` 里；守卫的理由文本改为指向这两个文件。

## 7. 合同拆分（每份一个主要结果，可独立验证；清单 5–8 项，20 分钟计划）

1. `win-identity`（低到中风险）：`boot_id` 的 win32 分支、`_validated_boot_id` 的平台模式、`resolve_executable` 的 Windows 规则（第 3.3 节）、`capture_process_identity` 在 Windows 上成立；原生探针测试记录注册表值的形状（不记值本身）。
2. `win-evidence`（中风险）：`windows_supervisor` 的证据文件、pin、按身份回滚、流事实；`streams_match` 与 `supervised_record`/`supervised_operation` 四处读取的非 POSIX 分支；用 `python -c` 直接写文件的子进程验证流事实（不经 Job）。
3. `win-supervise`（高风险）：Job Object 的启动、等待、终止、回收、中断吸收、句柄普查，`supervise` 完整可用；用真实进程树做原生测试（正常退出、非零退出、超时杀组、取消、孙进程占着输出、启动失败、残留回收、回收未确认）。
4. `win-wiring`（中风险）：`supervised_record`/`supervised_operation` 按平台选监督器；`run_supervised_operation` 与 `run_adapter_operation` 在 Windows 上端到端（stub adapter 真实子进程）；stream-ceilings 两处 `_reduce` 的传参第一次有测试证据；状态文档的 NOT_RUN 行更新。

## 8. 不做

- 不做 Linux；不做三平台一致性比对。
- 不改 `ProcessIdentity` 的字段（pid 复用护栏不进身份）。
- 不做 Ctrl-Break 宽限期、不分配控制台。
- 不改旧 `SubprocessRunner`（它的 18 个 Windows 跳过照旧）。
- 不做 `.cmd`/`.bat` 垫片的执行；不读 `PATHEXT`。
- 不联网、不读凭据、不调用真实 pi（1b 仍另批）。

## 9. 进度与遗留

- 第 1 份 `win-identity` 已集成（2026-10-10）：oracle `2570f59`（pi-base 上为 `0591618`）、a001 `4423c84`、规划端补全 `048bf32`；复审 `.agent/reviews/win-identity/attempt-001.md`（PASS，673 s；`check --fresh` 8/8；变异 31/33 与 23/33；独立复审 PASS）；干净工作树全量 820 passed / 289 skipped。
  - 补全：根检查按合同措辞收紧（盘符或 UNC 之后必须紧跟分隔符，`\\server\share` 单独不算有根）；oracle 加后缀补全与 UNC 根的用例；执行器测试两处用例加严。
  - 遗留：`BootId` 的读取只在 Administrator 账户下验证过（普通账户读 HKLM 一般允许，未实测）；pid 复用的护栏（进程创建时刻）不进身份，与 POSIX 一致。
- 第 2 份 `win-evidence` 已集成（2026-10-11）：oracle `3c14b2a`（pi-base 上为 `3b290ac`）、a001 `bb0fecd`、规划端补全 `776f3a4`；复审 `.agent/reviews/win-evidence/attempt-001.md`（PASS，602 s；`check --fresh` 8/8；变异 30/31 与 10/31；独立复审 PASS）；干净工作树全量 846 passed / 289 skipped。
  - 本轮实测补充的 Windows 事实：`O_CREAT|O_EXCL` 在悬空符号链接处会穿过链接建目标文件（所以独占创建前先 lstat）；本进程开着的文件删不掉、改不了名、也不能被别的文件覆盖（所以回滚先关后删）；没有 `os.pread`（事实用 `lseek(0)` 加 `os.read`）；没有 `O_BINARY` 的描述符是文本模式（写 LF 变 CR LF，读折 CR LF 且在 0x1A 停）。
  - 给第 3 份合同的事实（复审 F4、F5）：`_abandon_evidence` 关掉的描述符号会被下一次 `os.open` 复用，`supervise` 调用它之后必须把四个变量置 None，否则 `finally` 里的二次 `_close_fd` 会关掉别的新描述符；子进程还持有继承来的写句柄时 `os.unlink` 仍是 `PermissionError`，按身份删除要在 Job 终止并确认 `ActiveProcesses == 0` 之后，否则接受残留。lstat 与 `O_EXCL` 之间被植入悬空链接的窗口：拒绝、不泄漏，但穿过链接建出的文件留在链接指向处（永不按路径删除）。
  - 合同措辞遗漏：第 6 条该写 `(OSError, ValueError)`；执行器按“永不抛异常”读对了。
- 第 3 份 `win-supervise` 已集成（2026-10-11）：oracle `dc3179a`（pi-base 上为 `7d3a302`）、a001 `a387800`、规划端补全 `0cf6f8f`（只改 oracle）；复审 `.agent/reviews/win-supervise/attempt-001.md`（PASS，1134 s；`check --fresh` 8/8；变异 31/32 与 16/32；独立复审 PASS，无高/中发现）；干净工作树全量 875 passed / 289 skipped。
  - 本轮实测补充的 Win32 事实：`TerminateJobObject(job, 0xC000013A)` 后 `Popen.returncode` 是 3221225786（无符号）；整棵树 8 ms 内 `ActiveProcesses` 归 0；对挂起进程 `TerminateProcess` 后它没有写过一个字节；子进程环境块的键名被 Windows 转成大写，Python 子进程不需要 `SystemRoot`；`GetProcessHandleCount` 在一个完整周期后回到原值，但本进程第一次失败的 `CreateProcess` 永久多 8 个句柄（复审 F1，普查前要先做一次失败热身）。
  - 给第 4 份合同的事实（复审 F3、F4）：超时/取消路径上 `stragglers_killed` 实测恒为 False（树在等完组长前就空了），与 POSIX 一致，不要给断言它为 True 的用例去掉守卫；`tests/test_supervised_record.py` 直接从 `posix_supervisor` 导入 `supervise`，它与 `test_supervised_operation.py`、`test_adapter_run.py` 用 `os.killpg`/`SIGKILL` 清理，不是平台中立用例（要么保留守卫，要么加 `kill_pid` 分支）；`sentinelmesh/` 里没有代码解读负退出码或信号号，`runtime.py` 只要求 `int`，3221225786 原样通过；替换 `Popen` 的假对象要带 `_handle`。
  - 遗留：集成后第一次在干净工作副本上跑全量出现 2 个失败，当时只保留了最后一行、没有记下是哪两个；紧接着的复跑 875 passed / 0 failed（201 s，机器较忙），四个 Windows 监督器测试文件连跑三遍全过。怀疑是计时类断言在负载下抖动（`duration_seconds < timeout + 1.5`、`pid_gone_within 3 s`），未定位；下次全量要保留完整输出。
  - 变异工具的教训：让中断冒出去的变异体会把 pytest 整个会话打断（退出码 2），kit 的 `mutate` 不算抓住；断言“中断被吸收”的用例要自己 `try/except KeyboardInterrupt` 再判失败。规划端草稿用的变异脚本原来把任何非零退出码都算抓住，已改成只认退出码 1。
- 第 4 份 `win-wiring` 已集成（2026-10-11）：oracle `17503e9`（pi-base 上；同时把 `test_win_evidence_oracle.py` 里“接线仍走 POSIX”的钉子换成委托与平台选择的钉子）、合同 `794db61`、a001 `2c5b4ae`、规划端补全 `640aa4e`；复审 `.agent/reviews/win-wiring/attempt-001.md`（PASS，1447 s；`check --fresh` 9/9；变异 20/25 plus 6/7 once re-anchored on the candidate wording (the one left was an oracle gap, closed in the completion and caught since) 与 6/20）；干净工作树全量 909 passed / 281 skipped。
  - 本轮实测补充：W11 的来由（64 位身份）与 W12 的普查结果见第 6 节；`replay_decision` 对清理已确认的 STOPPED 运行报 `replay_allowed` True（与 POSIX 相同）；取消的运行经桥接后是 `cancelled`/3221225786/`REVIEW_PENDING`；同账户进程在 `mark_running` 里追加 `stderr.log` → `CLEANUP_UNCONFIRMED`，换成另一个文件 → `START_UNKNOWN`（Windows 上子进程不能离开 Job，“逃逸者写入”只能由同账户进程模拟）。
  - 复审 F1–F3 都是注释与措辞（NTFS 文件引用号高 16 位是序列号不是卷序列号；长度窗口按去掉符号后的位数算；“无符号”指无 `+`），补全已改。变异第 1 轮 5 条锚在规划端草稿措辞上而跳过，按候选措辞重锚后 6/7，漏掉的 1 条是 oracle 缺口（几千位的文本让 `int()` 抛异常），补全加用例后单独重跑抓住。执行器如实记下合同引用的 W11–W12 当时尚未写入设计说明——规划端疏忽，本次一并写入。
- 四份合同全部集成，方向收口：Windows 原生 Job Object 监督从 NOT_RUN 变成证据；Linux 仍 NOT_RUN；遗留：F2 的 `Popen` 与入 Job 之间的中断窗口、`SubprocessRunner` 的 18 个 Windows 跳过照旧、`adapter_run` 模块文档串首行仍写 POSIX（规划端补全已改）。
