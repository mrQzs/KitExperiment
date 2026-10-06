# REVIEW：scheduler-exact-inputs v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`04c981c79c392bdc38d3d5763f3c4dbb6e4c402c`（master `139fd9d` + 规划端 oracle `1ef2e6f` + `scheduler.recovered` 的 5 处 payload 断言加上 owner）
- 候选树：`5886a4618f9714fb1c27809533487eecf21edafd`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.0）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1029.852 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 REVISE

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok、工具错误 0 |
| G2 范围合规 | passed | 只改了两个允许的文件，violations 0。附件从中性临时目录附上，没有工作副本外路径，也没有访问主仓库（调度工具 2.3.0 起的改动第一次实机生效）。唯一的临时目录是执行说明给的截短写法，`rm -rf "$T"` 在最后，删除之后没有再接命令 |
| G3 行为正确 | failed | 第 2 条：`observe` 收到不可哈希的 dimension 时冒出 `TypeError`（F1） |
| G4 回归检查 | passed | checks/01（fresh）：V01 oracle 56、V02 10、V03 64、V04 38、V05 463 passed，V06–V08 通过 |
| G5 质量复审 | failed | 执行器测试有名不副实与不成立的断言（F2–F4） |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01、V02；独立复审的差分与随机探针 | 15 个方法被拒绝时账本都不变 |
| D02 | failed | 规划端探针；独立复审 | 精确类型表只有 F1 一处不符 |
| D03 | passed | V01；独立复审 11,000 例随机对照 | 与逐项 `observe` 零差异；失败时一项不记 |
| D04 | passed | V01、V03、V04 | 空原因被拒；`recovered` 带新 owner |
| D05 | passed | V03–V05、V08；独立复审与基线差分 80,239 步 | 合法调用的结果、事件顺序与快照不变 |
| D06 | passed | V02；代码复审 | Step 3 点名的用例都在；质量问题见 F2–F4 |
| D07 | passed | V06、V07；scope.json | 范围合规 |

## 发现
- **F1（中）`observe` 收到不可哈希的 dimension 时冒出 `TypeError`。**
  - 位置：`sentinelmesh/scheduler.py:656` 先用 dimension 查 `op.usage.get(dimension)`，之后才在 `_check_usage` 里检查 dimension 的类型。list、set、dict 都在这一行抛 `TypeError`（账本未变）。
  - 合同第 2 条：“不可哈希的值（例如 list）也照常报下表的 code，不能冒出 `TypeError`”；表中 `observe` 的 dimension 应报 `E_OBSERVATION_SHAPE`。
  - 基线同样会冒出（旧代码在 `dimension not in op.reservation` 处抛出），候选没有修掉。
  - 独立复审用 2019 个单参数探针确认，这是表内唯一冒出非 `ContractError` 的地方。
  - 规划端也有疏漏：oracle 只对 operation id 用了不可哈希的值，Step 3 也没有点名这一用例。
- **F2（低）`test_unhashable_values_raise_the_contract_code` 名不副实。** 文档字符串说“不可哈希的输入报表内 code”，测试体只是把 `CASES` 里已有的列表值重跑一遍。operation、dimension、call_key、`claim` 与 seed_* 的各参数都没有不可哈希用例，F1 正是从这里漏过去的。
- **F3（低）`test_stop_refuses_an_empty_reason_and_keeps_the_request_running` 名不副实。** 测试体只做了一次合法的 `stop("req-1")`，在基线上同样通过。
- **F4（低）“仍可 claim”的断言不成立。** `test_stop_with_an_empty_reason_changes_nothing` 用 `idem-1` 的幂等重放证明 request 仍可领取，但重放在检查 `stopped` 之前就返回了：把 `stopped` 设成 `"OPERATOR_STOP"` 后，重放仍返回 `replayed: True`。同一测试里 `stopped is None` 的断言有效。
- **F5（低）映射的“先编码 payload”没有执行器测试把守。** `test_settle_mapping_rejects_an_unencodable_later_entry` 的 `E_JSON_INTEGER` 来自缺省 evidence 的 `digest()`，不是记录前的 payload 编码。
  - 显式传入 evidence_hash 时，只有 payload 编码能拦住 `2**53`。候选在这条路径上正确，账本不变。
  - 独立复审写了一个“先校验、到记录时才编码”的变异：执行器的测试全部通过，而这个变异会先记下 tokens 与 cost，再抛错。
- **F6（信息）执行器测试在基线上的区分力。** 10 个测试中有 5 个在基线上也通过。`None`、`5`、bytes 这类非 str 值在旧实现上就会被拒绝；分辨精确类型靠 str 子类，按合同分工由 oracle 负责。这不算执行器的过错。
- **F7（信息·范围外）`rollover()` 在代际为 2^53−1 时先改后报错。** 它把代际加一、把所有 request 设为 `RECOVERY_REQUIRED`，然后报 `E_JSON_INTEGER`；`Scheduler(generation=2**53)` 也会被接受。
  - 规划端在固定候选上复现，基线行为相同。
  - `rollover` 不在合同的 15 个方法之内，记为残留，另出合同处理。
- 合同解读：执行器在 `contract_conflicts` 中写明，多个参数同时不合格时先报新增的格式检查。合同只规定了单点不合格时的 code，这一解读可以接受；v2 写明“多个参数同时不合格时报哪个 code，不作规定”。
- 合法调用的预期变化：`recover` 的 owner 含孤立代理项（如 `"\ud800"`）时，现在在修改之前报 `E_JSON_OUTPUT`，基线则接受。这是第 1、5 条的必然结果。

## 变异检查
- mutations/01（oracle + 执行器测试 + 回归测试）：36 个，抓住 36，漏掉 0，跳过 0。
  - 抽查 M06、M15、M21、M22、M23、M30、M33 的日志，失败的都是对应规则的用例，都是断言失败。
  - 其中 M23（空映射直接返回 `{}`）只有执行器的测试能抓住。
- mutations/02（只用执行器测试）：抓住 4（M21、M23、M32、M33），漏掉 32。漏掉的都是精确类型规则，按分工由 oracle 把守；原因见 F6。

## 结论理由
- F1 违反合同第 2 条，D02 不满足，属于执行错误；Step 3 没有点名这一用例，规划端有连带责任。
- 修正范围很小，可以在合同范围内定点完成；预算还剩 1 次（`max_attempts_total: 2`），所以出 v2 在本候选上返工，不由规划端代做。

## 后续动作
- v2：在本候选上继续，基线为本候选的返工提交加上规划端探针。
  1. `observe` 只用精确的 str 去查已记录的用量；dimension 不可哈希时报 `E_OBSERVATION_SHAPE`。
  2. 修改执行器测试：
     - 表内每个能取到不可哈希值的参数，各给一个 list，断言表内的 code（F2）；
     - F3 的测试改名，或并入相邻用例；
     - F4 改为对 task-2 用新键 `claim`，断言 `replayed` 为 False。
  3. 规划端探针守护 F1，以及“映射 + 显式 evidence_hash + `2**53`”的整体拒绝（F5）。
  4. 合同写明：多个参数同时不合格时报哪个 code，不作规定。
- F7 记入设计说明第 7 节的残留。
- 人工介入：约 60 分钟（oracle 与草稿验证、复审、探针复现）。
