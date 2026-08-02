# Observability and Operations

> 架构集：GROVE v1.0
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> 执行语义：[Execution Core](./10_Execution_Core.md)
> 事件契约：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)
> 验收门槛：[P0 Blockers and Acceptance](./90_P0_Blockers_and_Acceptance.md)

## 1. 定位与权威范围

观测性与最小生产运维属于 MVP Foundation，从首个端到端 Run 开始实现，不是
上线前补装的可选 Profile。本专题唯一负责：

- 权威状态、持久化观测事实、产品投影和诊断 telemetry 的边界。
- correlation、trace、metric、log、audit 的公共约束。
- MVP 必需指标、dashboard、alert、health 和恢复演练。
- telemetry 的脱敏、基数控制、故障隔离和验收。
- 后续 Capability Profile 如何扩展而不复制观测管线。

本专题不拥有 Agent Run state、checkpoint、authorization、Interaction、
Experience 或任何业务事实；对应 module 仍是唯一权威所有者。指标阈值和 blocker
关闭状态只由 `docs/90` 维护，本专题不复制数值真相。

## 2. MVP Foundation

首个 MVP 必须同时交付以下基础能力，不能用 interface stub 或空 dashboard 代替：

| 基础能力 | MVP 最小闭环 |
|---|---|
| 身份与租户 | Active Tenant Context、Run Authority、RLS/组合约束、强类型授权和审计 |
| 可靠异步执行 | durable Run Command、claim、lease/fence、checkpoint、接管、幂等、cancel、reconciliation |
| 契约与版本 | typed contract、immutable Skill/Spec/Manifest、schema version、capability fail fast |
| 观测与审计 | RuntimeEvent/audit outbox、OTel correlation、核心 metrics、脱敏日志、dashboard/alert |
| 可靠交互 | Interaction/UI projection、cursor、SSE backfill、command confirmation、Tenant context reset |
| 资源与上下文 | deadline、step/token/cost budget、bounded retry、cancel、ContinuationSummary |
| 评测与发布证据 | golden dataset、contract/security/recovery regression、固定 Runtime Build |
| 最小生产运维 | health/readiness、migration、PITR/恢复演练、pool/capacity/SLO 告警 |

缺少任一项时可以运行 POC，但不能称为可投入真实使用的 MVP Baseline。

## 3. 四类信息必须分开

```text
权威 module state
  ├─ committed observation/audit outbox
  │    └─ RuntimeEvent / Audit Fact               [持久化、不可采样]
  │          └─ Interaction / UI / Inspect view   [可重建 read model]
  └─ OTel SDK → OTLP → Collector
       └─ trace / metric / diagnostic log         [有界、允许采样或丢弃]
```

| 类型 | 回答的问题 | 可否丢失 | 能否驱动执行 |
|---|---|---:|---:|
| 权威状态 | Run、checkpoint、authorization 当前是什么 | 否 | 是，由其 owner 驱动 |
| RuntimeEvent / Audit Fact | 哪个已提交事实需要审计或投影 | 否 | 否 |
| Product Projection | 用户现在应看到什么 | 可重建 | 否 |
| Diagnostic Telemetry | 为什么慢、错或资源异常 | 可有界丢失 | 否 |

不变量：

1. trace、log、metric、SSE 或 projection 不能恢复 Graph、批准操作或产生 Run
   Signal。
2. 需要审计的事实必须从提交事务的 outbox 产生；不能依赖随后可能丢失的日志。
3. 必需 outbox 写入失败应使对应状态提交失败；OTel exporter 失败不得使 Run
   失败。
4. RuntimeEvent 是持久化观测事实，但不是统一 Event Store，也不能重建
   checkpoint。
5. Experience 只能消费经治理的投影和 ArtifactRef，不能把原始 telemetry
   直接当训练数据。

## 4. Correlation 与信任边界

### 4.1 标识语义

| 标识 | 用途 | 约束 |
|---|---|---|
| `correlation_id` | 连接一次用户意图、API、command 和 Run 因果 | 由可信入口建立，不承载权限 |
| `run_id` | 定位一次 Agent Run | 高基数，只用于 trace/log/查询，不作 metric label |
| `command_id` | 命令幂等与投递诊断 | 不作为授权凭据 |
| `node_execution_key` | 节点尝试与 seam 调用关联 | 由 Kernel 产生 |
| `trace_id/span_id` | 诊断调用链 | 允许采样，不证明业务事实 |
| `tenant_id` | 内部隔离和审计 | 不进入公共 URL、Baggage 或高基数 metric label |

trace context 只传播因果。Tenant、Principal、Run Authority、authorization、
fence、Run Signal 和 delegation 必须来自 Canonical Contract 或可信本地上下文，
不能从远端 trace header/Baggage 恢复。

Baggage 默认关闭；确需开启时只允许低敏、低基数 allowlist。进入第三方模型、
Tool 或 provider 前清除非标准传播字段。

### 4.2 长生命周期 Run

不能让一个 span 跨越数小时等待。每次 API command、Worker invocation 和恢复
attempt 使用独立 trace，通过 `correlation_id`、Run reference 和 span link 连接；
interrupt/wait 的持续时间由持久状态与 metric 计算，而不是保持开放 span。

## 5. 持久化观测与审计

MVP 至少必须形成以下已提交事实的 versioned event/audit 记录：

- Run 创建以及每次 lifecycle transition。
- command accepted、applied、rejected、conflict 和 dead-letter。
- Worker claim、lease 失效与 takeover 的结果摘要。
- Interrupt 创建、响应、stale/expired 和恢复结果。
- protected operation 的 authorization decision 与 capability unavailable。
- Run Data View checkpoint 接受或领域读取失败：接受事件可记录 logical call、
  `tool_request_id`、`view_schema_ref`、`observed_at`、安全 source ref、record count
  （若 Profile 允许公开）和 result hash；失败事件只记录统一 failure class，不记录
  业务正文、resource ref 或匹配数。
- budget/deadline exhaustion、cancel request 和 terminal failure classification。
- Interaction/UI projector 的 source watermark、gap 和 reconciliation health。
- 跨 Tenant 引用、凭据/策略篡改等安全拒绝。

每个事实必须携带适用的 Tenant、Run、correlation、causation、schema version、
source ID 和安全 payload reference。大对象使用 ArtifactRef；事件中不复制完整
Prompt、业务 input/output、provider response、credential 或 chain-of-thought。

RuntimeEvent 至少一次采集并以 stable source identity 去重；同 Run 的
commit-ordered sequence 用于 SSE/backfill。时间戳只表示发生或记录时间，不承担
顺序。安全审计可以采用单独 retention/projection，但必须复用同一 committed
outbox，不能形成第二次非原子写入。

## 6. Diagnostic Telemetry

生产基线固定为 OpenTelemetry API/SDK + OTLP。Collector 负责 vendor-neutral
接收、redaction、batch 和 export；具体 APM、metrics 和 log backend 可替换。

### 6.1 MVP span

```text
grove.api.request
grove.command.accept
grove.worker.claim
grove.run.invoke
grove.graph.node
grove.checkpoint
grove.inference
grove.knowledge.retrieve
grove.tool.read
grove.projector.apply
grove.sse.backfill
```

Profile 可以增加 `grove.action`、`grove.workspace`、`grove.delegation`、
`grove.goal.iteration` 等 span，但不能改变公共属性、采样和信任边界。

### 6.2 Exporter 故障隔离

1. SDK exporter 使用有界 batch、queue、timeout 和 retry。
2. Collector 至少启用 memory limit、batch、attribute redaction 和有界 queued
   retry；distribution/version/config hash 进入部署证据。
3. Collector/backend 不可用不能阻塞 checkpoint、command commit、Worker
   heartbeat 或改变 Run 结果。
4. SDK/Collector drop、retry 和 saturation 必须通过本地低基数 metric/alert
   暴露；不能为了“零丢失 telemetry”建立无界内存。

## 7. Metrics 与基数

### 7.1 MVP 指标族

| 范围 | 必需指标 |
|---|---|
| Command/Worker | queue depth、claim latency、dead-letter、lease expiry、takeover latency、stale writer reject |
| Run | active/waiting/terminal、end-to-end latency、failure class、cancel、budget/deadline exhaustion |
| Graph/Checkpoint | node latency/failure/retry、checkpoint write/load latency/failure |
| Inference/Knowledge/Tool | request latency、provider failure、schema retry、token、cost、retrieve latency/result class、read attempt/accepted count、transaction latency、TooBroad limit kind |
| Event/Projection | outbox backlog、RuntimeEvent→SSE lag、projector lag/watermark、gap/reconciliation、reconnect |
| Authorization/Security | allow/deny/error、reauth、cross-Tenant/tamper rejection；只用低基数分类 |
| PostgreSQL | pool usage/wait、transaction latency、lock timeout、replication/PITR health |
| Telemetry | exporter queue、drop、retry、Collector refusal/saturation |

### 7.2 Label allowlist

metric label 只允许有界维度，例如 environment、service、runtime build、Skill
Version、operation、run mode、status 和 failure class。禁止把以下值作为
Prometheus label：

- `tenant_id`、`user_id`、`run_id`、`command_id`、`trace_id`。
- Prompt、模型输出、URL、异常正文或任意业务字符串。
- delegation/branch/artifact/checkpoint 等实例 ID。

`ResourceSelectionUnavailable` 的 metric、RuntimeEvent 和普通日志不得加入
resource ref、匹配数或 not-found/denied 原因维度；只使用统一低基数 failure
code。授权审计同样不得成为可由普通 Run Inspect 枚举存在性的旁路。

高基数实例标识只进入受控 trace attribute、结构化诊断日志或 exemplar。

## 8. 日志、策略与敏感数据

### 8.1 不可配置的安全底线

结构化日志只用于进程诊断，不是 audit、recovery 或产品 timeline 的权威来源。
MVP 日志至少带 service、build、environment、correlation、run、component、
operation、status 和 safe failure class。

常规 RuntimeEvent、OTel 和日志默认禁止写入：

- credential、session/token、Run Authority 或完整 authorization reference。
- Prompt、chain-of-thought、业务 input/output 和 provider raw response。
- 未脱敏 Tenant/User identity、Knowledge/Memory 正文和 Artifact signed URL。
- 任意 checkpoint State、文件内容或数据库 statement parameter。

异常必须先归一为稳定 failure class，再记录脱敏 message。业务内容排障通过独立、
重新授权且有 retention 的 Run Inspect/Artifact 路径完成，不通过临时提高日志级别
绕过治理。

以下底线不能被 Tenant、环境变量、管理员或调试模式放宽：credential、token、
secret、signed URL 和 chain-of-thought 永不采集；telemetry 永不建立 Tenant、
Principal、authorization 或 execution fence；任何 capture 永不跨 Tenant。

### 8.2 Telemetry Policy

MVP 提供 strong-typed、versioned `TelemetryPolicy`，但配置只能在平台硬边界内
选择：

| 配置所有者 | 可以配置 | 不能配置 |
|---|---|---|
| Platform/Deployment | exporter、Collector、queue/memory、默认 sampling/retention、全局 redaction floor | 关闭必需 audit、允许敏感正文、无界 queue |
| Tenant admin | 平台范围内的 sampling、retention、safe attribute 子集、更严格 redaction、alert threshold | 自定义 exporter credential、增加平台禁止字段、使用高基数 metric label |

策略必须有 ref/version/hash、issuer、scope、effective time 和审计记录。每个新
telemetry signal 标记实际使用的 policy version；策略收紧或撤销对后续 signal
立即生效，不冻结在 Agent Run 内，也不追溯生成历史内容。

Telemetry Policy 不进入 `SkillExecutionSpec` 或 `evaluation_subject_hash`，因为
它不能改变 Graph、模型输入、授权和业务结果；但 Runtime Build 固定 policy
resolver、redactor、OTel SDK 和 semantic convention 的代码版本。无效、未知或
试图突破平台底线的策略必须 fail closed，不能退回 verbose default。

### 8.3 Diagnostic Capture Session

完整产品只有在出现无法通过 metadata、Run Inspect 和受治理 Artifact 定位的真实
支持需求后，才可以交付 Diagnostic Capture release。它是受运维治理的产品能力，
不是 Skill 可声明的 runtime Capability，也不能进入 `SkillExecutionSpec`。一次
Session 必须固定：

```text
tenant + exact run/component scope
purpose/ticket + requester + approver
field-path allowlist + redaction profile
start/expiry + retention deadline
record/byte budget + governed sink
```

规则：

1. 获批的 Prompt、typed input/output 或 Knowledge 片段只能以字段投影写入独立、
   加密且 Tenant-scoped 的治理存储；普通 OTel/log backend 仍只收到 reference。
2. Session 从 accepted 后的边界开始采集，不补抓历史数据；到期、预算耗尽、Run
   terminal 或人工撤销后立即停止。
3. 每次创建、读取、导出和删除都重新授权并审计；模型、Skill、Middleware、普通
   end user 和远端 header 都不能启动 Session。
4. credential、token、secret、signed URL、chain-of-thought 和未通过 redactor 的
   provider raw response 即使获批也不能采集。
5. capture 仍受有界 queue、latency 和 memory budget；sink 故障不能阻塞 Run。
6. MVP 不实现该能力；未启用时必须返回 `DiagnosticCaptureUnavailable`，不能以
   提高 log level 代替。

## 9. Product Projection 与 Run Inspect

Interaction/UI/Inspect projector 消费 RuntimeEvent 和权威 module 的 safe
projection，必须提供 cursor、source watermark、`complete/partial/stale` 和
reconciliation 状态。它可以迟到修正和重建，但不能：

- 推断或改写 Run terminal、authorization、approval、Join 或 checkpoint。
- 把缺失、未知 schema 或 backlog 解释为“没有数据”。
- 将 raw RuntimeEvent、trace 或日志直接渲染为业务组件。
- 因 projector 停机而阻塞在线 Agent Run。

Run Inspect 默认展示安全生命周期、阶段、失败分类、checkpoint 摘要、citation、
Run Data View provenance、artifact 和授权诊断引用；Profile projector 可按
`view_schema_ref` 增加已授权的领域摘要，但不展示 chain-of-thought 或未治理
payload。

## 10. Health、Dashboard 与 Alert

### 10.1 Health

- API readiness 验证 database、schema migration、Runtime Build 和必要 adapter
  compatibility；API Role 不得持有 command claim/Graph execution 能力，OTel
  backend 故障不使 API unready。
- Runtime Worker readiness 验证 database、claim/heartbeat、lease/fence、build
  route 和 checkpoint compatibility；它不依赖公共 HTTP 或 Projector 才能推进
  已接受的 Run。
- Projection/Reconciliation health 独立暴露 outbox/source watermark、lag、
  unknown schema、dead-letter、gap repair 和 orphan cleanup；其 unready 不影响
  API/Runtime Worker readiness，也不能阻塞 Execution。
- Governance/Evaluation health、queue、pool 和 quota 与在线 readiness 分离；离线
  job 饱和、暂停或失败不能把 API/Runtime Worker 标为 unready。
- 每个 Deployment Role 必须校验精确 role declaration、最小 database credential、
  独立 pool/quota 与 Runtime Build hash；角色能力混装或权限超集时 fail fast。
- reconciliation、backup/PITR 和恢复演练是独立 health evidence，不能用普通
  HTTP liveness 代替。

### 10.2 MVP dashboard

只建立四个可行动视图：

1. Execution：command、Worker、Run、checkpoint、takeover。
2. Dependency：model、Knowledge、PostgreSQL、SSE/projector。
3. Security：authorization、cross-Tenant/tamper rejection、audit pipeline。
4. Telemetry health：exporter/Collector queue、drop、retry 和 backend availability。

每个 dashboard 必须对应 owner、SLO、告警阈值和 runbook；没有行动含义的图表不
进入 MVP。

### 10.3 Alert 原则

- page：数据隔离风险、已提交 command 无法推进、checkpoint 持续失败、dead-letter
  增长、恢复超过 RTO、审计 outbox 无法提交。
- ticket：容量趋势、token/cost 偏移、projection lag、SSE reconnect 增长、
  telemetry 丢弃和评测漂移。
- 单次业务失败进入 Run Inspect，不默认 page；只有错误率或 SLO burn 达阈值才
  告警。

具体数值只引用 `docs/90` 的 Reference Target 和 P0/P1 gate。

## 11. Profile 扩展规则

每个 Release Track 必须在启用时声明新增的 durable fact、span、metric、dashboard
和 alert，并重跑适用的 N-07、N-08、N-11、N-29：

- Time Travel：recording completeness、mismatch、historical build availability。
- Memory：recall inclusion、candidate/outbox、consent/retention rejection。
- Durable Action：handoff、unknown/manual review、reconciliation、provider fact。
- Multi-Agent：fan-out、GoalLoop、Child acceptance/signal/Join/topology。
- Workspace：acquire/reattach/release、resource/egress reject、orphan cleanup。
- Trigger：occurrence、misfire、overlap、catch-up 和 submission conflict。
- Evolution：projection watermark、dataset gate、candidate/evaluation/publication。
- Diagnostic Capture：session admission、field rejection、expiry、budget、sink
  failure、read/export/delete audit。

未启用的 Profile 不应产生伪成功 metric 或空投影；Observation 明确返回
`CapabilityUnavailable`。

## 12. MVP 验收

至少证明：

1. 每个 command/run/interrupt terminal 事实可以从 durable event/audit 链定位，
   不依赖采样 trace。
2. Collector/backend 连续不可用达到 `docs/90` 窗口时，Run 结果、checkpoint、
   command latency 和进程内存仍满足门槛，且 drop/saturation 可告警。
3. 在 Prompt、input、output、exception 和远端 trace header 注入 secret/PII/
   forged Tenant/Principal；可信上下文不变，telemetry export 不包含注入内容。
4. kill/restart outbox publisher、projector 和 SSE client 后，durable fact 不重复，
   UI 通过 cursor/watermark 最终收敛。
5. metric cardinality 静态检查和负载测试证明禁止维度未进入 label。
6. Worker crash、lease expiry、stale writer、checkpoint failure、pool exhaustion 和
   slow dependency 都能在目标时间内触发可行动信号。
7. Run Inspect、trace、artifact、audit 和 metrics query 都重新执行 Tenant/
   Principal 授权；枚举跨 Tenant ID 全部拒绝并审计。
8. dashboard、alert 和 runbook 能让值班者从告警定位到 safe Run Inspect，且不需
   查询生产数据库或开启敏感 debug log。
9. Telemetry Policy 的未知版本、越界 attribute、高基数 label 和放宽 redaction
   请求全部 fail closed；策略切换后的 signal 带正确版本且不回写历史数据。
10. 按 `docs/90` 的 Deployment Role 故障矩阵分别终止 API、Runtime Worker、
    Projection/Reconciliation 和离线 Governance/Evaluation；在线语义、故障隔离、
    恢复时间、连接池配额与水平扩容结果均满足同一 Reference Target。

## 13. 不进入 MVP

- 自建通用日志搜索、APM、SIEM、数据湖或业务 BI 平台。
- 高级 trace waterfall、Multi-Agent topology 分析和跨 Run 运营大屏。
- 以 telemetry 训练、自动修改 Skill 或产生 Experience。
- 按 Tenant/User/Run 建 metric label 或无限保留 trace/log。
- 为追求诊断完整性阻塞在线执行或保存完整业务内容。
- Diagnostic Capture release、审批会话和敏感字段治理存储。

## 14. 参考

- [OpenTelemetry Signals](https://opentelemetry.io/docs/concepts/signals/)
- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
- [OpenTelemetry Baggage security](https://opentelemetry.io/docs/concepts/signals/baggage/)
- [OpenTelemetry Sampling](https://opentelemetry.io/docs/concepts/sampling/)
