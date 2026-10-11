# TASK: win-evidence

## Identity
- plan_version: 1
- repository: KitExperiment
- base_sha: 3b290acd6d17f077b4f6a14b361efb6758918247
- risk_level: medium
- authorized_actions: local_edit, local_test
- integration_actions: local_branch
- executor: step/step-5-preview · thinking high

## Goal
按设计说明 `.agent/design/windows-supervisor.md` 第 3.4 节与第 6 节 W8（用户决定第 5 节第 1 条）做 Windows 监督器的"证据一半"，不做 Job Object：
- 新建 `sentinelmesh/windows_supervisor.py`：两个流文件的独占创建、pin、按身份回滚（先关描述符再删文件）、通过 pin 读流事实、`streams_match`；没有 `O_NOFOLLOW`/`O_NONBLOCK`/`os.pread` 的地方全部用 `lstat` 先行、拒绝重解析点、`O_BINARY`、fstat 身份等于 lstat 身份的手工做法；
- `sentinelmesh/supervised_record.py` 的 `_read_record` 与 `_write_record`（partial 创建）、`sentinelmesh/supervised_operation.py` 的 `_record_digest` 各加一个非 POSIX 分支，做法与 `adapter_run.read_stream` 相同；POSIX 分支文本一字不改；
- 让规划端 oracle `tests/test_win_evidence_oracle.py` 全部通过；
- 新建执行器测试 `tests/test_win_evidence.py`（清单见 Step 5，刻意不长）。

## Non-goals
- 不做 Job Object、不定义 `supervise`、不改平台选择：`supervised_record` 仍然 `from .posix_supervisor import SupervisedRun, supervise`，`supervised_operation` 仍然 `from .posix_supervisor import streams_match`（oracle 钉住；第 3、4 份合同再换）。
- 不改 `sentinelmesh/posix_supervisor.py`、`adapter_run.py`、`process_identity.py`、`runtime.py`、`scheduler.py`、`core.py`、`cli.py`；不改两处记录模块里 POSIX 分支的文本、`_MAX_RECORD_BYTES`、记录的键集合与 schema、`_validated_streams`、`replay_decision`/`recorded_streams`/`record_supervised_run`/`evidence_digest` 的行为与签名。
- 不读 `os.environ`；除 `sys.executable -c` 之外不启动任何进程；不建 Job Object；不导入 `ctypes`。
- 不修改合同、`.agent/`、`.agents/`、`.pi/`、AGENTS.md、`code-kit-v2/`、`pyproject.toml`、oracle、夹具与允许集合之外的已有测试；不联网、不读取凭据、不调用 pi。

## Repository facts
- 基线 = master `6840d75`（设计说明 `.agent/design/windows-supervisor.md`：第 1 节实测、第 2 节现状、第 3.4 节方案、第 5 节用户决定、第 6 节 W8、第 9 节进度）+ 规划端 oracle 提交 `3b290ac`，后者只含 `tests/test_win_evidence_oracle.py`（18 个用例、67 个子用例；在基线上整个文件因 `import sentinelmesh.windows_supervisor` 失败）。oracle 直接导入 `windows_supervisor` 的 `_abandon_evidence`、`_fact_of`、`_open_evidence_files`、`_remove_own_evidence`、`_streams_of`、`streams_match`，并用 `mock.patch.object(windows_supervisor.os, "lstat"/"open"/"read", ...)`、`mock.patch.object(supervised_record.os, ...)`、`mock.patch.object(supervised_operation.os, ...)` 注入假 lstat/open/read——所以三个模块都要 `import os` 后用 `os.xxx` 调用，不要 `from os import lstat`。oracle 用一个"最低空闲描述符号"探针（打开再关闭一个文件、比较得到的 fd 号）证明失败路径不泄漏描述符。
- `sentinelmesh/posix_supervisor.py`（基线，不改）在 Windows 上可以导入（`signal.SIGKILL`、`os.killpg` 只在函数体内引用）。可以从它导入复用的名字：`SupervisedRun`、`_BLOCK_BYTES`（65536）、`_EVIDENCE_MODE`（0o600）、`_STDOUT_NAME`、`_STDERR_NAME`、`_STREAM_NAMES`（`("stdout", "stderr")`）、`_close_fd(descriptor)`（None 跳过、OSError 忽略）、`_digest_blocks(descriptor, limit)`（返回 `(bytes_read, digest_or_None)`）、`_identity_of(descriptor, name)`（fstat 的 `(st_dev, st_ino)`，OSError→`E_RUNTIME_EVIDENCE`）、`_valid_facts(value)`（闭合的四键 fact，调用时读 `posix_supervisor.MAX_STREAM_DIGEST_BYTES`）。`MAX_STREAM_DIGEST_BYTES` 只在 `posix_supervisor` 里有一份：oracle 用 `mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", n)` 同时压低 `_fact_of` 与 `_valid_facts` 的上限，所以 `_fact_of` 必须在调用时读 `posix_supervisor` 模块的这个属性（`from . import posix_supervisor as _posix` 后用 `_posix.MAX_STREAM_DIGEST_BYTES`），不能在导入时复制。POSIX 的 `_open_evidence_files` 返回的 8 元组顺序是 `(stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, stdout_identity, stderr_identity)`；`_open_evidence`、`_open_pinned`、`_remove_own_evidence`、`_fact_of`、`_stream_matches`、`streams_match` 的文档串写明了每条规则，Windows 版逐条对应。
- `sentinelmesh/adapter_run.py`（基线，不改）行 75–80：`_HAS_NOFOLLOW = hasattr(os, "O_NOFOLLOW")` 的注释与定义；`read_stream` 行 226–250 是非 POSIX 分支的模板（`pinned = os.lstat(path)`，`FileNotFoundError`→missing、OSError→unreadable、非 `S_ISREG` 或 `st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT`→unreadable、`os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))`，fstat 身份≠lstat 身份→unreadable）。
- `sentinelmesh/supervised_record.py`（基线）：常量 `_MAX_RECORD_BYTES = 65536`、`_RECORD_MODE = 0o600`；`_write_record(evidence_dir, name, content)` 的结构是外层 `try`（内层 `try` 建 partial、`_write_all`、`os.fsync`；`except BaseException` 关描述符后 raise）→ `os.close` → `os.link(partial, path)` → `except OSError`→`E_RUNTIME_EVIDENCE` → `finally` 里 `created` 为真则 `_discard_partial`；`_entry_exists(path)`（`os.lstat`，`FileNotFoundError`→False，其它 OSError→`E_RUNTIME_EVIDENCE`）定义在 `_write_record` 之后，运行时可以调用；`_read_record(evidence_dir, name)` 的结构是 `try: descriptor = os.open(...)` / `except FileNotFoundError: return None` / `except OSError`→`E_RUNTIME_EVIDENCE`，然后 `try: info = os.fstat(descriptor)`、`S_ISREG` 检查、大小检查、`_read_at_most`、`except RuntimeErrorCode: raise`、`except OSError`→`E_RUNTIME_EVIDENCE`、`finally` 关描述符。`RuntimeErrorCode` 是 `ValueError` 的子类，不是 `OSError`。模块文档串第 25 行起"reads are equally strict"一段描述读取规则。
- `sentinelmesh/supervised_operation.py`（基线）：`_record_digest(path)` 的结构与 `_read_record` 相同但不抛：`FileNotFoundError`→`None`、OSError→`"unreadable"`、非 `S_ISREG` 或超过 `_MAX_RECORD_BYTES`→`"unreadable"`、读超一字节→`"unreadable"`，否则 sha256；`evidence_digest(evidence_dir)` 把两个值放进 `{"handoff": ..., "observation": ...}`，`json.dumps(sort_keys=True, separators=(",", ":"))` 后取 sha256。模块已 `from typing import Any, Mapping, Optional`。
- Windows 文件事实（规划端 2026-10-10 实测，Windows Server 2025、Python 3.12.10、Administrator）：没有 `os.pread`、`os.O_NOFOLLOW`、`os.O_NONBLOCK`，有 `os.O_BINARY`；`os.open(O_WRONLY|O_CREAT|O_EXCL)` 在**悬空符号链接**处不会失败，而是穿过链接在目标位置建文件（活的符号链接→`FileExistsError`，目录→`PermissionError`）；本进程开着的文件（写描述符或只读 pin 任一）`os.unlink`、`os.rename` 都是 `PermissionError`（winerror 32），`os.replace` 用别的文件覆盖它是 `PermissionError`（winerror 5），全部关掉后才能删、改名、覆盖；写描述符、只读 pin 与 `lstat` 对同一文件给相同的 `(st_dev, st_ino)`，硬链接同身份，`fstat` 结果也带 `st_file_attributes`；`os.link(src, dst)` 在 dst 已存在（含符号链接）时 `FileExistsError`；`os.fsync` 可用；`os.lstat` 对含 NUL 的路径抛 `ValueError`，对孤立代理项抛 `FileNotFoundError`；`O_WRONLY` 描述符上 `os.read` 是 `OSError`（errno 9）；mode `0o600` 在 Windows 上无效（`st_mode` 是 0o666）；子进程通过 `stdout=fd` 继承写描述符写完退出后，pin 的 `fstat.st_size` 与 `os.read` 看到全部字节；本账户能建符号链接与硬链接。文本模式的危害：没有 `O_BINARY` 的写描述符把 LF 写成 CR LF，没有 `O_BINARY` 的读描述符把 CR LF 折成 LF 并在 0x1A 停下——oracle 的子进程输出与写入字节里故意含这三种字节。
- 规划端用一份草稿实现验证过（草稿未提交）：`windows_supervisor.py` 约 390 行（其中文档串占一半，执行器可以写短些）、两处记录模块共 +83/−12 行；oracle 18/18；全量 838 passed / 289 skipped；31 个故意偏差里 30 个被 oracle 抓住，剩下 1 个是等价变异（`_stream_matches` 去掉 `read != fact["bytes"]` 的比较：lstat 的大小检查在前，读到的字节数不同时摘要不可能相等）。

## Scope
- allowed_modified_files:
  - sentinelmesh/supervised_record.py
  - sentinelmesh/supervised_operation.py
- allowed_new_files:
  - sentinelmesh/windows_supervisor.py
  - tests/test_win_evidence.py
- allowed_deleted_files: 无
- forbidden_paths: `.agent/**`, `.agents/**`, `.pi/**`, `AGENTS.md`, `code-kit-v2/**`, `pyproject.toml`, `tests/fixtures/**`, `tests/test_win_evidence_oracle.py`, `tests/test_win_identity_oracle.py`, `tests/test_win_identity.py`, `tests/test_supervised_record.py`, `tests/test_supervised_record_gaps.py`, `tests/test_supervised_record_oracle.py`, `tests/test_supervised_operation.py`, `tests/test_supervised_operation_gaps.py`, `tests/test_supervised_operation_oracle.py`, `tests/test_supervisor_stream_facts.py`, `tests/test_supervisor_stream_facts_gaps.py`, `tests/test_supervisor_stream_facts_oracle.py`, `tests/test_posix_supervisor.py`, `tests/test_posix_supervisor_oracle.py`, `tests/test_adapter_run.py`, `sentinelmesh/posix_supervisor.py`, `sentinelmesh/adapter_run.py`, `sentinelmesh/process_identity.py`, `sentinelmesh/runtime.py`, `sentinelmesh/scheduler.py`, `sentinelmesh/core.py`, `sentinelmesh/cli.py`, `sentinelmesh/__init__.py`
- dependency_changes: forbidden
- 始终禁止：本合同、AGENTS.md（及 CLAUDE.md，如有）、.agent/、.agents/、.claude/、.codex/、.pi/、scripts/pi_dispatch.py、scripts/dispatch-pi.sh

## Behavior contract
所有"是 int""是 str""是 dict"都是精确类型（bool 不算 int，子类不算）。所有拒绝都是 `RuntimeErrorCode("E_RUNTIME_EVIDENCE", ...)`，除非另有说明。"身份"指 `(st_dev, st_ino)`；"普通文件"指 `stat.S_ISREG(st_mode)` 为真且 `getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT` 为零；`O_BINARY` 指 `getattr(os, "O_BINARY", 0)`。

1. **模块与名字**：新建 `sentinelmesh/windows_supervisor.py`，`__all__ == ["SupervisedRun", "streams_match"]`，`SupervisedRun` 就是 `posix_supervisor.SupervisedRun`（同一对象）。下面这些私有名字与签名由本合同固定（第 3 份合同的 `supervise` 要调用，oracle 直接导入）：`_open_evidence_files(evidence_dir)`、`_abandon_evidence(created, descriptors)`、`_remove_own_evidence(path, identity)`、`_fact_of(descriptor)`、`_streams_of(stdout_pin, stderr_pin)`、`streams_match(evidence_dir, streams)`；其它私有名字自定。不定义 `supervise`。帮助函数不按平台拒绝：它们是普通 `os` 调用，在任何平台都能运行。导入无副作用（不导入 `ctypes`，不读文件与环境，不起进程）。
2. **独占创建**（文件名 `stdout.log`、`stderr.log`，模式 `_EVIDENCE_MODE`）：先 `os.lstat(path)`：成功（路径上有任何条目，悬空链接也算）→ 拒绝，不打开任何东西；`FileNotFoundError` → 继续；其它 `OSError` → 拒绝。然后 `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, _EVIDENCE_MODE)`，`OSError` → 拒绝。
3. **pin**：`seen = os.lstat(path)`（`OSError` → 拒绝）；`seen` 不是普通文件 → 拒绝，不打开；`pinned = os.open(path, os.O_RDONLY | O_BINARY)`（`OSError` → 拒绝）；`os.fstat(pinned)` 与 `os.fstat(write_fd)`（`OSError` → 关掉 pin 再拒绝）；要求 `S_ISREG(fstat(pinned))`、pin 身份 == 写描述符身份、pin 身份 == `seen` 身份，三者有一条不满足 → 关掉 pin 再拒绝。每条失败路径都不泄漏描述符。
4. **`_open_evidence_files(evidence_dir)`**：返回与 POSIX 相同顺序的 8 元组 `(stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, stdout_identity, stderr_identity)`；顺序：建 `stdout.log` → 登记 `(path, None)` → 用 `_identity_of` 取身份填入 → pin → 建 `stderr.log` → 登记 → 取身份 → pin。任何异常（`BaseException`，含中断）→ `_abandon_evidence(created, (stderr_pin, stderr_fd, stdout_pin, stdout_fd))`（未打开的位置传 None）后把同一个异常原样 raise。`created` 是 `(path, identity_or_None)` 的序列。不校验 `evidence_dir`（第 3 份合同的 `supervise` 在调用前校验）。
5. **`_abandon_evidence(created, descriptors)`**：先对 `descriptors` 的每一项 `_close_fd`（None 跳过、已关闭忽略），然后对 `created` 的每一项 `_remove_own_evidence(path, identity)`。顺序固定：**先关后删**（开着的文件在 Windows 上删不掉）。不抛异常。
6. **`_remove_own_evidence(path, identity)`**：`identity` 为 None → 不动；`os.lstat(path)` 抛 `OSError` → 不动；不是普通文件（含重解析点）或身份 ≠ `identity` → 不动；否则 `os.unlink(path)`，`OSError` 忽略。永不抛异常，永不按路径删除。
7. **`_fact_of(descriptor)`**：`info = os.fstat(descriptor)`；`info.st_size > posix_supervisor.MAX_STREAM_DIGEST_BYTES`（调用时读模块属性）→ 返回 None 且一个字节都不读；`os.lseek(descriptor, 0, os.SEEK_SET)`；循环 `os.read(descriptor, min(_BLOCK_BYTES, info.st_size - total))` 直到读满 `st_size` 或读到空块，sha256 逐块更新，不拼接；过程中任何 `OSError` → None；返回恰好四键 `{"device": st_dev, "inode": st_ino, "bytes": 读到的字节数, "sha256": 小写十六进制}`。`_streams_of(stdout_pin, stderr_pin)` 返回 `{"stdout": _fact_of(stdout_pin), "stderr": _fact_of(stderr_pin)}`。
8. **`streams_match(evidence_dir, streams)`**：参数规则与 POSIX 相同：`type(evidence_dir) is not str` → False；`type(streams) is not dict` 或键集合不是恰好 `{"stdout", "stderr"}` → False；每个 fact 不通过 `_valid_facts` → False（None 也是 False）；整个函数不抛，`OSError`/`ValueError` → False。对每个 `<evidence_dir>/<name>.log` 依次：`os.lstat`（`OSError`/`ValueError` → False）→ 必须是普通文件 → `(st_dev, st_ino, st_size)` 必须等于 `(device, inode, bytes)` → `os.open(path, os.O_RDONLY | O_BINARY)`（`OSError`/`ValueError` → False）→ `os.fstat` 必须 `S_ISREG` 且 `(st_dev, st_ino, st_size)` 等于 lstat 的三元组 → `_digest_blocks(descriptor, bytes + 1)` 读到的字节数必须等于 `bytes` 且摘要等于 `sha256`；描述符在每条出路上关闭。两个文件都为真才返回 True。
9. **`supervised_record._read_record` 的非 POSIX 分支**：模块常量 `_HAS_NOFOLLOW = hasattr(os, "O_NOFOLLOW")`、`_O_BINARY = getattr(os, "O_BINARY", 0)`（放在 `_RECORD_MODE` 之后）。`_HAS_NOFOLLOW` 为真时原语句与原注释不变；否则 `seen = os.lstat(path)`（`FileNotFoundError` → 返回 None；其它 `OSError` → 拒绝）；`seen` 不是普通文件 → 拒绝（不打开）；`os.open(path, os.O_RDONLY | _O_BINARY)`（`FileNotFoundError` → None；其它 `OSError` → 拒绝）。fstat 之后、原有 `S_ISREG` 检查之后加一条：`seen` 非 None 且 fstat 身份 ≠ `seen` 身份 → 拒绝。其余（大小、读取、JSON、canonical、键集合）不变。
10. **`supervised_record._write_record` 的 partial 创建**：`_HAS_NOFOLLOW` 为真时原语句不变；否则先 `_entry_exists(partial)` 为真 → 拒绝（不创建、不删除任何东西，`created` 仍为 False），再 `os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, _RECORD_MODE)`。写入、`fsync`、`close`、`os.link`、丢弃 partial 全部不变。
11. **`supervised_operation._record_digest` 的非 POSIX 分支**：同样的两个模块常量（放在 `_STREAM_NAMES` 之后）；形状同第 9 条，返回值不同：lstat `FileNotFoundError` → None，其它 `OSError` → `"unreadable"`；不是普通文件 → `"unreadable"`；open `FileNotFoundError` → None，其它 `OSError` → `"unreadable"`；fstat 身份 ≠ lstat 身份 → `"unreadable"`。POSIX 分支文本不变。
12. **兼容性**：不改任何公开签名；`supervised_record` 与 `supervised_operation` 的导入行不变（第 13 条由 oracle 钉住）；两处记录模块的文档串各补一段非 POSIX 规则；`posix_supervisor.py`、`adapter_run.py` 一字不改（V08）。

## Steps
1. 读 AGENTS.md、本合同、设计说明第 1、2、3.4、5、6（W8）节、oracle，`posix_supervisor.py` 的模块文档串与 `_open_evidence`…`_remove_own_evidence`、`_fact_of`、`_stream_matches`、`streams_match`，`adapter_run.read_stream` 的非 POSIX 分支，`supervised_record._write_record`/`_read_record`/`_entry_exists`，`supervised_operation._record_digest`。基线与 Repository facts 不符就停止并报告。
2. 新建 `sentinelmesh/windows_supervisor.py`（第 1–8 条；约 220–300 行，文档串每个函数写 2–5 行，规则本身已在合同里）。
3. 改两处记录模块（第 9–11 条，共约 80 行）。
4. 跑 V01 直到 oracle 全过。
5. 新建 `tests/test_win_evidence.py`，只覆盖下面这些（oracle 已经覆盖了规则的各个边界；可以沿用 oracle 的 `next_fd`/`snapshot`/`symlink_or_skip` 写法，但不要照抄 oracle 的用例；子进程只用 `sys.executable -c`；文件只创建不执行；每个拒绝用例先验证合法基线，再只改一处；错误码全等断言；需要符号链接的用例在 `OSError`/`NotImplementedError` 时 `skipTest`）：
   - 原生（`@unittest.skipIf(os.name != "nt", ...)`）：`_open_evidence_files` 之后，写描述符与 pin 都开着时 `os.unlink(stdout_path)` 与 `os.replace(other, stdout_path)` 都抛 `PermissionError`；`_abandon_evidence` 之后两个文件都不在了，四个描述符 `os.fstat` 都抛 `OSError`；
   - 一个 `sys.executable -c` 子进程用 `_open_evidence_files` 给的写描述符写 stdout（含 CR LF 与 0x1A 字节）与 stderr，退出后 `_streams_of` 的两个 fact 与测试自己用 `os.lstat` + `read_bytes` 算出的四键一致，`streams_match` 为 True；全部关闭后把 `stdout.log` 换成字节相同的另一个文件 → False；
   - `_open_evidence_files` 在 `stderr.log` 已是普通文件时：`E_RUNTIME_EVIDENCE`、`stdout.log` 不在了、`stderr.log` 字节不变、最低空闲描述符号不变；
   - `_fact_of`：空流给 `bytes 0` 与空串的 sha256；pin 被 `os.read` 读过 3 字节之后 fact 不变；`mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", len - 1)` 时 None；
   - 记录读取：手写的 canonical 记录（`handoff.json` 与 v2 的 `observation.json`，后者带 started 身份与两个 fact 的 `streams`）→ `recorded_streams(spec, dir)` 等于写入的 streams，改动返回值不影响第二次读取；`observation.json` 换成指向同字节文件的符号链接 → `E_RUNTIME_EVIDENCE`；换成目录 → 同；
   - `supervised_record._write_record`：partial 位置已有普通文件时 `E_RUNTIME_EVIDENCE`、该文件字节不变、记录未创建；成功写入后目录里只有记录、没有 partial、字节等于 `canonical_json(content)`；
   - `evidence_digest`：空目录、只有 handoff、handoff 换成目录三种值各不相同，第三种等于 `hashlib.sha256(json.dumps({"handoff": "unreadable", "observation": None}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()`。
   写测试时先跑单个用例，不要每改一处就跑整个文件；整文件只在最后跑一次。
6. 运行 V01–V08。**不要自己做变异检查**：不要故意改坏实现来测试，也不要把仓库复制到临时目录去改。验收端会在固定候选上做变异检查。
7. 写执行报告；遇到合同没有定义的行为，停止并在 `unresolved_questions` 中说明。

## Validation commands

必要检查优先直接执行；输出管道必须保留被检查程序的真实失败状态，不能以 tail/echo 的成功退出码作为检查通过证据。需要截短输出又要保留真实退出码时，直接运行检查命令即可（工具会返回完整输出与退出码）；确要截短，写成 `T=$(mktemp -d); <命令> > "$T/out" 2>&1; echo "exit=$?"; tail -n 20 "$T/out"; rm -rf "$T"`，删除之后不再接任何命令，也不要写到 `/tmp/v01.txt` 这类固定路径。探针里不要用 `tempfile`。不要自己做变异检查（故意改坏实现，包括把仓库复制到临时目录去改）：验收端会在固定候选上做。只读写工作副本内的路径。

- V01（必要，基线应失败）：`python -m pytest -q tests/test_win_evidence_oracle.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V02（必要，基线应失败）：`python -m pytest -q tests/test_win_evidence.py` · cwd：仓库根 · 超时：300s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V03（必要）：`python -m pytest -q tests/test_supervised_record.py tests/test_supervised_record_gaps.py tests/test_supervised_record_oracle.py tests/test_supervised_operation.py tests/test_supervised_operation_gaps.py tests/test_supervised_operation_oracle.py tests/test_supervisor_stream_facts.py tests/test_supervisor_stream_facts_gaps.py tests/test_supervisor_stream_facts_oracle.py tests/test_posix_supervisor.py tests/test_posix_supervisor_oracle.py tests/test_adapter_run.py tests/test_ledger_reference_oracle.py tests/test_stream_ceilings_oracle.py tests/test_scheduler_supervised.py tests/test_win_identity_oracle.py` · cwd：仓库根 · 超时：600s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V04（必要，基线应失败）：`python -m pytest -q` · cwd：仓库根 · 超时：900s · 期望退出码：0 · 输出须匹配：`[1-9]\d* passed`
- V05（必要）：`python -m compileall -q sentinelmesh tests` · cwd：仓库根 · 超时：120s · 期望退出码：0
- V06（必要）：`git diff --check` · cwd：仓库根 · 超时：60s · 期望退出码：0
- V07（必要，基线应失败）：`python -c "import sys, sentinelmesh.windows_supervisor, sentinelmesh.supervised_record, sentinelmesh.supervised_operation; assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"` · cwd：仓库根 · 超时：60s · 期望退出码：0 · 输出须匹配：`IMPORT_SAFE_OK`
- V08（必要）：`git diff --quiet 3b290acd6d17f077b4f6a14b361efb6758918247 -- sentinelmesh/posix_supervisor.py sentinelmesh/adapter_run.py sentinelmesh/process_identity.py sentinelmesh/runtime.py` · cwd：仓库根 · 超时：60s · 期望退出码：0

V01、V04、V07 在基线上失败，是因为 `windows_supervisor` 模块还不存在；V02 是因为测试文件还不存在。V03 保护记录模块与流事实的现有规则（在 Windows 上大多跳过，`read_stream` 的非 POSIX 分支与 win-identity 的 oracle 会实际运行）；V08 保护 POSIX 监督器等四个文件一字不改（`--quiet` 在有差异时退出码 1；检查命令按 argv 直接执行，不经 shell）。全量约 120 秒。

## Definition of done
- D01：模块、名字、导入无副作用（第 1 条）；证据：V01、V07。
- D02：独占创建的 lstat 先行、pin 的三重身份、失败路径不泄漏（第 2–3 条）；证据：V01、V02。
- D03：`_open_evidence_files` 的顺序与回滚、`_abandon_evidence` 先关后删、`_remove_own_evidence` 按身份（第 4–6 条）；证据：V01、V02。
- D04：流事实与 `streams_match`（第 7–8 条）；证据：V01、V02。
- D05：两处记录模块的非 POSIX 分支与 partial 创建，POSIX 分支文本不变（第 9–12 条）；证据：V01、V02、V03、V04、V08、review。
- D06：Step 5 的测试齐全，不自行做变异检查；所有修改在允许集合内，无依赖变更；证据：V05、V06、scope。

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
- 时间分配（上限 30 分钟，按 22 分钟规划、留 8 分钟余量）：
  - 阅读约 4 分钟；
  - 实现约 9 分钟（新模块约 250 行，两处分支约 80 行；文档串写短）；
  - 测试约 5 分钟（7 个用例，先跑单个用例）；
  - 运行检查约 3 分钟（oracle 约 1 秒，V03 约 10 秒，全量约 120 秒）；
  - 执行报告约 1 分钟。

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
