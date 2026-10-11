# 复审记录：win-identity v1 · attempt-001

结论：PASS。候选逐条满足合同，fresh 检查 8/8，两轮变异 31/33 与 23/33（第一轮漏掉的两条是 oracle 自身的空白，规划端补 oracle；第二轮漏掉的十条里八条是合同留给 oracle 的规则或同一空白，一条是合同清单的例子不够尖，一条是执行器用例因错误的原因通过），独立复审 PASS。

- 基线：`0591618`（master `551e344` 设计说明 + oracle `0591618`）；合同 `8e48c7b`
- 执行：step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 用时 673.0 s（上限 1800 s）· Pi 退出码 0
- 候选树：`0eeb016`（补丁往返校验通过）；变更 A1 M1 D0 T0，越界 0，警告 0
- 事件流：22211 行，完整（agent_settled），最终 stopReason=stop；执行报告 json_ok（completed）；工具错误 1（第 11515 行：中途 `compileall` 报文档字符串里的 `\B`、`\s` 转义警告与一处语法错误，执行器随后把文档串改成原始字符串并修正，候选上 V05 通过）；自动重试 0；压缩 0
- 用量：input 115271 / output 69052 / cacheRead 2000384

## 机械门禁

| 门 | 结果 | 依据 |
| --- | --- | --- |
| G0 执行完整性 | 通过 | 结束事件完整，报告 JSON 合法，`unresolved_questions` 为空；`contract_conflicts` 一条（见下，裁定合同正确） |
| G1 模型与思考级别 | 通过 | 实际 step/step-5-preview，请求 high（本机 Step 不回报 providerThinkingLevel） |
| G2 范围 | 通过（一处备注） | scope.json：一个模块修改、一个测试文件新增，越界 0；新增被忽略的 `.pytest_cache/` 与 `__pycache__/` |
| G3 独立检查 | 通过 | `check --fresh` 8/8（`checks/01`）：V01 oracle 22、V02 执行器 16、V03 邻近 10 个文件 45 passed / 108 skipped、V04 全量 819 passed / 289 skipped、V05–V08 |
| G4 变异 | 通过：31/33（oracle + 执行器测试），23/33（只用执行器测试，见下） | `mutations/01`、`mutations/02` |
| G5 复审 | 通过 | 独立复审 PASS；规划端对照见下 |
| G6 候选固定 | 通过 | 补丁往返校验通过；fresh 检查在干净副本上做 |

G2 备注：自检全部用合同规定的 `T=$(mktemp -d); … ; echo "exit=$?"; tail …; rm -rf "$T"` 形式；kit 标出的唯一“可疑命令”是收尾时的 `rm -f execution_report.md`（执行器删掉自己写在工作副本根目录的草稿报告，按行号核对是清理自己的临时文件，不是仓库文件）。没有工作副本外路径。

执行器报告的 contract_conflicts：设计说明 W6 写“`_validated_boot_id` 在 Windows 上按这个形状校验”，合同 Non-goals 写不改 `_validated_boot_id`、第 3 条写 Windows 文本自己 fullmatch 自己的正则。裁定：合同正确（darwin/linux 分支在 Windows 上也要能用假 libc 测，`_validated_boot_id` 不能按运行平台选模式）；W6 的措辞由规划端修订。

## 规划端对照

Behavior contract：
1. `sys.platform == "win32"` 精确相等才走 Windows 分支；darwin、linux 与其它平台的拒绝不变；`winreg` 在 `_windows_boot_id` 内导入，`ImportError` 变成拒绝；顶层不导入 `winreg`、`ctypes`（V07）。符合。
2. 先 `OpenKey(HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")` 读 `MachineGuid`，`with` 打开；`(str, REG_SZ)` 不符就在第二个键之前拒绝；再读 `PrefetchParameters` 的 `BootId`，`(int, REG_DWORD)`；`(OSError, TypeError, ValueError)` 统一映射为 `E_RUNTIME_IDENTITY`。符合。
3. `f"{guid}:{count}"` 原样；`_WINDOWS_BOOT_ID_PATTERN.fullmatch`；每次重读。符合。
4. `resolve_executable` 的三项校验在前、顺序与错误码不变；`if os.name == "nt": return _resolve_executable_nt(...)` 在 `try` 块之前；POSIX `try` 块一字未改（diff 只在它之前插入两行与文档串）。符合。
5. 路径：`drive and isabs` 原样；无盘符而以分隔符开头、有盘符而不 isabs 的拒绝在 `join` 之前；其余 `join(cwd, argv0)`；裸名有盘符拒绝；PATH 必须存在且非空、`;` 切分、每项非空且 `_nt_is_rooted`（消息含 `PATH`）；候选 `join(entry, name)`，有扩展名只试本身，否则 `.exe` 再 `.com`，同一项内先后缀再下一项；没有就拒绝，消息含 `not found on environment PATH`。不读 `PATHEXT`、`os.environ`，不调 `shutil.which`（grep 确认 Windows 分支里没有这三者）。符合。
6. `realpath` 之后 `isfile` 且 `splitext(...)[1].lower() in (".exe", ".com")`，没有 `os.access`；`RuntimeErrorCode` 原样，`(OSError, ValueError, UnicodeError)` 映射。符合。
7. `capture_process_identity` 未改。符合。
8. 公开签名不变，没有新公开名字；V07。符合。

DoD：D01–D06 全部满足（证据：V01–V08、scope、本对照）。

执行器测试 `tests/test_win_identity.py`（16 个用例，自带假 `winreg` 模块与 `fake_registry` 上下文）对照 Step 4：大写 guid 与计数 0 → `<GUID>:0`；计数 7 → 8 两次文本不同且四次查询；guid 为 `REG_DWORD` 时只打开第一个键；第二个键 `PermissionError`、第一个值 `FileNotFoundError`、`(guid, REG_DWORD)`、`("7", REG_SZ)` 都 `E_RUNTIME_IDENTITY`；`cygwin` 不开键；垫片：`tool.cmd`/`tool.bat`/`tool.exe` 裸名到 `.exe`，删掉 `.exe` 后裸名与垫片绝对路径拒绝；快照与 `os.environ` 的 `PATHEXT=.CMD` 不改结果；PATH：六种坏值拒绝且消息含 `PATH`，两个有根项按顺序，`os.environ["PATH"]` 不读；显式路径：绝对、正斜杠、两种相对形式，`\…\tool.exe` 与 `C:tool.exe` 拒绝，`tool.exe` 目录拒绝；原生身份四键、boot id 与 `boot_id()` 相等且 fullmatch、摘要相等。每个拒绝先验证合法基线；错误码全等；原生用例有 `skipIf(os.name != "nt")`，假注册表用例平台中立。

独立复审（子代理，只读探针；自行重跑 V01+V02 38 passed、V03、`-W error::SyntaxWarning` 导入）：PASS。
- F1（中，不阻塞）：执行器的 `C:tool.exe` 拒绝用例在 `environment={}` 下测，因 PATH 缺失而以同一错误码通过（变异 M29 漏网的根因）；探针证实盘符守卫是实打实的（去掉它，同盘符的 `C:tool.exe` 会被 `ntpath.join` 接到 PATH 目录上解析成功）。裁定：规划端补全，用例改传 `{"PATH": self.directory}`。
- F2（低）：垫片用例缺了裸名 `tool.cmd` 的拒绝（Step 4 列了三者）；候选拒绝它，oracle 覆盖。裁定：规划端补全。
- F3（低）：`_nt_is_rooted` 用 `splitdrive` 非空且 `isabs`，与合同第 5 条“盘符或 UNC 且其后紧跟分隔符”有细微差别：无尾分隔符的 UNC 根（`\\server\share`）被当作有根。实际无害，但合同措辞与实现不一致，oracle 没钉。裁定：按合同措辞钉死——规划端补全把根检查改成“`splitdrive` 非空且其余部分以分隔符开头”，oracle 加用例。
- F4（低，过程）：执行器的 pytest 自检前 6 条用 `| tail -n N` 遮住了退出码，后 3 条加了 `${PIPESTATUS[0]}`；没有用合同的 `T=$(mktemp -d)` 形式，也没写固定 `/tmp` 路径；`executor-report.json` 的 `self_checks.command` 写的是合同原命令而不是实际管道。验收证据是 fresh 检查。记入反馈。
- F5（低，过程）：执行器先用 write 工具把执行报告写进了工作副本根目录（`allowed_new_files` 之外），随后自己发现并 `rm -f` 删除；最终 scope 零越界。记入反馈。
- F6（信息）：工具错误是中间版本文档串里的 `\x` 触发 `unicodeescape` 语法错误，改成原始字符串后干净。
- F7（信息）：`contract_conflicts` 的 W6 分歧取合同，与主验收端裁定一致；D01 标 done 可接受。
- F8（信息）：简报把 `executor-report.json` 写成了 `report.json`。

## 变异检查

- 清单：规划端草稿的 30 个偏差换成候选锚点并加三条（`scratchpad/make_mutations_win_identity_a001.py`，33 个，每个锚点恰好出现一次）：计数可为负、guid 只认小写、分隔符改横线、去掉形状检查、注册表键或值名改掉（四条）、改读 HKCU、缓存、两个种类检查去掉、guid 错时仍读计数、注册表 `OSError` 逃逸、`win` 前缀都走注册表、`win32` 走 linux、`.cmd` 是程序后缀、`.cmd` 也补全、`.com` 先于 `.exe`、带扩展名仍补全、`PATHEXT` 从进程或快照读、相对/空/无根 PATH 项接受、`os.access` 代替后缀规则、后缀区分大小写、无根路径接 cwd、盘符相对裸名查找、不解析链接、找不到时换错误码、Windows 走 POSIX 分支、查找用 `os.environ`。
- `mutations/01`（oracle + 执行器测试）：33 抓 31。漏掉 M18（`.cmd` 也作为补全后缀：PATH 前一项的 `tool.cmd` 会遮住后一项的 `tool.exe`）与 M20（带扩展名的名字仍补 `.exe`/`.com`：`tool.cmd.exe` 存在时 `tool.cmd` 会被解析）。两条都是合同第 5 条写了而 oracle 没钉的规则：规划端补 oracle 用例 `test_only_the_two_program_suffixes_are_ever_completed`，在候选上验证 23/23。
- `mutations/02`（只用执行器测试）：33 抓 23。漏掉：M01 负计数、M04 形状检查、M15 `win` 前缀、M19 `.com` 顺序、M27 后缀大小写、M30 链接——合同 Step 4 没有列给执行器的规则，留给 oracle；M18、M20——同上；M12 计数种类不检查——合同清单给的例子 `("13", REG_SZ)` 在“只查类型”的实现下也被拒绝，是清单的例子不够尖（应给 `(13, REG_SZ)`），oracle 的用例表有这一条；M29 `C:tool.exe`——执行器用例在 `environment={}` 下测，没有 PATH 时两种实现都拒绝，用例因错误的原因通过（低，记入复审，不返工；oracle 在有 PATH 的目录下测同一规则）。
- 草稿阶段：同一清单（30 条）在规划端草稿上 27/30，3 条等价（整数来源不可能有前导零、bool 计数被形状检查挡住、Windows 分支里没有会抛 `OSError` 的调用）。

## 裁定与下一步

- attempt-001：PASS。
- 规划端补全（单独提交）：oracle 加两个用例（M18/M20 的后缀补全规则；F3 的 UNC 根必须跟分隔符）；实现的根检查按合同措辞收紧（F3）；执行器测试的两处用例弱点（F1 传 PATH，F2 加裸名 `tool.cmd`）；设计说明 W6 措辞（收尾时与状态文档一起）。
- 集成：oracle `0591618` 与候选分支挑入 master，再补全提交；全量测试；标 INTEGRATED。

Verdict: PASS
