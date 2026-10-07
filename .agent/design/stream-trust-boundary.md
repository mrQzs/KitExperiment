# 设计说明：输出流的信任边界（macOS）

状态：用户同意方向（2026-10-07：“按你的建议来”），规划端按第 6 节决定，按第 4 节拆分合同
前置：adapter 接入监督运行已集成（`.agent/design/adapter-supervisor-wiring.md`），复审发现 F1 与 F3

## 1. 为什么做

adapter-supervised-run 复审的 F1：

- `_reap_group` 只看进程组。子进程 fork 出孙进程后退出并被回收，孙进程 `setsid()` 之后继续持有 stdout；监督器照样判定清理已确认，操作进 REVIEW_PENDING，用量照常记入。
- 孙进程之后还能追加或改写 stdout，记录的 `evidence_hash` 与最终的 `stdout.log` 不同。
- 改名或硬链接换上来的普通文件也会被当作流读取：读取只用了 `O_NOFOLLOW`。

所以现在记录的用量只是厂商的声明。目标是：**输出流在监督器回收进程组之后的任何改动，都能被发现**；在活路径上，发现了就不进入复审，也不记用量。

F3 一并处理：`usage_dimensions` 没有与操作的预约维度对账，列入未预约的维度要等进程跑完才在 `settle` 里报错。

## 2. 能做到什么，做不到什么

现实的风险主要不是恶意厂商，而是会漏进程的 CLI：留下一个后台助手，它继承了 stdout，还在往里写。

- **能发现**：回收之后流被追加、截断、原地改写；路径被换成另一个文件（改名、硬链接、符号链接、FIFO、目录），即使内容相同；文件被删除。
- **不能区分**：逃逸的后代在监督器计算摘要之前写入的内容，与运行本身的输出无法区分。
- **进程组之外的逃逸检测**，在 macOS 上没有可靠做法，不做：
  - 没有 cgroup；`setsid()` 之后父进程变成 launchd，进程树上不留痕迹；
  - 按“谁还持有这个文件”扫描（libproc）抓不住先关掉描述符、之后再按路径打开文件的进程；
  - 只能靠特权或沙箱，超出本项目的证据范围。
- **换代恢复**只有磁盘上的记录。同账户的进程能把流和记录一起伪造，所以恢复路径上的核对是相关性，不是认证；它挡得住上面那种漏进程的写入者。活路径以本进程内存里的事实为准。

## 3. 方案

1. **钉住证据文件**（监督器）：
   - 两个证据文件用 `O_EXCL` 创建之后，各按路径再开一个只读描述符（`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`），并用 `fstat` 核对它和写描述符是同一个文件（`st_dev`、`st_ino`）；
   - 这一步在 `before_start` 钩子与 `Popen` 之前完成，失败时与现有的证据文件回滚一样：关闭描述符、删掉两个文件、报 `E_RUNTIME_EVIDENCE`，不启动任何进程；
   - 子进程拿到的仍是只写的描述符。
2. **流的事实**（监督器）：
   - 进程组回收之后，对每个钉住的描述符 `fstat`，再用 `os.pread` 从偏移 0 读到当时的大小为止，算 SHA-256；
   - 事实为 `{"device": st_dev, "inode": st_ino, "bytes": 读到的字节数, "sha256": 小写十六进制}`；
   - 某个流读不了（`OSError`）时，它的事实为 `None`，不抛错。
   - `SupervisedRun.streams = {"stdout": 事实或 None, "stderr": 事实或 None}`；没有进程启动时为 `None`。
3. **观察记录 v2**（记录模块）：
   - 外层新增 `streams`，取值同 `SupervisedRun.streams`；schema 升为 `sentinelmesh-observation-v2`；
   - `observation`（`reconcile` 消费的五键或三键记录）不变；`replay_decision` 的判定不变。
4. **核对**：
   - `posix_supervisor.streams_match(evidence_dir, streams)`：两个文件都与事实一致才返回 True；读取以记录的字节数为界，从不抛错；
   - `supervised_record.recorded_streams(spec, evidence_dir)`：读出观察记录里的 `streams`，供换代恢复使用。
5. **复审之前核对**（桥接与 adapter，第 3 份合同）：
   - 活路径：`mark_running` 之后、求候选哈希之前，用内存里的 `run.streams` 核对磁盘上的两个流；不一致就 `unknown(reason="CLEANUP_UNCONFIRMED")`；
   - 恢复路径：只有记录里的 `streams` 与磁盘一致时才判 FINISHED，否则 START_UNKNOWN；
   - 桥接把内存里的 `SupervisedRun` 交给新增的 `before_finish` 钩子；adapter 在钩子里归约，核对自己读到的字节与 `run.streams["stdout"]` 一致，用监督器的摘要作为用量的 `evidence_hash`；
   - 维度对账：交接之前，`usage_dimensions` 必须是该操作预约维度（去掉 `dispatches`）的子集；`spec.request_id` 必须等于操作所属的 request。
6. **调度器的只读视图**（第 2 份合同）：新增 `Scheduler.operation(operation_id)`，返回操作的 request、task、状态与预约的副本；顺带修正 `rollover()` 在代际到达 2**53-1 时先改账本、后报错的问题。

## 4. 合同拆分

1. `supervisor-stream-facts`：第 3 节第 1–4 条，改 `posix_supervisor.py` 与 `supervised_record.py`。风险高（监督器）。
2. `scheduler-operation-view`：第 3 节第 6 条，只改 `scheduler.py`。风险低。
3. `verified-review`：第 3 节第 5 条，改 `supervised_operation.py` 与 `adapter_run.py`，依赖前两份。

## 5. 不做

- 不做进程组之外的逃逸检测（第 2 节）。
- 不限制子进程的输出：监督器照旧不截断、不拒绝输出；只是超过 E10 上限的流不做摘要。
- 不改 `reconcile` 与五键观察记录；不承诺断电之后记录仍在（与 posix-runtime-runner 第 6 节相同）。
- Linux 与 Windows Job Object 仍是 NOT_RUN；不调用真实 provider CLI，不读取凭据，不联网。

## 6. 规划端的决定（2026-10-07）

- E1：事实取自钉住的描述符，不取自路径。子进程在运行中改名或删除 `stdout.log`，事实仍描述它真正写入的那个文件。
- E2：事实在进程组回收之后计算，以当时 `fstat` 的大小为界，增长中的文件不会让摘要读不完。
- E3：记录位置选外层 `streams`，不进 `observation`：`reconcile` 的输入合同与已有用例都不变；旧的 v1 记录被新读取方拒绝，按损坏的证据处理（恢复时为 START_UNKNOWN，保守）。
- E4：摘要出错时事实为 `None`，不抛错。进程已经运行过，这时抛出 `RuntimeErrorCode` 会被桥接当成“启动前被拒”，把真实运行释放成 NOT_STARTED。
- E5：摘要期间的 `KeyboardInterrupt` 不吸收，照样冒出，钉住的描述符照样关闭；桥接随后按记录判为 START_UNKNOWN。进程组此时已经回收，没有必须做完的清理。
- E6：活路径上核对不一致时分两种原因（第 3 份合同起草时细化）：
  - 两个路径上仍是监督器钉住的文件，只是内容或大小在回收之后变了：`CLEANUP_UNCONFIRMED`，组外还有写入者活着；
  - 文件缺失、被换成别的文件，或者流根本没能摘要：`START_UNKNOWN`，证据不可用。运行中途替换文件也属于这一类，那时清理本身并没有问题。
- E7：stderr 与 stdout 一起钉住、一起核对：它同样能暴露组外的写入者。
- E8：`device` 与 `inode` 记为 JSON 整数。超过 2**53-1 的 inode 写不进规范 JSON，观察记录写入失败，桥接按 START_UNKNOWN 处理；APFS 的 inode 远小于这个值。
- E9：桥接通过新的 `before_finish(run)` 钩子把内存里的 `SupervisedRun` 交给调用方，时机在流核对之后、求候选哈希之前。adapter 在钩子里结算用量，证据哈希取监督器的摘要；它自己读到的字节还要再与内存里的事实比一次，因为桥接核对之后流仍可能被改。

- E10：摘要设上限 `MAX_STREAM_DIGEST_BYTES = 256 MiB`（supervisor-stream-facts a001 复审 R1 之后决定）。
  - 子进程拿到的写描述符不带 `O_APPEND`，一次 `ftruncate` 就能把文件变成很大的稀疏文件而什么都不写；不设上限时，监督器要读满它才能返回，内存还随之增长。
  - 超过上限的流没有事实（None），什么都不读；`streams_match` 遇到超过上限的事实直接返回 False。这样的运行进不了复审（第 3 份合同里是 START_UNKNOWN）。
  - 摘要逐块更新，内存不随流增长；256 MiB 远大于 adapter 能读的流（至多 16 MiB），也远大于本机实测的 Pi 会话输出（约 6 MB）。
  - 这修改了第 5 节“不给流设字节上限”：子进程的输出仍不受限制，受限的只是监督器愿意摘要的大小。

以上决定都可以在后续合同里修改；改动时更新本节。

## 7. 进度

- 第 2 份 `scheduler-operation-view` 已集成（2026-10-07）：oracle `91737ff`、a001 `88dff4b`（PASS）。
- 第 1 份 `supervisor-stream-facts` 已集成（2026-10-07）：
  - 提交：oracle `c3d8728`、a001 `9f582d5`（REVISE）、规划端测试 `9d1d498`、a002 `6a10888`（PASS）、规划端补全 `d69d45d`；
  - a001 的独立复审发现三个问题：
    - 子进程一次 `ftruncate` 就能让监督器读满任意大小的稀疏文件；
    - `streams_match` 遇到含 NUL 或孤立代理字符的路径会抛出；
    - 回滚按路径删除。

    由此新增决定 E10；
  - 规划端补全：取身份放进回滚的保护之内；补测 `lstat`、“超过上限不打开”，并把顺序用例改成确定性的。
- 第 3 份 `verified-review` 已集成（2026-10-07）：
  - 提交：oracle `5a2eae7`、a001 `69ddefc`（PASS）、规划端补全 `4fb0ceb`；
  - 独立复审建议 REVISE：流核对排在 `mark_running` 之后，却不在把异常转成 START_UNKNOWN 的保护之内，核对期间一次中断就会让操作停在 RUNNING。合同第 2 条没有要求这一点，所以由规划端补全，并补测交接之前各项检查的先后。
- 已知残留：
  - 逃逸的后代在事实取定之前写入的内容，与运行本身的输出无法区分；
  - 换代恢复只有记录，流与记录可以一起伪造；
  - 按身份删除在 `lstat` 与 `unlink` 之间仍有竞态窗口；
  - 超过 256 MiB 的流不做摘要，这样的运行进不了复审。
