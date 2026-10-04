# TASK: posix-identity-env

## Identity
- plan_version: 2
- repository: KitExperiment
- base_sha: 1af4d04801d6704d34a1a2e8447960999ee22e07
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
在上一轮候选（attempt-001）上定点返工 `sentinelmesh/process_identity.py` 与 `tests/test_process_identity.py`，目标有三：
- 消除复审复现的两处缺陷：底层异常冒出 `resolve_executable`；`file_sha256` 把整数当作文件描述符并关闭它；
- 收紧 PATH 与映射快照的语义；
- 补齐测试缺口。

## Non-goals
- 不改 v1 已经正确的行为：摘要编码与黄金向量、darwin 与 linux 的读取方式、不缓存、导入无副作用。
- 不修改 `sentinelmesh/runtime.py` 或其他模块；不接入监督器；Linux 与 Windows 不做原生验证。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`。

## Repository facts
- 基线 `1af4d04` = 返工提交 `8316a92`（上一轮候选 attempt-001，父提交 master `84c3726`）+ 规划端探针 `tests/test_process_identity_probes.py`：4 个用例复现下列缺陷，在基线上失败；规划端已用一份草稿修复验证它们与现有 40 个测试同时通过。
- 上一轮复审：`.agent/reviews/posix-identity-env/attempt-001.md`。下面是规划端在基线上实测的缺陷：
  - 对一个打开的 `tempfile.TemporaryFile().fileno()` 调用 `file_sha256`，返回从当前偏移起的摘要，并关闭了该描述符；
  - `file_sha256(True)` 把 1 号描述符当作文件读完并关闭；
  - `resolve_executable("./a\x00b", cwd=<目录>, environment={})` 与 `cwd="bad\x00cwd"` 冒出普通 `ValueError`；
  - 含 `\ud800` 的 argv0 冒出 `UnicodeEncodeError`。
- v1 合同（`.agent/plans/posix-identity-env.v1.md`）的 Behavior contract 第 1–7 条继续有效；本合同的 Behavior contract 只写修订与新增。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；macOS 15.7.9（Darwin 24.6.0）x86_64。

## Scope
- allowed_modified_files:
  - sentinelmesh/process_identity.py
  - tests/test_process_identity.py
- allowed_new_files: 无
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`, `tests/test_process_identity_probes.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
下面所说的“字符规则”，指 v1 第 1 条对环境名称和值的字符限制：不能有 `ord < 0x20`、`0x7F`，也不能落在 `0xD800–0xDFFF`。

1. **映射只读取一次**：
   - `environment_digest`、`verified_environment` 与 `resolve_executable` 先对传入的映射取一次快照（`dict(environment.items())`），之后的校验、摘要、PATH 查找与返回值都只使用这份快照；
   - `verified_environment` 返回快照的副本；
   - 非映射的输入仍按 v1 抛 `E_RUNTIME_ENVIRONMENT`。
2. **`resolve_executable` 的输入与 PATH**：
   - `argv0` 与 `cwd` 的类型必须恰好是 `str`、非空，并满足字符规则，否则抛 `E_RUNTIME_IDENTITY`；
   - 查找裸名称时，`PATH` 按 `:` 拆分后，每个条目都必须是非空的绝对路径（以 `/` 开头），否则抛 `E_RUNTIME_IDENTITY`；
   - 路径运算（拼接、`shutil.which`、`os.path.realpath`、`os.path.isfile`、`os.access`）抛出的 `OSError`、`ValueError`、`UnicodeError` 一律映射为 `E_RUNTIME_IDENTITY`。
3. **`file_sha256` 的输入**：
   - `path` 的类型必须恰好是 `str`、非空，并满足字符规则，否则抛 `E_RUNTIME_IDENTITY`；
   - 这项检查在任何 os 调用之前完成：整数、布尔值、bytes、`os.PathLike` 都在这一步被拒绝，不会被当作描述符或路径对象打开；
   - 其余路径错误（不存在、目录、无法读取，以及 `ValueError`、`UnicodeError`）抛 `E_RUNTIME_IDENTITY`。
4. **ctypes 声明**：darwin 分支为 `sysctlbyname` 设置 `argtypes = [c_char_p, c_void_p, POINTER(c_size_t), c_void_p, c_size_t]` 与 `restype = c_int`；`import ctypes` 失败时抛 `E_RUNTIME_IDENTITY`。

## Steps
1. 读 AGENTS.md、本合同、v1 合同、上一轮复审，以及基线中的两个文件。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 第 1–4 条修改 `sentinelmesh/process_identity.py`。
3. 在 `tests/test_process_identity.py` 中补下列用例，每个拒绝用例都先验证合法基线，再只改一处并断言具体 code：
   - `file_sha256`：
     - 整数、`True`、bytes 路径、`pathlib.Path` 都抛 `E_RUNTIME_IDENTITY`；
     - 对一个打开的 `TemporaryFile().fileno()` 调用后抛 `E_RUNTIME_IDENTITY`，并且该描述符仍然打开（`os.fstat` 成功）；
     - 含 NUL、含 `\ud800` 的 str 路径都抛 `E_RUNTIME_IDENTITY`；
     - 无法读取的文件（`chmod 000`；以 root 运行时 skip）抛 `E_RUNTIME_IDENTITY`；
   - `resolve_executable`：
     - argv0 与 cwd 中的 NUL、控制字符、`\ud800`、str 子类，分别抛 `E_RUNTIME_IDENTITY`；
     - PATH 含相对条目（如 `bin`）、空条目（如 `/usr/bin::/bin`）都抛 `E_RUNTIME_IDENTITY`；
     - 缺 PATH 时，对 `os.environ` 的 PATH 中确实存在的 `sh` 仍抛 `E_RUNTIME_IDENTITY`；
   - 环境名称与值为 str 子类时抛 `E_RUNTIME_ENVIRONMENT`；
   - darwin 失败路径：用补丁把 `ctypes.CDLL` 换成假的 libc，其 `sysctlbyname` 可控，分别覆盖下列情形，都抛 `E_RUNTIME_IDENTITY`；这些用例不依赖真实平台，在任何系统上都运行：
     - 取长度时返回非 0；
     - 长度为 0；
     - 取值时返回非 0；
     - 非 UTF-8 字节；
     - 格式不符；
     - 内嵌 NUL；
   - 不缓存：假的 `sysctlbyname` 在连续两次调用中返回两个不同的合法 UUID，`boot_id()` 依次返回这两个值；
   - 映射快照：一个 Mapping 子类第一次 `items()` 返回合法内容、第二次返回不同内容，`verified_environment` 的返回值等于第一次的内容；
   - `capture_process_identity(os.getpid(), executable=os.path.realpath(sys.executable), worktree_id="wt-1")` 的四个字段全部断言（boot_id 与 `boot_id()` 相等，executable_sha256 与 `file_sha256` 相等）。
4. 运行 V01–V07。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。

- V01（必要）：`python -m pytest -q tests/test_process_identity.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -m pytest -q tests/test_process_identity_probes.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V03（必要）：`python -c "from sentinelmesh.process_identity import environment_digest as d; assert d({'LANG': 'C', 'PATH': '/usr/bin:/bin'}) == 'fa0f07ab1a352dd1ee7596bc244caa6649e742dfe9f800f455ef68c822f75cde'; assert d({}) == '16f1bf80d85f8f89ba69f102a9a8a35f07dd8b0ac4b26cb68a9c0b46e29fde7e'; print('ENV_DIGEST_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`ENV_DIGEST_OK`
- V04（必要）：`python -c "import subprocess; from sentinelmesh.process_identity import boot_id; native = subprocess.run(['sysctl', '-n', 'kern.bootsessionuuid'], capture_output=True, text=True, check=True).stdout.strip(); assert boot_id() == native == boot_id(); print('BOOT_ID_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`BOOT_ID_OK`
- V05（必要）：`python -c "import sys, sentinelmesh.process_identity; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V06（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0

V02 是规划端针对已复现缺陷写的探针，在基线上失败；V06 因包含 V02 的探针，在基线上同样失败；其余检查保护 v1 已经正确的行为。

## Definition of done
- D01：`file_sha256` 拒绝非 str 路径且不触碰描述符（Behavior contract 第 3 条）；证据：V01、V02。
- D02：`resolve_executable` 的输入校验、PATH 绝对条目与异常映射（第 2 条）；证据：V01、V02。
- D03：映射快照与 ctypes 声明（第 1、4 条），v1 行为不退化；证据：V01、V02、V03、V04、V05。
- D04：补齐 Step 3 的测试，不自行做变异检查；证据：V01、review。
- D05：全量测试不退化，所有修改在允许集合内；证据：V06、V07、scope。

## Stop conditions
- 基线或 Repository facts 中的实测缺陷与本机不符。
- 必须修改允许范围之外的文件才能继续。
- 需要新增或升级依赖、联网、访问凭据或启动真实 provider。
- 本合同与 v1 合同对同一行为说法不同，或出现合同没有定义的行为。
- 达到单轮时限。

## Budget
- max_attempts_total: 2（按 task-id 汇总：attempt-001 已用 1 次，本版本是最后一次）
- max_wall_seconds_per_attempt: 1500
- max_wall_seconds_total: 3000
- cost_budget: tracking-only
- 强制方：调度器强制次数与墙钟；费用只统计
- 时间分配：
  - 阅读约 3 分钟；
  - 实现修订约 5 分钟；
  - 补测试约 9 分钟；
  - 运行检查约 2 分钟；
  - 执行报告约 3 分钟；
  - 合计约 22 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、探针测试、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不自己做变异检查，也不把仓库复制到临时目录运行。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
