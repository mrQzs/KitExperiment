# REVIEW：pi-runtime-identity v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`5f43cdf32ba029c3bc045871d8b753c024159366`
- 候选树：`3f2e8262ee2d2dac7d896a443773d72d72731dd2`
- 要求的执行模型：bb/step-5-preview · thinking high
- 实际模型：bb/step-5-preview；providerThinkingLevel 未回报，只能确认请求为 high
- 运行：Pi 退出码 3221225786；900.063s / 900s；TIMEOUT，未产生执行报告
- 费用：unknown
- 复审者：规划/验收会话；已尝试委派独立复审，但当前复审模型容量不足，结论基于调度器证据、fresh 检查、源码审查和变异检查

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | base、合同哈希、候选补丁往返树一致；实际 provider/model 与合同一致 |
| G1 执行完整 | failed | `summary.md` 明确 TIMEOUT，最终 stopReason=toolUse，没有 agent_settled 或 executor report |
| G2 范围合规 | passed | `scope.json` 记录 A1/M1/D0/T0，violations=0；主工作区未被修改 |
| G3 行为正确 | failed | 独立检查虽通过，但候选 API 与 Code-Kit 参考契约不兼容（见结论理由） |
| G4 回归检查 | passed | `check` 与 `check --fresh` 的 V01–V05 全部通过；fresh V02 为 124 passed |
| G5 质量复审 | failed | 变异 5/5 抓住，但核心 API 形状/语义偏离合同参考，不能集成 |
| G6 可集成 | failed | TIMEOUT 且存在必须返工的 API 差异，不能 branch/integrate |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | failed | fresh V02/V05；`candidate.patch` | 虽新增六个名称，但 `ProcessIdentity` 额外要求 request_id/run_id，`classify_start` 改成单一 observation 参数，`reconcile` 改成 keyword-only 参数；均不兼容 Code-Kit 参考 API |
| D02 | failed | `tests/test_runtime_identity_contract.py` | 候选有保守 replay/lifecycle 测试，但旧 Code-Kit 的 `classify_start(handoff_accepted=..., started_observation=..., expected=...)` 和 `reconcile(expected, observed)` 不能按参考调用 |
| D03 | passed | fresh V02/V03/V04；mutations/01 | 反例、identity/generation fence、START_UNKNOWN、cleanup replay、argv/NUL 均有测试；变异 5 抓住、0 漏掉 |
| D04 | blocked | summary、events-summary | 没有生产 provider/跨平台 native 操作，边界保持未运行；但执行器超时，未完成自述报告 |

## 范围与质量
- 越界与警告：`scope.json` violations=0、warnings=0；仅出现 .pytest_cache/__pycache__ 缓存关注项，未发现外部临时路径或外部副作用。
- 测试可信度：fresh 全量 124 passed；测试在基线 V01/V05 对新 artifact/API 失败；没有 skip 或删除既有断言。
- 变异检查（中高风险）：`mutations/01`：抓住 5 / 漏掉 0 / 跳过 0。
- 兼容性与残余风险：实现把 Code-Kit 的纯契约重新设计成另一套更严格、更宽的 API，可能使基于 Code-Kit 的调用方和隐藏测试直接失败；相对 cwd 限制也不是 Code-Kit `InvocationSpec` 的语义。
- 执行器自述与证据是否一致：没有执行器自述；summary/event 只证明实际模型、范围和超时。

## 结论理由
候选实现的离线行为质量较好，且 fresh 检查和变异检查均为绿色；但本合同明确要求以 Code-Kit runtime 作为参考 API/语义来源。Code-Kit 当前参考实现的可调用形状是：`ProcessIdentity(pid, boot_id, executable_sha256, worktree_id)`；`classify_start(handoff_accepted=..., started_observation=..., expected=...)`；`reconcile(expected, observed)`，并使用其既有状态/返回字段。候选将这些改成了不同的必填字段、单参数分类器和 keyword-only reconciliation，不能称为兼容移植。与此同时 G1 为 TIMEOUT，不能绕过把这个候选标成 PASS。

## 后续动作
- 需要修正的具体差异（REVISE）：重新出 `pi-runtime-identity.v2`，明确逐项保持 Code-Kit `scripts/codekit/runtime.py` 的构造器、函数签名、状态名和返回键；KitExperiment 旧 `Invocation` API 继续保留，新增兼容层应在独立模块或适配器中完成。补测试：用四字段 ProcessIdentity、关键字 classify_start、位置参数 reconcile 直接调用参考形状。
- 待决的业务或环境问题：无；Pi provider/model 已可用。
- 已授权且可以继续的集成动作：无，候选交返工，不执行 local_branch。
- 人工介入：约 15 分钟，用于参考 API 对照和独立检查。


