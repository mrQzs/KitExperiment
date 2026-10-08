# 设计说明：运维裁定与恢复请求（macOS 单宿主）

状态：用户已决定第 5 节（2026-10-08）；规划端决定见第 6 节。

## 1. 为什么做

持久账本与重启恢复（`.agent/design/durable-ledger.md`）做完之后，控制器崩溃重启能把在途操作定案，但请求一律停在 `RECOVERY_REQUIRED`，而调度器没有任何接口能让请求重新接受领取。`START_UNKNOWN` 的操作也一样：除了换代之后再跑一次 `recover`，没有出路。结果是崩溃之后系统不能接着干活。

## 2. 现状

- 请求的停止：`Scheduler.stop`、`rollover`（`RECOVERY_REQUIRED`）、`unknown`（`START_UNKNOWN`）与 `_enforce`（`BUDGET_EXCEEDED`、`USAGE_UNKNOWN`）都只在 `stopped` 为空时写入，所以它记的是第一个原因；后来的原因只在事件里。没有任何方法把它清空。
- 一个 `START_UNKNOWN` 的操作：
  - 仍在 `LIVE` 里：占一个并发名额，它的 task 不能再领取（`E_ACTIVE_TASK`），资源占用不释放；
  - 用量不完整时算作未知用量：同一请求里任何领取都会被拒绝（`E_USAGE_UNKNOWN`），任何交接也会被拒绝（`E_BUDGET_GUARD`）。
- 也就是说，即使把 `stopped` 清空，只要还有未定案的 `START_UNKNOWN`，请求照样什么都做不了。
- 已有的出路：
  - 迟到计量：`observe` 与 `settle` 对任何状态的操作都可以补记用量，不受代际限制；
  - 复审裁决：`fixture_verdict` 把 `REVIEW_PENDING` 判为 `ACCEPTED` 或 `REJECTED`；
  - 新代际接管：`recover` 只接受代际更旧的操作，而重启恢复已经把操作的代际改成了当时的新代际。

## 3. 参考设计的要求

07 第 4.4.9 节与第 8.2–8.3 节：
- `resume` 要求当前权限有效、请求代际匹配、未知启动和关键账目已解决或风险已明确批准、额度与并发满足、保留全部历史；只把 `stopped` 改回空不是合规的恢复。
- 恢复不重置累计用量与尝试次数，只开放合法的新准入；守卫再次变化就拒绝。
- 不能证明的情况保持 `START_UNKNOWN`；无法对账就继续停止，不能补 0；不倒推批准。
- 提高或降低额度需要授权与 CAS，并生成新的策略版本；参考原型故意不实现 `resume` 与提额。

## 4. 拟议接口

- **裁定未知启动**：`resolve_unknown(operation, generation, *, resolution, operator, evidence_hash, note)`。
  - 只接受 `START_UNKNOWN` 的操作，调用方必须是当前代际。
  - 运维核实之后给出结论，连同证据摘要、运维名与说明一起记入事件；调度器不核验这些证据，只负责记下来。
  - 结论的种类见第 5 节问题 1。
- **恢复请求**：`resume(request, generation, *, expected_reason, operator, note)`。
  - 比对代际与当前的停止原因（CAS），重新检查第 5 节问题 2 的全部前提；
  - 通过之后清空 `stopped`，发出 `request.resumed`（含先前的原因、运维名与说明），尝试次数与用量都不动。

## 5. 用户决定（2026-10-08）

1. **裁定有三种结论**：
   - 未启动（`NOT_STARTED`）：资源与未用的预留释放；
   - 放弃（`ABANDONED`）：运维确认进程已不在，结果作废，资源释放，task 可以作为新的尝试重领；用量另行补记，补记完整之前预留仍然占着；
   - 确认完成（`FINISHED`）：运维核实运行已结束、清理完整，给出候选摘要，操作进入 `REVIEW_PENDING`，走正常复审。
2. **全部定案才可恢复**：请求里没有 `START_UNKNOWN`，没有旧代际里未判定的操作，没有未知用量，额度不超；同时比对代际与停止原因。
3. **不做提额**：超过额度停止的请求，只在额度重新满足（取消预留、更正用量）之后才能恢复。

## 6. 规划端的决定（2026-10-08）

- P1：结论的名称沿用 `recover` 的 outcome：`NOT_STARTED`、`FINISHED`，另加 `ABANDONED`。`FINISHED` 必须带候选摘要，另外两种不得带（`E_CLEANUP_OR_CANDIDATE`，与 `recover` 相同）。
- P2：`ABANDONED` 是新的终态：
  - 不在 `LIVE` 里：不占并发名额，task 可以重领，尝试次数照算；
  - 加入 `UNRESOLVED`：用量不完整时仍算未知用量，预留按“已知用量之外仍占着”计算；补记到完整之后才放开。这与 `REJECTED` 一样，满足“不能补 0”。
- P3：三种结论都释放资源占用：未启动与放弃是因为进程已证实不在，确认完成与 `recover` 的 `FINISHED` 相同。操作的 owner 与代际不改，裁定者记在事件里。
- P4：裁定与恢复都要求当前代际（`E_FENCED`），旧控制器不能裁定或恢复。运维名与说明是精确 `str`，符合运行时的文本规则，运维名不超过 200 个字符、说明不超过 500 个字符；证据是 64 位小写十六进制摘要。调度器不核验权限与证据，只记录。
- P5：恢复的前提按这个顺序检查，先失败的先报：
  1. 形状：运维名、说明、期望原因；
  2. 代际；
  3. 请求存在；
  4. 已停止（`E_NOT_STOPPED`）；
  5. 停止原因等于期望原因（`E_STOP_REASON_CHANGED`）；
  6. 没有 `START_UNKNOWN`（`E_RESUME_UNKNOWN`）；
  7. 没有旧代际里仍在 `RESERVED`、`HANDOFF_ACCEPTED`、`RUNNING` 的操作（`E_RESUME_UNDECIDED`）；
  8. 请求与它的每个 task 都没有未知用量（`E_RESUME_USAGE_UNKNOWN`），已用加预留不超额度（`E_RESUME_BUDGET`）。

  当前代际里仍在进行的操作不妨碍恢复：运维停止请求时正在跑的运行可以照常收尾。
- P6：恢复只清空 `stopped`，发出 `request.resumed`（请求、先前的原因、运维名、说明）；尝试次数、用量、预留与历史事件都不动。之后的领取照常检查全部守卫。
- P7：持久账本把 `resolve_unknown` 与 `resume` 追加在 `MUTATING_METHODS` 末尾。日志格式名仍是 v1：旧代码读到这两种命令会以 `E_LEDGER_REPLAY` 失败关闭，新代码照常读旧日志。durable-ledger 的 oracle 钉住了方法列表，由规划端在新合同的基线里一并更新。

## 7. 合同拆分

一份合同 `request-resume`：
- 调度器的两个方法与 `ABANDONED` 状态；
- 持久账本的两个变更方法；
- oracle 用真实进程演示完整流程：控制器崩溃、重启恢复、运维补记用量并裁定、复审、恢复请求、重新领取并运行到复审，中间重开一次账本。

## 8. 不做

- 不核验运维的权限与证据（03 的授权体系不在范围内），只记录运维名、证据摘要与说明。
- 不自动恢复请求，不自动裁定 `START_UNKNOWN`。
- 不从输出流反推用量（崩溃运行的用量由运维补记）。
- 不做多宿主或分布式恢复；Linux 与 Windows 仍是 NOT_RUN。
