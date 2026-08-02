# Skill Framework

> 架构集：GROVE v1.0
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> 执行 ABI：[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)

## 1. 定位

> **Skill 是可治理的业务能力资产；Graph 是执行实现；Tool 是原子依赖。**

业务分析流程属于 Skill；版本化的读取、计算或外部能力属于 Tool。一段 Prompt 或
一张 Graph 只有具备 typed contract、不可变版本、权限和评测证据后，才能作为
Skill 发布。SQL query builder、database client 和连接池只是 adapter 内部实现，
不是模型可发现的 Tool。

Skill 可以同时声明固定政策/规则 Knowledge ref 与当前业务状态的 read Tool ref。
分类依据是时间和治理语义，而不是存储品牌：发布后固定版本的是 Knowledge，Run
中读取当前值的是 Run-scoped ToolResult。具体业务绑定属于业务 Profile；一个参考
实现见 [Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md)。

Agent 不是第七类能力资产：

```text
Agent = Skill Composition + Policy
```

Agent 只是 Application 的场景配置。它不复制 Skill Definition、Graph、
Permission 或 Evaluation，也不建立独立 Capability lifecycle。

Skill Framework 包含：

- Skill Definition。
- Skill Version。
- Skill Registry。
- Skill Permission。
- Skill Evaluation。
- Skill Composition。
- Skill Runtime resolve。

它不保存 Agent graph state，也不执行 durable action。

## 2. 类型

```python
Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:_-]+$"),
]
ResourceRef = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$"),
]
CapabilityName = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$"),
]
```

`Identifier`、versioned `ResourceRef` 与 `CapabilityName` 不混用。

## 3. Skill Definition

```python
class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: Identifier
    version: ResourceRef
    display_name: str
    description: str
    input_schema_ref: ResourceRef
    output_schema_ref: ResourceRef
    graph_ref: ResourceRef
    required_capabilities: tuple[CapabilityName, ...]
    knowledge_refs: tuple[ResourceRef, ...]
    memory_policy_ref: ResourceRef | None = None
    workspace_policy_ref: ResourceRef | None = None
    tool_refs: tuple[ResourceRef, ...]
    action_refs: tuple[ResourceRef, ...]
    required_scopes: tuple[Identifier, ...]
    dependency_refs: tuple[ResourceRef, ...]
    evaluation_suite_ref: ResourceRef
    budget_policy_ref: ResourceRef
    experience_policy_ref: ResourceRef | None = None
```

所有字符串在实现时使用明确长度和格式限制。语义为 set 的 tuple 在发布前
按稳定 key 排序并拒绝重复项；这样 frozen Definition 不会因容器可变性或
无序序列化破坏 content hash。

一旦进入 `approved/active`，Version 不可原地修改。以下任一变化都产生
新 Version：

- input/output schema。
- Graph、Prompt、Model policy。
- Tool/Action binding。
- Knowledge/Memory/Workspace policy。
- dependency closure。
- Permission 或 capability。
- Evaluation suite 或 budget。

运行时禁止解析裸 `latest`。

## 4. 生命周期

```text
draft → evaluating → approved → active → deprecated → retired
                     └───────────────→ rejected
```

- `draft/evaluating` 不承接生产流量。
- `approved` 已通过 gate，但未必在 release channel 生效。
- `active` 可供新 run resolve。
- `deprecated` 允许已有 run 恢复，拒绝新 run。
- `retired` 只能在 run 和依赖方排空后删除制品。
- `rejected` 保留证据，不允许发布。

## 5. Skill Registry

Skill Registry 是 Skill 资产的权威所有者，负责：

- Definition 与不可变 Version。
- publish、approve、deprecate、retire。
- dependency reverse lookup。
- Schema/Graph/Prompt/dependency artifact hash。
- content-addressed `SkillRuntimeManifest`。
- Permission 上限和 capability 声明。
- Evaluation evidence 与 release gate。
- tenant visibility、authorization、audit。

外围 Evolution Module 只能提交 `CapabilityCandidate` 和 evidence；不能
直接写 `active` Version。

Prompt、Graph、Policy、Model 和 Schema 初期作为 Skill Version 的
versioned artifact reference，不预建多个浅 Registry。只有出现独立生命
周期、治理所有者和多个真实调用方时再拆分。

## 6. Skill Runtime

对外只暴露一个关键 interface：

```python
class SkillRuntime:
    async def resolve(
        self,
        skill_ref: SkillRef,
        context: ResolveContext,
    ) -> SkillExecutionSpec: ...
```

`SkillExecutionSpec` 是一次 run 的不可变解析结果：

```text
skill_id / skill_version
run_mode
graph binding
contract binding
SkillRuntimeManifest ref/hash
RuntimeBuildManifest ref/hash
permission binding
permission preset ref/hash
permission envelope hash
required_capabilities
budget evaluation envelope/effective ref/hash + optional input-subset attestation
typed policy refs
evaluation_subject_hash
evaluation evidence set ref/hash
skill_spec_hash
```

LangGraph Execution Kernel 只读取 spec，不在 run 中重新查询 Registry。
上述仅是字段摘要；normative schema、hash、bootstrap、动态闭集和兼容规则
见 [SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)。

resolve 顺序：

1. 精确加载 Skill Version。
2. 验证 lifecycle 和 tenant visibility。
3. 加载 publish 时生成的 content-addressed `SkillRuntimeManifest`，重新
   验证完整 dependency closure。
4. 固定 Canonical Contract、State schema、Node Adapter 和所有 artifact
   version/hash。
5. 绑定当前已批准、与 deployment profile 匹配的内容寻址
   `RuntimeBuildManifest`。
6. 从 caller 可见且已批准的 catalog 解析 permission preset；省略时使用
   Agent/Tenant 默认值，并固定 ref/version/hash。
7. 计算 effective permission。
8. 计算固定权限上限、effect、authorization policy 与 preset 的
   `permission_envelope_hash`。
9. 验证 required capability，并解析 BudgetBinding；只有 Manifest allowlist 中的
   monotonic input limit 可以按 typed comparator 单向收紧。
10. 使用 evaluated budget envelope 计算 `evaluation_subject_hash`。
11. 验证 evidence 覆盖精确行为构建和 evaluation/release gate。
12. 生成 canonical spec 和 `skill_spec_hash`。
13. 与 `agent_run` 一起持久化后才接受 start command。

任何 hash、版本或 capability 不匹配都 fail fast。

## 7. Permission 与 Capability

有效权限：

```text
effective_permissions
  = Skill Permission 上限
  ∩ dependency permissions
  ∩ Tenant Policy
  ∩ Principal grants and Resource Scopes
  ∩ Run Mode Policy
```

MVP 的授权输入只允许 versioned Operation Catalog 中的 operation 和平台支持的
typed Resource Scope；未知 operation、attribute、rule version 或 resource type
一律拒绝。Authorization Role 只是这些 operation/scope 的命名集合，不承载
脚本、任意表达式或可执行策略。

Authorization Port 先产生 `ALLOW | DENY + decision_ref`。随后 versioned
permission preset 只把“已经授权”的 operation 映射为
`AUTO | ASK | DENY`。初始只允许 `interactive/workspace_edit/read_only/
unattended`；不存在 bypass。preset 不改变 effective permission，不替代执行时
reauthorization、业务审批或 fence，且活动 run 内不可切换。normative schema 和
精确语义见 [SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)。

preset definition 是 Skill/Policy Registry 发布的 immutable Policy asset；
Application 只能选择可见 ref，不能提交规则正文、scope 或自定义 effect mapping。

capability 单独检查：

```text
required_capabilities ⊆ deployment.available_capabilities
```

Capability Profile 不是权限集合。模型输出不能扩大 permission，也不能
声明新的 capability。

`workspace_policy_ref` 存在时必须声明 `execution.workspace`；未声明 policy
时不得仅因部署提供该 capability 就隐式创建 workspace。

组合 Skill 所需 permission/capability 是完整依赖闭包的并集，但最终
permission 仍与 Tenant、Principal grants 和 Resource Scopes 求交。

## 8. Evaluation

每个可发布 `evaluation_subject_hash` 至少有：

- Golden dataset：正常、边界、空值、恶意输入。
- Contract：schema、citation、artifact 完整性。
- Quality：业务正确率、召回率或 rubric。
- Safety：越权、Prompt Injection、敏感数据泄漏、Workspace 隔离与 egress。
- Permission envelope：权限上限、effect 分类、authorization policy 和
  permission preset。
- Reliability：超时、依赖失败、replay、幂等。
- Cost/latency：token、P50/P95、Tool/Workspace/Action 预算。
- Orchestration：subgraph persistence、fan-out/reducer、GoalLoop terminal、
  RoleTemplate、child Join/cancel/HITL 和 descendant budget。
- Context/extension：Continuation policy 与固定 adapter interceptor
  chain/order/failure policy。

Evaluation 默认覆盖精确 budget envelope。只有
[ADR-0022](./adr/0022-monotonic-input-limit-tightening-reuses-ceiling-evidence.md)
定义的单调 input admission limit 可以在 effective 值调低时复用 ceiling evidence：
已接受输入的 Graph/模型/Tool 行为必须完全不变，只增加 provider 前的确定性拒绝。
effective budget 与 attestation 仍进入 `skill_spec_hash`；token、deadline、fan-out、
调用次数或结果策略变化仍需新的 Evaluation Subject。

Evidence 固定：

```text
skill_version
skill_runtime_manifest_hash
evaluation_subject_hash
evaluation_suite_version
model/prompt policy versions
dataset_snapshot_hash
thresholds
result
evidence_uri
evidence_bundle_hash
trusted_issuer
runner_attestation_ref
reviewer
evaluated_at
```

Registry publish 必须验证 evidence issuer allowlist、attestation signature、
bundle/subject hash 和 reviewer/approver 权限；不能只相信数据库中的
`decision=passed`。

在线指标可触发撤回 release channel，但不能覆盖历史 evidence。
Evaluation Suite、Evaluation Run、baseline differential、hard gate 和 staged
rollout 的权威规范见
[Skill Evaluation, Evolution and Publication](./60_Evolution_and_Publication.md)。

## 9. Composition

1. dependency 必须固定精确 Version。
2. publish 时计算 closure、permission/capability 并集和预算，并生成
   content-addressed `SkillRuntimeManifest`。
3. 循环依赖在 publish 时拒绝。
4. 子 Skill typed output 通过显式 mapping 进入父 Skill input。
5. 同进程组合优先编译为 LangGraph subgraph。
6. Sub-agent、Swarm 和 GoalLoop 分别使用 per-invocation subgraph、
   `Send` + keyed reducer 和 bounded loop；它们不是新 Agent 类型。
7. 只有需要独立 lifecycle 的 dependency 才允许 `child_run` mode，并要求
   `run.delegation` capability、显式预算和父子取消 policy。
8. 需要可靠副作用的子流程仍跨 `DurableActionPort`。
9. 父 Skill Evaluation 必须包含组合后的并发、Join、loop 和 child-mode
   回归，不只复用子 Skill 分数。
10. Agent 引用一个 root Skill Composition 和一个 Policy Bundle；它不拥有
   子 Skill 的副本。
11. 动态 route 只能选择 `SkillRuntimeManifest` closure 内已解析的 Skill。

模式、Child Run acceptance、Run Signal 和 topology 规范见
[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)。

## 10. 示例

以下仅说明通用 Definition 结构，不是可发布的业务 Profile：

```yaml
skill_id: domain-analysis-example
version: 1.0.0
input_schema_ref: DomainAnalysisInput@1
output_schema_ref: DomainAnalysisResult@1
graph_ref: domain-analysis-graph@1.0.0
required_capabilities:
  - graph
  - knowledge
knowledge_refs:
  - governed-policy-snapshot@1
tool_refs:
  - domain.record.read@1
action_refs: []
required_scopes:
  - domain-record:read
dependency_refs: []
evaluation_suite_ref: domain-analysis-eval@1
budget_policy_ref: standard-analysis@2
experience_policy_ref: governed-default@1
```

每个 Tool 的 Manifest binding 必须显式固定：

```text
tool_ref / operation / resource_type / effect_class
input_schema / output_schema
logical call budget / timeout and result budget policy
partial_result_policy / selection_policy
adapter compatibility
```

模型 payload 不包含 Tenant/Principal、可信 scope、authorization 或执行 limit；
数据库型 Tool 也不包含 SQL 与数据库对象。这些由 Manifest、可信 policy 与 adapter
注入或强制。logical call、partial、selection 和恢复语义是具体 Tool contract 的
版本化行为，不能成为 Framework 隐式默认值。Asset Risk Reference Profile 的真实 YAML、binding
与安全策略只在其 Profile 文档维护。

该 Version 不要求 DBOS。若新增 `case.create@1` 并声明
`durable_action`，没有 Durable Action Profile 的部署必须拒绝运行。

Application 可以将组合绑定成场景：

```yaml
agent_ref: business-agent@1
root_skill_ref: business-skill-suite@3
policy_bundle_ref: business-policy@7
```

该绑定是配置 provenance，不是新的 Capability Version。

## 11. 被否决的方案

- Skill 等同于 Prompt、Tool 或 Graph。
- 使用 `latest` resolve。
- Graph node 自行查找依赖。
- Evolution 直接修改 active Skill。
- 为每类 artifact 预建独立 Registry。
- 以 demo 或人工试用代替可复现 Evaluation。
- 将 Agent 注册成一份复制 Skill/Graph/Permission 的能力资产。
- permission preset 扩大 scope、切换活动 run 或提供 bypass。
