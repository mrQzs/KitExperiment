# REVIEW：ledger-reference v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`7cdb039c386c418982df2a2e19d5e67e992cb0c3`（master `fb53e9a` 设计说明 + 规划端 oracle）
- 候选树：`05a799672defbd738edebcdc5536c798fffa8c8c`
- 要求的执行模型：step/step-5-preview · thinking high
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报（只能确认请求的级别）
- 运行：退出码 0 · 736.6 s / 1800 s · 未超时、未取消 · pi 0.99.2（Windows Server 2025）
- 费用：unknown（Pi 没有目录价格）；用量 input 161668 / output 46630 / cacheRead 2727424
- 复审者：规划/验收会话（Claude Code，claude-fable-5-1）；独立复审子代理（只读）参与，见“独立复审”

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 时一致；基线提交存在 |
| G1 执行完整 | passed | 事件流完整，以 `agent_settled` 结束（14715 行）；最终 stopReason=stop；工具错误 2（edit 工具的参数校验与文本匹配失败，无害）、自动重试 0、压缩 0；stderr 无标记；执行报告 json_ok |
| G2 范围合规 | passed | A1 M2 D0 T0，越界 0，警告 0；新增的被忽略路径只有 `__pycache__` 与 `.pytest_cache`；没有临时路径、工作副本外路径、可疑命令；“新增跳过标记”是合同要求的类级 `skipIf(hasattr(os, "O_NOFOLLOW"))` |
| G3 行为正确 | passed | check --fresh（checks/01）V01–V08 全部通过；D01–D07 逐项见下表 |
| G4 回归检查 | passed | 检查记录：checks/01（fresh）；V04 全量 684 passed / 289 skipped（基线 603 + oracle 28 + 执行器 53；captured 3 例在干净副本上跳过，预期） |
| G5 质量复审 | passed | 两轮变异各 32/34，漏掉的两个是等价或被遮蔽的变异（草稿阶段已知）；独立复审建议 PASS，唯一的低级别发现是合同措辞问题，裁定见下 |
| G6 可集成 | passed | 候选补丁往返校验通过；只改两个允许的模块、新增一个允许的测试文件 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | V01、V02、V08 | `reference` 注册在 `ledger` 组下；`--operation`、`--dialect` 必填，`--dialect` 用 `choices=(DIALECT,)`；五类参数错误退出码 2、stdout 为空、账本不动 |
| D02 | passed | V01、V02 | `operation` → `run_descriptor`（None 报 `ContractError("E_NOT_SUPERVISED", …)`）→ `operation_usage` → `read_stream(evidence_dir, max_bytes=MAX_STREAM_BYTES)`；任何状态都接受；无变更方法调用；流不 `ok` 退出码 0 |
| D03 | passed | V01、V02 | 恰好 13 键；`missing` 逐维复用 `_usage_incomplete`、按预留顺序；`stream` 摘要不放 `events`，`invalid` 时带 `line`/`code` |
| D04 | passed | V01、V02 | 参考值只用 `translate` 的结果；`reasons` 固定顺序；`complete = not reasons`；不完整时仍给五维累计；`models` 是列表的列表；五份夹具与调度器汇总逐项相符 |
| D05 | passed | V01、V02、V03 | 非 POSIX 分支：`lstat` 先行、非普通文件或重解析点不打开、`O_RDONLY | O_BINARY` 打开、`fstat` 身份比对；POSIX 分支的 `os.open(..., O_NOFOLLOW | O_NONBLOCK)` 逐字不变（独立复审 diff 核对），签名、返回形状、常量不变 |
| D06 | passed | V03、V04、V07、review | 五个已有命令不变；导入不触发 ctypes；Step 3 的用例齐全（53 个）；执行器没有自做变异 |
| D07 | passed | V05、V06、scope | 只改 `sentinelmesh/ledger_cli.py`、`sentinelmesh/adapter_run.py`，新增 `tests/test_ledger_reference.py`；无依赖变更；HEAD 未动 |

## 范围与质量
- 越界与警告：无。summary 的“复审需要关注”只有被忽略的缓存目录与合同要求的跳过标记。
- 合同矛盾：执行报告 `contract_conflicts` 与 `unresolved_questions` 为空；`ambiguity_mentions` 为空。limitations 一条：合同 Repository facts 写“执行器的工作副本里没有它”，执行器读成测试文件不在副本里（实际是 `.agent/runs` 不在）；是规划端措辞不清，执行器处理合理（记入 limitations、未停止）。
- 测试可信度：53 个用例（5 个子测试），每个拒绝用例先验合法基线再只改一处；13 键逐项与账本视图、运行描述、`operation_usage` 对照；`missing` 四种形态与非字母序；三种原因单独、两两、三者同现；`frames`≠事件数；流的每种状态；参数、拒绝、从不写（摘要 + seq + events + 证据目录列表）、账本必关（留住实例）、相对证据目录（chdir 两处）、非 POSIX 分支 8 例（含 mock `lstat` 三例）、子进程入口。约 15 个用例与 oracle 近乎同文（辅助函数、ClosedCase、EvidenceDirCase 等），是合同 Step 3 点名的内容，不算造假；其余用例是独立新增且有效的（独立复审 F3 的内存探针证实）。没有 skip（除合同要求的那一个类）、没有弱化断言、没有读取 `.agent/runs`。
  - 小缺口（独立复审 F4）：`test_an_amount_of_none_is_missing`、`test_an_amount_that_is_not_final_is_missing` 用 `assertIn` 而不是完整有序列表；三原因同现只断言 `reasons`。顺序与 `complete` 由其他用例把守（变异 M01/M02/M08 两轮都抓住），由规划端在补全里收紧。
- 变异检查（中风险）：
  - mutations/01（oracle + 执行器测试，34 个）：抓住 32 / 漏掉 2 / 跳过 0。抓住的都是断言失败（退出码 1），没有收集或导入错误；M05、M13、M22 被工具标为“只插入代码”，三者都改变了行为（条件清空用量、调用 `settle` 写账本、把 `agent_end` 当终止），日志里是预期用例的断言失败。
    - `M07 an unknown amount counts as known` → 归因：等价变异（`observe` 对 `final=True` 且 `amount=None` 报 `E_UNKNOWN_FINAL`，数额为 None 的记录永远不是最终值，“`final` 不是 True”那一条已经覆盖）→ 处理：注明，草稿阶段已知。
    - `M31 lstat regular-file check dropped` → 归因：被遮蔽的守卫（符号链接由重解析点属性拒绝，目录由 `os.open` 的 `PermissionError` 与 `fstat` 的普通文件检查拒绝；Windows 上没有既通过 `lstat` 普通文件检查又能打开成功的非普通文件）→ 处理：注明，草稿阶段已知。
  - mutations/02（只用执行器测试，同一清单）：抓住 32 / 漏掉 2 / 跳过 0，漏掉的同上。执行器测试单独就把守了合同的每条规则。
- 规划端另做的复核（本机，不在合同检查内）：`tests/test_pi_events_captured.py` 的 `CapturedReference` 对本机最大的完整流（pi-native-events a001，11.0 MB）经命令读取，五个维度、帧数、消息数、工具调用与错误数都与调度器的 `events-summary.json` 一致（规划端草稿上验证；集成后在候选上重跑）。
- 兼容性与残余风险：POSIX 分支逐字不变，但本机跳过了全部原生 POSIX 用例，运行时证据要等回到 macOS 跑一次 V03/V04；`read_stream` 对含 NUL 的路径仍会抛 `ValueError`（基线已有的行为，账本里的证据目录不可能含 NUL）；16 MiB 上限对接近 1800 秒的运行可能不够（设计说明第 3.5 节的遗留）。
- 执行器自述与证据是否一致：一致。自述的八项检查结果与 fresh 检查相同。过程偏差一处：执行器自检时把 `pytest … 2>&1 | tail -n N` 这类管道当作检查结果（合同明文不允许用管道的退出码作证据）；验收证据是调度器在固定候选上的 checks/01，不受影响，记入反馈。

## 独立复审
只读子代理按 independent-review.md 复审，建议 PASS，末行 `Verdict: PASS`。它的发现与主审的裁定：

- F1（低）`adapter_run.py:213-232`：非 POSIX 分支里 `os.open` 没有自己的异常处理，打开阶段的 `FileNotFoundError` 落入外层处理器报 `missing`，合同 7.3 写的是“`OSError` → `unreadable`”；只在 `lstat` 与 `open` 之间文件被删的竞态下可达，对命令只影响 `stream.status` 的用词（参考值都是 None，退出码都是 0）。裁定：候选的读法成立——规划端草稿正是同一结构，合同 7.3 把 `FileNotFoundError` 混进了“其他 `OSError`”；文件在两次调用之间消失，`missing` 就是此刻的事实，与 POSIX 分支（`os.open` 的 ENOENT 报 `missing`）一致。记为规划端决定 R10，由规划端在 oracle 里用 mock `os.open` 钉住两种映射（`FileNotFoundError` → `missing`、`PermissionError` → `unreadable`）。不豁免、不返工。
- F2（信息）POSIX 分支逐字不变的核对：`os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)` 一字不差（只是缩进进了 `if _HAS_NOFOLLOW:`，注释重新折行）；`fstat` 之后的共享尾段逐行相同，只插入了 `pinned is not None` 守护的身份比对；`__all__`、常量不变。无需修正。
- F3（信息）执行器测试与 oracle 的重合约 15 例；新增且有效的用例经内存探针验证（去掉身份比对、重解析点判断、文本模式打开、写账本、写证据目录、不关账本、原因倒序、用 `usage_complete` 判 illegal、只看终止判完整各有用例失败）。与两轮 `mutate` 的结果一致。
- F4（信息）两处 `assertIn` 与三原因同现只断言 `reasons`。裁定：随补全收紧。
- F5（信息）过程：执行器自检命令用了 `| tail`/`| head` 管道；`suspicious_commands`、`temp_path_uses`、`outside_worktree_paths` 为空；主仓库前后树一致。裁定：记入反馈，不影响证据。
- F6（信息）合同 Repository facts 的措辞（见“合同矛盾”）。裁定：规划端措辞问题，记入反馈。
- 子代理无法判断的项：POSIX 分支的运行时行为（本机跳过）——留到 macOS；正式 `mutate` 记录——已补做两轮（mutations/01、02）；F1 的真实竞态无法确定性复现——用 mock 钉住映射。

## 结论理由
G0–G6 全部通过：候选按合同第 1–8 条逐条实现，oracle 与执行器测试在干净副本上通过，两轮变异各抓住 32/34（漏掉的两个是草稿阶段就已知的等价与被遮蔽变异），独立复审建议 PASS。独立复审的 F1 是合同措辞与规划端草稿不一致，裁定以候选为准并写进设计说明；F4 是可选加强。结论 PASS；规划端在集成后的补全提交里钉住 R10 的两种映射并收紧 F4 的三处断言。

## 后续动作
- 需要修正的具体差异（REVISE）：无。
- 待决的业务或环境问题：无。
- 已授权且可以继续的集成动作：local_branch；之后在 master 上依次 cherry-pick oracle `7cdb039` 与候选提交，规划端补全（R10 的两个 oracle 用例、F4 的三处断言），在干净工作树上跑全量，更新 IMPLEMENTATION_STATUS 与设计说明，mark INTEGRATED。
- 人工介入：约 35 分钟（读候选与 53 个用例、两轮变异清单、独立复审整理与裁定）。
