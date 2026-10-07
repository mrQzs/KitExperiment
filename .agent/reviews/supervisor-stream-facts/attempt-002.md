# REVIEW：supervisor-stream-facts v2 · attempt-002

Verdict: PASS

## 基本信息
- 基线：`ad58ee8c83baa434e0f6d2951b96d173fc5cb869`（v1 oracle `6260633` + a001 候选 `91a0927` + 规划端测试 10 个，其中 7 个在基线上失败）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.1）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 858.8 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审（中途因限流中断一次，续跑完成）

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。1 个工具错误是 edit 的旧文本没有匹配，随即重试 |
| G2 范围合规 | passed（形式偏差） | 只改允许的两个文件，violations 0，没有工作副本外路径。三条命令在 `rm -rf "$T"` 之后又接了只读检查（`compileall`、`git diff --check`、导入检查、`date`），违反执行说明“删除之后不再接任何命令”。被删的都是本条命令自己用 `mktemp -d` 建的目录，不影响范围、行为或证据 |
| G3 行为正确 | passed（有补全） | v2 合同第 1–3 条在所有可达路径上满足（见下）；`_identity_of` 在保护区之外，只在理论上违反第 1 条（F2），由规划端补全 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V08 全部通过 |
| G5 质量复审 | passed（有补全） | 独立复审建议 PASS，附两项条件；三个测试缺口由规划端补测（F1、F3、F4） |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对（v2 的修改）
- 第 1 条：
  - `_identity_of` 在 `O_EXCL` 创建之后立即取写描述符的 `(st_dev, st_ino)`；
  - `_remove_own_evidence` 只在 `lstat` 显示普通文件、且身份相同时 `unlink`；回滚与钩子失败的清理都改用它；
  - 钉子仍与写描述符比较。
- 第 2 条：
  - `MAX_STREAM_DIGEST_BYTES = 256 * 1024 * 1024`，`_fact_of` 与 `_valid_facts` 在调用时读取模块全局；
  - `st_size` 超过上限时直接返回 None，一个字节都不读；
  - `pread` 逐块读取并逐块 `update`，不再拼接。
- 第 3 条：
  - `_valid_facts` 拒绝超过上限的字节数，不打开文件；
  - `_digest_blocks` 逐块读取与摘要；
  - `os.open` 与整个 `streams_match` 都把 `ValueError` 当作不一致。

## 变异检查
- mutations/01（规划端测试、v1 oracle、执行器测试与 `tests/test_posix_supervisor.py`）：32 个，抓住 30，漏掉 2：
  - M19“上限在定义时绑定”：规划端写错的变异。它只改了签名，函数体仍读模块全局，行为不变。mutations/03 把签名与函数体一起改，被抓住。
  - M09“回收之前计算事实”：规划端测试 `test_the_facts_are_taken_after_the_reap` 只是概率性的。组内成员在发现自己被过继之后才写，而 `Popen.wait` 轮询时最多会晚 50 ms 才发现组长退出，成员可能在等待返回之前就已经写完。见 F1。
- mutations/02（只用执行器测试）：抓住 13，漏掉 19。漏掉的都由规划端测试与 v1 oracle 覆盖。
- 记录模块与它的测试自 a001 起没有变化，a001 的 mutations/01 对记录的 13 个变异全部抓住，结论沿用。

## 发现
独立复审（只读，内存探针）建议 PASS，附两项条件：先提交确定性的顺序用例；`_identity_of` 由规划端修正，或者记为例外。它逐一核实 a001 的 R1–R3 都已修好：
- 超过上限的流一次 `pread` 都不做；64 MiB 的摘要与核对，内存峰值都只有 0.13 MiB；
- NUL、孤立代理字符与过长路径都返回 False；
- 删除前按身份核对。

- **F1（低·规划端测试缺陷）顺序用例不是确定性的。**
  - 组内成员在发现自己被过继之后才写，而 `Popen.wait` 轮询时最多会晚 50 ms 才发现组长退出，成员可能在等待返回之前就写完了，所以 M09 漏网。
  - 处理：规划端补全。成员改为等包装后的 `_reap_group` 先建一个 `go` 文件再写，写入一定落在“等待返回之后、回收之前”。改后正确的实现 5 次全过，M09 的变异 5 次全被抓住。
- **F2（低·合同字面偏差，几乎不可达）两次取身份不在回滚与关闭的保护之内。**
  - `supervise` 在 `_open_evidence_files` 返回之后、`try` 之前又对两个写描述符各做了一次 `fstat`（`posix_supervisor.py:727-728`）；`_open_evidence_files` 里也是先取身份、后登记进 `created`（`:273`、`:276`）。
  - 独立复审用内存探针让这两处 `fstat` 失败：抛出的是裸 `OSError`，四个描述符都没有关闭，已创建的文件只关闭不删除。合同第 1 条要求 `fstat` 失败时报 `E_RUNTIME_EVIDENCE`，并且每一条出口都关闭钉子。
  - 对刚打开的描述符做 `fstat` 实际上不会失败；落在这个窗口里的中断，与 a001 已接受的中断窗口同类。桥接的结论也不受影响：这时还没有任何记录，判为 NOT_STARTED 是正确的。
  - 处理：规划端补全。`_open_evidence_files` 先登记 `[路径, None]` 再取身份，取身份失败报 `E_RUNTIME_EVIDENCE`，身份随结果返回，`supervise` 不再第二次 `fstat`；身份为 None 时不删除（证明不了就不删）。
- **F3（低·测试缺口）删除前用 `lstat` 而不是 `stat`，没有用例守住。** 已改名的原件被一个符号链接取代时，用 `stat` 会解析到本次的 inode，删掉这个链接。规划端补测 `test_a_symlink_put_at_a_created_path_is_left_alone`。
- **F4（低·测试缺口）“超过上限时不打开任何文件”只按“不读”检查。** 规划端在上限用例里再统计一次针对证据路径的 `os.open`，并补测取身份失败的路径 `test_an_evidence_file_that_cannot_be_identified_starts_nothing`。
- 补全的三个规则各用一个变异验证：删除前改用 `stat`、取身份失败时冒出 `OSError`、去掉核对时的上限，都被抓住；全量 581 passed。
- **F5（信息·过程偏差）** 三条命令在删除临时目录之后又接了只读检查，见 G2。执行说明从 2.4.0 起就写明了这一点，执行器仍然这样做。
- **信息**：按身份删除，在 `lstat` 与 `unlink` 之间仍有竞态窗口。POSIX 没有“比较后删除”的原子操作，这是合同规定的写法；APFS 不复用 inode 编号。

## 结论理由
- 候选在所有可达路径上满足 v2 合同，a001 的 R1–R3 已修好，变异检查（含 mutations/03 的补跑）只漏掉 M09，原因是规划端测试本身的缺陷。
- F2 是几乎不可达的字面偏差；本版本是最后一次尝试，按惯例由规划端补全，不再派回执行器。

## 后续动作
- 集成到 master：依次 cherry-pick v1 oracle `6260633`、a001 候选 `91a0927`、规划端测试 `ad58ee8`、本候选的分支提交，以及规划端补全（F1–F4）。
- a001 标记 SUPERSEDED：它的提交作为 v2 基线的一部分集成。
- 人工介入：约 60 分钟（v2 合同与规划端测试、复审、三轮变异检查、补全）。
