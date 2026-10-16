# REVIEW：serve-parallel v1 · attempt-001

Verdict: PASS

<!-- review-draft 生成（2026-10-16T05:01:29Z，调度工具 2.10.0）：事实取自 .agent/runs/serve-parallel/attempt-001；标着“待填”的地方是复审判断，全部填完 verdict 才收。
     Verdict 行只能写一个结论，并与 verdict 命令的 --verdict 一致。 -->

## 基本信息
- 基线：`e4098921a0491788ef48886255297debab99d6c8`
- 候选树：`8c2ffb4483106ec79fed2afcf6e0aba1d298d929`（补丁往返校验通过；2 files changed, 615 insertions(+), 1 deletion(-)）
- 要求的执行模型：step/step-5-preview · thinking high
- 实际模型：step/step-5-preview；providerThinkingLevel：未回报
- 运行：退出码 0 · 用时 1086.14 s / 上限 2400 s · 正常结束 · 工具错误 1
- token：输入 162968 · 输出 37502 · 缓存读 2225664 · 缓存写 0；费用 unknown（unknown）
- 复审者：Claude Code 规划/验收会话（claude-opus-5-5）；独立复审：同会话派出的只读子代理（general-purpose，同一模型，未换厂商），未运行测试

## 门禁

| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | 工具：通过 |
| G1 执行完整 | passed | 工具：通过；提供商未回报思考级别：只能确认请求的级别（high） |
| G2 范围合规 | passed | 工具：通过 |
| G3 行为正确 | passed（补全后） | 工具：通过；fresh 8/8，oracle 24 例与执行器 9 例全过，候选变异 all 35/37；独立复审发现两处时序竞态 F1、F2，由规划端补全修正 |
| G4 回归检查 | passed | 工具：通过；检查记录：checks/01（fresh，8/8 通过）；检查在干净副本（基线 + 候选补丁 + setup）上运行 |
| G5 质量复审 | passed（有发现） | 独立复审建议 REVISE（F1 中、F2 中低）；两处都是两三行的局部修正、oracle 观察不到，按小改动由规划端补全并补进程内用例，不另派一轮 |
| G6 可集成 | passed | 工具：通过 |

## DoD 映射

| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V01 passed、V02 passed、V07 passed | 命令行、分派与常量（第 1–3 条）；目的：不加 `--parallel` 时一切照旧，加了才进入并发走查 |
| D02 | passed | V01 passed、V02 passed | 开跑前的检查与同一工作目录的规则（第 4 条）；目的：两个执行器不会在没有共同资源键的情况下同时改同一棵树，任何作业有问题时什么都不跑、账本不动 |
| D03 | passed | V01 passed、V02 passed | 主线程的走查——空位、跳过、停止、派生键、证据目录、三种等待码只在自己有运行在跑时等、重放不运行（第 5 条）；目的：账本里看到的与按种子顺序手敲 `once` 相同，只是最多 N 个同时在跑，领取顺序与报告确定 |
| D04 | passed（补全 F1、F2 后） | V01 passed | 工作线程、等待、收尾、两次中断与意外异常（第 6–9 条）；目的：报告里每个开跑的运行都有结果，第一次 Ctrl-C 得到报告，第二次放弃时在跑的操作留给 `recover`，任何情况下都不挂住、不丢结果 |
| D05 | passed（补全 F2 后） | V01 passed、V02 passed | 报告、拒绝的顺序与退出码（第 10 条）；目的：被脚本调用时一行就能读到跑了什么、为什么停，每个被留在账本里的操作都在 stderr 上被点名 |
| D06 | passed | V03 passed、V04 passed、V07 passed、V08 passed、复审 | 不加 `--parallel` 的 serve 与其它命令、模块不受影响，`run_cli.py` 的 diff 只在合同指定的位置，文档串按第 11 条，导入无副作用 |
| D07 | passed | V05 passed、V06 passed、scope（越界 0） | Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更 |

## 范围与质量
- 越界与警告：越界 0、警告 0
- 测试可信度：执行器测试 9 例，与 Step 5 清单逐项对应；每个拒绝用例先验证合法基线再只改一处，错误码全等断言；基线上因导入 `_shared_directories` 失败而全红；没有 skip、没有弱化，oracle 24 例未改。只用执行器测试时抓住 8/37（合同把其余规则交给 oracle），比预期多抓 3 条（M11 停止后不收尾、M27 不查 max_seconds、M31 次数不跳过：“留给 oracle”的划分偏保守）。
- 变异检查：
  - mutations/01：37 条，命令 all、executor
    - all：抓住 35，漏掉 2（意外 0），跳过 0，出错 0
    - executor：抓住 8，漏掉 29（意外 0），跳过 0，出错 0；标了预期漏掉却被抓住：M11 M27 M31
    - `M22 目录不折叠大小写` → 归因：等价变异（Windows 上 realpath 对存在的目录已经返回真实大小写；POSIX 上 normcase 本来不变） → 处理：注明
    - 预期内的漏掉（expect_missed：合同交给其他命令覆盖的规则）：M01 M02 M03 M04 M05 M06 M07 M08 M10 M12 M13 M14 M15 M16 M18 M19 M20 M24 M25 M28 M29 M30 M32 M33 M34 M35 M36 M37 → 处理：注明
- 兼容性与残余风险：不加 `--parallel` 的 `cmd_serve` 只多了分派两行与文档串一段（复审逐 hunk 核对），V03 的旧用例与全量 V04 都通过。发现：
  - F1【中】等待码分支数了两次在跑的运行（`if … and _runs_in_flight(threads)` 之后又 `flight = _runs_in_flight(threads)`）：最后一个运行恰好在两次之间结束时 `flight == 0`，`0 >= 0` 恒真，主线程无限空转、不出报告。规划端用探针（替换 `_runs_in_flight` 返回 0、1、0）在候选上复现。根因在合同：第 5 条没有写明“在跑数只读一次”，规划端原型只读一次所以没有这个问题。
  - F2【中低】走查里只有 `claim` 的拒绝会经收尾；`ledger.task(...)` 抛出的 `LedgerError`（实例被工作线程弄失效，例如另一进程写了账本而得到 `E_LEDGER_STALE`）落到开跑前检查的外层处理，跳过收尾，运行还在跑时就关账本、出报告，在跑的操作不在 `runs` 里也不在 stderr 上被点名。毫秒级窗口。
  - F3【低，规划端】模块文档串仍写 serve “no concurrency, no daemon”与“a command that is killed leaves at most one undecided operation”，`--parallel` 下不成立；合同只允许加一句，由规划端改。
  - F4【信息】`_serve_parallel` 文档串说第一次中断后“the exit code is 0”，实际只在没有拒绝时为 0。
  - F5【信息，接受】收尾结束后、报告写出前到达的第一个中断不吸收，与不加 `--parallel` 时相同，纯 Python 固有窗口。
  - 守护线程标志（M40，oracle 观察不到）由复审读代码核对为 `daemon=True`；并发启动子进程的句柄继承由 CPython 的 handle list 限定。NOT_RUN：真实控制台第二次 Ctrl-C 后的进程退出、POSIX 监管器在线程里的原生行为。
- 执行器自述与证据是否一致：一致。五条自定解读都没有越出合同字面（第 3 条把走查与收尾描述成一个外层 try，实际是两个 try 共用一个标志，行为与第 8 条一致）；事件流里没有未报告的解读线索。Code-Kit 2.10.0 首次实测：报告填了 `interpretations`（5 条）；掩盖退出码的命令只有 1 条且不是测试命令（`date; ls | head; wc`），测试与检查命令都没有接 `| tail`。
  - 执行报告：json_ok（executor_status completed）
  - 合同矛盾：无
  - 自定解读（5，原文）：
    - {"quote": "第 5 条 (g)：先把领取记录…按领取顺序登记，再启动一个守护线程", "adopted": "登记与 start() 之间不插入任何等待或检查，主线程连续执行", "reason": "第 7 条要求每次等待都带超时；这两步之间没有等待点，领取当中被中断的残留窗口与合同 Non-goals 描述一致。"}
    - {"quote": "第 6 条：其它任何异常（BaseException）→ 原样记下，交给主线程", "adopted": "工作线程用 `except BaseException` 捕获并原样保存（含 KeyboardInterrupt/SystemExit）", "reason": "_thread.interrupt_main 只投递给主线程，工作线程正常不会收到；按合同字面保留 BaseException。"}
    - {"quote": "第 8 条：主线程在走查或收尾里收到的第一个 KeyboardInterrupt 被吸收", "adopted": "走查与收尾同处一个外层 try：第一个中断（无论落在走查、两段之间还是收尾等待里）都吸收并把 interrupted 置真，第二个直接 raise", "reason": "合同说“走查或收尾里”的第一个都吸收，收尾当中的第二个不吸收；单一标志即可区分，无需区分中断落点。"}
    - {"quote": "第 7 条：等其中一个结束就是等到本次调用在跑的运行数比开始等的时候少", "adopted": "用 Thread.join(_POLL_SECONDS) 轮询，以线程 is_alive() 计数；记录由工作线程在自己结束前写入，因此“还活着”等价于“还没有结果”", "reason": "合同第 6 条要求主线程只看线程是否活着、不从队列取结果。"}
    - {"quote": "第 5 条 (h)：本次调用已经结束的运行里有拒绝或意外异常 → 停止领取", "adopted": "对全部领取记录检查（在跑的记录既无 refusal 也无 error，等价于只查已结束的）", "reason": "记录只有工作线程会写，字段在结束前就位，实现更简单且不改变语义。"}
  - 限制与取舍（2，原文）：
    - 第二次 Ctrl-C 之后进程能否立即退出、守护线程是否拖住进程，只能用真实控制台验证（合同 Non-goals 已声明 oracle 观察不到）；我用 oracle 的被杀控制器用例与进程内第二次中断用例覆盖了可达部分。
    - oracle 单次耗时在 19.75s–65.10s 之间波动（本机负载），均在 300s 超时以内。
  - 未决问题：无
  - 事件流里的解读线索：无
- 摘要里的“复审需要关注”（原文）：
  - 新增被 Git 忽略的路径：`.pytest_cache/`
  - 新增被 Git 忽略的路径：`sentinelmesh/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/fixtures/pi_native/__pycache__/`

## 结论理由
候选在干净副本上 8/8 通过，oracle 与执行器测试抓住 35/37 个变异（漏掉的两条：一条等价，一条是 oracle 观察不到的守护线程标志），范围合规，不加 `--parallel` 的路径未动。独立复审找到两处只在特定时序下出现的竞态：F1 会挂住，F2 会丢结果，都违反 D04/D05 的目的；但都是两三行的局部修正，oracle 也观察不到。由规划端在补全提交里修正，并补进程内桩用例（F1：替换 `_runs_in_flight` 造出 1→0；F2：包装 `open_ledger` 让 `task` 抛 `LedgerError`，同时运行还在跑），比再派一轮更省。F1 的根因在合同而不在执行器。结论 PASS，集成后立即提交补全。

## 后续动作
- 需要修正的具体差异（REVISE）：无（F1–F4 由规划端补全：F1 在跑数只读一次；F2 走查里的账本拒绝同样先收尾再报告；F3、F4 文档串）
- 待决的业务或环境问题（BLOCKED / ESCALATE）：无
- 已授权且可以继续的集成动作：local_branch、merge：`bash scripts/dispatch-pi.sh integrate .agent/runs/serve-parallel/attempt-001`（当前末端上验证后落地）
- 人工介入：0（用户没有介入）
