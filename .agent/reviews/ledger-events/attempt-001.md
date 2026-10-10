# 复审记录：ledger-events v1 · attempt-001

结论：PASS。候选逐条满足合同，fresh 检查 8/8，两轮变异 24/24 与 19/24，独立复审 PASS；一处低级别测试加固由规划端补全。

- 基线：`1981a36`（master `7ac6b2b` 设计说明 + oracle `1981a36`）；合同 `4fa8803`
- 执行：step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 用时 571.5 s（上限 1800 s）· Pi 退出码 0
- 候选树：`72f3a9c`（补丁往返校验通过）；变更 A1 M2 D0 T0，越界 0，警告 0
- 事件流：5728 行，完整（agent_settled），最终 stopReason=stop；执行报告 json_ok（completed）；工具错误 1（一次无匹配的 grep）；自动重试 0；压缩 0
- 用量：input 69621 / output 17456 / cacheRead 1525760

## 机械门禁

| 门 | 结果 | 依据 |
| --- | --- | --- |
| G0 执行完整性 | 通过 | 结束事件完整，报告 JSON 合法，`contract_conflicts` 与 `unresolved_questions` 为空 |
| G1 模型与思考级别 | 通过 | 实际 step/step-5-preview，请求 high（本机 Step 不回报 providerThinkingLevel） |
| G2 范围 | 通过（一处备注） | scope.json：两个模块修改，一个测试文件新增，越界 0；新增被忽略的 `.pytest_cache/` 与 `__pycache__/` |
| G3 独立检查 | 通过 | `check --fresh` 8/8（`checks/01`）：V01 oracle 11、V02 执行器 14、V03 账本/CLI/压缩/参考，V04 全量，V05–V08 |
| G4 变异 | 通过：24/24（oracle + 执行器测试），19/24（只用执行器测试） | `mutations/01`、`mutations/02` |
| G5 复审 | 通过 | 独立复审 PASS；规划端对照见下 |
| G6 候选固定 | 通过 | 补丁往返校验通过；fresh 检查在干净副本上做 |

G2 备注：自检的 pytest 命令全部接 `| tail -N`（V05–V08 用 `; echo "exit=$?"` 合规），是过程偏差，与前两轮相同；验收证据是 fresh 检查。没有临时路径探针、没有工作副本外路径。

## 规划端对照

Behavior contract：
1. `checkpoint_seq`：`__init__` 末尾默认参数；属性锁内先 `_require_available`；`compact()` 在外层 try/except 之后、`return` 之前赋值（失败路径先失效并抛出，不会到达）；`open_ledger` 传 `checkpoint[0]`。符合。
2. 常量与参数：`_EVENT_TYPES` 16 项；`_digits` 用 `re.fullmatch("[0-9]+")`，`_page_size` 1..10000；`ArgumentTypeError` 让 argparse 以 2 退出。符合。
3. 注册：`events` 子命令，`--type` `action="append"` + `choices`，`--after` 默认 0，`--limit` 默认 100。符合。
4. 流程：请求检查先于操作检查；归属按操作的 `request_id`（缓存）与 payload 的 `request_id`，`controller.fenced` 不属于任何请求；三过滤交集；`seq > after`、`[:limit]`；只读。符合。
5. 输出 9 键、事件原样、`next` 仅当还有匹配。符合。
6. `status` 仍是四个键（V03）；导入无副作用（V07）。符合。

DoD：D01–D06 全部满足（证据：V01–V08、scope、本对照）。

执行器测试 `tests/test_ledger_events.py`（14 个用例）对照 Step 3：访问器三种状态与关闭后的拒绝；请求级四种类型按 payload 归属（用紧预算让守卫发出 `request.stop_observed`）、操作事件按请求归属、`controller.fenced` 不出现；交集为空；每页一条翻完并拼回；`--after` 等于最后一条；参数错误各值（含 `" 5"`、`"5 "`、`"RUN.VERDICT"`）并断言 `usage:`、无 Traceback；默认页 100（101 条事件）；从不写（成功与拒绝之后摘要、命令表、目录不变）；子进程入口。每个拒绝先验证合法基线；错误码全等；无 skip；场景自建，不重复 oracle 的用例。

独立复审（子代理，只读探针）：PASS。
- F1（低）：`test_request_level_events_belong_by_their_payload` 只断言每个请求至少有一条请求级事件，没有把守“四种请求级类型都在场景里”；探针证实四种都在。裁定：规划端补全加一行断言。
- F2（低，过程）：pytest 自检带 `| tail`。记入反馈。
- 信息：唯一的工具错误是一次无匹配的 grep；测试用 `tempfile.mkdtemp` 加 `addCleanup` 与现有测试一致，不是探针；时间 571 s，其中四次重跑 `RequestAttributionCase` 是在调预算守卫的阈值；无合同歧义。

## 变异检查

- 清单：规划端草稿的 24 个偏差换成候选锚点（`scratchpad/make_mutations_events_a001.py`），每个锚点恰好出现一次：请求级事件不归属、操作事件按任务归属、忽略操作过滤、多个类型只取第一个、`--after` 含等于、页多一条、`next` 永远为空或最后一页也给、`matched` 数页、`total` 数匹配、未知请求或操作不拒绝、先查操作后查请求、默认 50、上限 10001、接受带符号、接受 0、不报检查点、压缩不更新访问器、打开不报检查点、降序、加派生字段、类型表少一个、命令改名。
- `mutations/01`（oracle + 执行器测试）：24 抓 24。
- `mutations/02`（只用执行器测试）：24 抓 19，漏 5：多个 `--type` 只取第一个、未知操作不拒绝、先查操作后查请求、输出不报检查点、类型表少一个。这五条规则合同 Step 3 刻意没有列给执行器（清单压短以免超时），都由 oracle 覆盖（`mutations/01` 抓住）；不算执行器少写，规划端也不另补。
- 草稿阶段：同一清单在规划端草稿上 23/24，漏掉的“接受 0”是靠空页上的 `IndexError` 碰巧以退出码 2 退出；oracle 改为要求 argparse 的 `usage:` 行之后抓住（`1981a36`）。

## 裁定与下一步

- attempt-001：PASS。
- 规划端补全（单独提交）：F1 的一行断言。
- 集成：oracle `1981a36` 与候选分支挑入 master，再补全提交；全量测试；标 INTEGRATED。

Verdict: PASS
