# 复审记录：win-evidence · attempt-001

- 合同：`.agent/plans/win-evidence.v1.md`（plan `fd5b563`）；基线 `3b290ac`（master `6840d75` + oracle）
- 候选树：`2336d6e76410cc8c20eb3eee27ae551f11c6ae09`；变更 A2 M2（`sentinelmesh/windows_supervisor.py` 新 378 行、`tests/test_win_evidence.py` 新 369 行、`supervised_record.py` +77/−6、`supervised_operation.py` +51/−6）；越界 0
- 执行：step/step-5-preview · thinking high · Windows Server 2025 · pi 0.99.2 · 601.6 s / 1800 s · 退出码 0 · 事件流完整（agent_settled）· 工具错误 1（`edit` 锚点未命中，执行器随即改用正确锚点，无害）· 残留进程无
- 结论：**PASS**

## 1. 独立检查（`check --fresh`，新工作副本）

V01–V08 全部通过：oracle 18 passed / 67 subtests；执行器测试 8 passed / 4 subtests；V03 相关套件 93 passed / 158 skipped；全量 846 passed / 289 skipped；compileall、`git diff --check`、导入无副作用（`ctypes` 未加载）、V08（`posix_supervisor.py`、`adapter_run.py`、`process_identity.py`、`runtime.py` 与基线零差异）均通过。记录：`.agent/runs/win-evidence/attempt-001/checks/01/checks.json`。

## 2. 变异检查（固定候选，31 个故意偏差）

第一轮（oracle + 执行器测试）：抓住 30 / 漏掉 1（`mutations/01/`）。漏掉的 M22（`_stream_matches` 去掉 `read != fact["bytes"]`）是等价变异：lstat 的大小检查在前，读到的字节数不同时摘要不可能相等。与规划端草稿上的结果一致。

第二轮（只用执行器测试 `tests/test_win_evidence.py`）：抓住 10 / 漏掉 21（`mutations/02/`）。抓住：M03 pin 文本模式、M04 匹配检查文本模式、M09/M10 回滚先删后关或不关、M13 身份未知时按路径删、M14 不 lseek、M16 上限私有副本、M18 两流互换、M20 匹配忽略 lstat 身份与大小、M31 缺失记录报 unreadable。漏掉的 21 条分三类：
- 合同 Step 5 的清单没有要求、留给 oracle 的规则（20 条）：M01 悬空链接处的 lstat 先行；M02 写描述符的文本模式（清单只要求子进程输出含 CR LF/0x1A，没要求经写描述符写入 LF）；M05–M08 pin 的三重身份（需要假 lstat/open 才能分开）；M11/M12 `_remove_own_evidence` 的身份与重解析标志；M15 上限处的合法基线；M17 读失败 → None；M19/M21 匹配检查的普通文件与"打开的就是看到的"；M23/M24 畸形参数；M25–M27 记录读取的重解析点、身份与缺失；M28 partial 处的悬空链接；M29/M30 记录摘要的重解析点与身份。
- 等价变异（1 条）：M22，同上。
- 执行器用例因错误原因通过：无。

## 3. 执行器报告核对

- 自检表与 fresh 检查一致（V04 执行器 846/289，fresh 相同）。
- 两处 `contract_conflicts` 裁定：
  1. `_remove_own_evidence` 对 `lstat` 的 `ValueError`（含 NUL 的路径）也"不动"——合同第 6 条"永不抛异常"优先于"抛 OSError → 不动"的列举，执行器读法正确；规划端在合同里漏写了 `ValueError`。
  2. `_create_evidence` 的 `lstat` 抛 `ValueError` 时经第 4 条的 `BaseException` 回滚后原样 raise——`supervise`（第 3 份合同）在调用前校验 `evidence_dir` 为绝对存在目录，含 NUL 的路径到不了这里；与 POSIX `_open_evidence` 一致，接受。
- 可疑命令（事件流第 3261 行 `T=$(mktemp -d); … rm -rf "$T"`）：合同规定的截短写法，内容是只读探针（悬空链接处 lstat/O_EXCL、开着的文件 unlink），只删自己建的临时目录。
- 新增的 skip 标记只有一处 `os.name != "nt"`（开着的文件删不掉是 Windows 原生事实），符合清单。
- 限制项"模块 378 行超出约 220–300 行"：文档串完整，不是规则违背。

## 4. 独立复审（只读子代理）

复审者把候选树检出到自己的分离工作副本（`git write-tree` 复现 `2336d6e`），逐条核对合同第 1–12 条、两处 POSIX 分支（`git diff -w` 零删除行）、范围、测试文件在 POSIX 上的可运行性，并做了五个探针。结论 **PASS**，发现：
- F1（低）：`supervised_operation.py` 的模块文档串没有补非 POSIX 段落（合同第 12 条要求两处都补；`supervised_record.py` 补了）。规划端补全。
- F2（低）：执行器的两处"已被占"用例都放普通文件，裸 `O_EXCL` 本来就拒绝，去掉 lstat 先行或 `_entry_exists` 也能过——这正是合同 Step 5 的写法，悬空链接的变体由 oracle 覆盖（变异 M01/M28 第一轮抓住）。记录为用例区分力的说明，不是缺陷。
- F3（信息）：两处 `ValueError` 的裁定探针证实不泄漏描述符；oracle 与执行器测试都没覆盖 NUL 路径进这两个函数。
- F4（信息，给第 3 份合同）：`_abandon_evidence` 关掉的描述符号会被下一次 `os.open` 复用（探针：关掉 3/5/4/6 后再开得到 3），Windows 的 `supervise` 调用它之后必须把变量置 None，否则 `finally` 里的二次 `_close_fd` 会关掉别人的新描述符。
- F5（信息，给第 3 份合同）：子进程还持有继承来的写句柄时，父进程全部关闭后 `os.unlink` 仍是 `PermissionError`，`_remove_own_evidence` 静默留下文件；`supervise` 必须先终止 Job 并确认 `ActiveProcesses == 0` 再按身份删除，否则接受残留。
- F6（信息）：lstat 与 `O_EXCL` 之间被植入悬空链接的窗口：拒绝、不泄漏，但穿过链接建出的目标文件与链接都留着（身份删除看到的是重解析点）。设计上"永不按路径删除"接受此结果；规划端在模块文档串加一句。
- F7（信息）：模块 378 行，超出软指引，文档串所致。
复审者认为执行器自述数字准确、Step 5 七项齐全、两处分歧裁定成立；唯一夸大是 D05 的"两模块文档串各补一段"只对 `supervised_record` 成立（F1）。

## 5. 规划端补全（集成后单独提交）

- F1：`supervised_operation.py` 模块文档串补一段非 POSIX 读取规则。
- F6：`windows_supervisor.py` 模块文档串加一句 lstat 与 `O_EXCL` 之间的窗口及其结果。
- F4、F5 写进设计说明第 9 节，作为第 3 份合同 `win-supervise` 的事实。
