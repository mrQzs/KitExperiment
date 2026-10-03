# TASK: pi-acceptance

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 3de150144b9a9478eca38200496faf19dbfe5f5c
- risk_level: low
- authorized_actions: local_edit, local_test
- integration_actions: none
- executor: step/step-5-preview · thinking high

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

## Behavior contract
- 离线 fixture 必须用明确的 ResourceRef、固定哈希和严格 JSON；不接受模型自述替代证据。
- 三个内置工作流必须保持 DAG/阶段顺序和能力锁；缺能力应拒绝。
- 合同 JSON/Markdown 必须同源；同一发布幂等键遇到不同审核载荷必须冲突。
- 生产连接器缺失或 synthetic 时保持 `PRODUCTION_BLOCKED`。

## Validation commands
- V01：新增测试文件存在并输出 `PI_ACCEPTANCE_ARTIFACT_OK`（基线应失败）。
- V02：`python -m pytest -q`，必须退出 0。
- V03：`python -m compileall -q sentinelmesh tests`，必须退出 0。
- V04：`git diff --check`，必须退出 0。

## Stop conditions
- 需要修改允许范围外文件、安装依赖、访问凭据或启动真实 CLI。
- 任一生产能力只能靠合成证据通过，或测试语义不明确。

## Report back
最后报告逐项列出 D01–D03、真实命令结果、证据路径和未运行能力。
