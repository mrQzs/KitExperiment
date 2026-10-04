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

进度：第 0 份 `runner-posix-cleanup` 已集成（`136657c`，规划端补测 `fd57f49`），并顺带修正了旧 runner 在强杀路径上重复输出的问题。

1. `posix-identity-env`：环境摘要校验，加上 boot_id 与可执行文件摘要的采集（纯函数，加平台探针），低到中风险。
2. `posix-supervisor`：进程组监督、文件输出、超时与宽限、SIGKILL 升级、残留确认、观察记录，高风险。用真实子进程树做原生测试：忽略 SIGTERM 的孙进程、占用输出的孙进程、快速退出、spawn 失败、取消。
3. `posix-reconcile-integration`：把监督器的观察记录接入 reconcile 的重放判定，端到端测试“超时且清理完成可以重放 / 清理未确认不可重放 / spawn 未知不可重放”。

## 4. 证据边界

- macOS 原生测试结果记为 macOS 原生证据；Linux、Windows Job Object 与三平台一致性仍为 NOT_RUN。
- 不调用真实 provider CLI，不读取凭据；测试只启动 `python -c` 一类本地进程。

## 5. 用户决定（2026-10-04）

1. 范围：先只做 macOS，Linux 与 Windows Job Object 保持 NOT_RUN。
2. 环境：新监督器默认不继承父进程环境，只用调用方显式给出的映射，并核对摘要。
3. 旧 `SubprocessRunner` 的两个实测缺陷作为第 0 份合同先修（`runner-posix-cleanup`）。
   - 修复范围：超时与取消时对整个进程组升级到 SIGKILL，收尾读取有时限，残留进程未清净时报 `incomplete`。
   - 正常结束后组内仍有成员时只报 `incomplete`，不主动杀。完整的残留回收留给新监督器（第 3 节第 2 份）。
