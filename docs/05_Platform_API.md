# Platform API

> 架构集：GROVE v1.0
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> 前端交互：[Frontend Interaction Design](./06_Frontend_Interaction_Design.md)
> 观测运维：[Observability and Operations](./12_Observability_and_Operations.md)
> 内部 ABI：[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)

## 1. 定位

GROVE 对 Application 暴露三个职责明确的接口：

```text
Platform API
├── Plan API
│   ├── discover
│   ├── estimate
│   ├── validate
│   └── preview
├── Execution API
│   ├── submit
│   ├── resume
│   ├── cancel
│   ├── fork
│   └── stream
└── Observation API
    ├── run / checkpoints / topology
    ├── events / interactions / ui_events
    ├── trace
    ├── artifact
    ├── evaluation
    └── experience
```

三类接口可以由同一个 FastAPI process 提供，但必须保持独立 contract、
权限和 module owner。“Platform API”只是产品入口和 transport router，
不拥有规划、执行或观测状态。

## 2. Agent 的定义

> **Agent = Skill Composition + Policy。**

Agent 是面向 Application 的场景配置，不是新的业务能力资产。能力、
版本、权限上限和 Evaluation 都属于 Skill。

```python
class AgentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_ref: str
    binding_version: str
    content_hash: str
    root_skill_ref: str
    policy_bundle_ref: str
```

例如：

```text
Business Operations Agent
  = BusinessOperationsSkillComposition@3
  + BusinessOperationsPolicyBundle@7

BusinessOperationsSkillComposition@3
  ├─ DomainAnalysisSkill@2
  ├─ PolicyInterpretationSkill@4
  ├─ DecisionSupportSkill@5
  └─ ReportSkill@3
```

约束：

1. Agent 不复制 Skill contract、Graph、Tool 或 Evaluation。
2. MVP Baseline 不建立独立 Agent Registry；Agent Binding 是 Application
   配置中的 immutable version，公开 alias/release pointer 可以指向它。
3. 解析后，Agent Binding version/hash 和 `root_skill_ref` 进入
   `SkillExecutionSpec`；完整 Skill closure 固定在其引用的
   `SkillRuntimeManifest`。
4. 动态路由只能在 Manifest 声明的 Skill closure、Spec permission 和
   resolved budget 闭集内进行。
5. 如果未来需要 Agent 模板市场，只增加配置 catalog，不把 Agent 提升为
   Capability。
6. Policy Bundle 只能收窄权限/预算或选择 Skill Version 已批准的策略；
   不能替换成未经该 Skill Evaluation 覆盖的 Prompt/Model/Tool/Action。

## 3. API 所有权

| 接口 | module owner | 权威输入 | 输出性质 |
|---|---|---|---|
| Plan API | Skill Framework + Resolver | Agent/Skill ref、intent、actor context、deployment profile | 无副作用的发现、估算、校验和预览 |
| Execution API | Execution Core | typed input、精确 resolve 条件、actor context | Agent Run command、handle 和 live stream |
| Observation API | Runtime/Interaction/Experience Projection | public run/skill/artifact ref、actor context | 只读 query view |

API 层不得：

- 直接读取或修改 LangGraph、DBOS 的系统表。
- 自己解析 `latest`、计算 permission 或拼接 Experience。
- 保存第二份 graph state。
- 把 Plan 结果当成执行授权。

### 3.1 Public Intent

Plan 与 submit 使用同一种目标表达，避免两套语义：

```python
class ExecutionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: UUID
    agent_ref: str | None = None
    skill_ref: str | None = None
    permission_preset_ref: str | None = None
    input: JsonValue
    constraints: ExecutionConstraints
```

`agent_ref` 与 `skill_ref` 必须且只能提供一个。JSON transport 到达 Skill
seam 后立即按已发布 input schema 转成 typed model；校验失败不能进入
estimate/preview/submit 的后续步骤。

`permission_preset_ref` 只能选择当前 tenant/actor 可见的 approved posture；
它不是 permission/scopes 声明。省略时使用 Agent/Tenant 固定的默认 preset。
`constraints` 只能表达 deadline、成本、质量或数据驻留等调用者约束，不能
自报 tenant、permission，也不能选择 spec 闭集外的 Model/Tool/Action。

普通 `submit` 只创建 `live` run。`replay/fork_dry_run/fork_commit` 必须通过
`fork` command 从授权的 source checkpoint 创建，不能由
`ExecutionIntent` 自报。

## 4. Plan API

### 4.1 Interface

```python
class PlanAPI(Protocol):
    async def discover(
        self,
        request: DiscoverCapabilities,
    ) -> CapabilityPage: ...

    async def estimate(
        self,
        request: ExecutionIntent,
    ) -> ExecutionEstimate: ...

    async def validate(
        self,
        request: ExecutionIntent,
    ) -> ValidationReport: ...

    async def preview(
        self,
        request: ExecutionIntent,
    ) -> ExecutionPreview: ...
```

### 4.2 discover

`discover` 返回当前 tenant、actor 和 deployment profile 可见、可候选的
Skill 摘要：

```text
skill_id / version / description
input/output schema refs
required capabilities
required permission summary
quality/cost/latency evidence summary
deprecation status
```

它不能泄露无权 Skill 的存在、内部 Tool 名称、Prompt、credential 或其他
tenant 的评测数据。

### 4.3 estimate

`estimate` 返回范围和假设，不返回虚假的精确报价：

```text
estimated_model_requests: min / likely / max
estimated_subgraph_delegations / fan-out width / goal iterations
estimated_child_runs: min / likely / max
estimated_tokens: min / likely / max
estimated_workspace_seconds: min / likely / max  # optional profile
workspace_resource_class                         # optional profile
estimated_latency_ms: p50 / p95
estimated_external_cost
possible_action_classes
confidence
assumptions
evidence_snapshot_ref
```

Estimate 是观测数据和固定 policy 下的预测，不是配额预留或费用保证。
Execution Kernel 仍必须执行真实 budget。

按 `evaluation_subject_hash/model_policy` 持续比较 estimate 与 actual；样本
不足、版本过期或误差超阈值时返回低置信度范围或 `unknown`，不能沿用旧
均值制造精确感。
启用 Multi-Agent route 时，actual 必须包含 same-run branch 和已委派 Child
Run 的归因 usage；不能只统计 Parent Run 后低估成本。
启用 Execution Workspace 时还必须归因实际 workspace lifetime/resource，
不能把 sandbox 成本隐藏在 Tool latency 中。

### 4.4 validate

`validate` 对候选执行做无副作用检查：

```text
input contract
Skill lifecycle and dependency closure
tenant visibility
actor ∩ tenant ∩ Skill permission
deployment capabilities
evaluation/release gate
policy compatibility
permission preset visibility/version/evidence
budget feasibility
artifact/version availability
```

`valid=true` 只说明在 `validated_at` 的快照下满足条件，不是授权凭证。

### 4.5 preview

`preview` 返回解析后的只读摘要：

```text
root Skill and dependency closure
graph/subgraph outline
allowed orchestration modes、Join/cancel policy 和 hard limits
possible Tool/Action classes
required approvals
effective permission summary
resolved permission preset ref/version/hash
Knowledge/Memory usage policy
budget and estimate range
contract/policy/version snapshot
runtime build ref/hash
evaluation_subject_hash and passed evidence summary
skill_spec_hash preview
```

Preview 不调用模型、不运行 Graph、不读取业务数据、不写 Memory，也不触发
Tool/Action。需要真实模型调用的模拟属于显式 dry-run Execution，不属于
Plan API。

## 5. Execution API

```python
class ExecutionAPI(Protocol):
    async def submit(
        self,
        request: SubmitExecution,
    ) -> AgentRunHandle: ...

    async def resume(
        self,
        request: ResumeExecution,
    ) -> AgentRunHandle: ...

    async def cancel(
        self,
        request: CancelExecution,
    ) -> CancellationResult: ...

    async def fork(
        self,
        request: ForkExecution,
    ) -> AgentRunHandle: ...

    async def stream(
        self,
        request: StreamExecution,
    ) -> AsyncIterator[RuntimeEvent]: ...
```

```python
class SubmitExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: UUID
    intent: ExecutionIntent
    expected_skill_spec_hash: str | None = None
```

```python
class ResumeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    run_id: UUID
    interrupt_ref: InterruptRef
    input: JsonValue
    expected_revision: int


class CancelExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    run_id: UUID
    expected_revision: int
    reason_code: str


class ForkExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: UUID
    source_run_id: UUID
    source_checkpoint_ref: CheckpointRef
    mode: Literal["replay", "fork_dry_run", "fork_commit"]
    fork_input: JsonValue | None = None
    memory_snapshot_mode: Literal["historical", "current"] = "historical"
    expected_source_revision: int
    expected_source_skill_spec_hash: str | None = None


class StreamExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    after_run_seq: int = 0
```

Execution API 不提供通用 `refresh_tool_result` 或在原 Run 上任意重执行 read Tool
的命令。已 checkpoint 的 Run Data View 是该 Run 的不可变执行事实；resume 只处理
匹配的 InterruptRef。是否允许 Graph 再次读取由 versioned Tool contract、Manifest
budget 与 Graph topology 共同固定，不能由客户端临时改写。当前
[Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md) 要求刷新时重新提交普通
`SubmitExecution`，生成新的 Run/spec/authorization 与 View。

`InterruptRef.interrupt_ref` 和 `CheckpointRef.checkpoint_ref` 是公开 opaque
ID，不是内部 LangGraph `checkpoint_id/thread_id`；其余字段是可验证
metadata。两者都不是访问凭证。resume input 必须先按 interrupt 对应的
已发布 schema 校验。

InterruptRef 绑定 tenant、run、checkpoint hash、interrupt schema 和一次性
nonce。API 在接受 resume 时校验当前 checkpoint projection 与 expected
revision，Driver/Kernel 在应用 input 前再读取权威 checkpoint 验证并原子消费
nonce。不能只相信可能滞后的 `agent_run.status`；旧/跨 run interrupt 返回
`RunStateConflict/CheckpointUnavailable`，node 与 reducer 应用数为 0。

`fork` 必须验证 `CheckpointRef.tenant_id/run_id` 与认证 tenant 和
`source_run_id` 一致，并校验 checkpoint hash、Graph Version、State schema
和 source spec binding；任何跨 run 拼接、过期 reference 或 hash tampering
都在创建新 run 前失败。

`fork_input` 只按 Graph Version 发布的 fork-input schema 更新允许字段，禁止
提交任意 LangGraph State patch。`replay` 不接受 `fork_input`，并强制使用
historical Memory 和 recorded-result adapters。

`submit` 必须：

1. 使用精确 Agent/Skill/Policy ref 重新 resolve。
2. 对当前 actor、tenant、command-determined run mode 和 deployment
   capability 重新校验。
3. 验证 typed input。
4. 原子持久化 immutable `SkillExecutionSpec`、`agent_run` 和审计引用。
5. 在同一 transaction 插入 `start` run command；需要 run-created audit
   event 时另写 observation outbox，但它不驱动执行。
6. commit 后由 PostgreSQL Execution Driver 异步启动 LangGraph。

客户端可传 `expected_skill_spec_hash` 做乐观一致性检查。若 Plan 后配置变化，
返回 `PlanChanged`，不能静默执行不同计划。

`submission_id` 是 public idempotency key。同一 tenant 下重复提交相同 request
digest 返回原 `run_id`；同一 ID 携带不同 digest 返回
`SubmissionConflict`，不能创建第二个 run。

Digest 覆盖 canonical intent、认证主体和 tenant scope。重复请求仍需重新
授权；另一个 actor 猜中 `submission_id` 不能读取原 run，而是返回无权或
冲突。

`resume`、`cancel`、`fork` 每次重新授权。前两者以 `expected_revision`
对目标 run 做 CAS，`fork` 以 `expected_source_revision` 对 source run 做
CAS。同一 `command_id/submission_id` 和相同 digest 返回原结果；相同 ID
携带不同 digest 返回 `CommandConflict/SubmissionConflict`。

command digest 覆盖 canonical request、source/interrupt/checkpoint ref、
expected revision、认证主体和 tenant scope。重复命令仍需重新授权；猜中 ID
不能读取原结果。

`fork` 总是创建新的 run/spec/thread 和幂等命名空间。禁止原地把
`fork_dry_run` 改成 `fork_commit`；要提交 dry-run，必须从选定 dry-run
checkpoint 再创建一个 `fork_commit`。

time-travel resolve 锚定 source spec，而不是重新解析当前 Agent alias 或
`latest`：

1. `replay` 固定 source 的 Graph/Contract/SkillRuntimeManifest/
   RuntimeBuildManifest，以及 Model、Prompt、
   retry、Knowledge、Memory、routing、redaction policy，并强制 historical
   snapshot 与 recorded-result adapter；独立 Run Delegation 同样只读取
   recording，不创建真实 Child Run。
2. `fork_dry_run/fork_commit` 固定 source 的 Graph、State schema、
   Contract、两类 Manifest 和上述 policy；当前 authorization/run-mode
   policy、tenant/actor 与预算上限重新求交集。
3. `fork_commit` 还要求该精确 Skill build 在当前 publication/evaluation
   gate 下仍允许创建可写 run；否则 fail fast。
4. 重新计算导致 permission envelope、预算或其他行为 hash 变化时，必须有
   匹配 evidence。当前 `ForkExecution` 不支持切换 Graph、Runtime Build 或
   其他行为 build；
   这类需求必须使用未来单独定义、带显式目标与已发布 checkpoint migrator
   的 migration command，不能把当前 alias 静默套到历史 State。

time-travel run 默认创建新的 `orchestration_id`。`fork_commit` 后续产生
Child Run 时使用新的 delegation/submission namespace，不能复用 source
Parent/Child relation。

`expected_source_skill_spec_hash` 只做 source 一致性检查，不授权使用该历史
spec。新 run 的 spec/hash 包含新 run mode、当前 permission binding 和适用的
evidence。

time-travel 保留 source spec 的 permission preset，并与当前授权/run-mode
policy 重新求值；当前 `ForkExecution` 不允许切换 preset。需要另一种 posture
时创建普通新 run，不能修改活动 run 或历史 checkpoint。

Cancel 只请求停止未来工作，不能承诺撤销已完成的外部事实。`stream` 是同一
RuntimeEvent projection 的实时 tail，不是第二套 event source。

Execution API 不增加 public `spawn_agent` endpoint。same-run Sub-agent、
Swarm 和 GoalLoop 是 root Graph 内部 route；独立 Child Run 只能由受信任
Kernel policy node 经 Run Delegation Coordinator 创建。这样客户端不能绕过
Manifest closure、预算和 Parent Run fence 直接生成 Child Run。

schedule/event Trigger Adapter 也是普通 submit caller。它验证固定 Trigger
Definition 和 occurrence 后，以稳定 `submission_id` 调用 `submit`；不得直接
调用 LangGraph、写 `run_command` 或 resume checkpoint。Trigger 语义见
[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)。
`trigger_ref/version/hash/occurrence_id` 只能由认证的 adapter transport
context 注入并进入 submission digest/Run provenance，不能加入 public
`ExecutionIntent` 让客户端自报。

## 6. Observation API

```python
class ObservationAPI(Protocol):
    async def run(self, query: RunQuery) -> AgentRunView: ...
    async def checkpoints(self, query: CheckpointQuery) -> CheckpointPage: ...
    async def topology(self, query: RunTopologyQuery) -> RunTopologyView: ...
    async def events(self, query: EventQuery) -> EventPage: ...
    async def interactions(self, query: InteractionQuery) -> InteractionPage: ...
    async def ui_events(
        self,
        query: UIEventQuery,
    ) -> AsyncIterator[UIProjectionEvent]: ...
    async def trace(self, query: TraceQuery) -> TraceView: ...
    async def artifact(self, query: ArtifactQuery) -> ArtifactView: ...
    async def evaluation(self, query: EvaluationQuery) -> EvaluationView: ...
    async def experience(self, query: ExperienceQuery) -> ExperienceView: ...
```

来源：

| Query | 数据来源 |
|---|---|
| run | Agent Run Projection |
| checkpoints | 授权后的 LangGraph checkpoint projection |
| topology | Graph build、RuntimeEvent、Run Delegation 与 safe checkpoint projection |
| events | Runtime Event Projection |
| interactions | Interaction Projection：safe checkpoint、Action Projection、Run Delegation 与 RuntimeEvent |
| ui_events | Interaction/UI Projection outbox 的 typed realtime tail |
| trace | 受治理 Trace Projection |
| artifact | Artifact metadata/store |
| evaluation | Skill Registry/Evaluation evidence |
| experience | optional Experience Projection |

Observation API 只返回可重建 read model，不拥有 checkpoint、Evaluation 或
Experience。未启用 Experience 等 optional capability 时返回明确的
`CapabilityUnavailable`，不能伪装为空结果。

`TraceView` 必须返回 sampling policy ref、backend `as_of` 和
`complete/partial/unavailable` 状态；因未采样、export drop 或 backend 故障
缺失时返回明确 reason，不能伪装成“该调用未发生”。审计事实仍以
RuntimeEvent/业务记录为准。

Checkpoint projection 只公开 checkpoint ref、创建时间、可安全显示的 node/
status 摘要、lineage、`inspect_available/replay_available` 和缺失 reference
类别，不公开完整 State、内部 ID 或未经字段级授权的数据。

`AgentRunView` 公开 `orchestration_id`、可授权的
`parent_run_id/parent_delegation_id`、精确 version/hash 的 trigger
provenance 和
`waiting_child_result` 状态，但不把 `run_delegation` projection 当成第二份
Child lifecycle。

`RunTopologyQuery` 以一个已授权 public `run_id` 为入口，深度和节点数必须
有上限。`RunTopologyView` 区分：

```text
time-travel source lineage
online orchestration parent/child relation
same-run subgraph invocation
fan-out branch
GoalLoop iteration
```

它同时返回 `as_of`、各 source watermark、`complete/partial/stale`
完整性状态和脱敏的 unresolved reference 类别。Projector backlog、未知事件
schema 或受授权 source 尚未对账时不得宣称 complete；调用方不能把 partial
视图中的缺失节点解释为“不存在”。

Topology 只返回 public run/reference、Skill Version、状态摘要、时间、
budget/usage 摘要和脱敏 failure；不返回内部 thread/checkpoint namespace、
完整 State、Prompt 或 Child credential。Parent Execution SSE 只包含 Child lifecycle
摘要，Child 内部事件必须按 Child `run_id` 再授权查询，因此不存在跨 run 的
伪全局顺序。

### 6.1 Interaction Projection 与 typed UI events

`InteractionProjector` 是 Observation 内部的可重建 read model，不是新服务，
也不是第二套 execution event source。它把已授权的 safe checkpoint
interrupt、Action approval、Parent/Child relation 和 RuntimeEvent 投影成：

```text
InteractionItem snapshot
  interaction_id
  presentation_run_id / owner_run_id / orchestration_id
  kind = user_input | permission_request | business_approval
  source_ref / source_hash / payload_schema_ref
  safe typed payload
  status = pending | resolved | expired | cancelled | stale
  revision / source watermarks / timestamps

UIProjectionEvent delta
  event_id / target_ref / projection_seq
  kind + payload_schema_ref + typed payload
  source_refs / source_watermarks / projected_at
```

`UIProjectionEvent.payload` 是按 discriminator 定义的 closed union，初始只含
message start/delta/end、interaction upsert/resolved、run status 和 child
status；禁止自由 `name + dict` CustomEvent。每种 payload 独立 version/size
limit，未知 schema 进入 dead letter。

首次加载读取 `InteractionPage` snapshot，随后按
`(target_ref, projection_seq)` 消费 `ui_events` SSE；断线先补 delta，再按
snapshot revision 对账。projector 用 `(source_ref, source_hash/revision)`
幂等，保存 source watermark，并周期 reconciliation。backlog、未知 schema
或未授权 source 导致 `partial/stale`，不能把缺失 interaction 当成已解决。

UI 只能经权威命令响应：用户输入和 permission request 使用
`ExecutionAPI.resume(owner_run_id, InterruptRef, expected_revision)`；业务审批
使用 Durable Action 的 approval command。Child interrupt 可以呈现在 Parent
inbox，但仍以 Child checkpoint 为准并路由到 `owner_run_id`。过期、重复、跨
run 或 revision 不匹配的响应必须拒绝；更新 projection 不能恢复 Graph、批准
Action 或修改权限。

页面信息架构、主信息流、pending interaction、typed reducer、command UX 和
多租户前端隔离见
[Frontend Interaction Design](./06_Frontend_Interaction_Design.md)。本专题只拥有
服务端 public interface 和 projection contract，不拥有页面 presentation state。

## 7. Plan → Execution 一致性

Plan 与 Execution 之间存在天然 TOCTOU：

```text
preview at T1
  → Skill/Policy/permission/capability changes
  → submit at T2
```

MVP Baseline 采用最小方案：

1. Plan response 包含 `snapshot_at`、精确 version refs 和
   `skill_spec_hash`。
2. Plan response 不携带授权 token。
3. Submit 总是重新 resolve 和授权。
4. 若调用者提供 `expected_skill_spec_hash` 且结果不同，fail fast。
5. 成功 submit 后以持久化 spec 为唯一执行依据。

只有出现“预留稀缺资源”或“审批后稍后执行”的真实需求时，才引入带过期、
签名和主体绑定的 `PreparedExecution`；MVP Baseline 不预建。

## 8. Error Contract

三类接口共享稳定 error code，但不共享可变 context：

```text
CapabilityNotFound
CapabilityUnavailable
PermissionDenied
InputContractInvalid
MissingCapability
EvaluationGateFailed
VersionUnavailable
PlanChanged
SubmissionConflict
CommandConflict
DelegationConflict
BranchResultConflict
RunSignalConflict
TriggerConflict
BudgetRejected
ToolQueryTooBroad
ResourceSelectionUnavailable
RunNotFound
RunStateConflict
CheckpointUnavailable
ReplayDataUnavailable
ReplayDataMismatch
ArtifactUnavailable
ProjectionNotReady
```

Error response 包含 `error_code`、`correlation_id`、可安全公开的 field
violations 和可选 `retry_after`；不得回显内部 ID、Prompt 或敏感策略。

`ToolQueryTooBroad` 表示按该 Tool contract 无法在可信预算内形成完整结果。response
只用 field violation 表示安全 `limit_kind` 和缩小范围建议，不含实际总量、部分
数据或内部 limit。其 retry owner 与是否必须新建 Run 由固定 Tool contract 决定；
Asset Risk Reference Profile 中它是 terminal、不可自动重试。

`ResourceSelectionUnavailable` 对不应向调用者区分的不存在、不可见、未授权、
跨 Tenant 或竞态失效资源使用同一 code/message/shape。response 不返回失败 ref、
索引、匹配数、omitted count 或内部原因。具体 selection 与重试策略由 Profile
固定；Asset Risk Reference Profile 要求重新选择并创建新 Run。

## 9. 安全和可观测性

1. tenant/actor 来自认证上下文，不接受客户端自报。
2. discover、topology、interactions/ui_events、trace、artifact、evaluation 和
   experience 都单独授权。
3. Plan 事件与 Execution 事件使用同一 correlation chain，但不同 event
   type。
4. 记录 Plan 输入 hash、snapshot、候选数、过滤原因摘要和 latency。
5. 不记录未脱敏 intent、credential、完整 Prompt 或无权 capability 名称。
6. 限制 discover page、preview graph size、query window 和 artifact range。

## 10. 最小实现

可以同进程部署：

```text
FastAPI process
├── /plan/*         → Skill Resolver
├── /executions/*   → Execution Core
└── /observations/* → read projections

PostgreSQL Execution Driver worker
└── persisted run command → fenced LangGraph invocation
```

API interface 分离不等于微服务拆分。Driver worker 与 API 使用同一部署制品，
但必须是独立进程/worker role，使 HTTP request 结束或 API 进程重启不影响
已提交 command。只有进一步的扩缩容、故障隔离或治理所有者成为真实需求时，
再拆为独立部署单元。

## 11. 被否决的方案

- 将 discover、plan、run、trace、evaluation 全部加入万能 `AgentRuntime`。
- Agent 自己复制 Skill contract、Graph、Permission 和 Evaluation。
- `validate` 结果直接作为 submit 授权。
- `preview` 暗中执行模型、Tool 或 Action。
- Observation API 直接查询 framework system tables。
- 用 Interaction/UI projection 替代 Interrupt、Action approval 或 checkpoint。
- 向前端发送自由 `name + dict` CustomEvent。

## 12. 技术依据

- [AgentScope typed events](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/event/_event.py)
- [AgentScope session projection](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/app/_service/_session_projection.py)
- [AgentScope permission presets](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/permission/_types.py)
- 为 API 分层提前引入三个微服务或三套数据库。
