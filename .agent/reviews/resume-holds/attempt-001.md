# REVIEW：resume-holds v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`5e28d9515e25d8d9be7d8edbc413556f9add494a`（master `8cd6860` + 规划端 oracle：新的 `tests/test_resume_holds_oracle.py` 9 个用例；`tests/test_request_resume_oracle.py` 改用 `expected_reasons`）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 799.4 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。4 个工具错误：一次工具名乱码（`ash`）、两次 edit 参数或旧文本不对、一条按设计返回 1 的检索命令，都随即恢复 |
| G2 范围合规 | passed | 只改允许的 3 个文件、新增允许的 1 个测试文件，violations 0，没有工作副本外路径，主仓库未变；这一次没有在 `rm -rf "$T"` 之后再接命令 |
| G3 行为正确 | passed | 第 1–6 条按原文实现；“不为空”先于复制检查（L2），只在调用方并发修改时影响错误码，由规划端补全 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过，全量 762 passed |
| G5 质量复审 | passed（有补全与遗留） | 独立复审建议 PASS；合同覆盖面的两处缺口（M、L1）记为遗留，交用户决定是否再出合同 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条：`_record_stop_reason` 按第一次出现的顺序记、每个原因一次；`stop`、`rollover`、`unknown`、`_enforce` 四处都调用它，请求已经停止时也调用；`stopped` 仍是第一个原因；`stop_reasons` 先查精确类型再查请求，返回元组。
- 第 2 条：`expected_reasons` 先查是精确 `list` 且不空，再复制，元素在副本上逐个检查（非空精确 `str`、互不重复）；与 `stop_reasons` 按顺序完全相等；载荷 `previous_reasons`；`stopped` 与原因列表一起清空。
- 第 3 条：`mark_running` 置 `started`；`resolve_unknown` 在状态检查之后拒绝开始过的操作判为 `NOT_STARTED`（`E_RESOLVE_CONFLICT`）；`recover` 的冲突条件扩展为 `RUNNING` 或开始过。
- 第 4 条：持久账本的 `stop_reasons` 与 `operation()` 一样先查可用。
- 第 5 条：旧测试改用新参数；钉住旧规则的用例改名为 `test_the_resume_names_every_stop_reason_recorded`，断言只点名一个原因或顺序不对都报 `E_STOP_REASON_CHANGED`，点名全部原因才往下检查。执行器另外找到第三处因叠加原因要点名全部原因的用例（停止之后又超额），按同一规则处理，写在 limitations 里。
- 第 6 条：V07 通过。

## 执行器的解读
- 合同矛盾与未决问题为空。limitations：多出的第三处旧用例按同一规则处理（采纳）；旧版 `resume` 命令的账本失败关闭、未做兼容（P12，采纳）。

## 发现
- **M（中·合同覆盖面）“开始过”登记得太晚，运行中途崩溃的用量仍能被当成 0。**
  - `started` 只在 `mark_running` 里置真（`scheduler.py:806`），而桥接要等运行结束、两份记录写完才调用它。
  - 控制器在运行中途死掉（最常见的崩溃）时，账本只看到 `HANDOFF_ACCEPTED`；重启恢复把它判为 `START_UNKNOWN` 之后，运维判 `NOT_STARTED` 照样通过，`recover(NOT_STARTED)` 也一样，tokens 归零。
  - 用户决定写的是“账本见过运行的操作”，合同与候选都照此实现；缺口在决定的覆盖面。
  - 两种补法：让桥接在进程启动时就登记开始（要改 `supervised_operation.py`，并影响重启恢复的 oracle）；或者对交给过受监督运行（记有运行描述）的操作一律不许运维判未启动——能证明未启动的情况，桥接与重启恢复已经按记录自动判定了。
- **L1（低–中·合同覆盖面）原因文字相同的两次暂停合并成一项。** 按 P8 同一个原因只记一次：两次 `stop("req-1", "OPERATOR_STOP")` 只留下一项（`request.stopped` 事件有两条），一次 `resume(["OPERATOR_STOP"])` 就把两次暂停一起清掉。`stop()` 的默认原因就是 `OPERATOR_STOP`，两位运维各自用默认值暂停时会撞上。
- **L2（低）“不为空”先于复制检查。** `scheduler.py:1210-1212` 先在调用方的列表上判断不为空，再复制；用 `settrace` 在复制前清空列表，报 `E_STOP_REASON_CHANGED` 而不是 `E_STOP_REASON`。空列表不可能等于一个已停止请求的原因列表，所以绝不会错误恢复，只是错误码不同。规划端补全：先复制，再在副本上检查。
- **L3（低·测试）** 执行器测试漏掉“先查冲突再查状态”（H20），由 oracle 守住；`tests/test_request_resume.py:42` 的 `DEFAULT_REASONS` 是共享的可变列表，被当作参数默认值。规划端补全：默认值改为每次新建。
- **信息（P12，按设计接受）**：两个方向都以 `E_LEDGER_REPLAY` 失败关闭，消息清楚（本轮之前的代码读新日志、新代码读含旧 `resume` 的日志）；不含 `resume` 的旧日志照常打开，原因列表由旧的停止命令重建；事件键从 `previous_reason` 改为 `previous_reasons`，仓库里没有别的代码读它。规划端确认：仓库与 master 上没有提交过任何账本文件，记过旧 `resume` 的账本只出现在测试的临时目录里。
- **独立复审对五个问题的回答**：
  - 第 1–6 条逐条成立（L2 一处细节除外）；`resume` 其余检查的 14 对组合没有错位；
  - M1 在合同定义下已关上：一万步随机操作里 `stopped` 始终等于列表第一项、列表无重复；持久账本中途重开 36 次，原因列表、`stopped`、事件、操作与 `started` 每次都完全一致；恢复两者都清；
  - L1 在 `resolve_unknown` 与 `recover` 里都关上了，接管之后、重开之后照样拒绝；`release_proven_unstarted` 与 `cancel_unstarted` 都走不通；残留见 M；
  - 改写后的 request-resume 测试仍是 25 个用例，断言从 94 条增至 98 条，钉住旧规则的那条改为断言新规则；
  - 所有修改都在锁下，只读方法返回元组副本。

## 变异检查
- mutations/01（三份 oracle 与执行器测试）：23 个全部抓住。
- mutations/02（只用执行器测试）：抓住 22 个，漏掉“先查冲突再查状态”（H20），由 oracle 的顺序用例守住。

## 结论理由
- 第 1–6 条按原文实现，独立复审建议 PASS。L2、L3 由规划端补全。M 与 L1 是用户决定在覆盖面上留下的缺口，记为遗留，交用户决定是否再出合同。

## 后续动作
- `branch` 保留本候选；规划端在 `pi-complete/resume-holds` 上补 L2（先复制再检查不为空）与 L3（测试默认值每次新建）。
- 集成顺序：oracle `5e28d95`、本候选、规划端补全。
- M、L1 写进设计说明的遗留，并问用户是否处理。
- 人工介入：约 40 分钟（问用户、设计说明、草稿与两份 oracle、改写旧测试的可行性验证、合同、变异检查、门禁核对、复审记录）。
