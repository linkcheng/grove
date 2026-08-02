# Execution Workspace

> 架构集：GROVE v1.0
> 上位边界：[GROVE Architecture](./00_GROVE_Architecture.md)
> 执行绑定：[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)
> 消息契约：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)

## 1. 定位

Execution Workspace 是可选的、按 Agent Run 隔离的短期执行环境，为已授权
Tool 提供文件系统和进程资源。只有 Skill 同时固定 `workspace` policy 并声明
`execution.workspace` capability 时才启用。

它不是：

- 第二个 Agent Runtime 或 Execution Kernel。
- LangGraph State、checkpoint、Memory 或 Artifact Store。
- 外部业务写入的旁路；外部 effect 仍必须进入 Durable Action。
- 允许运行时安装任意 Skill、MCP server 或 Tool 的扩展容器。

本设计只借鉴 [AgentScope](https://github.com/agentscope-ai/agentscope) 将不同
workspace provider 收敛到统一 seam 后的思路。GROVE 不引入其 Agent/Team
运行时、动态 manager 或第二套 event system；Workspace 只解决隔离执行环境
这一件事。

## 2. 所有权与边界

| 对象 | 权威所有者 | 其他 module 只保存 |
|---|---|---|
| workspace policy、Tool binding、bootstrap artifact | Skill Registry / SkillRuntimeManifest | 精确 ref/hash |
| adapter、sandbox image 与依赖构建 | RuntimeBuildManifest | 精确 ref/hash |
| 物理 workspace instance 与 acquire/release | Execution Workspace adapter | 不透明 `WorkspaceHandleRef` |
| 是否 acquire、何时 release | Execution Core 按 Agent Run lifecycle 决定 | lifecycle event |
| graph route、Tool 调用状态 | LangGraph State/Checkpointer | workspace reference |
| 跨 checkpoint 或 run 保留的文件 | Artifact Store | `ArtifactRef` |

Workspace adapter 不决定 graph route、retry、permission、Tool allowlist 或
Agent Run 终态。Kernel 不读取 provider client、容器对象或主机路径。

## 3. 最小接口

```python
class ExecutionWorkspacePort(Protocol):
    async def acquire(
        self,
        command: WorkspaceAcquireCommand,
    ) -> WorkspaceHandleRef: ...

    async def release(
        self,
        command: WorkspaceReleaseCommand,
    ) -> None: ...
```

接口只有生命周期操作。`exec/read/write` 仍由现有 typed Tool adapter 提供；
Workspace Tool adapter 用 handle 在 module 内解析 provider backend，SDK client
不穿过 seam。这样 Graph 和 Skill 不依赖 Docker、Kubernetes 或远程 sandbox
SDK。

实现可以是 local、container、Kubernetes 或远程 sandbox provider，但生产
Profile 必须使用 `RuntimeBuildManifest` 固定实际 adapter、依赖和 sandbox
image digest。Local adapter 只用于开发/测试，不能据此关闭生产隔离 P0。

未启用 capability 时使用 `DisabledExecutionWorkspaceAdapter`，任何 acquire
都返回 `MissingCapabilityError`，不能退化为宿主机目录或进程。

## 4. 不可变绑定

`SkillDefinition.workspace_policy_ref` 固定内容寻址 policy。v1 policy 至少固定：

```text
isolation_scope = per_run
filesystem roots / quota / path policy
network_mode = none                         # v1
CPU / memory / process / wall-clock limits
environment allowlist / fixed non-secret values
writable scratch roots / artifact commit size limits
lifetime / idle / cleanup policy
per-invocation and per-branch namespace policy
```

规则：

1. 有 `workspace_policy_ref` 时必须声明 `execution.workspace`；反之不自动创建
   workspace。
2. SkillRuntimeManifest 固定允许使用 workspace 的 Tool、`workspace_local`
   effect、read-only bootstrap ArtifactRef set 和 artifact-commit output
   mapping；RuntimeBuildManifest 固定实际 adapter 和 image。bootstrap 只按
   artifact identity 挂载到 provider 内部确定位置，不接受模型提供 host path。
3. policy、bootstrap artifact、Tool effect、adapter 或 image 任一变化都改变
   对应 Manifest/hash，并进入 `evaluation_subject_hash`。
4. policy 和 Manifest 不包含 credential、hostname、provider token、浮动 tag
   或 `latest`。
5. credential 不进入 workspace。外部只读访问所需 secret 留在对应 Tool
   adapter 内；可写 credential 只允许进入 Durable Action adapter。
6. 实现发布时以新 ABI minor 引入 `workspace` policy kind，并发布对应
   Canonical Contract version；禁止原地修改已发布 schema/fixture。

逻辑绑定确定性计算：

```text
workspace_binding_hash = sha256(canonical_json(
  tenant_id + run_id
  + skill_spec_hash
  + workspace_policy_hash
  + runtime_build_hash
  + sorted bootstrap ArtifactRef hashes
))
```

同一 run 的 worker takeover 使用相同 binding；不同 run 即使 policy 相同也
不能得到同一 physical workspace。

`SkillExecutionSpec` 顶层不增加 workspace 配置区；它只通过既有
`required_capabilities`、`policy_refs`、`runtime_manifest` 和
`runtime_build` 固定该能力。

## 5. 生命周期与恢复

```text
resolve exact policy / Manifest / RuntimeBuild
  → Driver obtains current execution_fence
  → start LangGraph at workspace lifecycle node
  → ExecutionWorkspacePort.acquire()
  → persist WorkspaceHandleRef before first workspace Tool
  → LangGraph invokes authorized Tool nodes
  → terminal / cancel / expiry
  → ExecutionWorkspacePort.release()
  → reconciliation cleans orphan instance
```

- `acquire` 只允许 `live` 和 `fork_commit`。acquire/release command ID 按
  tenant + run + binding + lifecycle transition 确定性生成；semantic digest
  不包含 attempt/fence，相同 ID 的其他语义字段不同必须冲突。
- acquire/release 都验证 tenant、run、binding hash 和当前
  `execution_fence`；过期 worker 不能创建或释放 workspace。
- Core worker 崩溃后，新 worker 必须重新连接同一个 run workspace。provider
  无法证明原 instance 时返回 `WorkspaceUnavailable`，不得从未知残留目录
  静默创建“看起来相同”的环境继续执行。
- provider 内部 retry 只允许发生在 acquire acknowledgement 前且受固定预算
  约束；handle 已产生后的 unavailable 交回 Kernel error route，不能由
  provider 隐式重建。
- interrupt/wait 期间 workspace 可以保留到 policy deadline；超过期限必须
  明确终止或要求新 run，不能无限占用资源。
- terminal/cancel 后 release 是幂等的；notification 丢失时由 reconciliation
  清理 orphan。
- `replay` 和 `fork_dry_run` 不 acquire workspace，只消费精确匹配的 Tool
  recordings；source handle 不复制到可执行 State。缺失或 hash 不匹配时
  fail fast，不能调用真实 provider。`fork_commit` 使用新 run/binding，不能
  接管 source workspace。

Workspace 内容不是恢复真相。任何在 checkpoint 之后仍必需的文件，必须由
ToolResult 提交成不可变 `ArtifactRef`；未提交 scratch 丢失不能由 Graph
假定存在。最终输出同样只通过 ArtifactRef 离开 workspace。

## 6. Tool 与副作用边界

Tool effect 允许三类：

| effect | 允许范围 | owner |
|---|---|---|
| `pure` | 无外部读取或写入的计算 | Tool adapter |
| `read` | 已授权的外部只读访问 | Tool adapter |
| `workspace_local` | 当前 run workspace 内的有界文件/进程变更 | Tool adapter + Workspace |

`workspace_local` 不等于通用 write 权限。写宿主机、企业数据库、第三方系统、
共享存储或发送消息仍是 `ActionProposal → ActionCommand`，必须进入 Durable
Action Runtime。Tool adapter 必须拒绝：

- 缺失、过期、跨 tenant 或跨 run 的 `WorkspaceHandleRef`。
- Manifest 未声明的 Tool、effect、路径、网络目标或 bootstrap 内容。
- workspace 根目录逃逸、symlink traversal、设备文件和未授权 host mount。
- 运行时安装或替换当前 spec closure 外的 Skill、Tool、MCP server。

v1 workspace process 不允许直接联网；外部只读访问走已有 typed `read` Tool
adapter，任何外部写走 Durable Action。Artifact commit 只能提交 Manifest
声明的输出路径，并固定 schema、sensitivity、retention 和 content hash，不能
把整个 workspace 打包外传。

## 7. Multi-Agent 语义

- 同一 Agent Run 的 Sub-agent 与 GoalLoop 使用同一物理 workspace；每个
  Sub-agent invocation 使用独立 namespace，同一 GoalLoop 的各 iteration
  复用该 goal instance 的稳定 namespace。
- Swarm/`Send` branch 使用稳定 `branch_id` 对应的独立目录；禁止并发写同一
  path。Reducer 只归并 typed result/ArtifactRef，不读取“最后写赢”的共享
  文件状态。
- Child Run 默认创建独立 workspace。Parent/Child 只能通过 typed input、
  `ArtifactRef` 和 `DelegationResult` 交换数据，不共享 live filesystem。
- v1 不支持跨 run、跨 tenant 或长期共享 workspace；出现真实需求后再以新
  capability 和独立安全评审引入。

这些规则不新增 Multi-Agent Runtime；拓扑、Join、预算与终止仍由 LangGraph
和 Run Delegation 协议拥有。

Kernel 把该逻辑 namespace 作为不透明 `ToolCommand.workspace_scope` 注入；
模型不能提供，adapter 也不能把它直接当作未经校验的文件路径。

## 8. 观测与安全

最低 lifecycle event：

```text
workspace_acquired
workspace_reattached
workspace_release_requested
workspace_released
workspace_denied
workspace_unavailable
workspace_orphan_cleaned
```

RuntimeEvent 只包含 run/workspace reference、policy/build hash、结果、耗时和
脱敏 failure，不包含命令正文、文件内容、绝对主机路径、环境变量或 credential。
高基数过程进入 trace/metrics，terminal、denied、unavailable 和 orphan cleanup
事实不得采样。Telemetry 失效不能阻止 release/reconciliation。

生产默认：无 host filesystem、无 privileged container、无 credential mount、
无 workspace 直接网络、资源 hard limit、tenant/run 物理隔离。解引用 artifact
和网络访问在实际 Tool seam 重新授权；secret 始终留在 adapter 边界内。

## 9. 验收与明确不做

发布 `execution.workspace` 前必须完成
[POC-L](./90_P0_Blockers_and_Acceptance.md)，
至少证明跨 run/tenant/host 不可见、worker takeover 可重新连接、旧 fence
不可 acquire/release、replay 无真实 workspace 调用、ArtifactRef 是唯一持久
输出，以及 terminal orphan 可在预算内清理。

v1 明确不做：

- per-agent、per-tenant 或长期共享 workspace。
- workspace snapshot 作为第二套 checkpoint。
- 通用 remote desktop、IDE session 或人机协同 UI 协议。
- 动态 Skill/MCP/Tool 安装和 current run closure mutation。
- workspace process 直接网络访问；未来若需要，必须新增独立 capability 与
  effect/协议级门禁，不能仅靠域名 allowlist。
- 让 workspace provider 承担 Tool authorization、Action durability 或
  Agent Run recovery。
