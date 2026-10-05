# REVIEW：scheduler-supervised-transitions v2 · attempt-002

Verdict: PASS

## 基本信息
- 基线：`63b4ed928a76f03d1ccdbbceb5c4d6e6fc072a47`（master `82d29a0` + oracle + attempt-001 的返工提交 + 规划端探针）
- 候选树：`3a20c4c`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 553.743 s / 1200 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 PASS

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok、工具错误 0 |
| G2 范围合规 | passed | 只改了两个允许的文件，violations 0；没有访问工作副本以外的路径 |
| G3 行为正确 | passed | v2 第 1–3 条满足；上一轮 F1–F7 已关闭 |
| G4 回归检查 | passed | checks/01：V01 oracle 10、V02 探针 7、V03 24、V04 21、V05 357 passed，V06–V08 通过 |
| G5 质量复审 | passed | 两处旧测试按合同改正；遗留的低级缺口由规划端补测 |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V02、V03；mutations/01、02 | 精确类型、身份先复制再校验 |
| D02 | passed | checks/01 V02、V03；探针 | `recover` 只接受精确 int；规划端探针确认 `True` 与 int 子类都报 `E_FENCED` |
| D03 | passed | checks/01 V01、V02、V04、V05、V08 | 被拒绝的调用不改变账本；v1 行为不变 |
| D04 | passed | 代码复审 | 两处旧测试改正，str 子类、`StrEnum`、dict 子类与 `2.0` 都有用例 |
| D05 | passed | checks/01 V06、V07；scope.json | 范围合规 |

## 范围与质量
- 增量只改了合同点名的检查：四处 `isinstance` 改为 `type(x) is str`；身份先查 `type is dict`、复制，再只校验副本；`recover` 的第 4 步内联为“精确 int 且等于当前代际”，code 与位置不变。
- 变异检查：
  - mutations/01（oracle + 探针 + 执行器测试 + 已有测试）：23 个，抓住 22 / 漏掉 1。`A-M15 身份不复制` → 归因：等价变异（`_emit()` 序列化 payload）→ 处理：注明。
  - mutations/02（只用执行器测试）：抓住 19 / 漏掉 4；新增的 7 个 v2 变异全部抓住，漏掉的 A-M05/06/07 由 oracle 把守。
- 独立复审提出的低级问题（规划端补测，单独提交）：
  - L1：`generation=True` 的用例是空测（当前代际为 2，`True == 1` 在旧实现上同样报 `E_FENCED`）；int 子类没有用例；`scheduler.py` 的注释把 `True` 写成等于 2。实现本身正确（规划端探针确认）。
  - L2：“身份检查先于代际与 owner”没有用例（实现正确，规划端探针确认）。
  - L3：`unknown` 的精确 str 用例打在已经 START_UNKNOWN 的操作上，一次改了两个条件。
  - L4（规划端引入）：探针与执行器测试都在模块级 `from enum import StrEnum`，需要 Python 3.11+；项目声明支持 3.10。由规划端加回退。
- 范围外（已有方法，只记录）：`finish()`、`fixture_verdict()`、`handoff()`、`_insert_usage()` 对 str 子类或非 int 代际有同类“先改后报错”的路径；`recover` 不校验 owner，`scheduler.recovered` 不含新 owner。后续合同再处理。
- 规划端的一处笔误：v2 探针提交说明与合同写“8 个用例”，实际是 7 个。

## 结论理由
- 合同要求全部满足，检查与变异证据充分；遗留问题只涉及测试与注释，由规划端补上。

## 后续动作
- 规划端补测（单独提交，随候选一起集成）：L1–L3 的用例，`scheduler.py` 注释修正，两个测试文件的 `StrEnum` 回退。
- 人工介入：约 20 分钟。
