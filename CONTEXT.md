# GROVE Domain Context

## Platform

**GROVE**:
`Governed Runtime for Observable, Versioned Execution`；受治理、可观测、版本化的
智能体执行平台，将经过治理和版本化的 Skill 执行为可靠、可恢复、可审计的
Agent Run。它区分业务能力资产、执行状态、企业知识、交互记忆和外部副作用，
避免由同一个含糊的“Agent”概念承担全部职责。
_Avoid_: EAR, EAP, Agent OS, Model Gateway, Agent Builder

## Delivery

**MVP Baseline**:
首个可投入真实使用的端到端业务切片，以及支撑它安全、可靠运行所不可关闭的
基础能力集合；它是当前交付承诺，不是所有未来 capability 的缩略实现，也不预设
资产或其他业务领域。Product MVP 必须显式绑定一个精确 Business Profile。
_Avoid_: Prototype, All Profiles, Feature Demo

**MVP Foundation**:
MVP Baseline 中不可关闭的横切能力集合，包括身份与租户、可靠异步执行、契约与
版本、观测与审计、可靠交互、资源边界、评测证据和最小生产运维。
_Avoid_: Optional Profile, Shared Utilities, Production Later

**Release Track**:
从 MVP Baseline 独立增加某项产品能力的发布路径，必须声明真实启用条件、依赖、
未启用行为、验收 gate 和关闭方式；Track 之间不隐含固定实施顺序。
_Avoid_: Mandatory Phase, Backlog Theme, Feature Flag

**Core Release**:
通过通用 G0～G2、G4～G8，证明 Execution Core、Skill Governance 与公共生产能力可发布的
结论；它不绑定 Business Profile，也不能宣称某个业务产品已经可用。
_Avoid_: Product MVP Release, Framework Demo, Business Acceptance

**Product MVP Release**:
在 Core Release 之上显式绑定一个精确 Business Profile，并通过该 Profile 的 G3、
Evaluation 与 Profile-specific POC 后形成的产品发布结论；不存在默认资产业务。
_Avoid_: Core Release, Generic Demo, Default Business Profile

**Core Walking Skeleton**:
使用 non-production conformance fixture 贯通 Tenant-aware API、持久化 command、
fenced Worker、checkpoint、RuntimeEvent、Projection、SSE 与 Run Inspect 的最小可执行
Core 链；它用于尽早证明状态所有权和故障边界，不形成业务质量或 Product MVP 结论。
_Avoid_: Product MVP, Business Profile, Feature Demo

**Deployment Role**:
从同一模块化代码库和发布闭包启动、可独立部署与扩缩容的一类进程职责，例如 API、
Runtime Worker、Projection/Reconciliation 或 Governance/Evaluation；它不是领域
Module，也不自动形成网络 Service 或独立状态所有者。
_Avoid_: Module, Microservice, Capability Profile

**Implementation Acceptance Record**:
对一个精确 source、Runtime Build、migration、配置、Capability/Business Profile 和
Reference Target 的不可变发布证据索引，证明全部适用 gate 已通过并记录审批、限制与
回滚结果；它不是测试摘要、人工 demo 或泛指的完成清单。
_Avoid_: Definition of Done, Release Note, Test Screenshot

## Identity and Tenancy

**Tenant**:
数据、策略、密钥、配额和审计的最高隔离单位；任何资源、运行和授权判断都只能
属于一个 Tenant，不能隐式跨越该边界。
_Avoid_: Organization, Workspace, Account

**User**:
可通过不同 Membership 参与多个 Tenant 的人类身份；User 身份本身不授予任何
Tenant 内权限。
_Avoid_: Tenant, Actor, Principal

**Membership**:
一个 User 与一个 Tenant 之间的归属关系，是 Tenant 内角色和 scope 的承载边界；
它不能授权访问其他 Tenant。
_Avoid_: Global Role, User Permission

**Active Tenant Context**:
由认证系统建立、把一次会话和请求绑定到唯一 Tenant 的可信上下文；切换 Tenant
必须重建该上下文，业务请求中的 `tenant_id` 不能建立或改变它。
_Avoid_: Tenant Selector, Request Tenant ID

**Deployment Cell**:
承载一个或多个 Tenant 的独立部署故障域，可拥有独立 database、密钥、Worker
池和容量配额；Cell 改变物理隔离级别，但不改变平台 contract 或 Tenant 语义。
_Avoid_: Tenant, Region, Per-Tenant Code Path

**Principal**:
在 Active Tenant Context 中完成认证、可参与授权判断的主体；分为代表 User 与
Membership 的 Human Principal，以及代表服务、Worker 或 Trigger 的 Workload
Principal。
_Avoid_: User, Actor, Credential

**Actor**:
发起命令、作出审批或形成业务因果的 Principal，是审计责任归属；Actor 身份本身
不是可沿执行链传递的授权。
_Avoid_: Principal, Permission, Run Identity

**Run Authority**:
由一次已授权提交产生、供异步 Agent Run 使用的有界且可撤销委托；它不包含调用者
credential，不能超过原始 Principal、Tenant policy 或 Skill permission。
_Avoid_: User Token, Principal Snapshot, Service Permission

**Authorization Role**:
在一个 Tenant 内分配给 Membership 或 Workload Principal 的、由已知 Operation
和 Resource Scope 组成的命名集合；它不包含脚本或任意策略表达式。
_Avoid_: Role Template, Agent Role, Policy Program

**Operation Catalog**:
受版本控制的受保护操作闭集；每个 Operation 都有稳定标识和资源类型，未知操作
必须拒绝。
_Avoid_: Tool List, Free-form Permission, Runtime Route

**Effect Class**:
由平台而不是模型声明的 Operation 副作用分类；`pure` 与 `read` 不改变企业或
外部状态，`workspace_local` 只改变隔离 Workspace，`external` 必须作为 Action
经过对应能力边界。
_Avoid_: Permission, Model Annotation, Tool Name

**Resource Scope**:
限定某个 Principal 可对哪些资源执行 Operation 的强类型边界，例如所有权、部门
或数据级别；它不是任意条件字符串。
_Avoid_: Query Filter, Prompt Rule, Policy Script

**Authorization Decision**:
Authorization Port 对当前 Principal、Tenant、Operation、Resource、Run Mode、
认证强度和可选 Run Authority 求值后产生的 `ALLOW` 或 `DENY` 事实；`ASK` 属于
Permission Preset 的后续交互决定，不是授权结果。
_Avoid_: Permission Preset, Approval, Cached Grant

## Capability Assets

**Agent**:
面向 Application 的场景配置，由一个 Skill Composition 与一组固定 Policy
组成；它不是业务能力资产，也不拥有独立 Graph、Tool、Permission 或 Evaluation。
_Avoid_: Capability, Skill, Agent Runtime

**Policy Bundle**:
用于约束某个 Skill Composition 在特定场景中的路由、模型、预算、授权和
运行模式的一组固定策略引用；它不能扩大 Skill 的权限或 capability。
_Avoid_: Agent Logic, Prompt Bundle

**Skill**:
具有强类型输入输出、版本、权限、依赖和评测证据的可复用业务能力资产。
_Avoid_: Tool, Prompt, Graph

**Skill Definition**:
描述 Skill 契约、依赖、权限上限和执行入口的不可变声明。
_Avoid_: Skill 配置

**Skill Version**:
Skill Definition 的不可变发布快照，是运行、审计和回滚的引用单位。
_Avoid_: latest

**Skill Permission**:
Skill 声明的最大能力范围；实际运行权限还必须与租户策略和操作者权限求交集。
_Avoid_: Tool Permission

**Permission Preset**:
对已经授权的 operation 选择 AUTO、ASK 或 DENY 的 versioned 交互姿态；它不授予
scope，不替代当前授权、审批或 reauth，且 Agent Run 内不可变。
_Avoid_: Permission Grant, Bypass Mode

**Skill Evaluation**:
由可信评测执行者签发，证明某个精确 Evaluation Subject 满足质量、安全和
成本门槛的可复现、可验证证据集合。
_Avoid_: Demo, 人工试用

**Evaluation Subject**:
由 Skill Composition、Graph、Inference、Policy、Knowledge、Tool、可选
Execution Workspace、Action 和 evaluated budget envelope 共同形成的精确行为构建；
它包含权限上限与授权策略，但不包含某次运行的具体操作者权限快照，或经形式证明
只缩小输入域的 effective admission limit。
_Avoid_: Agent Score, Latest Skill

**Monotonic Input-Limit Tightening**:
对 Manifest 明确允许的正整数 input admission limit，Resolver 证明 effective value
逐字段不大于 evaluated ceiling，且其他行为绑定不变；它改变 `skill_spec_hash`，但可
复用 ceiling 的 Evaluation evidence。任意预算变小不自动具备该性质。
_Avoid_: Arbitrary Budget Reduction, Evidence Bypass, Runtime Clamp

**Skill Governance**:
通过 Evaluation、Approval、Publication 和 Release Channel 管理 Skill
Version 进入生产的控制面流程；它不是可选的在线 Runtime capability。
_Avoid_: Evolution, Runtime Evaluation

**Skill Composition**:
通过固定版本依赖组合多个 Skill、但不改变子 Skill 契约的业务能力。
_Avoid_: Multi-Agent

**Role Template**:
由 Skill Runtime Manifest 引用的 versioned delegation 配置，把角色说明、目标
Skill、typed input/context mapping 和默认预算编译成现有 Delegation Command；
它不是 Agent、Capability 或权限容器。
_Avoid_: Agent Class, Permission Role, Dynamic Agent

**Skill Execution Spec**:
Skill Runtime 为一次 Agent Run 解析出的不可变执行绑定，是 Skill Framework
与 Execution Core 之间的 Execution ABI，也是该次运行的 Execution Snapshot。
_Avoid_: Skill Config

**Skill Runtime Manifest**:
由 Skill Version 发布的内容寻址运行制品，固定详细依赖、Tool/Action allowlist、
可选 Workspace compatibility 和 schema mapping；它被执行 ABI 引用，但不是
Agent Run 状态。
_Avoid_: ExecutionContext, Expanded SkillExecutionSpec

**Runtime Build Manifest**:
由受信任 build pipeline 发布的内容寻址执行环境清单，固定 Execution Kernel/Driver、
LangGraph/PostgresSaver、Pydantic/PydanticAI 及启用 adapter（含可选 Workspace）
的代码、依赖和镜像摘要；它不是业务 Skill 配置或可变主机环境。
_Avoid_: latest image, Environment Config

**Tool**:
提供一次原子访问、计算或 run-scoped workspace 内有界操作的底层依赖，本身
不是可治理的业务能力资产。
_Avoid_: Skill

**Typed Domain Tool**:
以固定领域 operation、resource type 和 versioned input/output schema 暴露的
Tool；模型只能填写领域字段，不能提供 SQL、数据库对象、授权上下文或执行限制。
_Avoid_: Generic SQL Tool, Database Client, Skill

**Capability**:
运行某个 Skill 所必需的部署能力，例如 graph、knowledge、
memory.long_term、execution.workspace、durable_action 或 run.delegation。
_Avoid_: Feature Flag

**Capability Profile**:
一个 GROVE 部署实际启用的 capability 集合；缺少 Skill 必需能力时必须
fail fast。
_Avoid_: Environment Config

**Business Profile**:
基于 Execution Core 的一个端到端领域落地约束包，拥有具体 Skill、Tool binding、Graph、
业务完整性、交互和验收语义；它复用 Capability Profile，但自身不是新 Capability，
也不能把单一领域规则反向写成 Core 默认值。每个 Product release 显式选择精确
ref/hash；Core 不提供隐式业务默认值。
_Avoid_: Capability Profile, Core Module, Tenant Configuration

**Reference Business Profile**:
用于证明和示范 Core 到真实业务闭环如何绑定的具体 Business Profile；它可以被产品
显式选择，但不是平台默认业务，也不能用自己的 G3 evidence 替另一领域通过验收。
_Avoid_: Core Default, Universal Test Profile, Capability Profile

## Execution

**Platform API**:
Application 使用的产品接口集合，只路由 Plan、Execution 和 Observation
请求，不拥有这些 module 的权威状态。
_Avoid_: Agent Runtime API

**Plan API**:
用于 capability discovery、成本估算、校验和无副作用预览的接口；结果是
带版本的快照，不是执行授权。
_Avoid_: Execution Planner

**Execution API**:
用于 submit、resume、cancel、fork 和 stream Agent Run 的命令接口，执行语义
由 Execution Core 和 Execution Kernel 拥有。
_Avoid_: AgentRuntime

**Observation API**:
用于读取 event、interaction、UI delta、trace、artifact、evaluation 和
experience 投影的只读接口，不参与恢复、审批或在线演化。
_Avoid_: Event Store, Learning API

**RuntimeEvent**:
由权威状态变更提交后产生的、不可采样且带版本的持久化观测事实，可用于审计和
重建 read model，但不能替代 checkpoint、Run lifecycle 或授权状态。
_Avoid_: Trace Span, Execution State, Event Sourcing Record

**Diagnostic Telemetry**:
用于诊断和聚合趋势的 trace、metric 与 log 信号；允许有界采样或丢弃，故障时
不得阻塞或改变 Agent Run。
_Avoid_: Runtime Event, Audit Fact, Recovery Source

**Telemetry Policy**:
在平台不可突破的安全底线内，控制 sampling、retention、exporter、安全属性、
redaction 和 alert threshold 的 versioned 运维策略；它不改变 Skill 行为、
权限或权威状态。
_Avoid_: Debug Flag, Permission Policy, Skill Policy

**Diagnostic Capture Session**:
后续可选的、经审批且限时限域的敏感排障采集会话，把明确获批的字段投影写入
独立治理存储；它不是提高日志级别，也不能采集 credential 或 chain-of-thought。
_Avoid_: Verbose Logging, Full Trace, Audit Export

**Interaction Projection**:
从 safe checkpoint interrupt、Action approval、Run Delegation 和 RuntimeEvent
派生的 pending interaction read model；它可重建，不能恢复 Graph 或批准 Action。
_Avoid_: Interaction State, Approval Queue

**Interaction Inbox**:
面向人的 Interaction Projection 视图；可以把 Child interaction 展示在 Parent
上下文，但响应仍必须路由到 exact owner run 与权威 Interrupt/Approval ref。
_Avoid_: Agent Mailbox, Runtime Queue

**UI Projection Event**:
Interaction/UI read model 的 versioned closed-union delta，带 projection cursor
和 source watermark；它不是 RuntimeEvent 事实或执行命令。
_Avoid_: CustomEvent, Event Source

**Execution Kernel**:
拥有 Agent Run graph state、节点调度、checkpoint、interrupt、恢复和
time travel 语义的唯一执行内核。
_Avoid_: Graph Framework, Agent SDK

**Execution Driver**:
把已持久化的 run command 可靠交给 Execution Kernel，并保证同一 Agent Run
只有一个有效写入者；它不拥有 graph state、route 或 lifecycle。
_Avoid_: Execution Kernel, Task Queue

**Execution Workspace**:
Agent Run 可选的、隔离的短期文件与进程环境，只供已授权 Tool 使用；它不拥有
graph state，内部文件也不是 checkpoint、Memory 或持久 Artifact。
_Avoid_: Agent Runtime, Shared Workspace, Artifact Store

**Run Command**:
请求启动、恢复、取消、内部继续或传递 Run Signal 的不可变命令；重复投递
不能产生第二次语义效果。
_Avoid_: Runtime Event, Job

**Run Signal**:
把已持久化的 Action 或 Child Run 终态事实送入等待中 Agent Run 的受信任
内部消息；它不是用户输入，也不是观测事件。
_Avoid_: Callback, Runtime Event, Public Signal

**Typed Inference**:
把一次 typed model request 转换为 typed result 的有界推理调用，不拥有
Agent 控制流或持久化运行状态。
_Avoid_: Agent Runtime, Agent Loop

**Adapter Interceptor**:
固定在 Runtime Build 中、仅作用于 adapter/projector seam 的 Observer、受限
Transformer 或只会收窄/拒绝的 Guard；它不能拦截 Graph 状态和授权所有权。
_Avoid_: Agent Middleware, Dynamic Plugin

**Canonical Execution Contract**:
平台 module 之间传递的不可变、可版本化 typed message，不是运行状态或
通用 Graph IR。
_Avoid_: ExecutionContext, Canonical State

**Model Decision Payload**:
模型生成的无可信身份与授权字段的强类型建议；只有 Kernel 注入 provenance
并完成 policy 校验后，才能形成 Canonical Decision 或执行 Command。
_Avoid_: Canonical Decision, Tool Call

**Agent Run**:
一次具有独立状态、版本和审计记录的 Agent 执行。
_Avoid_: Task, Job

**Run Inspect**:
对 Agent Run、event、checkpoint 摘要、trace、citation 和 artifact 投影的授权
只读查看；它不执行 Graph，也不创建、恢复或分叉 Agent Run。
_Avoid_: Replay, Resume, Time Travel

**Sub-agent**:
在同一 Agent Run 内由父 Skill 委派的固定版本子 Skill；它没有独立
Agent Run lifecycle。
_Avoid_: Child Run, Nested Agent Runtime

**Execution Subgraph**:
单一 Skill Graph 内用于模块化实现的 LangGraph 子图；没有独立角色、Skill
委派、协作协议或产品级拓扑，因此不能仅因使用 subgraph 就称为 Sub-agent。
_Avoid_: Sub-agent, Child Run, Role

**Multi-Agent Orchestration**:
Sub-agent、Swarm、GoalLoop 和受控 Child Run 协作的统称；它不是能力资产、
Agent 类型或独立 Runtime。
_Avoid_: Multi-Agent Runtime, Agent Cluster Runtime

**Swarm**:
由 Supervisor 在一个有限参与者闭集内分派工作并归并结果的有界协作模式。
_Avoid_: Agent Cluster, Peer Runtime

**GoalLoop**:
以显式目标、进度状态、预算和终止条件驱动的有界迭代模式。
_Avoid_: Autonomous Runtime, Infinite Agent Loop

**Child Run**:
通过受控 Run Delegation 创建、拥有独立 lifecycle、checkpoint、预算和取消
语义的 Agent Run。
_Avoid_: Sub-agent, Background Task

**Run Delegation**:
连接 Parent Run 与 Child Run 的幂等创建、终态通知和 Join 关系；它不拥有
任一 Run 的 graph state。
_Avoid_: Multi-Agent Runtime, Run Lineage

**Execution Trigger**:
由固定 schedule 或受信任事件 occurrence 产生、请求创建新 Agent Run 的
幂等触发事实。
_Avoid_: Run Command, Background Job

**Run Lineage**:
由 source run 与 source checkpoint 连接的 Agent Run 关系；replay 和 fork
创建新 run，不修改或回退 source run。
_Avoid_: Mutable Branch, In-place Fork

**Replay Recording**:
一次外部 seam 调用的不可变 request-hash/result reference 记录；replay 通过
source node execution key、seam 和 ordinal 精确匹配，不能按新 run 的 request
ID 猜测或回退真实调用。
_Avoid_: Runtime Event, Provider Cache

**Action**:
可能改变企业数据或外部世界状态的强类型请求。
_Avoid_: Tool Call

**Durable Action**:
需要持久化重试、等待、调度、审批或故障恢复的 Action。
_Avoid_: Background Task

## Context

**Knowledge**:
经过受信任发布、来源、版本和权限治理，可作为企业共享参考事实使用的内容；
一次 Run 执行时读取的当前可变业务状态不属于 Knowledge。
_Avoid_: Memory, Live Business State

**Knowledge Snapshot**:
由受信任发布流程生成、固定 source/version/content hash、ACL policy 和数据分类的
不可变 Knowledge 视图，是 Agent Run 可解析的 Knowledge 版本单位。
_Avoid_: Live Index, Latest Corpus, Memory Snapshot

**Citation**:
把 Knowledge Result 中的事实绑定到精确 Knowledge Snapshot、source version、
locator 和 content hash 的引用；它不是自由文本出处或 bearer URL。
_Avoid_: Search Snippet, Link, Model Attribution

**Live Business State**:
在 Agent Run 执行期间仍可能变化、需要按当前权限从业务系统读取的领域属性、状态
或指标；它通过 Effect Class 为 `read` 的 Tool 获取，不通过 KnowledgePort 获取。
_Avoid_: Knowledge, Knowledge Snapshot, Working Memory

**Run Data View**:
由 versioned read Tool 在一次 Run 内产生、经 schema 校验并被 checkpoint 接受的
typed observation；后续 node 读取该不可变结果或 ArtifactRef，而不是保持 live
source session。其调用次数、一致性、完整性与 selection 规则由具体 Tool contract
和业务 Profile 固定，不是 Execution Core 的全局假设。
_Avoid_: Knowledge Snapshot, Shared Cache, Live Source Session

### Asset Risk Reference Business Profile

**Explicit Asset Reference Selection**:
Asset Risk Reference Business Profile 的 `AssetStateQuery@1` 唯一资产选择方式；
调用者提交非空、唯一且有界的 `asset_refs`，不能使用 filter、search、query DSL、
`all_assets`、分页或排序间接扩大 selection。Manifest 固定经过评测的
`max_asset_refs` 硬上限，
Deployment/Tenant policy 只能为新 Run 调低有效上限，不能调高；新增选择方式或提高
硬上限必须发布新的 Tool/Manifest version。
_Avoid_: Asset Filter, Search Query, Implicit All Assets

**Asset State View**:
Asset Risk Reference Business Profile 中，`asset.state.read@1` 在一个短只读
transaction 中生成、被当前 Run checkpoint 接受的唯一 `AssetStateView@1`。它是
该 Profile 的 Run Data View；后续 node 只读取该视图，刷新当前状态必须新建 Run。
_Avoid_: Knowledge Snapshot, Live Database Session, Shared Cache

**Asset State Query Too Broad**:
Asset Risk Reference Business Profile 中，`AssetStateQuery@1` 超出受信任
row/byte/token/deadline budget 的条件；投影为通用 public error
`ToolQueryTooBroad`。它不携带 partial View，缩小范围后必须创建新 Run。
_Avoid_: Truncated Success, Pagination Request, Automatic Retry

**Asset Selection Unavailable**:
Asset Risk Reference Business Profile 中，`AssetStateQuery@1` 至少一个 asset ref
不存在、不可见或无权访问的条件；投影为通用 public error
`ResourceSelectionUnavailable`。它不指出失败项，也不返回已授权子集或 omitted
count。
_Avoid_: Partial Authorization, Not-Found Oracle, Filtered Success

**Working Memory**:
单个 Agent Run 或 conversation thread 内的当前消息、推理状态和中间结果。
_Avoid_: Long-Term Memory

**Continuation Summary**:
Working Memory 中由显式 Graph node 生成的结构化长上下文摘要，绑定 source
checkpoint/range hash、pending refs 和 recent tail；它不是 Long-Term Memory
或恢复权威。
_Avoid_: Memory Item, Free-form Summary, Checkpoint Replacement

**Conversation Memory**:
从对话历史派生、可跨当前运行召回的受治理摘要或记录，不是执行 checkpoint。
_Avoid_: Chat Log

**Long-Term Memory**:
跨 conversation thread 保存的用户偏好、历史任务经验和上下文事实，带有来源、
作用域、置信度和生命周期。
_Avoid_: Knowledge

**Memory Promotion**:
把非权威 Memory 经审核和来源校验转换成 Knowledge 的显式治理动作。
_Avoid_: Auto Learn

## Evolution

**Experience Manifest**:
从一次 Agent Run 派生、带有版本和治理信息的可重建引用清单，是 Memory
与 Evolution 的离线输入。
_Avoid_: Experience Object, Event Store

**Experience Head**:
同一 source run 与 collection policy 下当前可见的 Experience Manifest
Version；历史 Manifest 保持不可变，Head 可以原子前移或撤回。
_Avoid_: Mutable Experience Manifest

**Memory Candidate**:
从交互或 Experience 中提取、尚未通过来源、权限和保留策略校验的待审记忆。
_Avoid_: Memory

**Capability Candidate**:
Evolution 提出的 Skill、Policy、Prompt、Knowledge 或 Evaluation Dataset
变更建议，尚不是可执行的已发布能力。
_Avoid_: New Version

**Capability Catalog**:
用于统一发现已发布能力的可重建视图，不拥有这些能力的版本与生命周期。
_Avoid_: Central Registry

**Evolution**:
从受治理 Experience 中产生并评测 Capability Candidate 的离线过程。
_Avoid_: Online Learning, Self-Modification
