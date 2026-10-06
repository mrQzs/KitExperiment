# REVIEW：adapter-supervised-run v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`fd66ce31a024356fb9272e617d8e382f73211ee9`（master `173c1a0` + 规划端 oracle，16 个真实进程用例）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.1）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 907.399 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 PASS

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。3 个工具错误都无害：一次 grep 退出码、一次探索时预期的异常、一次 edit 未匹配 |
| G2 范围合规 | passed | 只新增两个允许的文件，violations 0；没有工作副本外路径。10 条临时目录命令都是执行说明给的写法，`rm -rf "$T"` 在最后。新增的 `skipIf(os.name != "posix")` 与 oracle 相同 |
| G3 行为正确 | passed | 逐条满足合同第 1–5 条（代码复审与独立复审一致） |
| G4 回归检查 | passed | checks/01（fresh）：V01 oracle 16、V02 16、V03 65、V04 全量 534 passed，V05–V07 通过 |
| G5 质量复审 | passed | 执行器测试把守了时机规则；测试缺口与稳健性问题由规划端补全（F5、F6） |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 |
|---|---|---|
| D01 | passed | V01、V02；独立复审 39 项流规则探针 |
| D02 | passed | V01、V02 |
| D03 | passed | V01、V02；执行器测试断言：调用方的 `candidate_hash` 被调用时，操作是 RUNNING，用量已记入，`run.finished_for_review` 还没有发生 |
| D04 | passed | V03、V04、V07 |
| D05 | passed | V02；缺口见 F5、F6 |
| D06 | passed | V05、V06；scope.json |

## 发现
- **F1（中·跨模块，不在本合同范围）用 `setsid()` 干净逃逸的后代，会让监督器判定清理已确认。**
  - 机理：`_reap_group` 只看进程组。独立复审用真实进程复现：
    - 子进程 fork 出孙进程后退出并被回收，孙进程 `setsid()` 后继续持有 stdout；
    - 结果是 REVIEW_PENDING，用量照常记入；
    - 孙进程之后还能追加内容，记录的 `evidence_hash` 与最终的 `stdout.log` 不同。
  - 硬链接或改名换上来的普通文件也会被当作流读取：合同只要求 `O_NOFOLLOW`。
  - 结论：同账户的子进程能伪造自己的流，记录的用量是厂商的声明，不是独立证明。
  - 处理：写进设计说明第 3 节的信任边界与 IMPLEMENTATION_STATUS；以后另立监督器合同，例如在观察记录里写入 stdout 的身份与摘要，或做进程组之外的逃逸检测。
- **F2（中/低·跨模块）非 str 的 `evidence_dir` 让操作停在 HANDOFF_ACCEPTED。**
  - 监督器在创建任何文件之前就拒绝了它，但桥接的释放路径 `evidence_digest(evidence_dir)` 冒出 `TypeError`，操作停在活跃状态，资源一直占着。
  - 直接调用 `run_supervised_operation` 也会触发；本合同第 4 条的前置检查不含 evidence_dir，所以不是本候选的违约。
  - 处理：规划端补全。桥接在交接之前拒绝非 str 的 `evidence_dir`，报 `E_RUNTIME_EVIDENCE`；`recover_supervised_operation` 同样处理。
- **F3（低·合同缺口）`usage_dimensions` 没有与预约维度对账。**
  - 列入未预约的维度时，`settle` 报 `E_OBSERVATION_SCOPE`，一项都不记；包装随之抛出，操作置为 START_UNKNOWN。
  - 这类配置错误要等进程跑完才暴露，成败还取决于厂商输出。
  - 处理：记为残留，以后的合同在交接之前核对。
- **F4（低·固有限制）`settle` 的全有或全无对异步中断不成立。**
  - 独立复审在逐项记录之间注入 `KeyboardInterrupt`：结果是 START_UNKNOWN，只记了一部分。
  - 操作与 request 都已停在 START_UNKNOWN，半记录的用量不会被当成已知运行放行。这与监督器文档承认的中断窗口属于同一类。
- **F5（低·测试缺口）交接前各项检查的先后没有多故障用例。**
  - 变异 M17（入口的调用对象检查改用 `isinstance`）因此两边都没抓住。合同写明了检查顺序，所以它不是等价写法。
  - 处理：规划端补测。
- **F6（低·测试稳健性）执行器测试有两处稳健性问题。**
  - 超时用例的进程清理注册在断言之后，前面的断言失败时睡眠进程会留下；
  - FIFO 用例在 `O_NONBLOCK` 退化时会一直阻塞，而不是失败。

  另外：空白行的子用例只断言了行号，没有断言 code；三个拒绝用例没有先跑合法基线。

  处理：规划端修正前两处。
- **信息**：
  - 子类 adapter 抛出非 `AdapterError` 时，异常在落定之后冒出，调用方拿不到结果（合同只规定了 `AdapterError` 的处理）；
  - 调用方 `candidate_hash` 返回非法值时，操作置为 START_UNKNOWN，用量保留为最终值。

## 变异检查
- mutations/01（oracle + 执行器测试）：18 个，抓住 17，漏掉 1（M17，见 F5）。
- mutations/02（只用执行器测试）：抓住 9，漏掉 9。漏掉的 M02–M05、M11–M14 都由 oracle 覆盖：
  - 未结算或不完整的结果；
  - 值类型；
  - 交接前的拒绝。
- 规划端草稿上有一个等价写法：去掉 fstat 的大小预检后，有界读取照样拒绝。

## 结论理由
- 所有门禁通过，D01–D06 满足；候选逐条满足合同。F1、F3、F4 是信任边界与合同范围之外的残留；F2、F5、F6 由规划端补全。

## 后续动作
- 集成到 master：依次 cherry-pick oracle `fd66ce3`、本候选的分支提交，以及规划端补全（F2 的桥接修正、F5 的补测、F6 的测试稳健性）。
- 设计说明与 IMPLEMENTATION_STATUS：写明 F1 的信任边界，以及 F3、F4 的残留。
- 人工介入：约 70 分钟（草稿与 oracle、发现并改正“用量必须在 finish 之前记入”、复审、变异检查）。
