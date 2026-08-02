# Durable Action Runtime

> 架构集：GROVE v1.0
> Profile：optional
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> Contract 规范：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)

## 1. 定位

Durable Action Runtime 只处理需要可靠执行的现实副作用：

- 外部写 API、发信、付款、退款、删除、资源创建。
- 重试、退避、限流、队列或定时调度。
- 超过单个 HTTP/Graph node 生命周期的任务。
- 等待 webhook、审批或外部事件。
- 多步骤业务事务和补偿。

分析、问答、Knowledge retrieval、Memory 和只读 Skill 不要求该 Profile。
任何声明 `durable_action` 的 Skill 在 Profile 缺失时必须于 resolve 阶段
失败。

DBOS 是当前 production adapter，不是 Execution Core。

## 2. Seam

```python
class DurableActionPort(Protocol):
    async def submit(
        self,
        command: ActionCommand,
    ) -> ActionHandle: ...
```

adapter：

- `DisabledDurableActionAdapter`：Core 默认，任何 submit fail fast。
- DBOS production adapter。
- deterministic fake adapter。

DBOS workflow 内部 step、retry、queue、event 不泄漏到 interface。
LangGraph State 只保存 `ActionHandle` 和 result reference。
`ActionCommand/Handle/Receipt` 的 typed field、authorization reference 和
idempotency 规则由 Canonical Contract 统一定义，本专题不复制第二份 schema。

## 3. Action 标识

```text
action_request_id
action_execution_id
logical_action_key
idempotency_key = {tenant_id}:{run_id}:{action_request_id}
action_command_digest = sha256(canonical ActionCommand semantics)
```

同一恢复必须复用原 key。time travel fork 生成新 `run_id`，进入新的动作
幂等命名空间。
同一 key/request ID 携带不同 digest 必须返回 `ActionRequestConflict`，不能
因 `ON CONFLICT DO NOTHING` 静默复用旧 execution。

digest 包含 tenant/run/request、logical action key、principal、action ref、
typed input hash、run mode、deadline/business precondition 和 idempotency
key；排除 `meta.message_id/trace`、`execution_fence` 以及可在重授权时变化的
authorization reference。这样 takeover 可以注入新 fence/授权证明，但不能
偷换业务动作。

`logical_action_key` 由 Kernel 根据 graph path、node execution key 和持久化
的业务操作 ordinal 确定性生成，模型和客户端不能提供。checkpoint 重入复用
同一 ordinal；业务上第二次合法执行必须先在 State 中产生新的 ordinal，不能
靠修改 input 绕过唯一约束。

DBOS adapter 将稳定 `action_execution_id` 映射为固定 workflow ID。
客户端不能提交或读取内部 execution/workflow ID。

## 4. Action Request

```sql
CREATE TABLE action_request (
    action_request_id      UUID PRIMARY KEY,
    tenant_id              TEXT NOT NULL,
    run_id                 UUID NOT NULL,
    logical_action_key     TEXT NOT NULL,
    idempotency_key        TEXT NOT NULL,
    action_command_ref     TEXT NOT NULL,
    action_command_digest  TEXT NOT NULL,
    input_hash             TEXT NOT NULL,
    principal_ref          TEXT NOT NULL,
    run_mode               TEXT NOT NULL,
    deadline               TIMESTAMPTZ,
    adapter_name           TEXT NOT NULL,
    action_execution_id    TEXT UNIQUE,
    action_ref             TEXT NOT NULL,
    action_schema_version  TEXT NOT NULL,
    action_runtime_version TEXT NOT NULL,
    execution_fence        BIGINT NOT NULL,
    status                 TEXT NOT NULL,
    command_authorization_ref TEXT NOT NULL,
    execution_authorization_ref TEXT,
    result_ref             TEXT,
    provider_receipt_ref   TEXT,
    last_error_ref         TEXT,
    reconcile_after        TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN (
        'accepted', 'started', 'waiting', 'succeeded',
        'failed', 'denied', 'stale', 'unknown',
        'manual_review', 'cancelled'
    )),
    CHECK (run_mode IN ('live', 'fork_commit')),
    UNIQUE (action_request_id, tenant_id),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, run_id, logical_action_key)
);
```

该表只在 Profile 启用时需要。

## 5. 跨运行时交接

不能在产生随机 ID 的同一个 Graph node 里直接执行副作用：

```text
prepare_action
  → LangGraph checkpoint
  → dispatch_action
  → wait_action_result
```

`prepare_action`：

1. 生成 logical key、request ID 和 typed command。
2. 派生稳定 idempotency key。
3. 注入当前 Execution Driver fencing token。
4. 只更新 Graph State。

`dispatch_action`：

1. 开启短数据库 transaction，并锁定目标 `agent_run` 与 logical action key。
2. 验证 command fence、run mode、当前 principal、tenant、本地 resource
   precondition 和 authorization policy。
3. 确定性派生 execution ID，以 `INSERT ... ON CONFLICT` 幂等创建
   `status=accepted` 的 ActionRequest，同时固化 immutable
   `action_command_ref/digest`、principal/run mode/deadline 和
   `command_authorization_ref`。命中已有 key 时必须比较 command/input digest；
   不同则 `ActionRequestConflict`，且 adapter/provider 调用数为 0。
4. commit；这是 Graph → Durable Action Runtime 的唯一所有权交接点。
5. transaction 外启动或查询同一 adapter execution；每次真正产生 effect
   前重新授权并校验 resource precondition，把该决策写入
   `execution_authorization_ref`。撤权或条件变化形成 `denied/stale`，不调用
   provider。
6. 写回 `started/waiting/terminal` status 与 handle。
7. 后续 Graph checkpoint 只保存 handle/reference。

cancel acceptance 与步骤 1～4 竞争同一 run lock。cancel 先提交时 fence 已
失效，ActionRequest 不能 accepted；action acceptance 先提交时，该 action
已经由 Durable Action Runtime 接管，之后的 cancel 只能按显式
cancel/compensation policy 处理。不能声称跨 PostgreSQL 与外部系统的瞬时
取消。

任意步骤崩溃后重入同一流程。不要增加崩溃后永久卡死的 `dispatching`
状态。

恢复 worker 使用相同 `action_request_id/logical_action_key/idempotency_key`
重新生成 transport command。若 ActionRequest 尚未 accepted，新 worker
注入当前 fence并重新竞争 acceptance；若已 accepted，则保留首次
`execution_fence` 作为审计事实，只启动或查询同一 execution，不能生成第二个
request/execution。

周期对账：

- `accepted` 超时：启动或查询固定 adapter execution。
- `started/waiting` 超时：按 adapter/execution ID 查询。
- adapter 终态但 Graph 未恢复：触发 completion bridge。
- provider 结果不确定：进入 `unknown/manual_review`。

## 6. 外部幂等事实

DBOS 只能保证 workflow/step 持久化重入，不能替外部系统创造 exactly-once。

| 外部能力 | 执行策略 |
|---|---|
| 自有 PostgreSQL | unique constraint + transaction |
| 支持 idempotency key | 原样透传并持久化 provider receipt |
| 不幂等但可查结果 | 先查后写；响应丢失后对账 |
| 不幂等且不可查询 | 禁自动重试/time travel；人工确认或补偿 |

Action Registry 静态声明能力。超时若无法证明失败，必须进入 `unknown`，
不能抛普通异常触发盲重试。

provider 原始 receipt 可能包含 PII、secret 或大 payload，必须先按 retention/
redaction policy 存为 `ArtifactRef`，`action_request` 和 `ActionReceipt` 只
保存 reference。禁止把 provider response JSON 直接复制到运行表和 event。
ActionCommand 的 typed input 同样只通过受治理 immutable command Artifact
持久化；运行表只保存 reference/hash。pending/waiting action 是 artifact
retention root，不能在 workflow 排空前删除 command。

## 7. DBOS Version 与恢复

通用字段 `action_runtime_version` 在 DBOS adapter 中保存
`application_version`：

1. 新 action 进入最新 approved application version。
2. 旧 workflow 只由匹配旧 version 的 executor 恢复。
3. 旧 pending/waiting workflow 排空后才能下线旧制品。
4. 修复旧 workflow 时显式 fork，不能原地套用新步骤。

不要同时让 DBOS 包裹完整 LangGraph run；否则 workflow 与 graph node
同时成为恢复单元。

## 8. 业务审批

审批属于 Durable Action Runtime，而非对话 interrupt：

```text
LangGraph ActionCommand
  → DBOS workflow 校验权限和业务前置条件
  → wait approve/reject event
  → execute side effect
  → persist receipt/result
  → internal completion bridge emits trusted Run Signal
  → Execution Driver resumes waiting LangGraph
```

审批记录包含 tenant、actor、policy version、request digest、reason 和时间。
`(tenant_id, action_request_id)` 只允许一个不可覆盖决定。

审批决定不等于永久授权。真正执行副作用之前必须重新检查当前 principal、
tenant、resource state、Skill permission ceiling 和 authorization policy。
权限已撤回或资源前置条件变化时，workflow 终止为明确的 denied/stale
结果，不能沿用旧 approval。只有授权策略显式定义的、带 expiry、scope、
request digest 和一次性消费语义的 durable grant 才能跨等待期使用。

endpoint 先授权并原子写唯一决定，再用稳定 message/event ID 通知 adapter。
重复 approve/reject 返回已有决定。

`ActionCompletionBridge`：

1. 只接收内部 `action_request_id`。
2. 按当前 tenant 读取 request。
3. 查询 adapter 权威终态。
4. 原子更新 receipt/result ref。
5. 验证 Graph 的 `RunWaitRef` 正等待同一 action。
6. 用稳定 completion ID 派生同一个 internal Run Signal。
7. 通过普通 run command path 幂等恢复；signal 丢失时由 reconciliation
   重建。
8. 遵守 Core 的“每个 target run 最多一个未消费 signal command”；与 Child
   completion 同时到达时按 stable source key 串行投递。

禁止把 adapter 提供的任意 payload 直接 merge 到 Graph State。
public resume 不能伪造 Action completion；internal signal 不能消费用户
`InterruptRef`。

## 9. Time Travel

最终 adapter seam 强制：

```python
if run_mode not in {"live", "fork_commit"}:
    raise ReplaySideEffectForbidden(run_mode)
```

UI 隐藏按钮、Prompt 约束或只在 Graph 某个 node 判断都不够。

`fork_commit` 是从 source checkpoint 创建的新 run，使用新幂等命名空间并
要求独立授权。历史 receipts 只读，不会因 replay 再次提交。

## 10. 取消

- Graph cancel：停止后续推理，不自动撤销已启动 action。
- cancel acceptance 会撤销当前 Graph invocation 的 fence；尚未 dispatch 的
  Graph 内 ActionCommand 因 fence 失效而不能 accepted。
- 已 accepted 的 action 已完成所有权交接，必须按 workflow
  cancel/compensation policy 处理。
- workflow cancel：按业务流程停止、拒绝或补偿。
- 已提交副作用可能不可撤销。
- 根 run cancel 由应用层按 action type 执行显式传播 policy。

UI 不得把“停止继续执行”展示为“现实事实已经撤销”。

## 11. DBOS HA

| 阶段 | 部署 | 声明 |
|---|---|---|
| POC/单机 | 单 active executor，固定 executor ID | 同 executor 重启恢复；不声明跨实例 HA |
| 生产 HA | 多 executor + DBOS Conductor | 失联检测和其他 executor 自动接管 |

不部署 Conductor 时，不宣称其他 executor 自动接管 orphan workflow。
GROVE 不自研租约、心跳和脑裂协议替代 Conductor。

## 12. 为什么不默认引入 Celery/arq

启用 Durable Action Profile 后，DBOS 已覆盖 workflow、queue、schedule 和
PostgreSQL durable state。再引入 Celery/arq 会新增：

- Redis/RabbitMQ。
- 第二套 task ID、retry、cancel 语义。
- PostgreSQL 与 broker 双写窗口。
- 第二套 worker 运维和可观测性。

只有出现 DBOS 不适合的独立计算负载，并有量化数据证明时，才评估专用
计算队列。Celery 不是 GROVE 默认异步层。

## 13. 被否决的方案

- DBOS 作为 Execution Core 必选组件。
- Graph node 直接调用外部写工具。
- 用分布式事务绑定 checkpoint 与 workflow。
- 把超时一律视为失败并自动重试。
- 把历史 approval 当作执行时仍然有效的 authorization。
- 在 action_request 中直接保存 provider receipt 正文。
- LangGraph 与 DBOS 重复拥有业务审批。
- Celery/arq 作为默认 durable action。

## 14. 技术依据

- [DBOS Architecture](https://docs.dbos.dev/architecture)
- [DBOS Workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial)
- [DBOS Workflow Communication](https://docs.dbos.dev/python/tutorials/workflow-communication)
- [DBOS Workflow Recovery](https://docs.dbos.dev/production/workflow-recovery)
- [DBOS Workflow Upgrades](https://docs.dbos.dev/python/tutorials/upgrading-workflows)
- [DBOS + LangGraph Example](https://docs.dbos.dev/python/examples/customer-service)
