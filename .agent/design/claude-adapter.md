# 第二个 CLI adapter：Claude Code 的 stream-json

状态：完成（2026-10-16）。用户决定见第 5 节，录制与合同的结果见第 9 节。

## 1. 为什么做

`smctl run once` / `serve` 现在认两个 adapter：
- `offline`：离线桩；
- `pi`：把 pi 的 `--mode json` 会话 v3 帧翻译成规范事件，再结算用量。

“真实的异构 provider 链”一直写在 `IMPLEMENTATION_STATUS.md` 的“不能证明”里。只有一种真实 CLI 时，adapter 协议、注册表与用量结算有没有偷偷依赖 pi 的形状，测不出来。

2026-10-16，用户给出“3 → 2 → 1”，这是第 1 项。第 3、2 项（Jev 影子决策、账本补读与 serve 轮询）已经完成。

## 2. 现状（2026-10-16 核对）

**adapter 与 pi 的先例**
- `run_cli._ADAPTERS` 按 adapter 自己的 id 精确匹配：`offline`、`pi`。作业文件写 `adapter_id`，`once`/`serve` 每次运行实例化一个 adapter。
- `pi_events.PiNativeAdapter` 是先例：
  - 翻译器很薄，只做帧到规范事件的映射；配对、顺序、终止都交给 `adapter_protocol` 的参考归约器；
  - 未知帧、畸形帧都失败关闭：变成 `unknown` 记录，不结算；
  - 用量只从权威的观测里取，`cost` 不是维度；
  - 夹具来自调度器录下的真实流（`tests/fixtures/pi_native/`，`reduce.py` 缩减），oracle 用夹具对照调度器自己的数。
- `live-pi-run`（2026-10-11）是唯一一次真实 provider 调用：
  - 用户批准一次；
  - 规划端用 `smctl run init/once` 跑 node.exe + pi 的 `cli.js`，单轮、无工具，提示是 `Reply with exactly: OK`；
  - 录下的流缩减成夹具，测试重放它得到同样的结算。

**本机的 CLI**
- Claude Code 2.1.301：`~/.local/bin/claude.exe`，原生 PE32+ 可执行文件。Windows 监管器能直接用 exe 的摘要确认身份，不需要像 pi、Codex 那样点名 node.exe 加脚本。
- Codex CLI 0.163.0：npm 全局安装，`codex.cmd` 转发给 node。

**Claude Code 的非交互参数**（`claude --help`）
- `-p`；`--output-format stream-json`（要配 `--verbose`）；
- `--tools ""`：禁用全部内置工具；
- `--setting-sources ""`：不加载 user、project、local 设置，所以也没有那里的 hook；
- `--strict-mcp-config`：不加载 MCP；
- `--no-session-persistence`；
- `--max-budget-usd`：花费上限。
- `--bare` 不能用：它只认 `ANTHROPIC_API_KEY`，不读 OAuth 登录，而本机是用 `/login` 登录的。

**stream-json 的形状**
- 按 Claude Code 与 Agent SDK 的文档，每行一个 JSON 对象，`type` 有 `system`（`subtype: "init"`）、`assistant`、`user`、`result`；
- `result` 是终止消息，带 `subtype`（`success` 或错误类）、`is_error`、`usage`（`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`）与 `total_cost_usd`；
- 这些都要用一次真实录制来核对，不凭文档写死。

## 3. 方案草图

分两步：先录、再写。

### 3.1 录一份真实流（规划端，需批准，第 5 节第 2 项）

**怎么录**：照 `live-pi-run` 的做法，规划端在一个空的临时目录里，用 `smctl run init/once` 跑一次：
- 作业的 argv 是 `claude.exe -p "Reply with exactly: OK" --output-format stream-json --verbose --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence --max-budget-usd 0.5`；
- 环境映射只给 Claude Code 找到自己登录状态所需的那几个变量（`USERPROFILE`、`APPDATA`、`LOCALAPPDATA`、`PATH` 等），不给任何 key。

**怎么处理录下的流**：
- 先在本地检查有没有凭据形状的字符串；
- 再缩减成 `tests/fixtures/claude_stream/<名>.jsonl` 与 `.meta.json`：保留每一帧的结构，截短长文本，记原长度与摘要；
- 旁边记下 CLI 自己报告的用量。

**为什么还要单独录一份**：上面这次 `once` 用的是 `offline` adapter（还没有 claude adapter），所以运行会是 `REVIEW_PENDING`，用量记为未知。需要的只是那份原样的 stdout。

### 3.2 翻译器与注册（合同，派给 Pi）

**翻译器**：新模块 `claude_events.py`，方言 `claude-stream-json`，结构照 `pi_events.py`：
- `system/init` 映射成 `run_started`；
- `assistant` 映射成 `assistant`（只保留结构字段，文本不进事件）；
- 终止只认 `result`：`subtype == "success"` 且 `is_error` 为假是成功终止，其余是 `error` 终止；
- 用量取 `result.usage` 的四个 token 维度，以及按维度相加得到的 `total_tokens`；它们是整个会话的合计，不从各条 `assistant` 消息累加，以免和合计重复计算；
- 未知 `type`、畸形帧、终止之后的帧都失败关闭，与 pi 的规则相同。

**注册**：`ClaudeStreamAdapter(OfflineAdapter)` 以 id `claude` 加进 `run_cli._ADAPTERS`，`once`、`serve`（含 `--parallel`、`--watch`）照常可用。

**oracle**：
- 用录下的夹具，经真实 `smctl run once` 重放（`python -c` 子进程原样写出夹具），结算出夹具旁记的数；
- 截断的流（没有 `result`）进入复审、用量未知；
- 另有手写的失败关闭用例：未知类型、畸形帧、终止之后的帧、错误终止；
- `offline` 与 `pi` 的流经 `claude` adapter 是 `result_error`，反之亦然。

## 4. 证据边界

- 真实调用只有第 3.1 节那一次，而且要用户批准。用的是用户当前登录的 Claude 账户；单轮、无工具、空目录，花费上限 0.5 美元。
- 其余全部离线：合同的测试只重放夹具。
- 凭据：作业只给变量名，不读、不打印 Claude Code 的登录文件。录下的流在提交前检查凭据形状。
- 一份录制只覆盖“单轮、无工具”的形状。工具调用、多轮、重试、压缩这些帧都没有样本，按未知帧失败关闭，与 pi 的处理相同，写进“不能证明”。

## 5. 用户决定（2026-10-16，均选推荐项）

1. **CLI**：Claude Code。
2. **录制**：批准一次最小调用（空目录、单轮、禁用工具、不加载设置与 MCP，提示 `Reply with exactly: OK`，花费上限 0.5 美元）。
3. **用量**：只记 token 四个维度加合计；CLI 报告的美元费用只进夹具的 meta。
4. **范围**：只做翻译与结算，新 adapter id `claude`。

## 6. 规划端决定

- 录制先于合同：合同的 oracle 与 Repository facts 要写真实帧的字段名，不写文档上的猜测。录不到（未批准）就退回手写夹具，并在合同里写明。
- 翻译器不读 `total_cost_usd` 之外的价格信息，也不调用任何网络。

## 7. 合同拆分

- 录制：规划端完成，一个证据提交，不是合同。
- `claude-adapter.v1`：
  - 新模块 `claude_events.py`；
  - `run_cli._ADAPTERS` 加一项；
  - 执行器测试；
  - 规划端先写 oracle 与草稿变异，`local_branch, merge`，时限按 serve-watch 的教训给 3000 秒。

## 8. 不做

- 工具调用、多轮、重试、压缩帧的翻译（没有样本）。
- 让 Claude Code 当 KitExperiment 的执行器：本合同只做 adapter，不改调度器。
- 读取或管理 Claude Code 的登录与凭据。

## 9. 进度与遗留

- 2026-10-16：核对现有 adapter、pi 的先例与本机的 CLI，写成本说明。
- 2026-10-16：用户决定第 5 节四项（均选推荐项），批准一次调用。
- 2026-10-16：录制完成。
  - 先干跑 `claude.exe --version`：`smctl run once` 在 Job Object 下启动，`REVIEW_PENDING`、退出码 0；观测记录的 `executable_sha256` 就是 `claude.exe` 的摘要（`84d61feeb1a0…`）。环境映射只给九个路径类变量，没有任何 key，Claude Code 自己读 OAuth 登录。
  - 再做那一次调用：退出码 0，`stderr` 为空，stdout 4 帧——`system`（`init`）、`assistant`（回答 `OK`）、`rate_limit_event`（文档里没有提到）、`result`（`subtype: success`、`is_error: false`、`num_turns: 1`）。`result.usage` 是输入 2、输出 4、缓存读 649、缓存写 2330；CLI 报告的费用 0.0188578 美元，模型是账户的缺省模型。
  - 流里没有凭据形状的字符串。`tests/fixtures/claude_stream/reduce.py` 把它缩减成 `windows-claude2_1_301-live-a001.jsonl` 与 `.meta.json`：超过 32 个字符或像路径的字符串整串换成长度加摘要，限额使用率的 5 个数清零，所以夹具不需要任何 release-check 放行。
  - 作业文件、账本、原始证据都只在会话临时目录。
- 2026-10-16：规划端草稿（约 240 行）与 oracle `tests/test_claude_adapter_oracle.py`（12 个用例）。
  - 草稿在夹具上翻出 `run_started`、`assistant`、`usage`、`run_finished` 四个事件，忽略 1 个 `rate_limit_event`，结算 2985 个 token（2 + 4 + 649 + 2330），与 CLI 自己的报告一致。
  - 草稿变异 20 条：抓住 19 条；“不核对 adapter 身份”是等价变异（父类 `OfflineAdapter.run` 用同一个错误码再核对一次），预期漏掉。
  - 定稿时补的两处：异常帧之后的 `usage` 事件不能声称用量完整；翻译结果在有任何异常帧时 `usage` 为空（原型只看终止之前的异常帧）。
- 2026-10-16：`claude-adapter.v1` 派给 Pi，PASS。
  - a001 用时 997 秒（上限 3000 秒），`check --fresh` 8/8。
  - 候选变异 24 条：oracle 与执行器测试合起来抓住 22 条；两边都漏的 2 条都是等价变异，一条是 `OfflineAdapter.run` 自己再核对一次身份，另一条是第二个 `init`，它照样被行号检查拦下。
  - 独立复审 PASS，七条低级别发现。规划端收尾：
    - 修正 `run_cli` 注释与两处文档串的措辞；
    - `num_turns` 加 `MAX_SAFE_INTEGER` 上限：归约器拒收更大的整数，翻译器不再对它报告“用量完整”；
    - 补三例测试：块计数、非 ASCII 帧的摘要、`num_turns` 上限。
  - 提交：oracle `1350884`（含 `test_run_cli_pi.py` 改为三项注册表）、合同 `b9bc14c`、复审 `83cf383`、候选 `ec6881a`、收尾在其后。
  - 决策模型的变异等价判断（shadow）仍然对每条都答“不等价”，真等价的 C23 只给 0.10。
- 遗留：
  - 只有“单轮、无工具”的样本，工具调用、多轮、重试、压缩都按未知帧失败关闭；
  - `smctl ledger reference` 仍只认 `pi-session-v3`（用户决定第 4 项，未扩展）。
