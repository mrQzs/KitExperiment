# REVIEW：runner-posix-cleanup v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`6b0de1435c4aaafa2b023f8e89101ca004ed649d`（master `cbb4bc4` + 规划端 oracle）
- 候选树：`5a27bc2d3a70fecef71f0c604bc74fc5ab88bdc0`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 610.89 s / 1500 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论见“独立复审”

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、实际模型一致；1 次工具错误是事件第 7912 行一条只读查询以退出码 1 结束，不影响结果 |
| G2 范围合规 | passed | A1/M1，violations 0。临时文件全部是同一条命令内 `T=$(mktemp -d)` … `rm -rf "$T"`，没有固定 `/tmp` 路径，KitExperiment AGENTS.md 新加的写法生效。nit：事件第 7364、7497、8724、8819 行在 `rm -rf "$T"` 之后还跟了 `date`、`git status`、`ps` 这类只读命令，与“以 rm -rf 作为最后一步”的字面要求略有出入，但没有命令再使用 `$T` |
| G3 行为正确 | passed | D01–D05 均有证据，见下表 |
| G4 回归检查 | passed | checks/01：macOS 干净副本，V01 7 passed、V02 6 passed、V03 171 passed、V04、V05 通过 |
| G5 质量复审 | passed | mutations/01 13 个抓住 9 个；漏掉的 4 个都是合同测试清单的遗漏，由规划端补测（见下） |
| G6 可集成 | passed | 补丁往返校验通过；执行 worktree 快照与候选一致 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01 | 超时与取消的有界收尾：进程组 SIGTERM → 宽限 1 s → 进程组 SIGKILL → 2 s 有界读取；读不完时回收组长、关闭父进程管道端并保留输出 |
| D02 | passed | checks/01 V01、V02 | 只有读完输出且进程组在 2 s 内确认已空才报 verified；PermissionError 视为组仍存活；正常结束仍有组员时报 incomplete |
| D03 | passed | checks/01 V02、V03；探针 | Windows 分支与基线逐字一致（原样移入 `_run_windows`）；启动失败、stdin、环境合并与 ProcessResult 构造不变。探针：强杀路径与优雅超时路径中超时前写出的 stdout/stderr 各只出现一次；逃出进程组的情形 4.0 s 返回 incomplete 并保留超时前的输出；300 KB stdin 正常送达 |
| D04 | passed | scope.json | 只改 `sentinelmesh/runtime.py`、新增 `tests/test_runner_posix_cleanup.py`；oracle 未改动 |
| D05 | passed | checks/01 V04、V05；scope.json | 无依赖变更、联网或凭据访问 |

## 范围与质量
- 越界与警告：
  - violations 0；新增被忽略路径只有缓存目录；新增的 skip 标记是合同要求的“Windows 上跳过”。
  - 事件第 806 行用了 macOS 没有的 `timeout` 命令，命令内的 pytest 因此没有运行；执行器在第 835 行去掉它重跑，没有影响。
- 测试可信度：
  - 执行器测试 6 个，覆盖合同要求的 4 个用例：stdin 送达、启动失败、SIGKILL 退出码与时长、强杀前输出保留。
  - 另 2 个分别覆盖逃出进程组时保留输出、优雅超时保留 timeout 原因；断言具体，每个用例都清理自己启动的进程组。
- 变异检查（中高风险）：
  - mutations/01（oracle + 执行器测试）：抓住 9 / 漏掉 4 / 跳过 0。抓住的 9 个逐个读了日志，都是对应规则的行为断言失败。
  - mutations/02（只用执行器测试）：抓住 5 / 漏掉 8。其余规则合同交给 oracle 覆盖。
  - 漏掉的 4 个都是合同测试清单的遗漏，由规划端补测：
    - `M02 SIGTERM 只发给组长` → 归因：合同遗漏 → 处理：规划端补测 `test_sigterm_reaches_every_group_member`
    - `M07 PermissionError 视为组已不存在` → 归因：输入观察不到（需白盒注入）→ 处理：规划端补测 `test_permission_error_on_the_group_probe_is_not_a_clean_group`
    - `M11 最终分支不回收组长` → 归因：合同遗漏 → 处理：规划端补测 `test_leader_is_reaped_when_an_escaped_process_keeps_a_pipe`
    - `M13 输出拼接成已读部分加尾部` → 归因：合同遗漏 → 处理：规划端补测 `test_graceful_timeout_keeps_every_byte_exactly_once`

    四条补测在候选上通过，并各自在对应变异上失败。
- 兼容性与残余风险：
  - 旧 API 在 POSIX 超时与取消路径上的行为按用户决定改变：返回更快，清理状态更诚实，新增取值 `incomplete`；现有测试与调用方没有依赖旧取值。
  - Windows 分支未改，Job Object 清理仍为 NOT_RUN。
  - 正常结束时的残留组员只报告、不回收，留给新监督器。
- 执行器自述与证据是否一致：一致。

## 独立复审
独立复审者只读复审，建议结论为“REVISE（只改测试）”，实现本身不需要修改。

它确认了以下事实：
- POSIX 分支逐条满足 Behavior contract 第 1–6 条；
- Windows 分支与 `run()` 前段与基线逐字相同；
- 按 CPython 3.12 的 `communicate` 语义，“替换而不追加”在三条路径上都让每个字节恰好保留一次；
- 最坏墙钟约为时限加 5 s，僵尸进程或 pid 复用只可能造成误报 incomplete，不会误报 verified。

它提出以下发现，规划端逐条复现后处理如下：
1. 中：M02、M07、M11、M13 在合起来的测试集上存活；第 86 行 `assertIn(b"early", …)` 接受重复输出（P1 已复现：旧 runner 强杀路径返回 `b'earlyearly'`，候选顺带修正了这个旧缺陷）。
   - 规划端裁定：合同 Step 3 写的是“stdout 仍含 early”，这些都是合同测试清单的遗漏，不是执行错误。
   - 处理：由规划端补测提交关闭，并把执行器两处输出断言改为逐字节相等。
2. 低：执行器的逃逸用例（第 92 行起）缺少就绪握手，负载重时可能误报失败（不会掩盖缺陷）。处理：规划端补测提交中补上握手。
3. 低：两个边界情形，记为残余风险，留给新监督器（设计说明第 3 节第 2 份）：
   - 只有不可中断进程才会出现的最终读取超时会丢掉输出；
   - 向进程组发 SIGTERM/SIGKILL 时的 PermissionError 会冒出 `run()`。按 kill 语义，只要组长仍属本用户就不会发生，实际效果与基线相同。
4. 低（过程）：
   - 4 条命令在 `rm -rf "$T"` 之后还跟了只读命令，属于形式偏差；
   - 第 806 行误用 `timeout`，随后重跑，没有产生虚假证据；
   - 工具错误那条命令列出了 `/usr/bin/python3*` 等系统目录，形式上越出工作目录，调度器的范围扫描没有发现。已记为 Code-Kit 待改进项。

结论分歧：独立复审建议 REVISE；规划端记为 PASS。理由是执行器满足了合同的全部明文要求，缺口来自合同测试清单（规划问题），按 review-pi 的规则由规划端补测提交。

## 结论理由
- 实现与 Behavior contract 逐条一致：两个实测缺陷都已修正，oracle 7 项全部通过。
- 输出保留与 Windows 分支经探针和逐字比较核实；执行过程符合临时文件规则。
- 变异检查暴露的 4 处测试缺口来自合同测试清单，由规划端补测，不影响对执行器候选的判断。

## 后续动作
- 需要修正的具体差异：无。
- 已授权且可以继续的集成动作：
  - local_branch；
  - 用户授权的本地集成：把 oracle 提交 `6b0de14` 与候选提交 cherry-pick 到 master，在组合结果上重跑全量测试；
  - 另做规划端补测提交（`tests/test_runner_posix_cleanup_gaps.py`）；
  - 标记 INTEGRATED。不推送。
- 人工介入：约 35 分钟（探针、两轮变异、补测设计与验证、报告）。
