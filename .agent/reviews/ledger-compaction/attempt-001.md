# 复审记录：ledger-compaction v1 · attempt-001

结论：REVISE（class TIMEOUT）——候选完整，缺的只是执行报告；按 review-pi 技能“超时而候选已经完整”的规则，由本记录的逐项复审代替报告，直接集成返工提交，attempt 标 SUPERSEDED。

- 基线：`ef3e46c`（master `cac3014` 设计说明 + oracle `c9aa8aa`、`ef3e46c`）；合同 `a615523`
- 执行：step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 用时 1800.0 s（上限 1800 s，被调度器终止，退出码 3221225786）
- 候选树：`e538a41`（补丁往返校验通过）；变更 A1 M2 D0 T0，越界 0，警告 0
- 事件流：27119 行，没有结束事件，最终 stopReason=toolUse；执行报告缺失；工具错误 5（`gash` 工具名乱码 1、edit 的 oldText 不匹配 1、write 缺 content 1、edit 参数畸形 2，都自行恢复）；自动重试 0；压缩 0
- 用量：input 215149 / output 87560 / cacheRead 10662400

## 机械门禁

| 门 | 结果 | 依据 |
| --- | --- | --- |
| G0 执行完整性 | 不通过（TIMEOUT） | summary：无 agent_settled / agent_end；Pi 在 1800 s 被终止；执行报告缺失 |
| G1 模型与思考级别 | 通过 | 实际 step/step-5-preview，请求 high（本机 Step 不回报 providerThinkingLevel） |
| G2 范围 | 通过（三处备注） | scope.json：`durable_ledger.py`、`ledger_cli.py` 修改，`tests/test_ledger_compaction.py` 新增，越界 0；备注见下 |
| G3 独立检查 | 通过 | `check --fresh` 8/8：V01 oracle、V02 执行器测试、V03 账本/CLI/恢复/裁定全部测试、V04 全量、V05 compileall、V06 diff --check、V07 导入无副作用、V08 `ledger compact --help`（`checks/01`） |
| G4 变异 | 通过：46/46（oracle + 执行器测试），44/46（只用执行器测试） | `mutations/01`、`mutations/02` |
| G5 复审 | 见下 | 规划端逐项复审（代替执行报告）与独立复审子代理 |
| G6 候选固定 | 通过 | 补丁往返校验通过；fresh 检查在干净副本上做 |

G2 的备注（都不是越界）：
- 事件流第 20806、21217、23505 行三条 `python - <<'EOF'` 探针用 `tempfile.mkdtemp()` 建临时账本，做完没有删除目录（AGENTS.md 只允许测试框架管理的目录，或 mktemp 之后以 `rm -rf` 收尾）。探针只读写自己新建的目录，没有触碰工作副本之外的既有内容，记为过程偏差。
- 自检命令多处接 `| tail -n N`、`| head -n N`、`| sed -n` 管道（合同的 Validation commands 不允许用管道的退出码作证据），记为过程偏差；验收证据是调度器在干净副本上的 `check`。
- 工作副本里新增被忽略的 `.pytest_cache/` 与三处 `__pycache__/`，是测试产物。

## 超时原因

shell_commands 共 56 条：实现在前约三分之一完成，之后的时间几乎都花在执行器测试上——用 `python - <<'EOF'` 的字符串替换反复改 `tests/test_ledger_compaction.py`（约 20 次 edit、3 次 write 之外另有十余次脚本替换），每改一次就整文件重跑一次（执行器测试约 20 s，oracle 约 30 s，全量约 70 s），最后一轮又对一个用例“再跑一次”确认稳定，再用 `sed -n` 分段读源码准备写报告，时限就到了。合同 Step 3 列了约 50 个测试项、时间分配按 31 分钟写，对 1800 s 的上限没有余量，这是规划问题，不算执行错误。

## 规划端逐项复审（代替执行报告）

Behavior contract 逐条对照候选（`candidate.patch`，行号以候选工作树为准）：

1. 格式与表：`LEDGER_FORMAT = "sentinelmesh-ledger-v2"`；`_SCHEMA` 三张表；`_read_meta` 不变所以 v1 文件报 `E_LEDGER_FORMAT`；`_read_checkpoint` 缺表 → `E_LEDGER_FORMAT`。符合。
2. `checkpoint_sha256(seq, last_sha, state)`：`sha256(("%d\n%s\n" % (seq, last_sha)).encode("ascii") + state.encode("utf-8"))`；`__all__` 加入 `entry_sha256` 与 `checkpoint_sha256`。符合。
3. `_encode_state`：七个键、`dataclasses.asdict`、三元组数组、`_jsonable` 之后 `json.dumps(separators=(",", ":"), ensure_ascii=False, allow_nan=False)`，不排序键；编码失败 → `E_LEDGER_WRITE`。符合。
4. `_decode_state`：`_strict_loads(text, "E_LEDGER_CHECKPOINT")`；顶层键集合；`generation` 精确 int 且不小于 meta 代际，`_ReplayScheduler` 的 `ContractError` → `E_LEDGER_CHECKPOINT`；requests / tasks / operations / usage 的字段集合取自 `dataclasses.fields`，每个字段精确类型（`_exact_int` 排除 bool），`stopped`、`candidate_hash`、`amount`、`run` 可为 None；操作 id 形状与键一致、任务存在、状态属于十个之一；幂等条目指向自己的操作且一一对应；资源占用的 repo 与 key 都核对、不得重复；事件键集合、序号从 1 连续、各字段类型。还原时 `resource_keys` 成 tuple、键还原成元组、`run` 深拷贝、`_events` 原样。符合第 4 条的 8 小项。
5. `_read_checkpoint`：0/1 行、四列、`seq` 精确 int 且 ≥ 0、三列文本、链接规则（0 对空串，否则 64 位十六进制 `_LINK`）、摘要核对。符合。
6. `open_ledger`：meta → checkpoint → commands；总是先用 meta 代际构造 `_ReplayScheduler`（`E_LEDGER_FORMAT`）；有检查点时 `_decode_state` 替换并把每个操作登记进 `used`；`_verify_chain(rows, start_seq, previous)` 从 `start_seq + 1` 起；返回 `start_seq + len(commands)`，无尾部时 `last_sha` 即检查点链接；`seq` 文档字符串改写。符合。
7. `_head`：commands 最后一条，否则 checkpoint 的 `(seq, last_sha)`，否则 `(0, "")`；`_append` 改用它。符合。
8. `compact()`：锁内、`_require_available`、编码与摘要、`BEGIN IMMEDIATE`（`sqlite3.Error` → `E_LEDGER_WRITE`）、事务内头核对（`E_LEDGER_STALE`）、替换检查点行、`DELETE … WHERE seq <= ?` 的 `rowcount`、`COMMIT`，任何 `BaseException` 先 `_rollback`；失败分三路都 `_invalidate`；返回四个键；不进 `MUTATING_METHODS`。符合。
9. `cmd_compact` 与 `register`：七个键、`_with_ledger`、只有位置参数 `ledger`。符合。
10. 兼容性：V03 覆盖六个已有命令与账本全部现有测试；V07 导入无副作用。符合。

DoD：

| DoD | 结论 | 证据 |
| --- | --- | --- |
| D01 格式 v2、表、`checkpoint_sha256` | 满足 | V01、V02、V03 通过；复审第 1–2 条 |
| D02 状态编码与恢复的校验 | 满足 | V01、V02；复审第 3–4 条；变异 46/46 与 44/46 |
| D03 检查点行、打开流程、`seq` 含义 | 满足 | V01、V02、V03；复审第 5–6 条 |
| D04 日志头与 `compact()` | 满足 | V01、V02；复审第 7–8 条 |
| D05 命令行 | 满足 | V01、V02、V08；复审第 9 条 |
| D06 兼容性、导入无副作用、Step 3 测试齐全、不自做变异 | 满足 | V03、V04、V07；执行器测试 30 个用例 10 个类，逐项对照 Step 3 见下；事件流里没有改坏实现的命令 |
| D07 范围与依赖 | 满足 | V05、V06、scope.json |

执行器测试对照合同 Step 3（`tests/test_ledger_compaction.py`，872 行，30 个用例）：
- 表面：`CompactionSurface` 两例（格式、`MUTATING_METHODS`、签名、三张表与四列、meta；`checkpoint_sha256` 对固定输入与自己用 hashlib 算的值相等，并对三列逐一敏感）。
- 状态形状与顺序：`StateShape` 三例（七个键的顺序、请求按播种顺序且不是字母序、操作按领取顺序且用递减 id 证明不是排序、17/5 个字段、三元组、事件与运行描述原样、压缩前后与重开之后 `reservation`/`operation_usage`/`request_ids`/`operation_ids`/`stop_reasons`/快照键序不变）。
- `compact` 的返回值：`CompactReturns` 两例（四个键、`last_sha` 等于最后一行、重复压缩 `removed` 为 0 且摘要相同、追加 3 条后 `removed` 为 3、`seq` 为 0 的检查点）。
- 第 5 条：`CheckpointRefusals`（两行、摘要不符、状态被改、负序号、0 带链接、>0 空链接、链接非十六进制、状态为整数、状态为 blob）。
- 第 4 条：`StateRefusals` 七例（顶层不是对象、多键、少键、重复键、代际低于 meta（两种账本）、代际超范围、代际为 bool；四个 dataclass 各多一字段与少一字段；25 种类型错误的子用例；未知状态、键与 id 不一致、id 形状、未知任务；任务指向未知请求、重复、非三元组；幂等条目的六种；资源占用的六种；事件的六种）。
- 第 6 条：`TheTail`（尾部早一、晚一、不链接检查点、覆盖检查点已含的序号、中间断链；meta 代际 0 → `E_LEDGER_FORMAT`；尾部 claim 复用检查点里的操作 id → `E_LEDGER_REPLAY`，并带换成新 id 就合法的对照）。
- 第 7 条：`TheLogHead`（另一实例压缩后本实例照常追加且新行链接到检查点、另一实例追加后本实例 `E_LEDGER_STALE` 并失效、过期实例不得压缩且文件无检查点）。
- 第 8 条：`FailedCompactions`（关闭的实例；`INSERT INTO checkpoint`、`DELETE FROM commands`、`COMMIT` 三处注入 `OperationalError` → `E_LEDGER_WRITE`、失效、文件无变化；`_encode_state` 抛 `ValueError` 同上）。
- 第 9 条：`TheCommandLine`（七个键与值、`status` 前后逐字相同、重复压缩、`E_LEDGER_MISSING` 退出码 3、缺参数与多参数退出码 2、子进程 `python -m sentinelmesh.cli ledger compact`）。
- 另一个进程重开：`AnotherProcess` 一例。
- 每个拒绝用例都经 `refuses()`：先打开未损坏的副本，再只改一处，再比对文件字节；摘要用自己的 hashlib；错误码全等断言；没有 skip。与 oracle 对照：辅助函数的设计（`with_state`、`edit_field`、`rename_operation`、`append_rechained`、`refuses`）明显参照了 oracle，但场景、数据与断言都是自己的（三个请求、递减 id、18 步），不是抄写。

独立复审（子代理，只读，23 个内存探针，建议结论 PASS——评价候选本身）：
- F1（中，过程）：三条探针 `tempfile.mkdtemp()` 不清理（事件流第 20806、21217、23505 行；用途是查 `(row,), =` 解包与“拒绝打开不改文件”的 SQLite 变更计数器）。裁定：过程偏差，记入反馈；候选不受影响。
- F2（中，过程）：自检带 `| tail`、`| head`、`| sed -n`、`| grep` 管道（第 6545、6620、25450、25493 行等）。裁定：同上；验收证据是 fresh 检查。
- F3（低）：执行器测试与 oracle 有约 24% 的有效行逐字重合、82 个拒绝标签中 34 个相同、`test_a_stale_instance_may_not_compact` 逐行复制；但 48 个独有标签正是 Step 3 要求的空白，摘要用自己的 hashlib、直接改 SQLite，不是造假。裁定：接受；合同没有写明“可复用 oracle 辅助函数、不必重复 oracle 用例”，记为规划端措辞缺口。
- F4（低）：约 60 个拒绝用例靠“解码 → 改一处 → 重编码”，没有“只重编码仍能打开”的对照；探针证实往返逐字节相同。裁定：规划端补全时加一个对照。
- F5（低）：执行器测试没有一处把压缩后文件的摘要与自己算的比（只钉住了 `checkpoint_sha256` 对固定输入的值）；oracle 有直接断言。裁定：规划端补全时加两行。
- F6（低）：`assertNotEqual(list(recorded["operations"]), sorted(operations))` 原来有 1/6 概率因随机 id 恰好有序而失败，执行器自己撞上、最后一条命令用递减 id 修好，fresh 的 V02 在最终树上通过。裁定：已修；集成后把执行器测试连跑三次核实。
- F7（低）：填充断言 `assertEqual(len(operations), 3)`、`import itertools` 排在 `inspect` 前。裁定：补全时顺手改。
- F8–F11（外观）：模块文档字符串断行残留、`compact` 里 `if db is None` 的死分支（带 `pragma: no cover`）、`open_ledger` 两段重复注释、`_verify_chain` 参数带默认值。裁定：F8、F10 补全时改；F9、F11 保留（防御性检查无害；默认值与合同不矛盾）。
- 合同逐条核对（第 1–10 条、Step 3 逐项）全部满足；范围干净。
- 超时原因：进入最终自检时已用 1322 s；实现一次成功（第一次跑 oracle 即 14/14）；之后 12 轮“跑测试 → 改”的错因都是执行器自己的假设（场景调用数、`(row,), =` 解包、Scheduler 把 `resource_keys` 排序、预留必须含全部维度），约 42% 的时间在等 pytest（全量跑了两次、整文件反复跑）。合同侧：时间分配不现实；Repository facts 没写 `resource_keys` 排序与预留维度两个事实。

## 变异检查

- 清单：规划端草稿上的 46 个故意偏差换成候选的锚点（`scratchpad/make_mutations_compact_a001.py`），每个锚点在候选里恰好出现一次。覆盖：格式仍为 v1、摘要漏掉 `seq`、压缩不删命令或少删一条、压缩不核对日志头、日志头忽略检查点或优先取检查点、链只按每行自己的 `previous` 核对、尾部从 1 编号、打开后的 `seq` 只数尾部、不核对检查点摘要、接受两行检查点、检查点里的操作 id 不登记为已用、接受低于 meta 的代际、bool 当 int、多字段或少字段、任意状态、键与 `operation_id` 不一致、任意形状的操作 id、幂等条目不指向自己的操作、操作没有幂等键、资源占用不在 `resource_keys` 里、事件不从 1 编号、事件多键、状态多键、检查点宽松解析、编码时排序键、压缩不替换旧检查点、压缩失败不失效、`removed` 报 0、链接不合序号、负序号、建表少一张、数额任意类型、caps 与资源键不检查、任务指向未知请求、任务重复、三元组任意长度、事件/幂等键/`started`/运行描述不恢复、命令行少一个键或改名。
- `mutations/01`（oracle + 执行器测试，`pytest -x`）：46 抓 46，0 漏，0 跳过。
- `mutations/02`（只用执行器测试）：46 抓 44，漏 2：M37“资源键不检查类型”与 M38“任务可以指向未知请求”。两条规则执行器都写了用例，但被遮蔽——它改的是第一个操作的 `resource_keys`，而那个操作还占着资源，资源占用检查先拒绝；它改的任务 task-z 已被领取，操作自己的任务检查先拒绝。oracle 在 `mutations/01` 抓住了两者（用已结束的操作与没人领取的任务）。规划端补全时把两个用例改成不被遮蔽的输入（第三个操作已结束、task-m 无人领取），并在集成树上重跑这两个变异核实。
- 草稿阶段：同一清单在规划端草稿上第一轮 40/46，漏掉的 6 个都是 oracle 自己的毛病（`with_state` 把编辑函数的返回值当作新状态；伪造 claim 的用例被代际围栏而不是被操作 id 登记拒绝；bool 给 int 字段没有用例），修正 oracle（`ef3e46c`）后 46/46。

## 裁定与下一步

- attempt-001：REVISE，class TIMEOUT。候选完整、fresh 检查 8/8、逐项复审没有遗留，按 review-pi 技能的规则由本记录代替执行报告，`branch --rework` 取得返工提交后直接集成，attempt 标 SUPERSEDED（ref 为集成提交）。
- 规划端补全（单独提交，按同样标准检查）：F4 的重编码对照、F5 的两处摘要直算断言、F7 的导入顺序与填充断言、F8 的文档字符串断行、F10 的重复注释；集成后执行器测试连跑三次核实 F6。
- 规划教训：合同 Step 3 的测试清单约 50 项，对 1800 s 的上限没有余量；高风险合同的时限要按测试项数给余量，或把测试清单压到执行器能在时限内写完并写报告的规模。

Verdict: REVISE
