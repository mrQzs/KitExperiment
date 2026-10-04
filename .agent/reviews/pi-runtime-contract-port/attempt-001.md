# REVIEW：pi-runtime-contract-port v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`60f2d159f30a36f956f542f363e72cd98a879f96`
- 候选树：`70b64e87f7babebff539c29751b1ac4b8f0100c4`（补丁往返校验：是）
- 要求的执行模型：bb/step-5-preview · thinking high（Windows 上派遣）
- 实际模型：bb/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 867.156 s / 1500 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5，macOS 上复审）；独立复审者按 `independent-review.md` 只读复审，结论同为 REVISE，发现已逐条复现后纳入

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致；基线提交存在 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、工具错误 0；执行报告 json_ok |
| G2 范围合规 | passed | A1/M1，violations 0。`/tmp` 命中（事件第 7835 行）只是内存探针里 `InvocationSpec` 的 cwd 字符串，没有文件访问。运行期间主工作区 HEAD 由 `4451479` 变为 `af6c130`：是规划端的两次提交（`142a388`、`af6c130`），只增加 `.agent/design/runtime-reference/*`，执行 worktree 的 HEAD 未动 |
| G3 行为正确 | failed | D01–D04 未满足，见下表与“需要修正的具体差异” |
| G4 回归检查 | passed | 检查记录：checks/03（macOS 干净副本，V01–V05 全部通过，120 passed）；checks/01、02 为 Windows 上的 in_place 与 fresh，同样通过 |
| G5 质量复审 | failed | 新测试把失败开放的行为写成断言（见“测试可信度”）；变异检查未运行 |
| G6 可集成 | 不集成 | 执行 worktree 在原 Windows 机器上，本机无法重新快照；候选补丁往返校验通过。G3/G5 失败，不 branch、不集成 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | failed | checks/03 V02/V05；内存探针 | 常量、`ALLOWED_TRANSITIONS`、argv 接受/拒绝集合与合同一致。但构造已接受的 `InvocationSpec` 调用 `.digest()` 会抛核心层 `ProtocolError`：`timeout_seconds=2**53` → `E_INTEGER_RANGE`，单个参数约 1.2 MB → `E_JSON_LIMIT`，违反“`.digest()` returns canonical SHA-256” |
| D02 | failed | 内存探针；candidate.patch `reconcile` | `reconcile(spec, None)` 与 `reconcile(spec, {})` 都返回 `NOT_STARTED`、`replay_allowed=True`。三个自选做法叠加：签名放宽为接受 None；从观察记录读取合同没有的 `handoff_accepted` 字段并缺省为 False；`{}` 按“没有观察”处理 |
| D03 | failed | 内存探针 | FAILED/STOPPED + `cleanup_confirmed: True` 时，`process_identity` 为 `{}` 或 `{'x': 1}` 也放行重放；NOT_STARTED + `process_identity: None` 或 `{}` 判为 NOT_STARTED 并放行。合同第 5 条的 ProcessIdentity 是四个必填且校验过的字段，这些都不是“matching process_identity” |
| D04 | failed | candidate.patch 测试文件 | 直接调用测试存在，旧 API 回归通过；但多处测试把上述失败开放行为写成断言（见下） |
| D05 | passed | scope.json；checks/03 V02/V04 | 无依赖、凭据、联网或真实 provider；原生清理保持未实现 |

## 范围与质量
- 越界与警告：violations 0、warnings 0。新增被忽略路径只有 `.pytest_cache/` 与 `__pycache__/`。
- 测试可信度：55 个新用例，基线上 V01/V05 失败、候选上通过，没有删除或弱化既有测试。但以下用例固定了错误行为，修正实现时必须同时改写：
  - `test_replay_allowed_only_for_clear_not_started`：断言 `reconcile(spec(), None)`、`reconcile(spec(), {})` 的 `replay_allowed` 为 True；
  - `test_empty_observation_record_has_no_start_evidence`、`test_status_is_taken_from_classify_start`：断言 `{}` / None → NOT_STARTED；
  - `test_started_observation_requires_bound_process_identity`：六键 identity（四字段 + run_id + environment_sha256）判为 STARTED；
  - `test_stale_generation_fence_rejected`：STARTING → STARTED 不带 identity；
  - `test_terminal_replay…` 之外，`classify(observation(state="FAILED", exit_code=3), handoff=False)` 断言为 FAILED。
- 变异检查（中高风险）：未运行。结论已由行为缺陷确定，现有测试锁定的是要改掉的语义；变异检查放到 v2 候选上。独立复审建议的首个变异：把 `reconcile` 的 handoff 缺省改为 True，现有用例应当变红。
- 对照冻结参考（`.agent/design/runtime-reference/`，执行器看不到）：参考测试套件在候选上 17 个用例 9 个失败；把候选错误码映射为参考错误码后仍有 6 处行为失败（三个 2**53 上界、`Lifecycle(state='STARTED')` 无 identity、被拒绝的 handoff 仍采纳 STARTED 观察、STARTED 接受非 identity）。参考套件不覆盖 `reconcile({})` 这类输入，失败开放问题由 21 项并排探针发现（20 项与参考不同）。
- 兼容性与残余风险：错误码全部与 Code-Kit 不同（`E_ARGV_*`、`E_SPEC_*`、`E_LIFECYCLE_*` 等对 `E_RUNTIME_*`）；`digest()` 用规范 JSON 且含 shell，参考按 `\0` 连接字段、不含 shell，同一规格两边摘要不同；没有 2**53-1 上界；`except RuntimeErrorSM` 捕获不到新的 canonical 错误。
- 执行器自述与证据是否一致：不一致。事件第 3397、5548 行执行器已经意识到“fail-closed would say replay not allowed without evidence”，仍选择更宽松的做法，并把这些选择写进 limitations，`contract_conflicts` 与 `unresolved_questions` 都为空，D02/D03 标为 done。执行说明要求遇到未定义行为时停止并报告。

## 结论理由
候选的范围、离线边界和检查都干净，但重放门禁在空观察、缺失观察和不成形的 process identity 上放行，这违反合同明文的“未知启动和不完整 cleanup 必须 fail closed”与第 5、8 条，属于执行偏差，失败分类记为 EXECUTION_ERROR。

规划端同样有责任，单独记录：
- 合同的 Canonical API 只是 Code-Kit runtime 的意译。错误码、摘要编码、整数上界、被拒绝 handoff 的处理、STARTED 必须带 identity，合同都没有写。
- 合同禁止执行器读取参考，冻结参考又是在派遣之后才提交的。

因此即使执行器没有选错，候选也不可能“逐项一致”。前三个 runtime 候选反复因同一类差异返工，根因相同。

## 后续动作
- 需要修正的具体差异（REVISE）：
  1. `reconcile` 只接受 Mapping（None 报 `E_RUNTIME_OBSERVATION` 一类错误），固定以 `handoff_accepted=True` 调用 `classify_start`，不读取观察记录里的 handoff 字段；
  2. `classify_start` 只把 None 当作“没有观察”，`{}` 走 run/environment 比较，得到 IDENTITY_MISMATCH；
  3. process identity 严格解码：只接受 `ProcessIdentity` 或恰好四键、经 `ProcessIdentity(**m)` 校验的 mapping，否则抛 identity 错误；NOT_STARTED 分支按键是否存在判断，不按真值；
  4. `handoff_accepted=False` 时，NOT_STARTED 以外的观察一律 IDENTITY_MISMATCH；
  5. Lifecycle：构造与 transition 进入 STARTED 都必须带 identity；
  6. timeout、pid、generation 加 2**53-1 上界，`.digest()` 对构造接受的规格不得抛异常；
  7. 错误码与 `digest()` 编码按冻结参考逐字对齐（v2 合同写明），并把冻结参考测试套件作为验收检查；
  8. 改写上面列出的锁定错误行为的用例。
- 待决的业务或环境问题（ESCALATE）：本合同 `max_attempts_total: 1` 已用完（默认值为 2）。返工需要用户追加 1 次 attempt 的预算，写进 v2 合同；v2 以本候选为基础，并在派遣前把参考源码与参考测试放进基线。
- 已授权且可以继续的集成动作：无。合同只授权独立复审后的 local_branch，本候选不集成。
- 人工介入：约 45 分钟（并排探针、参考套件对照、独立复审发现的复现与报告）。
