# REVIEW：scheduler-supervised-transitions v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`befbba9b95f874d39cc0df6a30a8e4abbcfb761c`（master `b454938` + 规划端 oracle）
- 候选树：`e712a9e8b45b9bc46a06198a2f7100aa954d78dd`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 514.101 s / 1500 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论同为 REVISE

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok、工具错误 0 |
| G2 范围合规 | passed | 只改了两个允许的文件，violations 0；临时目录用 `mktemp -d … rm -rf "$T"`；没有访问工作副本以外的路径 |
| G3 行为正确 | failed | 合同开头的总规则对 str 子类输入不成立（F1）；`recover` 接受非 int 代际（F5） |
| G4 回归检查 | passed | checks/01：V01 oracle 10、V02 19、V03 21、V04 345 passed，V05–V07 通过 |
| G5 质量复审 | failed | Step 3 第 5 项的顺序写反（F2）；“重复释放”的断言测的是 RUNNING（F3） |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | failed | 代码复审；规划端探针 | 对类型精确的输入满足；str 子类见 F1 |
| D02 | failed | 同上 | `unknown` 的 str 子类原因、身份的 str 子类键见 F1 |
| D03 | failed | 同上 | 九步顺序与三种结果正确；str 子类与非 int 代际见 F1、F5 |
| D04 | passed | checks/01 V03、V04、V07 | 已有行为不变 |
| D05 | failed | 代码复审 | F2、F3 |
| D06 | passed | checks/01 V05、V06；scope.json | 范围合规 |

## 发现
- **F1（中）str 子类能通过校验，结果是改了账本却没有事件。**
  - 新方法用 `isinstance(x, str)` 判断“是 str”；身份只比较原 dict 的键集合。
  - str 子类（包括 `StrEnum` 与 `(str, Enum)`）能通过这些检查，状态随即被改，直到 `_emit()` 做规范化时才报 `E_JSON_TYPE`/`E_JSON_KEY`。
  - 规划端在固定候选上用探针复现：`release_proven_unstarted`、`unknown`、`recover`、`mark_running`（身份键为 str 子类）都是“报 E_JSON_*，账本已改变”。`recover` 的情形最严重：接管已经发生，却没有 `scheduler.recovered` 事件，重试又会被 `E_RECOVERY_NOT_FENCED` 拒绝。
  - 合同开头要求“所有参数检查都在任何改动之前完成：被拒绝的调用不改变……”。无论把 str 子类算作 str 还是不算，现在的行为都不符合。
  - 已有的 `finish()`、`fixture_verdict()` 有同样的写法，属于已有行为，不在本合同范围。
- **F2（中）Step 3 第 5 项写反了顺序**：测试先 `settle` 再释放，合同要求“NOT_STARTED 之后补记”。因此 `observe` 在 NOT_STARTED 上被接受并计入 known 这条路径没有测试把守（规划端探针确认行为本身正确：返回 APPLIED，known 5、held 0）。
- **F3（低）**：注释写“已处于 NOT_STARTED”的断言，传入的是 RUNNING 的操作，与前一条断言重复；“重复释放”没有用例。
- **F4（低）**：`recover` 的检查顺序只固定了三组先后；成功路径缺 HANDOFF_ACCEPTED→FINISHED 与 RUNNING→START_UNKNOWN；成功后没有断言 request 的停止状态不变、恰好追加一条事件。
- **F5（低）**：`recover` 的第 4 步只做 `==` 比较，`generation=2.0` 能通过，并被写进 `op.generation`；再换代后，事件里的 `from_generation: 2.0` 会让 `_emit()` 报错，而改动已生效。
- **F6（低）**：执行器报告说“身份优先”的顺序已在测试中固定，但没有“非法身份 + RUNNING”的用例；报告还说第 5 项是“NOT_STARTED 后补记”，与测试不符。
- **F7（信息）**：身份应先复制再校验副本，避免并发修改调用方的 dict 时绕过校验。
- 执行器报告的那处解读（身份校验先于代际与状态检查）可以接受，v2 写进合同。
- 规划端注意（不要求本合同修改）：`scheduler.recovered` 不含新 owner；当前代际的 START_UNKNOWN 只能换代后恢复。桥接合同按此设计。

## 变异检查
- mutations/01（oracle + 执行器测试 + 已有测试）：16 个，抓住 15 / 漏掉 1 / 跳过 0。
  - `A-M15 身份不复制` → 归因：等价变异（`_emit()` 会序列化 payload，`events` 还会再深拷贝），单线程下观察不到 → 处理：注明；F7 要求先复制再校验，理由是并发。
- mutations/02（只用执行器测试）：抓住 12 / 漏掉 4。漏掉的 A-M05、A-M06、A-M07 按分工由 oracle 把守，A-M15 同上。

## 结论理由
- F1 违反合同的总规则，F2 没有完成 Step 3 的要求，都属于执行错误；F5 同属第 5 条的参数检查。
- 预算还剩 1 次（`max_attempts_total: 2`），出 v2 在本候选上定点返工。

## 后续动作
- v2（在本候选上继续，基线为本候选的返工提交 + 规划端探针）：
  1. 新方法的类型检查一律用精确类型：哈希、原因、outcome 是 `type(x) is str`；身份是 `type(x) is dict`，先复制，再要求副本的每个键都是精确的 str、键集合恰好四个，然后用副本构造 `ProcessIdentity`。
  2. `recover` 第 4 步：`type(generation) is not int` 或不等于当前代际，都报 `E_FENCED`。
  3. 把“身份校验先于代际与状态检查”写进合同。
  4. 补测：F2、F3、F4、F6，以及每个新方法的 str 子类与 `StrEnum` 输入（报具体的 code，账本不变）。
- 人工介入：约 25 分钟（oracle 与草稿、复审、探针复现）。
