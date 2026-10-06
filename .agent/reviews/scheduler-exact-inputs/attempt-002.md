# REVIEW：scheduler-exact-inputs v2 · attempt-002

Verdict: PASS

## 基本信息
- 基线：`35c65dcbfcc1b58c61b4b1928b34573a9efc631a`（attempt-001 候选的返工提交 `89d049e` + 规划端探针 `tests/test_scheduler_exact_inputs_probes.py`）
- 候选树：`165e982c46cccd0991266a4864ab4c7564a09dd1`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.0）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 859.01 s / 1200 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 PASS

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok、工具错误 0 |
| G2 范围合规 | passed | 只改了两个允许的文件，violations 0；没有工作副本外路径。4 条临时目录命令都是执行说明给的写法：只写 `"$T"` 之内，最后一步是 `rm -rf "$T"`，删除之后没有再接命令 |
| G3 行为正确 | passed | F1 已修正；第 1–6 条其余规则不变 |
| G4 回归检查 | passed | checks/01（fresh）：V01 oracle 56、V02 探针 2、V03 10、V04 64、V05 38、V06 465 passed，V07–V09 通过 |
| G5 质量复审 | passed | F2–F4 三处测试名副其实，没有削弱的断言；只有两条信息项，见下 |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V02、V03；独立复审 2420 个单参数探针 | `observe` 的不可哈希 dimension（8 种值）报 `E_OBSERVATION_SHAPE`，账本不变 |
| D02 | passed | V01、V04–V06、V09；与 attempt-001 差分 81,251 步 | 除 F1 外，结果、账本（含事件）与快照没有任何差异 |
| D03 | passed | V03；代码复审 | F2 的 38 行恰好是合同清单；F3 改名；F4 改为对 task-2 新领取，断言 `replayed` 为 False |
| D04 | passed | V07、V08；scope.json | 范围合规 |

## 复核
- 实现只改了一处：`observe` 只用精确的 str 去查已记录的用量（`sentinelmesh/scheduler.py:659`），与规划端草稿一致。
- 删掉的 2 行含断言的代码都在预期之内：
  - 旧 F2 循环里的调用：它用到的列表值仍由未改动的 `CASES` 覆盖，新 F2 也覆盖了同样的参数；
  - F4 要求删除的幂等重放断言。
- 独立复审在内存中用 attempt-001 的实现运行新 F2：只有 `observe` dimension 一行失败；原始基线则有 26 行失败。说明 F2 现在确实守住 F1。

## 发现（都不阻塞）
- **I1（信息）** `tests/test_scheduler_exact_inputs.py:214` 的文档字符串写 “Every Rule 2 parameter”，但 generation 没有不可哈希用例。合同 F2 的清单本来就不含 generation；独立复审确认 7 个方法收到 list 作为 generation 都报 `E_FENCED`。
- **I2（信息）** F2 的 38 行没有包在 `subTest` 里，第一行失败会挡住后面各行，只影响诊断。
- **I3（信息）** 只用执行器测试时，变异漏掉 18 个。漏掉的都由 oracle 或探针把守：str 子类与 `isinstance` 的区别、generation 类型、编码先后、`_enforce` 的时机、`recovered` 的 owner。
- **I4（信息·范围外）** `rollover()` 在代际为 2^53−1 时先改后报错，attempt-001 复审已记为残留，另立合同。

## 变异检查
- mutations/01（oracle + 探针 + 执行器测试 + 回归测试）：37 个，抓住 37，漏掉 0，跳过 0。
  - 新增的 M37“撤回 F1 修正”由规划端探针抓住（日志 `M17.log`：三个不可哈希 dimension 都失败）。
- mutations/02（只用执行器测试）：抓住 19，漏掉 18；attempt-001 时只抓住 4。新增的 F2 用例同样抓住了“撤回 F1 修正”。

## 结论理由
- 所有门禁通过，D01–D04 满足；独立复审的差分与探针都没有发现退化。

## 后续动作
- 集成到 master：依次 cherry-pick oracle `1ef2e6f`、payload 断言 `04c981c`、attempt-001 的返工提交 `89d049e`、v2 探针 `35c65dc` 与本候选的分支提交，然后在 master 上重跑全量测试。
- attempt-001 标记为 SUPERSEDED，本 attempt 集成后标记为 INTEGRATED。
- 设计说明第 7 节与 IMPLEMENTATION_STATUS 记录本合同；残留项加入 `rollover()` 的整数上限问题。
- 人工介入：约 20 分钟（探针、复审、变异检查）。
