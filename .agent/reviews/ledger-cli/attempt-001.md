# REVIEW：ledger-cli v1 · attempt-001

Verdict: PASS

## 基本信息
- 基线：`5c4bd85eed0277f147e4c5f471a29645447bbfbd`（master `9e8aa98` + 规划端 oracle `tests/test_ledger_cli_oracle.py`，12 个用例，其中 1 个用子进程跑 `python -m sentinelmesh.cli`）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2，调度工具 2.4.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 1122.5 s / 1800 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop、报告 json_ok。3 个工具错误：工具名乱码 `ash`（第四次）、一次替换前后相同的 edit、一次 macOS 的 BSD grep 不支持的 `-P`，都随即恢复 |
| G2 范围合规 | passed | 只改允许的 3 个文件、新增允许的 2 个文件，violations 0，没有工作副本外路径，主仓库未变，没有在 `rm -rf "$T"` 之后再接命令 |
| G3 行为正确 | passed（有补全） | 第 1–7 条成立；`resume` 的预读改变了报错先后（L1）、空证据被换成算出来的摘要（L2）、提交之后输出失败像参数错误（L3），都由规划端补全 |
| G4 回归检查 | passed | checks/01（fresh）：V01–V07 全部通过，全量 832 passed |
| G5 质量复审 | passed（有补全） | 独立复审建议 PASS；关闭账本与非最终用量两条规则没有测试守住（L4），由规划端补测 |
| G6 可集成 | passed | 补丁往返校验通过 |

## 逐条核对
- 第 1–2 条：`request_ids()` 按建立顺序；`operation_usage()` 先查精确类型，两层都复制；持久账本的两个读访问器先查可用。
- 第 3 条：`register` 挂 `ledger` 命令组（子命令必填），`cli.py` 在 `build_parser` 返回前调用；`_with_ledger` 打开、做一件事、在 `finally` 里关闭，拒绝时也关；变更一律用 `ledger.generation`。
- 第 4 条：成功时 `print(json.dumps(..., ensure_ascii=False, sort_keys=True))` 一行；拒绝时 stderr 一行 `{"error", "detail"}`、退出码 3；argparse 错误退出码 2。
- 第 5 条：`status` 的结构与 `attention` 的三条理由与合同一致；`stopped` 由停止原因是否为空推出。
- 第 6 条：四个变更命令的调用与输出与合同一致；`resume` 的原因按给出的顺序原样传入。
- 第 7 条：V07 通过。

## 执行器的解读
- 合同矛盾与未决问题为空。limitations 四条：`attention` 只列本请求自己的操作（合同的嵌套结构与 Step 3 只支持这一读法，采纳）；`stopped` 由 `stop_reasons` 非空推出（两者由同一处设置、同一处清空，采纳）；`resolve` 的取值按字母排序（集合不变，采纳）；真实入口只由 oracle 的子进程用例覆盖（采纳）。

## 发现
- **L1（低）`resume` 先读停止原因，改变了报错的先后。** `ledger_cli.py:205` 在调 `resume` 之前先 `stop_reasons(request)`：请求未知又带着不合格的说明或运维名时，CLI 报 `E_REQUEST_UNKNOWN`，而 `resume` 自己会先报 `E_NOTE` 或 `E_OPERATOR`。这次预读也没有用：恢复成功时手打的列表必然等于原因列表。补全：去掉预读，输出手打的列表。
- **L2（低·CLI 放宽了守卫）`settle --evidence ""` 记下了一个凭空算出来的证据摘要。** CLI 把 `--evidence` 设为必填，但调度器单维度的 `settle` 用的是 `evidence_hash or digest(...)`，空字符串被当成“没给”，换成由操作、维度与数额算出的摘要，看起来像真证据（`APPLIED`）。脚本里 `--evidence "$HASH"` 碰上没设的变量就会这样。补全：CLI 对空的证据报 `E_OBSERVATION_SHAPE`（调度器对不合格证据报的就是这个码），退出码 3，账本不变。
- **L3（低）提交之后的输出失败像参数错误，`stop` 又不是幂等的。** `_emit` 在变更提交、账本关闭之后才写 stdout；终端编码表示不了的字符（`PYTHONIOENCODING=ascii` 下的 `安全暂停`）让 `cli.main` 打印 `error: …` 并返回 2，和参数错误同一个码，可暂停已经记下了；运维照“失败”重试，就多了一个必须点名的暂停。补全：输出改为向 `sys.stdout.buffer` 写 UTF-8 字节，不再依赖终端编码；关闭的管道这类提交之后的失败仍可能出现，记为残留。
- **L4（低·测试）** 两套测试都守不住：不关账本（M12）——账本实例在函数返回时就被 CPython 回收，描述符数看不出泄漏；“非最终的数额也算已知”——`status` 会漏掉一个 `resume` 照样拒绝的操作；L1 的先后。另外执行器测试 `tests/test_ledger_cli.py:462-474` 的理由（没关的实例会挡住重开写入）在 DELETE 日志模式下不成立。补全：oracle 留住每个被打开的实例并断言它已关闭，补非最终用量与报错先后的用例，改正那条测试的说明。
- **信息（不影响结论）**：
  - 崩溃之后第一次打开时，残留的热日志会让 SQLite 回滚，连 `status` 也会写一次文件——这是 SQLite 的恢复，不是账本的变更；正常情况下 `status` 不改文件的字节、修改时间与目录；
  - `stopped` 由 `bool(stop_reasons)` 推出，依赖“`stopped` 等于列表第一项”这一不变式，前几轮的随机测试里一万多步都成立。
- **独立复审对四个问题的回答**：第 1–7 条逐条成立；除 L2 之外，CLI 没有放宽任何守卫（拒绝不改账本、缺文件不新建、原因不自动补、不用旧代际、记不下非最终用量、控制器冲突仍失败关闭）；非 ASCII 参数、带空格与中文的路径、相对路径、目录、损坏的账本都按合同处理；执行器的每个拒绝用例都有合法基线和文件摘要比对。

## 变异检查
- mutations/01（oracle 与执行器测试）：18 个抓住 17 个，漏掉 M12（不关账本），见 L4。
- mutations/02（只用执行器测试）：抓住 13 个；另漏掉 `--reason` 不是必填、受监督标记反了、用量接受子类、停止标记算错，都由 oracle 守住。

## 结论理由
- 第 1–7 条成立，独立复审建议 PASS。四条发现都是低严重度、一两行就能修，由规划端补全。判 PASS。

## 后续动作
- `branch` 保留本候选；规划端在 `pi-complete/ledger-cli` 上补全 L1–L4，并用变异体确认新用例抓得住。
- 集成顺序：oracle `5c4bd85`、本候选、规划端补全。
- 人工介入：约 40 分钟（关于 TypeScript 的分析与提问、设计说明、草稿与 oracle、合同、变异检查、门禁核对、复审记录）。
