# REVIEW：scheduler-operation-view v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`ce6d0655ae6da91c6ccfc66e4647cc103a9be829`（master `3cc13a3` + 规划端 oracle，8 个纯内存用例）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.1）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 299.0 s / 1200 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）。风险为 low，按 review-pi 不做独立复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok，工具错误 0 |
| G2 范围合规 | passed | 只改 `sentinelmesh/scheduler.py`，新增 `tests/test_scheduler_operation_view.py`；violations 0，没有工作副本外路径 |
| G3 行为正确 | passed | 逐条满足合同第 1–3 条（见下） |
| G4 回归检查 | passed | checks/01（fresh）：V01 oracle 8、V02 8、V03 132、V04 全量通过，V05–V06 通过 |
| G5 质量复审 | passed | 改动只有三处，注释写明了检查顺序的理由 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条 `operation()`：在锁内先查精确 str，再查字典；返回新 dict，七个键，`reservation` 是 `dict(op.reservation)` 的副本；不发事件、不受代际约束。
- 第 2 条 `rollover()`：先编码 `{"generation": 当前 + 1}`，再改代际、停止 request、追加事件，事件用已编码的载荷。
- 第 3 条构造函数：`exact_int` 在前（`E_INTEGER`），之后 `generation > 2**53-1` 报 `E_JSON_INTEGER`。

## DoD 映射
| DoD | 结果 | 证据 |
|---|---|---|
| D01 | passed | V01、V02 |
| D02 | passed | V01、V02 |
| D03 | passed | V01 |
| D04 | passed | V03、V04 |
| D05 | passed | V02；执行器测试覆盖 Step 3 的五项 |
| D06 | passed | V05、V06；scope.json |

## 变异检查
- mutations/01 与 02（oracle + 执行器测试）：11 个，全部抓住。
- mutations/03（只用执行器测试）：抓住 7，漏掉 4：
  - rollover 先停 request（V05）；
  - 构造函数不查上限（V06）、先查上限再查整数（V07）、上限差一（V11）。

  这四项都由 oracle 覆盖。合同 Step 3 没有要求执行器测构造函数的上限，不算执行遗漏。

## 发现
- 无阻塞发现。
- 信息：一个已知的边界变化，合同已经写明。代际是 2**53-1 时，`rollover()` 报 `E_JSON_INTEGER` 并保持账本不变；以前它会先改账本再报错（scheduler-exact-inputs 复审 F7）。

## 后续动作
- 集成到 master：依次 cherry-pick oracle `ce6d065` 与本候选的分支提交。
- 第三份合同 `verified-review` 用 `Scheduler.operation()` 核对 request 与预约维度。
- 人工介入：约 25 分钟（草稿与 oracle、合同、复审、变异检查）。
