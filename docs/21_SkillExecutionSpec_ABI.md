# SkillExecutionSpec ABI

> 架构集：GROVE v1.0
> 上位文档：[Skill Framework](./20_Skill_Framework.md)
> 下游协议：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)
> Budget evidence：[ADR-0022 单调收紧输入上限](./adr/0022-monotonic-input-limit-tightening-reuses-ceiling-evidence.md)

## 1. 定位

> **`SkillExecutionSpec` 是 Execution ABI：Skill Framework 与 Execution Core
> 之间唯一、不可变、可验证的执行绑定。**

它类似 syscall contract：上游决定“允许执行什么”，Kernel 决定“如何可靠
执行”。LangGraph 不回查 Registry 猜测依赖；Skill Resolver 不操纵 graph
state。

这里的 ABI 更准确地说是“executable snapshot + Kernel entry contract”。
Application 只提交 `ExecutionIntent`，不能自行构造或修改 spec；只有受信任
的 Skill Resolver 能生成它，Execution Core 必须验证 hash、issuer 和 tenant
binding。

```text
Agent/Skill intent
  → Skill Resolver
  → immutable SkillExecutionSpec
  → Execution Core validates ABI
  → LangGraph Execution Kernel
```

## 2. ABI 不变量

1. 一个 Agent Run 绑定一个 spec。
2. spec 引用全部是精确 version + content hash，不包含 `latest`。
3. spec 通过 content-addressed `SkillRuntimeManifest` 固定完整依赖闭包，
   不在顶层展开所有 Runtime 配置。
4. spec 只声明能力和上限，不包含运行时可变 state。
5. permission 是 Skill、Tenant、Run Authority、Principal grants、Resource
   Scope 和 RunMode 的有效交集快照。
6. Kernel 启动前验证 spec hash、artifact、capability 和 input contract。
7. 已启动 run 不因 Registry、Agent alias 或 release channel 变化而漂移。
8. 缺少 ABI version、artifact、converter 或 capability 时 fail fast。
9. spec 不包含 credential、provider client、database session 或 framework
   object。
10. 动态路由不能逃出 spec 声明的 capability 闭集。
11. 外部客户端提交的 spec 一律拒绝；public Plan 只返回脱敏摘要和 hash。
12. `run_mode` 是 spec 的不可变行为输入；改变 mode 必须创建新 run/spec。

## 3. 顶层结构

```python
class MonotonicInputSubsetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit_schema_ref: str
    changed_limit_keys: tuple[str, ...]
    comparator: Literal["positive_integer_componentwise_lte"]
    resolver_attestation_hash: str


class BudgetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_envelope: VersionedRef
    effective_budget: VersionedRef
    input_subset: MonotonicInputSubsetBinding | None = None


class SkillExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    abi_version: str
    spec_id: UUID
    issuer: str
    tenant_id: str
    source_agent_ref: VersionedRef | None
    run_mode: Literal["live", "replay", "fork_dry_run", "fork_commit"]

    skill: VersionedRef
    graph: GraphBinding
    contracts: ContractBinding
    runtime_manifest: VersionedRef
    runtime_build: VersionedRef
    permission: PermissionBinding
    required_capabilities: tuple[str, ...]
    budget: BudgetBinding
    policy_refs: tuple[PolicyRef, ...]

    evaluation_subject_hash: str
    evaluation_evidence_set: VersionedRef
    skill_spec_hash: str
    resolved_at: datetime
    resolver_version: str
```

顶层只承载十类执行信息：

```text
Skill identity + SkillRuntimeManifest
Run mode
Graph binding
Contract binding
Runtime build
Permission
Capability
Budget evaluation envelope + effective binding
Behavior policy refs
Evaluation binding + hashes
```

`runtime_manifest`、`runtime_build` 与 `policy_refs` 是内容寻址引用，不是
新的展开配置区。
所有嵌套 model 同样使用 `extra="forbid"` 和 frozen model。

## 4. 基础引用与 Core Binding

```python
class VersionedRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    version: str
    content_hash: str

class GraphBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph: VersionedRef
    graph_state_schema_version: str


class ContractBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contracts: VersionedRef
    converter_bundle: VersionedRef | None
```

`graph` artifact 自身绑定 nodes、edges、reducers、Node Adapter 和 State
mapping；这些内部字段不在 ABI 顶层重复。`contracts` 固定 Canonical
Contract bundle；不兼容历史版本才需要显式 converter。

`source_agent_ref` 仅用于 provenance。Kernel 不通过它重新解析能力。

## 5. SkillRuntimeManifest

`SkillRuntimeManifest` 是 Skill Version 的内容寻址运行制品，包含 Kernel
需要加载、但 ABI 不应逐项理解的详细闭包：

```text
root Skill input/output schema
fixed dependency Skill closure
Knowledge resource bindings
Tool allowlist、operation/resource type、typed schemas、effect
（pure / read / workspace_local）与 limits policy
允许单调收紧的 input limit key/schema/comparator/failure allowlist
optional Workspace bootstrap ArtifactRef set / artifact-commit output mappings
Action allowlist, effect and typed schemas
subgraph/dependency mappings
versioned RoleTemplate refs and exact target/input/context mappings
allowed delegation mode（subgraph / child_run）and output mappings
exact Typed Inference/Tool/Workspace/Action adapter compatibility requirements
```

Manifest 不包含 tenant、actor、credential、运行状态或 `latest`。它可以被
多个 run 共享；更新任何 closure 内容都会产生新 hash。删除 Manifest 后，
上述复杂性会重新散落到 spec 和 Graph node，因此这个独立 artifact 有实际
深度，不是转发层。

Manifest 也不吸收 Permission、Budget、Prompt/Model、Knowledge/Memory/
Workspace Policy；这些仍由 Spec 的独立 typed ref 固定。禁止用自由格式
payload 把所有配置再次塞回 Manifest。

其中 monotonic input-limit 声明只固定“哪些 typed key 可用哪个 comparator 收紧”
以及 evaluated ceiling compatibility，不复制每个 Deployment/Tenant 的 effective
Budget；后者只存在于 `SkillExecutionSpec.budget`。

Kernel 在 bootstrap 时按精确 ref/version/hash 加载 Manifest，不能回查
Registry 获取当前版本。

### 5.1 RuntimeBuildManifest

`RuntimeBuildManifest` 由 trusted build/release pipeline 内容寻址发布，至少
固定：

```text
Execution Kernel / Execution Driver / fenced saver build
Python runtime and dependency lock hash
LangGraph / PostgresSaver
Pydantic / PydanticAI / provider adapter
enabled Tool / Memory / Durable Action adapter builds
optional Execution Workspace adapter / sandbox image digest
optional Run Delegation Coordinator / Completion Bridge build
approved adapter interceptor chain、固定顺序与 per-hook failure policy
OpenTelemetry SDK/exporter and semantic-convention versions
worker/container image digest
supported ABI / Contract / Graph build matrix
SBOM / build provenance / signature references
```

它不包含 credential、hostname、动态 feature flag 或业务 allowlist。
SkillRuntimeManifest 固定“运行什么业务闭包”，RuntimeBuildManifest 固定
“由哪一个可复现的软件构建执行”；两者不能合并，也不能解析 `latest` image。
Telemetry Policy 是不改变业务行为的 versioned 运维策略，因此不进入 Spec 或
Evaluation Subject；RuntimeBuildManifest 只固定其 resolver/redactor/OTel 实现。
每个 telemetry signal 记录实际 policy version，策略收紧可以对活动 Run 的后续
signal 立即生效，但不能改变 Graph、模型输入、权限或历史 signal。
Resolver/Driver 必须验证 trusted build issuer、manifest signature、image
digest 和 worker attestation；worker 自报一个匹配 hash 不构成证明。

## 6. Policy Reference

```python
class PolicyRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "prompt",
        "model",
        "inference_retry",
        "knowledge",
        "memory",
        "workspace",
        "routing",
        "context",
        "run_mode",
        "redaction",
        "experience",
    ]
    policy: VersionedRef
```

规则：

1. 一个 `kind` 是否允许多个 ref 由 ABI version 明确，不能自由追加任意 key。
2. 未知 `kind` fail fast；禁止 `extras: dict[str, Any]`。
3. Memory/Workspace/Experience 等 optional kind 只有启用对应 capability
   时才能出现。
4. Prompt、Model、route、context、Knowledge/Memory/Workspace policy 的变化
   都会影响 `evaluation_subject_hash`。
5. Tool/Action 的详细 allowlist 属于 Manifest，不扩展成顶层字段。
6. Skill 对 adapter 的兼容要求属于 SkillRuntimeManifest；实际选定的代码、
   dependency 和 image digest 属于 RuntimeBuildManifest。framework object
   和 adapter 配置对象不得进入 Spec。

MVP 的 `knowledge` policy 必须解析到一个精确 Knowledge Snapshot ref/version/
content hash 和 retrieval policy ref/hash；不得保留 alias 或 `latest`。Snapshot
属于行为输入并进入 `evaluation_subject_hash/skill_spec_hash`，但当前
Principal、ACL decision 和实际 query/result 不进入 Evaluation Subject。缺失、
hash mismatch 或 deployment adapter 不兼容时，在首个 Graph node 前 fail fast。

Live Business State 不作为 Knowledge policy 或 Snapshot 内容在执行时解析；它由
Manifest 固定的 read Tool 在 Run 中读取。Tool ref/schema/effect/policy 与 adapter
build 进入 Evaluation Subject，实际业务值、读取时刻和 `ToolResult` 不进入 Spec，
而是进入该 Run 的 checkpoint 或 ArtifactRef。

每个 read Tool 的 Manifest 必须固定 operation、resource type、effect、input/output
schema、limits、logical call budget、partial/selection policy 与 adapter
compatibility。数据库 client、SQL 和实现对象不进入 closure；它们只属于 Runtime
Build。改变任一行为语义都会改变 Evaluation Subject，实际数据值只改变本次 Run
结果。Core 不为所有 Tool 预设调用次数、刷新、partial 或 selection 规则；首个
具体约束由
[Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md) 定义。

Manifest 可以显式声明某个 limit 允许 Deployment/Tenant policy 单向收紧。Resolver
必须验证 effective limit 不超过 Manifest ceiling，并把 evaluated/effective policy
ref/hash 与 subset attestation 固定到 `SkillExecutionSpec.budget`；配置变化只影响
新 Run。合法收紧复用 ceiling Evaluation evidence，但改变 `skill_spec_hash`。未声明
可收紧的 limit、试图扩大 ceiling 或无法解析精确 policy version 时 fail fast，
Graph/Tool provider 调用数为 0。

## 7. Permission、Capability 与 Budget

MVP 只接受 versioned Operation Catalog 中的 operation。Authorization Role
是 operation 与 typed Resource Scope 的命名集合；不支持任意表达式、租户脚本、
ReBAC 图或自定义 policy DSL。未知 operation、attribute、resource type 或 policy
version 必须返回 `DENY`。未来外接策略实现也只能位于同一个 Authorization Port
之后，不能改变以下输入与决定协议：

```text
AuthorizationRequest
  = Principal + Active Tenant Context + Operation + ResourceRef
  + RunMode + AuthStrength + optional RunAuthority

AuthorizationDecision = ALLOW | DENY + DecisionRef
```

```python
class PermissionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_authority_ref: str
    run_authority_hash: str
    authorization_policy: VersionedRef
    permission_preset: VersionedRef
    permission_envelope_hash: str
    effective_scopes: tuple[str, ...]
```

`permission` 是解析时上限：

```text
Skill permission
∩ dependency permissions
∩ tenant policy
∩ Run Authority
∩ Principal grants and Resource Scopes
∩ run mode policy
```

Authorization Port 先计算上述授权交集并只返回 `ALLOW | DENY`。再由 versioned
`permission_preset` 把“已经授权”的单次
operation 映射为 `AUTO | ASK | DENY`。preset 不能增加 scope、resource、Tool、
Action 或 effect，不能跳过当前授权、reauth、Durable Action approval 或
execution fence。初始 catalog 只提供四个语义稳定的 posture：

| preset | 已授权 operation 的交互语义 |
|---|---|
| `interactive` | 按 versioned effect policy 自动执行或询问；无显式 auto 规则时为 `ASK` |
| `workspace_edit` | 仅已授权的 `workspace_local` edit 可自动执行；外部 effect 仍按 `interactive` |
| `read_only` | 仅 `pure/read` 可执行；workspace write 和 external effect 为 `DENY` |
| `unattended` | 绝不发起 permission prompt；原本需要 `ASK` 的 operation 为 `DENY` |

preset 只处理 permission interaction；业务审批与任务语义要求的用户输入仍由
Action/Interrupt policy 决定。不存在 `bypass`/`accept_all` posture。preset
在 run 创建后不可切换；改变它必须创建新 run/spec，并重新匹配 Evaluation。

所有敏感操作执行前仍需检查当前主体和资源状态。spec 不能把已撤销凭据重新
授权。

`run_authority_hash` 只覆盖影响授权语义的发起 Principal、auth strength、
delegation boundary、roles/scopes 和对应 policy version；不包含 bearer token、
session ID、credential、一次性 trace 或单纯的签发时间。实际 Worker 还必须以
自己的 Workload Principal 认证；有效权限是 Worker 能力、Run Authority 与当前
Tenant/resource policy 的交集。相同授权语义可产生稳定 hash，实际过期/撤销仍由
seam 的当前授权检查处理。

`permission_envelope_hash` 固定 Skill 与全部 dependency 的 permission ceiling、
Tool/Action effect 分类、resource scope 规则和
`authorization_policy.content_hash`、`permission_preset.content_hash`。它不包含
Tenant/Run Authority 的实际 scopes，
因此同一行为构建可以复用 Evaluation；授权策略或权限上限变化则必须重新评测。

`required_capabilities` 只表达部署能力，例如 `graph`、
`memory.long_term`、`execution.workspace`、`durable_action`、
`run.delegation`，不复制 Manifest 内容。同 Run Sub-agent/Swarm/GoalLoop
只要求 `graph`；只有允许
`DelegationCommand(mode=child_run)` 的 Skill 才要求 `run.delegation`。
存在 `workspace` policy 的 Skill 必须要求 `execution.workspace`，但 capability
本身不会隐式创建 workspace。

`budget` 是 `BudgetBinding`。`evaluation_envelope` 是完整 Evaluation 覆盖的精确
VersionedRef，`effective_budget` 是当前 Run 实际强制的精确 VersionedRef。Kernel
在启动前加载两者并验证 hard limits；LangGraph node 只消费 effective 的只读
projection。Agent/Deployment/Tenant Policy 不能扩大 evaluated envelope。

默认要求 `effective_budget.content_hash == evaluation_envelope.content_hash` 且
`input_subset=None`。只有 Manifest 明确 allowlist 的 input admission limit 才允许
不同；此时受信任 Resolver 必须按 versioned limit schema 重新计算
`positive_integer_componentwise_lte`，确认只有 allowlist key 变小、其他字段完全
相同，并生成 `MonotonicInputSubsetBinding`。该 attestation 是可验证 provenance，
不是调用者提供的授权。

`resolver_attestation_hash` 的 canonical 输入固定为 Manifest hash、limit schema、
稳定排序的 changed keys、evaluated/effective budget content hash、comparator version
和 resolver version；不包含 spec hash 或 evidence ref，避免 hash 循环。Kernel 在
bootstrap 时重新计算，不能只相信 Resolver 字段或外部声明。

这种复用只适用于“少接收一些输入、已接收输入执行路径不变”的 limit。token、cost、
deadline、loop/fan-out、Tool call count、result size，以及改变 retry、route、partial
或 selection 行为的配置默认必须 exact；若改变，发布新的 evaluation envelope 并
重新评测。MVP comparator 不提供表达式或插件，只支持正整数逐字段 `<=`。

支持动态图的 budget 至少固定：

```text
max graph steps / goal iterations / consecutive no-progress
max same-run fan-out / delegation depth / concurrent branches
max active child runs / total descendants
token / cost / wall-clock deadline
Tool / Action / delegation call counts
```

fan-out 和 Child Run acceptance 前必须在父剩余预算内预留子预算；不能给每个
分支复制完整父额度。

所有语义为 set 的 tuple 在 canonical serialization 前按稳定 key 排序，
禁止依赖 Python set/frozenset 迭代顺序。

## 8. Hash 与持久化

Evaluation 不能引用最终 `skill_spec_hash`，否则会与
`evaluation_evidence_set` 形成 hash 循环。先计算：

```text
evaluation_subject_hash =
  sha256(canonical_json(
    skill.content_hash
    + run_mode
    + graph binding
    + contract binding
    + runtime_manifest.content_hash
    + runtime_build.content_hash
    + permission.permission_envelope_hash
    + behavior-affecting policy_refs
    + budget.evaluation_envelope.content_hash
  ))
```

它排除 Tenant、Run Authority/effective scopes、evidence set、
Agent alias 和 resolve instance metadata，但包含 runtime build、permission
envelope、authorization policy、实际解析出的 Model、Prompt、Knowledge、
Memory、Workspace、route、Tool/Action closure 和 evaluated budget envelope。
经 ADR-0022 验证的 effective input subset 不进入 Evaluation Subject，但完整
`BudgetBinding` 仍进入最终 `skill_spec_hash`。

默认纳入实际 `run_mode` 以及 prompt、model、inference retry、knowledge、
memory、workspace、routing、context、run-mode policy 和 redaction policy；仅控制
离线采集、且不改变在线行为的 `experience` policy 不进入 Evaluation
Subject，但仍进入最终 `skill_spec_hash`。

RoleTemplate ref/mapping、Sub-agent persistence mode、allowed child mode、
Join/cancel propagation、GoalLoop terminal policy 和 delegation/fan-out budget
分别进入 Manifest、routing/context policy 或 evaluated budget envelope；改变任一
执行语义都必须改变 `evaluation_subject_hash`，不能把普通预算缩小伪装成 monotonic
input subset 复用旧 composition evidence。

`evaluation_evidence_set` 是内容寻址 evidence index。其每个 evidence 必须
覆盖精确 `evaluation_subject_hash`，且 decision 为 `passed`；failed 或
inconclusive evidence 只用于审计，不能进入可执行 spec。

hash 输入是 spec 的语义执行绑定，不包含实例 metadata：

```text
skill_spec_hash =
  sha256(canonical_json(
    spec without
      spec_id,
      skill_spec_hash,
      resolved_at,
      permission.run_authority_ref
  ))
```

canonical profile 由
[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)
固定，并通过 golden bytes 测试。

`permission.run_authority_hash`、effective scopes、全部 content hash
和 policy ref 仍参与 hash；`budget.effective_budget` 与 subset attestation 也参与。
因此 effective input limit 收紧必然改变 `skill_spec_hash`，而相同 resolve 输入不会
因为新 `spec_id/resolved_at` 产生不同语义 hash。

这两个 hash 的职责不同：

```text
evaluation_subject_hash = 这个行为构建是否被评测
skill_spec_hash         = 这一次运行到底绑定了什么
```

`submit` 成功前必须在同一平台 transaction 中：

1. 插入 immutable spec blob/reference。
2. 插入 `agent_run` 并记录 `spec_id/hash`。
3. 插入 tenant-scoped `start` run command。
4. 可选写入 run-created/command-accepted observation outbox；Execution
   Driver 不消费该 outbox。

LangGraph checkpoint metadata 保存 `spec_id/hash`、该次写入的
`execution_fence`，以及触发该状态变化的
`applied_command_id/seq/digest`，不复制完整 spec。fence 在 worker takeover
时可以改变，但旧 fence 不能提交新 checkpoint；command 重投递则通过 applied
metadata 判断只确认 consumed 还是实际应用。

## 9. Kernel Bootstrap

```text
Execution API receives typed run input
  → require supported abi_version
  → verify spec hash
  → verify trusted issuer / internal provenance
  → verify tenant/principal binding
  → load exact Runtime Build / Manifest / Policy / Budget / Evidence refs
  → verify all content hashes and Evaluation Subject
  → check deployment capabilities
  → reauthorize volatile permission
  → validate run input against Manifest schema
  → persist run/spec/start command

Execution Driver claims start command
  → acquire monotonically increasing execution_fence
  → repeat volatile authorization and artifact availability checks
  → load exact graph/converter
  → start LangGraph
  → workspace lifecycle node acquires exact run workspace when policy is present
```

任何 bootstrap 失败发生在 Graph 启动前，node 启动数必须为 0，并形成明确
terminal failure/command evidence。API 检查与 Driver 检查是 TOCTOU 两端，
不是两套 resolve 语义。

Workspace capability、policy、build/image 和 bootstrap artifact 的验证属于
bootstrap；缺失时 Graph node 启动数为 0。物理 acquire 是进入 Graph 后的首个
幂等 lifecycle node，并在任何 workspace Tool 前 checkpoint handle；因此
provider failure 走明确 Graph error edge，不与 bootstrap fail-fast 混淆。

## 10. 动态 Agent 图

动态不等于无约束：

1. Plan/resolve 阶段在 Manifest 中固定可用 Skill closure。
2. Sub-agent、Swarm 和 GoalLoop 分别编译为 per-invocation subgraph、
   `Send` + keyed reducer 和 bounded loop。
3. LangGraph 可以按 State 在 closure 内使用 `Command`、`Send` 和 subgraph。
4. `DelegateProposal` 只能指向 Manifest closure 内的精确 Skill Version；
   policy node 才能产生 `DelegationCommand`。
5. `execution_mode=child_run` 必须同时由 Manifest、routing policy 和
   `run.delegation` capability 允许。
6. per-thread subgraph 必须固定 stable namespace，并禁止对同一 namespace
   并行调用。
7. branch/delegation ID 和 reducer 必须在重试、乱序和 crash 后确定性一致。
8. 运行时发现新的 Capability 只能产生新的 Plan/Run，不能热修改当前 spec。
9. 动态 fan-out 受 resolved budget 的深度、并发、step、goal iteration、
   descendant-run、token、Tool/Action 限制。

这样既保留动态 Agent 图，也不牺牲权限、成本估算、恢复和 Evaluation
归因。完整模式与 Child Run 协议见
[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)。

## 11. ABI 演进

| 变更 | 处理 |
|---|---|
| 新增 optional/default 字段 | minor ABI version |
| 新增 required field | major ABI version |
| 字段语义、permission/effect 改变 | major ABI version |
| Manifest 内部 schema 演进 | 独立 Manifest version；ref 语义不变时 ABI 不变 |
| 新增 Policy kind | ABI version 明确其 cardinality、required/optional 语义 |
| Graph State 改变 | 独立 State schema + migrator |
| Canonical Contract 改变 | 独立 contract version + converter |
| 仅 adapter 内部实现改变 | ABI 可不变；Runtime Build 与 Evaluation Subject 必须改变 |

Kernel 同时支持的 ABI major 数量必须明确。历史 run 所需 major 未排空前
不能删除 reader。

## 12. Acceptance

至少验证：

1. 同一输入和固定 Registry snapshot 生成相同 canonical bytes/hash。
2. Manifest dependency 版本冲突、循环、hash mismatch 全部 fail fast。
3. 发布、弃用或移动 release channel 后，已有 run spec 不变。
4. 缺 Graph/Contract/Manifest/Prompt/Policy artifact 时 node 启动数为 0。
5. Core-only 部署拒绝要求 Memory/Workspace/Action 的 Manifest。
6. 恶意 Decision 无法选择 Manifest closure 外 Skill/Tool/Action。
7. permission 撤回后敏感操作失败，即使 spec 仍保存旧快照。
8. v1/v2 ABI 与 Manifest golden fixtures 可由指定 reader 精确解析。
9. authorization policy、permission ceiling 或 effect 分类变化后，
   `evaluation_subject_hash` 必须变化，旧 evidence 不得复用。
10. Runtime Build hash 变化后 subject hash 必须变化；没有匹配 evidence 或
    worker image 时 node 启动数为 0。
11. same-run branch 完成乱序或重投递时 reducer 结果 hash 不变；同 branch
    不同 result hash fail fast。
12. Manifest 未允许 child mode、部署缺 `run.delegation` 或父预算不足时，
    Child Run/spec/start command 创建数为 0。
13. Workspace policy/build/image 任一 hash 变化后 subject hash 必须变化，旧
    evidence 不得复用。
14. 缺 `execution.workspace`、handle 跨 tenant/run、fence 过期或 run mode 为
    replay/dry-run 时，真实 workspace Tool 调用数为 0。
15. 四种 permission preset 使用同一 golden operation matrix；任何 preset
    都不能扩大 effective scopes，`read_only` 不写，`unattended` 不发 prompt，
    `workspace_edit` 不能自动批准 external effect。
16. preset/context policy 或 adapter interceptor chain/order/failure policy
    改变后 subject hash 必须变化；活动 run 仍使用原 spec。
17. MVP Knowledge Snapshot 缺失、hash mismatch、使用 `latest` alias 或 adapter
    build 不兼容时，Graph node 与真实 retrieve 调用数均为 0；Snapshot/retrieval
    policy 变化后 subject hash 改变且旧 evidence 失效。
18. Live Business State 不能通过 Knowledge alias 在运行时解析；read Tool 的
    ref/schema/effect/policy 或 adapter build 改变后 subject hash 改变，而仅业务
    数据值改变不会重写 Spec。
19. 模型提交 Tenant/scope/limit、adapter 实现字段、extra field 或 closure 外 Tool
    ref 时，在 provider 调用前拒绝；每个 ToolResult 只能符合 Manifest 固定的
    output schema。
20. Manifest 固定的 logical call budget 在 provider 前强制；checkpoint 后恢复复用
    已提交结果，checkpoint 前重试遵循固定 node/adapter policy，不能产生超预算的
    accepted result。
21. 分别越过 Tool 的 row/byte/token/deadline 边界，结果严格符合其 versioned
    partial-result policy；改变 limits、failure mapping 或 partial policy 后 subject
    hash 改变且旧 evidence 失效。
22. selection 中混入不存在、无权、跨 Tenant、重复或竞态失效引用，结果严格符合
    versioned selection/disclosure policy，并验证 public error 不泄露 contract 禁止的
    ref、计数或原因。Asset Risk Reference Profile 的精确次数、错误码和零 Inference 断言由其
    Profile 验收维护。
23. 对 Manifest allowlist 的正整数 input limit 分别使用相等、调低、调高、零值、
    未知 key 和篡改 attestation：只有相等与合法调低可 resolve；调低保持同一
    `evaluation_subject_hash/evidence_set` 但改变 `skill_spec_hash`，其余 Graph/
    provider/run 创建数为 0。
24. 改变 token、deadline、fan-out、Tool call count、partial/selection policy 或
    comparator version 时，不得生成 monotonic subset attestation；必须产生新的
    evaluation envelope/subject，缺匹配 evidence 时 node 启动数为 0。

## 13. 被否决的方案

- 只传 `skill_id`，让 Graph node 运行时查询 Registry。
- 把 Knowledge、Memory、Tool、Action、Inference 的完整配置全部展开在
  Spec 顶层。
- 用不受控 `extras` 或任意 policy key 维持“扩展性”。
- 把 `SkillExecutionSpec` 命名为第二个 `ExecutionPlan`。
- spec 包含 LangGraph State、conversation history 或 PydanticAI Context。
- 只记录 version 不记录 content hash。
- 动态 route 可加载 closure 外的新 Skill。
- 把 Agent 的 Skill/Policy 副本写进独立 Capability Registry。
- 提供 `bypass`/`accept_all` permission preset，或让 preset 替代 reauth。
- 仅凭“数值更小”复用 Evaluation evidence，或由 Tenant/client 自报 subset proof。
