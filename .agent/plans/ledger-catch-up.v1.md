# TASK: ledger-catch-up

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 4996e106fed511bc16d6d9635995309e2b54a70f
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch, merge
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/serve-watch.md`（第 3.1 节；用户决定第 5 节第 1 项“账本补读”；规划端决定第 6 节）改 `sentinelmesh/durable_ledger.py`：每次变更先拿写锁、把别的实例追加的命令补读进内存，再执行、追加、提交；新增 `refresh()` 只补读不写；补读遇到换代、接不上本实例的头时仍是 `E_LEDGER_STALE`。让规划端 oracle `tests/test_ledger_catch_up_oracle.py`（14 个用例）与基线里按新语义改写的 4 个旧测试全部通过；新建执行器测试 `tests/test_ledger_catch_up.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不改 `Scheduler` 与 `scheduler.py`、`run_cli.py`、`ledger_cli.py`、`restart_recovery.py` 或任何其它模块：本合同只改账本。`serve --watch` 与“控制器在开始时把代读一次”是下一份合同。
- 不改 `compact`：日志头不是自己的，仍然 `E_LEDGER_STALE`。不改打开、创建与检查点的格式，不改 `MUTATING_METHODS`、`LEDGER_FORMAT`、命令编码与链式摘要。
- 读视图（`task`、`snapshot`、`operation` 等）不自动补读，只有写入与 `refresh()` 补读。
- 不追求网络文件系统或多台主机上的多写者：只在本机 SQLite 上成立。
- 纯 Python 吸收中断有残留窗口：补读或内存调用当中的中断让实例失效，这是本合同要的结果，不要试图“恢复”半更新的内存。
- 不联网、不读凭据、不启动真实 pi。

## Repository facts
- 基线 = master `b4955b8`（设计说明初稿）+ 规划端提交 `4996e106fed511bc16d6d9635995309e2b54a70f`。后者包含三样东西：
  - 定稿的设计说明（第 3.1、5、6、9 节）；
  - 新文件 `tests/test_ledger_catch_up_oracle.py`：14 个用例，两个类 `CatchUpOracle` 与 `OperatorWhileServing`，Windows 上约 7 秒；
  - 按新语义改写的 4 个旧测试（见下）。
- 基线上的失败情况：
  - oracle 里补读语义的用例都失败；
  - “换代就 STALE”“refresh 是公开方法”这类用例部分失败（基线没有 `refresh`）；
  - 4 个改写过的旧测试都失败。
- 下列旧断言是错的，已在基线里改为对同一输入断言补读后的结果，不要改回去：
  - `tests/test_durable_ledger_oracle.py`：`test_a_second_instance_in_this_process_is_caught_up`（原来断言另一实例写过之后本实例 `E_LEDGER_STALE`）；
  - `tests/test_ledger_cli_oracle.py`：`test_a_write_while_a_controller_holds_the_ledger_is_caught_up_by_the_controller`；
  - `tests/test_ledger_compaction.py`：`test_another_instance_compacting_at_this_head_and_appending_is_caught_up`；
  - `tests/test_ledger_compaction_oracle.py`：`test_another_process_compacting_leaves_this_instance_current` 的后半段。
  “另一个进程换代 → 本实例 STALE”（`test_another_process_that_takes_over_makes_this_instance_stale`）照旧成立，没有改。
- oracle 的驱动方式：
  - 两个实例在同一进程里交替写；
  - 两个子进程从同一条起跑线各写 120 条；
  - 直接改 SQLite 表来造坏尾与另一段历史；
  - `mock.patch.object(durable_ledger, "_replay", …)` 在补读当中抛中断或异常：补读必须在调用时经模块级名字 `_replay` 重放每一条命令（不要提前绑定）；
  - `mock.patch.object(type(ledger._scheduler), "resume", …)` 让内存调用抛 `TypeError` 或中断；
  - 真实 `smctl run serve --parallel 1` 在跑时，另一个进程执行 `smctl ledger stop` 或 `verdict`。
- `sentinelmesh/durable_ledger.py`（1218 行）：
  - `DurableScheduler._apply` 约第 937 行：今天先执行内存调用，再 `_append`。内存调用抛 `ContractError` 时原样抛出、实例可用；抛其它异常时实例失效、原样抛出。追加失败时 `LedgerError` 原样传出，其它 `Exception` 变成 `E_LEDGER_WRITE`，都让实例失效。
  - `_append` 约第 972 行：`BEGIN IMMEDIATE`，核对 `_head(db) == (self._seq, self._last_sha)`（不等就 `E_LEDGER_STALE`），`INSERT`，`COMMIT`，然后推进 `_seq`、`_last_sha`。
  - `compact` 约第 995 行。
  - 打开时的工具函数：`_head` 约第 605 行；`_verify_chain(rows, start_seq, previous)` 约第 580 行（序号与链断了是 `E_LEDGER_CHAIN`）；`_read_checkpoint(db)` 返回 `(seq, last_sha, state)` 或 `None`；`_replay(scheduler, command_text, used)` 约第 687 行（任何失败都是 `E_LEDGER_REPLAY`）；`_strict_loads`；`_rollback(db)`；`_check_claim`。
  - `open_ledger` 约第 1153 行：用 `_ReplayScheduler` 或 `_decode_state`（也返回 `_ReplayScheduler`）重放；“已用操作 id”表 `used` 只在打开时临时构造。
  - `create_ledger` 约第 1081 行：内存调度器是普通的 `Scheduler(generation=generation)`。
  - 实例锁是 `threading.RLock`，所有读写都在锁内。SQLite 连接是 `isolation_level=None`，`sqlite3.connect` 的缺省忙等待是 5 秒。
- 实测（2026-10-16，真实进程）：今天 serve 有运行在跑时，另一进程 `smctl ledger stop` 成功；运行结束时 serve 得到 `E_LEDGER_STALE`，操作停在未决状态，预算的保留量还挂着。
- 规划端草稿实测（Windows，约 70 行）：
  - oracle 14/14；改写的 4 个旧测试通过；
  - 草稿树全量：除这 4 个旧测试（随基线改写）外，没有别的测试钉住旧行为；
  - 19 条故意偏差中 18 条被 oracle 抓住：写入不补读、`refresh` 不补、换代不算 STALE、不查衔接、`seq`/`sha` 不前进、新建账本仍用普通 Scheduler、拒绝也让实例失效、其它异常不让实例失效、`refresh` 的错误不让实例失效、检查点序号不更新、先执行后补读、返回少键、只查第一条是否换代、不核对复用的操作 id、写入用延迟事务、两处其它异常不变成 `E_LEDGER_REPLAY`；
  - 1 条等价：不单独查检查点。检查点越过本实例时，本实例之后的第一条命令必然接不上它的头，所以衔接检查已经给出 `E_LEDGER_STALE`。

## Scope
- allowed_modified_files:
  - sentinelmesh/durable_ledger.py
- allowed_new_files:
  - tests/test_ledger_catch_up.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_ledger_catch_up_oracle.py`, `tests/test_durable_ledger_oracle.py`, `tests/test_durable_ledger.py`, `tests/test_durable_ledger_gaps.py`, `tests/test_ledger_cli_oracle.py`, `tests/test_ledger_compaction.py`, `tests/test_ledger_compaction_oracle.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/run_cli.py`, `sentinelmesh/ledger_cli.py`, `sentinelmesh/cli.py`, `sentinelmesh/restart_recovery.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/windows_supervisor.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/pi_events.py`, `sentinelmesh/core.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下列名字由本合同固定：
- `DurableScheduler.refresh()`；
- 模块级 `_replay`（补读经它重放）；
- `create_ledger` 返回的实例的内存调度器是 `_ReplayScheduler`。

其余辅助函数（例如补读的私有方法）的名字由你定。执行器测试可以用私有名字。

1. **补读**（写入与 `refresh()` 共用，只在一个已经开始的事务里运行）：
   - 日志头 `_head(db)` 等于本实例的 `(seq, sha)`：什么也不做，补读 0 条。
   - 否则，按序号读出序号大于本实例 `seq` 的全部命令行。没有这样的行，或者第一行的 `previous` 不等于本实例的 `sha`：`LedgerError("E_LEDGER_STALE")`。
   - 用 `_verify_chain(行, 本实例 seq, 本实例 sha)` 核对链，链断了是它自己的 `E_LEDGER_CHAIN`。
   - 在重放任何一条之前，先看这些命令里有没有 `method` 为 `"rollover"` 的。有就是 `LedgerError("E_LEDGER_STALE")`，一条也不重放。
   - 用本实例内存里现有的全部操作现场构造“已用操作 id”表：`{操作 id: (request_id, idempotency_key)}`，与打开时对检查点的做法相同。
   - 按顺序对每条命令调用模块级的 `_replay(本实例的内存调度器, 命令文本, 表)`。
   - 全部成功后，`seq` 增加补读的条数，`sha` 设为最后一条的摘要。日志里有检查点时，把 `checkpoint_seq` 设为检查点的序号。
   - 返回补读的条数。
2. **写入**（`_apply`，所有变更方法共用）依次做：
   - （a）在实例锁内核对可用，绑定签名。签名错误在任何事情之前抛出，实例可用，与今天相同。
   - （b）`BEGIN IMMEDIATE`。`sqlite3.Error` 变成 `E_LEDGER_WRITE`，实例失效。
   - （c）第 1 条的补读。`LedgerError` 原样传出；其它 `Exception` 变成 `E_LEDGER_REPLAY`；其它 `BaseException`（中断）原样传出。三种情况都先回滚，再让实例失效。
   - （d）内存调用：
     - 抛 `ContractError`：回滚，实例**可用**。补读到的状态保留，因为那些命令已经在日志里了。原样抛出。
     - 抛其它任何异常（含中断）：回滚，实例失效，原样抛出（不包装）。
   - （e）编码命令；按 `(seq + 1, 命令文本, entry_sha256(sha, 命令文本), sha)` 插入；`COMMIT`。`LedgerError` 原样传出，其它 `Exception` 变成 `E_LEDGER_WRITE`，中断原样传出。三种情况都先回滚，再让实例失效。
   - （f）提交之后，`seq` 加一，`sha` 设为这条命令的摘要，返回内存调用的结果。
   写锁从（b）持有到（e）的提交或回滚，所以两个进程的写入由 SQLite 串行化。`_append` 的工作并入这里；不再用到的 `_append` 删掉。
3. **`refresh()`**：
   - 在实例锁内核对可用；不可用是 `E_LEDGER_UNAVAILABLE`。
   - 开一个读事务（`BEGIN`），在里面做第 1 条的补读，然后结束事务（回滚）。不写任何东西。
   - 返回恰好两键 `{"seq": 补读之后的 seq, "applied": 补读的条数}`。
   - 失败规则：`LedgerError` 原样传出；`sqlite3.Error` 与其它 `Exception` 变成 `E_LEDGER_REPLAY`；中断原样传出。三种情况都让实例失效。
   - `refresh` 不是变更方法，不进 `MUTATING_METHODS`。
4. **`create_ledger`**：内存调度器改用 `_ReplayScheduler(generation=generation)`。没有记下操作 id 时，它与 `Scheduler` 的行为相同；补读别的实例的领取时，它沿用日志里记下的 id。其余不变。
5. **不变的东西**：`compact`、`open_ledger`、只读视图、`close`、失效之后一切调用都是 `E_LEDGER_UNAVAILABLE`。
6. **文档串**：
   - 模块文档串里“a log another instance already moved on, fails closed”那条，改成补读的说法：串行化的写入、换代与接不上头仍然 STALE、坏尾 `E_LEDGER_CHAIN`、拒绝的尾 `E_LEDGER_REPLAY`；
   - `DurableScheduler` 的类文档串里“One instance is the only writer of its log…”那段同样改写；
   - `refresh` 与补读的私有方法各写一段文档串，写明第 1–3 条；
   - 导入无副作用。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 3.1、5、6 节、oracle 文件，以及 `durable_ledger.py` 的 `_apply`、`_append`、`compact`、`open_ledger`、`create_ledger`、`_verify_chain`、`_replay`、`_head`。基线与 Repository facts 不符就停止并报告。
2. 改 `durable_ledger.py`（第 1–6 条；约 80 行连文档串）。
3. 跑 V01 直到 oracle 全过（约 7 秒），再跑 V03（改写过的旧测试与账本的其它测试，约 90 秒）。
4. 跑 V04，确认 run、恢复与复审这些用账本的命令没有受影响。
5. 新建 `tests/test_ledger_catch_up.py`，只覆盖下面这些。oracle 已覆盖交替写、两进程并发、换代、检查点、另一段历史、坏尾、补读当中的失败与真实 serve；可以沿用 oracle 的写法，但不要照抄 oracle 的用例。每个拒绝用例先验证合法基线，再只改一处；错误码全等断言。
   - 没有新命令时 `refresh()` 返回 `{"seq": n, "applied": 0}`，而且 `commands` 表不变；
   - `create_ledger` 建的实例补读另一实例的一条领取，之后 `operation(该 id)` 可读，`operation_ids()` 与重新打开的相同；
   - 补读之后内存调用抛 `ContractError`：实例可用，`commands` 表没有新行，补读到的任务在 `task_ids` 里；
   - 换代不是第一条新命令时（先 `seed_task` 再 `rollover`），写入仍然 `E_LEDGER_STALE`，实例不可用，`commands` 表不变；
   - 关闭后的实例调用 `refresh()` 是 `E_LEDGER_UNAVAILABLE`。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V09。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告。合同没有写死、需要你自己定的地方，按最合理的读法继续，写进 `interpretations`；与合同矛盾、或没法继续时，停止并写进 `contract_conflicts` 或 `unresolved_questions`。

## Validation commands

必要检查优先直接执行。输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。

需要截短输出时：
- 照抄执行说明“机器合同摘要”里每条检查的“自检照抄”写法（bash 的写法以 `set -o pipefail;` 开头，管道的退出码就是被测程序自己的）；
- 自己临时写的测试命令要接 `| tail` 之类的管道时，开头同样加 `set -o pipefail;`；
- 不要把输出写到 `/tmp/v01.txt` 这类固定路径。

另外：探针里不要用 `tempfile`；不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改），验收端会在固定候选上做；只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_ledger_catch_up_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_ledger_catch_up.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要，基线应失败）：`python -m pytest -q tests/test_durable_ledger_oracle.py tests/test_durable_ledger.py tests/test_durable_ledger_gaps.py tests/test_ledger_cli_oracle.py tests/test_ledger_compaction.py tests/test_ledger_compaction_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要）：`python -m pytest -q tests/test_run_cli_serve_parallel_oracle.py tests/test_run_cli_serve_oracle.py tests/test_run_cli_oracle.py tests/test_run_cli_recover_oracle.py tests/test_restart_recovery_oracle.py tests/test_request_resume_oracle.py tests/test_review_verdict_oracle.py tests/test_task_done_oracle.py tests/test_ledger_events_oracle.py tests/test_ledger_reference_oracle.py` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：1200s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V08（必要，基线应失败）：`python -c "from sentinelmesh import durable_ledger as d; assert callable(d.DurableScheduler.refresh) and 'refresh' not in d.MUTATING_METHODS and not hasattr(d.DurableScheduler, '_append'); print('catch-up ok')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`catch-up ok`
- V09（必要）：`git diff --quiet 4996e106fed511bc16d6d9635995309e2b54a70f -- sentinelmesh/scheduler.py sentinelmesh/run_cli.py sentinelmesh/ledger_cli.py sentinelmesh/cli.py sentinelmesh/restart_recovery.py sentinelmesh/adapter_run.py sentinelmesh/supervised_operation.py sentinelmesh/supervised_record.py sentinelmesh/posix_supervisor.py sentinelmesh/windows_supervisor.py sentinelmesh/process_identity.py sentinelmesh/runtime.py sentinelmesh/pi_events.py tests/test_ledger_catch_up_oracle.py tests/test_durable_ledger_oracle.py tests/test_durable_ledger.py tests/test_durable_ledger_gaps.py tests/test_ledger_cli_oracle.py tests/test_ledger_compaction.py tests/test_ledger_compaction_oracle.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

在基线上，V01、V05、V08 失败是因为还没有补读与 `refresh`，V02 失败是因为测试文件还不存在，V03 失败是因为基线里改写过的 4 个旧测试断言的是补读后的结果。V04 保护用账本的其它命令，V09 保护十三个不该动的模块与七个测试文件（检查命令按 argv 直接执行，不经 shell）。全量约 300 秒。

## Definition of done
- D01：补读的规则——衔接、链、换代、已用操作 id、序号与检查点（第 1 条）；证据：V01、V02、V03；目的：操作员从另一个进程下的停止、裁决、恢复，会被一个正在运行的控制器看见，而不是把它弄坏；换代接管、另一段历史与坏日志仍然不被接受。
- D02：写入的顺序与失败规则——写锁、补读、内存调用、追加、提交，谁让实例失效、谁不（第 2 条）；证据：V01、V02、V03；目的：两个进程的写入串行化、一条不丢，调度器的拒绝不毁掉实例，半更新的内存永远不会被写进日志。
- D03：`refresh()` 与 `create_ledger` 的调度器（第 3–4 条）；证据：V01、V02、V08；目的：需要最新状态的调用方能只读地补上，新建的账本也能补读别的实例的领取。
- D04：其它命令与模块不受影响，`compact` 与打开不变，文档串按第 6 条，导入无副作用；证据：V04、V05、V09、review。
- D05：Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V06、V07、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 pi。
- 本合同对同一行为说法不同。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 2400
- max_wall_seconds_total: 4800
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配（上限 40 分钟，按 28 分钟规划、留 12 分钟余量）：
  - 阅读约 7 分钟；
  - 实现约 7 分钟（一个文件，约 80 行）；
  - 测试约 5 分钟（5 个用例，先跑单个用例）；
  - 运行检查约 8 分钟（oracle 约 7 秒，V03 约 90 秒，V04 约 120 秒，全量约 300 秒）；
  - 执行报告约 1 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍写进 `interpretations`（合同原文、你的做法、理由）；与合同矛盾的写进 `contract_conflicts`，没法决定的写进 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、改写过的旧测试、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
