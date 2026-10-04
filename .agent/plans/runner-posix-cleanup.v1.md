# TASK: runner-posix-cleanup

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 6b0de1435c4aaafa2b023f8e89101ca004ed649d
- risk_level: high
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
修正旧 `SubprocessRunner` 在 POSIX 上的两个实测缺陷，让规划端 oracle `tests/test_runner_posix_cleanup_oracle.py` 全部通过：
- 超时与取消时，`run()` 在有界时间内返回；
- 只有确认进程树已清净时才报 `cleanup="verified"`。

## Non-goals
- 不改 Windows 分支（`os.name == "nt"`）的行为；Windows Job Object 保持 NOT_RUN。
- 不改环境继承、argv/shell 处理、stdin 处理、`Invocation`/`ProcessResult` 的字段与校验、启动失败路径；不实现新的监督器、ProcessIdentity 采集或 Linux 支持。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、`sentinelmesh/adapter.py` 和 oracle 测试；不联网、不读取凭据、不启动真实 provider CLI。

## Repository facts
- 基线 `6b0de14` = master `cbb4bc4` + 规划端 oracle（`tests/test_runner_posix_cleanup_oracle.py`，7 个真实进程用例）。
- 设计与用户决定见 `.agent/design/posix-runtime-runner.md` 第 1、5 节。
- `SubprocessRunner.run`（`sentinelmesh/runtime.py`，`class SubprocessRunner` 内）现状：
  - 用 `start_new_session=True` 启动，组长 pid 就是进程组号；
  - 超时后对进程组发 SIGTERM，再用 `communicate(timeout=1.0)` 收尾；
  - 再超时只 `proc.kill()` 组长，然后调用无时限的 `communicate()`；
  - KeyboardInterrupt 分支是 `terminate()` 加无时限的 `communicate()`；
  - 无论结果如何都报 `cleanup="verified"`。
- 规划端实测（macOS，2026-10-04）：
  - 孙进程继承输出管道时，1 秒超时的 `run()` 阻塞了 20.1 秒；
  - 孙进程脱离管道时，2.0 秒返回 `verified`，但进程组仍存活。
- oracle 在基线上 7 项中失败 5 项（超时、脱离管道、逃出进程组、正常结束有残留、取消），另 2 项是回归保护。规划端用一份草稿修复验证过 7 项全部可以通过，草稿未提交。
- 现有测试没有断言 `cleanup` 或 `stop_reason` 的取值；`ProcessResult` 只要求它们是非空字符串。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1。

## Scope
- allowed_modified_files:
  - sentinelmesh/runtime.py
- allowed_new_files:
  - tests/test_runner_posix_cleanup.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/adapter.py`, `tests/test_runner_posix_cleanup_oracle.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
以下规则只适用于 POSIX 分支；`pgid` 指组长的 `proc.pid`。

1. **常量**：在模块级定义 `_TERM_GRACE_SECONDS = 1.0`、`_DRAIN_SECONDS = 2.0`、`_REAP_SECONDS = 2.0`。
2. **超时**：第一次 `communicate(input=..., timeout=invocation.timeout_seconds)` 超时后，按下面的顺序收尾：
   1. 对进程组发 SIGTERM，然后最多等 `_TERM_GRACE_SECONDS` 读完输出。读完则 `stop_reason="timeout"`。
   2. 否则对进程组发 SIGKILL，`stop_reason="timeout_killed"`，再最多等 `_DRAIN_SECONDS` 读完输出。
   3. 这次也没读完（说明进程组之外还有进程占着管道）时：
      - 最多等 `_DRAIN_SECONDS` 回收组长；
      - 关闭父进程持有的 stdout/stderr 管道端；
      - 保留已经读到的输出，并记为“未读完”。

   向进程组发信号时遇到 ProcessLookupError，按“组已不存在”继续。
3. **取消**：第一次 `communicate` 抛出 KeyboardInterrupt 时，执行与第 2 条完全相同的收尾。
   - `stop_reason` 一律为 `"cancelled"`（无论是否升级到 SIGKILL）；
   - `run()` 照旧返回结果，不重新抛出。
4. **清理状态**：
   - 满足两个条件才报 `cleanup="verified"`：
     - 输出已读完（第 2 条第 3 步没有发生）；
     - 在 `_REAP_SECONDS` 内确认进程组已空：每隔不超过 0.05 秒调用一次 `os.killpg(pgid, 0)`，直到它抛出 ProcessLookupError；PermissionError 视为组仍存活。
   - 否则报 `cleanup="incomplete"`。
   - 这条规则适用于 completed、timeout、timeout_killed 和 cancelled 四种结果；正常结束时组内仍有成员也报 `incomplete`，不主动杀它们。
   - 启动失败仍是 `cleanup="not_started"`。
5. **输出保留**：终止前已经写出的 stdout/stderr 字节必须出现在返回结果里，包括第 2 条第 3 步的情形。
6. **其他行为不变**：
   - Windows 分支保持基线行为（可以原样移到一个私有方法里）；
   - 启动失败、stdin 写入、环境合并、argv/shell、`ProcessResult` 的构造与字段都不变；
   - 正常结束的 `stop_reason` 仍为 `"completed"`。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 1 与第 5 节、oracle 测试文件和 `SubprocessRunner` 的现有代码。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 修改 POSIX 分支：第 1–4 条，并保证第 5、6 条成立。
3. 新建 `tests/test_runner_posix_cleanup.py`，覆盖下列 4 个 oracle 没有覆盖的用例：
   - 正常结束时 `input_bytes` 照常送达，输出照常捕获，`cleanup="verified"`；
   - 不存在的可执行文件：`started=False`、`stop_reason="start_failed"`、`cleanup="not_started"`；
   - 升级到 SIGKILL 后，`exit_code == -signal.SIGKILL`，且 `duration_seconds >= timeout_seconds`；
   - 组长先写出 `early` 并 flush，再忽略 SIGTERM 睡眠：超时后 `stdout` 仍含 `early`，`stop_reason="timeout_killed"`。

   每个用例都要在 `addCleanup` 中对自己启动的进程组发 SIGKILL，并在 Windows 上 skip。
4. 运行 V01–V05。oracle 失败时改实现，不要改 oracle。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_runner_posix_cleanup_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_runner_posix_cleanup.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V04（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V05（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0

V02 在基线上失败是因为测试文件还不存在；V03 在基线上失败是因为 oracle 失败。

## Definition of done
- D01：超时与取消的有界收尾（Behavior contract 第 1–3 条）；证据：V01。
- D02：清理状态只在确认清净时为 verified（第 4 条）；证据：V01、V02。
- D03：输出保留与其他行为不变（第 5、6 条）；证据：V02、V03。
- D04：所有修改都在允许集合内，oracle 未被改动；证据：scope。
- D05：无依赖变更、联网或凭据访问，可编译、无空白错误；证据：V04、V05、scope。

## Stop conditions
- 基线、oracle 或 `SubprocessRunner` 的现状与 Repository facts 不符。
- 必须修改允许范围之外的文件（包括 oracle）才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1500
- max_wall_seconds_total: 3000
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读合同、设计与 oracle 约 4 分钟；
  - 实现约 6 分钟；
  - 新测试约 6 分钟；
  - 运行检查约 2 分钟（oracle 约 15 秒，全量约 20 秒）；
  - 执行报告约 3 分钟；
  - 合计约 21 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、oracle 测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
