# 设计说明：SentinelMesh 原生 POSIX 进程监督（macOS 先行）

状态：用户已确认（2026-10-04），按第 3 节拆分合同
前置：pi-runtime-contract-port 已集成（2c3c115）：canonical InvocationSpec / ProcessIdentity / Lifecycle / reconcile 与 Code-Kit 一致

## 1. 为什么做

`IMPLEMENTATION_STATUS.md` 把“原生 macOS/Linux 进程监督”列为 NOT_RUN。现在规划端与执行端都在 macOS 上，这一行可以第一次取得原生证据（Linux 仍为 NOT_RUN）。

现有 `SubprocessRunner` 在 POSIX 上的缺口（读源码后用真实进程树在 macOS 15.7.9 上实测，2026-10-04）：

1. **超时不生效**。组长与孙进程都忽略 SIGTERM、孙进程继承输出管道时，`timeout_seconds=1` 的 `run()` 实际阻塞了 20.1 秒，正好等到孙进程自己退出。
   - 原因：超时后对进程组发 SIGTERM，1 秒后的 `proc.kill()` 只杀组长；接下来的 `communicate()` 没有时限，一直等管道 EOF。
   - 后果：孙进程永不退出时，`run()` 永远挂起。
2. **清理结果误报**。孙进程的 stdout、stderr 都脱离管道时，`run()` 2.0 秒返回，`cleanup="verified"`，但进程组仍然存活（孙进程残留）。
   - 在 SentinelMesh 里，“清理已确认”正是允许重放的前提，这是失败开放。
3. KeyboardInterrupt 分支同样是 terminate 加一个无时限的 communicate。
4. 子进程继承完整的 `os.environ`，凭据类变量会泄漏给执行进程。
5. 不采集 ProcessIdentity。

探针脚本只启动本机 `python -c` 进程，结束时清理了残留。

## 2. 方案要点（参考 Code-Kit `run_supervised` 的已验证做法）

- 新增独立的监督器，不改旧 `SubprocessRunner` 的行为；旧 API 保持兼容。
- 输入：canonical `InvocationSpec` 加环境映射。启动前用与 Code-Kit 相同的规则计算环境摘要，和 `spec.environment_sha256` 比对，不一致就拒绝启动（fail closed）。不继承 `os.environ`。
- 启动：`start_new_session=True`，stdin 接空设备，stdout/stderr 写到调用方给定的证据目录（文件，不用管道），这样孙进程占用输出时也不会挂起。
- 身份：pid；boot_id（macOS 用 `kern.bootsessionuuid`，Linux 用 `/proc/sys/kernel/random/boot_id`，取不到时报错，不猜测）；argv[0] 解析后的可执行文件 SHA-256；调用方给出的 worktree_id。
- 结束：墙钟时限到达或取消时，先对进程组发 SIGTERM，宽限期后对整个进程组发 SIGKILL。再用 `killpg(pgid, 0)` 确认组已为空：组为空才记 `cleanup_confirmed=True`，否则为 False，并列出残留 pid。
- 输出：一个 reconcile 能直接消费的观察记录，字段为 run_id、environment_sha256、state、process_identity、cleanup_confirmed；同时附 Lifecycle 记录（STARTING → STARTED → STOPPING → STOPPED / FAILED，spawn 结果不确定时为 START_UNKNOWN）。

## 3. 拆分建议（每份一个主要结果，可独立验证）

进度：
- 第 0 份 `runner-posix-cleanup` 已集成（`136657c`，规划端补测 `fd57f49`），并顺带修正了旧 runner 在强杀路径上重复输出的问题。之后规划端又修正了只剩僵尸进程的组返回 EPERM 时的崩溃（`74b4d62`）。
- 第 1 份 `posix-identity-env` 已集成（a001 `154c3ec`、a002 `281c2d2`，规划端补全 `2191050`）。
- 第 2 份 `posix-supervisor` 已集成：
  - 提交：oracle `4ddc790`、a001 `d8acf95`、v2 探针 `b5d434b`、a002 `31082c3`，规划端补全 `cd7c918`。
  - 约束与第 1 份衔接：cwd 必须是绝对路径；PATH 只取快照里的值；凭据类变量默认拒绝、白名单放行。

规划端在 macOS 上实测（2026-10-04、10-05），后续合同应直接引用：
- 进程组只剩未回收的僵尸进程时，`killpg` 对信号 0 与 SIGKILL 都返回 EPERM，不是 ESRCH；
- `kill(僵尸 pid, 0)` 成功，回收后才返回 ESRCH；
- 本机 Python 没有 `os.waitid`；
- CPython 的 `Popen.wait` 第一次收到中断时，会先再等子进程最多 0.25 秒（不超过截止时间），期间可能回收子进程，然后照样重新抛出。

监督器的已知残留：
- 调用 `setsid()` 的后代离开了进程组，不会被回收；
- 落在受保护的等待之外、只有几个字节码长的中断仍会冒出；
- 组长在 SIGKILL 后 2 秒内仍未结束时，`exit_code` 为 None，cleanup 未确认，不放行重放。

第 3 份需要遵守的约束：
- 观察记录的 `state` 是组长的结局；cleanup 未确认时，`reconcile` 给出 STOPPED 或 FAILED，且 `replay_allowed` 为 False；只有生命周期是 START_UNKNOWN；
- 已启动的运行可能 `exit_code` 为 None；
- 观察记录是封闭的：启动成功五键，未启动三键。

1. `posix-identity-env`：环境摘要校验，加上 boot_id 与可执行文件摘要的采集（纯函数，加平台探针），低到中风险。
2. `posix-supervisor`：进程组监督、文件输出、超时与宽限、SIGKILL 升级、残留确认、观察记录，高风险。用真实子进程树做原生测试：忽略 SIGTERM 的孙进程、占用输出的孙进程、快速退出、spawn 失败、取消。
3. `posix-reconcile-integration`：把监督器的观察记录接入 reconcile 的重放判定，端到端测试“超时且清理完成可以重放 / 清理未确认不可重放 / spawn 未知不可重放”。

## 4. 证据边界

- macOS 原生测试结果记为 macOS 原生证据；Linux、Windows Job Object 与三平台一致性仍为 NOT_RUN。
- 不调用真实 provider CLI，不读取凭据；测试只启动 `python -c` 一类本地进程。

## 5. 用户决定（2026-10-04）

1. 范围：先只做 macOS，Linux 与 Windows Job Object 保持 NOT_RUN。（2026-10-10 重新审视：两端都搬到 Windows 之后，用户决定做 Windows Job Object 监督，见 `windows-supervisor.md`；Linux 仍 NOT_RUN。）
2. 环境：新监督器默认不继承父进程环境，只用调用方显式给出的映射，并核对摘要。
3. 旧 `SubprocessRunner` 的两个实测缺陷作为第 0 份合同先修（`runner-posix-cleanup`）。
   - 修复范围：超时与取消时对整个进程组升级到 SIGKILL，收尾读取有时限，残留进程未清净时报 `incomplete`。
   - 正常结束后组内仍有成员时只报 `incomplete`，不主动杀。完整的残留回收留给新监督器（第 3 节第 2 份）。
4. 凭据类环境变量（2026-10-04）：新监督器默认拒绝名称含 TOKEN、KEY、SECRET、PASSWORD、CREDENTIAL、COOKIE、AUTH 的变量（与 Code-Kit `offline_runner` 的判定相同，按大写名称子串匹配）。
   - 调用方用参数显式列出允许的名称时才放行；
   - 证据与观察记录只记变量名，不记值。

## 6. 第 3 份合同的设计（规划端，2026-10-05）

- 目标：把监督器的观察记录落盘，只凭落盘的记录做重放判定。端到端证明三点：超时且清理完成可以重放；清理未确认不可重放；启动结果未知不可重放。
- 不改调度器。`Scheduler` 的状态迁移（`finish`、`unknown`、已交接后的未启动取消）接入监督器另做合同；本份只提供调用方可用的判定。
- 监督器增加钩子 `before_start`：
  - 在两个证据文件建好之后、Popen 之前调用一次；
  - 钩子抛出时，关闭并删除这两个文件，原样重新抛出，不启动进程；
  - 非 None 且不可调用时报 `E_RUNTIME_INVOCATION`（排在证据目录校验之后）。
- 新模块 `sentinelmesh/supervised_record.py`：
  - `record_supervised_run(...)`：在钩子里写 `handoff.json`（启动之前），`supervise()` 返回后写 `observation.json`；
  - `replay_decision(spec, evidence_dir)`：只读这两份文件，返回与 `reconcile` 相同的五键记录：
    - 两份都没有：NOT_STARTED，可以重放；
    - 只有交接：START_UNKNOWN，不可重放；
    - 交接或观察的规格摘要与 spec 不符：IDENTITY_MISMATCH，不可重放；
    - 两份都有且一致：`reconcile(spec, observation)`；
    - 以下情形都报 `E_RUNTIME_EVIDENCE`：只有观察、文件损坏、不是规范 JSON、多余或缺少键、是符号链接、超过 64 KiB。
  - 记录文件：
    - 格式：规范 JSON（排序键、紧凑、UTF-8、末尾换行），权限 0600；
    - 写法：先写到 `.<名称>.partial`（O_EXCL）并 fsync，再用硬链接发布；已存在的记录永不覆盖。
- 证据边界：
  - 进程崩溃时记录保持一致（链接是原子的）；
  - 断电后的持久性不验证：不对目录做 fsync，macOS 的 fsync 也不等于 F_FULLFSYNC。记为 NOT_RUN。

进度（2026-10-05）：第 3 份 `posix-reconcile-integration` 已集成：
- 提交：oracle `a27525e`、a001 `a2c0e71`（ACCEPTED_WITH_EXCEPTION），规划端补全 `c12a88f`。
- 接受的收紧：
  - 观察记录内层取值的类型也要正确（`run_id`、`environment_sha256`、`state` 是 str；`process_identity` 恰好四键，pid 是 int、其余是 str；`cleanup_confirmed` 是 bool），否则报 `E_RUNTIME_EVIDENCE`；
  - 写入只删除本次调用创建的 partial 文件。
- 规划端补全修正了合同遗漏：
  - 交接之前确认两份记录都不存在。没有这一步时，旧的观察记录会被当成本次运行的结果，放行一次确实启动过的运行；
  - 读取加 `O_NONBLOCK`（遇到 FIFO 不再阻塞），最多读 65537 字节；
  - 写入前检查不超过 65536 字节，成功路径上 close 失败也报错；
  - 监督器在启动前按 `ProcessIdentity` 的字符规则校验 `worktree_id`。
- 仍未做：接入 `Scheduler` 与 adapter；断电后的持久性（NOT_RUN）。
