# REVIEW：request-resume v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`50ef86acf12548e5f7e7be1f44f4e001e32c0651`（master `a12b705` + 规划端 oracle：新的 `tests/test_request_resume_oracle.py` 11 个用例，其中 1 个用真实进程走完崩溃到恢复的全程；`tests/test_durable_ledger_oracle.py` 期望的方法列表追加两个名字）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1313.1 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。2 个工具错误都是 edit 的旧文本不唯一或没有匹配，随即重试 |
| G2 范围合规 | passed（形式偏差） | 只改允许的 2 个文件、新增允许的 1 个测试文件，violations 0，没有工作副本外路径，主仓库未变。一条命令在 `rm -rf "$T"` 之后又接了 `git diff --check`；两段较长的多行命令是用 Python 改写自己的测试文件，不是变异检查 |
| G3 行为正确 | passed | 第 1–5 条按原文实现；两个合同层面的设计问题（M1、L1）在合同本身，执行器照合同字面实现 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过，全量 739 passed |
| G5 质量复审 | passed（有补全与后续合同） | 独立复审建议 PASS；顺序测试的缺口（L2）由规划端补全，M1、L1 交给后续合同 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条：`UNRESOLVED` 加入 `ABANDONED`，`LIVE` 不变；另有模块常量 `ABANDONED`、`RESOLUTIONS` 与 `_RESOLVED_STATES`。
- 第 2 条：`resolve_unknown` 按合同的顺序检查——结论、证据、候选、运维名、说明、代际、操作、状态——全部在改动之前；三种结论的状态、候选与资源释放、事件载荷都与合同一致；请求的停止不变。
- 第 3 条：`resume` 按合同的顺序检查——运维名、说明、期望原因、代际、请求、已停止、原因比对、未知启动、旧代际未判定、先查请求与每个 task 的未知用量，再查额度——之后只清空停止并发出 `request.resumed`。
- 第 4 条：`MUTATING_METHODS` 末尾追加两个名字，日志格式名不变。
- 第 5 条：V07 通过。

## 执行器的解读
- 合同矛盾与未决问题都为空。三条 limitations：
  - `resolve_unknown` 不核对 owner：合同只要求当前代际，采纳；
  - task 级的未知用量是请求级的子集：确实如此，任何 task 里的未知操作都会先在请求级被计到；
  - 真实进程的完整流程由 oracle 覆盖：采纳。

## 发现
- **M1（中·合同缺口）比对停止原因看不到后来叠加的停止。**
  - `stopped` 只记第一个原因。请求已经停止之后，`stop()`、`_enforce`、`unknown()`、`rollover()` 再次停止它，都不改这个字段。
  - 预算、未知用量、`START_UNKNOWN`、旧代际未定案这几类后来的停止，`resume` 自己的检查会重新核一遍，不会漏；但运维显式调用的 `stop()` 没有任何检查复核。
  - 规划端复现：请求因换代以 `RECOVERY_REQUIRED` 停止，再 `stop("req-1", "SECURITY_HOLD")`，`stopped` 仍是 `RECOVERY_REQUIRED`；`resume(expected_reason="RECOVERY_REQUIRED")` 成功，安全暂停只留在 `request.stopped` 事件里，就此失效。
  - 合同第 3 条第 6 项要求比对的就是 `stopped`，执行器的测试也钉住了这一行为。用户决定的“比对停止原因”只实现了一半，根源在规划端的合同。
- **L1（低·合同缺口）账本记得已经运行过的操作仍可判为未启动。** 一个经历了 `run.started` 的操作判为 `NOT_STARTED` 之后，未补记的用量按 0 计（`NOT_STARTED` 在 `_snapshot` 里只算已知用量），请求随即可以恢复；同样的转换在 `recover` 里会被拒绝（`E_RECOVERY_CONFLICT`）。合同把“不核验运维证据”列为非目标，所以候选没有违约，但这绕开了 P2 的“不能补 0”。
- **L2（低·测试空白）五对相邻检查的先后没有测试守住。** 独立复审对能同时失败的检查两两组合（`resolve_unknown` 27 对、`resume` 14 对），候选的顺序都对；但下列颠倒在两套测试下都能通过：`resolve_unknown` 的说明与运维名、候选与证据、操作与代际，`resume` 的说明与运维名、请求与代际。
- **L3（低·测试空白）task 级额度只有 oracle 在测。** 执行器测试里 task 的额度与请求相同。
- **信息（不影响结论）**：
  - I1：格式名仍是 v1，旧代码读到新命令以 `E_LEDGER_REPLAY: unknown method` 失败关闭，P7 已接受；错误码看起来像账本损坏，以后再加方法时考虑升格式名或单独的错误码。
  - I2：未知用量对 task 范围的检查都包含在请求范围里，删掉它是等价变异；“运维名接受子类”同样等价。
  - I3：重启恢复里记录被删、卡在 `RUNNING` 的操作现在有了出路：先用 `recover(outcome="START_UNKNOWN")` 转成 `START_UNKNOWN`，再裁定。
- **独立复审对五个问题的回答**：
  - 第 1–5 条逐条成立，两个新方法的检查顺序逐对核过；
  - `ABANDONED` 不会提前释放预留，不挡该允许的重领，不歪曲并发与尝试计数，与其他入口的交互都正确；
  - 恢复不会在仍有未定案、未知用量或超额时放开，也没有错误拒绝（当前代际的运行不挡）；
  - 持久账本上两条新命令的记录与重放完全一致，被拒绝的输入都在内存调用之前被拒绝；
  - 执行器测试大体隔离了各自的规则，例外是 L2、L3。

## 变异检查
- mutations/01（oracle、更新后的 durable-ledger oracle 与执行器测试）：30 个抓住 29 个。漏掉的是“运维名接受子类”，等价写法：`_runtime_text` 自己就要求精确 `str`。
- mutations/02（只用执行器测试）：抓住 25 个。除了上面的等价写法，还漏掉任务级额度与三处检查顺序（旧代际与未知启动、额度与未知用量、代际与形状），都由 oracle 把守。

## 结论理由
- 候选按合同原文实现，独立复审建议 PASS。M1、L1 是合同层面的设计问题，L2、L3 是测试空白。判 PASS。
- M1 要改 `resume` 的比对语义，属于用户决定的细化，交给后续合同，先问用户；L1 一并处理。在那之前 M1 记为已知残留。

## 后续动作
- `branch` 保留本候选；规划端在 `pi-complete/request-resume` 上补 L2 的五对顺序用例（加进 oracle 的两个顺序测试）。
- 集成顺序：oracle `50ef86a`、本候选、规划端补全。
- 后续合同处理 M1、L1：先问用户停止原因怎么比对、已运行过的操作能否判为未启动。
- 人工介入：约 40 分钟（设计说明与提问、草稿与 oracle、合同、变异检查、门禁核对、复审记录）。
