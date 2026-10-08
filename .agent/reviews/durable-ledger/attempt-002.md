# REVIEW：durable-ledger v2 · attempt-002

Verdict: REVISE

## 基本信息
- 基线：`2ee04c06c73c48bbaab6fe2e4ca8f97eb3ec15ed`（v1 oracle `bb93f04` + a001 候选 `9063615` + 规划端测试 `tests/test_durable_ledger_gaps.py`，10 个用例）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 829.2 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审（中途因限流与一次认证错误中断，续跑完成）

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。1 个工具错误是模型调用了不存在的工具 `ash_value`（工具名乱码），随即改用 edit 重试 |
| G2 范围合规 | passed | 只改允许的两个文件，violations 0；没有工作副本外路径、可疑命令或临时路径；主仓库未变 |
| G3 行为正确 | failed | v2-2 的连接方式按字面消去 `..`，打开的可能不是检查与创建的那个文件（F1，根源在合同措辞）；v2-2“连接照样关闭”“只抛出 LedgerError”“解析的任何失败”与 v2-3“建文件之后的任何失败”各有一处不成立（F2–F5，低） |
| G4 回归检查 | passed | checks/01（fresh）：V01–V08 全部通过 |
| G5 质量复审 | failed | 独立复审建议 REVISE：F1 是相对 a001 的回归，影响数据完整性 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对（v2 的修改）
- v2-1（内存调用的异常）：`_apply` 先绑定参数（签名不对的 `TypeError` 在调用之前冒出，实例照常可用）；内存调用抛出 `ContractError` 原样冒出、实例可用；任何其他异常（`BaseException`）先 `_invalidate()` 再原样冒出。编码与追加阶段的失败仍让实例失效。
- v2-2（打开）：
  - `_connect` 用 `Path(os.path.abspath(path)).as_uri() + "?mode=rw"`，绝不新建文件；路径可以含空格（执行器测试 `SpacesInThePath`）；
  - 连接之后的每一步（pragma、读、链校验、重放 Scheduler、重放）都在关闭连接的保护之内；
  - generation 先查规范十进制、不超过 16 位，再交给 Scheduler，`ContractError` 映射为 `E_LEDGER_FORMAT`；
  - `_strict_loads` 把 `RecursionError` 映射为 `E_LEDGER_REPLAY`，`_replay` 把重放调用的任何 `Exception` 映射为 `E_LEDGER_REPLAY`；
  - 记录的操作 id 必须是 `op-` 加 32 位小写十六进制；`_check_claim` 只允许同一 `(request, key)` 的幂等重放复用同一个 id。
- v2-3（创建）：`os.open(O_WRONLY|O_CREAT|O_EXCL, 0o600)` 原子建文件，`fstat` 记下 `(st_dev, st_ino)`；之后任何失败都关闭连接，只在 `lstat` 显示的身份相同时删除，报 `E_LEDGER_OPEN`；不是 `Exception` 的异常原样冒出。
- v2-4：命令编码（不排序键）、哈希链、过期检测、失效状态与只读调用都没有改动。

## 执行器的解读
- v1 第 4 条“表已存在报 `E_LEDGER_EXISTS`”与 v2-3“建文件之后的任何失败报 `E_LEDGER_OPEN`”：执行器按 v2 为准。采纳：有了 `O_EXCL`，另一个 `create_ledger` 在建文件那一步就拿到 `E_LEDGER_EXISTS`；`open_ledger` 不建表；“别的创建者抢先建表”在正常路径上不可达。
- `os.fstat` 失败时没有身份可核对，不删除文件，只报 `E_LEDGER_OPEN`：采纳，与“只在身份相符时删除”一致。

## 发现
- **F1（中·合同缺口，相对 a001 的回归）连接用的路径与检查、创建、删除用的路径可能不是同一个文件。**
  - `_connect`（`durable_ledger.py:121`）用 `Path(os.path.abspath(path)).as_uri()`。`abspath` 按字面消去 `..`；而 `os.lstat`、`os.open(O_EXCL)` 与 `_remove_created` 都由内核解析，遇到符号链接会先跟随它，再处理 `..`。
  - 规划端复现：`T/a/link -> T/b/sub`，路径 `T/a/link/../ledger.db`。内核解析到 `T/b/ledger.db`，`abspath` 得到 `T/a/ledger.db`。`T/a/ledger.db` 是别的程序的 SQLite 库时，`create_ledger` 成功，那个库里多了 `meta` 与 `commands` 两张表；`O_EXCL` 新建的 `T/b/ledger.db` 留下 0 字节的孤儿。独立复审另外复现了：外来空文件被接管；两处各有账本时，`open_ledger` 的 `lstat` 看的是一个，重放与写入的却是另一个。
  - 这让 `O_EXCL`“不接管别人放在路径上的文件”的保证落空。a001 直接 `sqlite3.connect(path)`，由 SQLite 逐段解析，没有这个问题。
  - 根源在合同：v2-2 写的是“路径要先转成绝对路径，再用 `as_uri()` 编码”，没有排除按字面规范化；规划端自己的草稿也是 `abspath`。执行器的写法是这句话最自然的读法。
  - 补全：改用 `Path(path).absolute()`，它不消去 `..`，SQLite 会逐段解析符号链接，与内核一致（独立复审用 `PRAGMA database_list` 验证过）。
- **F2（低·违约）pragma 期间的中断不关闭连接。** `_connect` 的 pragma 段只捕获 `LedgerError` 与 `sqlite3.Error`（`:128-139`），`KeyboardInterrupt`、`MemoryError` 从这里冒出时连接不关；`open_ledger` 调用 `_connect` 又在关闭连接的保护之外。v2-2 写明“`KeyboardInterrupt` 之类……照样冒出，连接照样关闭”。独立复审用包装连接复现：`/dev/fd` 多 1，create 删掉文件之后连接仍挂在已删除的 inode 上。
- **F3（低·违约，a001 已有）含 `os.fsencode` 编不了的代理字符的路径冒出 `UnicodeEncodeError`。** `"\ud800"` 这类 str 通过 `_check_path`，随后 `os.lstat` 抛出 `UnicodeEncodeError`（`ValueError`），违反“`open_ledger` 只抛出 `LedgerError`”。规划端草稿同样漏了。
- **F4（低·违约）解析命令时的 `MemoryError` 原样冒出。** `_strict_loads` 在 `_replay` 的保护之外，只映射 `RecursionError` 与 `ValueError`；同一个 `MemoryError` 出在重放调用里却报 `E_LEDGER_REPLAY`。v2-2 写的是“解析命令时的任何失败”。
- **F5（低·违约，几乎不可达）`os.close` 失败时不清理。** `fstat` 之后 `os.close(handle)` 在 `finally` 里、又在清理的保护之外：它抛出 `OSError` 时原样冒出，文件留下。v2-3 要求“新建文件之后的任何失败”都按身份删除并报 `E_LEDGER_OPEN`。
- **信息（不影响结论）**：
  - I1：`fstat` 失败时不删除文件，同意执行器的处理；
  - I2：进程在 `O_EXCL` 与 `COMMIT` 之间被杀，会留下 0 字节文件，之后 create 报 `E_LEDGER_EXISTS`、open 报 `E_LEDGER_FORMAT`，需要人工清理，写进设计说明；
  - I3：`_remove_created` 的 `lstat` 到 `unlink` 之间，以及 `os.open` 返回到进入保护之间，各有一个固有窗口；
  - I4：create 把 `_connect` 的 `LedgerError` 再包一层，消息成了 `E_LEDGER_OPEN: E_LEDGER_OPEN: …`；
  - I5：create 清理时的 `db.close()` 没有像 open 那样防护；
  - I6：执行器测试 `OpenPragmaFailure` 最后留下一个没关闭的实例。
- **独立复审对四个问题的回答**：R1–R7 都已修好；合同矛盾的解读正确；`_check_claim` 的顺序成立：覆盖只发生在 open 局部的重放 Scheduler 里，随即被拒绝并丢弃，没有部分重放的状态可见；新问题即 F1–F5。它对 16 个变更方法伪造了 16,000 次参数，只得到成功或 `ContractError`，证实 V08 只能靠注入来守。

## 变异检查
- mutations/01（oracle、规划端测试与执行器测试）：36 个，抓住 34，漏掉 2：
  - M29（V08：重放只接 `ContractError` 与 `TypeError`）：规划端探测了 16 个变更方法的各种怪值，包括孤立代理字符，调度器一律报 `ContractError`（编码失败是 `E_JSON_OUTPUT`）。所以用真实输入走不到，对现在的调度器是等价变异；但这条兜底规则防的是以后的调度器，规划端用注入异常的测试把它钉住；
  - M33（V12：清理从不删除）：测试缺口。规划端测试“失败的创建只删自己的文件”换掉了文件，执行器的 `CreateRefusals` 根本没建出文件，没有用例在建文件之后失败、又不换文件。
  - 规划端写了三处变异，以免被重复的检查遮蔽：
    - 去掉 `O_EXCL`（`lstat` 预检单独去掉是等价的）；
    - 改共用的 `_CANONICAL_INTEGER` 正则（两处规范检查各自单独去掉会互相遮蔽）；
    - 按新的缩进重写“打开时写入”。
- mutations/02（只用执行器测试）：抓住 15，漏掉 21，都由 oracle 与规划端测试覆盖；执行器测试独自守住了 pragma 失败时关闭连接（V13）。
- 规划端补测（未提交时在候选副本上验证）：注入 `KeyError`、`RuntimeError`、`AttributeError` 的重放与五个失败点的创建，各自抓住 V08、V12（以及 V04、V13、V14），全量 665 passed。

## 结论理由
- F1 让创建接管别人的文件、让打开读写另一个账本，影响数据完整性，并且是本轮引入的回归；F2–F5 是对 v2 字面规则的低严重度偏离。判 REVISE。
- F1 的根源是合同措辞与规划端草稿，所以分类为 PLAN_ERROR；F2–F5 属于执行器，严重度低，单独不足以返工。
- 合同的两次尝试已经用完，由规划端补全。

## 后续动作
- `branch --rework` 保留本候选；规划端在 `pi-complete/durable-ledger` 上补全：
  - F1：连接改用 `Path(path).absolute().as_uri()`；
  - F2：pragma 段任何异常都先关闭连接；
  - F3：`_check_path` 拒绝 `os.fsencode` 编不了的路径（`E_LEDGER_PATH`）；
  - F4：解析的任何 `Exception` 都报 `E_LEDGER_REPLAY`；
  - F5：`os.close` 失败时按身份删除并报 `E_LEDGER_OPEN`；I4、I5、I6 一并收尾；
  - 规划端测试补上 V08、V12，以及 F1–F5 的用例。
- 集成顺序：oracle `bb93f04`、a001 `9063615`、规划端测试 `2ee04c0`、本候选、规划端补全；a001 与 a002 标记 SUPERSEDED。
- 设计说明更新 L5（内存调用的非 `ContractError` 异常让实例失效）并写入 I2。
- 人工介入：约 75 分钟（变异规格与两轮变异、调度器探测、补测与验证、独立复审的三次续跑、复现 F1、复审记录）。
