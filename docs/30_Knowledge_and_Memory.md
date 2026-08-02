# Knowledge and Memory

> 架构集：GROVE v1.0
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> 事件与脱敏：[Observability and Operations](./12_Observability_and_Operations.md)
> 规范消息：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)
> 执行绑定：[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)
> 时间语义：[ADR-0017 Live Business State 通过只读 Tool 获取](./adr/0017-live-business-state-is-read-through-tools.md)

## 1. 核心区分

| 维度 | Knowledge | Memory |
|---|---|---|
| 语义 | 可作为企业共享事实 | 从交互产生的上下文记录 |
| 来源 | 文档、数据库、制度、已批准内容 | conversation/run/feedback |
| 作用域 | tenant/org/team/role | user/team/agent/skill |
| 权威性 | 有来源和版本 | 默认非权威，带置信度 |
| 生命周期 | Knowledge governance | TTL、遗忘、撤回、consent |
| 转换 | 正常 version update | 只能经 Memory Promotion |

流程、制度和通用操作方法属于 Skill 或 Knowledge，不应以
“procedural memory”绕过版本和评测。

## 2. MVP Knowledge Baseline

Knowledge Runtime 提供受治理、可引用的企业上下文，不理解 Agent 控制流：

```python
class KnowledgePort(Protocol):
    async def retrieve(
        self,
        request: KnowledgeRequest,
        *,
        principal: Principal,
    ) -> KnowledgeOutcome: ...
```

`KnowledgeOutcome` 是 `KnowledgeResult | CanonicalFailure`。成功 Result 的治理
事实至少包含：

```text
result_class = ok | empty
items
citations
knowledge_snapshot_ref/hash
source_versions/content_hashes
applied_acl_ref/hash
retrieval_policy_version
safe_query_trace_ref
```

MVP 只交付一个面向所选 Business Profile 的 production adapter 和一个预先治理的
Knowledge Snapshot。Snapshot 由受信任的发布步骤产生并固定：

```text
snapshot_ref/version/content_hash
source refs/versions/content hashes
tenant visibility and ACL policy ref/hash
purpose and data classification
retrieval/index build ref/hash
published_at and trusted issuer
```

Resolver 必须把精确 Snapshot 和 retrieval policy 绑定到本次
`SkillExecutionSpec`；不能解析 `latest`。Snapshot 或 policy 改变会改变行为与
Evaluation Subject。首次实现不建设通用 connector registry、crawler、动态
ingestion service、索引管理 UI 或跨 source query planner。

production adapter 的具体存储由所选 Profile 的真实 corpus 决定，但只实现一种，不同时预建
下列所有 adapter。后续 Knowledge Expansion 才可以按真实需求增加：

- RAG/vector retrieval。
- 受限只读 SQL（只查询已发布的不可变 snapshot）。
- 只读 MCP resource/tool。
- 文档、图谱或搜索。

无论采用哪种 adapter，Knowledge retrieve 都必须固定可验证的 immutable version；
读取 source 的当前可变状态仍属于 read Tool。

写型 MCP/tool 不属于 Knowledge Runtime，应分类为 Action；若需要重试、
等待、审批或长任务，则要求 Durable Action Profile。

### 2.1 Knowledge 与 Live Business State

MVP 的 Knowledge Snapshot 只承载已发布的制度、规则和文档语料。执行期间仍可能
变化的领域属性、状态和指标属于 Live Business State，由 Effect Class 为 `read`
的 Tool 在 Run 执行时获取。判断标准是时间语义，不是 source 技术：

- 固定到运行前已发布 version/hash 的内容是 Knowledge。
- 在运行中读取“现在是什么”的可变记录是 ToolResult。
- 数据库导出物可以经治理发布为 Knowledge Snapshot；直接查询当前数据库行仍是
  read Tool。

Knowledge Result 使用 Citation；read Tool 使用 `observed_at`、source ref、可用
revision/watermark 和 result content hash 作为 provenance。两者都进入 checkpoint
或授权 ArtifactRef，但不能共用 Citation 假装具有相同的时间语义。更新业务系统
当前值不会产生 Knowledge version；需要历史参考基线时显式发布新 Snapshot。

read Tool 的成功结果以 Run Data View 进入 checkpoint 或授权 ArtifactRef。Tool
ref/schema/effect、权限、预算、logical call、partial/selection policy 与 adapter
compatibility 由 Manifest 固定；模型不接触 Tenant/scope、执行 limits 或 adapter
实现对象。调用次数、一致性、完整性、刷新与 selection 不是 Knowledge Runtime 或
Execution Core 的全局规则。资产参考实现的单次读取、短一致性 transaction、拒绝 partial
与 all-or-nothing 语义只在
[Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md) 定义。

### 2.2 Request 与 outcome

Knowledge request 只允许模型提出 typed query intent；Knowledge ref、Snapshot、
Tenant、Principal、Resource Scope、purpose、timeout 和 result/token budget 由
可信 policy node 解析。模型、前端和普通 Middleware 不能提供或扩大这些字段。

Outcome 必须区分：

| outcome | 语义 | 是否可由模型补写 |
|---|---|---:|
| `ok` | 有已授权、带 Citation 的结果 | 否 |
| `empty` | 查询成功但没有匹配结果 | 否 |
| `denied` | 当前 Principal/Run Authority 无权 | 否 |
| `timeout` | 在固定 deadline 内未完成 | 否 |
| `unavailable` | Snapshot、adapter 或依赖不可用 | 否 |

`denied/timeout/unavailable` 使用 typed Canonical Failure，不能伪装为空结果；
`empty` 也不能驱动模型臆测企业事实。每个 item 必须至少有一个 Citation，Citation
固定 Snapshot、source version、locator 和 content hash，不包含 bearer URL。

## 3. Knowledge 不变量

1. 每个结果可追溯至 source/version。
2. retrieve 以认证上下文注入 tenant/principal，不接受模型自报。
3. ACL 在 retrieve seam 强制，不能只依赖上游 Graph。
4. citation 与检索 policy version 进入 checkpoint/event。
5. read-only SQL 使用 allowlist、参数绑定、statement timeout 和 row limit。
6. 无权来源不得通过 embedding/vector namespace 泄露存在性。
7. Memory 内容不进入 authoritative result，除非完成 Promotion。
8. MVP 每个 Run 只使用 Spec 固定的 Knowledge Snapshot；缺失或 hash 不匹配时在
   首个 Graph node 前 fail fast。
9. `empty/denied/timeout/unavailable` 是不同 outcome，不能互相降级。
10. query、result、citation 和 trace 遵守独立 size/token/retention/redaction
    budget；大内容通过授权 ArtifactRef，不进入 RuntimeEvent 或 telemetry。

### 3.1 Knowledge Expansion Release Track

只有出现多个真实 source、持续 ingestion 或独立数据治理 owner 后，才增加多
adapter、connector lifecycle、index rebuild、source health、增量发布和管理 UI。
扩展仍发布不可变 Knowledge Snapshot，并保持相同 KnowledgePort、Citation、ACL
和 outcome contract；不能把 live connector 状态暴露给 Graph。

## 4. Memory 模型

不建立独立 Memory Service。Memory 分两层：

```text
LangGraph Execution Kernel
  ├─ Working Memory
  │    └─ LangGraph State + Checkpointer
  └─ MemoryPort（optional）
       ├─ Conversation Memory
       └─ Long-Term Memory
```

### Working Memory

thread 内消息、摘要、计划、节点状态、interrupt、中间 artifact、检索引用
和 action handle。它只由 LangGraph Checkpointer 持久化。

Memory adapter 不复制完整 Graph State，也不参与 checkpoint 恢复。

#### Continuation Summary

长上下文压缩使用结构化 `ContinuationSummary`，它是 Working Memory 的
checkpoint state，不是 Long-Term Memory，也不通过 `MemoryPort` 读写。最小语义
字段固定为：

```text
task_overview
current_state
important_discoveries
next_steps
context_to_preserve
```

同时保存 `source_checkpoint/source_range_hash`、待处理的 typed
interrupt/wait/action/child references、原始 artifact references、生成该摘要的
inference provenance 和 `content_hash`。这些 reference 仍由各自权威模块解释；
摘要不能改变目标、权限、预算、terminal fact 或审批状态。

压缩由 LangGraph 的显式 `compress_context` node 完成：

1. 达到 spec 固定的 context budget 后触发，不由 provider 或 middleware
   临时决定。
2. 上一版摘要作为本轮输入，保留最近的原始消息 tail；ToolCall/ToolResult、
   Interrupt/Resume、RunWait/RunSignal 和 Child request/completion 不得拆对。
3. summary、tail、source references 和压缩决策在同一个 checkpoint 提交。
4. schema/hash/reference 校验失败时进入显式 failure route，不能退化为无来源
   自由文本继续执行。
5. inspect/replay 使用 checkpoint 中的原摘要；不得用当前模型重新摘要历史。

### Conversation Memory

从历史对话派生的受治理摘要或索引。它不是完整 chat log，也不是审计日志。

### Long-Term Memory

- 用户明确偏好。
- 跨任务个性化上下文。
- 历史任务的 episodic experience 和反馈。
- 尚未成为企业 Knowledge 的非权威个性化信息。

借鉴 AgentScope 的记忆分类时不扩充 GROVE enum：`user/feedback` 归入
`preference`，一次项目或任务事实归入 `conversation/episodic` 并设置 TTL，
`reference` 只保存为带来源的非权威 pointer，使用前重新读取和授权。代码模式、
架构规则、文件路径、Git 历史、调试手册和当前任务状态分别属于 Skill、
Knowledge、Workspace 或 Working Memory，不进入 Long-Term Memory。

### AgentScope memory 对照

AgentScope 当前把 memory 做成 Agent middleware family，而不是单一存储：

| AgentScope 机制 | GROVE 借鉴 | GROVE 不复制 |
|---|---|---|
| context summary：递归摘要 + recent messages | typed `ContinuationSummary` + recent tail | middleware 内隐压缩、无 source hash 文本 |
| AgenticMemory：`MEMORY.md` 索引 + topic Markdown | 分类、排除项和小索引思想；backend 仍在 MemoryPort 后 | 把代码/架构/路径当记忆，或文件成为恢复来源 |
| Mem0：static/agent/both recall/write control | policy 可选择自动 recall 或模型提出 typed recall proposal | memory tool 直接写 active、synthetic hint 持久化进 history |
| ReMe：session 级异步 retrieval/writeback | deadline 前并发预取；candidate outbox | 推理中途注入迟到结果、每轮无治理自动 writeback |

因此 GROVE 借鉴的是上下文缩小和使用体验，不采用“middleware 拥有记忆行为”这一
所有权。Graph 决定何时 recall/compress/record，MemoryPort 只实现存取 seam，
governance 决定 candidate 是否成为 active Memory。

## 5. MemoryPort

```python
class MemoryPort(Protocol):
    async def recall(
        self,
        request: MemoryRecallRequest,
        *,
        principal: Principal,
    ) -> MemoryRecallResult: ...

    async def record(
        self,
        candidate: MemoryCandidate,
        *,
        principal: Principal,
    ) -> MemoryReceipt: ...

    async def forget(
        self,
        request: ForgetMemory,
        *,
        principal: Principal,
    ) -> ForgetReceipt: ...
```

`principal` 来自受信任的 execution/auth context，不从模型 payload、Memory
request 或 LangGraph State 中自报。adapter 在 seam 再执行 tenant、subject、
scope、purpose 和当前授权检查；request 内的 authorization reference 只用于
审计，不能替代该检查。

Knowledge/Memory seam 使用的 normative message 定义见
[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)。

adapter：

- `NoMemoryAdapter`：Core 默认；optional recall 为空，required capability
  在 resolve 阶段失败，record 最终 seam 再 fail fast。
- `PostgresMemoryAdapter`：初始 production adapter，可使用 PostgreSQL/
  pgvector 或 LangGraph Postgres Store。
- deterministic fake：契约测试。

MemoryPort 是 module seam，不要求独立进程。只有出现独立扩缩容、数据驻留
或团队所有权后才评估远程部署。

recall 与 record 的控制属于 Graph，而不是 adapter 的隐藏行为：

- recall 只能由显式 `recall_context` node 发起；结果先写 checkpoint，再进入
  inference。
- optional recall 可以并发预取，但在进入 inference 前必须记录
  `included | timed_out | skipped`；开始推理后不得“赶上了就注入”。
- recall 内容以 `memory_id/version/content_hash` reference 加受限摘录进入上下文，
  不伪造成历史用户消息，也不持久化 synthetic hint。
- 自动提取只能通过 transactional outbox 提交 `MemoryCandidate`；后台任务不得
  绕过 candidate review 直接写 active Memory。
- adapter 的缓存、并发和 lifecycle 对 Kernel 隐藏；关闭或恢复时不得丢失已接受
  candidate receipt。

## 6. Memory Policy

Skill 通过 versioned policy 声明：

```text
required | optional
allowed_memory_types
read_scopes / write_scopes
namespace_template
max_recall_items / token_budget
write_mode = candidate_only | disabled
candidate_review_mode = explicit | reviewed_extraction
ttl
sensitivity_limit
purpose
```

若 policy 为 required，Skill 必须声明 `memory.long_term` capability。

## 7. Memory Item

```text
memory_id
memory_version
content_hash
tenant_id
subject_id
scope
memory_type = conversation | preference | episodic
content
provenance = run_id/checkpoint_id/message_id/experience_id
confidence
sensitivity
consent_basis
purpose
created_at / expires_at
supersedes
status = active | superseded | revoked | expired
```

约束：

1. 模型只提交 `MemoryCandidate`，不能直接写 active Memory。
2. explicit “请记住”与后台 extraction 使用不同 policy。
3. secret、高敏数据和无来源推断默认拒绝。
4. recall 按 tenant、subject、scope、Skill Permission 和 purpose 过滤。
5. 支持查看、纠正、forget 和 revoke。
6. 冲突通过新 version 与 `supersedes` 表达，不原地覆盖。

## 8. Experience 到 Memory 的路由

可选 Memory Curator 可以消费
[Experience Manifest](./50_Experience_Projection.md)，但只能产生候选：

| 内容 | 目标 |
|---|---|
| 某次任务、反馈与结果 | Episodic Memory |
| 用户明确偏好 | Profile/Preference Memory |
| 企业共享规律 | `KnowledgeCandidate` |
| 可复用流程与方法 | `SkillCandidate`/`PolicyCandidate` |

后两类不进入 active Memory。Memory 不直接驱动 capability evolution。

## 9. Memory Promotion

Memory 成为 Knowledge 必须：

1. 验证 provenance 和原始 source。
2. 检查 tenant、purpose、consent 和 sensitivity。
3. 由授权人员或明确 policy 审批。
4. 生成新的 Knowledge Version。
5. 记录 source memory refs 与 promotion evidence。
6. 后续 Memory revoke 不静默删除已发布 Knowledge，而触发独立复核流程。

## 10. Replay

Long-Term Memory 会漂移。每次实际 recall 必须把：

```text
memory_id / memory_version / content_hash
```

写入 checkpoint/event。

- `inspect` 只读取 source run 的历史 reference。
- `replay` 创建新 run，并强制使用历史 reference 或录制快照；缺失时
  `ReplayDataUnavailable`，不能读取 current。
- `fork_dry_run/fork_commit` 都创建新 run，可选 historical/current，但必须
  在创建时明确标记并进入新 spec/evaluation provenance。
- 非 `live/fork_commit` 禁止 Memory 写入。
- 已依法删除的正文不能为 replay 恢复；只保留匿名审计标识，并明确不可
  完全复现。

## 11. 租户与安全

- Knowledge namespace 与 Memory namespace 独立。
- retrieve 与 recall/record/forget 分别授权。
- tenant A 不得枚举 tenant B 的 source、memory ID 或向量命中。
- Prompt Injection 不能伪造 provenance、consent 或 promotion approval。
- trace/event/checkpoint 对 Memory 内容按 sensitivity 脱敏。
- retention/forget 必须覆盖正文、embedding、cache 和派生索引。

## 12. 被否决的方案

- 恢复独立 Memory Service 作为 GROVE 必选组件。
- Memory adapter 复制 LangGraph State。
- 把 chat log 直接当 Long-Term Memory。
- 自动把 Memory 写成企业 Knowledge。
- 把 semantic/procedural 内容都塞入 Memory。
- replay 静默读取 current Memory。
- 把 `ContinuationSummary` 写入 Long-Term Memory，或用它替代 checkpoint。
- recall 后台任务在推理中途 best-effort 注入结果。
- 将 Memory 内容伪造成 user/system message 并永久写回 thread。

## 13. 技术依据

- [LangGraph Memory](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [LangGraph Memory Concepts](https://docs.langchain.com/oss/python/concepts/memory)
- [AgentScope summary configuration](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/agent/_config.py)
- [AgentScope context summary implementation](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/agent/_agent.py)
- [AgentScope Agentic Memory middleware](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/middleware/_longterm_memory/_agentic_memory/_middleware.py)
- [AgentScope Mem0 middleware](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/middleware/_longterm_memory/_mem0/_middleware.py)
- [AgentScope ReMe middleware](https://github.com/agentscope-ai/agentscope/blob/9d1026fad17e6a985873c0981bb8d4aeacf98cf9/src/agentscope/middleware/_longterm_memory/_reme/_middleware.py)
