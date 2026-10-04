# TASK: posix-identity-env

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 84c372682d25c1ac8e23e4964be736be70da43bb
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
新增模块 `sentinelmesh/process_identity.py`，为后续的 POSIX 监督器提供两类能力：
- **显式环境的规范摘要与核对**：调用方给出的环境映射必须与 `InvocationSpec.environment_sha256` 一致。
- **进程身份的原生采集**：macOS 的 boot 会话 UUID、按显式 PATH 解析的可执行文件及其 SHA-256，组成 canonical `ProcessIdentity`。

## Non-goals
- 不启动、不监督进程；不修改 `sentinelmesh/runtime.py` 或任何已有模块；不接入 adapter 或 scheduler。
- 不读取凭据，不使用 `os.environ` 的内容做解析或摘要；Linux 与 Windows 不做原生验证（保持 NOT_RUN）。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`。

## Repository facts
- 基线 `84c3726`：全量 `python -m pytest -q` 175 passed。
- 设计与用户决定见 `.agent/design/posix-runtime-runner.md`（第 2 节“身份”“输入”，第 5 节第 2 条：新监督器默认不继承父进程环境）。
- canonical 类型在 `sentinelmesh/runtime.py`：
  - `RuntimeErrorCode(code, message)`；
  - frozen `InvocationSpec(request_id, run_id, command, cwd, environment_sha256, timeout_seconds, shell=False)`；
  - frozen `ProcessIdentity(pid, boot_id, executable_sha256, worktree_id)`，构造时校验：pid 为正整数，boot_id 与 worktree_id 为不含控制字符的非空文本，executable_sha256 为小写 64 位十六进制。
- 规划端在本机实测：`sysctl -n kern.bootsessionuuid` 两次都输出 `FBFA2D40-4D9D-4995-8F7B-F16C5602F4F0` 这种形式的大写 UUID；ctypes 调用 `sysctlbyname(b"kern.bootsessionuuid", …)` 得到同一值，缓冲区 37 字节（含末尾 NUL）。
- 工具链：PATH 前置的 venv 提供 `python` 3.12 与 pytest 9.1.1；平台 macOS 15.7.9 x86_64。

## Scope
- allowed_modified_files: 无
- allowed_new_files:
  - sentinelmesh/process_identity.py
  - tests/test_process_identity.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `sentinelmesh/runtime.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有错误都抛 `sentinelmesh.runtime.RuntimeErrorCode`，code 按下文。

1. **`environment_digest(environment) -> str`**
   - 校验，违反任一条都抛 `E_RUNTIME_ENVIRONMENT`：
     - `environment` 必须是 `collections.abc.Mapping`；
     - 每个名称的类型必须恰好是 `str`、非空、不含 `=`；
     - 每个值的类型必须恰好是 `str`，可以为空；
     - 名称和值中的每个字符都不能是 `ord < 0x20`、`0x7F`，也不能落在 `0xD800–0xDFFF`。
   - 摘要：对 `{"schema": "sentinelmesh-environment-v1", "variables": {名称: 值}}` 做 `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，UTF-8 编码后取 SHA-256，返回小写十六进制。
   - 黄金向量：
     - `{"LANG": "C", "PATH": "/usr/bin:/bin"}` → `fa0f07ab1a352dd1ee7596bc244caa6649e742dfe9f800f455ef68c822f75cde`；
     - `{}` → `16f1bf80d85f8f89ba69f102a9a8a35f07dd8b0ac4b26cb68a9c0b46e29fde7e`。
2. **`verified_environment(spec, environment) -> dict[str, str]`**
   - `type(spec) is not InvocationSpec` 时抛 `E_RUNTIME_INVOCATION`；
   - `environment_digest(environment) != spec.environment_sha256` 时抛 `E_RUNTIME_ENVIRONMENT`；
   - 否则返回一个新的普通 `dict` 副本，之后修改调用方的映射不影响返回值。
3. **`boot_id() -> str`**：每次调用都重新读取，不缓存。
   - `sys.platform == "darwin"`：在函数内导入 ctypes，用 libc 的 `sysctlbyname(b"kern.bootsessionuuid", …)` 先取长度再取值。返回码非 0、长度为 0、解码失败或格式不符都抛 `E_RUNTIME_IDENTITY`。
   - `sys.platform` 以 `linux` 开头：读取 `/proc/sys/kernel/random/boot_id`，去掉首尾空白。读取失败或格式不符抛 `E_RUNTIME_IDENTITY`（本机无法原生验证，测试用补丁覆盖）。
   - 其他平台：抛 `E_RUNTIME_IDENTITY`。
   - 格式：`[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}`，原样返回，不改大小写。
4. **`resolve_executable(argv0, *, cwd, environment) -> str`**
   - 校验：`argv0` 与 `cwd` 的类型必须恰好是 `str` 且非空，否则抛 `E_RUNTIME_IDENTITY`；`environment` 按第 1 条规则校验，否则抛 `E_RUNTIME_ENVIRONMENT`。
   - 含 `/` 的 `argv0`：绝对路径直接使用，相对路径相对 `cwd` 拼接。
   - 不含 `/` 的 `argv0`：只在 `environment["PATH"]` 中查找（`shutil.which(argv0, path=…)`），从不读取 `os.environ`。没有 `PATH` 键或它为空时抛 `E_RUNTIME_IDENTITY`。
   - 结果取 `os.path.realpath`，必须是可执行的普通文件，否则抛 `E_RUNTIME_IDENTITY`。
   - 返回真实绝对路径。
5. **`file_sha256(path) -> str`**：对普通文件的全部字节分块计算 SHA-256，返回小写十六进制。文件不存在、是目录或无法读取时抛 `E_RUNTIME_IDENTITY`。
6. **`capture_process_identity(pid, *, executable, worktree_id) -> ProcessIdentity`**
   - 返回 `ProcessIdentity(pid, boot_id(), file_sha256(executable), worktree_id)`；
   - pid 与 worktree_id 的校验由 `ProcessIdentity` 自己完成，原样冒出它的 `RuntimeErrorCode`。
7. **导入无副作用**：导入模块时不导入 ctypes，不读文件，不读环境变量，不启动进程。

## Steps
1. 读 AGENTS.md、本合同、设计说明，以及 `sentinelmesh/runtime.py` 中的 `RuntimeErrorCode`、`InvocationSpec`、`ProcessIdentity`。基线与 Repository facts 不符就停止并报告。
2. 按 Behavior contract 新建 `sentinelmesh/process_identity.py`，只依赖标准库和 `sentinelmesh.runtime`。
3. 新建 `tests/test_process_identity.py`，至少覆盖下列用例。每个拒绝用例先验证合法基线，再只改受测条件并断言具体 code。
   - 两个黄金向量；
   - 摘要与键的顺序无关，任一名称或值改变（包括空值与缺键）都会改变摘要；
   - 第 1 条的每类非法输入各一个用例：非映射、非 str 名称或值、空名称、名称含 `=`；名称和值中的 NUL、控制字符、DEL、代理码点分别覆盖；
   - `verified_environment`：匹配时返回相等且独立的副本；不匹配抛 `E_RUNTIME_ENVIRONMENT`；spec 类型不对抛 `E_RUNTIME_INVOCATION`；
   - macOS 上 `boot_id()` 等于 `sysctl -n kern.bootsessionuuid` 的输出，并且两次调用相同（非 darwin 时 skip）；
   - `boot_id()` 失败关闭：补丁把 `sys.platform` 改成不支持的值时抛 `E_RUNTIME_IDENTITY`；
   - linux 分支用补丁提供合法与非法的 boot_id 内容，分别返回值和抛 `E_RUNTIME_IDENTITY`；
   - `resolve_executable`：
     - 在临时目录建一个可执行脚本，只通过 `environment["PATH"]` 找到它（`os.environ` 的 PATH 不含该目录）；
     - 缺 `PATH` 或 PATH 为空时报错；找不到时报错；文件不可执行时报错；
     - 含 `/` 的相对路径相对 `cwd` 解析；符号链接解析为真实路径；
   - `file_sha256` 等于对同一字节计算的 `hashlib.sha256`；目录抛 `E_RUNTIME_IDENTITY`；
   - `capture_process_identity(os.getpid(), executable=os.path.realpath(sys.executable), worktree_id="wt-1")` 返回四个字段都正确的 `ProcessIdentity`；
   - 导入无副作用：在子进程中导入本模块后，`ctypes` 不在 `sys.modules` 中。
4. 运行 V01–V07。
5. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，不要写到 `/tmp/v01.txt` 这类固定路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_process_identity.py` · cwd：仓库根 · 超时：300s · 期望退出码：0
- V02（必要，基线应失败）：`python -c "from sentinelmesh.process_identity import environment_digest as d; assert d({'LANG': 'C', 'PATH': '/usr/bin:/bin'}) == 'fa0f07ab1a352dd1ee7596bc244caa6649e742dfe9f800f455ef68c822f75cde'; assert d({}) == '16f1bf80d85f8f89ba69f102a9a8a35f07dd8b0ac4b26cb68a9c0b46e29fde7e'; print('ENV_DIGEST_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`ENV_DIGEST_OK`
- V03（必要，基线应失败）：`python -c "import subprocess; from sentinelmesh.process_identity import boot_id; native = subprocess.run(['sysctl', '-n', 'kern.bootsessionuuid'], capture_output=True, text=True, check=True).stdout.strip(); assert boot_id() == native == boot_id(); print('BOOT_ID_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`BOOT_ID_OK`
- V04（必要，基线应失败）：`python -c "import sys, sentinelmesh.process_identity; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V05（必要）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0
- V06（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V07（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01–V04 在基线上失败，是因为模块与测试文件还不存在。

## Definition of done
- D01：环境摘要与核对（Behavior contract 第 1、2 条）；证据：V01、V02。
- D02：boot_id 采集与失败关闭（第 3 条）；证据：V01、V03。
- D03：可执行文件解析、文件摘要与身份组装（第 4–6 条）；证据：V01。
- D04：导入无副作用（第 7 条）；证据：V04。
- D05：已有测试不退化，所有修改在允许集合内，无依赖变更；证据：V05、V06、V07、scope。

## Stop conditions
- 基线、canonical 类型或 Repository facts 中的实测事实与本机不符。
- 必须修改允许范围之外的文件才能继续。
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
  - 阅读约 3 分钟；
  - 模块实现约 6 分钟；
  - 测试约 8 分钟；
  - 运行检查约 2 分钟；
  - 执行报告约 3 分钟；
  - 合计约 22 分钟。

## Report back
- 最后一条消息必须是执行报告：逐项说明 D01–D05，列出自检命令与结果、遗留问题与阻塞原因，末尾附上执行说明规定的 JSON 代码块。
- 自检结果是自述；最终是否通过由验收端决定。

## Forbidden
- 不修改本合同、调度脚本、代理规则或权限配置。
- 不删除、跳过或弱化测试来获得通过。
- 不提交；不推送、合并或部署。
- 不更换模型或提供商，不调用未授权的外部服务，不读取或输出凭据。
