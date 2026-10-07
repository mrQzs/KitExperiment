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
- 不给流设字节上限：监督器照旧不限制子进程的输出，摘要的耗时与输出大小成正比。
- 不改 `reconcile` 与五键观察记录；不承诺断电之后记录仍在（与 posix-runtime-runner 第 6 节相同）。
- Linux 与 Windows Job Object 仍是 NOT_RUN；不调用真实 provider CLI，不读取凭据，不联网。

## 6. 规划端的决定（2026-10-07）

- E1：事实取自钉住的描述符，不取自路径。子进程在运行中改名或删除 `stdout.log`，事实仍描述它真正写入的那个文件。
- E2：事实在进程组回收之后计算，以当时 `fstat` 的大小为界，增长中的文件不会让摘要读不完。
- E3：记录位置选外层 `streams`，不进 `observation`：`reconcile` 的输入合同与已有用例都不变；旧的 v1 记录被新读取方拒绝，按损坏的证据处理（恢复时为 START_UNKNOWN，保守）。
- E4：摘要出错时事实为 `None`，不抛错。进程已经运行过，这时抛出 `RuntimeErrorCode` 会被桥接当成“启动前被拒”，把真实运行释放成 NOT_STARTED。
- E5：摘要期间的 `KeyboardInterrupt` 不吸收，照样冒出，钉住的描述符照样关闭；桥接随后按记录判为 START_UNKNOWN。进程组此时已经回收，没有必须做完的清理。
- E6：活路径上核对不一致时，原因用 `CLEANUP_UNCONFIRMED`：流在回收之后还在变，说明组外还有写入者活着。
- E7：stderr 与 stdout 一起钉住、一起核对：它同样能暴露组外的写入者。
- E8：`device` 与 `inode` 记为 JSON 整数。超过 2**53-1 的 inode 写不进规范 JSON，观察记录写入失败，桥接按 START_UNKNOWN 处理；APFS 的 inode 远小于这个值。

以上决定都可以在后续合同里修改；改动时更新本节。

## 7. 进度

- 第 1 份 `supervisor-stream-facts`：规划中。
