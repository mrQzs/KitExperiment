# REVIEW：posix-supervisor v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`897809bc14a0b842fdd2161f1749683267bd1a55`（master `cba305f` + 规划端 oracle）
- 候选树：`9a03d36254d4d3e27f534e52baa4254b09e8f6fc`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 631.881 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论同为 REVISE

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop；1 次工具错误是 macOS 没有 `nproc`，无影响 |
| G2 范围合规 | passed | 只新增两个允许的文件，violations 0。没有临时路径，没有自做变异：模块只写了一次，4 次编辑都在测试文件 |
| G3 行为正确 | failed | 第 8 条不满足；第 7 条的 PermissionError 分支会让异常冒出（见下） |
| G4 回归检查 | passed | checks/01：macOS 干净副本，V01 oracle 11、V02 13、V03 269 passed，V04–V06 通过 |
| G5 质量复审 | failed | 执行器测试把第 8 条的偏离写成了断言；S-M02、S-M06 是测试缺口 |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01、V02 | 校验顺序、独占 0600 证据文件与回滚、start_failed 路径、描述符关闭都符合第 1–5 条 |
| D02 | failed | 代码复审；规划端探针 | 第 8 条与第 7 条的缺陷见“需要修正的具体差异”第 1、2 条 |
| D03 | passed | checks/01 V01、V02、V06 | 任何字段、repr 与证据文件名里都不含环境值；导入无副作用 |
| D04 | failed | 代码复审 | `test_unconfirmed_cleanup_is_start_unknown` 断言观察记录 `state == "START_UNKNOWN"`，与第 8 条相反 |
| D05 | passed | checks/01 V03–V05；scope.json | 只新增两个允许的文件 |

## 范围与质量
- 第 8 条偏离：
  - 合同把 `state` 定义为组长的结局（STOPPED 或 FAILED），观察记录用 `"state": state`；只有生命周期在 cleanup 未确认时转到 `START_UNKNOWN`。
  - 候选在未确认时把 `state` 改写为 `"START_UNKNOWN"`，同时用于生命周期和观察记录，测试也这样断言。
  - 安全性质（不允许重放）不受影响，因为 `reconcile` 对 `cleanup_confirmed` 为 False 的终态本来就不放行。但观察记录丢掉了组长的结局，`reconcile` 也无法再区分“清理未确认”与“启动未知”。
  - 执行器的推理中曾得出正确读法，代码随后偏离，报告没有列出合同冲突。
- PermissionError（规划端在 macOS 上实测）：
  - 只剩未回收僵尸进程的进程组，`killpg` 对信号 0 与 SIGKILL 都返回 EPERM，不是 ESRCH。
  - 候选的 `_signal_group` 只忽略 ProcessLookupError。在组员刚变成僵尸、还没被回收时，残留回收的 SIGKILL 会抛 PermissionError，并在进程启动后冒出 `supervise()`：调用方拿不到结果和观察记录，证据文件留下。
  - 合同只规定了信号 0 探测时 PermissionError 视为存在，没有规定发信号时怎么办，这部分是规划遗漏。同一缺陷也存在于已集成的旧 runner，已由规划端在 `74b4d62` 修正，并补了回归测试。
- 第二次中断：KeyboardInterrupt 只在第一次等待时被接住。停止阶段的宽限等待或回收轮询中再来一次中断就会冒出，忽略 SIGTERM 的进程组不会被 SIGKILL。合同没有定义这种情况，执行器也没有提问。v2 补上规则：停止与回收阶段的中断被吸收，有界步骤照常完成。
- 变异检查：mutations/01（oracle + 执行器测试）抓住 17 / 漏掉 2 / 跳过 0，日志均为行为断言失败。
  - `S-M02 SIGKILL 只杀组长` → 归因：测试缺口（残留回收会补杀，最终状态相同；但没有用例断言 `stragglers_killed is False` 或信号的目标与顺序）→ 处理：v2 补测
  - `S-M06 PermissionError 视为组已不存在` → 归因：测试缺口（同用户的测试进程不会遇到 EPERM，需要白盒注入）→ 处理：v2 补测
- 其他（低）：
  - 相对 cwd 用例用的是不存在的相对路径，删掉 `isabs` 检查也会被“目录不存在”拦下；
  - 轮询间隔没有断言；
  - 执行器自检用了 `| tail`，以 checks/01 为准。
- 执行器自述与证据：报告一致，但没有把第 8 条偏离与第二次中断列为合同问题。

## 结论理由
- 第 8 条是合同明文规定，候选与其测试都与之相反，属于执行错误。
- PermissionError 会让异常在进程启动后冒出，也必须修正（部分属于规划遗漏）。
- 预算还剩 1 次（`max_attempts_total: 2`），出 v2 在本候选上定点返工。

## 后续动作
- v2（在本候选上继续，基线为 master `74b4d62` + 本候选的返工提交 + 规划端探针）：
  1. 观察记录的 `state` 按第 8 条保留组长的结局，只有生命周期在未确认时转 START_UNKNOWN；改正对应测试。
  2. 发信号时的 PermissionError 与 ProcessLookupError 一样不中断流程，由轮询决定 cleanup。
  3. 停止与回收阶段收到的 KeyboardInterrupt 被吸收，有界步骤照常完成，结果照常返回。
  4. 补测：
     - 超时强杀时信号的目标与顺序（`os.killpg` 记录），以及 `stragglers_killed is False`；
     - 信号 0 探测抛 PermissionError 时 cleanup 未确认；
     - 发 SIGKILL 抛 PermissionError 时不冒出异常；
     - 停止阶段的第二次中断；
     - 存在的相对 cwd 被拒绝。
- 人工介入：约 45 分钟（含 EPERM 实测与旧 runner 修正）。
