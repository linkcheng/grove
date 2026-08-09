# P0 Blockers and Acceptance

> 架构集：GROVE v1.0
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> 本文件是阻断状态、POC 和发布 gate 的唯一权威来源。

## 1. 状态定义

设计状态与证据状态是两个独立轴，不能用“方案已经写进文档”冒充验证：

- **design_state**
  - `undecided`：职责、协议或选型仍有互相排斥的开放问题。
  - `architecture_decided`：唯一职责和关闭方案已固定；N-25/N-26 这类
    难逆决策还必须由 accepted ADR 固定。
- **evidence_state**
  - `open`：没有满足关闭记录要求的真实证据。
  - `verified`：真实目标环境通过全部关闭验收并写入关闭记录。
  - `waived`：仅由明确风险接受人、到期时间和替代控制临时豁免；不等于
    `verified`。

当前状态：

| 范围 | design_state | evidence_state | ID |
|---|---|---|---|
| 历史架构问题 | architecture_decided | 不适用；实现风险已转入 N 系列 | O-01 ～ O-07 |
| Execution Core / Platform API / Skill Governance | architecture_decided | open | N-03、N-05、N-07、N-08、N-15、N-16、N-18 ～ N-23、N-25、N-26 |
| Core Deployment Topology | architecture_decided | open | ADR-0023；G0 ～ G8；12.2 role matrix |
| Product MVP target Business Profile | undecided | open | ADR-0024；G3；发布前必须选择精确 ref/hash |
| Asset Risk Reference Business Profile | architecture_decided | open | 仅被产品选择时适用 POC-M；复用 N-08、N-15、N-16、N-18 ～ N-20、N-23 |
| Run Delegation | architecture_decided | open | N-27 |
| Execution Workspace | architecture_decided | open | N-30 |
| Memory | architecture_decided | open | N-14、N-17 |
| Durable Action / HA | architecture_decided | open | N-01、N-02、N-04、N-06、N-13 |
| Experience / Evolution | architecture_decided | open | E-01 ～ E-12 |

N-25/N-26/N-27 的难逆决策由 ADR-0004/0005/0006 固定；Core 物理部署与演进
边界由 ADR-0023 固定；其他关闭方案由各权威专题固定。`architecture_decided`
只表示本轮文档已消除设计分叉，不改变 `evidence_state`。
Product MVP 的目标 Business Profile 是尚未作出的产品发布选择，不是 Core 架构
分叉；它可以在 Core Walking Skeleton 实现期间保持 undecided，但进入 G3 实现前
必须冻结。

除 O 系列架构决策外，仓库当前没有 implementation/evidence artifact，因此
没有任何 verified P0。表格、代码或人工 demo 本身不能改变该状态。

P0 按目标 Profile 关闭，不能反向把 optional module 变成 Core 依赖：

```text
Core release
  └─ Core + Skill Governance + common production blockers

Product MVP release
  └─ Core release + selected Business Profile + G3 + Profile-specific POC

Asset Risk reference release
  └─ Product MVP release with Asset Risk Profile + POC-M

Memory release
  └─ Core release + Memory blockers

Durable Action release
  └─ Core release + Durable blockers

Run Delegation release
  └─ Core release + Run Delegation blockers

Execution Workspace release
  └─ Core release + Execution Workspace blocker

DBOS HA release
  └─ Durable + DBOS HA blocker

Experience Projection release
  └─ Core release + Experience blockers

Memory Curation release
  └─ Core + Memory + Experience Projection + Memory Curation blockers

Evolution release
  └─ Core + Experience Projection + Evolution blockers
```

未达到某个 optional gate 时，部署必须关闭 capability 声明，但不阻止不
依赖它的 Core 发布。
Business Profile gate 与 Capability Profile gate 正交：前者验证产品显式选择的领域
闭环，后者验证可选运行能力。平台没有隐式默认 Business Profile。Asset Risk POC-M
只阻断显式选择该参考 Profile 的 release，不把资产规则提升为 Core 或其他产品
blocker；选择其他领域必须提供自己的 Profile-specific POC 与 G3 evidence。
每个 optional Profile 还必须在该 capability 开启的配置下重跑适用的
common production blocker；例如 Run Delegation release 必须验证 N-07
Parent/Child topology 与 N-08 tenant isolation，不能只复用 Core-only 结果。

## 2. 原方案已关闭的问题

| ID   | 原问题                                  | 关闭决策                                                           |
| ---- | --------------------------------------- | ------------------------------------------------------------------ |
| O-01 | pydantic_graph 限制动态图               | LangGraph 是唯一 Execution Kernel                                  |
| O-02 | 自研 Pause/Resume/Checkpoint            | graph 恢复归 LangGraph；业务等待归 optional Durable Action         |
| O-03 | PostgreSQL 与 Redis job 双写            | 删除默认 Celery/arq；durable action 使用 PostgreSQL-backed adapter |
| O-04 | node 重跑重复副作用                     | DurableAction seam + 端到端稳定 idempotency key                    |
| O-05 | ExecutionContext 与 Graph State 分叉    | checkpoint 是 graph state 唯一真相                                 |
| O-06 | Agent 内部工具循环不可审计              | LangGraph 显式 route；PydanticAI 仅返回 typed result               |
| O-07 | Redis 同时承担 queue/broadcast/recovery | PostgreSQL event replay；NOTIFY 只唤醒                             |

## 3. Execution Core 与 Capability P0

| ID   | Profile           | 阻断问题                                                                                                 | 必须实现                                                                                                                                       | 关闭验收                                                                                                                                   |
| ---- | ----------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| N-03 | Core              | 旧 run 路由到新版，或历史执行制品/录制结果被提前删除                                                     | 固定 Graph/Contract/Manifest/Policy/RuntimeBuildManifest；从非终态 run、replay retention 和治理 hold mark-and-sweep pin；build-aware worker route | 发布 v2 后 v1 run 仍由原 build 恢复；保留窗内可 replay；被 pin 制品不可删除；缺 build/version/录制 fail fast                              |
| N-05 | Core              | PydanticAI 演变为第二个 Agent Runtime，隐藏 tool loop、state 或 durability                               | `TypedInferencePort`；禁 executable function Tool/toolset/MCP/Memory/durability；LangGraph 独占 route/lifecycle                              | adapter 配置 business Tool 或 durability 时启动失败；structured-output transport 可用；恶意`ActionProposal` 只进入 LangGraph policy node |
| N-18 | Core              | Canonical Model 变成第四份 State、重复 schema 漂移，或模型 payload 被直接执行                            | Canonical 是唯一 versioned schema；单向 Node Adapter；Model Payload/Decision/Command 分离；converter 绑定 Graph Version                     | 只有一个规范 schema；checkpoint 无独立 Platform/PydanticAI Context；伪造 tenant/auth 的 payload 被拒；历史 converter 缺失时 fail fast              |
| N-19 | Core              | PydanticAI provider/schema retry 与 LangGraph node retry 叠加，调用量失控                                | 按 failure class 指定唯一 retry owner；稳定 inference request ID；分层 budget 和 attempt event                                                 | 注入 schema/provider/node failure 后，实际模型请求次数严格等于 policy，耗尽后不被另一层盲目放大                                            |
| N-15 | Core              | Skill Version、dependency、permission/evaluation 或执行 ABI 漂移，或 Spec 膨胀为万能配置                 | immutable Version；content-addressed`SkillRuntimeManifest`；瘦 `SkillExecutionSpec` ABI；确定性 serialization/hash；发布 gate              | 依赖发布/弃用后已有 run 保持原 Manifest；ABI/Manifest/hash mismatch 在 node 启动前失败；Spec 无展开 Tool/Action/Memory/Workspace 配置      |
| N-16 | Core              | 缺 optional capability 时静默降级                                                                        | resolve 与 run-start 双检查；Disabled adapter 最终阻断                                                                                         | `MissingCapabilityError`；Graph node/action/memory write/workspace acquire 启动数为 0                                                    |
| N-20 | Platform API      | Plan 后配置变化、permission posture 被切换/绕过，或并发 submit/resume/cancel/fork 产生不同结果              | submit 重新 resolve/reauthorize；versioned permission preset 且无 bypass；expected hash；submission/command idempotency；revision CAS；checkpoint-bound one-time InterruptRef | 撤权/切 channel 正确拒绝；preset 不扩大 scope 且 run 内不可变；重复同 digest 返回原结果；不同 digest 冲突；并发 command 仅一个接受；旧 interrupt 不能注入 input |
| N-21 | Multi-Agent       | Agent/Role 成为重复 Capability，动态 route 逃出已评测 closure，或 fan-out/loop/reducer 非确定且失控          | Agent 仅绑定 root Skill Composition + Policy；RoleTemplate 只编译现有 delegation；Manifest 固定 closure；Kernel 校验 Proposal；stable branch/delegation key；keyed reducer；显式 GoalLoop 终止和总预算 | RoleTemplate 不能授予权限/动态 Tool；closure 外调用数为 0；乱序/重投 branch 的 aggregate hash 不变；同 key 不同 hash 冲突；达到 loop/depth/fan-out/预算上限后无新增调用 |
| N-22 | Platform API      | discover/preview/Observation 泄露能力或数据，或 preview 暗中执行模型和副作用                             | 每接口独立授权；preview 只读静态解析；Observation 只读 projection；optional capability 显式 unavailable                                        | 无权 capability 不可枚举；preview 的 model/node/Tool/Action/Memory/Workspace 调用数均为 0；跨 tenant observation 全拒绝                    |
| N-23 | Skill Governance  | Evidence 未绑定 permission/orchestration/workspace/context/interceptor/budget envelope、可伪造，任意“更小预算”复用证据，或单一总分掩盖安全回归 | subject hash 包含 permission preset/ceiling/effect/auth、Workspace、context、interceptor chain、subgraph/child/Join/loop policy 和 evaluated budget envelope；仅 ADR-0022 attested input subset 可复用；immutable Suite/Run；trusted issuer attestation；hard gate | 修改行为 policy 后 subject hash 改变且旧 evidence 失效；合法 input limit 收紧只改变 skill spec hash；伪造 subset/evidence 拒绝；hard gate/inconclusive 不得发布 |
| N-25 | Core              | FastAPI 崩溃后 run 丢失，或多个 worker 并发写同一 thread                                                  | PostgreSQL run command；applied-command metadata；SKIP LOCKED claim；lease/heartbeat；DB 原子 fenced saver/write guard；reconciliation          | 所有 crash point 最终恢复；command 语义只应用一次；同一 run 只有一个有效 fence；check/write 间换 fence仍拒绝；过期 worker 写入全拒绝       |
| N-26 | Core              | replay 取错/缺失录制后调用真实 seam，或 dry-run 原地提升权限和副作用                                      | replay/fork 创建新 run/spec；source-anchored binding；node/seam/ordinal recording key + request hash；禁止 fallback 与 run mode mutation       | source/alias 不变；缺失/错配录制均 fail fast；replay 对全部已启用真实 seam 调用数为 0；dry-run commit 产生新 run 与新授权                 |
| N-27 | Run Delegation    | Parent checkpoint、Child acceptance/completion/HITL 任一崩溃窗口导致重复 Child、孤儿 Run、永久等待、泄露结果或错误 Join | deterministic delegation/digest；prepared checkpoint proof；child spec/run/start/relation 原子 acceptance；delivery reauthorization；单 in-flight Run Signal；Child-authoritative HITL projection；reconciliation；取消传播 policy | 逐点 kill 后只有一个 Child Run，Parent 只应用一次结果并在 RTO 内恢复；未 checkpoint/提前/重复/篡改 completion 不误唤醒；Parent inbox stale reply 不恢复错误 Child；撤权不泄露结果；cancel race 符合 policy |
| N-30 | Execution Workspace | sandbox 逃逸、跨 run/tenant 泄露、旧 worker 操作当前环境、恢复依赖未持久 scratch，或 replay 调用真实 workspace | 内容寻址 policy/build/image；per-run isolation；fenced acquire/release；worker reattach；跨 checkpoint 文件只认 ArtifactRef；orphan reconciliation | 并发 run 不能读取对方或 host；越界 path/egress 全拒；takeover 连接原 instance；旧 fence 调用数为 0；replay/dry-run provider 调用数为 0；orphan 在预算内清理 |
| N-07 | Common production | runtime/UI event 顺序/schema 不稳定、拓扑/interaction 投影漂移，或跨 tenant source ID 冲突                  | tenant-scoped stable source ID；per-run/projection commit-ordered seq；typed closed-union event schema；outbox/inbox；source watermark；topology/interaction reconciliation | 重复、乱序、迟提交和跨 tenant 同名 source ID 后最终一致，SSE cursor 不漏；未知 schema 不猜测；topology 与 pending interaction 最终收敛；stale UI reply 被拒 |
| N-08 | Common production | 跨租户 checkpoint/knowledge/Tool/memory/workspace/action 引用，或 selection/disclosure 实现泄露未授权资源 | public-to-internal tenant mapping；每次重授权；RLS/组合约束；Manifest 固定 selection/disclosure policy；Asset Risk Reference Profile 采用 all-or-nothing | tenant A 所有跨租户操作失败并审计；每个 Tool 严格满足其 policy；选择 Asset Risk 时，混合 selection 无 View/子集/omitted count，且错误不区分不存在与未授权 |

## 4. Memory P0

| ID   | 阻断问题                                           | 必须实现                                                                                                  | 关闭验收                                                                       |
| ---- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| N-14 | Long-Term Memory 漂移破坏 replay；异步 recall 非确定；自动写入造成污染 | 固定 `memory_id/version/hash`；historical replay；显式 recall node 与 checkpointed included/timed_out/skipped；outbox typed candidate；TTL/consent/provenance | 更新/撤回后 replay 按显式模式；推理中途不注入迟到 recall；crash/retry 不直写 active；恶意/无来源 candidate 不 active |
| N-17 | Knowledge 与 Memory 权限/权威性混淆                | 独立 namespace/ACL；purpose filter；显式 Memory Promotion                                                 | 跨 scope recall/retrieve 拒绝；未 promotion 内容不进入 authoritative Knowledge |

## 5. Durable Action P0

| ID   | 阻断问题                                    | 必须实现                                                                         | 关闭验收                                                      |
| ---- | ------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| N-01 | action execution 与 Graph checkpoint 非原子 | `prepare -> checkpoint -> atomic accept -> dispatch`；固定 execution ID；receipt reconciliation | 每个持久化边界 kill 后只有一个 accepted request/execution     |
| N-02 | time travel 重复副作用                      | inspect 只读；replay/fork 新建 run；adapter seam 禁写；fork commit 独立授权       | action 前后 replay 均无新增事实；source run 不变               |
| N-04 | 对话 interrupt 与业务审批重复拥有决定或沿用过期授权 | typed wait state；唯一 approval；执行前重新授权；RunWaitRef + trusted Run Signal | 同一 action 只有一个决定；用户不能 resume action wait；伪造 signal 拒绝；撤权后不得执行 |
| N-13 | 外部成功但响应丢失，retry 重复事实或 receipt 泄密 | provider idempotency；receipt ArtifactRef；`unknown` reconciliation；能力分级 | fake provider 丢响应后事实数为 1；运行表/event 无 receipt 正文 |
| N-06 | DBOS HA                                     | Conductor + 多 executor；验证 orphan takeover                                    | kill executor 后在 RTO 内接管，无重复事实、无人工改库         |

N-06 只阻断 `durable_action.ha`，不阻断单 executor Durable Action Profile。

## 6. Experience 与 Evolution P0

| ID   | Profile         | 阻断问题                                                                           | 必须实现                                                                                                                      | 关闭验收                                                                                           |
| ---- | --------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| E-01 | Experience      | projector 丢失/重复、阻塞 GROVE，或原地修改/回退 immutable Manifest Head             | async outbox；immutable Manifest Version；watermark/status 单调 CAS Experience Head；idempotent projection/reconciliation      | kill projector 不影响 run；每 policy 一个 Head；incomplete→complete 产生新 Version；迟到旧投影与 revoked 均不能回退 |
| E-02 | Experience      | trace/input 泄露 secret、PII 或跨用途数据                                          | source redaction、reference-only manifest、purpose/consent/retention                                                          | 注入 secret 后 manifest 和无权 consumer 均不可见                                                   |
| E-03 | Experience      | Manifest 未固定完整版本、orchestration/trigger provenance 和发布 evidence，归因错误          | 固定 skill spec/runtime manifest、evaluation subject/release evidence、graph/model/prompt/tool/policy version、orchestration/parent/delegation、Trigger version/hash/occurrence ref 和 dataset hash | v2 发布后 v1 Experience 仍能标识原行为构建、触发与协作关系、门禁证据和运行配置 |
| E-04 | Memory Curation | Experience 自动写 active Memory                                                    | typed candidate、provenance、consent、sensitivity、conflict gate                                                              | 恶意/无来源/越权 candidate 全部拒绝                                                                |
| E-05 | Evolution       | Evolution 直接修改 active capability                                               | Candidate-only interface、immutable version、evaluation、approval、staged rollout                                             | direct update 被拒；旧 run 不漂移                                                                  |
| E-06 | Evolution       | reward hacking、过拟合、benchmark leakage                                          | holdout/golden、安全/成本回归、dataset provenance、人审                                                                       | 训练分升但 holdout/safety 降的 candidate 不可发布                                                  |
| E-07 | All optional    | Knowledge/Memory/Skill 分类漂移                                                    | typed candidate routing、显式 promotion                                                                                       | procedural 内容不写 Memory；未经 gate 不进入 Knowledge/Skill                                       |
| E-08 | All optional    | 跨 tenant 聚合 Experience/发布资产                                                 | tenant-scoped dataset/ACL；显式匿名化合同                                                                                     | tenant A 不能读取或训练 tenant B 数据                                                              |
| E-09 | Capability      | 中央 Registry/Catalog 成为恢复单点                                                 | Registry 分治；Catalog 只读可重建；run 保存完整 spec/hash                                                                     | Catalog 停机时已有 run 可恢复；Catalog 不能写 version                                              |
| E-10 | PostgreSQL      | 离线投影/评测耗尽在线资源                                                          | 独立 schema/role/pool、quota、batch rate limit                                                                                | Evolution 压测时在线执行 P95/连接数在预算内                                                          |
| E-11 | Evolution       | Candidate generator 自评、读取 holdout 答案或选择自己的唯一 judge                  | train/holdout 权限隔离；deterministic evaluator 优先；calibrated judge + high-risk human gate；职责分离                       | generator 无 holdout answer 权限；judge calibration 失败或高风险无人审时不可发布                   |
| E-12 | Evolution       | 执行量很大但 business outcome 无可靠归因，Evolution 优化采纳率等代理指标或选择偏差 | typed outcome source；attribution window；baseline/cohort；missingness/confound report；无法归因时`inconclusive`            | 构造流量选择偏差与延迟 outcome 数据，pipeline 不把相关性当提升；缺 outcome 的 Candidate 不自动发布 |

## 7. P1 生产阻断

| ID   | 问题                                               | 关闭验收                                                                                                         |
| ---- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| N-09 | cancel 被误认为撤销现实事实                        | 动作开始前/中/后取消，UI 和业务状态符合 policy                                                                   |
| N-10 | checkpoint/message/event/experience 膨胀或长上下文不可继续 | versioned `ContinuationSummary` + recent tail + source refs 原子 checkpoint；30 天等效容量测试满足延迟和存储预算 |
| N-11 | Runtime/UI SSE 慢 consumer 拖垮连接池/内存          | 无长事务、内存有界；按 run/projection cursor 重连不丢；snapshot + delta reconciliation                           |
| N-12 | PostgreSQL 共同故障域                              | PITR 恢复达到 RPO/RTO；任一 workload 不耗尽全部连接                                                              |
| N-24 | Plan cost/latency estimate 长期漂移，或遗漏 branch/Child/Workspace usage，误导预算和 UX | 按 Skill/build/model/orchestration/workspace profile 分桶比较 estimate 与含 descendants/workspace 的 actual，发布误差分位数、置信度和过期策略；超阈值时降级为 unknown range |
| N-28 | schedule/event trigger 重复、漏跑、并发穿透 overlap gate 或故障恢复后无限补跑 | stable occurrence/submission ID；原子 execution-head reservation；签名与 tenant mapping；misfire/concurrency policy；bounded catch-up；Trigger Adapter 只能调用 submit |
| N-29 | telemetry backend 故障反压在线执行，或 Baggage/attributes 泄露敏感上下文 | 有界 OTel SDK/OTLP export；Collector memory limit/batch/redaction/queued retry；Baggage allowlist；RuntimeEvent 与 telemetry 分离 |

## 8. 初始生产验收预算

在产品容量模型替换前，以下 **Reference Target v1** 具有约束力。替换必须在
POC/压测前经过评审并记录原因；`TBD`、平均值或“在预算内”不能关闭 blocker。

### 8.1 额定负载

| 指标 | Reference Target v1 |
|---|---:|
| submit/resume/cancel/fork command | 25 RPS，持续 15 分钟 |
| 同时 active LangGraph invocation | 100 |
| 同时 active Execution Workspace | 100（仅该 Profile） |
| 非终态 run（含 interrupt/wait） | 10,000 |
| 同时 SSE connection | 500 |
| RuntimeEvent 写入 | 200 events/s，单 event ≤ 64 KiB |
| 单 run 最大 graph step | 100 |
| 单 step 最大 dynamic fan-out | 32 |
| 单 orchestration 最大 logical delegation depth（subgraph + Child） | 8 |
| 单 orchestration 最大 Child Run depth | 8 |
| 单 Parent Run 同时 active Child Run | 32 |
| 单 orchestration 最大 descendant Run | 256 |
| 单 GoalLoop 最大 iteration | 20，且仍计入 graph step 上限 |

### 8.2 延迟、恢复与资源

| 指标 | 关闭阈值 |
|---|---|
| Plan/submit/resume/cancel/fork transport latency | 排除模型与排队，P95 ≤ 300 ms，P99 ≤ 1 s |
| committed run command → worker claim | 额定负载下 P95 ≤ 2 s，P99 ≤ 10 s |
| Core worker crash takeover | RTO ≤ 90 s |
| Action completion notification 丢失后的 reconciliation | ≤ 120 s |
| Child terminal → Parent Run Signal 可见 | P95 ≤ 2 s；notification 丢失后的 reconciliation ≤ 120 s |
| Workspace terminal/orphan cleanup | `execution.workspace` 下 ≤ 120 s |
| RuntimeEvent commit → SSE 可见 | P95 ≤ 2 s |
| topology projection 收敛 | 正常 P95 ≤ 5 s；projector 恢复后 ≤ 120 s |
| pending source → Interaction/UI 可见 | 正常 P95 ≤ 5 s；projector 恢复后 ≤ 120 s |
| telemetry backend/Collector 故障隔离 | 连续不可用 15 min 时 Run 无额外失败、进程内 telemetry 内存有界、drop/saturation 可告警；在线 P95/P99 不退化超过 10% |
| SSE reconnect | Runtime 使用 `run_seq`、UI 使用 `projection_seq` 补齐；最终 0 丢失、0 重复展示 |
| process crash RPO | 已提交 command/checkpoint/event 为 0 |
| PostgreSQL disaster recovery | PITR RPO ≤ 5 min，服务 RTO ≤ 60 min |
| 30 天等效容量测试 | checkpoint load P95 ≤ 250 ms；上述 API/SSE 阈值不退化超过 20% |
| PostgreSQL connection 配额 | online Core ≥ 70%，offline optional workload ≤ 20%，运维保留 ≥ 10% |

模型/provider 的端到端 latency、token 和费用按
`evaluation_subject_hash/model_policy` 单独设 Skill budget，不混入 transport
SLO。DBOS HA Profile 同样使用 90 秒 takeover 上限。

## 9. POC

### POC-A：Durable Action 幂等交接

```text
typed decision
  → prepare_action
  → LangGraph checkpoint
  → atomic action acceptance
  → dispatch_action
  → fixed DBOS workflow ID
  → fake provider
  → completion bridge
  → trusted Run Signal
```

逐点 kill，并模拟 provider 成功后丢响应，证明同一 request 最终只有一个
execution 和一个外部事实。该断言使用支持 idempotency key 的 fake provider；
对“不幂等且不可查询”能力，验收结果应是 `unknown/manual_review` 且无自动
重试，不能宣称 exactly-once。
复用相同 request/key 但篡改 input/action digest 时必须
`ActionRequestConflict`，已有 execution 不变且 provider 调用数不增加。

### POC-B：Time Travel 安全

Core 子集：

1. 完成一次包含 Inference、Knowledge 和 read Tool 的 run；启用 Memory 时再
   包含一次 recall。
2. inspect 只读 source run，不创建执行 run。
3. 从各 seam 前后 checkpoint 创建 replay run；验证对应录制结果被复用。
4. 删除任一必需录制结果，返回 `ReplayDataUnavailable`，真实 adapter 调用
   数仍为 0。
   篡改 ordinal/request/result hash 时返回 `ReplayDataMismatch`，不得误用
   相邻 recording。
5. replay/fork_dry_run 均有新 run/spec/thread，source run/checkpoint 不变。
6. source run 后移动 Agent alias/Skill release channel，replay/fork 仍绑定
   source build；尝试通过 `ForkExecution` 切换 Graph/build 必须拒绝。

Durable Action 扩展：

1. 完成一次有副作用的 run。
2. 从 action 前后 checkpoint replay；Action recording 被复用，外部事实数
   不增加。
3. 从 dry-run checkpoint 创建独立 `fork_commit`，重新授权后以新 run key
   产生一次 action；不存在 run mode UPDATE。

### POC-C：Version 恢复与 DBOS HA

1. Skill/Graph v1 在 interrupt 暂停。
2. 发布不兼容 Skill/Graph/Runtime Build v2。
3. v1 run 继续使用原 spec/Graph，并只由匹配 v1 Runtime Build 的 worker
   恢复；移除 v1 worker 时明确 `VersionUnavailable`，不路由到 v2。
   伪造 worker build hash、篡改 manifest signature/image digest 时 claim
   必须拒绝。
4. DBOS workflow 由匹配 application version 恢复。
5. HA Profile 下 kill executor，由 Conductor 在 RTO 内接管。

步骤 4、5 只适用于相应 DBOS Profile。

### POC-D：Permission、Tenant 与 Event

1. PydanticAI 返回伪造 tenant/auth/idempotency/fence 的恶意 Payload，验证
   schema 拒绝；合法 `ActionProposalPayload` 只能经 Node Adapter 与 policy
   node，且在授权前无外部请求。
2. 并发 approve/reject 只有一个决定。
3. approval 后撤销权限或改变 resource state，真实副作用调用数为 0。
4. tenant A 枚举 tenant B 的所有 public ID，全部拒绝。
5. 两个 tenant 使用相同 source/source_event_id 不冲突；同 tenant 重复、
   乱序、迟提交后 audit 不重复、SSE cursor 不漏。
6. 删除 completion notification，对账最终恢复 Graph。
7. 伪造 public resume/RunSignal 的 wait ref、source ref、schema 或 hash，
   checkpoint/reducer 应用数为 0。
8. 对四种 approved permission preset 运行同一 operation matrix；任何 preset
   都不扩大 scope，`read_only` 写调用数为 0，`workspace_edit` 不自动批准
   external effect，`unattended` 不产生 permission prompt，`bypass` 无法解析。
   活动 run 切换 preset 被拒并保持原 spec/hash。
9. 对 Interaction Projector 注入重复、乱序、未知 schema、停机和重连；snapshot
   + typed delta 最终收敛。伪造 CustomEvent、旧 revision、跨 run source ref
   或已解决 item 的响应都不能恢复 Graph/批准 Action。
10. 使用同一 golden UI event stream 驱动前端 reducer 两次，得到相同
    `RunViewState`；制造 cursor gap、command accepted 但 projection 延迟和
    tenant 切换，页面不乐观完成、不越序应用且旧 tenant store/SSE 被清空。

Core Release/最小 Product MVP 只执行步骤 1 的 schema/trust-boundary 部分以及步骤
4、5、8、9、10；步骤 1 的 Action route、步骤 2、3 属于 Durable Action Profile，
步骤 6、7 按启用的 Durable Action 或 Run Delegation Profile 执行。关闭的 capability
必须返回 unavailable，不能用 mock 成功结果关闭可选 Profile 的验收。

### POC-E：Knowledge 与 optional Memory 治理

MVP Knowledge Baseline：

1. 将 `KnowledgeSnapshot@1` 绑定到所选 Business Profile 的 root Skill Run，再发布
   Snapshot v2；
   已有 Run 的 retrieve、Citation 和 Spec hash 仍固定 v1，新 Run 才使用 v2。
2. 每个 `ok` item 都能验证 Snapshot/source version/locator/content hash；删除、
   篡改 Snapshot 或 adapter build 不匹配时，Graph node 与真实 retrieve 调用数为 0。
3. Tenant A、无 scope Principal 和模型伪造 Knowledge/Tenant/ACL 分别尝试读取或
   枚举 Tenant B source；结果全部拒绝且不泄露 source 是否存在。
4. 对相同 request 分别注入 `ok/empty/denied/timeout/unavailable`，得到五种稳定
   outcome；只有 `ok` 携带 items/citations，模型不得把其他状态补写为企业事实。
5. 超过 query/result/token/row/deadline budget 时在 seam 有界截断或 typed failure；
   RuntimeEvent、trace 和日志不出现 Knowledge 正文。
6. 固定同一政策 Knowledge Snapshot，在两次 Run 之间修改 Live Business State；
   Citation 保持相同，而 read ToolResult 的 `observed_at`、source revision/
   watermark 或 result hash 能区分实际读取值。Live Business State 不通过
   KnowledgePort 返回，Run Inspect 可查看当次 Run Data View provenance。具体
   Tool 的 schema、事务、调用预算、partial 与 selection 行为在对应业务 Profile
   POC 中验证。

Long-Term Memory 扩展：

7. Core-only 部署运行要求 Memory/Durable capability 的 Skill，fail fast。
8. checkpoint 后更新/撤回 Memory，historical replay 不读取 current。
9. 恶意/无 provenance MemoryCandidate 不 active；未 promotion Memory 不出现在
   authoritative Knowledge。
10. optional recall 分别在 deadline 前完成、超时和跳过；进入 inference 前的
   checkpoint 精确记录 `included/timed_out/skipped`，迟到结果不在中途注入。
11. candidate outbox/record/review 各边界 kill/retry 后，最多存在一个同 digest
    candidate，active Memory 写入数在治理批准前为 0。

步骤 1～6 属于 MVP Baseline；步骤 7～11 只在 Long-Term Memory release 启用后
适用。

### POC-F：Canonical Contract 与 Retry 边界

1. LangGraph State 经 Node Adapter 只投影当前 inference 所需字段。
2. PydanticAI internal message/RunContext/provider object 不进入 checkpoint。
3. 全仓只有 `16` 专题/对应 schema package 定义 normative Contract；集成文档
   不复制 class。
4. Payload 不含 meta/run/tenant/auth；Node Adapter enrichment 后才形成
   Decision，policy node 后才形成 Request/Command。
5. 部署 Canonical Contract v2 后，历史 v1 run 使用 v1 converter；缺失时
   fail fast，不能 fallback 到 latest。
6. schema validation 连续失败时，只由 PydanticAI 消耗指定 retry budget。
7. provider retry exhausted 后，LangGraph 进入 error edge，不使用默认
   node retry 重复同一 logical inference。
8. PydanticAI 可使用 structured-output transport，但 executable business
   Tool、toolset、MCP 和 durable capability 均无法注册。
9. context budget 触发显式 `compress_context`；summary、recent tail、成对
   Tool/Result、pending refs 和 source range hash 原子 checkpoint。逐点 kill
   后 replay 使用同一 summary/hash，不以当前模型重算。
10. 恶意 interceptor 尝试改 tenant/auth/effect/schema、增加 Tool、返回
    `ALLOW` 或跳过 terminal，全部在真实 adapter 前拒绝；telemetry Observer
    故障按有界 policy 降级并产生 health signal。

### POC-G：Platform API 与 SkillExecutionSpec ABI

1. discover 只返回当前 tenant/actor/profile 可见 Skill。
2. estimate/validate/preview 全程不启动 Graph、模型、Tool、Action、Memory 或
   Workspace。
3. preview 后撤销 actor scope，submit 必须重新授权并拒绝。
4. preview 后移动 Agent alias/release channel；携带旧 expected hash 的 submit
   返回 `PlanChanged`。
5. 同一 `submission_id` 重复提交相同 digest 返回原 run，修改 digest 返回
   `SubmissionConflict`。
6. 同一固定 Registry snapshot 两次 resolve 产生相同 Manifest 与 canonical
   spec hash。
7. Spec 顶层不存在展开的 inference/knowledge/memory/workspace/tools/actions
   配置，且未知 policy kind/自由格式 extras 解析失败。
8. 删除或篡改 Graph/Contract/Manifest/Policy artifact，Graph node 启动数为 0。
   删除/篡改 RuntimeBuildManifest 或只保留不匹配 worker 时同样为 0。
9. 恶意 `DelegateProposal/ToolProposal/ActionProposal` 指向 Manifest closure
   外 ref，全部在 Kernel policy node 拒绝。
10. 相同 `command_id` 相同 digest 返回原结果，不同 digest 冲突；并发
    resume/cancel/fork 只有一个通过 expected revision CAS。
    使用旧 checkpoint、跨 run 或已消费 InterruptRef 的 resume 全部拒绝，
    reducer/input 应用数为 0。
11. 未启用 Experience 时，Observation 返回 `CapabilityUnavailable`；跨
    tenant events/trace/artifact/evaluation/experience 查询全部拒绝。

### POC-H：Skill Evaluation 发布门禁

1. 固定 Suite、dataset、`evaluation_subject_hash`、
   `permission_envelope_hash`、Runtime Build、model、evaluator 和环境版本。
2. 运行 baseline/candidate differential，生成 immutable evidence bundle。
3. candidate 质量提高但 safety、permission 或 protected segment 任一 hard
   gate 失败，Publication 必须拒绝。
4. 样本不足导致 `inconclusive`，不能按 passed 处理。
5. model judge 在人工校准集失准时，judge result 不得作为发布依据。
6. Observation API 可查询已授权 evidence，但不能 approve/publish/rollback。
7. 修改 authorization policy、permission ceiling、Tool/Action effect、
   Workspace policy/build、subgraph/child mode、Join/GoalLoop terminal policy
   或 orchestration budget，subject hash 必须改变，旧 evidence 不得用于发布。
8. 篡改 bundle/subject hash、伪造 issuer/signature 或复制其他 tenant 的
   `passed` 行，Publication 全部拒绝；真实 evaluator 调用数不因重放而增加。
9. 对 Manifest 声明的 monotonic input limit 调低 effective 值：保持 ceiling
   `evaluation_subject_hash/evidence_set`，改变 `skill_spec_hash`，只执行确定性
   comparator/contract/UX 测试。调高、未知 key、非正整数、篡改 attestation 或改变
   token/deadline/fan-out 等 budget 时不得复用 evidence。

### POC-I：Execution Driver 单写者与恢复

1. 两个 worker 同时 claim 同一 start/resume/signal command，只有一个获得有效
   `execution_fence`。
2. 分别在 command commit 后、claim 后、模型返回后 checkpoint 前、
   checkpoint 后 command consumed 前 kill worker。
3. 所有非终态 run 在 90 秒内由 reconciliation/takeover 继续，command 不丢。
4. 让旧 worker 在 lease 过期后恢复；其 checkpoint、event、MemoryCandidate、
   Action acceptance 和 Child Run acceptance 全部以
   `StaleExecutionFence` 拒绝。
5. 丢弃 NOTIFY，只靠 polling/reconciliation，run 仍完成。
   对 `status=running` 且无 lease/command 的 run，只生成一个 deterministic
   internal `continue` command；waiting/terminal run 不生成。
6. 重复相同 command 只产生一个语义结果；dead-letter 形成告警，不直接修改
   checkpoint。
7. 在 Reference Target v1 额定负载下满足 command claim、连接池和恢复预算。
8. 模型调用期间接受 cancel，旧 fence 立即失效；模型返回后 checkpoint、
   event、Action acceptance 和 Child Run acceptance 均为 0，未交接的
   action/child 不会启动。
9. 在 fence check 与 checkpoint write 之间强制 takeover；即使应用线程继续，
   数据库也原子拒绝旧 checkpoint，证明不存在 check-then-write TOCTOU。
10. 让 cancel 与 action acceptance 竞争同一 run lock：cancel 先提交时
    ActionRequest 数为 0；acceptance 先提交时只有一个固定 execution，后续
    行为完全由 action cancel/compensation policy 决定。
11. 在 resume checkpoint 已提交、command 未 consumed 时 kill；重领后只
    确认 consumed，resume input/reducer/下游 node 的语义应用次数均为 1。

### POC-J：Multi-Agent 编排与 Child Run

Core 子集：

1. 同一 Sub-agent 并行调用使用不同 per-invocation namespace；故意把同一
   per-thread namespace 并行调用时，在执行前被 policy/graph validation
   拒绝。
2. 随机化 32 个 `Send` branch 的完成、失败和重投顺序；keyed reducer 的
   canonical aggregate hash 始终相同。同 branch 相同 hash 幂等，不同 hash
   明确 `BranchResultConflict`。
3. GoalLoop 分别因 success、iteration limit、no-progress、token/cost 和
   deadline 退出；达到 terminal 条件后模型、Tool、Action、delegation
   调用数不再增加。通过 resume 尝试修改 goal contract/required criteria
   时被拒绝，原 State 不变。
4. 恶意 Delegate Payload 伪造 tenant、authorization、delegation ID、
   child mode 或 closure 外 Skill，全部在 policy node 前后正确拒绝。
   subgraph 与 Child 交替递归达到 logical delegation depth 8 后，任何第 9
   层调用数为 0。RoleTemplate 指向 closure 外 Skill、携带 permission grant、
   动态 Tool/credential 或填充 schema 外字段时也全部拒绝。

Run Delegation 扩展：

5. 在 Parent delegation checkpoint 前后、child acceptance transaction
   各写入点、Child terminal 后、signal enqueue 前后、Parent signal
   checkpoint 前后逐点 kill。
   只存在内存但没有 prepared checkpoint proof 的 DelegationCommand 必须
   拒绝；篡改 permission/budget allocation ref/hash 同样拒绝，Child Run
   数为 0。
6. 每个 `delegation_id` 最终只有一个 child spec、Child Run、start command
   和 coordination relation；Parent reducer 只应用一次 completion。
   orchestration admission 的 active child 只增减一次并最终归零，累计
   descendant count 保持 1，不能因 retry 回退或重复增长。
7. Child 在 Parent 进入 wait 前完成，删除 notification；reconciliation 在
   120 秒内用同一 signal 恢复 Parent。
   多个 Child 同时完成时，每个 Parent 最多一个未消费 signal command。
8. 重复相同 completion 幂等；篡改 child run、terminal revision、result hash、
   tenant 或 wait ref 全部拒绝且 Parent State 不变。
   分别在 result authorization 前后、signal acceptance commit 前撤销 Parent
   的 result scope，signal 只返回不含业务结果的 denied DelegationResult；
   commit 后撤权不改写历史，但无权 Observation/Artifact 读取仍被拒绝。
9. cancel 与 child acceptance 竞争：cancel 先提交时 Child Run 数为 0；
   acceptance 先提交时按固定 attached/detached propagation policy。
10. `all/any_success/quorum/collect/detached` 分别产生预期 Join；迟到结果不能
    重开 terminal Parent。
11. replay/fork dry-run 使用 delegation recording，真实 Child Run 创建数为
    0；fork commit 使用新的 orchestration/delegation/submission namespace。
12. Parent Execution SSE 只有 Child lifecycle 摘要；Child detail 需单独授权，topology
    projector 在 backlog/未知 schema 时标记 partial/stale，并在乱序、重复、
    停机恢复和 reconciliation 后收敛到同一 complete 树。
13. Child interrupt 投影到 Parent inbox 后，响应 exact Child InterruptRef 可
    恢复 Child；重复、过期、跨 Child、取消后的响应全部拒绝。projector 停机
    不改变 Child wait；业务审批 item 只能走 Action approval，不能 public resume。

步骤 5～13 只在声明 `run.delegation` capability 时适用。

### POC-K：Schedule 与 Event Trigger

1. 同一 schedule occurrence 重复投递 100 次，只产生一个 Agent Run；固定
   timezone/parser 下的夏令时 gap/fold 和 Trigger Version 变更产生预期的
   canonical occurrence。
2. 分别在 occurrence/head reservation 前后、Execution API request/response
   前后和 `active_run_id` 回填前后 kill Adapter；reconciliation 重试相同
   `submission_id`，不产生第二个 run。
3. 同 occurrence 篡改 intent/template hash 返回 `TriggerConflict`。
4. 伪造 event signature、tenant、principal 或过期 replay window，run 创建数
   为 0。
5. 停机后分别验证 `skip/run_once/catch_up_bounded(n)`；补跑数量不超过 n。
6. 两个 Adapter 并发处理 tick，且前一 submission 尚未回填 run ID 时，
   `forbid_overlap` 仍只允许一个 active slot；其余 occurrence 明确 skipped。
   若声明 `replace`，它只通过公开 cancel command，并在旧 Run terminal 后
   提交替代 Run。

POC-K 关闭 P1 N-28，不阻断没有 schedule/event 入口的 Core release。

### POC-L：Execution Workspace 隔离与恢复

1. Core-only 部署运行要求 `execution.workspace` 的 Skill，验证
   `MissingCapabilityError`，workspace 与 Tool provider 调用数均为 0。
2. 在 Reference Target 下并发启动 100 个 run（同时覆盖不同 tenant 与同
   tenant），使用相同文件名；跨 run 读取、host path、symlink traversal、
   未授权 mount 和 egress 全部拒绝并审计。
3. 分别触发 CPU/memory/process/disk hard limit；违规 workspace 被拒绝或终止，
   其余 run 仍满足既有 SLO，provider/worker 无无界资源增长。
4. 在物理 acquire 后、handle checkpoint 前后分别 kill Core worker；新 worker
   使用精确 binding 重新连接同一 instance。旧 fence 的 acquire/release/Tool
   调用数为 0；provider 无法证明原 instance 时明确
   `WorkspaceUnavailable`，不静默创建替代环境。
5. 在 workspace mutation 后、artifact upload 后且 ToolResult checkpoint 前、
   checkpoint 后分别 kill；恢复只能依赖已 checkpoint 的 typed
   result/ArtifactRef，未提交 scratch 不得被当作存在。
6. 同一 run 的两个并发 `workspace_scope` 使用稳定独立 namespace；并发写同一
   path 在执行前拒绝。若同时启用 Multi-Agent/Run Delegation，再验证 branch
   映射稳定且 Child Run 使用独立 workspace，只通过 ArtifactRef 交换文件。
7. replay/fork dry-run 不复制 source handle，只消费精确 Tool recording，真实
   acquire/Tool provider 调用数为 0；伪造 source handle 必须跨 run 拒绝；
   篡改 policy/build/bootstrap hash 返回 `ReplayDataMismatch`；fork commit
   创建新的 run workspace。
8. terminal/cancel 后丢弃 release notification，只靠 reconciliation 在
   120 秒内清理 instance；RuntimeEvent/trace 不含命令、文件、主机路径或 secret。

POC-L 只关闭 `execution.workspace` Profile 的 N-30，不阻断 Core release。

### POC-M：Asset Risk Reference Business Profile

本 POC 只验证
[Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md)，且
只在产品显式选择该 Profile 时适用。不能把其单次读取、事务、partial 或 selection
规则推导为 Execution Core 的全局约束，也不能用本 POC 替代其他领域的 G3。

1. 对 `AssetStateQuery@1` 注入 filter、search、query DSL、`all_assets`、pagination、
   sort、SQL、database/schema/table/column/join、Tenant/scope/credential/limit、
   extra field 或 closure 外 Tool ref；Tool provider 与 database 调用数均为 0。
   空、重复或超过固定上限的 `asset_refs` 同样在 database 前 contract fail。
   分别设置低于、等于和高于 Manifest `max_asset_refs` 的 Deployment/Tenant 值：有效
   值取最小值；高于 ceiling 时 spec/run/provider/database 创建或调用数均为 0；合法
   降低保持 ceiling `evaluation_subject_hash/evidence_set`，但改变新 Run 的
   `skill_spec_hash`，活动 Run 仍使用原值。篡改 comparator、limit key 或 attestation
   同样在 run/provider 前拒绝。
   使用 golden dataset 分别测量单资产 P99 row、result byte、context token 和目标
   deadline 内的稳定容量；候选 ceiling 取四项约束的最小值，再乘 `0.8` 并向下取整。
   closing record 必须保存最终正整数、分布、预算、环境、报告/hash 和 Manifest
   hash；缺失任一项时 POC-M 不得关闭，运行配置也不得提供隐式默认值。
2. 固定同一政策 Knowledge Snapshot，在两次 Run 间修改当前资产状态；Citation
   保持相同，而 `AssetStateView@1` 的 `observed_at`、source revision/watermark
   或 result hash 能区分实际读取值。KnowledgePort 不返回当前资产状态。
3. 在 adapter 的多个固定 statement 之间并发更新 source；accepted View 来自同一
   `READ ONLY REPEATABLE READ` snapshot，transaction 在 checkpoint/Inference 前
   关闭。
4. 分别在读取返回前、返回后且 checkpoint 前、checkpoint 后 kill worker；前两者
   可按同 logical key 有界物理重读但最终只接受一个 result，checkpoint 后 database
   调用数为 0。第二个 logical success 在 provider 前拒绝。
5. 分别超过 row/byte/token/deadline limit；只得到 canonical
   `asset_state.query_too_broad` / public `ToolQueryTooBroad`，View、partial
   Artifact、第二个 logical success 与 Inference 调用数均为 0。
6. 分别混入不存在、同 Tenant 越权、跨 Tenant、读取中删除和重复 `asset_ref`；
   重复 ref 在 database 前 contract fail，其余只得到 canonical
   `asset_state.selection_unavailable` / public `ResourceSelectionUnavailable`。
   授权子集、omitted count、View、Artifact 与 Inference 调用数均为 0。
7. API、`domain_read_failed`、UI、普通日志与 metric 不泄露失败 ref、匹配数、
   not-found/denied 原因、SQL、内部 limits 或资产正文；`domain_view_accepted` 只
   携带 Profile 允许的 safe provenance。
8. 已 checkpoint 的 Run 没有 refresh-in-place；重新 submit 产生新的
   Run/spec/authorization 与 `AssetStateView@1`，旧 Run 的 checkpoint/hash 不变。

### POC-X：Experience 不影响执行

1. 在 projector 各边界 kill/restart。
2. 重复、乱序、遗漏 event，再运行 reconciliation。
3. GROVE run 正常完成；每个 policy 只有一个 Experience Head。
4. incomplete → complete 和 consent revoke 分别创建新 immutable Version，
   历史 Manifest bytes/hash 不变。
5. Manifest 只含已授权和脱敏 reference；revoked Head 不再被 consumer 使用。
6. complete/revoked 后投递更低 watermark 的 incomplete projection，Head
   revision/content hash 不变；重新 consent 只能进入新 policy lineage。

### POC-Y：Memory 类型路由

同一 Experience 分别生成：

- 任务结果 → Episodic Memory。
- 用户偏好 → Preference Memory。
- 企业规律 → KnowledgeCandidate。
- 操作流程 → SkillCandidate。

验证后两类不进入 active Memory。

### POC-Z：受治理 Evolution

1. 固定 dataset snapshot，生成 Skill v2 Candidate。
2. Evolution 不能直接写 active Skill。
3. v2 质量通过但 safety/holdout 失败，不能 publish。
4. 全部门禁通过并审批后产生不可变 v2。
5. v1 run 不漂移；新 run 才按 release channel 选择 v2。
6. rollback 只移动 channel，不修改或删除 v2。
7. Candidate generator 无 holdout answer 权限；Evaluation evidence 固定
   suite/dataset/spec/model/judge versions。
8. v2 总质量提高但 hard safety 或 protected segment 回归，仍不可发布。
9. 注入流量选择偏差、缺失和延迟 business outcome；无法可靠归因时结果为
   `inconclusive`，不能自动发布。

## 10. 测试策略

### Contract

- Platform API：Plan/Execution/Observation schema、独立授权、PlanChanged、
  permission preset、Interaction snapshot/UI delta、command idempotency、
  expected revision 和 fork lineage。
- Frontend RunInteractionModel：typed reducer、message/item revision、cursor
  gap recovery、projection health、command confirmation 和 tenant-scoped cache。
- Skill Runtime：immutable version、closure、permission、capability。
- SkillExecutionSpec ABI：瘦顶层、Manifest ref、canonical bytes/hash、
  permission preset/interceptor binding、BudgetBinding/monotonic input subset、
  artifact binding、dynamic closure、v1/v2 reader。
- ExecutionWorkspacePort：acquire/release command digest、fence、handle
  tenant/run binding、Tool effect、ArtifactRef commit 和 replay 禁用。
- Canonical Contracts/Node Adapter：唯一规范 schema、Payload/Decision/
  Request/Command trust split、v1/v2 golden conversion、minimum state
  projection、ContinuationSummary、InteractionItem/UIProjectionEvent closed
  union、缺 converter fail fast。
- TypedInferencePort/PydanticAI adapter：golden typed I/O、invalid output、
  refusal、无 executable Tool/toolset/MCP/durable state、structured-output
  transport、bounded provider/schema retry。
- Execution Driver：start/resume/cancel/continue/signal claim、lease/fence、
  takeover、reconciliation、stale writer rejection、dead-letter。
- LangGraph：subgraph persistence mode、stable branch key、keyed reducer、
  route、GoalLoop limit、compress_context、interrupt/resume、recorded replay、
  new-run fork lineage。
- Run Delegation：deterministic command/digest、atomic Child Run acceptance、
  RoleTemplate compile、RunWaitRef/RunSignal、Parent/Child HITL、Join、cancel
  propagation 和 completion reconciliation。
- Trigger Adapter：stable occurrence/submission ID、signature/tenant mapping、
  misfire、overlap、bounded catch-up。
- KnowledgePort：单一 Snapshot binding、Citation/source version/hash、ACL、
  purpose/budget、`ok/empty/denied/timeout/unavailable` outcome。
- Typed Tool / Run Data View：Manifest 固定 ref/schema/effect、权限、logical call、
  limits、partial/selection policy 与 adapter compatibility；成功 provenance、
  checkpoint/ArtifactRef、恢复复用和通用 failure projection。
- 选择 Asset Risk Reference Profile 时：`asset.state.read@1` 的强类型 schema、无
  通用 SQL、单 logical success、短一致性 transaction、超限 fail closed、selection
  all-or-nothing 与无 partial result；精确证据由 POC-M 关闭。
- MemoryPort：deterministic recall inclusion、candidate outbox、record/forget、
  TTL、consent、provenance、promotion。
- Adapter Interceptor：固定 chain/order/hash、Observer/Transformer/Guard 权限、
  semantic-subset post-validation 和 per-hook failure policy。
- InteractionProjector：stable item、source watermark、snapshot + typed delta、
  reconciliation、stale response 和 Child ownership routing。
- Observability/Operations：RuntimeEvent/audit/OTel 分层、correlation、label
  allowlist、redaction、exporter failure isolation、health/dashboard/alert/runbook。
- DurableActionPort：disabled/fake/DBOS 使用同一 capability、执行时重授权、
  receipt reference、fence、禁写和幂等契约。
- ExperienceProjector：immutable version、Head CAS、stable ID、reference-only、
  redaction、revocation 和 reconciliation。
- Evaluation/Evolution publication：Suite/Run、holdout isolation、hard gate、
  Candidate-only、approval、rollout。

### Fault injection

- LLM timeout/stream break/invalid JSON。
- schema retry exhausted、provider retry exhausted、两层 retry policy 冲突。
- node 完成后 checkpoint 前崩溃。
- command commit/claim/heartbeat/lease expiry/consume 各边界崩溃。
- 旧 worker 在 fence 失效后继续返回或尝试 Action/Child Run acceptance。
- delegation checkpoint、Child Run acceptance、terminal observation、
  signal enqueue/apply 和 Parent Join 各边界崩溃。
- Child terminal 早于 Parent wait，completion notification 丢失、重复或篡改。
- same-run branch 随机完成/重投；同一 per-thread subgraph namespace 并发。
- GoalLoop 连续产生等价 proposal、虚假 progress 或耗尽各层 budget。
- model request 在途时提交 cancel，随后让旧 worker 返回结果。
- resolve 后发布/弃用依赖。
- Plan 后撤权、移动 release channel 或改变 deployment capability。
- spec artifact 被删除、hash 被篡改或 ABI reader 缺失。
- workspace acquire 后 worker 崩溃、旧 fence 延迟返回、provider 不可重连、
  release notification 丢失和 orphan cleanup 重投。
- Memory recall 后修改/撤回/过期。
- recall deadline 前后完成、candidate outbox/record/review 边界崩溃。
- context compression inference/checkpoint 前后崩溃、pending ref 缺失或 hash
  篡改。
- DBOS 启动后 handle 写回前崩溃。
- approval 后、真实 side effect 前撤销权限或改变资源状态。
- provider 成功后响应丢失。
- webhook 重复/乱序/伪造。
- schedule tick 重复、scheduler 长时间停机和 event replay storm。
- Collector/telemetry backend 断开、限速和 queue saturation；在线执行不得
  因 export backpressure 失败或无界增内存。
- topology/interaction projector 停机、event 缺失、未知 UI schema、artifact
  删除和 source watermark 落后。
- 浏览器在 UI event backfill、live delta、command receipt 与 projection confirm
  各边界刷新/断网；恢复后不重复展示或提交。
- Guard/Transformer/Observer 分别超时、抛错或返回越权变换。
- PostgreSQL slow query/pool exhaustion/failover。
- read Tool 的 provider/checkpoint 边界 crash、checkpoint 后 takeover、重复 logical
  proposal 与各 Manifest budget；选择 Asset Risk 时，其 transaction 并发更新由
  POC-M 覆盖。
- SSE slow client/reconnect storm。

### Security

- 跨 tenant public ID、retrieve、recall、record、resume、signal、approve、
  cancel、fork。
- Prompt Injection 选择未授权 action、读取 secret、伪造 Memory provenance。
- 选择未批准/bypass permission preset，或在活动 run 中切换 posture。
- replay/fork 绕过 approval。
- Model Payload 伪造 tenant、principal、authorization、idempotency 或 fence。
- Tool Payload 注入 Tenant/scope、credential、timeout/limit、adapter 实现字段或
  extra field；全部在 provider 前拒绝。数据库型 Tool 额外覆盖 SQL/database/
  schema/table/column/join 注入。
- selection 混入不存在、不可见、无权、跨 Tenant 或竞态失效 ref；验证结果严格
  符合 Manifest disclosure policy。选择 Asset Risk Reference Profile 时还必须证明
  public error/timing bucket、UI、RuntimeEvent、log 和 metric 不标识具体 ref、匹配数或
  not-found/denied 原因，也不返回授权子集。
- Model Payload 伪造 delegation/branch/Child Run/Run Signal，或选择 closure
  外 Skill/child mode。
- 伪造 InteractionItem/UIProjectionEvent、Child owner run、revision 或 response
  ref；投影不得成为授权。
- tenant/actor 切换时保留旧 query cache、SSE、draft 或 Artifact URL；测试必须
  证明全部关闭/清除。模型 Markdown 注入 HTML/script/unsafe URL 必须被拒绝。
- interceptor 伪造 ALLOW、tenant/principal、effect、schema 或动态 Tool。
- 跨 tenant Parent/Child relation、topology、completion signal 和 Trigger
  occurrence。
- Workspace 跨 tenant/run handle、host path/symlink escape、未授权 mount、
  egress、credential 注入和共享 branch path。
- 恶意 trace header/Baggage 伪造 tenant、principal、authorization 或注入
  credential/PII；可信执行上下文不得改变，export 前必须脱敏。
- Experience 跨 purpose/consent 使用。
- Candidate 跨 tenant dataset 或绕过 publish gate。
- capability enumeration、preview/topology/trace/artifact/evaluation/
  experience 越权。
- Evolution generator 读取 holdout answer 或选择自己的唯一 judge。

## 11. P0 关闭记录

不能用“代码已写”或人工 demo 关闭。每个记录至少包含：

```text
blocker_id
profile
owner
test_suite/test_case
fault_injection_point
observed_invariant
framework/application versions
reference_target_version
observed_load_and_percentiles
evidence_artifact
reviewer
closed_at
```

持久化/恢复类 P0 必须在真实 PostgreSQL 和实际选定 runtime/adapter 版本
运行。mock 只验证 interface。

首次 POC 前必须发布 content-addressed `RuntimeBuildManifest` 和对应
`framework-baseline` evidence，至少固定 Python、PostgreSQL、
LangGraph/PostgresSaver、Pydantic/PydanticAI、DBOS 及 adapter
build/container digest；启用 Run Delegation 时还要固定 Coordinator/
Completion Bridge build；启用 Execution Workspace 时还要固定 workspace
adapter 与 sandbox image digest。禁止使用版本范围、浮动镜像 tag 或
`latest` 关闭兼容性测试。

关闭记录必须引用不可变报告；报告至少包含 Reference Target、测试数据、
实际 P50/P95/P99、错误率、连接池峰值、恢复时间和未满足项。任一关闭验收未
覆盖时状态仍为 open。

## 12. 系统实现验收基准

“实现完成”不是代码合并、单测通过、文档完成或人工 demo。它是对一个精确发布
对象作出的可复核判断：

```text
architecture conformance
+ functional vertical slice
+ security and Tenant isolation
+ reliability and recovery
+ observability and operations
+ performance and capacity
+ evaluation and business quality
+ UI and projection convergence
+ reproducible release and rollback evidence
```

所有 gate 均 fail closed：缺少报告、报告不能绑定精确 build/profile/config、仅使用
mock、或结果无法复现时，都视为未通过。单个 P0 关闭记录证明一个 blocker；本节的
`ImplementationAcceptanceRecord` 聚合全部适用证据，证明一个精确 release 可以
进入目标环境，两者不能互相替代。

### 12.1 验收 Gate

| Gate | 必须证明 | 最小证据 |
|---|---|---|
| G0 Reproducible Build & Migration | 相同 source、lock、制品和配置可重建；schema 可安全升级；失败可回到已知可运行状态 | source commit、lock/SBOM/signature、`RuntimeBuildManifest`、image digest、migration hash、迁移兼容测试和 rollback/roll-forward 演练 |
| G1 Contract & Architecture | ABI、Canonical Contract、state owner、依赖方向和 fail-fast capability 与架构一致，没有旁路执行/授权/持久化路径 | schema/contract golden、version converter、state-owner/invariant tests、module dependency checks、disabled-adapter tests |
| G2 Real Integration | 选定的真实基础设施和 production adapter 共同工作，mock 没有掩盖事务、版本或 provider 行为 | 真实 PostgreSQL/RLS/PostgresSaver、选定 model/provider、Profile 声明的 Knowledge/Tool adapter 和 Runtime Build integration report；mock 仅限 unit test |
| G3 Business E2E & UX | 目标 Business Profile 的真实纵向闭环、业务质量、人工交互和 UI 投影可用 | 从认证提交经过所有已声明 Knowledge/Tool/Action seam，到 inference/checkpoint/result/report/UI/Inspect 的 E2E、golden dataset、业务阈值、human review、typed reducer/reconnect evidence |
| G4 Security & Multi-tenancy | 身份、Tenant、授权、secret、输入、前端缓存与侧信道边界不能被绕过 | cross-Tenant/ID enumeration、RLS/role、injection、credential、timing/disclosure、tenant-switch reset 和 audit evidence |
| G5 Reliability & Recovery | command、checkpoint、lease/fence、projection 和恢复在崩溃、重复、乱序与依赖故障下保持唯一语义 | fault matrix、idempotency、stale-writer rejection、takeover、projector/reconciliation、backup restore 与 PITR report |
| G6 Performance & Resource | Reference Target 下的延迟、吞吐、连接、存储、队列和内存有界，角色隔离有效 | load/soak/30 天等效容量报告，P50/P95/P99、错误率、pool 峰值、queue/memory 上限、资源 quota 与扩缩容证据 |
| G7 Observability & Operations | 权威事实不依赖 telemetry；故障可发现、定位、处置，观测故障不反压在线执行 | RuntimeEvent/audit completeness、OTel isolation、dashboard/alert/runbook drill、role readiness、safe Run Inspect 与 on-call drill |
| G8 Release Governance | 精确行为构建已有有效评测、适用 blocker 已关闭，发布、观察和回退路径受控 | Evaluation Subject/evidence、P0/P1 closure、approval、bounded rollout/soak plan 和 rollback artifact/result |

G0～G2、G4～G8 是所有可投入真实使用的 release 必选。G3 对 Business Profile/
产品 release 必选；纯 Core artifact 可以不声明业务完成，但不能据此称为可投入使用的
MVP。启用 optional Capability Profile 时，增加其 POC、blocker 与 E2E；未启用时也
必须证明 capability 未声明、所有入口 fail fast 且不存在旁路降级。

Gate 本身必须通过，但其中的证据项按声明的 release scope 适用：Core-only artifact
在 G8 使用 `framework-baseline` 与兼容证据；Business Profile 产品 release 再绑定
精确 Evaluation Subject 与业务 Evaluation evidence。不能因为某个证据项不适用而
跳过整个 gate。每个 gate result 至少记录 `gate_id`、`status`、`evidence_refs`、
`owner`、`reviewer` 和 `executed_at`。

适用 P0 必须是 `verified`，`waived` 的 P0 不能通过生产验收。适用 P1 必须
`verified`；仅当它不破坏任一必选 gate，且具有风险接受人、审批人、到期时间、补偿
控制、监控与撤回条件时，才可使用有界 waiver。open、过期、范围不匹配或没有补偿
控制的 waiver 一律失败。

### 12.2 Deployment Role 故障与扩缩容矩阵

按 [ADR-0023](./adr/0023-start-with-a-role-separated-modular-monolith.md) 部署的
角色必须在相同 Runtime Build 和 Reference Target 下至少通过：

| 注入/操作 | 必须观察到的结果 |
|---|---|
| 终止全部 API Role instance | 已 claim 的 Run 继续推进；API 恢复后从 durable state 提供一致结果，不在请求进程补跑 Graph |
| 终止持有 lease 的 Runtime Worker | stale writer 被 fence 拒绝；另一 Worker 在 Core takeover RTO 内恢复，API 与 Projector 保持可用 |
| 终止 Projection/Reconciliation Role | Run 和 command 继续推进；重启后按 source watermark 补齐，UI/Inspect 在 projection recovery 阈值内收敛 |
| 让 Governance/Evaluation pool 饱和或失败 | 在线 Worker 无连接饥饿；API/command/Run SLO 和在线 PostgreSQL 配额仍满足 Reference Target |
| 禁用 OTel Collector/backend | RuntimeEvent/audit 仍完整，Run 结果不变；telemetry 内存有界并产生 drop/saturation 告警 |
| 交换或扩大 role database credential | 非职责表、Tenant 越界和 `BYPASSRLS` 全部拒绝；错误 role/profile 在启动时 fail fast |
| 同一角色执行 `1 → N → 1` 水平伸缩 | command、fence、checkpoint、terminal event 和 projection 无重复语义或状态分叉，仅吞吐/可用性改变 |

阈值只引用本文件的 Reference Target；不得在测试脚本中另设更宽松的隐式数字。若
Deployment Cell 或网络服务抽取改变故障边界，必须重跑本矩阵，并为新增远程失败
模式补充 contract、security、load 与 recovery evidence。

### 12.3 验收判断与不可变记录

对 Business Profile 产品 release，唯一通过条件为：

```text
accepted =
  every_required_gate == passed
  AND every_applicable_P0.evidence_state == verified
  AND every_applicable_P1 satisfies (
        evidence_state == verified
        OR (evidence_state == waived AND waiver_status == active_bounded)
      )
  AND business_profile_E2E_passed_on_reference_target
  AND no_open_expired_or_out_of_scope_waiver
  AND immutable_implementation_acceptance_record_approved
```

`ImplementationAcceptanceRecord` 至少固定：

```text
record_schema_ref
release_ref / release_version
source_commit
runtime_build_manifest_ref / hash
database_migration_ref / hash
target_environment / deployment_cell_ref
deployment_topology_ref / hash
deployment_config_ref / hash
capability_profile_ref / hash
business_profile_ref / hash | null for Core Release
contract / ABI / state_schema versions
reference_target_version
evaluation_subject_hash / evidence_set refs
gate_results / applicable_blocker_closure_refs
contract / integration / E2E / security evidence refs
fault / recovery / load / soak / DR / UI evidence refs
known_limitations
waivers(owner / approver / expiry / compensating_controls / revoke_condition)
rollback_artifact / runbook_result
reviewers / approved_at
```

记录与所有报告必须内容寻址、只读可审计，并能由 CI/release pipeline 验证引用、hash、
签名、适用 Profile 与结果状态。截图、聊天确认、手工勾选或现场演示可以辅助评审，
不能成为通过依据。任何 source、build、migration、部署拓扑、行为相关配置、Profile
或 Evaluation Subject 变化，都产生新的验收记录；不能沿用旧 release 的批准结论。

## 13. 交付模型

路线采用 `MVP Baseline + independent Release Tracks`，不是 Phase 2 到 Phase 7
必须依次实施的瀑布。MVP Baseline 是当前交付承诺；每个 Release Track 只有在
出现真实启用条件后才进入实施，并独立关闭适用 blocker、POC 和回滚 gate。
未选择的 Track 必须保持 capability unavailable，不能交付半实现。

### MVP Baseline A：Core 契约

- 固定 MVP Foundation：身份租户、可靠异步执行、契约版本、观测审计、可靠
  交互、资源与上下文边界、评测证据和最小生产运维；任一项都不是 optional
  Profile。
- Agent = Skill Composition + Policy；Plan/Execution/Observation public
  contracts。
- Skill Registry、content-addressed SkillRuntimeManifest、versioned
  RuntimeBuildManifest、SkillExecutionSpec ABI、Capability Profile。
- Canonical Execution Contracts（含 Continuation/Interaction/UI projection
  types）、Node Adapter 与 versioned converters。
- Versioned Telemetry Policy：Platform 硬安全包络、Deployment/Tenant 可配置
  字段、policy ref/hash/audit 和越界配置 fail closed；Diagnostic Capture release
  不进入 MVP。
- 四种 versioned permission preset 与受限 Adapter Interceptor SPI；无 bypass、
  无动态注册。
- Run Command、Execution Driver interface、lease/fence、trusted Run Signal
  和 public run command/state machine contract。
- `TypedInferencePort` + PydanticAI adapter（无 executable business
  Tool/Memory/durability）、KnowledgePort、Knowledge Snapshot/Citation/outcome
  contract。
- `NoMemoryAdapter`、`DisabledDurableActionAdapter`、
  `DisabledExecutionWorkspaceAdapter`、`DisabledRunDelegationCoordinator`。
- 完成 contract/golden tests；P0 保持 open，直到 MVP Baseline B 真实环境证据。

### MVP Baseline B：一个已选择的只读 Business Profile

- 实现前显式冻结内容寻址的 `business_profile_ref/hash`；禁止空值、`latest` 或
  平台内置资产默认值。Profile 必须拥有 typed input/output、root Skill/Graph、
  Knowledge/Tool closure、Effect Class、交互/UI、业务质量阈值、golden dataset、
  human review 和 Profile-specific POC。
- 最小 Baseline 所选 Profile 只依赖 Execution Core，只允许 `pure/read`，且只有一个 root
  Agent Binding；若真实场景需要 optional capability，相应 Release Track、blocker、
  POC 和 gate 自动成为该 Product MVP 的前置条件，不能静默降级。
- 使用真实认证与 Tenant 上下文、选定 model/provider、production Knowledge adapter
  和不可变 Knowledge Snapshot，跑通提交、Graph、checkpoint、typed result/report、
  Interaction/UI 与 Run Inspect。若 Profile 声明 Live Business State，再交付其自有的
  production read Tool；没有该需求时不为通过验收虚构 Tool。
- [Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md) 只是
  一个可选实例。选择它时执行 POC-M 及资产 ADR；选择其他领域时提供自己的 Profile
  文档、golden dataset、disclosure/完整性规则和同等级 POC，不能复用 POC-M 冒充 G3。
- LangGraph + fenced PostgresSaver + PostgresExecutionDriver。
- 按 ADR-0023 分别部署 API、Runtime Worker 和 Projection/Reconciliation Role；
  Governance/Evaluation 使用隔离的离线 pool，并通过 12.2 的故障与扩缩容矩阵。
- RuntimeEvent/SSE、typed UI event + Interaction Projection、Run Inspect；按
  `docs/12` 交付 OTel correlation、核心 metrics、脱敏日志、四个可行动 dashboard、
  alert、health/readiness、PITR 与恢复演练，N-29 不得延后。
- MVP Frontend 交付 Execution Launch、Run Interaction、History/Inspect、
  `RunInteractionModel` typed reducer、pending interaction、reconnect UX，以及由所选
  Business Profile 拥有的 typed renderer；前端不能内置资产作为通用页面语义。
- 显式 `compress_context` + versioned ContinuationSummary；N-10 必须以 30 天等效
  容量证据关闭，不能只用短流程功能测试代替。
- `durable_action`、`execution.workspace`、`run.delegation`、`memory.long_term`、
  `experience.projection` 和 `capability.evolution` 在最小 Baseline 中关闭；外部写
  operation 返回 `CapabilityUnavailable`，普通 Tool 不能绕过。
- Sub-agent、Supervisor、Swarm、GoalLoop、RoleTemplate、Join、Child Run、replay、
  fork 和任意 checkpoint 继续执行均不进入最小 Baseline；POC-B/J、N-21/N-26 不
  阻断它，相关 ADR 只保留后续规范。
- 完成 POC-C/D、POC-E Knowledge Baseline、POC-F/G/H/I、所选 Profile 的 POC，
  关闭适用的 N-03、N-05、N-08、N-15、N-16、N-18 ～ N-20、N-22、N-23、N-25。
- 通过 G0～G8，按 12.1 满足全部适用 P0/P1，并发布经审批、内容寻址的
  `ImplementationAcceptanceRecord`；否则只能称为 Core artifact、POC 或受限测试
  环境，不能称为已实现的 Product MVP。

### Release Track：Time Travel

- 实现 nondeterministic seam recording、source-anchored replay、fork dry-run/
  commit、新 Run lineage、历史 Runtime Build 路由与 retention/pinning。
- 前端增加 checkpoint 可用性、replay/fork 和 lineage 入口。
- 关闭 N-26，完成 POC-B Core 子集；启用 Durable Action 后再完成对应扩展。
- 未启用时只提供 Run Inspect 与重新提交 live Run；关闭 Track 不删除仍在
  inspect/audit retention 内的历史事实。

### Release Track：Knowledge Expansion + Long-Term Memory

- 多 source/adapter、connector lifecycle、持续 ingestion、index rebuild、source
  health、增量 Knowledge Snapshot 发布和治理 UI；没有真实多源需求时不实施。
- PostgresMemoryAdapter、deterministic recall inclusion、candidate outbox、
  promotion/replay refs；Knowledge 与 Memory namespace/authority 保持分离。
- 未启用时仍保留 MVP 的单 Snapshot Knowledge Baseline，但
  `memory.long_term` 明确 unavailable。
- 关闭 N-14、N-17，完成 POC-E Long-Term Memory 扩展；Knowledge Expansion
  需为新增 adapter/source 补充同等级 ACL、Citation、outcome 和故障验收。

### Release Track：optional Durable Action

- DBOS adapter、action handoff、approval、reconciliation。
- 关闭 N-01、N-02、N-04、N-13，完成 POC-A 与 POC-B Durable Action 扩展。

### Release Track：Skill Composition 与 Multi-Agent

- fixed-version composition、per-invocation subgraph、Supervisor + `Send` +
  keyed reducer、bounded GoalLoop。
- RoleTemplate registry/compiler；模板只引用 closure 内 Skill，不授予权限。
- stable branch/delegation key；depth/concurrency/descendant/iteration/token/
  cost/deadline budget。
- Multi-Agent semantic event schema、trace link 和 topology projection。
- 完成 POC-J Core 子集，关闭 N-07、N-21。
- 按需启用 `run.delegation`，实现 atomic Child Run acceptance、
  RunWaitRef/RunSignal、Parent/Child HITL projection、Join/cancel/reconciliation；
  完成 POC-J 扩展后关闭 N-27。未关闭时不声明该 capability。
- composition 与 orchestration evaluation。

### Release Track：Schedule / Event Trigger

- 仅在产品需要无人值守 schedule 或外部 event 入口时实现 Trigger Adapter。
- 完成签名、tenant/workload mapping、stable occurrence/submission ID、misfire、
  overlap gate、bounded catch-up 和故障恢复。
- 完成 POC-K，关闭 P1 N-28；未启用时没有隐式 cron、线程或 callback 入口。

### Release Track：Execution Workspace

- 仅在 Skill 必须执行隔离文件或进程工作负载时启用
  `execution.workspace`。
- 完成 run-scoped sandbox、fenced acquire/release、reattach、ArtifactRef 交接、
  egress/path policy 和 orphan reconciliation。
- 完成 POC-L，关闭 N-30；未启用时不得使用宿主目录作为降级。

### Release Track：Experience / Evolution / Governed Publication

#### Gate 1：Experience Projection

- Manifest/ArtifactRef/collection policy。
- 完成 POC-X，关闭 E-01、E-02、E-03、E-08、E-10。
- 启用 Memory Curator 时完成 POC-Y，关闭 E-04 和适用的 E-07；未启用时
  不声明 Memory Curation release。

#### Gate 2：Evolution Shadow Mode

- immutable Evaluation Suite/Run、Candidate 和 differential report，禁止
  publish。
- 完成 POC-Z 前四步与 holdout/hard gate 验证，关闭 E-05、E-06、E-07、
  E-09、E-11、E-12。

#### Gate 3：Governed Publication 与 Production

- approval、immutable publish、staged rollout、rollback。
- 完成 POC-Z。
- 按需关闭 DBOS HA 和所有适用 P1。

### Release Track：Enterprise Productization

- Tenant、Membership、Authorization Role、Operation Catalog 和 quota 管理界面。
- 企业身份接入、审计检索与导出、retention/legal hold、密钥轮换和安全运营。
- 只有真实支持需求成立时才增加 Diagnostic Capture release：审批、限时限域、
  字段 allowlist、独立治理存储、自动删除和全链路审计；它不是 Skill runtime
  Capability，未单列 blocker/POC 并完成安全验收前不得启用。
- Skill/Evaluation/Publication 控制台、版本差异、rollout/rollback 和 evidence
  可视化。
- SLO dashboard、容量与成本治理、Deployment Cell placement、备份恢复演练和
  support tooling。
- 高级前端信息架构、通知和运维视图；不得复制 Execution/Observation 权威状态。
- 每项只在对应组织、合规或运营需求存在时进入实施，并沿用同一 contract 和
  Profile gate；不能以“完整产品”为由一次性预建所有管理面。

## 14. 从设计到实现的最短路径

实施从领域无关的 Core Walking Skeleton 开始，不从 Asset Risk 或其他参考 Profile
反推平台结构。本节只拥有实施依赖与退出顺序；协议仍由各专题拥有，阈值、blocker
状态、POC 和证据格式仍由本文件前述章节拥有。

### 14.1 首条可执行链

第一条链使用 versioned、non-production conformance fixture，不产生业务发布结论：

```text
authenticated request + Active Tenant Context
  → API Role accepts submit
  → PostgreSQL Run Command
  → Runtime Worker claims lease/fence
  → deterministic conformance Graph
  → checkpoint + committed RuntimeEvent outbox
  → Projection/Reconciliation Role
  → SSE + generic Run Inspect
```

fixture 只验证 contract、状态所有权和基础设施。需要验证 G2 时，它必须调用真实
PostgreSQL、选定 model/provider 和适用 production adapter，但仍不得进入 production
Skill channel、提供领域质量分数或关闭 G3。测试数据、fixture Skill 和 renderer 也
不能内置资产字段或成为未来 Business Profile 的隐式模板。

### 14.2 Work Package 与退出条件

Work Package 的编号、名称、依赖、状态和结果摘要只在
[`ROADMAP.md`](../ROADMAP.md) 注册。本节只保留 Gate/证据映射，不复制任务状态或详细
任务书。

| Work Package | Gate/证据 |
|---|---|
| WS-0 | G0 的 build/migration/SBOM/signature/rollback 基础证据；首次 `framework-baseline` |
| WS-1 | POC-F/G；G1；N-15/N-16/N-18/N-19 的 contract 子集 |
| WS-2 | POC-D 对应部分；G4；N-08/N-20/N-22 的入口子集 |
| WS-3 | POC-I、POC-C Core 子集；G2/G5；N-03/N-05/N-25 |
| WS-4 | POC-D UI/event 子集；G7；N-07/N-11/N-29 |
| WS-5 | POC-H、适用 POC-C/D/F/G/I；Core `ImplementationAcceptanceRecord` |
| WS-6 | POC-E Knowledge Baseline、Profile-specific POC、G3；若选择 Asset Risk 才执行 POC-M |
| WS-7 | Product MVP `ImplementationAcceptanceRecord` |

WS-0～WS-4 只是可演示的工程增量，不能称为 release。WS-5 的出口是通用 Core
Release，不是产品完成；WS-6 开始前必须冻结目标 Business Profile。若所选 Profile
需要 Durable Action、Run Delegation、Execution Workspace、Multi-Agent 或其他
optional capability，先插入并关闭对应 Release Track，再继续 WS-6；不能把它从
Profile closure 删除、用 mock 成功或包装成普通 Tool。

### 14.3 依赖与并行边界

精确依赖图只维护在 [`ROADMAP.md`](../ROADMAP.md)。以下内容约束允许的并行工作与阶段边界。

- Business Profile discovery 可以与 WS-2～WS-5 并行，但不能修改 Core contract 来
  迎合单一领域；共性只有在至少两个真实 Profile 证明后才考虑上提。
- Frontend 在 UI Projection contract 稳定后实现 generic reducer/Run Inspect；领域
  renderer 等到 WS-6，避免在 Core 页面硬编码资产或其他业务状态。
- WS-3 之前不并行开发可选 Runtime；WS-5 之前不拆网络服务。真实负载证明进程角色
  或 Deployment Cell 无法解决后，才按 ADR-0023 评审服务抽取。
- 每个 Work Package 只向 CI 写入内容寻址 evidence artifact；只有 WS-5/WS-7 的
  `ImplementationAcceptanceRecord` 能形成发布结论。

### 14.4 实施纪律

1. 先把退出不变量写成 contract/integration/fault/security test，再实现对应路径；
   不以覆盖率百分比替代明确不变量。
2. unit test 可以 mock port；integration、recovery、RLS、load 和 G2/G3 必须使用
   本节声明的真实组件与精确 Runtime Build。
3. 每次只打通一个端到端增量，不同时建设 Memory、Action、Multi-Agent、Workspace、
   Trigger、Time Travel 或 Evolution 的空壳。
4. 任何实现便利若新增第二状态真相、授权旁路、无界 queue/context 或领域 Core
   字段，立即停止并回到所属 ADR/contract 修正，不用 middleware 或补偿任务掩盖。
5. Work Package 完成只更新 evidence；不得因为代码合并、页面可见或 demo 成功把
   P0/Gate 标为 `verified`。
