# REVIEW：durable-ledger v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`bb93f04d2fec66f8f71097a153f43499072db2fa`（master `35426a2` + 规划端 oracle，16 个用例，部分启动真实子进程）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 840.9 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 REVISE

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。1 个工具错误是 edit 的旧文本没有匹配 |
| G2 范围合规 | passed | 只改允许的文件，`scheduler.py` 只提出了 `_new_operation_id`；violations 0 |
| G3 行为正确 | failed | 第 5 条“失败时关闭连接”“绝不新建文件”不成立（R2、R4）；第 5 条“任何不符都报 E_LEDGER_REPLAY”对部分输入不成立（R3）；第 4 条“不改动已有文件”在竞态下不成立（R5） |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过 |
| G5 质量复审 | failed | 内存调用期间的中断会写出无法重放的日志（R1，合同缺口） |
| G6 可集成 | passed | 补丁往返校验通过 |

## 执行器的解读
- 设计说明的 L5 写着“排序键”，第 4 节写着“幂等重放的 claim 不记”，都与合同矛盾。执行器以合同为准，这是对的：两处都是规划端写错了，设计说明已经更正。
- `seq` 在实例不可用时同样报 `E_LEDGER_UNAVAILABLE`：采纳。
- `meta` 的 generation 为 `"0"` 时冒出 `ContractError`：执行器把它列入 unresolved_questions。被篡改的账本应当报账本自己的错误码，见 R3。

## 发现
- **R1（中高·合同缺口）内存调用期间的中断让实例继续可用，此后的日志无法重放。**
  - 内存调用（`durable_ledger.py:353`）在负责失效处理的 `try` 之外。
  - 独立复审的探针：在 claim 已写入操作、幂等键与资源占用之后，让 `_emit` 抛出 `KeyboardInterrupt`。实例仍然可用，内存里有这个操作，日志里没有；接着对它 `handoff` 成功并被记入日志；重放时 handoff 找不到操作，报 `E_LEDGER_REPLAY`，账本从此打不开。
  - 合同第 6 条第 2 步写的是“它的异常原样冒出，什么都不记录，实例照常可用”，第 5 步的失效只针对第 3–4 步。候选照字面实现，缺口在合同。
  - v2：内存调用抛出 `ContractError` 之外的任何异常（包括 `KeyboardInterrupt`），实例都失效。
- **R2（中·违约）`open_ledger` 失败时没有关闭连接。** 哈希链校验、代际构造与重放都在关闭连接的保护之外（`:486-489`）。每次打开损坏的账本都漏一个描述符：规划端的测试里，三次失败的打开留下了三个描述符。
- **R3（中·违约，部分属于合同缺口）打开时会冒出非 `LedgerError` 的异常。**
  - 嵌套很深的命令冒出 `RecursionError`：`_strict_loads` 只捕获了 `ValueError`。
  - 5000 位的规范十进制 generation 能通过正则，`int()` 却抛出 `ValueError`。
  - generation 为 `"0"` 或 2^53 时，冒出 Scheduler 的 `ContractError`。
  - 重放的调用抛出 `ContractError`、`TypeError` 之外的异常时原样冒出。
  - v2：generation 不超过 16 位并且被 Scheduler 接受，否则报 `E_LEDGER_FORMAT`；解析与重放的任何 `Exception` 都报 `E_LEDGER_REPLAY`。
- **R4（低中·违约）`open_ledger` 可能新建文件。** 路径在 `lstat` 与 `sqlite3.connect` 之间消失时，connect 会新建一个空文件，违反“绝不新建文件”。v2 用 `mode=rw` 的 URI 连接。
- **R5（中）创建失败时的清理会删掉另一个创建者的账本。**
  - `_discard` 按“文件为 0 字节”来判断是不是自己的文件，而 `lstat` 检查与 connect 之间不是原子的。
  - 独立复审复现：A 做完 `lstat` 之后，B 连上同一路径并持有写锁；A 等锁超时，看到 0 字节的文件就删掉了；B 随后提交，整个账本写进了一个已被删除的 inode。
  - 同一个竞态也会让 A 在别人刚放上的空文件上建表。
  - v2：用 `O_EXCL` 原子地创建文件并记下身份；连接用 `mode=rw`；失败时只在身份相符时删除。
- **R6（低·测试缺口）哈希链的前后链接没有测试守住。** mutations/01 的 M05（只按每行自己的 `previous` 核对摘要）漏掉了，它并不等价：删掉一条、把后面的重新编号之后，每一行本身仍然自洽，M05 会接受，悄悄丢掉一条命令也能打开。
- **R7（低）构造出来的日志可以让两次 claim 复用同一个操作 id**，得到真实 Scheduler 达不到的状态。v2：重放时拒绝已存在的 id，以及不是 `op-` 加 32 位小写十六进制的 id。
- **信息（残留）**：
  - 参数是在内存调用之后才从调用方传入的对象上编码的：调用方如果在调用期间并发修改自己传入的映射或列表，记下的命令就可能与实际执行的不同。这算调用方的责任，写进残留。
  - 实例打开期间，路径上的文件被删掉重建时，实例会继续往已删除的 inode 里追加，过期检测看不到。超出本合同范围。

## 变异检查
- mutations/01（oracle 与执行器测试）：21 个，抓住 20，漏掉 1：M05，见 R6。
- mutations/02（只用执行器测试）：抓住 12，漏掉 9，都由 oracle 覆盖。执行器测试还守住了“只核对日志头的序号”（D03）与写失败的通用分支（D23），这两条在规划端草稿上原本是等价写法。

## 结论理由
- R2–R5 违反合同第 4、5 条；R1 让实例写出无法重放的日志，需要先修订合同。合同还剩一次尝试，判 REVISE，并按 v2 合同返工。

## 后续动作
- `branch --rework` 保留本候选，作为 v2 基线的一部分。
- v2 合同写明 R1、R3、R4、R5、R7 的规则。规划端测试 `tests/test_durable_ledger_gaps.py` 放进 v2 基线，覆盖 R1–R7。
- 人工介入：约 70 分钟（设计说明、草稿与 oracle、合同、复审、变异检查、补测草稿）。
