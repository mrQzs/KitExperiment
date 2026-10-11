# 复审记录：win-supervise · attempt-001

- 合同：`.agent/plans/win-supervise.v1.md`（plan `453b34f`）；基线 `7d3a302`（master `349d87c` + oracle）
- 候选树：`31ed5f0004031e45f7862b4cb692c283fbf9c698`；变更 A1 M1（`sentinelmesh/windows_supervisor.py` +530/−15、`tests/test_win_supervise.py` 新 301 行）；越界 0
- 执行：step/step-5-preview · thinking high · Windows Server 2025 · pi 0.99.2 · 1134.4 s / 1800 s · 退出码 0 · 事件流完整（agent_settled）· 工具错误 0 · 残留进程无
- 结论：**PASS**

## 1. 独立检查（`check --fresh`，新工作副本）

V01–V08 全部通过：oracle 22 passed / 13 subtests；执行器测试 7 passed；V03 相关套件 89 passed / 112 skipped；全量 875 passed / 289 skipped；compileall、`git diff --check`、导入无副作用（`ctypes` 未加载）、V08（`posix_supervisor.py`、`supervised_record.py`、`supervised_operation.py`、`process_identity.py`、`runtime.py`、`adapter_run.py` 与基线零差异）均通过。记录：`.agent/runs/win-supervise/attempt-001/checks/01/checks.json`。

## 2. 变异检查（固定候选，32 个故意偏差）

第一轮（oracle + 执行器测试）：抓住 31 / 出错 1（`mutations/01/`）。出错的 M13（回收轮询里的 `KeyboardInterrupt` 不吸收而是抛出）让 pytest 整个会话被中断（退出码 2），kit 按规则不算抓住：oracle 的用例应自己拦住冒出的中断再判失败。规划端补全把 `test_an_interrupt_during_the_reap_poll_is_absorbed` 改成 `try/except KeyboardInterrupt → self.fail(...)`，在集成后的干净工作副本上单独重跑 M13 确认抓住（见第 5 节）。草稿阶段 32/32 是因为规划端的草稿变异工具把任何非零退出码都算抓住，掩盖了这一点。

第二轮（只用执行器测试 `tests/test_win_supervise.py`）：抓住 16 / 漏掉 16（`mutations/02/`）。抓住：M06–M10 停止与回收的五条（退出码 1、不升级为 timeout_killed、不终止、不等组长、不杀残留）、M12 不报告残留、M14 取消报成 completed、M15/M16 清理标志与 START_UNKNOWN、M17 非零退出报 STOPPED、M18 不摘要流、M19 钩子失败留下文件、M20–M22 三种句柄不关、M31 回收窗口缩短。漏掉的 16 条全部是合同 Step 4 清单没有要求、留给 oracle 的规则：M01 平台守卫；M02–M04 挂起创建、先入 Job 再恢复、被拒进程终止（要靠假 API 或延时的假 `assign_and_resume`）；M05 退出码的数值（执行器只与常量比较）；M11 成员数读不到；M13 中断吸收；M23 导入时加载 ctypes（V07 守着）；M24 检查顺序；M25 凭据名；M26/M27 子进程的环境与 cwd；M28 身份里的 pid；M29/M30 启动失败的观察与异常类型；M32 KILL_ON_JOB_CLOSE。执行器用例因错误原因通过：无。

## 3. 执行器报告核对

- 自检表与 fresh 检查一致（V04 875/289，fresh 相同）。
- `contract_conflicts` 与 `unresolved_questions` 都为空；事件流第 28530 行是执行器自己核对 DoD 的叙述，不是分歧。
- 新增的 skip 标记只有一处 `os.name != "nt"`（整个执行器测试类，原生进程树），符合清单。
- 私有名字按合同固定（`_win32`、`_Job` 及其五个方法、`_start_in_job`、`_stop_job`、`_reap_job`），其余自定（`_WIN32`、`_Win32Bindings`、`_KILL_ON_JOB_CLOSE`、`_JOB_EXTENDED_LIMIT_INFORMATION`、`_JOB_BASIC_ACCOUNTING_INFORMATION`）；`OSError` 不带 errno（合同未要求）。
- 执行器的七个用例与清单一一对应：三层进程树超时、退出码 2 的组长留下孙进程、取消 + 孙进程、取消 + 不可确认的清理、钩子写文件后抛 `KeyboardInterrupt`、同目录第二次运行被拒、100 KB 流事实与句柄普查。

## 4. 独立复审（只读子代理）

复审者把候选树读进自己的分离工作副本（`git write-tree` 复现 `31ed5f0`），逐条核对合同第 1–12 条（含与 `posix_supervisor.supervise` 的逐行比较、15 行删除全是文档串/导入/`__all__`、证据一半字节不变）、执行器用例的清理与区分力、范围，并做了句柄普查与残留进程两个探针（每种结局 Δ0，超时的三层树立即全部消失）。结论 **PASS**，无高/中发现：
- F1（信息）：句柄普查依赖 CPython 的一次性开销——本进程第一次失败的 `CreateProcess` 让 `GetProcessHandleCount` 永久多 8（裸 `subprocess.Popen` 同样如此，之后再失败不再增加）。oracle 的普查用例里"启动失败"子用例只因为两个启动失败的用例按字母序先跑才通过；执行器的两处普查断言（热身后的正常运行、未到 `Popen` 的钩子回滚）不受影响。规划端补全：普查前先做一次失败热身。
- F2（低，设计遗留）：`Popen` 返回与 `assign_and_resume` 之间落下的 `KeyboardInterrupt` 会留下一个不在任何 Job 里、永远挂起的进程（POSIX 的同一窗口留下的是会跑完的进程）。合同未定义、oracle 钉不住；记入设计说明第 3.5 节"中断吸收"的遗留。
- F3（信息）：超时/取消路径上的 `stragglers_killed` 取决于等完组长后的成员数，实测 5/5 为 False，与 POSIX 一致；接线合同不要给这种断言去掉守卫。
- F4（信息，给接线合同）：`test_supervised_record.py` 直接从 `posix_supervisor` 导入 `supervise`，它与 `test_supervised_operation.py`、`test_adapter_run.py` 用 `os.killpg`/`SIGKILL` 清理，不是平台中立用例；`sentinelmesh/` 里没有代码解读负退出码或信号号，`runtime.py` 只要求 `int`，所以 3221225786 原样通过；替换 `Popen` 的假对象要带 `_handle`。
复审者认为执行器报告每一项都核对属实，没有夸大。

## 5. 规划端补全

集成后单独提交（只改 oracle，候选不动）：
- 变异 M13：`test_an_interrupt_during_the_reap_poll_is_absorbed` 自己拦住冒出的 `KeyboardInterrupt` 并判失败，在集成后的干净工作副本上单独重跑 M13 确认抓住。
- 复审 F1：`test_every_run_gives_its_handles_back` 在量基准前先做一次会失败的裸 `subprocess.Popen`（热身掉一次性的 +8），不再依赖用例顺序。
- F2 写进设计说明第 3.5 节的遗留；F3、F4 写进第 9 节给第 4 份合同。

Verdict: PASS
