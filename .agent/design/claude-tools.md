# Claude Code 的工具调用与多轮

状态：设计中（2026-10-16），等用户决定第 5 节。

## 1. 为什么做

`claude-adapter`（2026-10-16 完成）让 `smctl run once` / `serve` 认得 Claude Code 的 `stream-json`，但只有“单轮、无工具”这一种形状：
- `assistant` 里只要出现 `tool_use` 块，或者流里出现 `user` 帧（工具结果就装在这里），翻译器都按未知帧失败关闭；
- 结果是运行进入复审、用量未知（`USAGE_UNKNOWN`）。

真实任务离不开工具，所以现在用 `claude` adapter 跑不了任何真实任务，只能跑“回一句话”。pi 那边早就能翻译工具帧（`tool_execution_start/end` 映射成 `tool_started` / `tool_finished`），两边差的正是这一块。

`claude-adapter` 第 9 节记的两个遗留，本方向一起处理：
- 工具调用与多轮没有样本；
- `smctl ledger reference` 只认 `pi-session-v3`。

## 2. 现状（2026-10-16 核对）

**翻译器 `claude_events.py`**
- `assistant` 帧只接受 `text` 与 `thinking` 块，其余块（`tool_use`、图片等）都是 `claude.unknown_frame`；
- 除了 `system/init`、`assistant`、`rate_limit_event`（忽略）、`result`，任何 `type`（包括 `user`）都是未知帧；
- 用量只取 `result.usage` 的四个维度，再加合计；
- `assistant_messages` 按帧计数，事件载荷里带 `message_id`。

**参考归约器 `OfflineAdapter.run`**（`adapter_protocol.py`），工具配对规则已经有：
- `tool_started` 需要合法的 `tool_id`，同一 id 未关闭前不能再开；
- `tool_finished` 必须关闭一个已开的 id；
- 终止时还有没关闭的工具，就拒收：`E_ADAPTER_TOOL`。

**pi 的先例**（`pi_events.py`）
- 工具事件只带 `tool_id`、`tool`（名字）与 `is_error`，不带输入和输出；
- 另外计 `tool_calls`、`tool_errors`。

**`smctl ledger reference`**（`ledger_cli.py`）
- `_DIALECTS = ("pi-session-v3",)`，`_REFERENCES` 只有 `_pi_reference` 一项；
- pi 的参考值是逐条 assistant 消息用量之和，流不完整时照样给数，并附 `reasons`（`no_terminal`、`illegal_usage`、`unknown_frames`）；
- 账本不记方言，由操作员用 `--dialect` 点名。

**本机 Claude Code 2.1.301 的相关参数**（`claude --help`，未调用 provider）
- `--tools <tools...>`：只开放列出的内置工具；
- `--allowedTools`：免确认的工具；
- `--restricted`：去掉运行命令或代码的内置工具（除非 `--tools` 点名），忽略 user、project、local 设置，把文件工具限制在工作目录里，拒绝 `bypassPermissions`。

**按 Claude Code 与 Agent SDK 的文档，工具相关的帧**（都要靠录制核对，不写死）
- `assistant` 的 `message.content` 里有 `tool_use` 块：`id`、`name`、`input`；
- 工具结果是 `type: "user"` 的帧，`message.content` 里是 `tool_result` 块：`tool_use_id`、`content`、可选的 `is_error`；
- 每帧有 `parent_tool_use_id`：主线程为 `null`，子代理（Task 工具）的帧不为空；
- 一条 assistant 消息的几个内容块可能拆成几帧输出，它们共用同一个 `message.id`，`usage` 也逐帧重复。

## 3. 方案草图

分两步：先录、再写。

### 3.1 录一份带工具的真实流（规划端，需批准，第 5 节第 1 项）

**怎么录**：照上一轮的做法，在会话临时目录里用 `smctl run init/once` 跑一次（`offline` adapter，只为拿到原样的 stdout）。
- 工作目录是一个新建的空目录，里面只有 `a.txt`，内容是一行无害的短文本。
- argv：`claude.exe -p "<提示>" --output-format stream-json --verbose --restricted --tools Read --allowedTools Read --setting-sources "" --strict-mcp-config --no-session-persistence --max-budget-usd 0.5`。
- 提示：先用 Read 读 `a.txt` 和 `missing.txt`（后者不存在），再只回答 `a.txt` 的第一个词。这样一次调用就能覆盖：
  - 一次成功的工具调用、一次出错的工具调用；
  - 至少两轮；
  - 工具之后的文本回答。
- 环境映射同上一轮：只给路径类变量，不给任何 key。

**怎么处理**：
- 先查有没有凭据形状的字符串；
- 再用现有的 `tests/fixtures/claude_stream/reduce.py` 缩减。它对所有帧通用：路径和长字符串换成长度加摘要。旁边记下 CLI 自己报告的用量。

**录完先核对**，结论写进第 9 节：
- 帧的实际字段；
- 多块消息是否拆帧；
- `result.usage` 是否等于逐条消息用量之和（同一 `message.id` 只算一次）。

如果 `result.usage` 不是整个会话的合计，回来再问第 5 节第 3 项。

### 3.2 翻译器扩展与 reference（合同，派给 Pi）

**`tool_use` 块**
- 映射成 `tool_started`，载荷 `{tool_id, tool}`；
- 只要 `id` 与 `name` 都是非空文本，`input` 不进事件；
- 同一帧里的 `text` / `thinking` 照旧计数。

**`user` 帧**
- 内容必须全是 `tool_result` 块。每块映射成 `tool_finished`，载荷 `{tool_id, tool, is_error}`：
  - `tool` 取之前那个 `tool_use` 的名字；
  - `is_error` 缺省视为假，出现时必须是布尔值。
- 下面几种都失败关闭：
  - `tool_use_id` 没开过；
  - 文本形式的 `user` 消息；
  - 其他块。

**仍失败关闭的帧**
- `parent_tool_use_id` 不为空（子代理）；
- `system` 的其他 `subtype`（压缩、hook 等）；
- `stream_event`；
- 图片与 `redacted_thinking` 块。

**计数**
- 新增 `tool_calls` 与 `tool_errors`；
- `assistant_messages` 改为按不同的 `message.id` 计数，若录制证实会拆帧。

**结算**
- 仍只取 `result.usage`（第 5 节第 3 项）；
- 终止时还有没关闭的工具，归约器照旧拒收。

**reference**
- `--dialect` 加 `claude-stream-json`，`_REFERENCES` 加一项；
- 有 `result` 时，参考值就是结算会用的那个数；
- 没有 `result` 时的规则见第 5 节第 4 项。

**oracle**（规划端先写）
- 录下的流经真实 `smctl run once` 重放，结算出 CLI 报告的数；
- 工具事件的个数与配对正确；
- 截断在工具调用中间的流，进复审、用量未知；
- 手写的失败关闭用例：
  - 未开过的 `tool_use_id`；
  - 重复的 `tool_use` id；
  - 文本形式的 `user` 消息；
  - 子代理帧；
  - `is_error` 不是布尔值；
- `reference` 在完整流与截断流上各给出什么；
- 单轮夹具的旧用例保持不变。

## 4. 证据边界

- 真实调用只有第 3.1 节那一次，要用户批准：
  - 用的是用户当前登录的 Claude 账户；
  - 只开 Read，工作目录是一个只有一个文件的空目录，`--restricted`；
  - 花费上限 0.5 美元。
- 其余全部离线：合同的测试只重放夹具。
- 凭据：作业只给变量名，不读、不打印 Claude Code 的登录文件。录下的流在提交前检查凭据形状。
- 一份录制只覆盖“只读工具、两轮左右”的形状。下面这些没有样本，仍按未知帧失败关闭，写进“不能证明”：
  - 子代理；
  - 压缩；
  - 重试；
  - hook；
  - 权限拒绝之后的流（`permission_denials`）；
  - 写文件与运行命令的工具。

## 5. 用户决定（待定）

1. **录制**：批准一次带只读工具的调用（推荐，第 3.1 节）／不录，照文档手写夹具。
2. **工具事件**：照 pi 映射成 `tool_started` / `tool_finished`，交给归约器配对（推荐）／只计数，不出事件。
3. **结算用量**：
   - 仍只取 `result.usage`（推荐）；
   - 另外核对逐消息之和（同一 `message.id` 只算一次），不相等就算用量未知；
   - 改为逐消息累加。
4. **没有 `result` 的流，reference 给什么**：
   - 逐消息用量之和，同一 `message.id` 只算一次，标 `no_terminal`（推荐，与 pi 的“流不完整照样给数”一致）；
   - 不给数，只报 `no_terminal`；
   - 这一轮不做 reference。

## 6. 规划端决定

- 录制先于合同（上一轮的教训）：oracle 与 Repository facts 写真实帧的字段名。
- 一个合同 `claude-tools.v1`，包括 `claude_events.py` 的扩展、`ledger_cli.py` 的方言与 reference，以及执行器测试；`local_branch, merge`，时限 3000 秒。
- 多命令变异写成 `all` 加 `executor` 两条命令。

## 7. 合同拆分

- 录制：规划端完成，一个证据提交，不是合同。
- `claude-tools.v1`：见第 6 节。

## 8. 不做

- 子代理、压缩、重试、hook、写文件与运行命令的工具（没有样本）。
- 在运行描述里记方言（要升日志格式），`--dialect` 仍由操作员点名。
- 让 Claude Code 当 KitExperiment 的执行器。
- 读取或管理 Claude Code 的登录与凭据。

## 9. 进度与遗留

- 2026-10-16：核对翻译器、归约器的工具配对规则、`ledger reference` 与本机 CLI 的参数，写成本说明。
