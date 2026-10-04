# REVIEW：pi-runtime-contract-port v2 · attempt-002

Verdict: ACCEPTED_WITH_EXCEPTION

## 基本信息
- 基线：`89db55bb9eadcf955a0f8ecb67609548ff1d93a2`（master `a14079f` + attempt-001 候选 `a84fbb1` + 规划端 oracle `4585eee`、`89db55b`）
- 候选树：`0aca9f36442a267fc6ea93a5642e3823c9a13b4c`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 717.865 s / 1500 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论见“独立复审”

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致；基线提交存在 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、实际模型一致；1 次工具错误是事件第 9402 行一次 edit 的 oldText 不匹配，随后重试成功 |
| G2 范围合规 | 豁免 | 机械检查 violations 0（只改了允许的两个文件，oracle、参考与 `.agent/` 未动）。但执行器三次把检查输出写到固定的 `/tmp/v01.txt`、`/tmp/v02.txt`、`/tmp/v05.txt` 再 `rm -f`（事件第 12533、12578、12900 行），违反执行说明第 5 条。影响评估见“范围与质量” |
| G3 行为正确 | passed | D01–D05 全部有证据，D03 的“blocked”经裁定解除（见“合同矛盾裁定”） |
| G4 回归检查 | passed | checks/01：macOS 干净副本，V01 38 passed、V02 158 passed、V03、V04、V05 83 passed |
| G5 质量复审 | passed | 34 个改写的测试没有弱化；变异 mutations/01 26/26、mutations/03 1/1 抓住；执行器测试文件有 4 处覆盖缺口（行为都由 oracle 守住），由规划端补测提交 |
| G6 可集成 | passed | 候选补丁往返校验通过；执行 worktree 快照与候选一致 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01；AST 比对 | canonical 层与冻结参考逐个定义比较（忽略 docstring 与注解写法）：`_MAX_SAFE_INTEGER`、`_runtime_text`、`validate_argv`、`_observation_identity`、`LIFECYCLE`、`ALLOWED_TRANSITIONS`、`Lifecycle`、`classify_start`、`reconcile` 完全相同；`InvocationSpec`、`ProcessIdentity` 去掉合同允许保留的 `validate()`、`to_dict()` 后完全相同。差分 oracle 在固定网格上无差异 |
| D02 | passed | checks/01 V02、V05；逐字比较 | 从 `class Invocation` 起的旧 API 与基线逐字一致；删掉的 `field` 导入原本就没有使用 |
| D03 | passed（有缺口） | checks/01 V05；review | 55 个测试函数全部保留，没有任何函数的断言调用变少，没有 skip/xfail/宽泛 except；失败开放的断言已改为参考结果，例如 `reconcile(spec(), {})` → `("IDENTITY_MISMATCH", False)`、`reconcile(spec(), None)` → `E_RUNTIME_OBSERVATION`。“不带 identity 进入 STARTED”一项没有按“同一输入断言参考结果”改写（见“测试可信度”），由规划端补测 |
| D04 | passed | scope.json | M2，violations 0 |
| D05 | passed | checks/01 V03、V04；scope.json | 无依赖变更、联网或凭据访问 |

## 合同矛盾裁定
执行器报告的 4 处 contract_conflicts 都出自 Behavior contract 第 5 条：
- 规划端的本意是“下列 a001 旧断言是错的，要改掉”；
- 合同用“至少包括：- `{}`/None 的观察可以重放 …”这种列表写法，执行器读成了要断言的目标语义；
- 它按第 1 条与冻结参考实现，把冲突写进报告并把 D03 标为 blocked。

裁定：4 处都以冻结参考为准，执行器采用的做法正确，D03 解除 blocked。这是规划端措辞问题，不算执行错误；改进已写进 Code-Kit plan-task 技能。

执行器另在 limitations 中指出：合同“草稿验证”写的是 20 个测试函数失败，实测为 34 个。原因是规划端统计时只 grep 了 `FAILED`，漏掉了只在子测试里失败的用例（`SUBFAILED`）。执行器按第 5 条的总要求全部改写，处理正确；这同样是规划问题。

## 范围与质量
- 越界与警告：
  - 机械检查 violations 0、warnings 0；新增被忽略路径只有 `.pytest_cache/` 与 `__pycache__/`。
  - 固定 `/tmp` 路径是本 attempt 的执行流程偏差。每次都是 `python -m pytest … > /tmp/vNN.txt 2>&1; echo "VNN exit=$?"; tail -1 /tmp/vNN.txt; rm -f /tmp/vNN.txt`，意图是保留真实退出码同时截短输出。三个文件现已不存在；只写测试输出、不碰仓库，但无法证明此前没有覆盖同名文件。
  - 同类偏差在 pi-acceptance a002 出现过一次，是第二次。根因是执行说明只要求“保留真实退出码”而没有给出合规写法。处理：Code-Kit 合同模板与 AGENTS.md 的执行器一节补上了“直接运行，或 `T=$(mktemp -d)` … `rm -rf "$T"`”的写法；KitExperiment 的 AGENTS.md 同步补上。`pi_dispatch.py` 是锁定的旧实现（a4fa199），不在本次修改。
- 测试可信度：
  - 55 个函数全部保留；34 行含断言的代码被替换、新增 40 行，都是对同一输入改断参考结果，没有删除测试或放宽断言。
  - 执行器测试文件的缺口（独立复审提出，规划端逐条核实成立；对应行为都由 oracle 守住，不影响结论）：
    1. 中：`test_stale_generation_fence_rejected` 原本断言“不带 identity 进入 STARTED”成功，执行器给所有调用补上了 identity，而不是对同一输入断言 `E_RUNTIME_IDENTITY`；`test_record_construction_is_validated` 同样改成了带 identity 的构造。文件里因此没有任何断言保护“STARTED 必须带 identity”（mutations/02 中 M08 漏掉）。
    2. 低：多余键的 identity 用例只用了不匹配的 run_id/环境或 `note`，缺少“四字段 + 匹配的 run_id 与环境摘要”这一形态，而它正是 a001 当初放行的输入；执行报告“六键 identity 断言 E_RUNTIME_IDENTITY”的说法不准确。
    3. 低：`test_replay_allowed_only_for_clear_not_started` 新增的 `observation(state="NOT_STARTED", identity_value=None)` 与第一项输入完全相同（helper 在 identity 为 None 时不写这个键），名不副实。
    4. 低：`test_input_types` 中“reconcile 从不读取 handoff 标志”的循环用的是 NOT_STARTED，classify_start 在检查 handoff 之前就已返回，测不出这条规则。
    处理：规划端在集成后另做一个提交补齐这 4 处，按同样标准检查（全量测试 + 针对性变异），不改动执行器候选。
- 变异检查：
  - mutations/01（三个 oracle 加执行器测试）：抓住 26 / 漏掉 0 / 跳过 0。逐个读了日志，失败的都是对应规则的断言，没有语法、导入或收集错误。
  - mutations/02（只用执行器测试文件）：抓住 19 / 漏掉 7，漏掉的 7 个变异归因如下，都已由 oracle 覆盖，见 mutations/01：
    - `M05 NOT_STARTED 分支按真值判断 identity` → 归因：合同交给 oracle 覆盖 → 处理：注明
    - `M08 构造 STARTED 不要求 identity` → 归因：合同交给 oracle 覆盖 → 处理：注明
    - `M12 timeout 无安全整数上界` → 归因：合同交给 oracle 覆盖 → 处理：注明
    - `M13 pid 无安全整数上界` → 归因：合同交给 oracle 覆盖 → 处理：注明
    - `M14 构造时 generation 无上界` → 归因：合同交给 oracle 覆盖 → 处理：注明
    - `M15 digest 分隔符不同` → 归因：合同交给 oracle 覆盖 → 处理：注明
    - `M18 不透明文本不拒绝 C1 控制符` → 归因：合同交给 oracle 覆盖 → 处理：注明
  - mutations/03（独立复审建议的追加探针）：`M27 reconcile 读取记录里的 handoff 标志（强转 bool，缺省 True）` 抓住 1/1，由 fail-closed oracle 的 `test_reconcile_assumes_an_accepted_handoff_and_ignores_record_flags` 捕获。
  - 另有三个变异与原实现等价，未列入清单：闭合映射检查有 TypeError 兜底；`transition` 的 STARTED 检查与代际耗尽检查有 `replace()` 触发的 `__post_init__` 兜底。
- 兼容性与残余风险：
  - 错误码、摘要编码与整数上界现在与 Code-Kit 相同。
  - `except RuntimeErrorSM` 仍捕获不到 canonical 错误（RuntimeErrorSM 是子类，合同第 2 条有意保留）。
  - 原生进程监督、RuntimeLease 与持久化仍为 NOT_RUN。
- 执行器自述与证据是否一致：一致。自检结果与 checks/01 相同，limitations 与合同矛盾如实报告。

## 独立复审
独立复审者只读复审，建议结论 PASS（偏差另行记录），并给出以下判断与发现：
- 判断：canonical 层与参考逐个定义一致；旧 API 从 `class Invocation` 上方的装饰器到文件末尾逐字节相同（两侧 SHA-256 `d6f16cd0…`）；4 处“合同矛盾”是执行器误读，不是合同自相矛盾。
- 发现：上节列出的测试文件中 1 处中、3 处低的缺口，以及确认成立的固定 `/tmp` 写入。

规划端逐条复现全部采纳。只在结论名称上与独立复审不同：固定 `/tmp` 写入违反执行说明，且是第二次出现，所以记为 ACCEPTED_WITH_EXCEPTION 而不是 PASS。

## 结论理由
- 候选在行为上与冻结参考逐项一致，旧 API 不变，测试改写没有弱化，三类 oracle 与变异检查都证明了这一点。
- 不记为 PASS，是因为执行器违反了执行说明第 5 条的临时文件规则：固定 `/tmp` 路径、第二次出现。它不影响候选内容，影响已评估为低。
- 按 Code-Kit 规则以 ACCEPTED_WITH_EXCEPTION 豁免 G2：
  - 豁免范围只限这三条写入固定 `/tmp/vNN.txt` 的检查命令；
  - 决定者为规划端（claude-opus-5-5），依据是用户 2026-10-04 的授权“Pi 跑完后直接验收集成”；
  - 同类问题的规则缺口已在本次修补。

## 后续动作
- 需要修正的具体差异：无。
- 待决的业务或环境问题：无。
- 已授权且可以继续的集成动作：
  - local_branch（`branch` 生成候选提交）；
  - 规划端补测提交：补齐“测试可信度”中执行器测试文件的 4 处缺口；
  - 用户授权的本地集成：按顺序把 `a84fbb1`、`4585eee`、`89db55b` 与 v2 候选提交 cherry-pick 到 master，在组合结果上重跑全量测试；
  - 然后标记 a002 为 INTEGRATED，a001 与同目标的 pi-runtime-identity、pi-runtime-kit-compat 为 SUPERSEDED。不推送。
- 人工介入：约 40 分钟（AST 比对、两轮变异、测试改写逐项核对、过程偏差评估与报告）。
