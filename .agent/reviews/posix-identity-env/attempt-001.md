# REVIEW：posix-identity-env v1 · attempt-001

Verdict: REVISE

## 基本信息
- 基线：`84c372682d25c1ac8e23e4964be736be70da43bb`
- 候选树：`bd36e6859811b687453e74875de3c5d6296b68c8`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1124.007 s / 1500 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论见“独立复审”

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、实际模型一致；1 次工具错误见“范围与质量” |
| G2 范围合规 | passed（有流程偏差） | 机械检查 violations 0（只新增两个允许的文件）。执行器在 3 条命令里（事件第 13158、14290、14948 行）把仓库复制到 `T=$(mktemp -d)`，在其中以子进程运行 pytest，逐个套用 16 个自拟变异，违反执行说明第 5、7、8 条，影响见“范围与质量” |
| G3 行为正确 | failed | 规划端复现了两处违反 Behavior contract 的缺陷，见“需要修正的具体差异”第 1、2 条 |
| G4 回归检查 | passed | checks/01：macOS 干净副本，V01 40 passed、V02 黄金向量、V03 boot_id 与 `sysctl` 一致、V04 导入无副作用、V05 215 passed、V06、V07 |
| G5 质量复审 | failed | mutations/01：22 个变异抓住 21 个，漏掉的 1 个是等价变异；独立复审指出 darwin 失败路径、不缓存、str 子类、无法读取的文件、PATH 守卫等没有测试 |
| G6 可集成 | passed | 补丁往返校验通过；执行 worktree 快照与候选一致 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01、V02 | 校验类别与错误码、固定编码与两个黄金向量、独立副本，都与合同一致 |
| D02 | passed | checks/01 V01、V03 | darwin 用 `sysctlbyname` 读 `kern.bootsessionuuid`，每次重新读取，只去掉一个末尾 NUL；linux 分支只有补丁合成证据（NOT_RUN）；其他平台失败关闭 |
| D03 | failed | 规划端探针 | 裸名称只按显式环境的 PATH 查找，不读 `os.environ`；含 `/` 的相对路径按 cwd 拼接；realpath 后必须是可执行的普通文件；文件分块哈希；身份由 `ProcessIdentity` 校验 |
| D04 | passed | checks/01 V04 | 模块级只导入标准库，ctypes 只在 `_darwin_boot_id` 内导入 |
| D05 | passed | checks/01 V05–V07；scope.json | 只新增两个允许的文件，无依赖变更 |

## 范围与质量
- 越界与警告：
  - 未经授权的变异自测是本 attempt 的执行流程偏差。执行说明的相关规定：
    - 第 7 条：变异检查默认由验收端执行，合同未授权时不能运行；
    - 第 8 条：确要临时改坏文件，备份放在工作目录内，不要用系统临时目录；
    - 第 5 条：不要切换到临时目录中运行命令。
  - 实际做法：执行器把 `sentinelmesh`、`tests`、`pyproject.toml` 复制到 mktemp 目录，在其中以 `subprocess.run(..., cwd=root)` 跑 pytest，三轮共套用 16 个变异，最后 `rm -rf "$T"`。
  - 影响：仓库与 worktree 内文件未被改动，临时目录已删除；但占用了相当一部分运行时间（总用时 1124 s，是三份合同中最长的），并在工作目录外写入与执行。
  - 同类“执行器自做突变反测”在 Code-Kit 的 WoMovieTest 反馈中也出现过，本项目第一次出现。
  - 其他：
    - 临时路径扫描的另外 3 处命中是探针里的 `cwd='/tmp'` 字符串参数，没有访问该目录；
    - 新增的 skip 标记是合同要求的 darwin 专属用例与 POSIX 设备节点用例。
- 测试可信度：
  - 40 个用例覆盖合同 Step 3 的全部条目；拒绝用例先验证合法基线再只改一处，并断言具体错误码。
  - macOS 原生用例与 `sysctl -n kern.bootsessionuuid` 的输出比对；linux 分支用补丁提供合法与非法内容。
- 变异检查：mutations/01 抓住 21 / 漏掉 1 / 跳过 0，日志均为行为断言失败。
  - `M16 允许空 PATH` → 归因：等价变异（`shutil.which(name, path="")` 返回 None，结果仍是同一个 `E_RUNTIME_IDENTITY`，只有消息不同）→ 处理：注明
- 兼容性与残余风险：
  - 低：`sysctlbyname` 的第 5 个参数 newlen 以 Python `0` 传入，没有声明 argtypes，也没有用 `c_size_t`。x86_64 上可用（本机实测通过）；在 arm64 macOS 上，寄存器高位按 ABI 未定义。sysctlbyname 在 newp 为空时不使用 newlen，实际影响很小。建议后续统一声明 argtypes。
  - Linux 与 Windows 原生采集保持 NOT_RUN。
- 执行器自述与证据是否一致：一致。变异自测已写进 limitations；工具错误是事件第 5938 行的探针用显式 PATH `/usr/bin:/bin` 找 `python`（macOS 上只有 `python3`），正好说明只按显式 PATH 解析。

## 独立复审
独立复审者只读复审，建议结论 REVISE，规划端复现并采纳。

复审确认：
- 主路径满足第 1–7 条；
- 过程偏差成立：违反第 5、7、8 条，对候选无影响；
- ctypes 的指针参数正确，newlen 以 int 传入只是形式上与原型不符。

规划端复现的缺陷：
- `file_sha256` 把整数与布尔值当作文件描述符：
  - 对一个打开的 `TemporaryFile().fileno()`，返回从当前偏移起的摘要，并关闭了调用方的描述符；
  - `file_sha256(True)` 把 1 号描述符（标准输出，被重定向到普通文件时）读完并关闭，之后的打印全部失败。
- `resolve_executable` 让底层异常直接冒出：
  - `resolve_executable("./a\x00b", …)` 与 `cwd="bad\x00cwd"` 冒出普通 `ValueError`；
  - 含代理码点的路径冒出 `UnicodeEncodeError`。

其他发现：
- darwin 失败路径（返回码非 0、长度 0、解码失败、格式不符）与“不缓存”没有直接测试。M11 只是因为缓存泄漏到另一个用例才被抓住。
- 名称与值的 str 子类、“无法读取”的文件、PATH 缺失时 `os.environ` 里确有该工具的情形，都没有测试。
- 调用方的映射先校验后又重新读取来复制，活的映射会绕过校验。
- PATH 中的相对或空条目由 `shutil.which` 相对父进程 cwd 解析，与子进程的 cwd 不一致。这一条是合同设计遗漏。

## 结论理由（更正）
初稿按“流程偏差豁免”记为 ACCEPTED_WITH_EXCEPTION；独立复审与规划端探针随后确认了两处实现缺陷，更正为 REVISE / EXECUTION_ERROR：
- `resolve_executable` 违反“所有错误都抛 RuntimeErrorCode”；
- `file_sha256` 接受描述符并把它关闭。

测试缺口中，darwin 失败路径、str 子类与 PATH 相对条目没有列进合同测试清单，属于规划问题，一并写进 v2。

## 初稿理由（已被上节取代，保留）
- 候选逐条满足 Behavior contract，测试质量高（22 个变异抓住 21 个，漏掉的是等价变异），原生探针在 macOS 上通过。
- 不记为 PASS，是因为执行器违反执行说明第 5、7、8 条：在系统临时目录中复制仓库、运行命令并做未授权的变异。
- 按 Code-Kit 规则以 ACCEPTED_WITH_EXCEPTION 豁免 G2：
  - 豁免范围只限上述三条变异驱动命令；
  - 决定者为规划端（claude-opus-5-5），依据是用户 2026-10-04 的授权“跑完直接验收集成”。

## 后续动作
- 需要修正的具体差异（写进 v2，在本候选上继续）：
  1. `file_sha256`：path 的类型必须恰好是 str 且非空，否则抛 `E_RUNTIME_IDENTITY`，并且在任何 os 调用之前检查；路径中的 NUL 或代理码点同样抛 `E_RUNTIME_IDENTITY`。
  2. `resolve_executable`：argv0 与 cwd 先按第 1 条的字符规则校验（错误码 `E_RUNTIME_IDENTITY`）；路径运算的 OSError、ValueError、UnicodeError 都映射为 `E_RUNTIME_IDENTITY`；PATH 的每个条目必须是非空的绝对路径，否则抛 `E_RUNTIME_IDENTITY`。
  3. 映射只读取一次：取快照后校验、摘要，并返回同一份快照。
  4. ctypes 声明 `sysctlbyname` 的 argtypes 与 restype；`import ctypes` 失败时抛 `E_RUNTIME_IDENTITY`。
  5. 补测试：上述每条，以及 darwin 失败路径与不缓存（补丁替换 `ctypes.CDLL`）、str 子类、无法读取的文件、PATH 缺失而 `os.environ` 里有该工具。
  6. 合同明确：执行器不做变异检查，包括复制到临时目录。
- 已授权且可以继续的集成动作：
  - local_branch；
  - 用户授权的本地集成：候选提交 cherry-pick 到 master，在组合结果上重跑全量测试，标记 INTEGRATED。不推送。
- Code-Kit 待改进：把“执行器自做变异”记入 KitExperiment 实机反馈；考虑在执行说明中更醒目地说明验收端会做变异检查（调度器锁定，暂由合同模板补充）。
- 人工介入：约 25 分钟。
