# REVIEW：serve-watch v1 · attempt-001

Verdict: PASS

<!-- review-draft 生成（2026-10-16T09:21:58Z，调度工具 2.10.0）：事实取自 .agent/runs/serve-watch/attempt-001；标着“待填”的地方是复审判断，全部填完 verdict 才收。
     Verdict 行只能写一个结论，并与 verdict 命令的 --verdict 一致。 -->

## 基本信息
- 基线：`1816f1c5f85338f4417d1eeaf2434e53d1970537`
- 候选树：`c7af567799e99c29595580aa9502880ab658fcce`（补丁往返校验通过；2 files changed, 609 insertions(+), 143 deletions(-)）
- 要求的执行模型：step/step-5-preview · thinking high
- 实际模型：step/step-5-preview；providerThinkingLevel：未回报
- 运行：退出码 0 · 用时 2375.641 s / 上限 2400 s · 正常结束 · 工具错误 6
- token：输入 236711 · 输出 110121 · 缓存读 5321472 · 缓存写 0；费用 unknown（unknown）
- 复审者：Claude Code 规划/验收会话（claude-opus-5-5）；独立复审：同会话派出的只读子代理（general-purpose，同一模型，未换厂商），未运行测试，用纯内存探针在基线与候选上对照了 F1

## 门禁

| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | 工具：通过 |
| G1 执行完整 | passed | 工具：通过；提供商未回报思考级别：只能确认请求的级别（high） |
| G2 范围合规 | passed | 工具：通过 |
| G3 行为正确 | passed | fresh 8/8；oracle 14/14、执行器 13/13；候选变异 oracle + 执行器 16/18（“遍末不补读”预期漏掉；“不加 --watch 也走第二遍”由 serve-parallel 的 oracle 把守，那份 oracle 在 V03、不在这次变异的命令里） |
| G4 回归检查 | passed | 工具：通过；检查记录：checks/01（fresh，8/8 通过）；检查在干净副本（基线 + 候选补丁 + setup）上运行 |
| G5 质量复审 | passed（有发现） | 独立复审建议 PASS：F1（低）不加 `--watch` 时第一次读时钟落到吸收中断的 try 之外，与基线差一个字节码级的窗口；F6（信息）执行器的 `_sleep` 用例替换了全局 `time.sleep`/`time.monotonic`，全量里有偶发风险；均由规划端补全 |
| G6 可集成 | passed | 工具：通过 |

## DoD 映射

| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V01 passed、V02 passed、V07 passed | 命令行、分派、常量与辅助函数（第 1–3 条）；目的：不加 `--watch` 时一切照旧，`--poll-seconds` 单独出现时什么都不跑 |
| D02 | passed | V01 passed、V02 passed | 遍的循环——补读、`needs_operator`、`finished`、停顿、跨遍的计数（第 4–6 条）；目的：操作员从另一个进程下的裁决会被下一遍看见，被拒绝的任务自动再领，环境坏掉的任务不会每 30 秒烧掉一次尝试，没有能推进的任务时命令自己结束 |
| D03 | passed | V01 passed | 报告与退出码（第 7 条）；目的：一行报告说清走了几遍、跑了什么、为什么结束 |
| D04 | passed（补全 F1 后） | V03 passed、V04 passed、V08 passed、复审 | serve 的另外两种用法与其它命令不受影响，`run_cli.py` 的 diff 只在合同指定的位置，文档串按第 8 条，导入无副作用 |
| D05 | passed | V05 passed、V06 passed、scope（越界 0） | Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更 |

## 范围与质量
- 越界与警告：越界 0、警告 0
- 测试可信度：执行器测试 13 例，覆盖 Step 4 的四组（`_last_operation`、`_watch_finished` 的六种情形加没有作业的任务、`_sleep`、不加 `--watch` 的 `--poll-seconds`）；在基线上全部失败；没有 skip、没有弱化。只用执行器测试时抓住 5/18（其余是 oracle 与 V07 把守的规则）。
- 变异检查：
  - mutations/01：18 条，命令 all、executor
    - all：抓住 16，漏掉 2（意外 1：M17），跳过 0，出错 0
    - executor：抓住 5，漏掉 13（意外 2：M04 M11），跳过 0，出错 0
    - `M04 W04 NOT_STARTED 不算 needs_operator`（executor 意外漏掉） → 归因：输入观察不到（执行器测试不读常量；oracle 与 V07 把守） → 处理：注明，无需补测试
    - `M11 W12 缺省停顿 60 秒`（executor 意外漏掉） → 归因：输入观察不到（执行器测试不读缺省值；oracle 的报告用例与 V07 把守） → 处理：注明，无需补测试
    - `M17 W21 不加 --watch 也走第二遍`（all 意外漏掉） → 归因：命令选择（不加 `--watch` 的并发走查由 serve-parallel 的 oracle 把守，那份 oracle 在 V03、不在这次变异的命令里） → 处理：注明，无需补测试
    - 预期内的漏掉（expect_missed：合同交给其他命令覆盖的规则）：M01 M02 M03 M08 M09 M10 M13 M15 M16 M18 → 处理：注明
- 兼容性与残余风险：不加 `--watch`、`--parallel` 的旧 serve 一字未动；`--parallel` 不加 `--watch` 时只走一遍、不补读、报告不多键。发现：
  - F1【低】`started = _clock()` 移到了遍循环之前，不加 `--watch` 的 `--parallel` 在第一次读时钟时收到 Ctrl-C 会冒出而不是报告 `interrupted`（基线吸收）；文档串那句“与不加 `--parallel` 时相同”随之不准。补全移回走查的 try，并补一个替身时钟的用例。
  - F2【信息，接受】遍与遍之间几条语句上的中断不吸收，属于 Non-goals 接受的残留窗口。
  - F3【信息】外来控制器留下的未结束操作会让 watch 一直等，直到操作员 Ctrl-C 或 `recover` 换代（换代后补读 `E_LEDGER_STALE`，以 `refused` 结束）——合同第 3 条的字面，设计如此。
  - F4【信息】`_last_operation` 每遍 O(任务数 × 操作数)，当前规模可忽略；以后可每遍构造一次映射。
  - F5【信息】`--poll-seconds` 没有上限，几百位的数会在第一次停顿时溢出。
  - F6【信息】执行器 `_sleep` 用例替换全局 `time.sleep`/`time.monotonic`，补全改为只替换 `run_cli.time`。
  - F7【信息】设计说明第 6 节说“合同二在命令开始时把代读一次”，合同没写，候选与基线一样每次读；因为换代会让实例失效，两者等价，补全时改设计说明的说法。
- 执行器自述与证据是否一致：一致。四条自定解读都在合同字面之内；其中“开跑前拒绝时 `passes` 为 0”合同没写，规划端接受。事件流里的两条解读线索一条是测试组织方式、一条就是报告的第 4 条。用时 2375 秒，离 2400 秒上限只差 25 秒：合同的时间分配偏紧（35914 行事件流、6 次工具错误），回流到下一份合同的预算。决策模型（Jev，shadow）给“自行解读合同”0.92，与报告的 4 条解读相符。
  - 执行报告：json_ok（executor_status completed）
  - 合同矛盾：无
  - 自定解读（4，原文）：
    - {"quote": "第 3 条 _sleep：以 time.monotonic() 计；Step 4：替换 time.sleep 记录每次等待", "adopted": "_sleep 执行器测试同时替换 time.sleep 与 time.monotonic，用假时钟按每次睡眠量前进", "reason": "_sleep 用 time.monotonic 判定是否等满，若只替换 time.sleep 而不推进时钟会无限循环；合同第 3 条要求以 monotonic 计，故必须让假时钟随睡眠前进才能终止并核对每次 ≤_POLL_SECONDS、合计不少于要求秒数。"}
    - {"quote": "第 3 条/Step 4：合计不少于要求的秒数", "adopted": "断言 sum(waits) >= seconds - 1e-9", "reason": "十个 0.1 秒在二进制浮点下求和可能略小于 1.0；用 1e-9 容差是对“不少于”的忠实读法。"}
    - {"quote": "第 5 条：遍数从 1 开始计", "adopted": "passes 在进循环前初始化为 0；--watch 下若走查前检查（如 E_RUN_JOBS）拒绝则报告 passes:0", "reason": "合同说“遍数从 1 开始计”指真正运行的遍；走查前拒绝时一遍都没跑，oracle 无用例触及此路径，0 是如实计数。"}
    - {"quote": "第 5 条：这一段里的 ContractError/LedgerError 是主线程的拒绝", "adopted": "遍间 try 的 except 写成 (ContractError, LedgerError, RunSpecError)，与走查自身拒绝路径一致", "reason": "RunSpecError 只在走查前的 load 检查产生，遍间不可能抛出，纳入属防御且与走查 except 签名一致，行为与合同字面（ContractError/LedgerError）等价。"}
  - 限制与取舍：无
  - 未决问题：无
  - 事件流里的解读线索（逐条核对是否都在上面的自定解读、合同矛盾或未决问题里）：
    - 第 24918 行（thinking）：on" tasks flips the result. The contract says "第 3 条的五种情形各一个" — one each of the five situations. I interpret this as: five focused test cases, each exercising one branch. So separate test methods (or subTests) each with a minimal ledger. Let me
    - 第 34772 行（thinking）：s is consistent. Since RunSpecError can't occur there, it's behaviorally identical to the contract. I'll note it as an interpretation for transparency. Actually, let me reconsider — is there any risk that catching RunSpecError in the between-pass section masks
- 摘要里的“复审需要关注”（原文）：
  - 新增被 Git 忽略的路径：`.pytest_cache/`
  - 新增被 Git 忽略的路径：`sentinelmesh/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/fixtures/pi_native/__pycache__/`
  - 测试或检查命令的退出码被管道掩盖（1 条，例如事件流第 21045 行 `…LedgerError, RunSpecError) as exc:\|finally:\|while True:\|if not watch:\|report\[\|def _serve_parallel\|def _constant" sentinelmesh/run_cli.py | sed -n '1,40p'`；另有 8 条探索命令也如此）：执行器没有看到被检查程序的退出码，报告里的 reported_exit_code 是推断值，以 check 的结果为准（2.10.0 起执行说明给了每条检查的自检照抄写法）

## 结论理由
合同第 1–8 条逐条满足，fresh 8/8，oracle 与执行器测试抓住 16/18 个变异（漏掉的两条：一条是 oracle 观察不到的遍末补读，一条由不在变异命令里的 serve-parallel oracle 把守），范围合规，旧 serve 与 `--parallel` 的单遍行为不变（除 F1 的读时钟窗口）。F1 与 F6 都是几行的局部修正，由规划端补全并补用例。结论 PASS。

## 后续动作
- 需要修正的具体差异（REVISE）：无（F1、F6、F7 由规划端补全）
- 待决的业务或环境问题（BLOCKED / ESCALATE）：无
- 已授权且可以继续的集成动作：local_branch、merge：`bash scripts/dispatch-pi.sh integrate .agent/runs/serve-watch/attempt-001`（当前末端上验证后落地）
- 人工介入：0（用户没有介入）
