# REVIEW：posix-identity-env v2 · attempt-002

Verdict: REVISE

## 基本信息
- 基线：`1af4d04801d6704d34a1a2e8447960999ee22e07`（attempt-001 的返工提交 `8316a92` + 规划端探针）
- 候选树：`edebd1d152894191b96e5c91b0b3cafc5e42c876`（补丁往返校验：是）
- 要求的执行模型：step/step-5-preview · thinking high（macOS，pi 1.0.2）
- 实际模型：step/step-5-preview；providerThinkingLevel 未回报，只能确认请求的是 high
- 运行：Pi 退出码 0 · 656.254 s / 1500 s · 未超时、未取消、无残留进程
- 费用：unknown
- 复审者：Claude Code 规划会话（claude-opus-5-5）；独立复审者按 `independent-review.md` 只读复审，结论同为 REVISE（只改测试）

## 门禁
| 门禁 | 结果 | 说明 |
|---|---|---|
| G0 输入一致 | passed | plan.md / contract.json 哈希与 prepare 记录一致 |
| G1 执行完整 | passed | agent_settled、stopReason=stop；3 次工具错误都无影响，见“范围与质量” |
| G2 范围合规 | passed | M2，violations 0。临时文件全部按 `T=$(mktemp -d)` … `rm -rf "$T"` 写；事件第 11118 行删除后跟了 `date +%s`，属 nit。执行器这次没有自做变异，也没有复制仓库 |
| G3 行为正确 | passed | v2 第 1–4 条与 v1 第 1–7 条在代码复审中逐条成立；规划端探针 4 项通过 |
| G4 回归检查 | passed | checks/01：V01 58 passed、V02 探针 4 passed、V03 黄金向量、V04 boot_id、V05 导入安全、V06 237 passed、V07 |
| G5 质量复审 | failed | 3 个拒绝用例被另一条规则遮蔽，D04 未满足 |
| G6 可集成 | passed | 补丁往返校验通过；执行 worktree 快照与候选一致 |

## DoD 映射
| DoD | 结果 | 证据 | 说明 |
|---|---|---|---|
| D01 | passed | checks/01 V01、V02 | `file_sha256` 在任何 os 调用之前先校验文本；描述符不再被打开或关闭 |
| D02 | passed | checks/01 V01、V02 | argv0/cwd 字符规则、PATH 绝对条目、路径异常映射 |
| D03 | passed | checks/01 V01–V05 | 单次快照、ctypes 原型、`import ctypes` 失败映射；v1 行为不退化 |
| D04 | failed | mutations/01、02 | Step 3 要求“每个拒绝用例只改一处并断言具体 code”，但有 3 处被遮蔽（见下） |
| D05 | passed | checks/01 V06、V07；scope.json | 只改两个允许的文件，探针未改动 |

## 范围与质量
- 变异检查：
  - mutations/01（只用执行器测试）：抓住 11 / 漏掉 5；
  - mutations/02（加规划端探针）：抓住 12 / 漏掉 4。
  - 逐个读了日志，抓住的都是行为断言失败。
  - 漏掉的变异：
    - `V2-M02 file_sha256 不映射 UnicodeError` → 归因：等价变异（路径在 try 之前已按字符规则校验，UnicodeError 是 ValueError 的子类）→ 处理：注明
    - `V2-M03 argv0 不按字符规则校验` → 归因：执行器少写（坏 argv0 指向的文件都不存在，“找不到”分支给出同一个 code）→ 处理：规划端补测
    - `V2-M14 darwin 取值返回码不检查` → 归因：执行器少写（失败子用例的载荷本身就不是 UUID，格式校验给出同一个 code）→ 处理：规划端补测
    - `V2-M16 darwin 截断到第一个 NUL` → 归因：执行器少写（NUL 落在 UUID 中间，截断后仍不合法）→ 处理：规划端补测
    - `V2-M08 不取快照` → 只被规划端探针抓住。执行器的 `SnapshotMapping` 只在 `items()` 上变化，部分原因是 Step 3 的字面写法（规划问题）→ 处理：规划端补测各种访问方式都变化的映射，以及 `resolve_executable` 从快照取 PATH 的用例
- 独立复审另提出的缺陷，规划端已复现：
  - 违反 Mapping 协议的映射（`items()` 产出长度不对的项，或不可哈希的键）在 `dict(environment.items())` 处冒出普通 `ValueError` / `TypeError`，违反 v1 “所有错误都抛 RuntimeErrorCode”。
  - 处理：规划端补全时映射为 `E_RUNTIME_ENVIRONMENT` 并加测试。
- 其他：
  - bytes 子用例没有用存在的路径，不能单独证明类型规则（低，补测时一并加强）；
  - `import ctypes` 失败分支没有测试（合同未要求）。
- 规划缺陷（规划端）：
  - 合同要求读 v1 合同与上一轮复审，但它们只在 master 上，不在侧分支基线 `1af4d04` 中。执行器没有越出工作目录，在 `unresolved_questions` 中如实报告，并按 v2 转述的规则完成。
  - 严格按执行说明应记为 contract_conflict 并把相关 DoD 标为 blocked，但这是规划端造成的。
  - Code-Kit 的 `validate` 也没有提示 Repository facts 中的 `.agent/` 文件不在基线。已记入 Code-Kit 实机反馈。
- 残余风险（留给第 2 份合同）：
  - 相对 cwd 会让 `./tool` 相对监督器自己的 cwd 解析，监督器应只接受绝对 cwd；
  - realpath 的结果没有再按字符规则检查，但 `file_sha256` 会在后续失败关闭。
- 工具错误：
  - 事件第 2941 行读取不在 worktree 的 v1 合同，见规划缺陷；
  - 第 8625 行误用不存在的工具名 `ash`；
  - 第 8974 行 edit 的 oldText 不唯一，随后重试成功。

## 结论理由
- 实现满足合同，过程也合规。
- 但 D04 明确要求拒绝用例只改一处，执行器有 3 处被另一条规则遮蔽，所以记 REVISE / EXECUTION_ERROR。
- 本合同 `max_attempts_total: 2` 已用完。按 review-pi 的规则，剩余部分小而明确，由规划端在返工提交上直接补齐，作为单独的提交，按同样的验收标准检查（全量测试 + 针对性变异），然后把 attempt-001 与 attempt-002 标记为 SUPERSEDED。

## 后续动作
- 规划端补全，单独提交：
  1. `_environment_snapshot` 把 `TypeError`/`ValueError` 映射为 `E_RUNTIME_ENVIRONMENT`；
  2. 新增 `tests/test_process_identity_gaps.py`，覆盖下列用例，并证明每条在对应变异或缺陷上失败：
     - 带控制字符的可执行文件名真实存在；
     - darwin 取值失败但缓冲区里是合法 UUID；
     - 合法 UUID 之后跟内嵌 NUL；
     - 违反协议的 Mapping；
     - 各种访问方式都变化的映射，以及 `resolve_executable` 从快照取 PATH；
     - 存在路径的 bytes 形式。
- 集成：把 `8316a92`、`1af4d04`、本 attempt 的返工提交与规划端补全提交 cherry-pick 到 master，并重跑全量测试。不推送。
- 人工介入：约 40 分钟。
