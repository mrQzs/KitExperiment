# 执行环境记录

<!-- 由 check-pi-env 技能生成或更新。只记录事实，不写 API key、令牌或完整环境变量。 -->

- 记录时间：2026-10-04
- 记录者：Claude Code 2.1.289 规划会话（claude-opus-5-5）
- 结论：可以派遣（doctor 无问题，smoke 两步通过）

## 规划端

| 字段 | 值 |
|---|---|
| 工具与版本 | Claude Code 2.1.289 |
| 规划模型 | `claude-opus-5-5` |
| 模型配置来源 | `/model`（已保存为新会话默认） |
| 思考或推理强度 | effort `max` |
| 读取的规则文件 | 仓库 `AGENTS.md`（已提交，1309 bytes）；没有 CLAUDE.md。会话主目录是上级目录，AGENTS.md 由规划端显式读取 |
| 技能 | 仓库内没有 `.agents/skills`；使用 `../Code-Kit/.agents/skills` 的 check-pi-env、plan-task、dispatch-pi、review-pi |
| Codex 沙箱 / 审批 / 项目信任 | 不适用（本机有 codex-cli 0.157.1，本次未使用） |

## 执行端（Pi）

| 字段 | 值 |
|---|---|
| pi 版本 | 1.0.2（用户于 2026-10-04 升级；升级前本机为 0.87.1） |
| pi 调用方式 | `~/.nvm/versions/node/v24.11.1/bin/pi` |
| 执行模型 | `step/step-5-preview`（`pi --list-models` 精确 ID，命中 1 行） |
| 思考级别 | high。smoke 中提供商没有回报实际级别（`provider_thinking_levels` 为空），只能确认请求的级别 |
| 思考预算设置 | 未设置（使用默认） |
| 凭据就绪 | ready（`pi auth check --no-refresh`：`authType` 为 `api_key`） |
| 工具集 | 合同默认 read,grep,find,ls,edit,write,bash |
| Pi 可用的思考级别 | off, minimal, low, medium, high, xhigh, max |
| rg / fd | 可用：ripgrep 14.1.1、fd 10.3.0（`~/.stepcode/agent/bin`，在 PATH 上） |

## 上下文与配置来源

| 来源 | 路径 | 说明 |
|---|---|---|
| 全局上下文文件 | 无 | doctor 未发现 |
| 全局 SYSTEM.md / APPEND_SYSTEM.md | 无 | doctor 未发现 |
| 全局 settings.json 的键 | lastChangelogVersion, theme | 只记录键名 |
| worktree 所在目录链上的上下文文件 | 仓库 `AGENTS.md`（sha256 `2061c5eb…`） | worktree 默认位于系统临时目录的 `pi-worktrees/` 下 |
| 项目 `.pi/` 资源 | 无 | 调度器用 `--no-approve` 忽略 |
| 仓库 AGENTS.md（及 CLAUDE.md，如有） | AGENTS.md 已提交到 HEAD `af6c130` | 执行器可加载 |

## 操作系统与仓库

| 字段 | 值 |
|---|---|
| 系统与架构 | macOS 15.7.9（24G830）x86_64 |
| Shell | 交互 zsh；脚本用 `/bin/bash` 3.2.57 |
| Git | 2.50.1（Apple Git-155）；`core.autocrlf` 未设置 |
| Python（调度工具） | `/usr/bin/python3` 3.9.6；PATH 前置 venv 时为 3.12.14 |
| `.agent/runs/` 已被忽略 | 是 |
| 从 Windows 带来的状态 | 工作区 33 个文件只差 CRLF 行尾、`.git/worktrees` 有 4 条指向 Windows 临时目录的失效记录：都按用户决定保留。`.agent/runs/` 中历史 attempt 的 `meta.json` 记录 Windows 路径与 `git_autocrlf=true` |

## 项目工具链

| 工具 | 版本 | 来源 |
|---|---|---|
| Python | 3.12.14 | 共享 venv `../.venv`（uv 0.12.14 创建，位于两个仓库之外）；`pyproject.toml` 要求 >=3.10 |
| pytest | 9.1.1 | 同一 venv；系统 Python 没有 pytest |

本机没有 `python` 命令，而合同检查与 AGENTS.md 的验证命令写作 `python -m pytest -q`：调度器（以及它启动的 Pi 与检查命令）必须在 PATH 前置 `../.venv/bin` 后运行。

## 验证记录

| 项目 | 结果 | 证据 |
|---|---|---|
| doctor（不调用模型） | ok；唯一备注是仓库内没有 PI-Kit 技能（使用 Code-Kit 的技能） | `.agent/runs/_env/doctor.json` |
| 无工具连通性 | 通过：2.1 s，回复 `PI_SMOKE_OK`，实际模型 `step/step-5-preview` | `.agent/runs/_env/smoke.json` |
| 最小工具任务（写文件并读回） | 通过：5.2 s，write 与 read 各至少 1 次，工具错误 0 | 同上 |
| 本机基线测试 | venv 下 `python -m pytest -q` 65 passed；`python -m compileall -q sentinelmesh tests` 通过 | 本次规划会话 |
| 代表性代码任务 | 本机尚未派遣；此前的 attempt 在 Windows 上运行 | `.agent/runs/<task-id>/` |

## 变更记录

| 日期 | 变化 | 需要复评的内容 |
|---|---|---|
| 2026-10-04 | Windows x64：`bb/step-5-preview` · high，pi 1.0.1（Code-Kit doctor 核验），PowerShell Python 3.9.23 / Git Bash 3.12.3 | — |
| 2026-10-04 | 迁移到 macOS：执行模型改为 `step/step-5-preview` · high（用户同意；`bb` 在本机未配置，auth 为 `invalid_state`），pi 升级到 1.0.2，共享 venv 提供 `python` 与 pytest；doctor 与 smoke 通过 | pi、provider 或 venv 变化后重跑 doctor 与 smoke |

本文件记录调度输入，不证明原生平台或生产连接器能力；这些能力在有对应的取证或原生证据前保持 `NOT_RUN` / `BLOCKED`。
