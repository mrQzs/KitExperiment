# REVIEW：pi-native-events v1 · attempt-001

Verdict: ACCEPTED_WITH_EXCEPTION

## 基本信息
- 基线：`396e0a9f8d4ff5b227a9c7a41567b9ee31100e97`（master `7dda4ef` + 规划端 oracle）
- 候选树：`d1bd4febcbd2c6562960c90a99fe519efe615688`
- 要求的执行模型：step/step-5-preview · thinking high
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报（只能确认请求的级别）
- 运行：退出码 0 · 912.4 s / 1800 s · 未超时、未取消 · pi 0.99.2（Windows Server 2025）
- 费用：unknown（Pi 没有目录价格）；用量 input 149439 / output 88765 / cacheRead 2279424
- 复审者：规划/验收会话（Claude Code，claude-fable-5-1）；独立复审子代理（只读）参与，见“独立复审”

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 时一致；基线提交存在 |
| G1 执行完整 | passed | 事件流完整，以 `agent_settled` 结束（29036 行）；最终 stopReason=stop；工具错误 0、自动重试 0、压缩 0；stderr 无标记；执行报告 json_ok |
| G2 范围合规 | passed | A2 M0 D0 T0，越界 0，警告 0；新增的被忽略路径只有 `__pycache__` 与 `.pytest_cache`；没有临时路径、没有工作副本外路径、没有可疑命令 |
| G3 行为正确 | passed | check --fresh（checks/01）V01–V07 全部通过；D01–D08 逐项见下表 |
| G4 回归检查 | passed | 检查记录：checks/01（fresh）；V04 全量 599 passed / 288 skipped（基线 551 + oracle 26 + 执行器 22；captured 2 例在干净副本上跳过，预期） |
| G5 质量复审 | failed（豁免） | 执行器为合同 Step 3 点名的“传给父类的是拷贝”写的用例是空断言，变异 M33 存活；实现正确，由规划端补全，见“独立复审”F1 与“结论理由” |
| G6 可集成 | passed | 候选补丁往返校验通过；只新增两个允许的文件 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V01、V02 | 常量、`__all__`、`translate` 的入口规则（str/bytes/bytearray 与不可迭代报 `E_ADAPTER_EVENT`，生成器只消费一次）、`PiTranslation` 十四个字段按合同顺序（执行器按合同取 `dialect` 在先，设计说明 3.1 的顺序已更正） |
| D02 | passed | V01、V02 | 映射表与忽略计数；配对、`run_started` 位置、终止唯一性留给参考归约器（oracle `Reducer` 类证实） |
| D03 | passed | V01、V02 | 只累加合法 assistant `message_end`；七种非法用量值整条流不完整、usage 事件无 token 键、`PiTranslation.usage` 仍给合法消息之和 |
| D04 | passed | V01、V02 | 只有 `agent_settled` 终止；非 `stop` 记 `error` 并带 `reason`；终止后的帧是未知帧；无终止的流不结算 |
| D05 | passed | V01、V02 | 未知与畸形帧 payload 恰好五键、不含原帧、sha256/bytes 取合同第 8 条的编码；reason 文字逐字一致；检查顺序一致 |
| D06 | passed | V01、V02 | `PiNativeAdapter` 的类属性、`probe`、`run` 的两个前置检查、父类结算、深拷贝；三份完整夹具 SUCCEEDED 且用量等于调度器，两份截断 UNKNOWN |
| D07 | passed（有例外） | V03、V04、V07、review | 导入不触发 ctypes、不读文件、不起进程；参考归约器与接线测试不退化；Step 3 的用例都写了，但“传给父类的是拷贝”一条是空断言（独立复审 F1），由规划端补全；执行器没有自做变异 |
| D08 | passed | V05、V06、scope | 只新增 `sentinelmesh/pi_events.py` 与 `tests/test_pi_events.py`；无依赖变更；HEAD 未动 |

## 范围与质量
- 越界与警告：无。summary 的“复审需要关注”只有被忽略的缓存目录，以及执行器报告的 1 处合同矛盾（见下）。
- 合同矛盾裁定：执行器指出设计说明 3.1 把 `events` 列在 `dialect` 前，而合同第 2 条把 `dialect` 列在前；它按合同实现。裁定：以合同为准，是规划端把设计说明写成了描述性列表；设计说明已更正。另一处 limitations（设计说明 3.6 写 96/80，夹具 meta 是 64/48）是基线里的设计说明落后于 oracle 提交，master `7c7ab57` 已更正；不影响行为。
- 测试可信度：执行器测试 22 个用例（36 个子测试），每个拒绝用例先验证合法基线再只改一处，断言恰好一个畸形事件的 `line`、`frame_type`、`reason` 与五键 payload；三对检查顺序用例；`ignored` 逐项计数；事件 id/seq；未知帧摘要（非 ASCII 与嵌套）；`usage_complete` 转假后保持为假、合法消息仍累计；生成器耗尽；frozen dataclass；`models`/`agent_cycles`/`tool_errors`/`last_stop_reason`；`probe`、`run` 的身份检查与拷贝。没有 skip、没有弱化断言、没有读取 `.agent/runs`。
  - 小缺口：`tool_execution_end` 的 `toolCallId`/`toolName` 形状只在 oracle 里测（执行器只测了 `isError`）；“交给父类的是拷贝”的用例拿父类收到的事件与另一次 `translate()` 的结果比较身份，永远不相同，看不出共享（见变异 M33）。两处都不影响行为。
- 变异检查（中高风险）：
  - mutations/01（oracle + 执行器测试，36 个）：抓住 35 / 漏掉 1 / 跳过 0。每个抓住的都是断言失败，没有收集或导入错误。
    - `M33 the reducer is handed the translator's own events` → 归因：等价变异（父类 `OfflineAdapter.run` 不改动输入事件，`run` 也不返回翻译结果，拷贝与否不可观察）→ 处理：注明。
  - mutations/02（只用执行器测试，同一清单）：抓住 26 / 漏掉 10 / 跳过 0。漏掉的 M01（快照用量）、M05–M08（bool/float/负数/不安全整数）、M10（非 stop 也 run_finished）、M12（session 不在第 1 行）、M13（终止后的帧）、M32（终止 reason）按合同 Step 3 的分工由 oracle 把守，M33 同上；执行器没有少写合同点名的用例。
- 规划端另做的复核（本机，不在合同检查内）：`tests/test_pi_events_captured.py` 对 `.agent/runs` 里 31 次完整流（30 次历史运行加 a001 自己这次 pi 0.99.2 的流）复核，候选的翻译与调度器的 `events-summary.json` 全部一致，5 份夹具与各自的完整流翻译相同；候选与规划端草稿在这 31 条流上事件逐项相同。唯一差异在合成输入上：畸形的首个 `session` 之后再来一个 `session`，候选报 `second session header`，草稿报“不在第 1 行”，两者都是畸形帧，设计说明 N10 已按候选的写法记录。
- 兼容性与残余风险：不改任何已有模块；`OfflineAdapter` 行为不变（oracle 证实）。残余：未见过的帧类型（compaction、retry、queue、`entry_appended`）按未知处理，新版 pi 出现它们时请求会停在用量未知，需要新方言版本；payload 中 `response_id`/`thinking_level` 若含控制字符会被归约器的 payload 检查拒绝（失败关闭，真实数据没有）。
- 执行器自述与证据是否一致：一致。自述的七项检查结果与 fresh 检查相同；`contract_conflicts` 与 limitations 如实写出。

## 独立复审
只读子代理按 independent-review.md 复审，建议 REVISE（只限测试文件），末行 `Verdict: REVISE`。它的发现与主审的裁定：

- F1（中）`tests/test_pi_events.py:448-453`：“`run` 传给父类的是拷贝”的用例拿父类收到的事件与另一次 `translate()` 的结果比身份，必然不同，是空断言；变异 M33（深拷贝改成 `list(translation.events)`）因此存活。实现本身正确（`pi_events.py:406` 深拷贝；子代理的内存探针确认父类收到的事件与内部翻译不是同一对象）。裁定：执行器写了合同 Step 3 点名的用例但断言无效，属“写了但断言太弱”；实现正确、修正只有十来行、再派一轮 Pi 不划算，由规划端在集成后的补全提交里改成用 `mock.patch` 截获 `run` 内部那次翻译再比较身份并篡改验证，重跑 M33 确认抓住。这是本次豁免 G5 的唯一理由。
- F2（低）`pi_events.py:152-161`：终止之后的非 dict 帧或 `type` 非 str 的帧，候选先做 dict/type 检查（报 `pi.invalid_frame`），合同第 7 条“之后的任何帧都是未知帧”与第 9 条说法不一；执行器在事件流第 27093 行自己裁定了，报告没写进 `unresolved_questions`。两种读法都失败关闭。裁定：候选的读法成立，设计说明补记为 N15；执行器漏报裁定记入反馈。
- F3（低）`pi_events.py:183-189`：`session_seen` 在形状检查之前置真，畸形首头之后的第二个头报 `second session header` 而不是“不在第 1 行”。合同只写“此前没有 session”，候选的读法成立；设计说明 N10 已按候选记录。
- F4（低）执行器测试的弱点：reason 用前缀匹配（`assistant content` 同时匹配两种 reason；oracle 同样）；`SessionHeader._assert_one` 比 `_one_invalid` 弱；`tool_execution_end` 的 `toolCallId`/`toolName` 畸形只在 oracle 里；块 `{"type": 5}` 两份测试都没有。其余 Step 3 条目逐项都有用例且不是抄 oracle。裁定：随 F1 一起由规划端补全（执行器测试与 oracle 都改成 reason 全等，并补三个形状用例）。
- F5（信息）payload 里逐字复制的字符串（`type`、`toolCallId` 等）没有上限：1.1 MB 的 `type` 让 `translate` 不抛（合同第 2 条），`run` 则由父类的 payload 检查报 `E_ADAPTER_EVENT` 而不是 UNKNOWN。仍是失败关闭，第 10 条允许父类异常冒出；经 `read_stream` 的真实流单行不超过 1 MiB，到不了这里。裁定：设计说明 N4 的“永远有界”改为“在 Pi 实际的字符串长度下有界，超长时由归约器拒绝”。
- F6（信息）`_frame_digest` 的二次退路：`repr` 也失败时 `bytes=0`、sha256 为空串摘要，合同没有定义。裁定：不抛即可，设计说明补记。
- F7（信息）设计说明 3.1 字段顺序与 3.6 缩减阈值已过时：前者本次更正，后者 master `7c7ab57` 已更正。
- F8（信息）范围与过程干净：scope 无越界无警告；blob 哈希与补丁一致；V04 计数可对账（599 = 551 + 26 + 22）；插入式变异 M04/M29 看过日志，都是真实行为改变。
- 子代理无法判断的一项（`test_pi_events_captured.py` 没在任何地方跑过）：主审已在本机对 31 次完整流跑过，全部一致（见上）。

## 结论理由
G0–G4、G6 通过；候选按合同第 1–12 条逐条实现，oracle 与执行器测试在干净副本上通过，变异检查 35/36，规划端在 31 次真实流上的复核与调度器一致。G5 有一处执行器偏差：合同 Step 3 点名的“`run` 传给父类的是事件的拷贝”用例写成了空断言（独立复审 F1，变异 M33 存活）。实现本身正确，修正只有十来行、只改测试，再派一轮 Pi 不划算；按 review-pi 对“写了但断言太弱”的处理，由规划端在集成后的补全提交里补齐并重跑 M33。豁免：G5，理由如上，决定者：规划端（用户 2026-10-08 的常设授权：跑完直接验收集成），适用范围只限本 attempt。结论 ACCEPTED_WITH_EXCEPTION，不记为 PASS。

## 后续动作
- 需要修正的具体差异（REVISE）：无。
- 待决的业务或环境问题：无。
- 已授权且可以继续的集成动作：local_branch；之后在 master 上依次 cherry-pick oracle `396e0a9` 与候选提交，规划端补全（F1 的拷贝用例、F4 的 reason 全等与三个形状用例、F2/F3 的 oracle 用例），在干净工作树上跑全量，更新 IMPLEMENTATION_STATUS 与设计说明，mark INTEGRATED。
- 人工介入：约 45 分钟（读候选、两轮变异清单、本机完整流复核、独立复审整理与裁定）。
