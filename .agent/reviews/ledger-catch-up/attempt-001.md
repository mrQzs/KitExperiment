# REVIEW：ledger-catch-up v1 · attempt-001

Verdict: ACCEPTED_WITH_EXCEPTION

<!-- review-draft 生成（2026-10-16T07:32:27Z，调度工具 2.10.0）：事实取自 .agent/runs/ledger-catch-up/attempt-001；标着“待填”的地方是复审判断，全部填完 verdict 才收。
     Verdict 行只能写一个结论，并与 verdict 命令的 --verdict 一致。 -->

## 基本信息
- 基线：`4996e106fed511bc16d6d9635995309e2b54a70f`
- 候选树：`dbfcb649629dfb06bde4e74b533eff84fdb59242`（补丁往返校验通过；2 files changed, 370 insertions(+), 46 deletions(-)）
- 要求的执行模型：step/step-5-preview · thinking high
- 实际模型：step/step-5-preview；providerThinkingLevel：未回报
- 运行：退出码 0 · 用时 1198.391 s / 上限 2400 s · 正常结束 · 工具错误 1
- token：输入 138967 · 输出 25873 · 缓存读 1782784 · 缓存写 0；费用 unknown（unknown）
- 复审者：Claude Code 规划/验收会话（claude-opus-5-5）；独立复审：同会话派出的只读子代理（general-purpose，同一模型，未换厂商），未运行测试，用两个小探针（替换连接对象、settrace）确认了 M1、M2

## 门禁

| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | 工具：通过 |
| G1 执行完整 | passed | 工具：通过；提供商未回报思考级别：只能确认请求的级别（high） |
| G2 范围合规 | passed | 工具：通过 |
| G3 行为正确 | passed（补全后） | fresh 两次：checks/01 的 V04、checks/02 的 V05 各失败一次，失败的都是 serve-parallel 的同一个 oracle 用例（第一次中断收尾），原因是既有缺陷：`_await_run` 用 `Thread.join(0.1)`，在 Python 3.12 上 join 被中断会把运行中的线程标成已结束。候选没有动 `run_cli.py`（V09 通过）；缺陷已在 master `c41d557` 修好并加了确定性用例，`integrate` 会在带修复的末端上重跑全部检查。补读本身：oracle 14/14、执行器 9/9、变异 20/20 |
| G4 回归检查 | failed | 工具：不通过；检查记录：checks/02（fresh，8/9 通过）；必要检查 V05：failed；检查在干净副本（基线 + 候选补丁 + setup）上运行 |
| G5 质量复审 | passed（有发现） | 独立复审建议 REVISE：M1、M2（中）是写入路径上几处中断后实例没有失效的窗口，L1 文档串措辞，L2 执行器测试比清单多 4 个（无害），I1 `checkpoint_seq` 的文档串；均由规划端补全修正并补 4 个进程内用例，不另派一轮 |
| G6 可集成 | passed | 工具：通过 |

## DoD 映射

| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V01 passed、V02 passed、V03 passed | 补读的规则——衔接、链、换代、已用操作 id、序号与检查点（第 1 条）；目的：操作员从另一个进程下的停止、裁决、恢复，会被一个正在运行的控制器看见，而不是把它弄坏 |
| D02 | passed（补全 M1、M2 后） | V01 passed、V02 passed、V03 passed | 写入的顺序与失败规则——写锁、补读、内存调用、追加、提交，谁让实例失效、谁不（第 2 条）；目的：两个进程的写入串行化、一条不丢，调度器的拒绝不毁掉实例，半更新的内存永远不会被写进日志 |
| D03 | passed | V01 passed、V02 passed、V08 passed | `refresh()` 与 `create_ledger` 的调度器（第 3–4 条）；目的：需要最新状态的调用方能只读地补上，新建的账本也能补读别的实例的领取 |
| D04 | passed（V05 的失败属于既有缺陷，见 G3） | V04 passed、V05 failed、V09 passed、复审 | 其它命令与模块不受影响，`compact` 与打开不变，文档串按第 6 条，导入无副作用 |
| D05 | passed | V06 passed、V07 passed、scope（越界 0） | Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更 |

## 范围与质量
- 越界与警告：越界 0、警告 0
- 测试可信度：执行器测试 9 例，覆盖 Step 5 的 5 项并多写了 4 项（其中一项与 oracle 的公开方法用例几乎相同，复审 L2，无害）；错误码全等断言，拒绝用例先验证合法基线；基线上全部失败；没有 skip、没有弱化，oracle 与改写过的 4 个旧测试未动。变异：oracle + 执行器 20/20；只用执行器 13/20，比预期多抓 2 条（M10、M14）。
- 变异检查：
  - mutations/01：20 条，命令 all、executor
    - all：抓住 20，漏掉 0（意外 0），跳过 0，出错 0
    - executor：抓住 13，漏掉 7（意外 0），跳过 0，出错 0；标了预期漏掉却被抓住：M10 M14
    - 预期内的漏掉（expect_missed：合同交给其他命令覆盖的规则）：M04 M09 M11 M15 M16 M17 M18 → 处理：注明
- 兼容性与残余风险：只改了 `durable_ledger.py`，`compact`、打开与只读视图不变。发现：
  - M1【中】提交之后推进 `seq`/`sha` 的两行在 try 之外：异常落在提交与这两行之间时，实例仍可用、头落后自己的日志一条，下一次补读会把自己的命令再重放一次，之后写进日志的命令让账本再也打不开（复审用 settrace 探针复现）。CPython 3.12 的真实信号打不进这个窗口，但根因在合同第 2 条（f）把推进写在了（e）的失败规则之外。
  - M2【中】`BEGIN IMMEDIATE` 只接 `sqlite3.Error`：忙等时按 Ctrl-C，实例可用且持有写锁，挡住操作员的写入；几处“先回滚再失效”在回滚被第二次中断时跳过失效；拒绝分支的回滚失败会留下打开的写事务。
  - L1 `refresh` 文档串说“连检查点序号都不写”，而内存里的检查点序号会动；I1 `checkpoint_seq` 属性的文档串在补读之后不再精确。
  - 既有缺陷（不属于本候选）：serve-parallel 的 `_await_run` 用 `Thread.join`，见 G3；已修。
  - 并发：从补读到提交持有 RESERVED 锁，不丢写、不死锁；`refresh` 遇到 5 秒忙等超时会是 `E_LEDGER_REPLAY` 并失效，下一份合同（serve-watch）要把它当作“重新打开”。NOT_RUN：网络文件系统与多主机。
- 执行器自述与证据是否一致：基本一致。四条自定解读都在合同字面之内，事件流里的解读线索都在报告里；报告说“只覆盖 Step 5 的 5 项”而实际写了 9 个用例，自述不准确（L2）。中断落在写锁处怎么办，合同没写，执行器也没报告——即 M2(a)。决策模型（Jev，shadow）的报告筛查给“自行解读合同”0.95，与报告里 4 条解读相符。
  - 执行报告：json_ok（executor_status completed）
  - 合同矛盾：无
  - 自定解读（4，原文）：
    - {"quote": "第 1 条“按序号读出序号大于本实例 seq 的全部命令行。没有这样的行，或者第一行的 previous 不等于本实例的 sha：E_LEDGER_STALE。用 _verify_chain 核对链，链断了是它自己的 E_LEDGER_CHAIN”", "adopted": "先显式比对首行 previous 再交给 _verify_chain", "reason": "接不上头被单列为 E_LEDGER_STALE，而 _verify_chain 自己会报 E_LEDGER_CHAIN，必须先分流"}
    - {"quote": "第 1 条“在重放任何一条之前，先看这些命令里有没有 method 为 rollover 的”", "adopted": "用与重放相同的 _strict_loads 解析以取 method；解析失败即 E_LEDGER_REPLAY", "reason": "合同未规定该扫描步骤的错误码，与这些命令进入重放时的报错保持一致"}
    - {"quote": "第 3 条“不写任何东西”与第 1 条“日志里有检查点时，把 checkpoint_seq 设为检查点的序号”", "adopted": "不写日志/数据库（读事务回滚），内存 checkpoint_seq 仍按第 1 条更新", "reason": "两条规则对写入与 refresh 共用，checkpoint_seq 是内存视图而非日志内容"}
    - {"quote": "Step 5“换代不是第一条新命令时（先 seed_task 再 rollover）”", "adopted": "用另一个进程执行 rollover，使待补读命令为 [seed_task, rollover]", "reason": "同时覆盖“只查第一条是否换代”这一变异面"}
  - 限制与取舍（2，原文）：
    - 补读只在本机 SQLite 上被测试证明；网络文件系统与多主机多写者仍为 NOT_RUN
    - 纯 Python 吸收中断的残留窗口按合同保留为失效行为，未尝试恢复半更新的内存
  - 未决问题：无
  - 事件流里的解读线索（逐条核对是否都在上面的自定解读、合同矛盾或未决问题里）：
    - 第 8218 行（text）：读里报 `E_LEDGER_REPLAY`，与它进入重放时一致。 3. 合同第 3 条说 `refresh()` “不写任何东西”，同时第 1 条要求补读成功后更新 `checkpoint_seq`；我把“不写”理解为不写日志/数据库（读事务随后回滚），内存里的 `checkpoint_seq` 仍按第 1 条更新，理由：第 1 条对两种调用共用，且 `checkpoint_seq` 是内存视图而非日志内容。 4. 换代用例我选了“先 `seed_task` 再 `rollover`”（合同
- 摘要里的“复审需要关注”（原文）：
  - 新增被 Git 忽略的路径：`.pytest_cache/`
  - 新增被 Git 忽略的路径：`sentinelmesh/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/fixtures/pi_native/__pycache__/`

## 结论理由
补读的规则（衔接、链、换代扫描、已用操作 id、`refresh`、`create_ledger`）全部符合合同，oracle 14/14、变异 20/20，范围合规。两次 fresh 检查各有一处失败，都是 serve-parallel 里已经在 master 修好的既有缺陷（`Thread.join` 被中断时把运行中的线程标成已结束），与本候选无关，所以结论是带例外的接受，例外由 `integrate` 在带修复的末端上重跑全部检查来核销。独立复审找到的 M1、M2 是写入路径上的中断窗口，根因在合同第 2 条的字面顺序；都是几行的局部修正，由规划端补全并补 4 个进程内用例（替换连接对象在写锁、回滚之后抛中断，settrace 在提交与新头之间抛中断）。

## 后续动作
- 需要修正的具体差异（REVISE）：无（M1、M2、L1、I1 由规划端补全）
- 待决的业务或环境问题（BLOCKED / ESCALATE）：无
- 已授权且可以继续的集成动作：local_branch、merge：`bash scripts/dispatch-pi.sh integrate .agent/runs/ledger-catch-up/attempt-001`（当前末端上验证后落地）
- 人工介入：0（用户没有介入）
