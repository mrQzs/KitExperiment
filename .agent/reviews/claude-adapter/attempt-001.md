# REVIEW：claude-adapter v1 · attempt-001

Verdict: PASS

<!-- review-draft 生成（2026-10-16T11:18:59Z，调度工具 2.10.0）：事实取自 .agent/runs/claude-adapter/attempt-001；标着“待填”的地方是复审判断，全部填完 verdict 才收。
     Verdict 行只能写一个结论，并与 verdict 命令的 --verdict 一致。 -->

## 基本信息
- 基线：`1350884b227f6e65bd9c69d80b492221f4102fa0`
- 候选树：`e55c7bcc491472e9538b145bd6df9692897e10e6`（补丁往返校验通过；3 files changed, 627 insertions(+), 13 deletions(-)）
- 要求的执行模型：step/step-5-preview · thinking high
- 实际模型：step/step-5-preview；providerThinkingLevel：未回报
- 运行：退出码 0 · 用时 996.687 s / 上限 3000 s · 正常结束 · 工具错误 1
- token：输入 133408 · 输出 22116 · 缓存读 1527552 · 缓存写 0；费用 unknown（unknown）
- 复审者：Claude Code 规划/验收会话（claude-opus-5-5）；独立复审：同会话派出的只读子代理（general-purpose，同一模型，未换厂商），跑了 oracle 与执行器测试，并用进程内探针逐一试了失败关闭的输入

## 门禁

| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | 工具：通过 |
| G1 执行完整 | passed | 工具：通过；提供商未回报思考级别：只能确认请求的级别（high） |
| G2 范围合规 | passed | 工具：通过 |
| G3 行为正确 | passed | fresh 8/8；oracle 12/12、执行器 6/6；候选变异 24 条中 22 条至少被一条命令抓住，两边都漏的 2 条都是等价变异（C15 身份由父类再核对一次；C23 第二个 init 必然不在第 1 行，照样被行号检查拦下） |
| G4 回归检查 | passed | 工具：通过；检查记录：checks/01（fresh，8/8 通过）；检查在干净副本（基线 + 候选补丁 + setup）上运行 |
| G5 质量复审 | passed（有发现） | 独立复审建议 PASS，七条发现全是低级别：注释与文档串措辞（F1–F3）、`num_turns` 没有上限（F4）、块检查顺序的两种读法（F5）、两处测试缺口（F6）、Step 4 之外多一例（F7）；F1–F4、F6 由规划端补全 |
| G6 可集成 | passed | 工具：通过 |

## DoD 映射

| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V01 passed、V02 passed、V07 passed | 常量、翻译结果与逐帧规则（第 1–3、5 条）；目的：真实录下的 Claude Code 流被一帧不差地翻成规范事件，正文不进事件，没见过的帧一律失败关闭 |
| D02 | passed | V01 passed、V02 passed | 终止帧与用量（第 4 条）；目的：账本里结算的 token 正是 CLI 自己在 `result` 里报告的会话合计，错误终止与用量不合法时不多记一分 |
| D03 | passed | V01 passed、V07 passed | adapter 类与注册（第 6–7 条）；目的：作业写 `adapter_id: "claude"` 就能经 `once`/`serve` 结算，名字精确匹配，pi 与离线桩不受影响 |
| D04 | passed（文档串措辞在补全中修正） | V03 passed、V04 passed、V08 passed、复审 | 其它模块与命令不受影响，文档串与导入按第 8 条 |
| D05 | passed | V05 passed、V06 passed、scope（越界 0） | Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更 |

## 范围与质量
- 越界与警告：越界 0、警告 0
- 测试可信度：执行器测试 6 例（Step 4 的五组加一例经参考归约器的正常结算），在基线上全部失败；没有 skip、没有弱化，没有照抄 oracle。它与 oracle 分工：只用执行器测试时抓住 10/24，漏掉的是 oracle 把守的规则；oracle 漏掉的 M22–M24（摘要算法、合计上限、冻结）正是 Step 4 指定给执行器的，执行器测试都抓住了。复审另指出两处两边都抓不住的偏差（块计数互换、摘要的 `ensure_ascii`），补全补测试。
- 变异检查：
  - mutations/01：24 条，命令 oracle、executor
    - oracle：抓住 19，漏掉 5（意外 4：M21 M22 M23 M24），跳过 0，出错 0
    - executor：抓住 10，漏掉 14（意外 13：M02 M03 M04 M06 M07 M08 M09 M11 M15 M16 M17 M20 M21），跳过 0，出错 0
    - `M02 C02 init 不必是第一帧`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M03 C05 工具调用块也接受`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M04 C06 result 之后的帧也接受`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M06 C08 负数也接受`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M07 C09 布尔也算整数`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M08 C10 is_error 不看`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M09 C11 没有 assistant 也算成功`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M11 C13 注册表没有 claude`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M15 C17 is_error 不查类型`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M16 C18 num_turns 不查`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M17 C19 assistant 不要求模型`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M20 C22 异常帧不清空用量`（executor 意外漏掉） → 归因：命令分工（oracle 已抓住；合同 Step 4 要求执行器不照抄 oracle） → 处理：注明，无需补测试
    - `M21 C23 第二个 init 也接受`（oracle、executor 意外漏掉） → 归因：等价变异（第二个 init 必然在第 1 行之后，行号检查照样记成 `claude.invalid_frame`，只是原因文字不同） → 处理：注明；决策模型 D2 给它的等价概率只有 0.10
    - `M22 C24 摘要不排序键`（oracle 意外漏掉） → 归因：命令分工（合同 Step 4 指定给执行器测试，executor 命令已抓住） → 处理：注明，无需补测试
    - `M23 C25 合计不查上限`（oracle 意外漏掉） → 归因：命令分工（合同 Step 4 指定给执行器测试，executor 命令已抓住） → 处理：注明，无需补测试
    - `M24 C26 翻译结果不冻结`（oracle 意外漏掉） → 归因：命令分工（合同 Step 4 指定给执行器测试，executor 命令已抓住） → 处理：注明，无需补测试
    - 预期内的漏掉（expect_missed：合同交给其他命令覆盖的规则）：M13 → 处理：注明
- 兼容性与残余风险：`offline` 与 `pi` 不受影响（V03、V08，`test_run_cli_pi.py` 的三项注册表由规划端在基线提交里改好）。复审逐一试了空流、只有限额帧、只有 `result`、畸形或错位的 init、`result` 之后的异常帧、畸形 `result`、中间的异常帧、非对象帧，都不记用量；把标记字符串放进思考、正文、工具输入、`user` 帧、`result` 正文、init 的 `cwd`，事件里都找不到。发现：
  - F1【低】`run_cli.py` 注册表注释在三个 adapter 时仍写“Neither adapter starts a provider”。
  - F2【低】`run_cli.py` 模块文档串“`offline` reduces …, `pi` translates … and `claude` translates … -- each of them reduced the same way”读起来像 `offline` 也在翻译。
  - F3【低】`claude_events.py` 文档串说成功之外“每一种形状都是完整的 `error` 终止”，没有排除字段畸形的 `result`（那是 `claude.invalid_frame`、不发终止），也没写用量非法时运行仍结算、只是用量未知。
  - F4【低】`num_turns` 没有 `MAX_SAFE_INTEGER` 上限：`num_turns = 2**60` 时翻译结果说用量完整，归约器却以 `E_ADAPTER_EVENT` 拒收。失败关闭成立，只是两层说法不一。
  - F5【低，接受】内容块逐块检查，第一个出问题的块决定是 UNKNOWN 还是 INVALID；与 pi 一致，归约器两者都当 unknown。
  - F6【低】块计数互换、摘要改 `ensure_ascii=True` 两种偏差，oracle 与执行器测试都抓不住；执行器测试文档串说每个拒绝用例先证明合法基线，合计上限那例没有。
  - F7【低，接受】Step 4 说“只覆盖这些”，执行器多写一例正常结算（解读第 6 条），无害。
  - 规划端自己的：`usage` 事件总带 `assistant_messages`，作业若把它列进 `usage_dimensions`，用量不完整时它也会被结算（与 pi 相同，合同第 4 条如此规定）；合同 Goal 写“清单见 Step 5”，实际在 Step 4。
- 执行器自述与证据是否一致：一致。七条自定解读复审都认可，其中第 3 条（`result` 之后的异常帧也清空用量）其实是合同第 5 条的要求。报告说 V08 有 15 个路径，实际 14 个，属笔误。用时 997 秒，上限 3000 秒的三分之一。决策模型（Jev，shadow）给“自行解读合同”0.86，与报告的 7 条解读相符；变异等价判断仍然全部答“不等价”，包括真等价的 C23。
  - 执行报告：json_ok（executor_status completed）
  - 合同矛盾：无
  - 自定解读（7，原文）：
    - {"quote": "第 2 条：models（排序后的元组）；第 3 条：把 model 记进 models", "adopted": "models 为去重排序后的模型名字符串元组（如 (\"claude-opus-5-5\", \"claude-sonnet-5\")）", "reason": "Claude 的 system/init 与 assistant 帧只有裸模型名，没有 pi 那样的 provider 字段；合同只写“排序后的元组”，未规定元素形状，字符串元组是最直接的读法。"}
    - {"quote": "第 3 条：正常事件的 event_id：L<行号>，usage 用 L<行号>.usage", "adopted": "assistant 事件 id 为 L<行号>（不带 .assistant 后缀），usage 事件为 L<行号>.usage", "reason": "合同明确列出两种形式，与 pi_events 的 L<行号>.assistant 不同；按合同字面执行，seq 仍从 0 连续、id 仍唯一。"}
    - {"quote": "第 3 条：异常帧“同时 unknown 加一”与第 5 条“usage 只在用量完整且没有异常帧时”", "adopted": "任何异常帧（包括出现在 result 之后的）都把 usage 清成 {}、usage_complete 置假", "reason": "合同“定稿时补的两处”与规划端变异清单（result 之后有异常帧时翻译结果仍带用量）都要求结果级同样清零。"}
    - {"quote": "第 3 条未给出除三个之外的原因文案（frame is not an object / frame after result / frame type outside the dialect）", "adopted": "其余原因沿用 pi_events 的措辞风格：type is not text、system subtype is not init、second init header、init header is not the first frame、session id or model is not text、claude_code_version is not text、message is not an assistant message、assistant content is not a list、assistant content block is malformed、assistant content block is outside the dialect、model or message id is not text、result subtype, is_error or num_turns is malformed", "reason": "合同只钉死三个 reason 文案与事件类型；这些是自由实现细节，保持与 pi_events 同风格便于对照。"}
    - {"quote": "第 3 条 system/assistant 的检查顺序以列表顺序给出", "adopted": "system：subtype≠init（UNKNOWN）→ 已见 init（INVALID）→ 不在第 1 行（INVALID）→ 字段类型（INVALID）；assistant：message/content 形状 → 块形状 → 块类型（UNKNOWN）→ model/id 类型", "reason": "合同按此顺序逐条列出；与 pi_events 的同名检查顺序一致，先失败关闭未知形状再查已知字段。"}
    - {"quote": "Step 4 列举的五组用例", "adopted": "另加一例 test_a_matching_invocation_settles_the_stream：Invocation(adapter_id=\"claude\") 经参考归约器结算出 SUCCEEDED 与五维用量", "reason": "五组之外补上 adapter 类的正常路径（oracle 已覆盖注册与身份拒绝），未照抄 oracle 用例，也未越出允许新增文件范围。"}
    - {"quote": "第 3 条：文本、字节串或不可迭代的输入抛 E_ADAPTER_EVENT", "adopted": "dict 输入不抛错（它可迭代，键被当作帧逐条失败关闭）", "reason": "合同只把 str/bytes/bytearray 与不可迭代列为拒绝；dict 可迭代，行为与 pi_events 一致，因此测试里只钉 42/None/文本/字节。"}
  - 限制与取舍：无
  - 未决问题：无
  - 事件流里的解读线索：无
- 摘要里的“复审需要关注”（原文）：
  - 新增被 Git 忽略的路径：`.pytest_cache/`
  - 新增被 Git 忽略的路径：`sentinelmesh/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/fixtures/claude_stream/__pycache__/`
  - 新增被 Git 忽略的路径：`tests/fixtures/pi_native/__pycache__/`

## 结论理由
合同第 1–8 条逐条满足，fresh 8/8；oracle 与执行器测试合起来抓住 22/24 个变异，漏掉的两条都等价；范围合规，`offline` 与 `pi` 不受影响。失败关闭端到端成立，对话文本不进事件。七条发现都是低级别，F1–F4、F6 由规划端补全：措辞、`num_turns` 上限、两例测试。结论 PASS。

## 后续动作
- 需要修正的具体差异（REVISE）：无（F1–F4、F6 由规划端补全）
- 待决的业务或环境问题（BLOCKED / ESCALATE）：无
- 已授权且可以继续的集成动作：local_branch、merge：`bash scripts/dispatch-pi.sh integrate .agent/runs/claude-adapter/attempt-001`（当前末端上验证后落地）
- 人工介入：0（用户没有介入）
