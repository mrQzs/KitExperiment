# TASK: pi-acceptance

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: 979aea067fb5bf3595612a49e38aa3edf94e16d6
- risk_level: low
- authorized_actions: local_edit, local_test
- integration_actions: none
- executor: bb/step-5-preview · thinking high

## Goal
由 Pi `step-5-preview` high 为 KitExperiment 增加离线验收测试，覆盖固定基线、PlanningBundle/Contract IR、三个内置工作流和生产连接器 fail-closed 门禁。

## Non-goals
- 不修改 `sentinelmesh/` 产品实现、设计文档、AGENTS、合同或调度配置。
- 不调用真实供应商、不读取凭据、不联网、不安装依赖、不提交或合并。

## Scope
- allowed_new_files: `tests/test_pi_acceptance.py`
- allowed_modified_files: none
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/**`
- dependency_changes: forbidden

## Repository facts
- 入口：`sentinelmesh/` 标准库原型，设计材料位于 `code-kit-v2/`；现有离线测试在 `tests/`。
- 工具链：Python 3.9/3.12，无第三方依赖锁；pytest 已由当前环境提供。
- 当前基线已通过 45 项离线测试；本合同只增加一项验收测试，不改变产品实现。

## Behavior contract
- 离线 fixture 必须用明确的 ResourceRef、固定哈希和严格 JSON；不接受模型自述替代证据。
- 三个内置工作流必须保持 DAG/阶段顺序和能力锁；缺能力应拒绝。
- 合同 JSON/Markdown 必须同源；同一发布幂等键遇到不同审核载荷必须冲突。
- 生产连接器缺失或 synthetic 时保持 `PRODUCTION_BLOCKED`。

## Steps
1. 阅读 AGENTS.md、设计摘要和现有测试，确认基线与 scope。
2. 在允许的新测试文件中编写确定性 fixture，覆盖上述不变量。
3. 运行全部 validation commands，保留真实结果；不要修改产品代码。
4. 最后一条消息报告每个 definition-of-done 和未运行能力。

## Validation commands
- V01（必要，基线应失败）：`python -c "from pathlib import Path; p=Path(r'tests/test_pi_acceptance.py'); assert p.exists() and p.stat().st_size>0; print('PI_ACCEPTANCE_ARTIFACT_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`PI_ACCEPTANCE_ARTIFACT_OK`
- V02（必要）：`python -m pytest -q` · cwd：仓库根 · 超时：180s · 期望退出码：0
- V03（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V04（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0

## Definition of done
- D01：新增离线验收覆盖，验证基线、规划合同、工作流和集成门禁的关键不变量；证据：V01、V02。
- D02：保持依赖零、只读生产边界和 NOT_RUN/BLOCKED 语义；证据：V02、V04。
- D03：完整测试与编译检查通过；证据：V02、V03、V04。

## Stop conditions
- 需要修改允许范围外文件、安装依赖、访问凭据或启动真实 CLI。
- 任一生产能力只能靠合成证据通过，或测试语义不明确。

## Report back
最后报告逐项列出 D01–D03、真实命令结果、证据路径和未运行能力。

## Budget
- max_attempts_total: 1
- max_wall_seconds_per_attempt: 600
- max_wall_seconds_total: 600
- cost_budget: tracking-only
- 时间分配：调查 15%，实现 55%，检查 20%，报告 10%。

## Forbidden
- 不修改合同、AGENTS.md、`.agent/`、设计材料或 `sentinelmesh/`。
- 不删除、跳过或弱化既有测试；不提交、不推送、不合并、不部署。

