# REVIEW：verified-review v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`c4c8083ca2c8de2ff0b48ba3bbb42a8801bed84a`（master `a115764` + 规划端 oracle，16 个真实进程用例，基线上 15 个失败）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1169.4 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。2 个工具错误都是 edit 的旧文本没有匹配，随即重试 |
| G2 范围合规 | passed | 只改两个允许的模块，新增允许的测试文件；violations 0，没有工作副本外路径；临时目录命令都按执行说明的写法，删除之后没有再接命令 |
| G3 行为正确 | passed（有补全） | 逐条满足合同第 1–6 条（见下）；流核对不在保护之内，可能让操作停在 RUNNING（F1）。这是合同遗漏，由规划端补全 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过 |
| G5 质量复审 | passed（有补全） | 独立复审建议 REVISE，范围很窄；规划端补全 F1，并补测检查顺序（F2） |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条：
  - 检查按顺序进行：`_checked_spec`、`_checked_evidence_dir`、`_checked_hook`、`_checked_operation`（读 `scheduler.operation` 并比较 `request_id`）；
  - 恢复路径在已有两项之后、读记录之前做 `_checked_operation`。
- 第 2 条：
  - `_settle` 在 `mark_running` 之后先做 `_stream_reason`：事实缺失时报 START_UNKNOWN；`streams_match`（通过模块级名字调用）为 False 时，两个路径的 `lstat` 身份都等于事实就是 CLEANUP_UNCONFIRMED，否则是 START_UNKNOWN；
  - 核对通过之后依次是 `before_finish(run)`、`candidate_hash`、`finish`，三者抛出时 `unknown(START_UNKNOWN)` 并原样抛出。
- 第 3 条：FINISHED 还要求 `_recorded_streams_match` 为真；`recorded_streams` 抛出 `RuntimeErrorCode` 时算不一致，不求候选哈希。
- 第 4 条：
  - `read_stream(expected=…)` 在 `max_bytes` 检查之后、打开文件之前校验事实的精确形状；
  - 先比较身份与大小，再做上限检查；读完之后比较字节数与摘要；不符时返回 `changed`。
- 第 5 条：
  - 第 7 项对账读的是操作自己的预约；
  - 不再包装 `candidate_hash`，改为传入 `before_finish`；钩子带着事实归约，`evidence_hash` 取监督器的摘要。
- 第 6 条：没有新增导入副作用（V07）。

## 变异检查
- mutations/01（oracle、执行器测试，以及桥接与 adapter 的已有测试）：24 个，抓住 22，漏掉 2，都是等价写法：
  - R15“`lstat` 的 `ValueError` 冒出”：证据目录已经由监督器核验为已存在的绝对目录，APFS 的文件名必然是合法 UTF-8，这条路径不可达；
  - A09“证据哈希取自己读到的”：只有状态为 ok 时才记用量，这时读到的字节的摘要必然等于监督器的摘要。
- mutations/02（只用执行器测试）：抓住 10，漏掉 14，漏掉的都由 oracle 覆盖。
- R14“`recorded_streams` 的错误冒出”被抓住：执行器用 `mock.patch.object(bridge, "recorded_streams", …)` 单独隔离了这条。从真实磁盘上无法做到让 `replay_decision` 成功而 `recorded_streams` 抛错，因为两者共用同一次读取。

## 发现
独立复审（只读，内存探针）建议 REVISE，范围很窄：除下面的 F1 之外，第 1–6 条都满足。它也确认了三处等价：
- `recorded_streams` 抛错，会被前面 `_decision_of` 的失败遮蔽；
- `lstat` 抛出 `ValueError` 不可达；
- “证据哈希取自己读到的”与取监督器的摘要必然相同。

- **F1（中·合同遗漏）流核对期间的中断会让操作停在 RUNNING。**
  - `_settle` 先 `mark_running`（`supervised_operation.py:344`），再做流核对（`:350-353`），核对在 `:354-363` 的 `try/except BaseException` 之外。
  - 独立复审用内存探针让 `streams_match` 抛出 `KeyboardInterrupt`：scheduler 只收到 `mark_running`，中断冒出，操作停在 RUNNING；资源一直占着，该 task 报 `E_ACTIVE_TASK`，request 也不会停止，只有换代之后的 recover 能处理。
  - 窗口不小：核对要把两个流各重读一遍，每个最多 256 MiB；子进程把输出截成 256 MiB 的稀疏文件，就能把窗口拉到大约一秒。
  - 这违背了模块的规则“操作永远不会停在 RUNNING”，以及“中断照样按记录落定”的文档承诺。但合同第 2 条只列出了钩子、`candidate_hash` 与 `finish` 抛出时的处理，并没有要求把流核对也放进保护：候选是照合同字面实现的，责任在规划端。
  - 处理：规划端补全。流核对单独包进 `try`，出现 `BaseException` 时调用 `unknown(reason="START_UNKNOWN")` 并原样抛出；带原因的那次 `unknown` 留在 `try` 之外，避免调用两次。补测 `test_an_interrupt_while_the_streams_are_checked_is_start_unknown`。
- **F2（低·测试缺口）交接之前各项检查的先后，只在单个故障下测试过。** 规划端补测 `test_the_checks_before_the_handoff_keep_their_order`，用两个故障同时出现的输入分辨顺序：
  - 钩子不可调用，且操作不存在；
  - 证据目录不是 str，且钩子不可调用；
  - 证据目录不是 str，且规格属于另一个 request；恢复路径同样测这一组。
- 补全的规则各用一个变异验证：把核对放回保护之外，或者调换三处检查的先后，都被抓住；全量 622 passed。
- **信息**：执行中提到一次合同的可能歧义（事件流第 2411 行：`recorded_streams` 还可能抛出别的异常）。执行器按合同只把 `RuntimeErrorCode` 当作不一致，并在 limitations 里写明“从真实磁盘上做不到 `replay_decision` 成功而 `recorded_streams` 抛错”。这一取舍与合同一致。
- **信息（设计已接受）**：
  - 桥接核对之后、钩子读取之前，流仍可能被改。这时钩子读到 `changed`，不记用量，运行照常进入 REVIEW_PENDING，request 因用量未知而停止；oracle 把这当作预期行为。
  - 用量永远不会取自被改过的字节。
  - 恢复路径分两次读记录，只算相关性证据。

## 结论理由
- 候选逐条满足合同。F1 是规划端没有写进合同的要求，修正只有几行。按测试设计“合同未列明的细分在首次验收时记为规划补充”的规则，以及 adapter-supervised-run 的先例，由规划端补全，不派回执行器。
- 独立复审的建议是 REVISE。没有采纳，理由如上：执行器照合同实现，缺口在合同。

## 后续动作
- 集成到 master：依次 cherry-pick oracle `c4c8083`、本候选的分支提交，以及规划端补全（F1、F2）。
- 合同模板的教训：在可能停在中间状态的步骤之后新增代码时，合同要写明它也受同一保护。记入 Code-Kit 的实机反馈。
- 人工介入：约 50 分钟（草稿与 oracle、合同、复审、变异检查、补全）。
