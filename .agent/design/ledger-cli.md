# 设计说明：运维命令行入口 `smctl ledger`（macOS 单宿主）

状态：用户已决定第 5 节（2026-10-09）；规划端决定见第 6 节。用户已决定用 Python 实现（不改 TypeScript）。

## 1. 为什么做

持久账本、重启恢复、运维裁定与恢复请求都做完了（`.agent/design/durable-ledger.md`、`.agent/design/request-resume.md`），可是每一步都得直接调 Python 方法：打开账本、读停止原因、找出待裁定的操作、补记用量、裁定、恢复。运维没有一个能照着走的入口，崩溃之后“接着干活”仍要写代码。

## 2. 现状

- `smctl`（`sentinelmesh/cli.py`，`[project.scripts]` 的入口）是 argparse 的子命令集合：`validate-config`、`plugin-lock`、`debug-bundle`、`baseline`、`workflow`、`contract-render`。任何异常都打印 `error: ...` 到 stderr，退出码 2。没有专门的 CLI 测试。
- 持久账本：`open_ledger(path)` 重放日志、只读不写；每个变更调用追加一条命令；另一个实例推进了日志时，旧实例下一次追加报 `E_LEDGER_STALE` 并失效（失败关闭）。
- 现有的只读接口：`generation`、`events`、`snapshot(request, task=None)`、`operation(op)`、`operation_ids()`、`run_descriptor(op)`、`stop_reasons(request)`。
  - 没有列出全部请求的方法：请求 id 只能从事件里推；
  - 也看不到单个操作记下的用量：`snapshot` 只给出每个请求有几个未知操作，不说是哪几个。
- 运维要用到的变更方法：`stop`、`settle`（`observe`）、`resolve_unknown`、`resume`。重启恢复驱动 `recover_after_restart` 需要调用方给出 `candidate_hash`（证据目录 → 候选的摘要），只有控制器知道候选是什么。

## 3. 拟议的命令

`smctl ledger <命令> LEDGER [选项]`，`LEDGER` 是账本文件路径。每个命令都打开账本（重放），最多做一次变更，然后关闭。代际一律取账本的当前代际。

- `status`：只读。输出 JSON：
  - 账本的代际与命令条数；
  - 每个请求：是否停止、停止原因列表、预算快照；
  - 需要运维处理的操作：`START_UNKNOWN`、旧代际里未判定的、用量不完整的，各带状态、代际、所属 task、有没有运行描述，以及记下的用量。
- `stop --request R --reason TEXT`：显式暂停一次。
- `settle --operation OP --dimension D --amount N --evidence HASH [--revision N] [--correction]`：补记一项最终用量。
- `resolve --operation OP --resolution {NOT_STARTED,ABANDONED,FINISHED} --operator NAME --evidence HASH --note TEXT [--candidate HASH]`：裁定一个 `START_UNKNOWN`。
- `resume --request R --reason A [--reason B ...] --operator NAME --note TEXT`：按顺序点名停止原因，恢复请求。

输出：成功时 stdout 一个 JSON 对象；账本拒绝（`ContractError`、`LedgerError`）时 stderr 一个 JSON 对象 `{"error": code, "detail": ...}`。

为了让 `status` 说得出是哪个操作缺用量，调度器与持久账本各加两个只读方法：`request_ids()` 与 `operation_usage(op)`。

## 4. 参考设计

07 第 8.3 节的运维逐步恢复流程（R1 冻结、R4 核对运行、R5 核对预算、R7 补偿、R9 明确恢复）：每一步都要可审计、不改写历史、无法证明的保持未知。CLI 只是把已有的受控方法交给运维，不新增绕过守卫的入口。

## 5. 用户决定（2026-10-09，均选推荐项）

1. **恢复时必须逐个手打原因**：每个原因一个 `--reason`，按顺序；`status` 列出当前原因供核对，没有“照抄全部原因”的捷径。
2. **沿用失败关闭**：CLI 写入之后，开着同一个账本的控制器下一次追加报 `E_LEDGER_STALE` 并失效，需要重开；不加持有标记。
3. **第一版只做五个命令**：`status`、`stop`、`settle`、`resolve`、`resume`；重启恢复仍由控制器在重启时自己跑。

下面是当时的选项，留作记录：

1. **恢复时的原因怎么给**：
   - 必须逐个手打 `--reason`；`status` 会列出当前原因，但没有“照抄全部原因”的捷径；
   - 或者提供 `--all-current-reasons` 一类的捷径。这会让比对形同虚设：恢复只能保证“读完之后没有新暂停”。
2. **控制器还开着账本时，CLI 写入怎么办**：
   - 沿用失败关闭：控制器下一次追加报 `E_LEDGER_STALE` 并失效，需要重开（不加新机制）；
   - 或者加一个持有标记，控制器开着账本时拒绝 CLI 的写入。
3. **第一版的范围**：
   - 只做第 3 节的五个命令；重启恢复仍由控制器在重启时自己跑（它才知道候选摘要怎么算）；
   - 或者把重启恢复也做进 CLI，这需要先定候选摘要的算法。

## 6. 规划端的决定（2026-10-09）

- C1：调度器新增两个只读方法，持久账本照 `operation()` 的样子各提供一个读访问器（实例不可用时报 `E_LEDGER_UNAVAILABLE`）：
  - `request_ids() -> tuple[str, ...]`：全部请求 id，按建立的顺序；
  - `operation_usage(operation) -> dict`：这个操作记下的每一项用量 `{维度: {"call_key", "revision", "amount", "final", "evidence_hash"}}`，一份新的拷贝；未知或不是精确 `str` 的操作报 `E_OPERATION_UNKNOWN`。
- C2：新模块 `sentinelmesh/ledger_cli.py` 提供 `register(subparsers)`，在 `smctl` 下挂 `ledger` 命令组；`cli.py` 的 `build_parser` 调用它。每个命令打开账本、最多做一次变更、在 `finally` 里关闭。
- C3：退出码与输出：
  - 成功：退出码 0，stdout 一行 JSON（`json.dumps(..., ensure_ascii=False, sort_keys=True)` 加换行）；
  - 账本拒绝（`ContractError`、`LedgerError`，包括打不开账本）：退出码 3，stderr 一行 JSON `{"error": code, "detail": detail}`，stdout 不输出；
  - 参数错误：沿用 argparse，退出码 2。
- C4：`status` 的 JSON：
  - 顶层：`ledger`（给定的路径）、`generation`、`seq`；
  - `requests`：按 `request_ids()` 的顺序，每项 `request_id`、`stopped`（布尔）、`stop_reasons`、`snapshot`、`attention`；
  - `attention`：按 `operation_ids()` 的顺序，只列需要运维处理的操作，每项 `operation_id`、`task_id`、`state`、`generation`、`supervised`（有没有运行描述）、`usage`（`operation_usage` 的结果）与 `reasons`。`reasons` 取下面三个值中成立的那些，按这个顺序：
    - `start_unknown`：状态是 `START_UNKNOWN`；
    - `undecided`：状态是 `RESERVED`、`HANDOFF_ACCEPTED` 或 `RUNNING`，代际比当前旧；
    - `usage_unknown`：状态属于 `UNRESOLVED`，并且预留里有一项用量不是“已知数额的最终值”（与 `_snapshot` 的口径相同）。
- C5：各变更命令一律用账本的当前代际，从不换代：
  - `stop` 输出 `stop_reasons`；
  - `settle` 调用单维度的 `settle`（`final=True`，`call_key` 用默认值），输出调度器返回的结果（`APPLIED`、`REPLAY` 或 `HISTORICAL_ONLY`）与 `operation_usage`；
  - `resolve` 输出操作视图；
  - `resume` 输出 `previous_reasons`。

## 7. 合同拆分

一份合同 `ledger-cli`：两个只读方法与读访问器、`ledger_cli.py` 与 `cli.py` 的挂接。oracle 在进程内调用 `cli.main` 捕获输出，另用一个子进程跑 `python -m sentinelmesh.cli ledger status` 验证真实入口；完整演示崩溃之后运维的全部步骤：查看、补记、裁定、恢复，再接着领取。

## 8. 不做

- 不改实现语言（用户决定，2026-10-09）。
- 不做提额、不核验运维的权限与证据。
- 不做交互式界面；不联网。
