# WS-4 Observation Slice 任务书

任务状态、依赖和交付进度以 [`ROADMAP.md`](../../ROADMAP.md#work-packages) 为准。

## 目标结果

在现有 WS-2 命令入口和 WS-3 durable execution 之上建立一个最小、可重建、租户隔离的
观测闭环：权威事务原子产生 RuntimeEvent/Audit Fact，Projection/Reconciliation Role
构建 Interaction/UI/Inspect read model，API 通过有界 cursor/backfill 和 SSE 暴露安全
视图；OTel、Collector、projector 或 SSE 故障不得改变或阻塞 Run。

## 背景与当前问题

WS-0～WS-3 已建立构建、契约、租户命令和 durable execution，但当前缺少一条从已提交
运行事实到安全 Run Inspect、UI projection 和运维信号的完整链路。若直接依赖日志、
trace、内存事件或 raw RuntimeEvent 渲染，会产生第二状态真相、断线丢失、跨租户泄露
和 telemetry 反压执行等问题。

## 范围

### In Scope

- 为 WS-2/WS-3 已存在的 command、run、claim/lease、checkpoint、失败和 terminal
  transition 产生 versioned RuntimeEvent/Audit Fact；outbox 与权威状态在同一
  PostgreSQL 事务提交。
- 实现 tenant-scoped stable source identity、每个 run 的 commit-ordered `run_seq`、
  精确 payload schema/version、去重和未知 schema dead-letter。
- 由独立 `projection_reconciliation` 角色消费 outbox，维护 source watermark，并
  构建可重建的 Runtime/Interaction/UI/Inspect read model。
- 实现授权后的 generic Run Inspect、event/interactions 查询，以及使用
  `projection_seq` 的 UI snapshot、SSE backfill、实时 tail、gap repair 和
  reconciliation。
- 冻结 headless UI projection/reducer contract，并用确定性 reducer 与 golden fixture
  证明 duplicate、乱序、gap、unknown schema、迟到事件和 tenant switch 语义；不创建
  Vue 页面或生产版 `RunInteractionModel`。
- 实现 OTel correlation、最小 span/metric/log、低基数 label allowlist、脱敏和有界
  exporter/Collector 队列。
- 为 API、runtime worker、projection/reconciliation 和 telemetry 分别提供符合职责的
  health/readiness；投影或 Collector 不健康不得使 API/Runtime Worker 失去推进能力。
- 交付 WS-4 范围内的 contract、真实 PostgreSQL integration、故障注入、安全、资源
  边界和运维演练证据。

### Out of Scope

- 完整 Vue 页面、生产版 `RunInteractionModel`、产品 Run History/Run Inspect UX；这些
  在 WS-6 第一阶段基于 WS-4 已稳定契约实现。
- 使用 RuntimeEvent、projection、trace、metric 或日志恢复 Graph、批准操作、产生
  Run Signal 或改写权威状态。
- Time Travel、replay/fork、Experience/Evolution、Memory、Durable Action、Execution
  Workspace、Multi-Agent runtime 及其领域投影。
- 领域 renderer、业务字段、raw Prompt/input/output/provider response、credential、
  chain-of-thought 或 signed URL 的观测与展示。
- 新 broker、独立微服务、第二套 event bus、第二份授权/状态引擎或为了未来能力预建的
  空协议。
- 将 WS-4、G7 局部证据或可见 UI 描述为 Core/Product release。

## 依赖与前置条件

- WS-3 的 command/run/checkpoint durable owner 和 worker loop 必须先形成稳定可消费事实；
  WS-4 不修补或复制其执行状态机。
- Canonical RuntimeEvent、InteractionItem、UIProjectionEvent、failure、tenant 和
  authorization 契约保持唯一版本权威。
- PostgreSQL 继续作为 outbox、cursor、watermark 和 read model 的持久化边界；API、
  runtime 与 projection 角色使用独立最小权限和连接池。
- 依赖 N-07、N-11、N-29、POC-D UI/event 子集与 G7；数值阈值只引用 `docs/90`
  Reference Target。
- WS-6 只消费 WS-4 已验收的 projection/SSE/reducer contract，不回头改变其事件语义来
  适配单一 Profile 或页面实现。

## Exit Invariants

1. 每个必须审计/投影的状态变化与 outbox fact 原子提交；已提交 fact 不依赖采样 trace、
   日志或进程内存。
2. RuntimeEvent/Audit Fact 不是 checkpoint 或授权真相；任何 projector、SSE 或
   telemetry 路径都不能驱动执行。
3. 同 tenant/source 重投幂等，跨 tenant 相同 source ID 不冲突；同 run 的 `run_seq`
   和同 target 的 `projection_seq` 按提交顺序单调。
4. duplicate、乱序、迟提交、断线、cursor gap 和 projector restart 后最终收敛且不
   重复展示；未知 schema 不猜测并使视图明确为 partial/stale。
5. Projection/Reconciliation Role、OTel Collector/backend 或 SSE client 故障不阻塞
   command、heartbeat、checkpoint 或 Run terminal commit，且所有 queue、buffer、
   retry 和数据库事务有界。
6. Observation/Inspect/SSE 每次从 Active Tenant Context 和当前 principal 重新授权；
   跨 tenant 枚举失败且不通过错误、日志、metric 或 timing 泄露存在性。
7. telemetry 不承载 Tenant/Principal/authorization/fence，不记录禁止字段，不使用高
   基数实例 ID 作为 metric label。
8. 角色 readiness、credential、pool 和能力保持分离；错误 role/profile 或权限超集
   fail closed。
9. 未启用 capability 明确返回 unavailable，不产生伪成功事件、空投影或占位 UI。
10. WS-4 只形成 Observation Slice 工程增量，不形成 Core/Product release 结论。

## 验收标准

- Contract：RuntimeEvent、InteractionItem、UIProjectionEvent closed union、schema/
  version、size limit、canonical fixture、source identity、cursor、watermark、partial/
  stale 和 unknown schema 全部有正常、边界及恶意用例。
- PostgreSQL integration：真实数据库证明状态/outbox 原子性、RLS/列权限、角色隔离、
  commit-ordered sequence、幂等 inbox、watermark CAS、dead-letter 与从空 read model
  重建。
- API/SSE：snapshot 后按 cursor backfill 再进入 realtime；慢 consumer、断线、重连
  风暴和 gap 不持有长事务、不产生无界内存，最终 0 丢失、0 重复展示。
- Reducer：同一 golden UI stream 重放两次产生相同 canonical RunViewState；duplicate、
  乱序、gap、unknown schema、projection 延迟和 tenant switch 不乐观完成、不越序应用
  且清除旧 tenant state。测试对象是 headless contract/reducer，不是 Vue 页面。
- Fault：分别 kill/restart outbox publisher、Projection/Reconciliation Role、SSE
  client，并禁用 Collector/backend；Run 继续推进，投影在 watermark 上恢复，
  telemetry drop/saturation 可观测。
- Security：跨 tenant run/event/interaction/inspect 全拒绝；伪造 trace/Baggage、
  source ref、schema、revision 和 UI event 不能改变可信上下文、恢复 Graph 或批准操作；
  导出内容通过敏感字段与 label 基数检查。
- 资源目标：验证当前 WS-4 适用的 Reference Target，包括 RuntimeEvent→SSE P95 ≤ 2 s、
  投影正常 P95 ≤ 5 s/恢复 ≤ 120 s、500 SSE connection、200 events/s 且单 event
  ≤ 64 KiB，以及 telemetry backend 连续不可用 15 min 时在线 P95/P99 退化不超过 10%。
- 运维：四个可行动 dashboard、对应 owner/SLO/alert/runbook、role health 和一次从告警
  定位到 safe Run Inspect 的演练可复现；不查询生产数据库、不提升敏感日志级别。
- 最终证据只声明 WS-4 适用的 N-07/N-11/N-29、POC-D UI/event 子集和 G7 工程证据，
  不提前关闭 WS-5 Core Release。

## 状态所有权与信任边界

- Agent Run、command、checkpoint 和 authorization 继续由现有 owner 持有。
- RuntimeEvent/Audit Fact 是不可采样的已提交观测事实，但不能恢复执行。
- RuntimeEvent 行的原子写入属于权威事务的观测侧扩展：在 run→command 锁序内
  分配 `run_seq` 并插入 outbox 行，不修改 WS-3 状态机转移逻辑；fault matrix 中
  "kill outbox publisher" 针对 relay/notify 环节，而非权威事务内的原子 INSERT。
  `run_seq` 由锁定 `agent_run` 行的 commit-order 分配（doc 10 §11、doc 16 §15）。
- Interaction/UI/Inspect 是可删除并重建的 read model，只报告 source watermark 和完整性。
- Diagnostic telemetry 允许有界采样或丢弃，只负责诊断，不证明业务事实。
- OTel Collector 属于可替换诊断基础设施，不是第 5 个 grove 部署角色
  （ADR-0023）；进程内 OTel SDK 使用有界 export，Collector 故障不反压在线执行。
- API/SSE 使用公共 ID，并在内部做 tenant-scoped ownership lookup；projection ID、trace
  context 和 Baggage 都不是授权凭据。

## API、数据与迁移

- 新增 table、index、RLS/FORCE RLS、trigger/function、role grant 和 downgrade 必须
  通过 Alembic 管理，并纳入真实 `upgrade head → downgrade base → upgrade head` 验证。
- Runtime SSE 使用 `run_seq`；UI/Interaction SSE 使用独立 `projection_seq`。
  PostgreSQL LISTEN/NOTIFY 只能唤醒，补偿事实必须按持久化 cursor 查询。
- Observation response 只暴露安全 public view、`as_of`、watermark 和
  `complete/partial/stale/unavailable`；不得返回内部 thread/checkpoint namespace、
  完整 State 或 raw payload。
- Run Inspect 只展示当前 WS-0～WS-3 已拥有的安全生命周期、失败分类、checkpoint 摘要
  和诊断引用；后续 Profile 数据明确 unavailable。

## 故障、资源与重试

- publisher、projector、SSE 和 OTLP export 都使用有界 batch、timeout、retry、queue/
  buffer 与总时限；异常和取消必须释放事务、连接与订阅。
- projector reconciliation 只从权威 fact/source 重建，不从现有 read model 推断缺失
  事实。
- slow client 不能占用长事务或耗尽在线连接池；projection 和 telemetry 使用独立配额，
  不能挤占 API/Runtime Worker。
- unknown schema、dead-letter、gap、lag、drop 和 saturation 必须形成低基数 health/
  metric/alert。

## 可观测性与运维

- MVP span 至少覆盖 API request、command accept、worker claim、run invoke、graph node、
  checkpoint、projector apply 和 SSE backfill 中实际存在的 seam。
- metric label 禁止 tenant/user/run/command/trace 及任意业务字符串；高基数 ID 只进入
  受控 trace/log。
- Collector 配置启用 memory limit、batch、attribute redaction 和有界 queued retry；
  无效策略或试图放宽安全底线时 fail closed。
- API/Runtime Worker readiness 不依赖 projector 或 telemetry backend；Projection/
  Reconciliation health 独立暴露 backlog、watermark、unknown schema、dead-letter 和
  gap repair。

## 前端阶段边界

- WS-4 只冻结并验证服务端 Observation/Projection/SSE contract、adapter seam 与
  headless deterministic reducer；不建立 Vue 页面、路由、store 或生产交互 module。
- WS-5 使用 WS-4 证据证明 Core backend 与运维边界，不补做产品 UI。
- WS-6 第一阶段实现通用 Vue shell、生产版 `RunInteractionModel`、Run History、Run
  Inspect、SSE reconnect 和 tenant cache reset；第二阶段接入选定 Profile 的 typed
  renderer 与真实 E2E。
- WS-7 只做 accessibility、性能、安全、load/soak、发布与回滚验证，不再引入新的核心
  UI 行为。

## 未决问题

无。

## 来源

### 权威来源

- [GROVE Roadmap](../../ROADMAP.md#work-packages)
- [P0 Blockers：N-07/N-11/N-29、POC-D、G7 与 Reference Target](../90_P0_Blockers_and_Acceptance.md)
- [Observability and Operations](../12_Observability_and_Operations.md)
- [Platform API：Observation API](../05_Platform_API.md#6-observation-api)
- [Canonical Execution Contracts：RuntimeEvent 与 UI Projection](../16_Canonical_Execution_Contracts.md#15-runtimeevent)
- [Execution Core：RuntimeEvent 与 SSE](../10_Execution_Core.md#11-runtimeevent-与-sse)
- [Frontend Interaction Design：Bootstrap、SSE 与 typed reducer](../06_Frontend_Interaction_Design.md#7-bootstrap-sse-与-typed-reducer)
- [README：Vue 前端阶段边界](../../README.md)
- [ADR-0014：观测性与最小运维是 MVP Foundation](../adr/0014-observability-is-an-mvp-foundation.md)
- [ADR-0013：MVP 提供 Run Inspect 而不提供 Time Travel](../adr/0013-mvp-provides-inspect-not-time-travel.md)
- [ADR-0023：按角色分进程的模块化单体](../adr/0023-start-with-a-role-separated-modular-monolith.md)

### Proposed

无。
