# 执行环境记录

<!-- 由 check-pi-env 技能生成或更新。只记录事实，不写 API key、令牌或完整环境变量。 -->

- 记录时间：2026-10-09（UTC；本机 2026-10-08 晚）
- 记录者：Claude Code 2.1.295 规划会话（claude-fable-5-1）
- 结论：可以派遣（doctor 无问题，smoke 两步通过）

## 规划端

| 字段 | 值 |
|---|---|
| 工具与版本 | Claude Code 2.1.295 |
| 规划模型 | `claude-fable-5-1` |
| 模型配置来源 | `/model`（已保存为新会话默认） |
| 思考或推理强度 | effort `max` |
| 读取的规则文件 | 仓库 `AGENTS.md`（已提交，1623 bytes，sha256 `46fc1993…`）；没有 CLAUDE.md。会话主目录是上级目录 `Desktop/PLan`，AGENTS.md 由规划端显式读取 |
| 技能 | 仓库内没有 `.agents/skills`；使用 `../Code-Kit/.agents/skills` 的 check-pi-env、plan-task、dispatch-pi、review-pi（Code-Kit master `8fd2edb`，pi_dispatch 2.4.2） |
| Codex 沙箱 / 审批 / 项目信任 | 不适用（本机有 codex-cli 0.160.1，本次未使用） |

## 执行端（Pi）

| 字段 | 值 |
|---|---|
| pi 版本 | 0.99.2（本机 npm 全局安装；macOS 上最后一次记录为 1.0.4） |
| pi 调用方式 | `node` v24.19.0 运行 `~/AppData/Roaming/npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js`（doctor 的 pi_command） |
| 执行模型 | `step/step-5-preview`（`pi --list-models` 精确 ID，命中 1 行） |
| 思考级别 | high。smoke 中提供商没有回报实际级别（`provider_thinking_levels` 为空），只能确认请求的级别 |
| 思考预算设置 | 未设置（使用默认） |
| 凭据就绪 | ready（`pi auth check --no-refresh`：`authType` 为 `api_key`） |
| 工具集 | 合同默认 read,grep,find,ls,edit,write,bash |
| Pi 可用的思考级别 | off, minimal, low, medium, high, xhigh, max |
| rg / fd | 可用：`~/.pi/agent/bin/rg.exe`、`fd.exe`（pi 自带；`rg` 也在 PATH 上，`fd` 不在） |

## 上下文与配置来源

| 来源 | 路径 | 说明 |
|---|---|---|
| 全局上下文文件 | 无 | doctor 未发现 |
| 全局 SYSTEM.md / APPEND_SYSTEM.md | 无 | doctor 未发现 |
| 全局 settings.json 的键 | lastChangelogVersion, theme | 只记录键名 |
| worktree 所在目录链上的上下文文件 | 仓库 `AGENTS.md`（sha256 `46fc1993…`） | worktree 默认位于系统临时目录的 `pi-worktrees/` 下 |
| 项目 `.pi/` 资源 | 无 | 调度器用 `--no-approve` 忽略 |
| 仓库 AGENTS.md（及 CLAUDE.md，如有） | AGENTS.md 已提交到 HEAD | 执行器可加载 |

## 操作系统与仓库

| 字段 | 值 |
|---|---|
| 系统与架构 | Windows Server 2025 Standard 10.0.26100 x64（doctor：`Windows-2025Server-10.0.26100-SP0`），原生终端 |
| Shell | Git Bash 5.3.15（`x86_64-pc-cygwin` 构建）；PowerShell 7 |
| Git | 2.55.0.windows.5；`core.autocrlf=true` |
| Python（调度工具） | `python` 3.12.10（`C:/Program Files/Python312`）；`python3` 是应用商店占位程序，不要用 |
| `.agent/runs/` 已被忽略 | 是 |
| 仓库状态 | 2026-10-08 从 macOS 副本整体复制到 `Desktop/PLan`（Administrator 账户）；工作区干净，这个副本没有只差 CRLF 的文件；`.git/worktrees` 仍有 4 条指向旧 Windows 账户（MrQzs）临时目录的失效记录，按用户决定保留；`.agent/runs/` 中历史 attempt 的 `meta.json` 记录的是 macOS 与旧 Windows 路径 |

## 项目工具链

| 工具 | 版本 | 来源 |
|---|---|---|
| Python | 3.12.10（系统安装，没有 venv） | `pyproject.toml` 要求 >=3.10 |
| pytest | 9.1.1 | 系统 Python 的 site-packages |
| SQLite（Python 链接） | 3.49.1 | `sqlite3.sqlite_version` |

合同检查与 AGENTS.md 的验证命令写作 `python -m pytest -q`，本机的 `python` 就是 3.12.10，不需要前置 PATH。调度器在系统临时目录 `%TEMP%`（本机为 `~/AppData/Local/Temp/2`）下建 worktree。

## 验证记录

| 项目 | 结果 | 证据 |
|---|---|---|
| doctor（不调用模型） | ok；唯一备注是仓库内没有 PI-Kit 技能（使用 Code-Kit 的技能） | `.agent/runs/_env/doctor.json`（2026-10-09T01:15Z） |
| 无工具连通性 | 通过：2.5 s，回复 `PI_SMOKE_OK`，实际模型 `step/step-5-preview`，费用 unknown（Pi 无目录价格） | `.agent/runs/_env/smoke.json` |
| 最小工具任务（写文件并读回） | 通过：4.3 s，回复 `DONE`，工具错误 0 | 同上 |
| 本机基线测试 | 补 POSIX 跳过标记之前：557 passed / 251 skipped / 33 failed（全部是 POSIX 专用代码或夹具在 Windows 上没有跳过）；补标记之后（master `Skip POSIX-only tests on Windows`）：`python -m pytest -q` 551 passed / 286 skipped；`python -m compileall -q sentinelmesh tests` 通过 | 本次规划会话 |
| 代表性代码任务 | 本机（Administrator 账户）尚未派遣；此前的 attempt 在 macOS 与旧 Windows 账户上运行 | `.agent/runs/<task-id>/` |

本机跳过的 286 个用例覆盖：原生 POSIX 监督器、进程身份（PATH 规则、boot id）、持久账本的描述符与权限夹具、`read_stream` 的 `O_NOFOLLOW`。这些行在 IMPLEMENTATION_STATUS 中对 Windows 仍是 NOT_RUN；本机的全量通过不证明它们。

## 变更记录

| 日期 | 变化 | 需要复评的内容 |
|---|---|---|
| 2026-10-04 | Windows x64：`bb/step-5-preview` · high，pi 1.0.1（Code-Kit doctor 核验），PowerShell Python 3.9.23 / Git Bash 3.12.3 | — |
| 2026-10-04 | 迁移到 macOS：执行模型改为 `step/step-5-preview` · high（用户同意；`bb` 在本机未配置，auth 为 `invalid_state`），pi 升级到 1.0.2，共享 venv 提供 `python` 与 pytest；doctor 与 smoke 通过 | pi、provider 或 venv 变化后重跑 doctor 与 smoke |
| 2026-10-09 | 回到 Windows（Windows Server 2025，Administrator 账户）：pi 0.99.2，系统 Python 3.12.10 + pytest 9.1.1（没有 venv），`step/step-5-preview` · high 不变；doctor 与 smoke 通过（用户 2026-10-08 批准 smoke）；33 个 POSIX 专用用例补了跳过标记 | pi 升级或换回 macOS 后重跑 doctor 与 smoke；第一次派遣后核对 0.99.2 的事件流以 `agent_settled` 结束 |

本文件记录调度输入，不证明原生平台或生产连接器能力；这些能力在有对应的取证或原生证据前保持 `NOT_RUN` / `BLOCKED`。
