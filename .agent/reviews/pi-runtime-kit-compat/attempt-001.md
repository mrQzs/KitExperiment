# REVIEW：pi-runtime-kit-compat v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`9c97134396cce23e0e47694030b53d3c728e9d41`
- 候选树：`2a436923affa598d03c24777b3dcd86e2b85342c`
- 要求的执行模型：bb/step-5-preview · thinking high
- 实际模型：bb/step-5-preview；providerThinkingLevel 未回报，只能确认请求为 high
- 运行：Pi 退出码 0；1047.969s / 1200s；未超时、未取消、无残留进程
- 费用：unknown
- 复审者：规划/验收会话；已进行独立 fresh 检查和源码/probe/变异复审，另行委派的独立复审待回传

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | base、合同、候选补丁往返树一致；实际 provider/model 与合同一致 |
| G1 执行完整 | failed | summary 记录最终正文含未执行的 `<tool_call>`，虽有 agent_settled，但执行报告为 no_json，调度器将状态置为 NEEDS_REVIEW |
| G2 范围合规 | passed | `scope.json` 为 A1/M1/D0/T0，violations=0，主工作区未被改动 |
| G3 行为正确 | failed | 新 API 名称存在，但关键签名调用语义和 Code-Kit 参考返回形状不兼容 |
| G4 回归检查 | passed | `check` 与 `check --fresh` V01–V05 全部通过；fresh V02 为 118 passed |
| G5 质量复审 | failed | 变异 5/5 抓住，但测试只保护了候选自定义语义，没有保护参考调用方的 observation/reconcile 形状 |
| G6 可集成 | failed | G1/G3 失败，不能 branch 或集成 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | failed | fresh V02/V05；独立 probe | `validate_argv` 签名和四字段 ProcessIdentity 通过；但缺少 Code-Kit 的 `LIFECYCLE` 常量，Lifecycle 可变且状态/转换集合扩大，未完全兼容 |
| D02 | failed | candidate.patch；probe | `classify_start` 没有解析参考 observation 的 `run_id/environment_sha256/state/process_identity` 形状；`reconcile(expected: InvocationSpec, observed: Mapping)` 不能接受参考 expected，直接抛 `RuntimeErrorSM(E_IDENTITY_TYPE)`；返回字符串而非参考 mapping（status/run_id/expected_environment_sha256/observed_environment_sha256/replay_allowed） |
| D03 | failed | candidate.patch；probe | generation 增加和 stale fence有实现，但 Lifecycle 是原地可变且不保留参考 frozen/replace 语义；错误表 `RuntimeErrorCode` 是常量容器而非 Code-Kit 风格异常；opaque text 未拒绝 C1 0x80–0x9f |
| D04 | failed | fresh V02；独立 probe | 新测试覆盖候选 API 118 项，但未直接保护参考 reconcile mapping、参考 start observation 字段和 `LIFECYCLE` 常量 |
| D05 | passed | scope/events-summary；fresh V03/V04 | 无依赖、无凭据、无联网或真实 provider；native cleanup 保持未运行 |

## 范围与质量
- 越界与警告：`scope.json` violations=0、warnings=0；仅 pytest/pycache 缓存关注项，无外部临时路径。
- 测试可信度：fresh 118 passed、V01/V05 基线失败；没有删除既有测试，但新增测试未覆盖参考 mapping 语义。
- 变异检查（中高风险）：`mutations/01`：抓住 5 / 漏掉 0 / 跳过 0。它证明候选内部 gate 有断言，不能证明参考 API 兼容。
- 关键独立 probe（在候选 worktree）：
  - `reconcile(InvocationSpec(...), {'state':'NOT_STARTED', ...})` → `RuntimeErrorSM E_IDENTITY_TYPE`，参考应返回 mapping。
  - `classify_start(handoff_accepted=True, started_observation={'run_id':'run','environment_sha256':'a'*64,'state':'NOT_STARTED'}, expected=InvocationSpec(...))` → 候选按缺少 `started`/identity 判为 START_UNKNOWN，参考应为 NOT_STARTED。
  - 候选 `has LIFECYCLE` 为 False；Code-Kit 参考导出 `LIFECYCLE`。
- 兼容性与残余风险：调用方若按 Code-Kit 传 InvocationSpec/observation，会被拒绝或得到错误分类；不能用于控制重放。
- 执行器自述与证据是否一致：无 executor-report；事件完整但末尾有未执行 tool-call，summary 已明确。

## 结论理由
候选补齐了若干输入校验和 generation 保护，范围、离线边界和局部测试质量良好；但合同目标是逐项兼容 Code-Kit runtime，而候选没有实现参考的 `reconcile` 输入/输出、start observation 字段解析和 `LIFECYCLE` 导出，属于核心行为缺口。独立检查全绿不能替代这些缺失的调用方证据。

## 后续动作
- 需要修正的具体差异（REVISE）：重新出新合同，明确并测试：
  1. `LIFECYCLE = ('NEW','STARTING','STARTED','STOPPING','STOPPED','FAILED','START_UNKNOWN')` 及 `ALLOWED_TRANSITIONS`；
  2. frozen `Lifecycle` 的 `transition` 返回新对象、每次 generation+1、START_UNKNOWN 可恢复；
  3. `classify_start` 解析 `run_id`, `environment_sha256`, `state`, `process_identity`；
  4. `reconcile(InvocationSpec, Mapping)` 返回参考五键 mapping，只有 NOT_STARTED 或完整终止身份/cleanup 才 replay；
  5. `RuntimeErrorCode(ValueError)`/旧 RuntimeErrorSM 的兼容错误对象和 C1/surrogate 边界。
- 待决业务/环境问题：无；bb/step-5-preview high 可用。
- 已授权且可以继续的集成动作：无，候选交返工，不执行 local_branch。
- 人工介入：约 20 分钟，用于参考 API probe 和审查。


