# 复审记录：stream-ceilings v1 · attempt-001

结论：PASS。候选逐条满足合同，fresh 检查 8/8，两轮变异 28/28 与 15/28（漏掉的 13 条全是合同刻意留给 oracle 的规则），独立复审 PASS；两处低级别整洁问题由规划端补全。

- 基线：`35e7f05`（master `66ef144` 设计说明 + oracle `35e7f05`）；合同 `54a481a`
- 执行：step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 用时 467.2 s（上限 1800 s）· Pi 退出码 0
- 候选树：`dc753e6`（补丁往返校验通过）；变更 A1 M3 D0 T0，越界 0，警告 0
- 事件流：3911 行，完整（agent_settled），最终 stopReason=stop；执行报告 json_ok（completed）；工具错误 0；自动重试 0；压缩 0
- 用量：input 52653 / output 12986 / cacheRead 903424

## 机械门禁

| 门 | 结果 | 依据 |
| --- | --- | --- |
| G0 执行完整性 | 通过 | 结束事件完整，报告 JSON 合法，`contract_conflicts`、`unresolved_questions`、`limitations` 为空 |
| G1 模型与思考级别 | 通过 | 实际 step/step-5-preview，请求 high（本机 Step 不回报 providerThinkingLevel） |
| G2 范围 | 通过（一处备注） | scope.json：两个模块修改、一行测试修改、一个测试文件新增，越界 0；新增被忽略的 `.pytest_cache/` 与 `__pycache__/` |
| G3 独立检查 | 通过 | `check --fresh` 8/8（`checks/01`）：V01 oracle 17、V02 执行器 9、V03 邻近 11 个文件、V04 全量 781 passed / 289 skipped、V05–V08 |
| G4 变异 | 通过：28/28（oracle + 执行器测试），15/28（只用执行器测试，见下） | `mutations/01`、`mutations/02` |
| G5 复审 | 通过 | 独立复审 PASS；规划端对照见下 |
| G6 候选固定 | 通过 | 补丁往返校验通过；fresh 检查在干净副本上做 |

G2 备注：自检全部用合同规定的 `T=$(mktemp -d); … ; echo "exit=$?"; tail …; rm -rf "$T"` 形式（kit 的启发式扫描把每条都标为“可疑命令”与“临时路径”，是规定写法的误报）；一处在 `rm -rf "$T"` 之后接了只读的 `git status --porcelain`，两处把 V05–V08 串在一个 `$T` 里各自回显退出码，都是过程偏差；验收证据是 fresh 检查。没有工作副本外路径。

## 规划端对照

Behavior contract：
1. 常量：`MAX_STREAM_BYTES = 64 * 1_048_576`；`DEFAULT_MAX_LINE_BYTES = DEFAULT_MAX_JSON_BYTES`（从 `.core` 导入）；`MAX_LINE_BYTES = 8 * 1_048_576`；`DEFAULT_MAX_STREAM_BYTES` 不变；`__all__` 加两个名字、原有保留；三个文件里没有 16 MiB 字面量。符合。
2. `read_stream` 签名顺序 `evidence_dir, *, max_bytes, expected, max_line_bytes`；`_check_max_bytes` → `_check_max_line_bytes` → `_checked_expected`，之后才拼路径；校验精确 int、含两端、`E_ADAPTER_SCHEMA`、消息含 `max_line_bytes`。符合。
3. `_parse_stream(raw, max_line_bytes)` 每段 `strict_json_loads(segment, max_bytes=max_line_bytes)`，不传深度；`invalid` 与其他答案不变。符合。
4. `run_adapter_operation` 新增仅关键字 `max_line_bytes` 于 `max_stream_bytes` 之后；在 `_check_max_bytes` 之后、`supervised_spec` 之前校验；`_reduce` 多了关键字参数 `max_line_bytes`（私有签名，允许），两处调用都传它，`_reduce` 再传给 `read_stream`。符合（Windows 上没有测试到这两行，由独立复审逐行核对并用 stub 探针证实）。
5. `cmd_reference` 传 `max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES`；输出不变。符合。
6. `tests/test_adapter_run.py` 只改第 262 行。符合（单行 diff）。
7. 其他公开签名不变；导入无副作用（V07）。符合。

DoD：D01–D06 全部满足（证据：V01–V08、scope、本对照）。

执行器测试 `tests/test_stream_ceilings.py`（9 个用例）对照 Step 4：一行恰好 1500 字节读出、1499 时 `invalid` 第 3 行、几行里第一条超长的行报自己的行号；`max_line_bytes` 大于 `max_bytes` 合法而整流上限仍由 `max_bytes` 决定；拒绝表（0、-1、True、1.5、"8"、8 MiB + 1）在不存在的证据目录上拒绝、合法值得到 `missing`、`max_stream_bytes` 先报；`run_adapter_operation` 以 `max_line_bytes=0` 在移交前拒绝（精确 `Invocation`、`OfflineAdapter()`、事件不变、RESERVED、证据目录为空），同时给坏的 `max_stream_bytes` 时报它；`reference` 读 1 MiB + 1 的一行并给出用量，截断到 64 MiB + 1 后 `too_large`、`reference` 为 None。每个拒绝先验证合法基线；错误码全等；无 skip；与 oracle 的辅助函数同构（合同允许）。

独立复审（子代理，只读探针）：PASS。
- F1（低）：执行器测试与 oracle 高度同构（Step 4 的五项本来就与 oracle 重叠，合同允许沿用写法）；五项齐全、无弱断言。记录在案。
- F2（低，过程）：一处 `rm -rf "$T"` 之后接 `git status --porcelain`，两处把四条检查串在一个临时目录里。记入反馈。
- F3（低，整洁）：`tests/test_stream_ceilings.py` 的 `MIB` 定义后未用；`adapter_run.py` 的 `run_adapter_operation` 文档字符串重排后一行约 100 列。裁定：规划端补全。
- 信息：两处 `_reduce` 传参用 stub 探针（stub 掉 `run_supervised_operation` 与 `_reduce`，不起进程）证实 `before_finish` 路径与无评审路径都收到传入的 `max_line_bytes`，不传时为 1 MiB；探针脚本 `scratchpad/probe_reduce_callsites.py`。无合同歧义；时间去向合理。

## 变异检查

- 清单：规划端草稿的 28 个偏差换成候选锚点（`scratchpad/make_mutations_ceilings_a001.py`），每个锚点恰好出现一次：整流上限 32/128 MiB、单行上限 4/16 MiB、两个默认值改成最大值、接受 0、接受 bool、拒绝恰好 8 MiB、错误码或消息改掉、`read_stream` 不校验或顺序颠倒（在 `max_bytes` 之前、在 `expected` 之后）、解析器忽略参数或永远用最大值、上限变成开区间、放宽深度、两个签名的默认值改成最大值、`run_adapter_operation` 不校验或顺序错（在 `max_stream_bytes` 之前、规格之后、预约之后）、`__all__` 少一个、`reference` 不传行上限或仍用 16 MiB、测试里的旧字面量回来。
- 没有测试把守的行为：两处 `_reduce` 的 `max_line_bytes` 传参（Windows 上没有监督器，oracle 与执行器测试都到不了那两行）；由独立复审逐行核对与 stub 探针证实，不列变异。
- `mutations/01`（oracle + 执行器测试）：28 抓 28。
- `mutations/02`（只用执行器测试）：28 抓 15。漏掉的 13 条全是合同 Step 4 没有列给执行器的规则，留给 oracle：四个常量的值（M01–M04）、`read_stream` 的校验在 `expected` 之前（M14）、深度不放宽（M18）、两个签名的默认值（M19、M20）、`run_adapter_operation` 的校验在规格与预约之前（M23、M24）、`__all__`（M25）、`reference` 的整流上限是 64 MiB 而不只是“比小流大、比 64 MiB + 1 小”（M27）、`tests/test_adapter_run.py` 的旧字面量（M28，执行器测试不可能把守自己改的那一行）。不算执行器少写。
- 草稿阶段：同一清单在规划端草稿上 28/28。

## 裁定与下一步

- attempt-001：PASS。
- 规划端补全（单独提交）：F3 的两处整洁。
- 集成：oracle `35e7f05` 与候选分支挑入 master，再补全提交；全量测试；标 INTEGRATED。

Verdict: PASS
