# REVIEW：posix-reconcile-integration v1 · attempt-001

Verdict: ACCEPTED_WITH_EXCEPTION

## 基本信息
- 基线：`f0668df6de8ecd501db51e73575200f62824affa`（master `de0123c` + 规划端 oracle）
- 候选树：`8c77636583b22c0a46b789d08ff06a684e09a06e`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1369.406 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，建议同为 ACCEPTED_WITH_EXCEPTION

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。4 次工具错误：一次是在主仓库里 `wc` 找不到 oracle（见 G2），三次是编辑参数错误，都未生效 |
| G2 范围合规 | waived | 只改了 3 个允许的文件，violations 0。但执行器读取了主仓库的设计说明，并 `cd` 到主仓库执行只读命令，详见“过程例外” |
| G3 行为正确 | waived | 合同第 1–5 条在合同定义的输入上都满足。观察记录内层的类型校验超出合同字面，详见“行为例外” |
| G4 回归检查 | passed | checks/01：macOS 干净副本，V01 oracle 11、V02 13、V03 监督器 40、V04 310 passed，V05–V07 通过 |
| G5 质量复审 | passed | Step 4 的用例齐全；低级问题见下 |
| G6 可集成 | passed | 补丁往返校验通过 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01–V03；mutations/01 H-M01–H-M05 | 钩子位置、校验顺序、BaseException 回滚并原样重抛 |
| D02 | passed（例外） | checks/01 V01、V02；mutations/01 | 写入与读取符合第 2 条；内层类型校验为例外 |
| D03 | passed | checks/01 V01、V02 | 判定顺序、五键记录、只读 |
| D04 | passed | checks/01 V07 | 导入无副作用 |
| D05 | passed | checks/01 V02；代码复审 | Step 4 每一项都有用例 |
| D06 | passed | checks/01 V04–V06；scope.json | 范围合规，无依赖变更 |

## 行为例外（G3）
- 执行器在读取观察记录时，除键集合外还校验内层取值的类型：
  - `run_id`、`environment_sha256`、`state` 必须是 str；
  - `process_identity` 必须恰好四键，pid 是 int，其余三个是 str；
  - `cleanup_confirmed` 必须是 bool。

  类型不对时报 `E_RUNTIME_EVIDENCE`。
- 合同第 4 条第 5 步规定，通过第 2 条形状检查的记录交给 `reconcile`。所以这些输入的结果变了：原本是 IDENTITY_MISMATCH、START_UNKNOWN、不可重放的记录，或 `E_RUNTIME_PID`、`E_RUNTIME_IDENTITY`，现在一律报 `E_RUNTIME_EVIDENCE`。
- 独立复审逐项核对了对照表：额外校验只会抛错，不会挡住合同允许的重放（三键 NOT_STARTED；FAILED/STOPPED 且清理已确认、身份有效），也从不放宽。规划端接受为收紧，写进设计说明第 6 节。
- 执行器在报告的“取舍”与 limitations 里如实写了这一点，但没有放进 `contract_conflicts`。
- “只删除本次调用创建的 partial”这一解读同样接受：O_EXCL 打开失败时不删别人的文件。

## 过程例外（G2）
- 根因在调度器：执行器收到的合同附件名是主仓库运行目录下的绝对路径（`.../KitExperiment/.agent/runs/.../plan.md`）。执行器据此做了三件事：
  - 读取主仓库的 `.agent/design/posix-runtime-runner.md`（事件 31）；
  - `cd` 到主仓库执行 `git log`、`rev-parse`、`ls`、`wc`（事件 77）；
  - 又执行了 `git cat-file`、`git branch -a`、`git status --short`（事件 166）。
- 影响：
  - 没有写入：`observations.json` 记录 `changed: false`；主仓库 index 的修改时间早于运行开始。
  - 没有带入基线以外的信息：主仓库、worktree 与基线中的设计说明 sha256 相同。
  - `git status --short` 的输出把主仓库约 30 个未提交文件的名称和全部分支名发给了提供商，只有名称，没有内容。
- 处理：不为此返工。调度器（锁定版本）的问题记入 Code-Kit 实机反馈：附件应使用相对路径，执行说明应写明只读 worktree 内的文件。

## 合同遗漏（规划端，集成时一并修正）
独立复审提出以下问题，规划端都已在固定候选上用探针复现：
1. **（中高）预先存在的观察记录会放行重放**。
   - 钩子只确认交接记录能发布，没有确认 `observation.json` 不存在。目录里若有一份同规格的旧观察记录，进程照常启动；写观察记录时 link 报已存在，调用方收到 `E_RUNTIME_EVIDENCE`；之后的判定读的是旧记录。
   - 探针 A 结果：进程确实运行了，判定为 `NOT_STARTED`、`replay_allowed=True`，失败开放。
   - 合同第 3 条（写入失败判 START_UNKNOWN）与第 4 条在这种输入下不一致。这是规划端的遗漏，不是执行错误。
2. **（低）FIFO 让判定永久阻塞**：合同规定的 `O_RDONLY | O_NOFOLLOW` 不带 O_NONBLOCK，打开命名管道会一直等写入方，S_ISREG 检查根本执行不到（探针 B 复现）。这也解释了 R-M22 为什么存活。
3. **（低）读取在 fstat 之后没有上限**：文件在两步之间增长时，内存与时间都没有上限。
4. **（低）写入端没有大小上限**：超过 64 KiB 的交接记录能写入，进程也会启动，但之后的判定永远报错；超过 1 MiB 时冒出 `ProtocolError`，而不是 `E_RUNTIME_EVIDENCE`。
5. **（低）成功路径上吞掉了 close 的 OSError**；监督器文档串里的校验顺序没有加入钩子检查。

## 范围外缺陷（已集成的监督器）
- `_validated_worktree_id` 用 `_scalar_text` 校验，会放过 C1 控制字符（U+0080–U+009F）；而 `ProcessIdentity` 会拒绝它们。
- 后果：`worktree_id="wt\x85"` 会在进程启动后才抛 `E_RUNTIME_IDENTITY`，进程组无人监督地继续运行。探针 D 复现：异常抛出后进程组仍存活。
- 规划端在同一次补全中修正。

## 测试与变异
- mutations/01（oracle + 执行器测试）：27 个，抓住 25 / 漏掉 2 / 跳过 0，日志均为行为断言失败。
  - `R-M15 不 fsync` → 归因：输入观察不到（不验证断电后的持久性，记为 NOT_RUN）→ 处理：注明。
  - `R-M22 不是普通文件也读取` → 归因：被另一条规则遮蔽（目录在 read 时报 EISDIR，转成同一错误码；FIFO 根本到不了这一步）→ 处理：规划端补测 FIFO 用例。
- mutations/02（只用执行器测试）：抓住 18 / 漏掉 9。漏掉的 9 个里 7 个按分工由 oracle 把守，另 2 个同上。
- 执行器测试的低级问题：
  - 两个拒绝用例没有先验证合法基线；
  - “没有启动进程”靠 0.3 秒的等待；
  - 只读用例只覆盖了记录齐全的分支。

  都不影响结论。
- 执行器自检中 V01–V03 用了 `| tail`，以 checks/01 为准。

## 结论理由
- 候选在合同定义的输入上满足全部规则，检查与变异证据充分。
- 两项例外不放宽重放，也不影响候选内容。
- 失败开放问题出在合同上，由规划端在集成时用单独提交修正并补测，不消耗执行预算。

## 后续动作
- 规划端补全（单独提交，随候选一起集成）：
  1. 钩子在写交接记录之前，用 `lstat` 确认两份记录都不存在，否则报 `E_RUNTIME_EVIDENCE`、不启动进程。
  2. 读取：加 `O_NONBLOCK`，最多读 65537 字节；写入：在 try 内先算规范字节，超过 65536 字节就报 `E_RUNTIME_EVIDENCE`；成功路径上 close 失败也报错。
  3. 监督器：`worktree_id` 改用与 `ProcessIdentity` 相同的字符规则，在启动前拒绝；补上文档串里的校验顺序。
  4. 规划端补测 `tests/test_supervised_record_gaps.py`：旧观察记录、FIFO（子进程加超时）、写入超限、读取超限、`worktree_id` 含 C1 字符。
  5. 设计说明第 6 节写明内层类型校验与 partial 的处理。
- 人工介入：约 50 分钟（设计、oracle 与草稿验证、复审、探针复现）。
