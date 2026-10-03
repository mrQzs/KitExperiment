# SentinelMesh reference implementation

这是 `code-kit-v2/sentinelmesh-design.md` 的标准库原型。它用于验证协议、锁、调度、投影、证据和调试边界，不会自动调用外部 AI CLI 或修改用户仓库。

## 快速验证

在仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q sentinelmesh tests
python -m sentinelmesh.cli --help
```

## 主要模块

- `core.py`：严格 JSON、固定序列化、ResourceRef、事件 envelope 和 seq 幂等。
- `evidence.py`：不可变原始证据 segment 和字节范围。
- `config.py`：profile/role 解析和本机覆盖白名单。
- `approvals.py`：输入锁、CAS、撤销和漂移检查。
- `plugins.py`：声明式 manifest、依赖图和 PluginLock。
- `adapter.py` / `runtime.py`：Pi/第二 CLI 描述符、调用构造和受控子进程。
- `scheduler.py`：DAG、领取、租约、预算、unknown usage 和恢复门禁。
- `state.py`：追加事件、游标、投影、brief 和重建。
- `verification.py`：测试目录、证据要求和 fail-closed 聚合。
- `observability.py`：结构化日志、指标和 DebugBundle。
- `migration.py`：before/after 预览、CAS 和用户修改冲突检测。
- `baseline.py`：只读 Git/工作区/工具链基线，固定摘要和漂移比较；没有 HEAD 时明确保持不完整。
- `protocols.py` / `planner_protocol.py` / `planner.py`：严格 PlanningBundle、Contract IR、草稿 CAS、双格式合同和发布收据。
- `planner_runtime.py` / `planner_projection.py`：规划就绪、结果完整性、预算边界和只读生命周期投影。
- `workflows.py`：受信的七插件目录和 bugfix/documentation/regression 三个 DAG 配方。
- `integration_records.py` / `integration_gates.py`：目标、检查、证据清单、覆盖率和离线/生产连接器门禁。

原型的单元测试是离线证据；它们不能替代设计文档要求的真实 CLI、三平台、生产授权、账单、迁移和断电测试。

新增命令入口：

```powershell
python -m sentinelmesh.cli baseline .
python -m sentinelmesh.cli workflow bugfix
python -m sentinelmesh.cli contract-render input.json --markdown contract.md --json contract.json
```

`baseline` 只读采样，不会创建提交；`workflow` 只解析内置清单和能力锁；`contract-render` 由同一
Contract IR 生成 JSON/Markdown 两个成员。任何缺少真实平台、供应商 CLI、凭据或生产连接器的结论都保持
`NOT_RUN`/`BLOCKED`，不会因为离线 fixture 或退出码 0 自动升级。

## 与 Code-Kit 的关系

`KitExperiment` 是 `Code-Kit` 设计的可运行 Python 参考实现。它复用相同的严格 JSON、资源引用、输入锁、插件锁、调度、投影和 fail-closed 验收原则，但保持依赖为零，便于在本地和 CI 中做确定性验证。适配器只负责构造受控调用和规范化事件，不会自动连接真实供应商或修改用户仓库。

新增边界检查后，配置、审批引用、插件 manifest、观察摘要、vendor 事件、进程调用和迁移输入都会在进入核心逻辑前拒绝错误类型、控制字符、非有限数值、缺失来源或内容漂移。
