# Execution Core

> 架构集：GROVE v1.0
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> 对外接口：[Platform API](./05_Platform_API.md)
> 观测运维：[Observability and Operations](./12_Observability_and_Operations.md)
> 集成协议：[LangGraph + PydanticAI Integration](./15_LangGraph_PydanticAI_Integration.md)
> 编排协议：[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)
> 执行 ABI：[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)
> 可选环境：[Execution Workspace](./25_Execution_Workspace.md)
> P0 验收：[P0 Blockers and Acceptance](./90_P0_Blockers_and_Acceptance.md)

## 1. 职责

Execution Core 回答：

> **一个使用固定 Skill 版本的 Agent Run，如何安全、可调试、可恢复地完成。**

Core 包含：

- Execution API 的命令语义。
- Skill resolve 后的 `SkillExecutionSpec`。
- LangGraph Execution Kernel 与 PostgreSQL Checkpointer。
- PostgreSQL Execution Driver、run command、单写者 lease/fencing 与恢复对账。
- internal Run Signal；可选 Run Delegation Coordinator 复用同一 Driver。
- `TypedInferencePort`；PydanticAI 是 production adapter。
- KnowledgePort。
- RuntimeEvent、SSE、审计 outbox 和 trace correlation；OTel、metrics、logs、
  dashboard、alert 与运维验收由 `docs/12` 负责。

MemoryPort、ExecutionWorkspacePort 和 DurableActionPort 是可选 seam。
Experience/Evolution 是外围 consumer，不参与 Core 恢复。same-run Sub-agent、
Swarm 和 GoalLoop 属于 LangGraph route；独立 Child Run 需要可选
`run.delegation` capability，不引入第二个 Execution Kernel。

## 2. Execution API

```python
class ExecutionAPI:
    async def submit(self, request: SubmitExecution) -> AgentRunHandle: ...
    async def resume(self, request: ResumeExecution) -> AgentRunHandle: ...
    async def cancel(self, request: CancelExecution) -> CancellationResult: ...
    async def fork(self, request: ForkExecution) -> AgentRunHandle: ...
    async def stream(self, request: StreamExecution) -> AsyncIterator[RuntimeEvent]: ...
```

Execution API 是 GROVE 三类 public interface 之一，不是另一个
execution engine。API 先原子持久化 run/spec/command，再由 Execution Driver
把 command 可靠交给精确版本的 LangGraph Execution Kernel。

调用者不需要知道 LangGraph graph、checkpointer、stream mode 或内部
checkpoint metadata。业务 endpoint 只提交 public run ID 与 typed request。

Plan API 属于 Skill Framework/Resolver；Observation API 属于 read
projection。两者不加入 Execution API。完整 public contract 见
[Platform API](./05_Platform_API.md)。

## 3. 核心标识

| 标识 | 含义 |
|---|---|
| `run_id` | 一次独立执行；fork 创建新 run |
| `submission_id` | Execution API submit 的 public idempotency key |
| `command_id` | public command 或 internal continue/signal 的稳定幂等 key |
| `root_run_id` | source run 与所有 time-travel fork 的 lineage 分组 |
| `orchestration_id` | online Parent/Child 执行树分组；root run 取自身 run ID，与 `root_run_id` 语义不同 |
| `orchestration_depth` | root 为 0，Child 等于 Parent + 1 |
| `parent_run_id` | 可选直接 Parent Run；same-run subgraph 为空 |
| `parent_delegation_id` | 创建 Child Run 的确定性 delegation ID |
| `thread_id` | LangGraph 持久化 thread；一个 run 对应一个 thread |
| `checkpoint_id` | LangGraph 历史状态点 |
| `branch_id` | `root_run_id` 下的分支 |
| `skill_id/skill_version` | 固定业务能力版本 |
| `source_agent_ref` | 可选 Agent 场景配置来源；不参与 Kernel 二次解析 |
| `trigger_ref/version/hash/occurrence_id` | 可选受信任 schedule/event provenance；客户端不能自报 |
| `skill_spec_id` | 本次 run 的 immutable `SkillExecutionSpec` |
| `skill_spec_hash` | Skill 依赖闭包与策略快照 hash |
| `evaluation_subject_hash` | 已评测行为构建的稳定 hash；不含 run principal/evidence refs |
| `graph_version` | 固定图代码版本 |
| `canonical_contract_version` | Canonical Execution Contracts 版本 |
| `graph_state_schema_version` | checkpoint State schema/reducer 版本 |
| `prompt_policy_version` | Prompt/instruction 版本 |
| `model_policy_version` | provider/model/settings/fallback 版本 |
| `inference_retry_policy_version` | provider/schema retry 预算版本 |
| `typed_inference_adapter_version` | PydanticAI adapter implementation 版本 |
| `runtime_build_ref/hash` | Kernel/Driver/framework/adapter 的内容寻址构建 |
| `correlation_id` | 跨 HTTP、graph、event 和可选 action 的链路 |
| `trace_id` | 单次请求或执行尝试的观测标识 |
| `tenant_id` | 所有对象的租户隔离键 |
| `execution_fence` | 单调递增的单写者 fencing token；不是业务版本 |

## 4. Agent Run 投影

`agent_run` 用于查询和治理，不是第二份 checkpoint：

```sql
CREATE TABLE agent_run (
    run_id               UUID PRIMARY KEY,
    submission_id        UUID NOT NULL,
    submission_digest    TEXT NOT NULL,
    root_run_id          UUID NOT NULL,
    orchestration_id     UUID NOT NULL,
    orchestration_depth  INTEGER NOT NULL DEFAULT 0,
    parent_run_id        UUID,
    parent_delegation_id UUID,
    source_run_id        UUID,
    source_checkpoint_id TEXT,
    source_checkpoint_ref TEXT,
    source_checkpoint_hash TEXT,
    tenant_id            TEXT NOT NULL,
    source_agent_ref     TEXT,
    trigger_ref          TEXT,
    trigger_version      TEXT,
    trigger_hash         TEXT,
    trigger_occurrence_id TEXT,
    skill_id             TEXT NOT NULL,
    skill_version        TEXT NOT NULL,
    skill_spec_id        UUID NOT NULL,
    skill_spec_hash      TEXT NOT NULL,
    evaluation_subject_hash TEXT NOT NULL,
    thread_id            TEXT NOT NULL,
    branch_id            TEXT NOT NULL DEFAULT 'main',
    graph_version        TEXT NOT NULL,
    canonical_contract_version TEXT NOT NULL,
    graph_state_schema_version TEXT NOT NULL,
    prompt_policy_version TEXT NOT NULL,
    model_policy_version TEXT NOT NULL,
    inference_retry_policy_version TEXT NOT NULL,
    typed_inference_adapter_version TEXT NOT NULL,
    runtime_build_ref     TEXT NOT NULL,
    runtime_build_hash    TEXT NOT NULL,
    run_mode             TEXT NOT NULL DEFAULT 'live',
    status               TEXT NOT NULL,
    revision             BIGINT NOT NULL DEFAULT 0,
    execution_fence      BIGINT NOT NULL DEFAULT 0,
    lease_owner          TEXT,
    lease_until          TIMESTAMPTZ,
    latest_checkpoint_id TEXT,
    latest_applied_command_seq BIGINT,
    next_event_seq       BIGINT NOT NULL DEFAULT 1,
    correlation_id       TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ,
    CHECK (run_mode IN (
        'live', 'replay', 'fork_dry_run', 'fork_commit'
    )),
    CHECK (orchestration_depth >= 0),
    CHECK (status IN (
        'accepted', 'running', 'waiting_user_input',
        'waiting_action_result', 'waiting_child_result',
        'cancel_requested',
        'succeeded', 'failed', 'cancelled'
    )),
    CHECK (
        (source_run_id IS NULL
         AND source_checkpoint_id IS NULL
         AND source_checkpoint_ref IS NULL
         AND source_checkpoint_hash IS NULL)
        OR
        (source_run_id IS NOT NULL
         AND source_checkpoint_id IS NOT NULL
         AND source_checkpoint_ref IS NOT NULL
         AND source_checkpoint_hash IS NOT NULL)
    ),
    CHECK (
        (parent_run_id IS NULL AND parent_delegation_id IS NULL)
        OR
        (parent_run_id IS NOT NULL AND parent_delegation_id IS NOT NULL)
    ),
    CHECK (
        (trigger_ref IS NULL
         AND trigger_version IS NULL
         AND trigger_hash IS NULL
         AND trigger_occurrence_id IS NULL)
        OR
        (trigger_ref IS NOT NULL
         AND trigger_version IS NOT NULL
         AND trigger_hash IS NOT NULL
         AND trigger_occurrence_id IS NOT NULL)
    ),
    UNIQUE (run_id, tenant_id),
    UNIQUE (tenant_id, submission_id),
    UNIQUE (tenant_id, thread_id),
    UNIQUE (tenant_id, parent_delegation_id),
    UNIQUE (tenant_id, root_run_id, branch_id)
);
```

`status` 可以短暂滞后；恢复判断组合 Execution Driver 的 command/lease、
LangGraph checkpoint/interrupt 以及已启用 optional adapter 的权威状态。
表中的 Skill/Graph/Contract/Policy/adapter/runtime-build 列都是 immutable
spec 的查询投影，必须在插入时逐项校验一致，之后禁止独立 UPDATE；冲突时
以 `skill_spec_hash` 指向的原 spec 为准并 fail fast，不能挑一个“看起来新”
的列继续运行。

trigger provenance 同样在 insert 后不可修改，但不进入 Skill capability 或
Graph State。submission digest 必须覆盖精确 Trigger Definition
version/hash、occurrence 和 intent hash；只有认证的 Trigger Adapter transport
context 可以设置这些列。

公开状态是只读投影：

```text
accepted
  → running
      ├─ waiting_user_input ──resume──→ running
      ├─ waiting_action_result ──internal signal──→ running
      ├─ waiting_child_result ──internal signal──→ running
      ├─ succeeded
      ├─ failed
      └─ cancel_requested ──node boundary──→ cancelled
```

业务审批期间，LangGraph 的权威状态仍是 `waiting_action_result`；UI 可以结合
Action Projection 显示 `waiting_business_approval`，但不能把它写成第二份
run lifecycle。

Child Run 等待期间，父 LangGraph 的权威状态是 `waiting_child_result`；
`run_delegation` 只保存交接与完成投递状态，Child lifecycle 由 Child Run
自己的 checkpoint/`agent_run` 拥有。`root_run_id` 不得用于表达该父子关系。

`resume/cancel` 必须携带 `command_id/expected_revision`；`fork` 使用
`submission_id/expected_source_revision`。相同幂等 ID、相同 digest 返回原
结果；不同 digest 返回 `CommandConflict/SubmissionConflict`。revision
不匹配返回 `RunStateConflict`。并发 resume、cancel 或 fork 只有一个命令
能被接受。

## 5. PostgreSQL Execution Driver

Execution Driver 解决的是“谁在何时调用 Kernel”，不解决“Graph 下一步做
什么”。它拥有：

- start/resume/cancel、internal continue 和 trusted signal command 的可靠
  投递与去重。
- worker claim、lease、heartbeat 和 crash takeover。
- 同一 run 的单写者 fencing token。
- 非终态 run、过期 lease 和遗漏 wake 的 reconciliation。

它不拥有：

- LangGraph State、route、checkpoint 或 interrupt。
- Skill/Policy resolve。
- node retry、业务审批或 Durable Action workflow。
- RuntimeEvent 作为恢复来源。

唯一外部 interface 保持为一个 command union：

```python
class ExecutionDriver(Protocol):
    async def dispatch(
        self,
        command: StartRun | ResumeRun | CancelRun | ContinueRun | RunSignal,
    ) -> RunCommandReceipt: ...
```

production adapter 是 `PostgresExecutionDriver`，契约测试使用
`DeterministicExecutionDriver`。queue、lease、polling、NOTIFY 和
reconciliation 都隐藏在该 module 内。

最小持久化模型：

```sql
CREATE TABLE run_command (
    command_id      UUID PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    run_id          UUID NOT NULL,
    command_seq     BIGINT NOT NULL,
    command_type    TEXT NOT NULL,
    command_schema_version TEXT NOT NULL,
    command_digest  TEXT NOT NULL,
    payload_ref     TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner     TEXT,
    lease_until     TIMESTAMPTZ,
    execution_fence BIGINT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_error_ref  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (command_type IN ('start', 'resume', 'cancel', 'continue', 'signal')),
    CHECK (status IN ('pending', 'leased', 'consumed', 'dead_letter')),
    UNIQUE (tenant_id, command_id),
    UNIQUE (tenant_id, run_id, command_seq)
);
```

command acceptance 与业务数据提交顺序：

```text
submit/resume/cancel or trusted signal transaction
  → validate + authorize + applicable revision/wait CAS
  → persist immutable spec/run changes
  → insert run_command
  → optionally insert observation/audit outbox
  → commit
  → best-effort NOTIFY

worker
  → prove RuntimeBuildManifest / image attestation
  → claim only matching pending/expired command with FOR UPDATE SKIP LOCKED
  → CAS agent_run lease and increment execution_fence
  → load exact spec/graph/converter
  → invoke LangGraph until terminal/interrupt/yield
  → consume command and release lease
  → if still running after cooperative yield, atomically insert next continue command
```

`LISTEN/NOTIFY` 只减少轮询延迟；polling 和 reconciliation 才保证最终执行。
Driver 不跨模型调用持有数据库事务。
Driver 只消费 `run_command`；observation/audit outbox 只产生 RuntimeEvent，
不得形成第二条 job delivery path。

`command_seq` 使用 command acceptance 后的 `agent_run.revision`，因此同一
run 的命令有稳定总序；它与 RuntimeEvent `run_seq` 是不同序列，不能混用。
`payload_ref/hash` 指向按 command schema、sensitivity 和 retention 保存的
immutable artifact，queue row 不复制 resume input 或完整 State。

每次 command 真正改变 Graph lifecycle 时，checkpoint metadata 必须在同一
checkpoint transaction 记录：

```text
applied_command_id
applied_command_seq
applied_command_digest
```

`agent_run.latest_applied_command_seq` 只是查询投影，checkpoint metadata 是
权威判断。worker 领取 command 后先比较：

1. checkpoint 已记录相同 `command_seq/digest`：不再应用
   start/resume/cancel/signal input，只把 command 幂等标记为 consumed。
2. checkpoint 的 applied sequence 更大：该旧 command 只做一致性核对后
   consumed。
3. command 是当前最早未应用命令：才交给 Kernel；checkpoint 成功后再
   consumed。
4. 相同 sequence 不同 digest、顺序逆转或无法读取 checkpoint：fail fast
   并进入 reconciliation/manual review，不能猜测重放。

因此 crash 在“checkpoint commit → command consumed”窗口只产生重复投递，
不会把 resume input 或 cancel 语义应用两次。

resume 还必须在同一 fenced checkpoint transaction 验证
`InterruptRef` 绑定的 tenant/run/checkpoint/schema/nonce，并原子标记 nonce
已消费。`agent_run.status/latest_checkpoint_id` 只是预检投影；权威
checkpoint 不匹配时不得调用 reducer 或启动下一 node。

`continue` 只允许 Driver/reconciler 创建，不携带用户 input，也不绕过
user/action/child wait。创建时以
`(tenant_id, run_id, revision, "continue")` 确定性派生 command ID，并通过
revision CAS 分配 `command_seq`；它只从最新 checkpoint 继续一个
`status=running` 且无有效 lease/未处理 command 的 run。
正常 yield 在消费当前 command 的同一 transaction 插入下一条 continue；
reconciler 只补偿该 transaction 前后的 crash window。

`signal` 是 trusted internal command，不是 public resume。只有注册的
`ActionCompletionBridge` 或 `ChildCompletionBridge` 可以创建：

1. signal ID 由 source terminal fact 的稳定 ID/revision/hash 确定性派生。
   run command ID 再由 `(tenant_id, target_run_id, signal_id)` 确定性派生。
2. acceptance transaction 锁定目标 run，验证 tenant、`RunWaitRef`、
   source ref/schema/hash 和当前 waiting status；Child result 还必须验证
   delivery authorization decision 的 policy/resource revision 在 commit
   时仍有效。
3. 验证后递增 run revision、分配 `command_seq` 并插入 signal command；
   不接受客户端提供 expected revision。
4. 同一 run 任意时刻最多允许一个未消费 signal command；其他已完成 source
   由 bridge 按 stable source key 延迟投递。
5. 相同 signal ID、相同 digest 返回原 command；不同 digest 返回
   `RunSignalConflict`。
6. source 先于目标 wait 完成时只保存 source terminal fact；bridge 在目标
   checkpoint 进入匹配 wait 后再插入 signal command。
7. worker 应用 signal 时再次在 fenced checkpoint transaction 校验
   `RunWaitRef`，按 signal schema 产生显式 reducer input。
8. Child group 应用一条 signal 后，同一 checkpoint 必须保存只含剩余 source
   的新 `RunWaitRef`，或提交已经满足/失败的 Join route；下一条 signal 只能
   匹配新 wait。
9. signal 不消费 public `InterruptRef`，也不能携带任意 State patch；目标
   已 terminal 或不再等待 source 时只审计并关闭 coordination relation，
   不能重开 run。

Child result 以 signal acceptance transaction commit 作为交付授权时间点。
若 authorization revision 在 commit 前变化，不得插入旧 payload，bridge
必须重新求值并形成当前允许的 succeeded/denied `DelegationResult`；commit
后重试必须返回已接受的原 command/payload，不能按新权限重写为不同 digest。
后续撤权不改写历史 checkpoint，但 Artifact/Observation 读取仍各自重新授权。

Run Signal 的 typed contract 见
[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)。

接受 `cancel` 时必须在同一 transaction 把 run 标记为
`cancel_requested`、递增 revision，并递增 `execution_fence`、清除当前
lease，从而立即撤销旧 invocation 的写资格。Driver 领取 cancel command 时
取得后续新 fence，并在不再调度新 node 的前提下完成 Kernel cancellation。
已经发出的模型/只读调用可以返回，但结果必须因旧 fence 被丢弃；已由 Durable
Action Runtime 接管的外部事实不自动撤销，按 Action cancel/compensation
policy 处理。已经 accepted 的 Child Run 也不会被删除；Run Delegation
Coordinator 按固定 propagation policy 向 attached child 提交普通 cancel
command。

所有可能提交权威事实的 seam 必须验证当前 fence：

- LangGraph checkpointer write。
- `agent_run` revision/status 更新。
- RuntimeEvent source outbox。
- MemoryCandidate record。
- Action 的首次 durable acceptance。
- Child Run 的首次 delegation acceptance。

Action request 一旦以当前 fence 原子提交为 `accepted`，执行所有权即转移给
Durable Action Runtime；后续 workflow 恢复不再用 Graph fence 决定是否执行，
而使用固定 execution ID、执行时授权和 action cancel/compensation policy。

Child Run 一旦以当前 Parent fence 原子提交为 `accepted`，它就取得独立
run/spec/thread/fence；后续恢复只看 Child 自己的 Execution Driver 状态。
Parent fence 不能撤销 Child 已提交 checkpoint，只能按固定 propagation
policy 向 Child 提交普通 cancel command。

production 使用 `FencedPostgresSaver` adapter：每次 checkpoint transaction
先验证 `(tenant_id, run_id, execution_fence)` 仍匹配 `agent_run`，再调用选定
LangGraph PostgresSaver 的精确版本实现。不能只把 fence 放进 metadata 后由
上游自觉检查。

fence check 与 checkpoint insert/update 必须在同一数据库 transaction 和
connection 中完成，或由数据库 trigger/guard function 原子拒绝旧 token。
“Python 先 SELECT、随后由 PostgresSaver 另开 transaction 写入”仍有 TOCTOU，
不满足本协议。若固定版本的 PostgresSaver 没有可验证的 transaction seam，
必须维护精确版本的自有 fenced saver adapter；不能降级为非原子检查。

Driver 只能把 command 分配给通过 build/image attestation、匹配
`runtime_build_hash` 的 worker。
升级时保留旧 worker image/pool 直到相关 run 排空或迁移；没有匹配 worker 时
run 保持可诊断的 `VersionUnavailable`，禁止交给“兼容大概相同”的最新 image。

旧 worker 在 lease 过期后即使仍返回模型结果，也只能丢弃结果并记录
`StaleExecutionFence`，不能 checkpoint 或产生副作用。模型请求本身不保证
exactly-once，但重复次数受 node/run budget 约束。

reconciler 至少检查：

1. 过期 `leased` command 若已等于或早于 checkpoint applied metadata 则标记
   consumed；否则回到可领取状态。
2. `status=running` 的 run 没有有效 lease 或未处理 command 时，按
   `(run_id, revision)` 幂等补一个 internal `continue` command。
3. terminal/user-interrupt run 不被自动重启。
4. `waiting_action_result` 只由匹配 Action terminal fact 的 internal
   `signal` command 恢复。
5. `waiting_child_result` 只由匹配 Child terminal fact 和 `RunWaitRef` 的
   internal `signal` command 恢复；notification 丢失时从 `run_delegation`
   与 Child `agent_run` 重建同一个 signal。
6. public resume 不能伪造 signal，internal signal 不能消费用户
   `InterruptRef`。
7. dead-letter command 形成告警和可审计的人工处置入口，不直接改 checkpoint。

## 6. Execution Kernel：LangGraph

LangGraph 是 GROVE 唯一 Execution Kernel，独占：

- Agent Run lifecycle。
- graph state transition、route、loop 和 parallel scheduling。
- node retry policy。
- checkpoint、interrupt、resume、replay 和 time travel。
- Tool/Knowledge/Memory/Action/Delegation node 的调用编排。

Kernel 接收不可变 `SkillExecutionSpec`，不在 run 中查询最新版。下图描述完整
Capability 空间，不代表首个 MVP 全部启用：

```text
START
  → authorize
  → recall_context
  → context_budget_route
      ├─ continue
      └─ compress_context
  → typed_inference
  → policy_route
      ├─ read_knowledge
      ├─ execute_subgraph
      ├─ delegate_child_run       [optional]
      ├─ propose_action
      ├─ wait_user_input
      └─ finalize
```

任意 Skill Graph 都可以通过同一 typed Tool seam 读取 Live Business State，并把
成功 `ToolResult[ViewT]` 作为 Run Data View checkpoint。Kernel 只保证：

- Tool ref、schema、Effect Class、权限、budget 与 adapter compatibility 来自已
  resolve 的 Manifest closure，而不是模型或客户端临时字段。
- node retry 使用稳定 logical call identity；已 checkpoint 的结果在恢复时复用，
  不因 worker takeover 隐式重做外部读取。
- Graph 只能沿已发布 topology 决定是否再次调用 Tool；客户端没有任意重执行命令。
- typed failure 与 success/Artifact 互斥，是否接受 partial、调用次数、source
  transaction 和 selection disclosure 都由具体 Tool contract/Profile 固定。

Core 不持有数据库 session，也不规定所有 read Tool 都只能调用一次、必须使用某种
隔离级别或拒绝 partial。首个固定单次读取的业务 Graph 只在
[Asset Risk Reference Business Profile](./31_Asset_Risk_Reference_Profile.md) 定义，不能被复制为 Kernel
全局分支。

动态图使用 LangGraph 原生 `Command`、`Send`、reducer 和 subgraph：

- Sub-agent 默认编译成 per-invocation subgraph。
- Swarm 编译成有限 Supervisor + `Send` + keyed reducer。
- GoalLoop 编译成带显式终止 edge 的 bounded loop。
- Kernel 对每次 subgraph/Child 调用计算统一 `delegation_depth=current+1`；
  它与只计算 Child Run 边数的 `orchestration_depth` 不同。

动态 fan-out 必须通过 typed validation，并受节点数、委派深度、并发、
descendant-run、循环、token、cost 和 deadline 限制。同一 per-thread
subgraph namespace 不得并行调用；并行 worker 使用隔离的 per-invocation
namespace。

任意需要可靠副作用的操作只能经 `DurableActionPort`。未声明
`durable_action` capability 的 Skill 不能进入 action node。

动态 route 只能在 spec 引用的 `SkillRuntimeManifest` 所固定的
Skill/Tool/Action closure 内发生。运行时发现 closure 外 Capability 时必须
形成新的 Plan/Run，不能热修改当前 spec。

需独立 lifecycle 的委派由 policy node 形成 `DelegationCommand(mode=child_run)`
并交给可选 Run Delegation Coordinator。Child Run 仍通过同一 Driver 和
LangGraph Kernel；Core 不引入 nested Agent runtime。完整的 Join、取消、
trigger 和 topology 语义见
[Multi-Agent Orchestration](./17_Multi_Agent_Orchestration.md)。

### 6.1 Continuation context

`compress_context` 是 versioned Graph node，不是 inference adapter、Memory
adapter 或 middleware hook。触发阈值、最近消息 tail 和摘要 token budget 由
immutable spec policy 固定；node 输入包含上一版 `ContinuationSummary`、本轮
有序消息、实际 recall references 和 pending operation references。

node 输出必须先通过 typed schema、reference 和 hash 校验，再与最近 tail
原子 checkpoint；之后 inference 只消费该 checkpoint 中的 summary + tail +
references。ToolCall/ToolResult、Interrupt/Resume、RunWait/RunSignal 和 Child
request/completion 保持原子配对。压缩失败进入 Graph 的显式 failure/retry
route，不能绕过 checkpoint 直接用临时摘要继续。

恢复和 replay 加载历史 `ContinuationSummary`；只有 live run 到达下一个固定
触发点才能生成新版本。摘要仅减少模型上下文，不删除审计事件、artifact 或
checkpoint history，也不替代 Long-Term Memory。

## 7. Graph Version

`graph_version` 不可变并绑定：

- 节点/edge/reducer 代码制品。
- State schema 与 Node Adapter。
- checkpoint migration version。

Skill、Manifest、Runtime Build、Contract、Prompt/Model policy 和 Budget 不并入
`graph_version`；它们由 `SkillExecutionSpec` 以独立 content hash 共同绑定。
这样更新 Knowledge/Prompt 不必伪造一个“Graph 变更”，历史 run 仍由完整
spec 精确恢复。

恢复必须精确路由：

```python
graph = graph_registry.require(run.graph_version)
await graph.ainvoke(resume_command, config=checkpoint_config)
```

这里的 `graph_registry` 是按 content hash 加载已发布 Graph build 的内部查找
实现，权威 artifact 仍属于 Skill Registry；它不是独立平台服务或可变 Catalog。

禁止找不到版本时 fallback 到 `latest`。

升级规则：

1. 新 State 字段先设为可选或提供默认值。
2. 重命名采用新增、兼容、排空旧 thread、删除。
3. 不兼容变更使用显式 migrator 创建新 run/checkpoint。
4. 旧实现保留至 active/interrupted thread 排空或迁移。
5. 缺少旧版本时 fail fast。

### 7.1 执行制品保留

不可变 spec 只有在其引用仍可加载时才有意义。Artifact retention 使用从以下
root 开始的 mark-and-sweep：

```text
non-terminal Agent Run specs
pending/leased/dead-letter run commands and their payload artifacts
accepted/unjoined Run Delegation、Child completion 与 signal artifacts
RuntimeEvent/topology branch manifests inside observation retention
active/deprecated release channels
pending Action requests/workflows
run/checkpoint inside declared inspect/replay retention window
ReplayRecordingRef and result artifacts inside that window
legal/audit retention holds
```

被 root 引用的 Graph、State schema、Canonical Contract、converter、
Manifest、Policy、Budget、Evaluation evidence、adapter build/container
全部被 pin。`retired` 只能阻止新 run，不能删除仍被 pin 的制品。
adapter/framework/image 的精确闭包由 spec 引用的 `RuntimeBuildManifest`
给出，而不是从当前部署反推。

清理必须先生成引用报告并经过 grace period；删除后不得 fallback 到 latest。
Observation API 必须公开 `inspect_available/replay_available` 及缺失的 reference
类别；超过声明窗口后允许不可 replay，但不能让用户在启动 replay 后才静默
调用真实 seam。
外部模型供应商可能撤下固定 model，即使本地版本完整也无法继续真实推理；
此时 live resume 返回 `VersionUnavailable`，只能显式迁移为新 run。历史
replay 仍可使用已录制 canonical result。

## 8. Interrupt

| 等待状态 | 所有者 | 恢复者 |
|---|---|---|
| `waiting_user_input` | LangGraph | 通过授权的用户 |
| `waiting_action_result` | LangGraph | `ActionCompletionBridge` 产生的 internal Run Signal |
| `waiting_child_result` | LangGraph | `ChildCompletionBridge` 产生的 internal Run Signal |
| `waiting_business_approval` | Durable Action Runtime | 经过授权的审批人 |

LangGraph interrupt 只用于 Agent 图语义。节点恢复可能从开头重入，因此
interrupt 前不能执行非幂等写操作。

公开 resume endpoint 不接受 `waiting_action_result/waiting_child_result`。
内部 bridge 必须校验 tenant、target run、`RunWaitRef`、source terminal
fact 和 schema/hash 全部匹配后才能幂等 signal。等待源提前完成时保留终态，
待匹配 wait checkpoint 出现后再投递，不能丢失或强行重开父 run。

## 9. Time Travel

| `run_mode` | 状态 | 推理 | Memory 写入 | 副作用 |
|---|---|---|---|---|
| `live` | 正常 | 真实或策略指定 | 允许，受策略约束 | 允许，受权限/HITL 约束 |
| `inspect` | source run 只读查询，不创建执行 run | 不调用 | 禁止 | 禁止 |
| `replay` | 从 source checkpoint 创建新 run | 强制复用录制结果 | 禁止 | 禁止 |
| `fork_dry_run` | 从 source checkpoint 创建新 run | 可重新推理 | 禁止 | 禁止 |
| `fork_commit` | 从 source checkpoint 创建新授权 run | 可重新推理 | 允许 | 允许 |

最终 seam 必须强制：

```python
if run_mode not in {"live", "fork_commit"}:
    raise SideEffectForbidden(run_mode)
```

run mode 创建后不可修改。要提交 dry-run 的结果，调用者必须从选定的
dry-run checkpoint 再创建一个 `fork_commit` run；它从 source binding
生成新 spec，重新授权并校验预算和当前资源条件，获得新的
run/thread/submission/spec/fence。Agent 不能自行创建或切换可写 run。

新 run 的行为绑定以 source spec 为锚，不解析当前 alias 或 `latest`。
`replay` 固定 source Graph/Contract/SkillRuntimeManifest/
RuntimeBuildManifest，以及 Model、Prompt、retry、Knowledge、Memory、
routing、redaction policy，并使用 historical snapshot；fork 同样固定这些
checkpoint-compatible binding，同时重新计算当前
authorization/run-mode policy、tenant/actor permission 和预算交集。派生的
行为 hash 必须有匹配 evidence，`fork_commit` 还必须通过当前 publication
gate。

当前 `ForkExecution` 不支持切换 Graph、State schema、Runtime Build 或其他
行为 build。
这类需求必须由未来单独定义、带显式目标与已发布 checkpoint migrator 的
migration command 完成；在该协议落地前一律 fail fast，不能把新 alias 直接
套到历史 State。

LangGraph 原生 replay 会重新执行 checkpoint 之后的 node，因此 GROVE replay
必须把 Inference、Knowledge、Tool、Memory、Action 和独立 Run Delegation
seam 全部切换为 recorded-result adapter。任一必需录制结果缺失时返回
`ReplayDataUnavailable`，禁止退回真实模型、当前 Memory、外部 API 或 Action。

每次 source seam 调用按
`(source node_execution_key, seam_kind, logical_call_ordinal)` 保存 immutable
`ReplayRecordingRef`。replay 先定位 recording，再比较 typed request 的
semantic hash、schema、source snapshot 和 result hash；任一错配返回
`ReplayDataMismatch`。新 run 的 request/message ID 不参与 lookup。

`replay/fork_dry_run` 不接受真实 Child Run；录制的 Child completion 以
`DelegationResult` 进入新 run。`fork_commit` 若重新委派，必须使用新的
`orchestration_id/delegation_id/child submission_id` 并重新授权。time-travel
source lineage 与 live Parent/Child orchestration 不能共用 ID。

不可逆副作用不能被 time travel 撤销。UI 必须显示 checkpoint、branch、
历史 durable action receipts 和当前 side-effect mode。

## 10. Typed Inference Layer

LangGraph inference node 只依赖小 interface：

```python
ResultT = TypeVar("ResultT", bound=BaseModel)


class TypedInferencePort(Protocol):
    async def infer(
        self,
        request: CanonicalInferenceRequest,
        *,
        result_type: type[ResultT],
    ) -> CanonicalInferenceResult[ResultT]: ...
```

PydanticAI 是 production adapter；测试使用 deterministic fake。它只负责：

- typed model input。
- 一次 logical inference 内的 model/provider interaction。
- structured output parsing/validation。
- 有界 output/schema 和 provider retry。
- usage、model response 和 validation error 标准化。

PydanticAI 明确不负责：

- Agent Run lifecycle 或 graph state。
- route、loop、parallelism、sub-agent delegation。
- Tool/Knowledge/Memory/Action 调用。
- checkpoint、interrupt、resume 或 time travel。
- Graph node retry/recovery、durability 或业务审批。

PydanticAI adapter 不向外暴露 `Agent` 对象。即使内部使用 PydanticAI
`Agent` 封装模型调用，也必须禁用 executable function tools、toolsets、
MCP、durable integration 和跨调用状态。

模型 typed result 使用无可信 metadata 的判别联合：

```python
InferenceDecisionPayload = (
    FinalAnswerPayload
    | KnowledgeProposalPayload
    | ToolProposalPayload
    | ActionProposalPayload
    | DelegateProposalPayload
)
```

Node Adapter 先把无可信 metadata 的 `InferenceDecisionPayload` enrichment
为上述 Canonical Decision；LangGraph policy node 再把通过校验的建议转换成
`KnowledgeRequest`、`ToolCommand` 或 `ActionCommand`。只读访问也由
LangGraph 显式 Knowledge/Tool node 执行。

PydanticAI structured output 可能用 provider tool-calling 传输 output
schema；这种纯输出编码不是 GROVE business Tool。允许 output transport，
禁止 executable function tools、Tool Registry、MCP 和有副作用的 output
function。

Tool/Action metadata 至少包含：

```text
effect = pure | read | write | external
required_scopes
timeout_policy
replay_policy
schema_version
```

这些 metadata 属于 Skill/Tool/Action Registry 和 LangGraph policy node，
不注入 PydanticAI Tool Registry。

inference adapter 不持有 business Tool 或写凭据。schema/provider retry
归 Typed Inference Layer，node recovery 和 error route 归 LangGraph；
同一种 failure 不得在两层盲目重试。完整协议见
[LangGraph + PydanticAI Integration](./15_LangGraph_PydanticAI_Integration.md)。
所有 module 间 request/result/decision/command/reference 的规范见
[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)。

## 11. RuntimeEvent 与 SSE

RuntimeEvent 用于 audit、metrics source、debug 和 UI projection 输入，不用于
恢复；它与可丢失 Diagnostic Telemetry 的边界见
[Observability and Operations](./12_Observability_and_Operations.md)：

```sql
CREATE TABLE runtime_event (
    event_id        UUID PRIMARY KEY,
    run_seq         BIGINT NOT NULL,
    tenant_id       TEXT NOT NULL,
    run_id          UUID NOT NULL,
    orchestration_id UUID NOT NULL,
    correlation_id  TEXT NOT NULL,
    causation_id    UUID,
    trace_id        TEXT,
    source          TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    event_schema_version TEXT NOT NULL,
    payload_schema_ref TEXT NOT NULL,
    payload         JSONB NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, run_seq),
    UNIQUE (tenant_id, source, source_event_id)
);
```

规则：

- 至少一次采集，stable `source_event_id` 去重。
- 每个 run 通过锁定 `agent_run` 行分配 commit-ordered `run_seq`。
- `run_seq` 只在单个 run 内有序；Parent/Child 之间不承诺伪全局顺序，
  使用 `orchestration_id`、causation 和 trace link 关联。
- event/payload schema 必须精确 versioned；未知版本进入可观测 dead letter，
  不能按当前 schema 猜测解析。
- `occurred_at` 不承担顺序。
- PostgreSQL `LISTEN/NOTIFY` 只唤醒 SSE，断线按 `run_seq` 查询补偿。
- projector 丢失时周期对账补齐终态事件。
- 外围 Experience Projector 只能消费已脱敏事件和 artifact references。

产品 UI 默认消费 Observation 的 `InteractionItem` snapshot 与 typed
`UIProjectionEvent` delta，而不是把 raw RuntimeEvent payload 当组件协议。
InteractionProjector 可以读取 RuntimeEvent、safe checkpoint/Action/Delegation
projection，但只产生带 source watermark 的可重建 read model；投影不能反向
恢复 Graph、批准 Action 或生成 Run Signal。Execution API 的 `stream` 仍返回
raw RuntimeEvent，Observation UI stream 使用独立 `projection_seq` cursor。

SSE 使用短事务、有界缓冲和断线重放；不得长期占用数据库事务或让慢客户
端形成无界内存。

Multi-Agent 的 delegation、fan-out、goal iteration、Child Run 和 Run Signal
事件采用 `16/17` 固定的 versioned payload schema。Parent Execution SSE 只复制 Child
lifecycle 摘要，不复制 Child 全部 node event；详细事件按 Child public
`run_id` 单独授权读取。fan-out 的高基数 branch 细节默认进入 trace/metrics，
失败和最终 reducer 事实不得采样。

## 12. 安全与租户隔离

```text
public run/action ID
  → tenant/actor/scopes from authentication context
  → tenant-scoped ownership lookup
  → internal thread/execution ID
  → runtime adapter
```

要求：

1. 不接受客户端自报 `tenant_id`。
2. 不暴露内部 `thread_id/checkpoint_id/action_execution_id`。
3. resume/approve/cancel/fork 每次重新授权；Child acceptance 同样重新授权。
4. internal signal 验证 bridge identity、tenant、RunWaitRef、source terminal
   fact 和 hash，不接受 public actor payload。
5. Tenant-owned 表强制 `tenant_id NOT NULL`、tenant 组合外键/唯一约束和
   fail-closed RLS；在线角色不是表 owner 且不能 `BYPASSRLS`，缺失可信
   Active Tenant Context 时不能读取 Tenant 数据。
6. 框架系统表只允许内部数据库角色访问。
7. Prompt/checkpoint/event/trace 中的敏感数据脱敏。
8. 模型只能选择租户、Skill 与 actor 共同授权的闭集。
9. 写凭据只注入 Durable Action adapter。

## 13. 部署

```text
PostgreSQL Cluster
├── langgraph schema
├── ear schema
├── knowledge schema
├── memory schema      # optional
└── dbos schema        # optional

FastAPI / Platform API process
PostgreSQL Execution Driver worker process
Run Delegation Coordinator process # optional
DBOS application process       # optional
```

MVP 的多个 Tenant 共享同一 database 和各 module schema；schema 不按 Tenant
动态创建。不同 module 可以共享 PostgreSQL 集群，但使用独立 schema、role、
migration 和 pool policy。Core 初期不需要 Redis Pub/Sub。

合规、数据驻留或规模形成真实需求后，可以把 Tenant 整体路由到拥有独立
database、密钥和 Worker 池的 Deployment Cell。Cell 仍使用相同 contract；
业务代码不能按共享库或独立库分支，MVP 不实现动态建库和双写迁移框架。

PostgreSQL 是共同故障域，生产必须有 PITR、恢复演练、连接池配额、容量
告警和 workload 隔离。

## 14. 被否决的 Core 方案

- pydantic_graph 作为核心图引擎：不满足动态图、debug、time travel 的
  产品核心，不继续自研 lifecycle。
- PydanticAI Agent 管 Tool/Memory/durability：会成为第二个 Agent Runtime。
- LangGraph 与 DBOS 同时包裹完整 Agent run：恢复所有权冲突。
- 自研统一 Event Store 恢复所有 runtime：破坏各状态所有者。
- 为未来替换 LangGraph 预建 Executor SPI：只有一个真实实现，没有收益。
- Redis 作为可靠恢复真相：Core 使用 PostgreSQL checkpoint/event replay。
- 把 FastAPI request task 当作可靠 worker：进程崩溃后没有 claim、接管和对账。
- 用线程、临时 queue 或 callback 创建 Child Run：缺少 parent fence、幂等
  acceptance、completion signal 和 reconciliation。
- 为 Sub-agent、Swarm 或 GoalLoop 建第二套 Agent lifecycle：会与 LangGraph
  checkpoint、权限和预算所有权冲突。
- 原地把 dry-run 提升为 commit：会扩大 immutable spec 的权限与副作用范围。

## 15. 技术依据

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Time Travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [LangGraph Backward Compatibility](https://docs.langchain.com/oss/python/langgraph/backward-compatibility)
