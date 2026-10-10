# 设计说明：输出流的两道上限（stream-ceilings）

状态：待用户决定第 5 节；规划端决定见第 6 节。
前置：`adapter_run.read_stream`（stream-trust-boundary、adapter-supervised-run）、Pi 原生事件翻译（pi-native-events）与 `smctl ledger reference`（ledger-reference）都已集成。

## 1. 为什么做

- ledger-reference 的遗留：“接近 1800 秒的运行可能超过 16 MiB，提高上限要连同 adapter 路径一起决定”。本轮先量，再决定。
- 实测（`.agent/runs/*/attempt-*/events.jsonl`，pi_dispatch 捕获的 34 条真实 Step 流，规划端证据，不入库）：

  | 量 | 最大值 | 来自 | 上限 | 余量 |
  | --- | --- | --- | --- | --- |
  | 整流字节数 | 11,287,565 | ledger-compaction a001（1800 秒被时限切断，0 次压缩，没有 `agent_end`） | `MAX_STREAM_BYTES` = 16,777,216 | 1.49 倍 |
  | 整流字节数 | 11,040,707 | pi-native-events a001（912 秒，完整） | 同上 | 1.52 倍 |
  | 最长一行 | 719,200（`agent_end`） | resume-holds a001 | 协议单文档上限 1,048,576 | 1.46 倍 |

  `agent_end` 帧镜像整段对话，随对话增长，直到 pi 压缩上下文；被时限切断的运行没有它。一次跑满 1800 秒而且正常结束的运行，两道上限都只剩约 1.5 倍余量，而合同预算以后可能超过 1800 秒。
- 撞上任一上限的后果相同：`read_stream` 报 `too_large`（整流）或 `invalid`/`E_JSON_LIMIT`（单行），归约器拿不到事件，用量不记（失败关闭），操作留给运维结算；而 `smctl ledger reference` 用同样的上限读同一个文件，也只能报同样的答案、`reference` 为 null。运维没有任何工具支持的出路，只能手工数原始文件。

## 2. 现状

- `adapter_run.py`：`DEFAULT_MAX_STREAM_BYTES` = 1 MiB（调用方不给时的整流上限），`MAX_STREAM_BYTES` = 16 MiB（调用方最多能要的上限）；`_check_max_bytes` 要求精确 int 且 `1..MAX_STREAM_BYTES`，否则 `E_ADAPTER_SCHEMA`（`run_adapter_operation` 在移交之前检查）。`read_stream` 先 `fstat` 的大小、再最多读上限加一字节；超过是 `too_large`（`sha256`、`bytes` 都为 None）。
- 单行：`_parse_stream` 对每一行调 `core.strict_json_loads(segment)`，用的是协议的默认 `max_bytes` = `DEFAULT_MAX_JSON_BYTES` = 1 MiB、深度 64；超过报 `invalid`，带行号与 `E_JSON_LIMIT`。`strict_json_loads` 本身接受 `max_bytes` 参数。
- 调用方：`run_adapter_operation(..., max_stream_bytes=DEFAULT_MAX_STREAM_BYTES)`；`ledger_cli.cmd_reference` 用 `read_stream(evidence_dir, max_bytes=MAX_STREAM_BYTES)`；候选 1b 的接线是 `max_stream_bytes=MAX_STREAM_BYTES`。
- 翻译器不复制帧内容：`_message_end` 只数 text/thinking/toolCall 块、复制 stopReason/provider/model/responseId 与用量；`_agent_end` 忽略。翻译出来的规范化事件 payload 很小，规范化协议自己的 payload 上限（`adapter_protocol` 的 `check_json_value` 1 MiB）不受帧大小影响。
- 内存与时间（Windows，本机）：`read_stream` 读 11.3 MB 的真实流 2.49 秒，峰值内存 74 MB，约为原始字节的 6.5 倍（原始字节、按行切分的副本、解析出的字典）。
- 钉住数字的测试：`tests/test_adapter_run.py` 把 `16 * 1_048_576 * 2` 列为越界值，`tests/test_adapter_run_oracle.py` 把 `16 * 1_048_576 + 1` 列为越界值；`tests/test_ledger_reference*.py` 用常量 `MAX_STREAM_BYTES + 1` 造 `too_large`。

## 3. 拟议

### 3.1 整流上限

`MAX_STREAM_BYTES` 从 16 MiB 提高到 64 MiB（第 5 节问题 1）。它是“这个进程最多肯读多少子进程输出进内存”的护栏，不是预算：64 MiB 是 kit 在自己的时限下产出的最大流的 5.7 倍，按实测倍率峰值内存约 0.45 GB、解析约 14 秒，仍然有界。`DEFAULT_MAX_STREAM_BYTES` 保持 1 MiB（第 5 节问题 4）：不说就是小的，要大必须明说。

### 3.2 单行上限

`read_stream` 加关键字参数 `max_line_bytes`，默认 `DEFAULT_MAX_LINE_BYTES` = 1 MiB（等于协议的 `DEFAULT_MAX_JSON_BYTES`，行为不变），最大 `MAX_LINE_BYTES` = 8 MiB（第 5 节问题 2）；校验与 `max_bytes` 同样严格（精确 int、`1..MAX_LINE_BYTES`，否则 `E_ADAPTER_SCHEMA`，在打开文件之前）。`_parse_stream` 以它调 `strict_json_loads(segment, max_bytes=max_line_bytes)`，深度仍是 64。`run_adapter_operation` 加同名参数 `max_line_bytes`，移交之前检查。

这改的是“一行 Pi 原生帧最长多少”，不是协议：规范化事件仍由 `adapter_protocol` 用 1 MiB 的 payload 上限把守，翻译器又不复制帧内容，所以 8 MiB 的一行只会让更长的对话被翻译，不会让更大的 payload 进入账本。

### 3.3 两条路径共用

`cmd_reference` 用 `read_stream(evidence_dir, max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES)`；候选 1b 的接线相应是 `max_stream_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES`（第 5 节问题 3）。运维命令与控制器读同一个文件、用同一组护栏：控制器能记的用量，运维就能算出参考值；控制器记不了的，运维也只会看到同样的 `too_large`/`invalid`，不会出现“控制器不认、运维却算出一个数”的分歧。

### 3.4 不变的部分

- `read_stream` 的答案形状不变：`too_large` 仍是 `sha256`、`bytes` 为 None（没有读，就没有事实）；`invalid` 仍带 `line` 与 `code`。
- 读的顺序不变：`expected` 的身份与大小比较先于上限（大小不符是 `changed`，不是 `too_large`）。
- 协议常量 `DEFAULT_MAX_JSON_BYTES`、`DEFAULT_MAX_JSON_DEPTH`，`adapter_protocol` 的 payload 上限，监督器的摘要上限 `MAX_STREAM_DIGEST_BYTES`（256 MiB，远大于 64 MiB）都不动。

## 4. 参考设计

07 的输出流信任边界：子进程的输出是证据不是输入，读它的进程要有界、失败关闭、从不推断。两道上限都保留，只是按实测把护栏放到有余量的位置，并让运维路径与控制器路径一致。

## 5. 用户决定（2026-10-10，均选推荐项）

1. **整流上限提高到 64 MiB**（峰值内存约 0.45 GB，是实测最大流的 5.7 倍）。
2. **单行上限**：`read_stream` 加 `max_line_bytes`，默认 1 MiB、最大 8 MiB，Pi 路径与 `reference` 用 8 MiB。
3. **作用范围**：adapter 运行路径与 `reference` 用同一组常量。
4. **默认值**：`DEFAULT_MAX_STREAM_BYTES` 保持 1 MiB，大的上限必须由调用方明说。

下面是当时的选项，留作记录：

1. 64 MiB；或者 32 MiB（约 0.22 GB，2.8 倍）；或者保持 16 MiB，只做单行上限。
2. 加 `max_line_bytes`；或者保持协议默认 1 MiB（长对话正常结束的 `agent_end` 一行超过 1 MiB 时整条流 `invalid`）。
3. 两条路径共用（对同一文件给同一答案）；或者只提高 `reference`，控制器路径不动。
4. 默认保持 1 MiB；或者默认也提高到新上限。

## 6. 规划端的决定（2026-10-10）

- S1：常量 `MAX_STREAM_BYTES = 64 * 1_048_576`；新增 `DEFAULT_MAX_LINE_BYTES`（等于 `core.DEFAULT_MAX_JSON_BYTES`，1 MiB）与 `MAX_LINE_BYTES = 8 * 1_048_576`，两者进 `__all__`；`DEFAULT_MAX_STREAM_BYTES` 不变。
- S2：`read_stream(evidence_dir, *, max_bytes=DEFAULT_MAX_STREAM_BYTES, expected=None, max_line_bytes=DEFAULT_MAX_LINE_BYTES)`；检查顺序 `max_bytes` → `max_line_bytes` → `expected`，都在碰文件之前；`max_line_bytes` 的校验与 `max_bytes` 同样严格（精确 int、`1..MAX_LINE_BYTES`），错误码 `E_ADAPTER_SCHEMA`，消息里写明 `max_line_bytes`。两个上限互相独立，`max_line_bytes` 可以大于 `max_bytes`。
- S3：`_parse_stream(raw, max_line_bytes)` 调 `strict_json_loads(segment, max_bytes=max_line_bytes)`；深度仍是解析器的默认 64；`invalid` 的行号与错误码不变。
- S4：`run_adapter_operation` 加关键字参数 `max_line_bytes: int = DEFAULT_MAX_LINE_BYTES`，紧随 `max_stream_bytes` 之后、在 `supervised_spec` 与预约检查之前校验（移交之前拒绝，Scheduler 不变、证据目录为空）；两处 `_reduce` 都传它。
- S5：`cmd_reference` 用 `read_stream(evidence_dir, max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES)`；输出形状不变。
- S6：候选 1b 的接线句改为 `max_stream_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES`（只改设计说明）。
- S7：钉住旧数字的测试：`tests/test_adapter_run_oracle.py` 的越界值 `16 * 1_048_576 + 1` 与 `tests/test_pi_events_captured.py` 的措辞由规划端随 oracle 提交改；`tests/test_adapter_run.py` 的越界值表（`16 * 1_048_576 * 2` 现在在范围内）由执行器改，列入允许修改。
- S8：oracle 用合成的 Pi 形状的流（`agent_end` 加一个填充字段，精确到字节）证明单行规则，用小帧堆出的 17 MiB 流证明整流规则；真实流的大小只是规划端证据。

## 7. 合同拆分

一份合同 `stream-ceilings`（小）：
- `sentinelmesh/adapter_run.py`：常量、`max_line_bytes` 参数与校验、`_parse_stream` 传参、`run_adapter_operation`/`_reduce` 传参、`__all__`；
- `sentinelmesh/ledger_cli.py`：`cmd_reference` 用两个最大值；
- 调整钉住旧数字的现有测试（`tests/test_adapter_run.py` 的越界值表）；
- 执行器测试 `tests/test_stream_ceilings.py`；
- 规划端 oracle `tests/test_stream_ceilings_oracle.py`：常量的值；单行在 1 MiB 与 8 MiB 之间的真实形状的流（`agent_end` 镜像）默认 `invalid`/`E_JSON_LIMIT`、给了 `max_line_bytes` 后 `ok`，用量相同；整流在 16 MiB 与 64 MiB 之间可读、64 MiB 加一字节 `too_large`；`max_line_bytes` 的校验值表与检查顺序（在打开文件之前、与 `max_bytes` 同样的错误码）；`run_adapter_operation` 移交前拒绝；`reference` 读 1 MiB 以上的一行；`changed` 仍先于 `too_large`；默认值不变；规范化 payload 上限不受影响（翻译器不复制内容）。

## 8. 不做

- 不做流式逐行解析（内存倍率的大头是解析出的字典，归约器需要全部事件；省下的只有两份原始字节的副本）。
- 不改 `too_large` 的答案形状（不报没有读到的大小）。
- 不改协议常量、规范化事件的 payload 上限、监督器的摘要上限。
- 不联网、不读凭据、不调用 pi。

## 9. 进度与遗留

- 待用户决定第 5 节。
