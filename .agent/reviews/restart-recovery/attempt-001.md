# REVIEW：restart-recovery v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`4c7307073575a7cfe05a8cdc660bbc873a9cb3e2`（master `b7c2899` + 规划端 oracle，12 个用例，其中 3 个启动真实的控制器子进程并用 SIGKILL 杀掉）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1159.3 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。2 个工具错误都是 edit 的参数不对（缺 path、编辑区域重叠），随即重试 |
| G2 范围合规 | passed（形式偏差） | 只改允许的 3 个文件、新增允许的 2 个文件，violations 0，主仓库未变。扫描到的工作副本外路径是探针里环境变量的值 `/usr/bin:/bin`，不是访问。形式偏差两处：两条命令在 `rm -rf "$T"` 之后又接了 `git diff --check` 与 `compileall`；一次行为探针（可执行文件不存在时记录器怎样拒绝）写在本条命令自己用 `mktemp -d` 建、随即删除的目录里。都不涉及实现或仓库副本，不是变异检查 |
| G3 行为正确 | passed（有补全） | 第 1–5 条在所有可达路径上满足；只有命令列表先检查后复制，只在调用方并发修改自己的列表时可达（L1），由规划端补全 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过，全量 702 passed |
| G5 质量复审 | passed（有补全） | 独立复审建议 PASS；两个测试缺口（L2、L3）与桥接文档（L4）由规划端补全 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1 条（调度器的运行描述）：
  - `handoff(..., *, run=None)`：已有检查的顺序不变，描述在预算检查之后、第一次改动之前校验；不合格报 `E_RUN_DESCRIPTOR`，不发事件、操作仍为 `RESERVED`、不存描述；
  - `_run_descriptor` 逐层精确类型、键集合与键的精确 `str`、归属、`_runtime_text`、`InvocationSpec` 可构造，保存新建的副本；
  - `operation_ids()` 按领取顺序，`run_descriptor()` 返回深拷贝；事件载荷不变。
- 第 2 条：`DurableScheduler.operation_ids()`、`run_descriptor()` 与 `operation()` 一样先 `_require_available()`，只读内存。
- 第 3 条：桥接交接时传 `run=_run_descriptor(spec, evidence_dir, worktree_id)`，其余不变。
- 第 4 条：入参检查在换代之前；恰好一次 `rollover()`；`RESERVED` 取消；`HANDOFF_ACCEPTED`、`RUNNING` 有描述时恢复、无描述时跳过；其余状态不碰不报；非 `LedgerError` 的 `Exception` 报 `failed` 并继续，`LedgerError` 与非 `Exception` 原样冒出。
- 第 5 条：V07 通过。

## 执行器的解读
- 在私有 `_Operation` 上新增默认 `None` 的字段 `run`：采纳。合同要求描述存在操作上；Non-goals 的“私有属性不变”指已有属性。
- `RuntimeErrorCode` 映射为 `ContractError("E_RUN_DESCRIPTOR", exc.code)`，运行时的码放进 `detail`：采纳，与 `_identity_copy` 一致。

## 发现
- **L1（低）命令列表先检查后复制。** `Scheduler._run_descriptor`（`scheduler.py:552-577`）的 `spec = dict(copy["spec"])` 是浅拷贝：元素检查（`:555`）、`InvocationSpec` 的 `tuple(...)`（`:572`）与保存时的 `list(...)`（`:577`）三次读的都是调用方的列表，违反第 1 条“每一层都先复制再检查”。独立复审用 `sys.settrace` 模拟另一线程在 `:576` 追加 `7`：内存账本存下了 `['pass', 7]`；持久账本重开时报 `E_LEDGER_REPLAY`。只有调用方在另一线程并发修改自己的列表时可达，桥接每次都新建字典；持久账本记录的又是调用方的原对象，并发修改参数本来就是 durable-ledger 的非目标。补全：确认是精确 `list` 之后立刻复制，只检查、构造与保存这份副本。
- **L2（低·测试缺口）“中断立刻冒出”没有测试能失败。** 执行器测试（`tests/test_restart_recovery.py:462-476`）里，打过补丁的 `cancel` 对第二个操作什么都不做，“恢复越过了中断”的断言永远成立；oracle 的中断子用例对每个操作都抛出 `KeyboardInterrupt`，同样看不出驱动是否继续了。独立复审验证：逐个操作捕获 `BaseException`、循环结束再抛出的变异体两处都能通过。补全：其余操作调用真实的 `cancel_unstarted`，oracle 的子用例只在第一个操作上中断，并断言第二个操作没有被碰。
- **L3（低·测试缺口）“描述在已有检查之后校验”只测了一半。** 执行器测试（`:264-293`）覆盖了 `now`、owner、代际、状态与租约、请求已停，没有 guard 与预算。把描述校验挪到请求已停与 guard 之间的变异体能通过全部测试，而它是可观察的：guard 过期且描述不合格时，候选报 `E_GUARD_STALE`，变异体报 `E_RUN_DESCRIPTOR`。补全：各补一个 guard 过期、预算超限的用例。
- **L4（低）合同隐含的桥接行为变化，对 `worktree_id` 同样成立。** 空的、或含控制字符的 `evidence_dir` 与 `worktree_id` 现在在交接时报 `ContractError("E_RUN_DESCRIPTOR")`，操作停在 `RESERVED`，占用与资源都还在；以前它们到达监督器，被判为 `NOT_STARTED` 并释放。相对路径与不存在的路径不变。仓库里没有调用方或测试因此出错；唯一的生产调用方 `adapter_run` 原样上抛调度器的 `ContractError`，它的文档已经写明被拒绝的调用不启动任何东西、操作留在 `RESERVED`。好处是空字符串不再让 `evidence_digest("")` 去读当前目录下的记录。补全：在 `run_supervised_operation` 与 `_checked_evidence_dir` 的文档里写明。
- **信息（不影响结论）**：
  - I1：只凭记录的恢复是合同接受的风险。`HANDOFF_ACCEPTED` 的记录被删光（目录还在）会判为 `NOT_STARTED` 并释放；`RUNNING` 的记录被删光时，`recover` 拒绝 `RUNNING → NOT_STARTED`（`E_RECOVERY_CONFLICT`），驱动每次重启都报 failed，操作一直留在 `RUNNING`，需要运维处理。
  - I2：取消 `RESERVED` 时不看它的代际，合同就是这样规定的；换代之后请求都已停止，`claim` 拒绝已停的请求，只有恢复期间另一线程新建并领取的请求才会被误取消。
  - I3：`scheduler.py:555-561` 的逐字段精确类型检查与 `InvocationSpec` 自己的检查重复，删掉它们的变异应算等价；只有“`command` 必须是精确 `list`”不重复。
  - I4：描述在重放时会再校验一次。以后若收紧 `_runtime_text` 或 `InvocationSpec`，带描述的旧账本会以 `E_LEDGER_REPLAY` 打不开；带 `run` 的新日志在旧代码上也会失败关闭，而格式名仍是 v1。写进设计说明。
- **独立复审对四个问题的回答**：
  - 第 1–5 条逐条成立，只有 L1 一处例外；
  - 驱动不会推翻已判定的状态，不会留下改了一半的操作，也不会报告没做的事。恢复 `RUNNING` 是安全的：`mark_running` 只登记“有过进程”，不是判定；`recover` 还拒绝 `RUNNING → NOT_STARTED`，记录被删也不会释放一个确实启动过的运行；
  - 读取、取消与错误处理的顺序正确；
  - 执行器测试大体隔离了各自的规则，例外是 L2、L3。

## 变异检查
- mutations/01（oracle 与执行器测试）：40 个，全部抓住。规划端草稿上的 38 个偏差按候选代码重写，另加取消分支的两个（取消时吞掉账本错误、取消的错误直接抛出）。
- mutations/02（只用执行器测试）：抓住 35，漏掉 5：持久账本关闭后读描述、桥接不传描述、描述用固定工作树、恢复分支吞掉账本错误、恢复分支吞掉中断。这 5 个都由 oracle 把守，合同 Step 4 没有要求执行器测它们；取消分支的错误处理反过来只有执行器测试把守。

## 结论理由
- 第 1–5 条在所有可达路径上满足，独立复审建议 PASS。L1 只在调用方并发修改自己的参数时可达；L2、L3 是测试缺口；L4 是合同隐含的行为变化，需要写进文档。判 PASS，由规划端补全。

## 后续动作
- `branch` 保留本候选；规划端在 `pi-complete/restart-recovery` 上补全 L1–L4，并重跑独立复审点名的两个变异体：逐个捕获 `BaseException`、循环结束再抛出；把描述校验挪到 guard 之前。
- 集成顺序：oracle `4c73070`、本候选、规划端补全。
- 设计说明写入 I1、I4。
- 人工介入：约 45 分钟（集成后重验草稿、合同、变异规格与两轮变异、门禁核对、复审记录）。
