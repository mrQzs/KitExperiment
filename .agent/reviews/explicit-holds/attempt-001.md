# REVIEW：explicit-holds v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`43b0e7c166d63a91829683548bf77ed102ba08eb`（master `f836ac9` + 规划端的三处 oracle 改动：新的 `tests/test_explicit_holds_oracle.py` 9 个用例，`tests/test_resume_holds_oracle.py` 与 `tests/test_request_resume_oracle.py` 改到第 10 节的规则）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 533.9 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。2 个工具错误：一次工具名乱码（`gash`，连续三轮出现 `bash` 被写成乱码）、一次 edit 的旧文本没有匹配，都随即恢复 |
| G2 范围合规 | passed | 只改允许的 2 个文件、新增允许的 1 个测试文件，violations 0，没有工作副本外路径，主仓库未变，没有在 `rm -rf "$T"` 之后再接命令 |
| G3 行为正确 | passed | 第 1–5 条按原文实现；`recover` 留下的绕过路径（M）在合同本身，由规划端补全 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过，全量 785 passed |
| G5 质量复审 | passed（有补全） | 独立复审建议 PASS；M 违背用户决定的本意，规划端补全并改掉把它当成预期的那条执行器测试 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条：新增 `_record_explicit_stop`，`stop()` 每次都追加；`_record_stop_reason` 只给自动原因用，仍去重；`stopped` 仍是第一个原因。
- 第 2 条：`resume` 删去“互不重复”，仍先复制、在副本上逐个检查非空精确 `str`；比对与其余检查不变。
- 第 3 条：冲突条件扩展为 `op.started or op.run is not None`，位置仍在状态检查之后；`recover` 未改。
- 第 4 条：三个旧用例改到新规则，`..._distinct_reasons` 改名，其余用例不动。
- 第 5 条：V07 通过。

## 执行器的解读
- 合同矛盾、未决问题与 limitations 都为空。

## 发现
- **M（中·合同缺口）`recover` 仍是运维把受监督运行判为未启动的一条路。**
  - 规划端决定 P13 写的是“`recover` 不改”，它只拦“当前是 `RUNNING` 或开始过”的操作（`scheduler.py:1054`）。
  - 独立复审复现：交接时记有运行描述、旧控制器 `unknown()` 之后停在 `START_UNKNOWN`；重启之后恢复驱动按 L12 不碰它；`resolve_unknown(NOT_STARTED)` 被拒（`E_RESOLVE_CONFLICT`），但改调一次 `recover(op, "ops", 2, outcome="NOT_STARTED")` 就成功，tokens 归零。已被恢复驱动接管过的操作再换一次代也一样。
  - 这既绕过了用户“受监督的运行一律不许运维判未启动”的决定，也推翻了 L12 要求不推翻的 `START_UNKNOWN` 判断。执行器的测试 `tests/test_explicit_holds.py:421-437` 还把这条路断言为可用——那是合同写的“`recover` 不改”的直接结果，不算执行器的错。
  - 补全：`recover` 对“结论是 `NOT_STARTED`、操作处于 `START_UNKNOWN` 且记有运行描述”同样报 `E_RECOVERY_CONFLICT`。恢复驱动从不对 `START_UNKNOWN` 调 `recover`，它按记录把 `HANDOFF_ACCEPTED` 判为未启动的那条路（两份记录都为空，oracle 钉住）不受影响。
- **L（低·历史数据）** 运行描述是 restart-recovery 之后才有的，在那之前写下的账本里受监督的交接没有描述。规划端确认：仓库里没有提交过任何账本文件，这样的账本只出现在测试的临时目录里。
- **信息（不影响结论）**：
  - I1：显式暂停用了自动原因的文字时，结果与先后有关（先显式后自动只有一项，先自动后显式有两项），但不会丢暂停，恢复时条件本身仍会重新检查；
  - I2：列表随显式暂停增长，运维工具若把 `stop_reasons()` 读出来原样交回去，比对只能保证“读完之后没有新暂停”，这是设计本身的取舍；
  - I3：执行器把另一个名字里写着旧规则的用例（`..._first_seen_order_...`）也改了名，符合本意。
- **独立复审对四个问题的回答**：第 1–5 条逐条成立，冲突检查与前面 8 项检查逐对没有错位；所有经过桥接的交接在 `resolve_unknown` 上都关上了（`op.run` 只在交接时写入，接管、重放之后都还在），唯一的绕过是 `recover`；六千步随机操作里 `stopped` 始终等于列表第一项，持久账本中途重开 30 次完全一致；改写的旧测试保留原意，单靠执行器测试抓住 9 个变异中的全部 9 个。

## 变异检查
- mutations/01（四份 oracle 与执行器测试）：9 个全部抓住。
- mutations/02（只用执行器测试）：同样 9 个全部抓住。

## 结论理由
- 第 1–5 条按原文实现，独立复审建议 PASS。M 是规划端决定 P13 留下的缺口，补全它正是用户决定的本意，不涉及新的语义选择，由规划端补全，不再问用户。判 PASS。

## 后续动作
- `branch` 保留本候选；规划端在 `pi-complete/explicit-holds` 上补全 M：`recover` 的冲突条件补上“`START_UNKNOWN` 且记有运行描述”；改写执行器那条把绕过当成预期的测试；oracle 加一个用例钉住新规则。设计说明加 P16，更正 P13。
- 集成顺序：oracle `43b0e7c`、本候选、规划端补全。
- 人工介入：约 35 分钟（问用户、设计说明、草稿与三份 oracle 的改动、旧测试改写的可行性验证、合同、变异检查、门禁核对、复审记录）。
