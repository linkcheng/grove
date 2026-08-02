---
status: accepted
scope: asset-risk-reference-profile
---

# Asset Risk Reference Profile 拒绝 partial Asset State View

本决策只适用于 Asset Risk Reference Business Profile；Execution Core 允许其他 Tool contract 显式定义
安全、可评测的 partial/pagination 语义，不把 `reject` 设为全局默认值。

该 Profile 的 `AssetStateView@1` 只有完整成功语义。若可信 limits policy 的
row、result bytes、context token 或 execution deadline 任一预算被超过，
`asset.state.read@1` 返回 canonical `asset_state.query_too_broad`，public API
投影为 `ToolQueryTooBroad`。Graph checkpoint 该 failure 后进入 terminal
failed，不接受 `AssetStateView@1`，也不启动任何依赖资产状态的 Inference。

`AssetStateView@1` 不定义 `truncated`、`partial`、`next_cursor` 或“已返回前 N 条”
语义。adapter 不能通过隐式分页、第二次 Tool 调用、缩小字段或丢弃记录来制造
成功。若数据库已经产生部分行或中间 Artifact，adapter 必须丢弃并确保它们不进入
ToolResult、checkpoint、RuntimeEvent、UI projection 或模型上下文。

Failure 只公开稳定 error code、`limit_kind` 的安全枚举、correlation ID 和缩小
范围的操作建议，不公开 SQL、table、真实总行数、未授权资源、内部 limit policy
或部分数据。用户需要收窄 `AssetStateQuery@1` 并通过普通 submit 创建新 Run；
resume、refresh-in-place 和自动改写查询均不允许。

## Consequences

- 只要 `AssetStateView@1` 存在，就表示对该次已接受查询没有因平台预算发生截断。
- `domain_view_accepted` 与 `domain_read_failed` 互斥；TooBroad 只产生后者。
- `ToolQueryTooBroad` 的 `retry_owner=none`、`retryable=false`；相同查询不能由
  Kernel 或 adapter 自动重试，用户必须改变输入后新建 Run。
- limits policy、failure mapping 和 partial-result policy 属于 Evaluation Subject；
  改变任一语义必须产生新版本并重新评测。
- 混合已授权、未授权或不存在资源的查询按 ADR-0021 同样整次拒绝，不返回授权
  子集或 omitted count。
