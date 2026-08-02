# Canonical Execution Contracts

> 架构集：GROVE v1.0
> 上位文档：[Execution Core](./10_Execution_Core.md)
> 集成约束：[LangGraph + PydanticAI Integration](./15_LangGraph_PydanticAI_Integration.md)
> 编排语义：[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)
> 上游 ABI：[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)

## 1. 定位

Canonical Execution Contracts 是平台 module seam 上的稳定、不可变 typed
messages。它们负责隔离 LangGraph、PydanticAI、Knowledge、Memory、
Execution Workspace、Tool、Durable Action、Experience 的内部模型。

它们不是：

- 第四份 Runtime State。
- 一个贯穿全系统的 mutable `ExecutionContext`。
- 通用 Graph IR。
- framework object 的序列化镜像。
- 把所有数据塞进同一个 envelope 的理由。

LangGraph State 仍是 Agent Run 执行状态唯一持久化来源。

## 2. Contract 类别

| 类别 | 语义 | 示例 |
|---|---|---|
| Payload | 模型生成的无可信身份字段建议；不能跨 trust seam 执行 | `ToolProposalPayload` |
| Request | 经过 policy 授权的一次无副作用读取或有界推理 | `CanonicalInferenceRequest`、`KnowledgeRequest` |
| Decision | Kernel enrichment 后的下一步建议，仍不具有执行权 | `ToolProposal`、`ActionProposal` |
| Command | 经过 Kernel policy 授权后的执行命令 | `ToolCommand`、`WorkspaceAcquireCommand`、`ActionCommand` |
| Result | 某个 module 的完成结果 | `KnowledgeResult`、`ToolResult` |
| Reference | 指向不可变或版本化内容 | `ArtifactRef`、`TraceRef` |
| Event | 已发生事实的观测记录 | `RuntimeEvent` |
| Failure | 标准化、脱敏的失败描述 | `CanonicalFailure` |

Payload、Decision 与 Request/Command 必须分开。模型只能生成 Payload；
Node Adapter 注入可信 provenance 形成 Decision；只有 LangGraph policy node
能产生可发送到 Knowledge/Tool/Action seam 的 Request/Command。

## 3. 通用规则

所有 top-level Request、Decision、Command、Result 和 Event：

1. 使用 `extra="forbid"` 和 frozen model。
2. 声明稳定 `contract_name` 与 `contract_version`。
3. 使用 UUID message/request ID，不复用 framework attempt ID。
4. 带 `tenant_id`、`correlation_id` 和必要的 `causation_id`。
5. 时间使用 UTC RFC 3339；金额使用明确 currency；大小和数量有上限。
6. 只携带当前调用所需最小数据。
7. 大内容、敏感内容和 provider 原始响应使用 reference。
8. 不包含 LangGraph `State/Command`、PydanticAI `Agent/RunContext`、DBOS
   handle、workspace provider client 或数据库 session。

Model Decision Payload 不携带 `ContractMeta`、tenant、run、permission、
authorization、request ID 或 idempotency key。它不是跨 module contract，
只能作为一次 `CanonicalInferenceResult` 的 typed result，由 Node Adapter
立即校验和 enrichment。

公共 metadata：

```python
class ContractMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_name: str
    contract_version: str
    message_id: UUID
    tenant_id: str
    correlation_id: str
    causation_id: UUID | None = None
    trace_id: str | None = None
```

`ContractMeta` 是不可变标识，不承载 runtime state、permission object 或
通用 payload。

## 4. Contract Catalog

```text
Execution binding
└── SkillExecutionSpec

Inference
├── CanonicalInferenceRequest
├── CanonicalInferenceResult
├── InferenceDecisionPayload
│   ├── FinalAnswerPayload
│   ├── KnowledgeProposalPayload
│   ├── ToolProposalPayload
│   ├── ActionProposalPayload
│   └── DelegateProposalPayload
└── CanonicalDecision
    ├── FinalAnswer
    ├── KnowledgeProposal
    ├── ToolProposal
    ├── ActionProposal
    └── DelegateProposal

Knowledge
├── KnowledgeRequest
├── KnowledgeResult
└── Citation

Working context
├── ContinuationPendingRef
└── ContinuationSummary

Memory（optional）
├── MemoryRecallRequest
├── MemoryRecallResult
└── MemoryCandidate

Workspace（optional）
├── WorkspaceAcquireCommand
├── WorkspaceHandleRef
└── WorkspaceReleaseCommand

Tool
├── ToolCommand
└── ToolResult

Action（optional）
├── ActionProposal
├── ActionCommand
├── ActionHandle
└── ActionReceipt

Orchestration
├── DelegationCommand
├── DelegationResult
├── RunWaitRef
├── RunSignal
├── ChildRunRequest                # run.delegation
├── ChildRunHandle                 # run.delegation
└── ChildRunCompletion             # run.delegation

Artifacts and observation
├── ArtifactRef
├── CheckpointRef
├── InterruptRef
├── ReplayRecordingRef
├── TraceRef
├── ExperienceManifestRef
├── EvaluationEvidenceRef
├── RuntimeEvent
├── InteractionItem
├── UIProjectionEvent
└── CanonicalFailure
```

`SkillExecutionSpec` 是 Skill Framework → Execution Core 的 ABI，完整规范见
[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)。

Catalog 是协议命名空间，不是 MVP Baseline 的一次性实现清单：

```text
MVP Baseline required
  SkillExecutionSpec
  Inference / Decision
  Knowledge
  ArtifactRef / CheckpointRef / InterruptRef
  EvaluationEvidenceRef / Failure / RuntimeEvent
  InteractionItem / UIProjectionEvent

Release Track gated
  ReplayRecordingRef    when Time Travel enabled
  DelegationCommand / DelegationResult / RunWaitRef / RunSignal
                        when Multi-Agent enabled
  Child Run contracts    when run.delegation enabled
  Memory contracts       when memory.long_term enabled
  Workspace contracts    when execution.workspace enabled
  Action contracts       when durable_action enabled
  Experience contracts   when experience.projection enabled

Runtime-build gated
  Continuation contracts when context compression is enabled
```

没有真实 caller/adapter 的 contract 只保留设计，不提前生成 package、table
或 transport endpoint。

## 5. Reference Contracts

```python
class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    tenant_id: str
    version: str
    content_hash: str
    media_type: str
    schema_ref: str | None = None
    sensitivity: str
    retention_policy_ref: str


class CheckpointRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_ref: str
    checkpoint_hash: str
    tenant_id: str
    run_id: UUID
    graph_version: str
    graph_state_schema_version: str
    created_at: datetime


class InterruptRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interrupt_ref: str
    interrupt_hash: str
    tenant_id: str
    run_id: UUID
    checkpoint: CheckpointRef
    interrupt_schema_ref: str
    nonce_hash: str
    created_at: datetime
    expires_at: datetime | None


class RunWaitRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    wait_ref: str
    wait_hash: str
    tenant_id: str
    run_id: UUID
    checkpoint: CheckpointRef
    wait_kind: Literal["action_result", "child_result"]
    source_ids: tuple[str, ...]
    result_schema_ref: str
    created_at: datetime
    expires_at: datetime | None


class ReplayRecordingRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recording_ref: str
    recording_hash: str
    tenant_id: str
    source_run_id: UUID
    source_checkpoint: CheckpointRef
    graph_version: str
    node_execution_key: str
    seam_kind: Literal[
        "inference",
        "knowledge",
        "tool",
        "memory",
        "action",
        "delegation",
    ]
    logical_call_ordinal: int
    request_semantic_hash: str
    result_ref: ArtifactRef
    result_schema_ref: str
    result_hash: str
    created_at: datetime


class TraceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    tenant_id: str
    redaction_policy_ref: str


class ExperienceManifestRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experience_id: UUID
    tenant_id: str
    version: str
    content_hash: str
    collection_policy_ref: str


class EvaluationEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_run_id: UUID
    tenant_id: str
    evaluation_subject_hash: str
    suite_ref: str
    decision: Literal["passed", "failed", "inconclusive"]
    evidence_bundle_hash: str
    issuer: str
    attestation_ref: ArtifactRef
```

Reference 不是访问凭证。consumer 每次解引用都必须重新授权，且校验
tenant、version 和 content hash。
Evaluation evidence 还必须验证 issuer trust policy、attestation signature、
subject hash 和 bundle hash；数据库中一条自报 `passed` 的记录不构成门禁
证据。

InterruptRef 只能在其绑定的当前 checkpoint 上消费一次。nonce 的原子消费与
resume input 的 checkpoint commit 使用同一 fenced transaction；重复的相同
command 只返回原结果，不同 command 或旧 checkpoint 不能再次注入 input。

RunWaitRef 只供内部 bridge/Driver 使用，不向 public resume 暴露。
`source_ids` 使用稳定排序且有数量上限；它表达当前 checkpoint 等待的确切
Action 或 delegation 集合。Run Signal 必须匹配其 tenant、run、checkpoint、
wait kind、source ID、result schema 和 hash，不能只依据可能滞后的
`agent_run.status` 唤醒。
`result_schema_ref` 指向 `ActionReceipt` 或 `DelegationResult` 等送入父
reducer 的 envelope schema；Child factual status 另由
`ChildRunCompletion` 固定，因此同一 wait 可以等待不同 Skill output。

ReplayRecordingRef 的 lookup key 是
`(source_run, node_execution_key, seam_kind, logical_call_ordinal)`；
`logical_call_ordinal` 由 Kernel 确定性分配，模型不能提供。录制的
`request_semantic_hash` 排除新 run/request/trace ID 和当前 authorization
reference，但包含 typed business input、精确 policy/snapshot/resource ref
和 schema version。replay request hash 不匹配时返回 `ReplayDataMismatch`，
不能返回相邻结果或调用真实 seam。

对 `workspace_local` Tool，semantic hash 排除物理 workspace ref/instance、
source run binding 和执行 fence；这些值在新 run 中必然不同。它必须包含
workspace policy、RuntimeBuild/sandbox image、bootstrap ArtifactRef、Tool
effect、规范化相对路径/input 和 contract version；逻辑 scope 已由
`node_execution_key` 与 ordinal 固定。任一环境或输入变化都必须 mismatch，
不能用忽略整个 Workspace binding 的方式提高命中率。

## 6. Inference Contracts

```python
class CanonicalInferenceRequest(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    inference_request_id: UUID
    run_id: UUID
    node_id: str
    node_attempt: int
    input: InputT
    context: InferenceContext
    context_refs: tuple[ArtifactRef, ...]
    instructions: tuple[CanonicalMessage, ...]
    result_schema_ref: str
    prompt_policy_ref: str
    model_policy: ResolvedModelPolicy
    model_policy_ref: str
    retry_policy: ResolvedInferenceRetryPolicy
    inference_retry_policy_ref: str
    budget: InferenceBudget
    budget_policy_ref: str


class CanonicalInferenceResult(BaseModel, Generic[ResultT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    inference_request_id: UUID
    result: ResultT
    model_ref: str
    usage: ModelUsage
    provider_attempts: int
    schema_retries: int
    provider_response_ref: ArtifactRef | None
```

约束：

- `input/result` 必须对应已发布 schema，不能使用无约束
  `dict[str, Any]`。
- 相同 logical inference 的 provider/schema retry 保持同一个
  `inference_request_id`。
- LangGraph 显式重新推理时必须生成新 ID，并通过 `causation_id` 关联。
- Result 不携带 PydanticAI message history 或 provider object。

模型结构化输出使用独立 payload union：

```python
InferenceDecisionPayload = Annotated[
    FinalAnswerPayload[OutputT]
    | KnowledgeProposalPayload
    | ToolProposalPayload[InputT]
    | ActionProposalPayload[InputT]
    | DelegateProposalPayload[InputT],
    Field(discriminator="kind"),
]


class FinalAnswerPayload(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["final_answer"]
    output: OutputT
    rationale_summary: str
    confidence: float


class KnowledgeProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["knowledge_proposal"]
    query: str
    knowledge_refs: tuple[str, ...]
    filter: KnowledgeFilter
    rationale_summary: str
    confidence: float


class ToolProposalPayload(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool_proposal"]
    tool_ref: str
    input: InputT
    rationale_summary: str
    confidence: float


class ActionProposalPayload(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["action_proposal"]
    action_ref: str
    input: InputT
    rationale_summary: str
    confidence: float


class DelegateProposalPayload(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["delegate_proposal"]
    target_skill_ref: str
    input: InputT
    rationale_summary: str
    confidence: float
```

所有 payload 都有明确 schema、enum 和大小限制，但没有可信 metadata。
`CanonicalInferenceResult[
InferenceDecisionPayload]` 到达 Node Adapter 后，必须先验证 result schema、
Manifest closure 和字段限制，再注入 `meta/run_id/decision_id` 形成 Canonical
Decision。payload 永不直接发送给业务 adapter。

生成给模型的 `ToolProposalPayload` JSON Schema 必须把 Tool ref 收窄为当前
SkillExecutionSpec closure 的精确允许集合，并为每个 ref 使用对应 versioned input
schema 与 `extra="forbid"`。Tenant、Principal、可信 scope/budget、authorization、
credential 和 adapter 实现字段永远不是模型字段；即使模型输出，Node Adapter 也
必须在 Tool provider 前拒绝，不能清洗后继续。数据库型 Tool 也不得暴露 SQL 或
database/schema/table/column/join 等实现对象。

完整 retry、structured-output 和 Node Adapter 规则见
[LangGraph + PydanticAI Integration](./15_LangGraph_PydanticAI_Integration.md)。

## 7. Decision Contracts

```python
CanonicalDecision = Annotated[
    FinalAnswer
    | KnowledgeProposal
    | ToolProposal
    | ActionProposal
    | DelegateProposal,
    Field(discriminator="kind"),
]
```

所有 Decision 至少包含 `meta`、`run_id`、`decision_id`、`kind`、
`rationale_summary` 和 `confidence`。核心变体：

```python
class FinalAnswer(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["final_answer"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    output: OutputT
    artifact_refs: tuple[ArtifactRef, ...]
    rationale_summary: str
    confidence: float


class DelegateProposal(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["delegate_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    target_skill_ref: str
    input: InputT
    rationale_summary: str
    confidence: float
```

`KnowledgeProposal` 和 `ToolProposal` 具有相同可信 metadata，但仍只是建议：

```python
class KnowledgeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["knowledge_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    query: str
    knowledge_refs: tuple[str, ...]
    filter: KnowledgeFilter
    rationale_summary: str
    confidence: float


class ToolProposal(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    tool_ref: str
    input: InputT
    rationale_summary: str
    confidence: float
```

`rationale_summary` 是可审计说明，不要求保存或暴露 chain-of-thought。
PydanticAI 只生成不含 tenant/run/permission 的 typed decision payload；
Node Adapter 校验 payload 后注入可信 `meta/run_id/decision_id`，再形成
Canonical Decision。字段名相同不代表对象相同，模型不能自报这些字段。

Kernel 接受 Decision 前必须验证：

1. discriminator 与 schema。
2. capability 在 `SkillExecutionSpec.runtime_manifest` closure 内。
3. input 符合目标 schema。
4. permission、run mode、budget、loop 和 delegation-depth limit。
5. tenant/resource scope。

## 8. Orchestration Contracts

`DelegateProposal` 仍只是建议。只有 policy node 可以形成可执行
`DelegationCommand`：

```python
class DelegationCommand(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    decision_id: UUID
    delegation_id: UUID
    parent_run_id: UUID
    parent_node_execution_key: str
    logical_delegation_ordinal: int
    delegation_depth: int
    target_skill_ref: str
    execution_mode: Literal["subgraph", "child_run"]
    input_schema_ref: str
    input: InputT
    output_schema_ref: str
    authorization_decision_ref: str
    delegated_permission_ref: str
    delegated_permission_hash: str
    budget_policy_ref: str
    budget_allocation_ref: str
    budget_allocation_hash: str
    execution_fence: int


class DelegationResult(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    delegation_id: UUID
    parent_run_id: UUID
    execution_mode: Literal["subgraph", "child_run"]
    child_run_id: UUID | None
    output_schema_ref: str
    status: Literal[
        "succeeded",
        "failed",
        "denied",
        "cancelled",
    ]
    output: OutputT | None
    artifact_refs: tuple[ArtifactRef, ...]
    usage: UsageSummary
    result_authorization_ref: str | None
    failure: CanonicalFailure | None
```

`delegation_id` 由 Kernel 根据 Parent Run、node execution key 和 logical
ordinal 确定性生成。模型、客户端和 Child Skill 都不能提供。
`execution_fence` 用于阻止已取消/接管的 Parent worker 首次创建 Child Run；
same-run subgraph 也记录它以关联当前 node attempt，但不产生第二个
lifecycle。

`delegated_permission_ref/hash` 固定不含 credential 的有效 scope/resource/
effect 包络；`budget_allocation_ref/hash` 固定父 checkpoint 已预留的本次
graph step、token、cost、call/descendant quota 和 absolute deadline。两者都
必须能从 prepared checkpoint 的受治理 reference 解出并校验，不能指向可移动
alias。

delegation semantic digest 包含 tenant/Parent Run、node execution key、
ordinal、`delegation_depth`、精确 target Skill、execution mode、typed
input、schema、上述预算 allocation/deadline 和 delegated permission
envelope；排除
`meta.message_id/trace`、`execution_fence` 和可在重授权时变化的
authorization decision ref。takeover worker 可以在相同业务委派上注入当前
fence/authorization，但不能改变 target、input、mode、permission package 或
预算。

`DelegationResult.output_schema_ref` 必须等于对应 Command/Handle 固定的
schema。成功状态要求 output 可按该 schema 校验且 failure 为空；其他状态
要求 failure 与 output 恰当互斥。Child Run output 较大或敏感时只返回
`ArtifactRef`，不能复制完整 Child State。
Child completion delivery 必须重新授权；Child Run 的 succeeded 或 denied
delivery 必须保存当时的 `result_authorization_ref`，撤权时返回不含业务
结果的 `status=denied` DelegationResult。same-run subgraph 可以复用
command authorization reference。

独立 Child Run 使用：

```python
class ChildRunHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    delegation_id: UUID
    parent_run_id: UUID
    child_run_id: UUID
    orchestration_id: UUID
    child_skill_spec_hash: str
    delegated_permission_hash: str
    budget_allocation_hash: str
    delegation_depth: int
    output_schema_ref: str
    status: Literal["accepted"]


class ChildRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    delegation: DelegationCommand
    prepared_checkpoint: CheckpointRef


class ChildRunCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    completion_id: UUID
    delegation_id: UUID
    parent_run_id: UUID
    child_run_id: UUID
    child_terminal_revision: int
    status: Literal["succeeded", "failed", "cancelled"]
    output_schema_ref: str
    result_ref: ArtifactRef | None
    result_hash: str | None
    failure: CanonicalFailure | None
    completed_at: datetime
```

Coordinator 接受 ChildRunRequest 前必须校验：

1. request/delegation/checkpoint metadata 的 tenant、correlation 和 Parent
   Run 一致。
2. `prepared_checkpoint` 属于同 tenant/Parent Run/Graph/State schema。
3. checkpoint metadata 已记录同一
   `delegation_id/command semantic hash/node execution key`，以及同一
   permission/budget allocation ref/hash。
4. delegation fence 仍是 Parent Run 当前 fence。
5. `delegation_depth` 等于 Parent 当前调用栈深度 + 1，且不超过 resolved
   hard limit。

Child Run acceptance 随后原子创建 child spec、`agent_run`、start command
和 coordination relation。相同 delegation command digest 返回原 Handle；
不同 digest 返回 `DelegationConflict`。不能仅凭内存中的
`DelegationCommand` 创建 Child Run。

ChildRunCompletion `succeeded` 时 `result_ref/result_hash` 必填且 failure
为空；`failed/cancelled` 时 failure 必填，业务无结果时 result 为空。
Completion `output_schema_ref` 必须等于 Handle/Child Spec 绑定，且
`result_ref.schema_ref/content_hash` 与之匹配；不能引用完整 checkpoint。

Action/Child terminal fact 通过同一种内部 signal command 进入等待 Run：

```python
class RunSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    signal_id: UUID
    target_run_id: UUID
    wait: RunWaitRef
    signal_kind: Literal["action_completed", "child_run_completed"]
    source_ref: str
    source_fact_version: str
    payload_ref: ArtifactRef
```

约束：

1. 只有受信任 completion bridge 可以创建 RunSignal；它不是 public API。
2. `signal_id` 从 source terminal fact 稳定派生。
3. payload 必须满足 `RunWaitRef.result_schema_ref`，并校验 Artifact tenant、
   schema 和 content hash；Child signal payload 是经过 delivery authorization
   的 `DelegationResult`，不是裸 `ChildRunCompletion`。
4. 相同 signal ID、相同 digest 只应用一次；不同 digest 是
   `RunSignalConflict`。
5. checkpoint 必须原子记录 applied command metadata 和已消费 signal ID。
6. RunSignal 只形成允许字段的 reducer input，不能携带任意 State patch。
7. Child 先完成、Parent 后进入 wait 时，terminal fact 保留并由
   reconciliation 延迟产生同一个 signal。
8. Child group 每应用一条 signal，必须在同一 checkpoint 中写入仅含剩余
   source 的新 `RunWaitRef`，或提交 Join route；下一条 signal 只能匹配新的
   wait，不能并发复用旧 checkpoint。
9. Child signal acceptance commit 是 result delivery 的授权时间点；该
   transaction 必须验证 `result_authorization_ref` 绑定的 policy/resource
   revision 仍有效。commit 前撤权则不接受旧 payload，并重新形成 denied
   result；commit 后重试复用已接受的 command/payload，不按新权限重写历史
   digest。

Child Run、Join、cancel、replay 和 async trigger 的行为规范见
[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)。

## 9. Knowledge Contracts

```python
class KnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    decision_id: UUID
    knowledge_request_id: UUID
    run_id: UUID
    authorization_decision_ref: str
    query: str
    knowledge_refs: tuple[str, ...]
    filter: KnowledgeFilter
    purpose: str
    budget: RetrievalBudget
    required_citation_level: str


class KnowledgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    knowledge_request_id: UUID
    result_class: Literal["ok", "empty"]
    items: tuple[KnowledgeItem, ...]
    citations: tuple[Citation, ...]
    knowledge_snapshot_ref: str
    knowledge_snapshot_version: str
    knowledge_snapshot_content_hash: str
    applied_acl_ref: str
    applied_acl_hash: str
    retrieval_policy_ref: str
    retrieval_policy_hash: str
    safe_query_trace_ref: TraceRef | None
    truncated: bool
```

只有 policy node 能从 `KnowledgeProposal` 产生 `KnowledgeRequest`，并解析
精确 Knowledge Snapshot、purpose、真实 budget、citation level 与 authorization
reference；模型提供的 Knowledge/Tenant/Principal/scope 不进入 Request。

`KnowledgeOutcome` 是 `KnowledgeResult | CanonicalFailure` 的 closed union：
成功只有 `ok/empty`；失败使用稳定的 `knowledge.denied`、`knowledge.timeout`、
`knowledge.unavailable` code。它们不能互相降级，失败时不得携带 items、citation
或 source 存在性。每个 `ok` item 至少绑定一个 Citation；Citation 固定 Snapshot、
source version、locator 和 content hash，不包含 bearer URL。空结果是合法 typed
result，不能用异常或模型臆测替代。

MVP 的每个 result 只绑定一个 Knowledge Snapshot；该 Snapshot 可以固定多个
source。未来增加 source/adapter 时仍通过新 Snapshot version 发布，不把 live
connector 列表或 `latest` alias 暴露给 Graph。ACL/retrieval policy 字段只保存
ref/hash，不暴露规则正文；Node Adapter 也不得把它们投影为模型可修改输入。

## 10. Working Context 与 Memory Contracts

`ContinuationSummary` 是 LangGraph State 中的 typed Working Memory，不是
MemoryPort message。它不使用 `ContractMeta`，但随
`graph_state_schema_version` version/hash，并接受与 module contract 相同的
`extra="forbid"`、frozen、size-limit 和 canonical serialization 约束。

```python
class ContinuationPendingRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["interrupt", "run_wait", "action", "child"]
    ref: str
    ref_hash: str
    schema_ref: str
    owner_run_id: UUID


class ContinuationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_id: UUID
    run_id: UUID
    source_checkpoint: CheckpointRef
    source_range_hash: str
    task_overview: str
    current_state: str
    important_discoveries: str
    next_steps: str
    context_to_preserve: str
    pending_refs: tuple[ContinuationPendingRef, ...]
    source_artifact_refs: tuple[ArtifactRef, ...]
    inference_request_id: UUID
    prompt_policy_version: str
    model_policy_version: str
    content_hash: str
    created_at: datetime
```

`content_hash` 覆盖除自身外的完整 canonical payload。`source_range_hash` 覆盖
本次被压缩的上一版 summary 与有序 message/reference range。每个
`ContinuationPendingRef` 只保留 typed 定位信息；真正的 interrupt、wait、
action 或 Child 状态仍从其权威记录读取。

`compress_context` node 必须把新 summary、未压缩的最近 tail、source artifact
refs 和压缩 decision 原子写入同一 checkpoint。summary 不能创建、完成或取消
pending operation，不能改变 permission、budget、goal 或 terminal fact。未知
schema、hash 不匹配、缺少成对 ToolCall/ToolResult 或待处理 reference 时 fail
fast；不得用自由文本 fallback。

### MemoryPort messages

MemoryPort 未启用时，Kernel 必须在 resolve/run-start 阶段阻断依赖它的
Skill。

```python
class MemoryRecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    recall_request_id: UUID
    run_id: UUID
    authorization_decision_ref: str
    subject_ref: str
    purpose: str
    memory_types: tuple[str, ...]
    query: str
    budget: RecallBudget
    historical_snapshot_ref: str | None


class MemoryRecallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    recall_request_id: UUID
    memories: tuple[MemoryRef, ...]
    snapshot_ref: str
    truncated: bool
```

在线执行只提交 `MemoryCandidate`，不能直接写 active Long-Term Memory。
Candidate 包含 source、confidence、consent、purpose、sensitivity、TTL 和
conflict key，由 Memory governance 决定是否激活。
Memory adapter 还必须接收独立的 trusted principal context 并在 recall/
record/forget seam 重新授权；`authorization_decision_ref` 是审计引用，不是
bearer token。

## 11. Workspace Contracts

```python
class WorkspaceAcquireCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    workspace_command_id: UUID
    run_id: UUID
    workspace_policy_ref: str
    workspace_policy_hash: str
    runtime_build_hash: str
    bootstrap_artifact_refs: tuple[ArtifactRef, ...]
    binding_hash: str
    run_mode: Literal["live", "fork_commit"]
    execution_fence: int


class WorkspaceHandleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_ref: str
    workspace_instance_id: UUID
    tenant_id: str
    run_id: UUID
    binding_hash: str
    created_at: datetime
    expires_at: datetime


class WorkspaceReleaseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    workspace_command_id: UUID
    run_id: UUID
    workspace: WorkspaceHandleRef
    reason: Literal[
        "terminal",
        "cancelled",
        "failed",
        "expired",
        "orphan_reconcile",
    ]
    execution_fence: int
```

Workspace command 只能由 Kernel lifecycle 生成，模型和 public client 不能
提供。command ID 由 tenant、run、binding 和 lifecycle transition 确定性派生，
不能随 worker attempt 改变。acquire/release 按 command ID + semantic digest
幂等；digest 排除 message/correlation/causation/trace ID 和
`execution_fence`，但包含 contract version、tenant 与全部绑定语义；fence
只作为当前写权限校验。相同 ID 的其他语义字段不同必须冲突。adapter 在产生
或释放物理 instance 前原子验证 tenant、run、binding hash 和当前
`execution_fence`，过期 worker 不得改变 workspace。

`WorkspaceHandleRef` 是定位引用，不是访问凭证；不能包含 endpoint、主机路径、
provider token 或 SDK object。每次 Tool 调用仍按当前 principal、Tool effect、
path/network policy 重新授权。`replay/fork_dry_run` 不产生 AcquireCommand。
完整生命周期和隔离规则见
[Execution Workspace](./25_Execution_Workspace.md)。

## 12. Tool Contracts

```python
class ToolResultProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str
    observed_at: datetime
    source_revision_or_watermark: str | None
    result_content_hash: str


class ToolCommand(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    decision_id: UUID
    tool_request_id: UUID
    run_id: UUID
    authorization_decision_ref: str
    tool_ref: str
    input: InputT
    timeout_policy_ref: str
    workspace: WorkspaceHandleRef | None = None
    workspace_scope: str | None = None


class ToolResult(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    tool_request_id: UUID
    output: OutputT | None
    artifact_refs: tuple[ArtifactRef, ...]
    provenance: ToolResultProvenance | None
    failure: CanonicalFailure | None
```

Execution Core 的普通 Tool node 只执行 `pure/read`，或在启用 Workspace Profile
时执行 Manifest 明确声明的 `workspace_local` 能力。任何 workspace 外的
`write/external` effect 必须转换为 `ActionProposal → ActionCommand` 并进入
`DurableActionPort`；不能用“Tool”绕过副作用治理。

只有 policy node 能从 `ToolProposal` 产生 `ToolCommand`；Tool adapter 不接受
Proposal/Payload。`timeout_policy_ref` 由 Manifest/Policy 解析，模型不能选择。
Tool adapter 还接收独立的 trusted principal context，在调用 seam 重新校验
tenant、resource、effect 和当前授权；`authorization_decision_ref` 只用于
审计，不能作为 bearer token。

每个 Tool binding 的 `tool_ref/operation/resource_type/effect_class/input_schema/
output_schema`、logical call budget、partial/selection policy、limits policy 与
adapter compatibility 都固定在内容寻址 Manifest。Core 不为某个 adapter 增加专用
Command/Result 类型，也不把“一次读取”“某种数据库隔离级别”“拒绝 partial”或
“all-or-nothing selection”设为所有 Tool 的隐式规则。

`tool_request_id` 在同一 logical node retry 中保持稳定，物理 attempt 使用独立
attempt metadata。read Tool 的成功 `ToolResult[ViewT]` 经 checkpoint 后成为该 Run
的 Run Data View；恢复复用已提交结果。是否允许第二个 logical call、失败前是否可
产生 partial、selection 如何披露和 source transaction 如何组织，必须由具体 Tool
contract 与 Graph 明示，并由 adapter/evidence 验证。资产参考 Tool 的这些固定语义
只在 [Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md)
定义。

需要 workspace 的 ToolCommand 由 Kernel 注入绑定当前 run 的 handle 和不透明
`workspace_scope`；模型不能选择。scope 由 node/subgraph/branch 的稳定 execution
key 派生，不是可拼接的主机路径。缺失、过期或跨 tenant/run 的 handle/scope
必须在调用 provider 前拒绝。跨 checkpoint/run 保留的文件只能通过
`artifact_refs` 提交，workspace 内部路径不是持久结果。
Manifest effect 为 `workspace_local` 时 handle/scope 必须同时存在；`pure/read`
时二者必须同时为空，不能借机把外部 read Tool 放进 sandbox 执行。

`ToolResult` 成功时 `failure=None`，且 `output` 或 `artifact_refs` 至少存在一个；
失败时 `failure` 非空、`output=None`、`artifact_refs` 为空且 `provenance=None`。
成功的 `read` Tool 必须携带 provenance，其中 `result_content_hash` 覆盖
canonical output 及
ArtifactRef hash；`pure/workspace_local` Tool 是否携带由 contract 明确。当前业务
状态使用该 provenance，不使用 Knowledge Citation。小结果随 checkpoint 持久化，
大结果只保存授权的内容寻址 ArtifactRef，Run Inspect 必须能还原该次 Run 实际
看到的结果。

## 13. Action Contracts

```python
class ActionProposal(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["action_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    action_ref: str
    input: InputT
    rationale_summary: str
    confidence: float


class ActionCommand(BaseModel, Generic[InputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    decision_id: UUID
    action_request_id: UUID
    run_id: UUID
    principal_ref: str
    authorization_decision_ref: str
    action_ref: str
    input: InputT
    idempotency_key: str
    approval_ref: str | None
    run_mode: Literal["live", "fork_commit"]
    execution_fence: int
    deadline: datetime | None


class ActionHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    action_request_id: UUID
    action_execution_id: str
    status: Literal[
        "accepted",
        "started",
        "waiting",
        "succeeded",
        "failed",
        "denied",
        "stale",
        "unknown",
        "manual_review",
        "cancelled",
    ]


class ActionReceipt(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    action_request_id: UUID
    action_execution_id: str
    status: Literal[
        "succeeded",
        "failed",
        "denied",
        "stale",
        "unknown",
        "manual_review",
        "cancelled",
    ]
    execution_authorization_ref: str | None
    output: OutputT | None
    external_receipt_ref: ArtifactRef | None
    failure: CanonicalFailure | None
```

`idempotency_key` 由 Execution Core 生成并端到端传递；模型不能提供。ActionReceipt
描述已发生事实，time travel 不得删除或重写。
Durable Action Runtime 对 canonical ActionCommand semantics 计算稳定 digest；
相同 request/key 不同 digest 必须冲突，不能复用已有 execution。

`authorization_decision_ref` 是 command 创建时的决策；真正写入前 Durable
Action Runtime 必须重新授权，并把当时的
`execution_authorization_ref` 固定进 Receipt。approval 不能替代该检查。
发生或尝试外部 effect 时该 reference 必填；在执行前即 cancelled 的 Receipt
可以为空，`denied/stale` 则保存相应拒绝或前置条件决策 reference。

`execution_fence` 由 Execution Driver 注入。Durable Action seam 在首次
durable acceptance transaction 中原子验证 fence；过期 worker 不能创建
`accepted` ActionRequest。acceptance 之后由 Durable Action Runtime 独立
拥有执行与恢复，Graph fence 不能替代 action cancel/compensation policy。

## 14. Failure Contract

```python
class CanonicalFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: str
    failure_class: str
    retry_owner: Literal[
        "none",
        "typed_inference",
        "execution_kernel",
        "run_coordination",
        "durable_action",
        "operator",
    ]
    retryable: bool
    safe_message: str
    detail_ref: ArtifactRef | None = None
```

`retryable=true` 不授权调用者重试；只有 `retry_owner` 可以根据固定 policy
执行。Failure 不包含 stack、credential、完整 Prompt 或 provider secret。

Tool contract 可以把 namespaced canonical failure 投影为以下通用 public error：

| public error | 通用语义 | 安全公开上限 |
|---|---|---|
| `ToolQueryTooBroad` | 无法在可信预算内形成该 contract 所要求的有效结果 | 安全 `limit_kind` 与收窄建议；无实际总量、内部 policy 或 partial result |
| `ResourceSelectionUnavailable` | selection 无法满足且不能安全区分不存在、不可见与未授权 | 统一 message/shape；无失败 ref、索引、匹配数、omitted count 或内部原因 |

public error 不覆盖 `CanonicalFailure.failure_class/retry_owner/retryable`；具体 Profile
必须固定这些字段，客户端也只能按字段与已发布 command policy 行动。Asset Risk
Reference Profile 的 canonical code 和不可重试语义见
[Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md)。

## 15. RuntimeEvent

```python
class RuntimeEvent(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    event_id: UUID
    run_id: UUID
    orchestration_id: UUID
    run_seq: int
    event_type: str
    source: str
    source_event_id: str
    payload_schema_ref: str
    payload: PayloadT
    occurred_at: datetime
```

Event 是观测事实，不用于恢复。`run_seq` 由 Runtime Event Projection
commit-order 分配；consumer 使用 `(run_id, run_seq)` 游标。
`source_event_id` 只要求在 `(tenant_id, source)` 内稳定唯一，去重键必须是
`(tenant_id, source, source_event_id)`。
持久化 projection 必须保留 `meta.contract_version/causation_id` 和
`payload_schema_ref`；未知 schema 进入 dead letter，不能用当前 payload
model 猜测反序列化。

`run_seq` 不在 Parent/Child Run 之间形成全局顺序。跨 run 只通过
`orchestration_id`、`meta.causation_id`、delegation reference 和 trace link
关联。Parent RuntimeEvent stream 可以记录 Child lifecycle 摘要，但不能复制 Child 的全部
事件并伪装成同一序列。

### 15.1 Interaction 与 UI Projection Contracts

`RuntimeEvent` 是已发生事实；`InteractionItem/UIProjectionEvent` 是由事实
生成的可重建 read model。后两者不能用于 checkpoint 恢复、Action approval、
permission decision 或 Run Signal。

```python
class ProjectionSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: Literal[
        "runtime_event",
        "interrupt",
        "action_approval",
        "run_delegation",
    ]
    source_ref: str
    source_hash: str
    source_revision: int | None
    source_seq: int | None
    source_schema_ref: str


class InteractionItem(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: UUID
    tenant_id: str
    presentation_run_id: UUID
    owner_run_id: UUID
    orchestration_id: UUID
    kind: Literal[
        "user_input",
        "permission_request",
        "business_approval",
    ]
    source: ProjectionSourceRef
    payload_schema_ref: str
    safe_payload: PayloadT
    status: Literal[
        "pending",
        "resolved",
        "expired",
        "cancelled",
        "stale",
    ]
    revision: int
    source_watermarks: tuple[ProjectionSourceRef, ...]
    created_at: datetime
    expires_at: datetime | None
    resolved_at: datetime | None


UIProjectionPayload = Annotated[
    MessageStarted
    | MessageDelta
    | MessageCompleted
    | InteractionUpserted
    | InteractionResolved
    | RunStatusChanged
    | DomainViewAccepted
    | ChildStatusChanged,
    Field(discriminator="kind"),
]


class UIProjectionEvent(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ContractMeta
    event_id: UUID
    target_kind: Literal["run", "orchestration"]
    target_ref: UUID
    projection_seq: int
    payload_schema_ref: str
    payload: PayloadT
    source_refs: tuple[ProjectionSourceRef, ...]
    projected_at: datetime
```

closed union 的最小 payload 字段固定为：

| discriminator | 必需字段 |
|---|---|
| `message_started` | `message_id`、`owner_run_id`、`role`、`content_schema_ref` |
| `message_delta` | `message_id`、`delta_seq`、typed `safe_delta` |
| `message_completed` | `message_id`、`last_delta_seq`、`content_hash`、可选 `ArtifactRef` |
| `interaction_upserted` | 完整 `InteractionItem` |
| `interaction_resolved` | `interaction_id`、`item_revision`、final status、source ref |
| `run_status_changed` | `run_id`、public status、run revision |
| `domain_view_accepted` | `run_id`、`tool_request_id`、`view_schema_ref`、`observed_at`、safe source ref、result hash、可选 Profile-safe item count |
| `child_status_changed` | `parent_run_id`、`child_run_id`、`delegation_id`、public status/revision |

同一 `message_id` 的 delta 按 `delta_seq` 去重排序，completed 必须与最终
`content_hash` 一致；cursor gap 必须先从 durable projection/source history
补齐或重新投影，不能显示 provider 原始 chunk 或猜测完成内容。

`UIProjectionEvent.payload` 的实际 type 必须属于 versioned
`UIProjectionPayload` closed union；泛型不能在 transport 上退化为任意 JSON。
每个 `(tenant_id, target_kind, target_ref)` 独立分配 commit-ordered
`projection_seq`。snapshot 与 delta 均携带 source watermarks；未知 source
schema、投影 backlog 或授权裁剪使 view 标记为 `partial/stale`。

`interaction_id` 由 source kind/ref/hash 确定性派生。Child interaction 可令
`presentation_run_id=Parent`、`owner_run_id=Child`，但 response 必须解析并验证
source 中的 exact `InterruptRef` 或 Action approval ref；projection ID、item
revision 和 safe payload 都不是执行授权。

Core 与 optional Profile 的规范 event type：

| family | event type | payload 必需 reference |
|---|---|---|
| domain view | `domain_view_accepted`、`domain_read_failed` | logical call、Tool request、view schema、checkpoint/result hash 或 safe failure；无业务正文 |
| delegation | `delegation_proposed`、`delegation_authorized`、`delegation_rejected` | node execution、decision、target Skill、authorization/failure |
| delegation | `delegation_started`、`delegation_completed` | delegation、execution mode、result/failure、usage |
| swarm | `fanout_started`、`fanout_reduced` | safe branch manifest ref/hash、width、reducer result/failure |
| goal | `goal_iteration_started`、`goal_progress_evaluated`、`goal_terminal` | goal、iteration、evidence、terminal reason |
| child run | `child_run_accepted`、`child_run_terminal`、`child_run_joined` | parent/child/delegation、terminal/result reference |
| wait | `run_waiting`、`run_signal_accepted`、`run_signal_applied` | wait ref、signal/source reference |
| context | `context_compression_decided`、`context_compressed`、`context_compression_failed` | source checkpoint/range hash、summary hash/failure；无摘要正文 |
| workspace | `workspace_acquired`、`workspace_reattached`、`workspace_release_requested`、`workspace_released`、`workspace_denied`、`workspace_unavailable`、`workspace_orphan_cleaned` | workspace ref、policy/build hash、result/failure；无路径或内容 |

每种 event type 必须有独立 `payload_schema_ref` 和大小限制。branch/node 的
高基数细节可以只进入 trace，但 reject、failure、budget exhaustion 和最终
reducer/terminal 事实不得采样。payload 不包含完整 input/output、Prompt、
chain-of-thought、credential 或 provider receipt。

## 16. State 更新协议

Result/Decision 不直接修改 LangGraph State：

```text
canonical result
  → Node Adapter validates contract/version/IDs
  → explicit reducer input
  → LangGraph State update
  → checkpoint
```

Reducer 只接收该 node 允许写入的字段。未知字段、跨 node state patch 和
framework object 必须拒绝。

## 17. Version 与兼容

独立版本：

```text
contract family version
graph state schema version
Node Adapter/converter version
SkillExecutionSpec ABI version
```

规则：

1. 新增 optional/default 字段可以保持兼容。
2. 删除、重命名、改变含义或扩大 effect 必须发布新 major version。
3. converter 是显式、单向、可 golden-test 的代码制品。
4. 历史 run 精确加载原 contract/converter，禁止 fallback 到 latest。
5. 未知 discriminator、version 或 enum fail fast。

## 18. 序列化与测试

用于 hash/signature 的 contract 采用固定 canonical JSON profile：

```text
UTF-8
UTC RFC 3339 datetime
lower-case UUID
deterministic key ordering
absent optional field 与 explicit null 不混用
SHA-256 content hash
```

实现选择一种明确标准并冻结 golden bytes；不得直接依赖 framework 默认
序列化行为作为跨版本 hash。

每个 contract family 必须有：

- 正常/边界/恶意 golden fixtures。
- `extra="forbid"` 和 size-limit tests。
- vN → vN+1 compatibility/converter tests。
- tenant/reference tampering tests。
- redaction tests。
- duplicate/retry/idempotency tests。
- delegation key/digest、Child completion、RunWaitRef/RunSignal mismatch tests。
- Workspace command digest、fence、tenant/run handle 与 lifecycle idempotency tests。
- RuntimeEvent event-type/payload-schema compatibility 和 topology golden tests。
- Continuation source range/hash、pending ref、paired call/result 和 replay tests。
- Interaction stable ID/source watermark 与 UIProjectionPayload discriminator、
  cursor/reconciliation/stale response tests。

## 19. 被否决的方案

- 一个覆盖所有 module 的 `ExecutionContext`。
- 将完整 LangGraph State 传给 PydanticAI、Memory 或 Evolution。
- 使用 `dict[str, Any]` 逃逸 contract。
- 把 Decision 当 Command 直接执行。
- 让模型或 public client 生成 delegation ID、Child Run 或 Run Signal。
- 用 public resume、RuntimeEvent 或 callback 冒充内部 completion signal。
- 使用普通 Tool 执行 workspace 外的 write/external side effect。
- 把 workspace provider client、主机路径或 credential 写入 checkpoint。
- 用 RuntimeEvent 重建 framework state。
- 用 InteractionItem/UIProjectionEvent 恢复 Graph、批准 Action 或授予权限。
- 允许自由 `name + dict` UI event 绕过 closed-union schema。
- 缺少历史 converter 时读取最新 schema 猜测恢复。
