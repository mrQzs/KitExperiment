# Claude Code 的工具调用与多轮

状态：设计定稿（2026-10-16），第 5 节两次提问均已决定，规则见第 3.2 节。

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

### 3.2 翻译器扩展与 reference（合同，派给 Pi；2026-10-16 按录制与第 5 节定稿）

**`system` 帧**按 `subtype` 分：
- `init`：同前，唯一且是第一帧。
- `permission_denied`：`tool_use_id` 必须是非空文本，否则是畸形帧。合法时：
  - 不出事件，`ignored["system/permission_denied"]` 加一；
  - 另计 `permission_denials`。
- `informational`、`thinking_tokens`：不出事件，按 `system/<subtype>` 计入 `ignored`。
- `model_refusal_fallback`：`original_model` 与 `fallback_model` 都必须是非空文本，否则是畸形帧。合法时：
  - 不出事件；
  - `fallbacks` 加一，`fallback_model` 记进 `models`。
- 其余 `subtype`：未知帧。

**`assistant` 帧**
- 帧上有 `parent_tool_use_id` 且不为 `null`（子代理）：未知帧。
- 内容块：
  - `text`、`thinking` 照旧计数；
  - `tool_use` 的 `id`、`name` 都必须是非空文本，否则整帧畸形；
  - 本流里出现过的 `tool_use` id 再次出现（不论是否已关闭），整帧畸形；
  - 其余块：未知帧。
- 一帧要么整帧接受，要么整帧记为异常，不出半帧的事件。
- 接受时：
  - 先出一个 `assistant` 事件，id `L<n>`，载荷 `model`、`message_id`、`text_blocks`、`thinking_blocks`、`tool_use_blocks`；
  - 再按块的顺序，每个 `tool_use` 出一个 `tool_started`，id `L<n>.tool<k>`（k 从 1 起），载荷 `tool_id`、`tool`；
  - `tool_calls` 加上本帧 `tool_use` 块的个数。
- `assistant_messages` 按不同的 `message.id` 计数。

**逐消息用量**
- `message.usage` 的四个 vendor 键都是 `0..MAX_SAFE_INTEGER` 的整数时，按 `message.id` 记下；同一 id 后来的帧覆盖前面的。
- 否则这条消息记为“用量不合法”。帧本身不因此算畸形。

**`user` 帧**
- `parent_tool_use_id` 不为 `null`：未知帧。
- `message` 不是 `role: "user"` 的对象：畸形帧。
- `content` 不是列表：未知帧。
- `isSynthetic` 为 `true`：
  - 块全是 `text` 时，不出事件，计入 `ignored["user/synthetic"]`；
  - 否则是未知帧。
- 其余情况：
  - 块必须非空且全是 `tool_result`，否则是未知帧。
  - 每块的 `tool_use_id` 必须是一个已开启、尚未关闭的工具 id，否则整帧畸形。
  - 每块的 `is_error` 缺省为假，出现时必须是布尔值，否则整帧畸形。
  - 接受时，每块出一个 `tool_finished`，id `L<n>.tool<k>`，载荷 `tool_id`、`tool`（取开启时的名字）、`is_error`；`is_error` 为真的块计入 `tool_errors`。

**其余帧**
- `rate_limit_event`：同前。
- `result`：形状检查与终止规则同前。终止事件的载荷加 `tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`。
- 其他 `type`（`stream_event` 等）：未知帧。
- 终止之后的帧：同前。

**结算用量**（第 5 节第二次提问第 1 项）
- 来源是 `result.modelUsage`。它必须是非空对象；每个模型的 `inputTokens`、`outputTokens`、`cacheReadInputTokens`、`cacheCreationInputTokens` 都必须是 `0..MAX_SAFE_INTEGER` 的整数。
- 按维度把各模型相加，映射到 `input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`；四者之和是 `total_tokens`。每个和与总和都不能超过 `MAX_SAFE_INTEGER`。
- 任何一项不合法，用量就未知，运行照常结束。
- 不再读 `result.usage`。
- 任何异常帧照旧把结算用量清空。

**翻译结果新增字段**（追加在原有字段之后）
- `reported_usage`：`result` 合法时 `modelUsage` 的合计；不因异常帧清空；没有 `result` 或用量不合法时为 `None`。
- `message_usage`：逐消息用量之和，四维加合计，同一 id 只算一次。
- `illegal_message_usage`：用量不合法、没有算进去的消息数。
- `tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`。

**`smctl ledger reference --dialect claude-stream-json`**
- `reported_usage` 不为 `None` 时，参考值取它。`reasons` 只可能是 `unknown_frames`。
- 否则参考值取 `message_usage`，`reasons` 按下面的顺序列出：
  1. `no_terminal`：没有合法的 `result`；
  2. `illegal_usage`：`result` 的用量不合法，或者有消息的用量不合法；
  3. `partial_output`：参考值来自逐消息，输出只是下限；
  4. `unknown_frames`。
- `complete` 当且仅当 `reasons` 为空。
- 其余字段：`terminal`、`subtype`、`is_error`、`num_turns`、`assistant_messages`、`tool_calls`、`tool_errors`、`permission_denials`、`fallbacks`、`frames`、`unknown`、`models`。
- `pi-session-v3` 的参考值一字不改。

**oracle**（规划端先写）
- 录下的工具流经真实 `smctl run once` 重放，结算出 `modelUsage` 的合计 10 / 670 / 11602 / 3401（合计 15683）；
- 工具事件 4 对、配对正确；忽略与计数的种类和个数都对；
- 单轮夹具的结算不变；
- 截断在工具调用中间的流：进复审、用量未知；
- 手写的失败关闭用例：
  - 未开过的 `tool_use_id`；
  - 重复的 `tool_use` id；
  - 不带 `isSynthetic` 的文本 `user` 消息；
  - 子代理帧；
  - `is_error` 不是布尔值；
  - 畸形的回退帧与权限拒绝帧；
  - 其他 `system` subtype；
  - `modelUsage` 的各种不合法；
- `reference` 在完整流、截断流、用量不合法的流上各给出什么；
- 上一轮的 oracle 与执行器测试，凡是钉住旧语义的（`result.usage`、按帧计消息数、载荷与字段的精确集合、工具块与 `user` 帧算未知帧），由规划端在 oracle 提交里改钉。

## 4. 证据边界

- 真实调用只有第 3.1 节那一次，要用户批准：
  - 用的是用户当前登录的 Claude 账户；
  - 只开 Read，工作目录是一个只有一个文件的空目录，`--restricted`；
  - 花费上限 0.5 美元。
- 其余全部离线：合同的测试只重放夹具。
- 凭据：作业只给变量名，不读、不打印 Claude Code 的登录文件。录下的流在提交前检查凭据形状。
- 这一份录制覆盖了：只读工具、6 轮、一次权限拒绝、一次安全拒绝之后的模型回退、拆帧。下面这些仍然没有样本，按未知帧失败关闭，写进“不能证明”：
  - 子代理（`parent_tool_use_id` 不为空）；
  - 压缩，以及 hook、API 重试之类的其他 `system` subtype；
  - 回退时被撤回、却一直没有结果的工具调用：归约器在终止时会拒收（`E_ADAPTER_TOOL`），运行进复审；
  - 写文件与运行命令的工具。

## 5. 用户决定

第一次提问（2026-10-16，均选推荐项）：
1. **录制**：批准一次带只读工具的调用（第 3.1 节）。已录，见第 9 节。
2. **工具事件**：照 pi 映射成 `tool_started` / `tool_finished`，交给归约器配对。
3. **结算用量**：仍只取 `result.usage`，前提是“它是整个会话的合计”。录制推翻了这个前提（第 9 节），重新问。
4. **没有 `result` 的流，reference 给什么**：逐消息用量之和，同一 `message.id` 只算一次，标 `no_terminal`。录制显示逐消息的 `output_tokens` 不是终值（第 9 节），重新问。

第二次提问（录制之后，2026-10-16，均选推荐项）：
1. **结算用量**：取 `result.modelUsage` 各模型之和，也就是计费的全额。它包括被撤回的轮次和回退模型。缺失或畸形时用量未知。
2. **没有 `result` 的流，reference 给什么**：照样给逐消息之和，同一 `message.id` 只算一次，包括被撤回的消息；`reasons` 标 `no_terminal` 与 `partial_output`（输出只是下限）。有 `result` 时，取结算用的那个数。
3. **拒绝回退**：接受并计数。运行照常判成功或失败；记下回退次数；两个模型都列进 `models`；被撤回帧里的工具事件照旧配对。
4. **其余新见帧**（`permission_denied`、`informational`、`thinking_tokens`、带 `isSynthetic` 的 `user` 文本）：忽略并计数，权限拒绝另计 `permission_denials`。不带 `isSynthetic` 的 `user` 文本仍失败关闭。

## 6. 规划端决定

- 录制先于合同（上一轮的教训）：oracle 与 Repository facts 写真实帧的字段名。
- 一个合同 `claude-tools.v1`，包括 `claude_events.py` 的扩展、`ledger_cli.py` 的方言与 reference，以及执行器测试；`local_branch, merge`，时限 3000 秒。
- 多命令变异写成 `all` 加 `executor` 两条命令。
- 上一轮的 `tests/test_claude_adapter.py`（执行器写的）与 `tests/test_claude_adapter_oracle.py` 里钉住旧语义的断言，由规划端在 oracle 提交里改钉，合同把这两个文件列为受保护文件。执行器的新测试写进新文件 `tests/test_claude_tools.py`。
- 被撤回的帧不从事件里删掉：撤回改的是会话的上下文，不改已经发生的工具调用与计费。

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
- 2026-10-16：用户决定第一次提问的四项（均选推荐项），批准一次调用。
- 2026-10-16：录制完成。`smctl run once` 退出码 0，`REVIEW_PENDING`；`offline` adapter 报 `E_ADAPTER_EVENT`，用量未记。CLI 报告 `success`、`num_turns: 6`，费用 0.0504443 美元。stdout 22 帧，`stderr` 为空，没有凭据形状的字符串。缩减成夹具 `tests/fixtures/claude_stream/windows-claude2_1_301-tools-a001.*`，`reduce.py` 加了 `--source` 参数，并在 meta 里记下 `modelUsage`。

  **流的经过**比提示设想的乱，但正好碰到了真实运行里会有的几种帧：
  1. 第一次 Read 被 `--restricted` 拒绝，说“路径在工作目录之外”。原因是作业里的 `cwd` 由短路径展开，大小写和磁盘上不同（`Desktop-Plan` 对 `Desktop-PLan`）。看来 Claude Code 比较路径时区分大小写，这是它自己的问题。
  2. 接着 Opus 5.5 的安全分类器拦下了这条回答（`api_refusal_category: cyber`）。CLI 插入一条合成的 `user` 文本消息，重试一次，模型发出一个缺参数的 Read；然后转用 `claude-opus-4-8`，并撤回前面三帧。
  3. 回退之后的模型读到 `a.txt`；读 `missing.txt` 出错；最后回答。

  **帧的种类**
  - `system` 帧有五种 subtype：`init`、`permission_denied`（带 `tool_use_id`）、`informational`（通知文本）、`model_refusal_fallback`（带 `retracted_message_uuids`、`original_model`、`fallback_model`）、`thinking_tokens`（估计值，出现 3 次）。
  - `user` 帧有两种：
    - 工具结果：`tool_result` 块，失败的带 `is_error: true`，成功的不带 `is_error`；
    - 合成文本：`isSynthetic: true`，带 `refused_message_id`。
  - `assistant` 帧的块有 `text`、`thinking`、`tool_use`。回退后的第一帧带 `supersedes`，列出被撤回的帧的 `uuid`。
  - 所有帧的 `parent_tool_use_id` 都是 `null`。

  **拆帧**
  - 一条消息的每个内容块单独成一帧，共用 `message.id`，`usage` 逐帧重复。
  - 同一条消息的两个 `tool_use` 帧之间，夹着第一个工具的结果帧。工具在块到达时就执行。

  **用量**
  - 逐消息（同一 `message.id` 只算一次）：
    - 输入与两个缓存维度是终值；
    - `output_tokens` 是块写出时的计数：4 条消息合计 27，实际是 670。
  - `result.usage` 是 6 / 627 / 8253 / 3060（输入、输出、缓存读、缓存写），不含被撤回的那条消息。
  - `result.modelUsage` 按模型分列，两个模型合计 10 / 670 / 11602 / 3401：
    - 其中输入与两个缓存维度，正好等于逐消息之和；
    - 它的 `costUSD` 之和等于 `total_cost_usd`，所以它才是计费的全额。
  - 上一轮的单轮夹具里，`modelUsage` 与 `result.usage` 相等（只有一个模型，没有撤回）。所以改用 `modelUsage`，不会改变那一份的结算。
- 2026-10-16：用户就录制推翻的两项重问、新增的两项作答（均选推荐项），规则定稿为第 3.2 节（`4074012`）。
- 2026-10-16：规划端草稿（`claude_events.py` 约 400 行，`ledger_cli.py` 加约 30 行）与 oracle `tests/test_claude_tools_oracle.py`（17 个用例，`dc03ae0`）。
  - 同一提交改钉上一轮的 `tests/test_claude_adapter_oracle.py`（非法用量改在 `modelUsage` 上；矩阵里的工具块改成没有 id 的）与 `tests/test_claude_adapter.py`（两条消息不同 id、`result` 带 `modelUsage`、新字段、异常帧改用 `stream_event`、总和越界改在 `modelUsage` 上）。
  - 草稿在工具夹具上翻出 4 对工具事件，结算 15683（`modelUsage` 合计）；单轮夹具仍是 2985。
  - 基线上新 oracle 14 个用例失败，两个改钉的文件 4 个失败；草稿上 38 个用例全过。
  - 草稿变异 40 条：抓住 39 条；漏掉的 1 条是等价变异（各维之和的上限检查被总和的检查兜住）。
