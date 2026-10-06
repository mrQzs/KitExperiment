# adapter 接入监督运行（设计说明，2026-10-06）

## 1. 目标

让 adapter 生成的调用由原生 POSIX 监督器运行，经调度器桥接完成交接与落定；然后把厂商事件流按 04 的规则规范化、归约成 `AdapterResult`，再把其中的用量按预算维度记入调度器。

这是 IMPLEMENTATION_STATUS 中“wiring the supervised runs into adapters”一项。真实 provider CLI、凭据与联网仍是 NOT_RUN。

## 2. 现状

- `sentinelmesh/adapter.py`：
  - `PiAdapter` 与 `ClaudeCodeAdapter` 生成 `runtime.Invocation`（argv、cwd、env、超时）；
  - `normalize` 只解析每行的 `type` 与 `payload`，没有序号、来源、终止与工具配对规则。
- 监督与桥接：
  - `run_supervised_operation` 已能完成交接、监督运行，并按证据落定；
  - stdout 写在证据目录的 `stdout.log`。
  - 监督器用显式环境里的 PATH 自己解析可执行文件，并核对环境摘要，所以 argv[0] 可以是命令名。
- 调度器：
  - REVIEW_PENDING 的操作只要用量不是最终值，就算作未结清；
  - 之后对同一 request 的领取报 `E_USAGE_UNKNOWN`。

  所以接线若不结算用量，一次运行之后整个 request 就无法再领取。

## 3. 选择

- **参考实现**：Code-Kit 的 `scripts/codekit/adapters/protocol.py` 与 `offline.py`，冻结在 `.agent/design/adapter-reference/`（来源提交见 manifest）。
  - 规范化事件有固定词表：`run_started`、`assistant`、`tool_started`、`tool_finished`、`usage`、`run_finished`、`error`。
  - 序号从 0 连续递增，事件 id 不重复，来源必须与 adapter 一致，payload 是有界、无环、有限的 JSON。
  - 结算规则：
    - `run_started` 唯一且在最前；终止事件唯一且在最后；
    - 工具开始与结束必须成对；
    - 出现未知事件即判为不完整；
    - 用量取最后一条 `usage` 事件。
  - 04 文档附录 B 自述是教学参考，方言也是合成的。Code-Kit 的实现是已经交付、有测试的同一合同族，所以选它；附录 B 留作以后细化多轮与用量去重的参考。
- **依赖**：KitExperiment 的 `protocols.check_json_value` 与 `runtime.validate_argv` 与 Code-Kit 逐字相同（`validate_argv` 只多一段文档字符串），已核对。因此可以逐字移植，只改 import 路径。
- **流格式**：监督路径上，厂商 stdout 必须是规范化事件 JSONL，每行一个事件对象。
  - 真实 Pi 的原生 JSONL 不是这种格式，翻译层保持 NOT_RUN；
  - 测试用桩 CLI 直接输出规范化事件，明确是合成协议，不冒充 Pi 支持（04 第 1.1 节）。
- **用量**：
  - 只在 `AdapterResult` 的 `settled` 与 `raw_complete` 都为 True 时记录；
  - payload 中与预约维度同名、值为精确非负 int 的项按最终值记入，`dispatches` 由交接记录，不在此列；
  - 其余情形用量保持未知，request 因此停在未结清。这是失败关闭：拿不准就不放行新的领取。
- **证据**：
  - stdout 按 `supervised_record` 的方式只读：`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`、必须是普通文件、有字节上限；
  - 用量观察的 `evidence_hash` 是 stdout 全部字节的 SHA-256。
- **信任边界**（adapter-supervised-run 复审，F1）：同账户的子进程能伪造自己的流。
  - 不跟随符号链接只挡住了链接，改名或硬链接换上来的普通文件照样会被读取；
  - 用 `setsid` 干净逃逸、不留僵尸的后代，会让监督器判定清理已确认，复审之后它还能改写或追加流。

  所以记录的用量是厂商的声明，不是独立证明。要把它变成证明，需要另立监督器合同，例如在观察记录里写入 stdout 的身份与摘要，或做进程组之外的逃逸检测。

## 4. 合同拆分

1. `adapter-protocol-port`：把 `protocol.py` 与 `offline.py` 移植到 `sentinelmesh/adapter_protocol.py`。oracle 有两份：
   - 逐字移植的 Code-Kit 测试；
   - 与冻结参考逐项比较的差分测试。
2. `adapter-supervised-run`：新建 `sentinelmesh/adapter_run.py`，用桩 CLI 做真实进程测试：
   - 把规范化 `Invocation` 转成 `InvocationSpec`；
   - 监督运行；
   - 读取 stdout，归约成 `AdapterResult`；
   - 按第 3 节记录用量。
3. 视第 2 份的结果，再决定是否要做 Pi 原生事件的翻译层。这需要真实 Pi 的证据，仍是 NOT_RUN。

## 5. 不做

- 不调用真实 provider CLI，不读取凭据，不联网。
- 不改调度器、监督器与记录模块；不替 07 签发 PASS。
- 不改 legacy `adapter.py`：旧的 `SubprocessRunner` 路径继续用它，新路径用 `adapter_protocol`。

## 6. 规划端的决定（用户：“你来决定”，2026-10-06）

- D1：参考选 Code-Kit 的实现，不选 04 附录 B。
- D2：监督路径的流格式是规范化事件 JSONL；Pi 原生格式的翻译另议。
- D3：用量只在完整结算时记录，否则保持未知。
- D4：legacy `adapter.py` 保持不变，与新模块并存。

以上决定都可以在后续合同里修改；改动时更新本节。

## 7. 进度

- 第 1 份 `adapter-protocol-port` 已集成（2026-10-06）：
  - 提交：oracle `821a9b1`、a001 `928145a`（PASS）；
  - `sentinelmesh/adapter_protocol.py` 与冻结的参考逐字一致，只改了 import、模块文档字符串与合并后的 `__all__`；
  - 差分 oracle 在描述符、调用、规范化、注册表与穷举的结算序列上与参考完全相同。
- 第 2 份 `adapter-supervised-run` 已集成（2026-10-06）：
  - 提交：oracle `b0f46b4`、a001 `bba63ec`（PASS）、规划端补全 `9726844`；
  - 用量必须在 `finish` 之前记入：`finish` 检查预算时，用量不是最终值的操作会把 request 永久停止。所以包装桥接的 `candidate_hash`，它只在结束复审的路径上、操作还是 RUNNING 时被调用；
  - 规划端补全：桥接在交接之前拒绝非 str 的 `evidence_dir`（原来会让操作停在 HANDOFF_ACCEPTED），并补了交接前检查顺序与两处测试稳健性。
- 已知残留：
  - 第 3 节的信任边界（F1）；
  - `usage_dimensions` 没有与预约维度对账：列入未预约的维度时，要等运行结束才在 `settle` 里报错，操作随之置为 START_UNKNOWN；
  - `settle` 对异步中断不是全有或全无，但操作与 request 都会停在 START_UNKNOWN；
  - 子类 adapter 抛出非 `AdapterError` 时，异常在落定之后冒出，调用方拿不到结果；
  - Pi 原生事件的翻译仍是 NOT_RUN。
