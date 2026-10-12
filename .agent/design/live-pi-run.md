# 设计说明：真实 pi 在监督器下运行一次（live-pi-run，候选 1b）

状态：用户 2026-10-11 批准三项均选推荐项（第 5 节）；规划端决定见第 6 节；已执行并入库（第 9 节）。
前置：controller-cli 三份合同（`smctl run init/once/recover`，`pi` adapter）、windows-supervisor 四份合同、pi-native-events、stream-ceilings、ledger-reference 均已集成（2026-10-11）。

## 1. 为什么做

- `IMPLEMENTATION_STATUS.md` 的"尚未证明"清单里，"a real pi running under the supervisor with its native stream translated" 是唯一一条所有接线都已就绪、只差一次真实运行的行：命令（`smctl run once`）、adapter（`pi`）、翻译器（`pi-session-v3`，31 条捕获流的证据）、Windows Job Object 监督器、身份记录、用量结算、`smctl ledger reference`——每一环都有离线证据，但从未串成一次真实的 provider 调用。
- 一次真实运行会产生三样东西：该行从 NOT_RUN 升到 PASS（Windows，带证据）；一条经产品命令捕获的真实流，归约后进 `tests/fixtures/pi_native/`，让 run-cli-pi 的 oracle 多一个"从 `smctl` 自己记录的 stdout.log 来"的夹具；以及真实 pi 1.1.0 的会话 v3 帧是否仍被翻译器完全识别（夹具里的流来自 pi 1.0.x）。
- 它不是一份 Pi 合同：产品代码不改，凭据不能经过合同与执行器，所以由规划端直接执行一次、记录证据、提交夹具与测试。

## 2. 现状（2026-10-11 实测，Windows Server 2025）

- `pi` 在本机是 npm 的 `pi` shell 垫片（`%APPDATA%\npm\pi`，内容是 `exec node .../node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js "$@"`），`pi --version` → `1.1.0`；`node.exe` 是 `C:\Program Files\nodejs\node.exe`，v24.19.0。
- 干跑已成立：作业 `command = [node.exe, cli.js, "--version"]`、`adapter_id: "pi"`、环境只有 `SYSTEMROOT`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`TEMP`/`TMP`/`HOMEDRIVE`/`HOMEPATH`/`PATH`（PATH 只含 node 目录），经真实 `smctl run once` → `REVIEW_PENDING`、`exit_code: 0`、`stdout.log` 是 `1.1.0\n`（流 `invalid`/`E_JSON_SYNTAX`，意料之中——`--version` 不是会话），`observation.json` 里 `process_identity.executable_sha256` 等于 `node.exe` 的 sha256（`3602f2bb…0237`），`boot_id` 带本机 BootId。也就是说真实程序在 Job Object 下启动、被记录、被归约的整条路径已通，只差 provider 调用。
- pi 的凭据在它自己的配置目录（`~/.pi/auth.json`，这里只记名字），`pi auth check --provider <p> --model <m> --json` 是 Code-Kit 派发前就在用的准备检查；派发器自己把 pi 跑在 `--provider step --model step-5-preview --mode json --no-session` 下。
- 本仓库的捕获夹具来自派发器的 `events.jsonl`（pi 1.0.x，macOS 与 Windows），归约工具 `tests/fixtures/pi_native/reduce.py` 以 attempt 目录（`events.jsonl` + `events-summary.json`）为输入。

## 3. 方案草图

规划端在仓库外的临时目录执行一次（作业文件、账本、证据目录都不进仓库）：

1. `smctl run init <ledger> --spec <spec.json>`：一个请求、一个任务，五个 token 维度的上限足够大。
2. `smctl run once <ledger> --job <job.json> --evidence <dir> --worktree-id live-1 --owner planner`，作业：
   - `command = [node.exe, cli.js, "--provider", <provider>, "--model", <model>, "--thinking", "off", "--no-session", "--no-context-files", "--no-extensions", "--no-skills", "--no-mcp", "--no-tools", "--mode", "json", "-p", "Reply with exactly: OK"]`；
   - `environment` = 第 2 节的九个变量（值取自本账户，只在临时作业文件里，不打印、不提交）；`allowed_secret_names: []`——pi 自己读 `auth.json`，环境里不放任何凭据；如果 provider 的 key 只在某个环境变量里，用户只需告诉我变量名，它进 `allowed_secret_names` 与 `environment`，仍不打印、不提交；
   - `adapter_id: "pi"`，`usage_dimensions` 五个，`timeout_seconds: 300`，上限用缺省。
3. 预期：`REVIEW_PENDING`、`result.status == "SUCCEEDED"`、五个维度 APPLIED 且 > 0（cache 可为 0）、`smctl ledger reference --dialect pi-session-v3` 的 `usage` 与 outcome 一致、`frames` 等于 stdout.log 的行数、`terminal == "agent_settled"`、`last_stop_reason == "stop"`、`unknown == 0`（pi 1.1.0 的帧全部被认出；若有未知帧，记为翻译器的下一份合同，不在本次改）。
4. 证据入库：把 `stdout.log`（就是 pi 的原始会话流）与一份按派发器摘要格式写的 `events-summary.json` 交给 `reduce.py`，得到 `tests/fixtures/pi_native/windows-pi110-live-smctl-a001.jsonl` + `.meta.json`（meta 多记：`smctl` 的 outcome 摘要、`node.exe` 的 sha256、账本结算的五个数、pi 与 node 版本）；提交前 grep 归约后的两个文件没有凭据形状的字符串。新测试 `tests/test_live_pi_fixture.py`：该夹具经 `smctl run once` 回放到 `REVIEW_PENDING`，五个维度等于 meta 里 `smctl` 当时结算的数，且 `translate` 对归约流与原流给同一结果的性质由 `test_pi_events_captured.py` 的现有机制覆盖（规划端机器上有原流）。
5. 记录：`IMPLEMENTATION_STATUS.md` 的"real pi under the supervisor" 行升到 PASS（Windows，一次，证据是夹具 + 本说明第 9 节的实测数字）；"real provider CLI calls, credentials and network" 仍是 NOT_RUN 的总体表述改为"除第 9 节记录的这一次外"。

## 4. 证据边界

- 恰好一次 provider 调用，一个固定提示，`--thinking off`，`--no-tools`，不读文件、不写会话；费用在一次最小请求的量级。
- 凭据：规划端不读、不打印、不上传；作业文件只在临时目录，运行后删除；证据目录只保留 `stdout.log` 归约后的夹具；`stderr.log` 只看长度与是否含凭据形状，不入库。
- 联网：只有这一次 pi 进程联网；`smctl` 自身不联网。
- 不做的证明：断电持久性、Linux、多提供商仍 NOT_RUN。

## 5. 用户决定（2026-10-11，均选推荐项）

1. **是否批准这一次真实运行**（凭据经 pi 自己的 `auth.json`，联网一次）：推荐批准，提示语固定为 `Reply with exactly: OK`。
2. **provider / model / thinking**：推荐 `step` / `step-5-preview` / `off`（与派发器和现有夹具同一提供商，最便宜）。
3. **捕获流是否归约后入库为夹具并加回放测试**：推荐是（归约会截断所有长字符串，提交前再 grep 凭据形状）。

## 6. 规划端决定

- 由规划端直接执行，不派合同：凭据不经过执行器，产品代码不改；若真实运行暴露产品缺陷（例如 pi 1.1.0 的新帧类型），缺陷修复另按合同流程做，本次只记录。
- 作业文件的环境只列第 2 节的九个变量：干跑证明它们足以让 node.exe + cli.js 启动；provider 调用若还需要别的变量（代理、证书），第一次失败会在 `stderr.log` 里说明，再补一个名字重跑一次，仍只记名字。
- 新夹具的名字按既有规则 `<platform>-pi<version>-<source>-a001`：`windows-pi110-live-smctl-a001`。

## 7. 合同拆分

无 Pi 合同。规划端步骤：批准 → 一次运行 → 归约与 grep → 新测试与 meta → 状态文档 → 提交（夹具、测试、状态、本说明第 9 节）。

## 8. 不做

- 不做第二次调用、不做工具调用、不做多轮会话。
- 不改 `pi_events.py`、`run_cli.py` 或任何产品模块。
- 不把作业文件、账本、`stderr.log`、未归约的 `stdout.log` 提交进仓库。

## 9. 进度与遗留

- 2026-10-11：干跑（`--version`）经 `smctl run once` 成立，见第 2 节。
- 2026-10-11：用户批准后规划端执行一次（真实 `smctl` 子进程，Windows Server 2025，node v24.19.0，pi 1.1.0，`--provider step --model step-5-preview --thinking off`）：`once` 退出码 0；outcome `REVIEW_PENDING`、`stop_reason: completed`、`exit_code: 0`、流 `ok` 9962 字节 15 帧（`session`、`agent_start`、`turn_start`、3×`message_start`/`message_update`/`message_end`、`turn_end`、`agent_end`、`agent_settled`，`unknown: 0`）；`result.status: SUCCEEDED`，助手回答 `OK`；五个维度 APPLIED：`input_tokens` 536、`output_tokens` 3、`cache_read_tokens` 0、`cache_write_tokens` 0、`total_tokens` 539，账本 `known` 相同，`smctl ledger reference` 的 `usage`/`frames`/`terminal`/`models` 一致；`observation.json` 的 `process_identity.executable_sha256` 等于 node.exe 的 sha256；`stderr.log` 0 字节；`stdout.log` 与 `stderr.log` 都没有凭据形状的字符串；请求未停止。
  - 入库：`.agent/runs/live-pi-run/attempt-001/`（git 忽略，本机的规划端证据：`events.jsonl` = `stdout.log` 原样、规划端按 pi 自己的帧算出的 `events-summary.json`、`meta.json`、`preflight.json`）；`reduce.py` 归约成 `tests/fixtures/pi_native/windows-pi110-live-smctl-a001.jsonl`（15 → 14 行，4965 字节，16 个字符串截断）+ `.meta.json`（多一个 `live_run` 块：命令、node 与 pi 版本、node.exe 摘要、outcome 摘要、账本结算的五个数、助手文本）；`tests/test_pi_events_captured.py` 的三个用例在本机把新 attempt 与夹具一起核过；新测试 `tests/test_live_pi_fixture.py`（2 个用例）把夹具经 `smctl run once` 回放到同样的结算。归约后的流里仍有 4 处本机路径前缀（会话头与系统提示的 cwd，被截到 48 字符），与既有夹具同类。
  - 作业文件、账本、原始证据目录只在会话临时目录，未提交。
  - 遗留：pi 1.1.0 这次只走了最简单的会话形状；compaction/retry/queue 等帧类型仍无捕获；Linux 与 macOS 上的真实运行仍 NOT_RUN。
