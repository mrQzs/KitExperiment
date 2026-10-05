# REVIEW：supervised-operation-bridge v1 · attempt-001

Verdict: ACCEPTED_WITH_EXCEPTION

## 基本信息
- 基线：`806d9ffbcff88794f2f88fd11a22045d586a046d`（master `68b6e05` + 规划端 oracle）
- 候选树：`91c0db7`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1119.328 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 ACCEPTED_WITH_EXCEPTION

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。2 次工具错误：一次读主仓库里不存在的 oracle，一次工具名拼错，都未生效 |
| G2 范围合规 | waived | 只新增两个允许的文件，violations 0；但执行器用主仓库的绝对路径读取了两份文件（见“过程例外”） |
| G3 行为正确 | passed | Behavior contract 第 1–4 条在合同定义的输入上都满足 |
| G4 回归检查 | passed | checks/01：V01 oracle 13、V02 14、V03 69、V04 386 passed，V05–V07 通过 |
| G5 质量复审 | passed | Step 3 每一项都有用例；遗漏的规则由规划端补测（见下） |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01、V02 | `evidence_digest` 与合同逐字一致，各种文件状态都不抛出 |
| D02 | passed | checks/01 V01、V02；mutations/01 | 顺序、三个落定分支、`candidate_hash`/`finish` 失败转 START_UNKNOWN、中断后先落定再重抛 |
| D03 | passed | checks/01 V01、V02 | 检查先于一切，候选哈希在 `recover()` 之前求值 |
| D04 | passed | checks/01 V03、V04、V07 | 已有模块不退化，导入无副作用 |
| D05 | passed | checks/01 V02；代码复审 | 见“测试”一节 |
| D06 | passed | checks/01 V05、V06；scope.json | 范围合规 |

## 过程例外（G2）
- 执行器在会话开头用主仓库的绝对路径读取了 `.agent/design/scheduler-supervisor-wiring.md` 与 `sentinelmesh/supervised_record.py`，又试图读取主仓库里的 oracle（不存在，失败）。之后全部改用相对路径。
- 两份文件与 worktree 中的字节相同；`observations.json` 记录主仓库无变化；没有 shell 命令进入主仓库。
- 合同的 Forbidden 已明文写“不访问工作副本以外的路径（包括主仓库）”，执行器仍然这样做。根因仍是调度器把主仓库路径写进了合同附件名，这是第二次出现；已记入 Code-Kit 实机反馈。

## 合同遗漏（规划端，集成时一并修正）
1. **（中）被监督进程能伪造“未启动”的观察记录**。
   - 子进程能读到交接记录，自己写出一份规范的三键 NOT_STARTED 观察记录。之后我方的观察记录因目标已存在而发布失败，`run` 为 None，`replay_decision` 读到伪造记录判为 NOT_STARTED，于是 `release_proven_unstarted` 释放占用，request 不停止。
   - 规划端用探针在固定候选上复现：子进程确实运行过，操作却落为 NOT_STARTED，资源已释放。
   - 实现与合同字面一致（合同规定“由记录决定”），是合同没有要求核对内存中的运行事实。
   - 修正（规划端补全）：实时路径上，落盘判定必须与内存中的运行事实一致，否则判为 START_UNKNOWN：
     - `run` 为 None 时，只有在两份记录都不存在时才接受 NOT_STARTED；
     - `run` 有进程身份时，不接受 NOT_STARTED；
     - 进入复审还要求内存中的观察记录 `cleanup_confirmed` 为 True，且状态与落盘判定相同。
   - 恢复路径只有落盘证据。按 SentinelMesh 设计（05 文档 4.3.9），同账户下的记录可被伪造，不是安全边界；强对抗环境需要另行建立 OS 隔离。记为已知限制。
2. **（低）测试缺口**（oracle 与执行器测试都没有把守，规划端补测）：
   - 启动之后观察记录写不进去 → START_UNKNOWN、`error` 为 `E_RUNTIME_EVIDENCE`（探针确认实现正确）；
   - `candidate_hash` 抛 `KeyboardInterrupt` → START_UNKNOWN；
   - `now` 与 `allowed_secret_names` 原样传递。
3. **（低）停在 RUNNING 的窄窗口**：`mark_running` 返回之后、进入 `try` 之前落入异步中断，或换代后 `unknown()` 被拒绝，操作会停在 RUNNING。这是纯 Python 的固有限制，换代后 `recover()` 能接管。记为已知残留。

## 测试与变异
- mutations/01（oracle + 执行器测试）：15 个，抓住 14 / 漏掉 1。
  - `B-M12 摘要不按 fstat 拒绝超限` → 归因：被另一条规则遮蔽（有界读取仍会读到 65537 字节并判为 unreadable）→ 处理：注明。
- mutations/02（只用执行器测试）：抓住 10 / 漏掉 5；其中 4 个按分工由 oracle 把守，B-M12 同上。
- 执行器测试的低级问题：
  - `run_op` 只在调用正常返回后才登记进程组的清理；
  - 几处没有断言重抛的就是原来的异常对象。

  都不影响结论。
- 执行器报告：
  - V02 的数字误记为与 V01 合跑的结果；
  - V03 自检接了 `| tail`。

  以 checks/01 为准。
- 执行器在 `unresolved_questions` 中报告的两处未定义情形（`evidence_dir` 不是 str 时自然抛出 `TypeError`；`unknown()` 自身失败时传出调度器的异常）处理可以接受，写进设计说明。

## 结论理由
- 候选在合同定义的输入上满足全部规则，检查与变异证据充分。
- G2 的越界是只读的，内容与基线相同，根因在调度器，按用户“跑完直接验收集成”的授权豁免。
- 伪造记录造成的失败开放出在合同上，由规划端在集成时用单独提交修正并补测，不消耗执行预算。

## 后续动作
- 规划端补全（单独提交，随候选一起集成）：实时路径上的内存事实核对；补测伪造记录、观察记录写入失败、候选哈希被中断，以及 `now`/`allowed_secret_names` 的传递。
- 设计说明写明恢复路径的同账户伪造限制与 RUNNING 窄窗口。
- 人工介入：约 30 分钟（oracle 与草稿、复审、探针复现）。
