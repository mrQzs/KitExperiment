# 设计说明：崩溃运行的参考用量 `smctl ledger reference`（单宿主）

状态：用户已决定第 5 节（2026-10-09，均选推荐项）；规划端决定见第 6 节。
前置：`smctl ledger`（`.agent/design/ledger-cli.md`）与 Pi 原生事件转换（`.agent/design/pi-native-events.md`）都已集成。

## 1. 为什么做

- 用户的遗留清单：“崩溃运行的用量要运维手工补记。”控制器在运行中途死掉时，账本只知道这个操作交接过（有运行描述）、用量未知；`smctl ledger status` 能指出是哪个操作、缺哪一维，但数额要运维自己去证据目录里翻 `stdout.log`——那是 Pi 的原生流，2–11 MB、几千到几万行——再把每条 assistant 消息的 usage 相加。
- pi-native-events 做的正是这件事：`translate(frames)` 从同一种流算出五个维度的累计（`usage`），并说明算得完不完整（`usage_complete`、`terminal`、`unknown`）。
- 用户给这个方向定的边界（2026-10-08）：“从输出流算出用量，只能作参考值。”所以这条命令只读、只算、只打印；补记仍由运维用 `settle` 手打数额与证据摘要，账本记下的是运维的决定，不是程序的推断。

## 2. 现状

- `smctl ledger` 有五个命令（`status`、`stop`、`settle`、`resolve`、`resume`）。`status` 列出需要运维处理的操作，每项带 `supervised`（有没有运行描述）、`usage`（已记用量）与 `reasons`，但不打印证据目录。
- 运行描述（restart-recovery L8–L9）：`{"spec": {request_id, run_id, command, cwd, environment_sha256, timeout_seconds}, "evidence_dir", "worktree_id"}`，`run_descriptor(op)` 从内存读，持久账本有同名读访问器。账本不记录这次运行用的是哪个 adapter、输出流是什么格式。
- 输出流是证据目录里的 `stdout.log`，由监督器以 `O_EXCL` 建立、子进程写入。`adapter_run.read_stream(evidence_dir, max_bytes, expected)` 失败关闭地读它：`O_RDONLY | O_NOFOLLOW | O_NONBLOCK` 打开、必须是普通文件、不超过上限（`max_bytes` 最大 `MAX_STREAM_BYTES` = 16 MiB）、每行一个 JSON 对象（协议的严格解析器），结果是 `ok`、`missing`、`unreadable`、`too_large`、`invalid`（带行号与错误码）或 `changed`（给了监督器的流事实而文件不是它）。它从不抛出，由调用方决定“不记用量”。
- 监督器记下的流事实（设备号、inode、字节数、SHA-256）只在收割之后写进观察记录；控制器死在运行中途时没有这份记录，参考值没有事实可比，只能自己算摘要。
- Windows 上没有 `os.O_NOFOLLOW` 与 `os.O_NONBLOCK`：`read_stream` 一调用就报 `AttributeError`；而且 `os.open` 不带 `O_BINARY` 时 C 运行库按文本模式读，CR LF 被折成 LF、遇到 0x1A 就停。本机（Windows Server 2025，Python 3.12.10）2026-10-09 实测：
  - 普通文件的 `os.lstat`、`os.stat`、`os.fstat` 三者的 `(st_dev, st_ino)` 相同；
  - 符号链接的 `lstat` 报 `S_ISLNK` 且带 `FILE_ATTRIBUTE_REPARSE_POINT`，身份与目标不同；打开它得到的 `fstat` 身份等于目标，不等于链接自己；
  - 硬链接与原文件身份相同（与 POSIX 一样，`O_NOFOLLOW` 也挡不住硬链接）；
  - 打开目录报 `PermissionError`；`O_BINARY` 读回的字节与写入的逐字节相同。
- 真实流的大小：`.agent/runs` 下 31 次运行的 `events.jsonl` 为 2.0–11.0 MB（最大是 pi-native-events a001，912 秒，11.0 MB）；五份缩减夹具各不超过 400 KB。
- `PiTranslation` 即使不完整也给出到此为止的累计（`usage`），并用 `usage_complete`、`terminal`、`unknown` 说明为什么不完整。

## 3. 拟议

### 3.1 命令

`smctl ledger reference LEDGER --operation OP --dialect pi-session-v3`

1. 打开账本（重放），取 `operation(OP)`（未知报 `E_OPERATION_UNKNOWN`）；
2. 取 `run_descriptor(OP)`，没有运行描述就拒绝：`E_NOT_SUPERVISED`（这个操作没交给过受监督的运行，没有流可读）；
3. 取 `operation_usage(OP)`；
4. `read_stream(evidence_dir, max_bytes=MAX_STREAM_BYTES)` 读 `stdout.log`（不给 `expected`：崩溃之后没有监督器的事实）；
5. 流是 `ok` 时按方言算参考值；否则参考值为 null，流的状态照实打印；
6. 打印一行 JSON，`finally` 里关闭账本。

全程不调用任何变更方法；证据目录也只读。流缺失、损坏或过大不是命令的失败：退出码 0，`stream.status` 说明原因；账本拒绝（包括 `E_NOT_SUPERVISED`）退出码 3；参数错误退出码 2。

### 3.2 输出

```
{"command": "reference", "operation_id", "request_id", "task_id", "state", "generation",
 "evidence_dir", "dialect",
 "reservation": {维度: 数额}, "recorded": operation_usage 的结果,
 "missing": [预留里还没有已知最终数额的维度，按预留顺序],
 "stream": {"status", "sha256", "bytes"}（invalid 时另有 "line" 与 "code"）,
 "reference": null 或方言给出的对象}
```

`pi-session-v3` 的参考值：

```
{"complete": 布尔, "reasons": [...], "terminal": "agent_settled" 或 null, "last_stop_reason",
 "usage": {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"},
 "assistant_messages", "tool_calls", "tool_errors", "agent_cycles", "frames", "unknown",
 "models": [[provider, model], ...]}
```

`reasons` 按这个顺序列出成立的项：`no_terminal`（没有 `agent_settled`）、`illegal_usage`（某条 assistant 消息的用量不合法，这条消息没计入）、`unknown_frames`（有未知或畸形帧）；`complete` 为真当且仅当 `reasons` 为空。`usage` 是合法 assistant 消息的累计，不完整时也给（第 5 节问题 3）。

运维接着做的事：把 `stream.sha256` 当作 `settle --evidence` 的证据摘要，按 `missing` 列出的维度逐项补记。

### 3.3 方言

账本不知道流是什么格式，命令也不猜：`--dialect` 必填，第一版只认 `pi-session-v3`（第 5 节问题 2）。规范化 adapter 协议的流目前没有真实的生产者，不做。

### 3.4 Windows 上读流

`read_stream` 在没有 `O_NOFOLLOW` 的平台上用等价的检查（第 5 节问题 4）：先 `lstat`（不是普通文件或带重解析点属性就是 `unreadable`），再以 `O_RDONLY | O_BINARY` 打开，`fstat` 的 `(st_dev, st_ino)` 必须等于 `lstat` 的，否则 `unreadable`；之后与 POSIX 共用同一段代码（事实比对、上限、读取、摘要、解析）。POSIX 分支逐字不变。

### 3.5 上限

沿用 `MAX_STREAM_BYTES`（16 MiB）：它保护运维进程的内存，和保护控制器是同一个理由；31 次真实流都在 11 MB 以内。超过上限的流报 `too_large`、没有参考值，运维只能另外处理；接近 1800 秒的运行可能超过 16 MiB（a001 按 912 秒 11 MB 推算），提高上限会同时改变 adapter 路径的边界，留作单独的决定。

## 4. 参考设计

07 第 8.3 节 R5（核对预算）：用量以权威事实为准，不能证明的保持未知，不能补 0；运维补记要可审计。参考值把“权威事实”的候选——流的摘要与按方言算出的数——摆在运维面前，补记本身仍是运维的、记名的、带证据摘要的决定。

## 5. 用户决定（2026-10-09，均选推荐项）

1. **入口**：单独的只读命令 `reference`；`status` 不读证据目录。
2. **方言**：`--dialect` 必填，第一版只认 `pi-session-v3`，不自动识别。
3. **不完整的流**：照样给出到此为止的累计，标明 `complete: false` 与原因；命令从不写账本。
4. **Windows 上读流**：用第 3.4 节的等价检查，POSIX 分支不动。

下面是当时的选项，留作记录：

1. **入口**：
   - 单独的只读命令 `reference`，一次看一个操作；`status` 不读证据目录（它是纯账本视图，不应因为某个证据目录打不开、过大而变慢或失败，也不必为了它指定方言）；
   - 或者 `status` 对每个 `supervised` 的关注操作顺带读流、附上参考值。
2. **方言**：
   - `--dialect` 必填，第一版只认 `pi-session-v3`；流的格式不记在账本里，让子进程写的字节决定自己怎么被解读是失败打开；
   - 或者按第一行自动识别（Pi 的 session 头 / 规范化协议的 `run_started`），识别不了才报错。
3. **不完整的流**（没有 `agent_settled`、某条用量非法、有未知帧）：
   - 照样给出到此为止的累计，标明 `complete: false` 与原因；运维自己判断，命令从不写账本。崩溃运行的流多半是被截断的，“至少消耗了多少”正是运维要看的；
   - 或者不完整就不给数字，只报原因。
4. **Windows 上读流**：
   - 用第 3.4 节的等价检查，整份合同（读流、算参考值、命令）都能在本机验证，POSIX 分支不动；
   - 或者非 POSIX 平台报 `unsupported`，读流的部分等回到 macOS 再验证，本机只能验证纯计算部分。

## 6. 规划端的决定（2026-10-09）

- R1：命令名 `reference`，参数 `LEDGER`、`--operation`、`--dialect`（argparse `choices`，目前只有 `pi-session-v3`）。打开、一次工作、`finally` 关闭，沿用 `_with_ledger`。操作处于任何状态都可以查（参考值只是信息），不限于需要运维处理的。
- R2：检查顺序：参数（argparse，退出码 2）→ 打开账本 → `operation`（`E_OPERATION_UNKNOWN`）→ 运行描述（`E_NOT_SUPERVISED`，以 `ContractError` 抛出，退出码 3，stderr 一行 JSON，与其他拒绝相同）→ `operation_usage` → 读流 → 参考值。
- R3：`evidence_dir` 按账本记下的原样使用，不规范化；相对路径按运维当前目录解析，所以输出里原样打印它。
- R4：读流不给 `expected`；`max_bytes` 用 `MAX_STREAM_BYTES`。非 `ok` 的流 `reference` 为 null、退出码 0；`stream` 只报 `status`、`sha256`、`bytes`，`invalid` 时再加 `line` 与 `code`（与 `run_adapter_operation` 的 `_stream_summary` 相同）。
- R5：`missing` 的口径与 `status` 的 `usage_unknown` 相同：预留里某一维没有观察、数额为 None 或不是最终值，就列出来；按预留的顺序。
- R6：参考值只用 `pi_events.translate` 的结果，不重算：`usage`、`terminal`、`last_stop_reason`、`assistant_messages`、`tool_calls`、`tool_errors`、`agent_cycles`、`frames`、`unknown` 逐项照抄；`illegal_usage` 看是否有 `assistant` 事件的 `usage` 为 None；`models` 转成列表的列表。
- R7：没有 `O_NOFOLLOW` 的平台（实际上就是 Windows）走第 3.4 节的分支：`os.lstat` 的 `FileNotFoundError` 是 `missing`，其他 `OSError` 是 `unreadable`；不是 `S_ISREG` 或 `st_file_attributes` 含 `FILE_ATTRIBUTE_REPARSE_POINT` 是 `unreadable`；打开用 `os.O_RDONLY | os.O_BINARY`；`fstat` 不是普通文件或 `(st_dev, st_ino)` 与 `lstat` 不同是 `unreadable`。模块导入仍无副作用；两个分支之后的代码共用。
- R8：证据分三类：五份缩减夹具经命令算出的参考值要与调度器的汇总一致（captured）；本机 `.agent/runs` 里最大的完整流（11 MB）经命令读取是规划端专用证据；Windows 分支的链接拒绝是本机原生证据（建符号链接需要权限，没有权限时跳过那一例，其余照跑）。
- R9：不改 `status`、`settle` 与账本；不给 `settle` 加“照抄参考值”的捷径。
- R10（a001 复审 F1，规划端裁定）：非 POSIX 分支在打开阶段遇到 `FileNotFoundError`（`lstat` 与 `open` 之间文件被删）报 `missing`，其他 `OSError` 报 `unreadable`，与 POSIX 分支对 `os.open` 的映射相同。合同第 7.3 条把 `FileNotFoundError` 混进了“其他 `OSError`”，规划端草稿与候选都按这里的读法实现；oracle 用 mock `os.open` 钉住两种映射。

## 7. 合同拆分

一份合同 `ledger-reference`：
- `sentinelmesh/ledger_cli.py`：`reference` 命令与 `pi-session-v3` 的参考值；
- `sentinelmesh/adapter_run.py`：`read_stream` 的非 POSIX 分支；
- 执行器测试 `tests/test_ledger_reference.py`。
- 规划端 oracle `tests/test_ledger_reference_oracle.py`：五份夹具经 CLI 与调度器汇总对照；不完整流的三种原因；流的各种状态（缺失、目录、符号链接、硬链接、过大、坏行、非对象行、CR LF 与 0x1A 字节原样）；拒绝与退出码；账本与流都没被写；每次打开的账本都关闭了；`python -m sentinelmesh.cli` 入口。
- 规划端专用：`tests/test_pi_events_captured.py` 增加一例，对本机最大的完整流跑一次命令。

## 8. 不做

- 不自动补记，不给 `settle` 加从参考值取数的捷径（用户 2026-10-08：只作参考值）。
- 不读任意路径的流（没有 `--stream PATH`）：参考值只对账本里有运行描述的操作算。
- 不做规范化 adapter 协议的方言；不把方言记进运行描述（要先给日志格式升版本）。
- 不提高 16 MiB 上限；不联网，不读凭据，不调用 pi。

## 9. 进度与遗留

- 已集成：oracle `02b04ef`、a001 `015fe05`、规划端补全 `1e5832e`；复审 `.agent/reviews/ledger-reference/attempt-001.md`（PASS，独立复审 PASS）；干净工作树全量 684 passed / 289 skipped（带 `.agent/runs` 时 688 passed / 286 skipped）。
- a001 的验收：`check --fresh` 8/8；两轮变异各抓住 32/34（oracle 加执行器测试、只用执行器测试），漏掉的两个是数额为 None 永远不是最终值、`lstat` 的普通文件检查被重解析点检查与打开遮蔽；规划端草稿阶段就已知。
- 遗留：
  - 16 MiB 上限（第 3.5 节）：接近 1800 秒的运行可能超过，提高上限要连同 adapter 路径一起决定；
  - POSIX 分支逐字不变，但本机跳过全部原生用例，回到 macOS 后跑一次 V03/V04；
  - `read_stream` 对含 NUL 的路径仍抛 `ValueError`（基线行为；账本里的证据目录不可能含 NUL）；
  - 只有 `pi-session-v3` 一种方言，规范化协议的流等有真实生产者再加；方言不记在账本里。
