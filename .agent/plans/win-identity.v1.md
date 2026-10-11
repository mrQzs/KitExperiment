# TASK: win-identity

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 0591618aad3022ab9219dbc0c8ffa9d2e123b8b3
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/windows-supervisor.md` 第 3.2、3.3 节（用户决定第 5 节第 2、3 条）给 `sentinelmesh/process_identity.py` 加上 Windows 分支：`boot_id()` 在 `win32` 上把注册表里的 `MachineGuid` 与 `BootId` 原样拼成 `<guid>:<count>`；`resolve_executable` 在 `os.name == "nt"` 上按 Windows 规则解析（`;` 分隔且每项带盘符或 UNC 根的 PATH、裸名只补 `.exe`/`.com`、`.cmd`/`.bat` 拒绝、从不读 `PATHEXT`）。
- 只修改 `sentinelmesh/process_identity.py`；
- 让规划端 oracle `tests/test_win_identity_oracle.py` 全部通过；
- 新建执行器测试 `tests/test_win_identity.py`（清单见 Step 4，刻意不长）。

## Non-goals
- 不改 darwin、linux 分支与 POSIX 的 `resolve_executable` 文本；不改 `_BOOT_ID_PATTERN`、`_validated_boot_id`、`environment_digest`、`verified_environment`、`file_sha256`、`capture_process_identity` 的行为与签名。
- 不改 `ProcessIdentity` 的字段；不改 `runtime.py`、`posix_supervisor.py`、`supervised_record.py`、`supervised_operation.py`；不做 Windows 监督器本身（后续合同）。
- 不读 `os.environ`（PATH、PATHEXT 都不读）；不调 `shutil.which` 做 Windows 查找（它读 `os.environ["PATHEXT"]`）；不执行任何被解析的文件。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试；不联网、不读取凭据、不调用 pi。

## Repository facts
- 基线 = master `551e344`（设计说明 `.agent/design/windows-supervisor.md`：第 1 节实测、第 2 节现状、第 3.2/3.3 节方案、第 5 节用户决定、第 6 节规划端决定 W6、W7）+ 规划端 oracle 提交 `0591618`，后者含 `tests/test_win_identity_oracle.py`（22 个用例：用放进 `sys.modules` 的假 `winreg` 模块在任何平台上钉注册表规则——两个值按顺序读、键关闭、文本原样、不缓存、打不开或读不到或类型不对一律 `E_RUNTIME_IDENTITY`、guid 失败时不再读计数、只有 `win32` 走注册表；Windows 原生的 `resolve_executable` 规则——`.exe` 先于 `.com`、垫片与其它后缀各条路线都拒绝、`PATHEXT` 不读、不用 `which`、PATH 项必须有根、显式路径原样或接 cwd、无根与盘符相对路径拒绝、目录与缺失拒绝、链接解析到真实文件、敌意文本；本机原生——`boot_id()` 等于测试自己读注册表拼出的文本、`capture_process_identity` 四字段、解释器经自己的目录解析），并把 `tests/test_process_identity.py` 里“不支持的平台”例子从 `win32` 换成 `cygwin`。在基线上 oracle 的假注册表用例与原生用例失败（`boot_id` 报“不可用”，Windows 查找走 POSIX 规则）。
- `sentinelmesh/process_identity.py`（基线）：导入 `collections.abc, hashlib, json, os, re, shutil, sys` 与 `typing.Any, Mapping`；常量 `_BOOT_ID_PATTERN`（UUID，大小写都收）、`_DARWIN_BOOT_ID_SYSCTL`、`_LINUX_BOOT_ID_PATH`、`_SHA256_CHUNK_BYTES`；`_validated_boot_id(text, source)` 用 `_BOOT_ID_PATTERN.fullmatch`；`_darwin_boot_id()` 在函数内 `import ctypes`；`boot_id()` 依次判断 `sys.platform == "darwin"`、`sys.platform.startswith("linux")`，否则 `RuntimeErrorCode("E_RUNTIME_IDENTITY", "boot id is not available on this platform")`；`_validated_identity_text(value, label)`；`_path_lookup_value(environment)` 是 POSIX 的 PATH 规则（`:` 分隔、`/` 开头）；`resolve_executable(argv0, *, cwd, environment)` 先 `_validated_identity_text(argv0, "argv0")`、`_validated_identity_text(cwd, "cwd")`、`snapshot = _validated_environment(environment)`，然后一个 `try` 块：含 `/` 的 argv0 是路径（绝对原样、相对接 cwd），裸名 `shutil.which(argv0, path=_path_lookup_value(snapshot))`，`os.path.realpath`，`isfile` 且 `os.access(X_OK)`，`RuntimeErrorCode` 原样抛出，`(OSError, ValueError, UnicodeError)` 变成 `E_RUNTIME_IDENTITY`；`file_sha256(path)`；`capture_process_identity(pid, *, executable, worktree_id)` 返回 `ProcessIdentity(pid, boot_id(), file_sha256(executable), worktree_id)`。模块没有 `__all__`。`_validated_environment` 对值里的控制字符报 `E_RUNTIME_ENVIRONMENT`，这发生在任何路径逻辑之前。
- `sentinelmesh/runtime.py` 的 `ProcessIdentity.__post_init__` 只要求 `boot_id` 是非空标量文本（`_runtime_text`），不校验 UUID 形状，所以 `<guid>:<count>` 能进身份与账本。
- Windows 事实（规划端 2026-10-10 实测）：`HKLM\SOFTWARE\Microsoft\Cryptography` 的 `MachineGuid` 是 `REG_SZ`、36 字符小写 UUID；`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters` 的 `BootId` 是 `REG_DWORD`（本机 13）；`winreg` 在解释器启动时就已在 `sys.modules` 里（所以假模块要用 `mock.patch.dict(sys.modules, {"winreg": fake})` 替换，分支内的 `import winreg` 才会拿到它）；`os.access(X_OK)` 对任何存在的文件为真；`shutil.which` 读 `os.environ["PATHEXT"]`；`ntpath.join("C:\\dir", "C:tool.exe")` 给 `C:\\dir\\tool.exe`、`ntpath.join("C:\\dir", "\\x\\tool.exe")` 给 `C:\\x\\tool.exe`（所以盘符相对与无根路径必须在 join 之前拒绝）；`os.path.realpath` 返回磁盘上的真实大小写并解析符号链接与 junction；`os.path.isfile` 对含 NUL 的路径返回 False 而不抛；本账户能建符号链接。`python` 是系统 3.12.10，`pytest` 9.1.1。
- 规划端用一份草稿实现验证过（草稿未提交，约 140 行）：oracle 22/22；全量 803 passed / 289 skipped；30 个故意偏差里 27 个被 oracle 抓住，其余 3 个是等价变异（计数来自整数不可能有前导零、bool 计数会被形状检查挡住、Windows 分支里没有会抛 `OSError` 的调用）。

## Scope
- allowed_modified_files:
  - sentinelmesh/process_identity.py
- allowed_new_files:
  - tests/test_win_identity.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_win_identity_oracle.py`, `tests/test_process_identity.py`, `tests/test_process_identity_gaps.py`, `tests/test_process_identity_probes.py`, `tests/test_posix_supervisor.py`, `tests/test_posix_supervisor_oracle.py`, `sentinelmesh/runtime.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/supervised_record.py`, `sentinelmesh/supervised_operation.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/core.py`, `sentinelmesh/cli.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有“是 int”“是 str”都是精确类型（bool 不算 int，str 子类不算 str）。私有名字可自定，公开名字不可。所有拒绝都是 `RuntimeErrorCode("E_RUNTIME_IDENTITY", ...)`，除非另有说明。

1. **`boot_id()` 的 Windows 分支**：`sys.platform == "win32"`（精确相等，`win64`、`cygwin` 不算）时走 Windows 分支；darwin、linux 分支与其它平台的拒绝不变。分支在函数内 `import winreg`（模块顶层不导入 `winreg`，也仍然不导入 `ctypes`）；`ImportError` 变成拒绝。
2. **读两个值**：先用 `winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")` 读 `winreg.QueryValueEx(key, "MachineGuid")`，键用 `with` 打开（退出时关闭）；它必须是 `(str, winreg.REG_SZ)`；然后同样读 `r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"` 的 `"BootId"`，必须是 `(int, winreg.REG_DWORD)`。第一个值失败（打不开、读不到、类型或种类不对）时不再打开第二个键。`OpenKey`/`QueryValueEx` 抛出的 `OSError`（含 `FileNotFoundError`、`PermissionError`）、`TypeError`、`ValueError` 都变成拒绝。
3. **文本**：`f"{guid}:{count}"`，guid 原样（不改大小写、不去花括号、不 strip），count 十进制；结果必须 `fullmatch` 正则 `[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}:(?:0|[1-9][0-9]*)`，否则拒绝（所以带花括号、带换行、空串、非 UUID 的 guid 与负数都拒绝）。每次调用都重新读注册表，不缓存。
4. **`resolve_executable` 的 Windows 分支**：`os.name == "nt"` 时，三项校验（`argv0`、`cwd` 的文本规则，`_validated_environment` 的快照）与原来相同、顺序相同、错误码相同（环境值里的控制字符仍是 `E_RUNTIME_ENVIRONMENT`），之后进入 Windows 规则；POSIX 分支的文本一字不改。
5. **路径与裸名**：argv0 含 `\` 或 `/` 时是路径：`os.path.splitdrive` 给出盘符或 UNC 且其后紧跟分隔符的（“有根”）原样使用；以分隔符开头而没有盘符的（`\x\tool.exe`、`/tool.exe`）与有盘符而无根的（`C:sub\tool.exe`）拒绝；其余接在 `cwd` 之后（`os.path.join(cwd, argv0)`）。argv0 不含分隔符但 `splitdrive` 有盘符的（`C:tool.exe`）拒绝。其余是裸名：`environment["PATH"]` 必须存在且非空，否则拒绝；按 `;` 切分，每一项必须非空且有根，否则拒绝，消息含 `PATH`；候选是按 PATH 顺序的 `os.path.join(entry, name)`：argv0 有扩展名（`os.path.splitext(argv0)[1]` 非空）时 name 只有 argv0 本身，否则依次 `argv0 + ".exe"`、`argv0 + ".com"`（同一项内先 `.exe` 后 `.com`，再到下一项）；第一个 `os.path.isfile` 为真的候选胜出；没有就拒绝，消息含 `not found on environment PATH`。不读 `PATHEXT`（快照里的也不读），不读 `os.environ`，不调 `shutil.which`。
6. **结果**：`resolved = os.path.realpath(candidate)`；必须 `os.path.isfile(resolved)` 且 `os.path.splitext(resolved)[1].lower()` 在 `(".exe", ".com")` 里，否则拒绝（`.cmd`、`.bat`、无扩展名、`tool.exe.txt`、目录都拒绝；`.EXE`、`.Com` 接受）；不用 `os.access`。`RuntimeErrorCode` 原样抛出；`OSError`、`ValueError`、`UnicodeError` 变成拒绝。返回 `resolved`。
7. **`capture_process_identity`** 不改：在 Windows 上 `ProcessIdentity(pid, boot_id(), file_sha256(executable), worktree_id)` 自然成立。
8. **兼容性**：不改任何公开签名；不新增公开名字；导入无副作用（V07）。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 1、2、3.2、3.3、5、6 节、oracle，以及 `process_identity.py`（常量、`_validated_boot_id`、三个 boot id 函数、`_path_lookup_value`、`resolve_executable`、`capture_process_identity`）。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–6 条改 `process_identity.py`（约 120–140 行：常量、注册表读取、Windows 的 PATH 项校验与查找、Windows 解析函数、`resolve_executable` 与 `boot_id` 各加一个分支）。模块文档串与 `resolve_executable` 的文档串顺带补一段 Windows 规则。
3. 跑 V01 直到 oracle 全过。
4. 新建 `tests/test_win_identity.py`，只覆盖下面这些（oracle 已经覆盖了规则的各个边界；**可以沿用 oracle 的假 `winreg` 写法**，但不要照抄 oracle 的用例；假注册表用例用 `mock.patch.object(sys, "platform", "win32")` 与 `mock.patch.dict(sys.modules, {"winreg": fake})`，在任何平台都能跑；原生用例加 `@unittest.skipIf(os.name != "nt", ...)`；文件只创建不执行；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言）：
   - 假注册表：大写 guid 与计数 0 → 文本恰好 `<GUID>:0`；两次调用之间把计数改成另一个值 → 两次文本不同（不缓存）；`MachineGuid` 是 `REG_DWORD` 时只打开了第一个键；
   - 假注册表的失败：第二个键 `OpenKey` 抛 `PermissionError`、第一个值 `QueryValueEx` 抛 `FileNotFoundError`、`(guid, REG_DWORD)`、`("13", REG_SZ)` → 都是 `E_RUNTIME_IDENTITY`；同一个假模块下 `sys.platform` 为 `cygwin` → 拒绝且没有打开任何键；
   - 垫片（原生）：临时目录里 `tool.cmd`、`tool.bat`、`tool.exe` 三个文件，裸名 `tool` 解析到 `.exe`；删掉 `.exe` 后 `tool`、`tool.cmd`、垫片的绝对路径都拒绝；快照里加 `PATHEXT=".CMD"` 与 `mock.patch.dict(os.environ, {"PATHEXT": ".CMD"})` 都不改变结果；
   - PATH 规则（原生）：相对项、空项（`dir;`）、无根项（`\dir`）、用 `:` 连接的两项 → 拒绝且消息含 `PATH`；两个有根项按顺序查找（第一项胜出）；`os.environ["PATH"]` 指向含 `tool.exe` 的目录而快照的 PATH 指向空目录 → 拒绝；
   - 显式路径（原生）：绝对路径原样、正斜杠形式、相对路径接 cwd（`sub\tool.exe` 与 `sub/tool.exe`）；`\sub\tool.exe` 与 `C:tool.exe` 拒绝；名为 `x.exe` 的目录拒绝；
   - 原生身份：`capture_process_identity(os.getpid(), executable=os.path.realpath(sys.executable), worktree_id="wt-1")` 的 `to_dict()` 恰好四个键，`boot_id` 等于 `boot_id()` 且 `fullmatch` 第 3 条的正则，`executable_sha256` 等于 `hashlib.sha256` 读整个文件的结果。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
5. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
6. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_win_identity_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_win_identity.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_process_identity.py tests/test_process_identity_gaps.py tests/test_process_identity_probes.py tests/test_posix_supervisor.py tests/test_posix_supervisor_gaps.py tests/test_posix_supervisor_oracle.py tests/test_posix_supervisor_probes.py tests/test_supervised_record.py tests/test_supervised_operation.py tests/test_adapter_run.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要）：`python -c "import sys, sentinelmesh.process_identity; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要，基线应失败）：`python -c "import re; from sentinelmesh.process_identity import boot_id; print('BOOT_ID_SHAPE_OK' if re.fullmatch(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}:(?:0|[1-9][0-9]*)', boot_id()) else 'BOOT_ID_SHAPE_BAD')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`BOOT_ID_SHAPE_OK`

V01、V04 在基线上失败，是因为 Windows 分支还不存在；V02 是因为测试文件还不存在；V08 是因为基线的 `boot_id()` 在 Windows 上报 `E_RUNTIME_IDENTITY`。V03 保护环境摘要、darwin/linux 分支、POSIX 规则（在 Windows 上跳过）与监督记录的现有规则；V07 保护导入无副作用。V08 只打印形状判断，不打印 boot id 本身。

## Definition of done
- D01：`boot_id()` 的 Windows 分支、两个注册表值的读取顺序与类型、文本与形状、不缓存（Behavior contract 第 1–3 条）；证据：V01、V02、V08。
- D02：`resolve_executable` 的 Windows 规则：三项校验不变、路径与裸名、PATH 项、候选顺序、不读 `PATHEXT`/`os.environ`/`which`（第 4、5 条）；证据：V01、V02。
- D03：结果的后缀规则与错误映射（第 6 条）；证据：V01、V02。
- D04：`capture_process_identity` 在 Windows 上成立（第 7 条）；证据：V01、V02。
- D05：兼容性与导入无副作用（第 8 条）；POSIX 分支文本不变；证据：V03、V04、V07、review。
- D06：Step 4 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

## Stop conditions
- 基线、oracle、已有接口或 Repository facts 与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 pi。
- 本合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总，包括返工版本）
- max_wall_seconds_per_attempt: 1800
- max_wall_seconds_total: 3600
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配（上限 30 分钟，按 20 分钟规划、留 10 分钟余量）：
  - 阅读约 3 分钟；
  - 实现约 6 分钟（一个文件约 130 行）；
  - 测试约 6 分钟（6 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 2 秒，V03 约 5 秒，全量约 170 秒）；
  - 执行报告约 2 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D06，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。
- 对合同的任何解读或取舍，写进 `contract_conflicts` 或 `unresolved_questions`，不要只写在 limitations 里。

## Forbidden
- 不修改本合同、oracle、夹具、已有模块（允许集合之外）与已有测试（允许集合之外）、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不访问工作副本以外的路径（包括主仓库与 `.agent/runs`）。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
