# REVIEW：supervisor-stream-facts v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`626063340b5bc99ce422ff3d5529f7f09207c42f`（master `02314c4` + 规划端 oracle：16 个真实进程用例，另改了三个已有测试）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.1）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1257.5 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。2 个工具错误都是 edit 调用缺参数，随即重试 |
| G2 范围合规 | passed | 只改两个允许的模块，新增一个允许的测试文件，violations 0。`/dev/fd` 是描述符计数，只读。12 条临时目录命令都是执行说明给的写法，`rm -rf "$T"` 在最后。运行期间主工作区 HEAD 前进，是规划端提交了第二份合同的计划 |
| G3 行为正确 | failed | 第 3 条“从不抛出”不成立（R2）；第 1 条“只删除本次创建的文件”在竞态下不成立（R3） |
| G4 回归检查 | passed | checks/01（fresh）：V01–V08 全部通过 |
| G5 质量复审 | failed | 摘要的开销由 `st_size` 决定而不是由写出的字节数决定，内存是它的两倍（R1）；四条规则没有测试把守（R4） |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条钉住：
  - `_open_pinned` 按路径用 `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` 打开，再对钉子与写描述符各 `fstat` 一次，要求普通文件且 `(st_dev, st_ino)` 相同；
  - `_open_evidence_files` 依次创建并钉住两个文件，任何失败都关闭本次打开的全部描述符，再按路径删除 `created` 里的文件（路径已被替换时的问题见 R3）；
  - 钉住发生在 `before_start` 之前；`supervise` 用一个 `try/finally` 覆盖钩子、exec 与运行的全部出口来关闭钉子。
- 第 2 条事实：
  - 在 `_reap_group` 之后计算，`fstat` 之后用 `os.pread` 从偏移 0 读到当时的大小为止；
  - `OSError` 让该流为 None，中断照样冒出；
  - `SupervisedRun.streams` 是末尾带默认值的字段，`start_failed` 为 None。
- 第 3 条 `streams_match`：类型精确（bool 不算 int，dict 子类不算），先比 `st_dev/st_ino/st_size` 再有界读取，任何 `OSError` 都算不一致，描述符在 finally 中关闭；`ValueError` 没有被拦住（R2）。
- 第 4、5 条记录：schema v2，外层四键闭合；`_validated_streams` 按内层观察的键集判断是否已启动；`replay_decision` 与 `recorded_streams` 共用 `_records_for`，判定逻辑不变。
- 第 6 条导入：只新增 `hashlib`、`stat` 两个标准库导入（V08）。

## DoD 映射
| DoD | 结果 | 证据 |
|---|---|---|
| D01 | failed | 竞态下回滚会删掉别人的文件（R3） |
| D02 | passed | V01、V02、V03；事实的顺序与边界没有测试把守（R4），开销问题见 R1 |
| D03 | failed | 两类 str 参数会抛出（R2） |
| D04 | passed | V01、V03、V04 |
| D05 | passed | V01 |
| D06 | passed | V04、V05、V08 |
| D07 | passed | V02；执行器测试覆盖 Step 4 的五项，另加两项 |
| D08 | passed | V06、V07；scope.json |

## 变异检查
- mutations/01（oracle、执行器测试与记录、监督器的已有测试）：37 个，抓住 33，漏掉 4：
  - M05“回收之前计算事实”：这一条是规划端写错的变异，它多算了一次事实，却没有替换回收之后的那次计算，所以不改变行为。按正确写法同样没有测试能抓住（R4）；
  - M10“摘要读到 EOF”、M24“字节数取 fstat 的大小”：只有文件在摘要期间增长或缩短才看得出来，没有测试把守（R4）；
  - M14“核对时不比大小”：等价写法，之后的长度比较照样拒绝。
- mutations/02（只用执行器测试）：抓住 5，漏掉 32。漏掉的都由 oracle 覆盖；合同 Step 4 只要求执行器补 oracle 之外的用例。

## 发现
独立复审（只读，内存探针，不写文件）建议 REVISE；以下发现与规划端的复核合并。

- **R1（中·规划缺口与质量）摘要的开销由 `st_size` 决定，而且整段留在内存里。**
  - `_fact_of`（`posix_supervisor.py:438-471`）按 `st_size` 循环读取，把每块追加进列表，最后 `b"".join` 再求哈希；`_stream_matches`（`:507-546`）经 `_read_at_most` 先把至多 `bytes + 1` 字节读进内存。
  - 子进程拿到的是不带 `O_APPEND` 的写描述符。一次 `os.ftruncate(1, N)` 就能把 `st_size` 变成 N，文件是稀疏的，子进程什么都没写。之后监督器要读满 N 字节，内存峰值约 2N。N 取到 2^40 这一级，`supervise` 就永远返回不了；否则以 `MemoryError` 退出（不是 `OSError`，会从 `supervise` 冒出，桥接判为 START_UNKNOWN）。
  - 独立复审的内存模拟：`fstat` 报 256 MiB、`pread` 返回零时，`_fact_of` 做了 4096 次 `pread`，峰值 512 MiB。
  - 这违背了监督器“在有界时间内报告”的保证。合同第 2 条只要求以 `fstat` 的大小为界，设计第 5 节决定不给流设上限，所以这是规划缺口；拼接再哈希是候选自己的写法。
  - 处理：v2 合同新增决定 E10：摘要上限 `MAX_STREAM_DIGEST_BYTES = 256 MiB`，超过时事实为 None，什么都不读；两处都逐块更新摘要，内存不随流增长；`streams_match` 遇到 `bytes` 超过上限的事实直接返回 False。
- **R2（中/低·违约）`streams_match` 对两类 str 参数抛出异常。**
  - `_stream_matches` 的 `os.open`（`:522`）只捕获 `OSError`。独立复审的探针：
    - `streams_match("/nonexistent\x00x", 合法事实)` 抛出 `ValueError: embedded null character`；
    - `streams_match("/nonexistent/\ud800", 合法事实)` 抛出 `UnicodeEncodeError`。
  - 合同第 3 条要求“对任何参数值或文件状态都不抛出”。`supervise`、`replay_decision`、`recorded_streams` 遇到这样的目录会先报 `E_RUNTIME_EVIDENCE`，所以只有直接调用 `streams_match` 的调用方会碰到。下一份合同的桥接会直接调用它。
  - 处理：v2 要求 `ValueError` 同样算不一致，oracle 补这两个参数值。
- **R3（低·违约，窄竞态）回滚按路径删除。**
  - 身份不符时（`:198-230`），回滚 `for path in created: _remove_evidence(path)`（`:269-270`）删掉的是路径上现在的文件，也就是替换上来的那个；本次创建、已被改名走的文件反而留下。合同第 1 条要求“删除本次调用创建的证据文件”。
  - 触发需要一个同账户的并发进程，在 `O_EXCL` 创建与钉住之间动手；那时还没有子进程。
  - 处理：v2 要求删除之前用 `os.lstat` 核对路径上仍是创建时的那个文件（写描述符的 `st_dev`、`st_ino`），钩子失败时的删除同样核对。
- **R4（低·测试缺口）四条规则没有测试把守。**
  - M10（摘要读到 EOF）与 M24（字节数取 `fstat` 的大小）：只有文件在摘要期间增长或缩短才看得出来；
  - “事实在回收之后取”：mutations/01 的 M05 是规划端写错的变异（多算一次，没有替换回收之后的计算），按正确写法也没有测试能抓住；
  - “钉子与写描述符比较身份”：oracle 只把只读打开换成诱饵文件，分不出比较对象是写描述符还是路径；改成与 `os.stat(路径)` 比较也能通过全部测试。
  - 处理：规划端补测 `tests/test_supervisor_stream_facts_gaps.py`，见 v2 合同。
- **信息**：
  - `os.open` 返回到描述符被记下之间、钉住完成到外层 `try` 之间，中断仍可能漏掉一个描述符或留下一个文件。这与模块文档已经承认的中断窗口同类；
  - 独立复审确认：没有一条路径会让已经启动的进程在桥接看来是“未启动”；记录 v2 的闭合校验、`replay_decision` 判定不变、`recorded_streams` 的顺序都符合合同。

## 结论理由
- R2、R3 违反合同第 3 条与第 1 条，按 scheduler-exact-inputs a001 的先例判 REVISE。
- R1 让监督器失去有界返回的保证，需要新的规划决定（E10），在 v2 合同里一并交给返工；R4 的测试由规划端放进 v2 的基线。

## 后续动作
- `branch --rework` 保留本候选，作为 v2 基线的一部分；v2 合同只剩一次尝试（max_attempts_total 2）。
- v2 合同：R1 的上限与逐块摘要、R2 的 `ValueError`、R3 的身份核对删除；规划端补测 R4。
- 人工介入：约 80 分钟（草稿与 oracle、合同、复审、两轮变异检查、补测草稿）。
