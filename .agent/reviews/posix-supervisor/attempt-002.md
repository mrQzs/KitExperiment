# REVIEW：posix-supervisor v2 · attempt-002

Verdict: REVISE

## 基本信息
- 基线：`c5ac19477131971f4e256f22281e9d2e596c8032`（master `f458fb4` + oracle + attempt-001 的返工提交 + 规划端探针）
- 候选树：`9f279482ab6f510b43309c0c7cc5b94efb4b5dc2`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 802.811 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论同为 REVISE

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop。1 次工具错误是一次参数不全的 edit 调用，未生效。执行报告末尾的 JSON 在 D03 一项多了 `"done",`，调度器记为 json_invalid；报告正文与证据一致 |
| G2 范围合规 | passed | 只修改两个允许的文件，violations 0。临时目录都用 `mktemp -d … rm -rf "$T"`；没有自做变异。执行器只读过标准库 `subprocess.py`（工作目录外，不涉及凭据） |
| G3 行为正确 | failed | 第 3 条在截止时间边界上不成立（见下 F1） |
| G4 回归检查 | passed | checks/01：macOS 干净副本，V01 oracle 11、V02 探针 7、V03 18、V04 282 passed，V05–V07 通过 |
| G5 质量复审 | failed | Step 3 的轮询间隔用例没有把守 0.05 秒（S-M26 存活）；另有测试稳健性问题（见下） |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V02、V03 | 观察记录保留组长结局，只有生命周期转 START_UNKNOWN |
| D02 | passed | checks/01 V01–V03；mutations/01 | 发信号的 PermissionError 被忽略，升级 `timeout_killed` 与 `stragglers_killed` 不受影响；信号顺序由探针逐次断言 |
| D03 | failed | 代码复审；规划端内存探针 | 主体正确，截止时间边界有偏差（F1） |
| D04 | failed | mutations/01、02 | 轮询间隔用例与模块常量比较（F2） |
| D05 | passed | checks/01 V01、V04–V07；scope.json | v1 行为不退化，范围合规 |

## 范围与质量
- **F1（中）第 3 条的截止时间边界**：`_wait_for_leader` 吸收中断后，若剩余时间已不大于 0，直接返回 False，不再看组长是否已退出。
  - CPython 3.12 的 `Popen.wait` 第一次收到中断时，会再等子进程最多 `min(0.25 秒, 剩余时间)`，期间可能已经回收组长，然后照样重新抛出中断。这段等待的终点就是截止时间。
  - 没有中断时，`_wait` 在截止时刻会再检查一次，结果是 True；有中断时结果变成 False。第 3 条规定“截止时间、结束条件与后续步骤都不变，中断不改变 stop_reason”，这里违反了。
  - 后果：宽限等待里会向已回收组长的进程组多发一次 SIGKILL，并把 `"timeout"` 改成 `"timeout_killed"`，而 `exit_code` 是组长自己的结局（例如 -15），记录自相矛盾。观察记录的 `state` 仍是 FAILED，重放判定不受影响。
  - 触发条件：
    - 超时路径（第一次等待没被打断）；
    - 中断落在宽限窗口最后 0.25 秒内；
    - 组长恰在截止前最后一个轮询间隔内退出。

    窗口窄，但能真实发生。规划端用内存替身在固定候选上复现：`_wait_for_leader` 返回 False，`_stop_group(..., "timeout")` 发出 SIGTERM 与 SIGKILL 并返回 `timeout_killed`。
  - 合同的 Repository facts 写了 CPython 的 0.25 秒行为，规划端草稿用 `proc.wait(timeout=max(0.0, 剩余))` 避开了这一点：timeout 为 0 时 CPython 仍检查一次。归为执行错误。
- **F2（中）轮询间隔**：
  - `test_reap_poll_never_sleeps_longer_than_the_interval_bound` 把每次休眠与从被测模块导入的 `_REAP_POLL_SECONDS` 比较，常量改大时界限随之变大；常量用例也没有锁定它。
  - Step 3 明确要求“回收轮询的间隔不超过 0.05 秒”。
- **F3（低，规划遗漏）中断吸收的残留窗口**：只有 `try` 体内抛出的中断会被吸收。`continue` 的回边、截止时间的计算，以及回收阶段第一次探测与 SIGKILL，都在 `try` 之外。
  - 吸收一次中断后、重新进入 `try` 之前若再来一次 SIGINT，仍会冒出。这个窗口只有几个字节码，纯 Python 无法彻底关闭（需要屏蔽 SIGINT）。
  - 合同只排除了“第一次等待之前”的窗口，没有提到这一类，属于规划遗漏。作为已知残留记入设计说明。
- **测试稳健性（低）**：
  - `interrupt_first_wait` 按 ready 文件是否存在决定是否注入，不按调用次数。组长若赶在第一次等待之前写出 ready，就不会注入，用例约 32 秒后以错误结局失败。只会假失败，不会假通过。
  - `test_interrupt_during_the_wait_after_the_kill_is_absorbed` 没有拦住冒出的 KeyboardInterrupt：mutations/02 中 S-M21 让整个 pytest 会话中断（退出码 2，只跑了 7 个用例）。
  - 它用“第 3 次 `wait()`”定位强杀后的等待，和实现结构绑在一起。
  - 清理在 `supervise()` 返回后才登记。
- **变异检查**：
  - mutations/01（oracle + 探针 + 执行器测试）：30 个，抓住 29 / 漏掉 1 / 跳过 0，日志均为行为断言失败。
    - `S-M26 轮询间隔 0.5 秒` → 归因：执行器测试缺口（F2）→ 处理：规划端补测。
  - mutations/02（只用执行器测试）：抓住 15 / 漏掉 15。
    - 漏掉的 15 个里，14 个按合同分工由 oracle 与探针把守，另 1 个是 S-M26。
    - S-M21 被抓住的方式是 pytest 会话被中断（见上）。
  - mutations/03（追加，独立复审提出）：
    - `S-M31 等待组长时每次中断都重置截止时间` → 漏掉；
    - `S-M32 回收轮询中断后重置截止时间` → 漏掉。
    - 归因：规划端探针缺口。探针只断言了“没有提前结束”，没有断言“没有延后”。处理：规划端补测。
- 执行器自述与证据：报告与证据一致。“V04 基线 1 failed”一句在事件流中没有对应的运行。未列出合同问题。

## 结论理由
- F1 违反第 3 条“中断不改变结束条件与 stop_reason”，F2 没有完成 Step 3 要求的间隔用例，都属于执行错误。
- 预算已用完（`max_attempts_total: 2`）。剩余工作小而明确，由规划端在返工提交上补齐，单独提交后集成；attempt-001 与 attempt-002 标记 SUPERSEDED。

## 后续动作
- 规划端补全（单独提交）：
  1. `_wait_for_leader` 改为 `proc.wait(timeout=max(0.0, 剩余))`，到截止时刻仍检查一次组长；剩余时间的计算移进 `try`。
  2. 规划端补测 `tests/test_posix_supervisor_gaps.py`：
     - 轮询间隔按真实探测的时间间隔钉住 0.05 秒；
     - 截止时刻已回收的组长判为已退出，`stop_reason` 保持 `timeout`；
     - 两处截止时间在中断后都不延后。
  3. 执行器测试：`interrupt_first_wait` 改按调用次数注入；强杀后等待的用例拦住冒出的 KeyboardInterrupt。
  4. 设计说明记入 F3 的残留窗口。
- 人工介入：约 40 分钟（探针设计与验证、复审、内存探针复现）。
