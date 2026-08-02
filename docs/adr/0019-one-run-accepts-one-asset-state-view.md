---
status: accepted
scope: asset-risk-reference-profile
---

# 一个 Asset Risk Run 只接受一个 Asset State View

本决策只适用于 Asset Risk Reference Business Profile。Execution Core 不规定所有 read Tool 每个 Run
只能成功一次，也不规定 source adapter 必须使用 PostgreSQL 或该隔离级别。

该 Profile 的每个 Agent Run 只允许一次逻辑 `asset.state.read@1`。adapter 在一个
短生命周期、`READ ONLY REPEATABLE READ` 的 PostgreSQL transaction 中执行固定且
有界的内部查询，生成一个 `AssetStateView@1` 后立即结束 transaction。数据库
transaction 不能跨 LangGraph checkpoint、Inference、interrupt 或 worker yield。

该 Profile 的 `@1` Graph 固定为：

```text
validate_input
  → optional collect_missing_input / exact InterruptRef resume
  → retrieve_policy_knowledge
  → read_asset_state
  → checkpoint accepted AssetStateView@1
  → inference / risk analysis
  → typed report
```

`read_asset_state` 使用稳定 logical read key 和 `tool_request_id`。只有
checkpoint 已接受的 `ToolResult[AssetStateView@1]` 才是该 Run 的权威视图；后续
node 必须复用它，不能再次查询当前数据库。需要刷新状态时创建新 Run，并重新
resolve、authorize 和读取。

“一次逻辑读取”不承诺故障下只有一次物理数据库访问：若 worker 在读取成功但
checkpoint 提交前崩溃，node 可以按同一 logical key 进行有界重试并重新读取；
最终只有一个成功结果进入 checkpoint。若 checkpoint 已提交，恢复时数据库调用数
必须为 0。Inference 只能在该 checkpoint 之后开始，因此同一 Run 不会混用两个
资产视图。

## Consequences

- `AssetStateView@1` 是 run-scoped typed observation，不是 Knowledge Snapshot、
  cache 或 live database session。
- budget 固定 `asset.state.read@1` 的 logical success count 为 1；重复模型 Proposal
  或 Graph route 必须在 Tool provider 前拒绝。
- 多个固定 SQL statement 可以在同一个短 transaction 内组成该 View，但不能由
  模型逐步探索数据库，也不能在模型调用之间保持 transaction。
- `observed_at`、source revision/watermark、result hash、logical read key 和
  `tool_request_id` 随 ToolResult 进入 checkpoint/Run Inspect。
- 超出 row/byte/token/deadline budget 时按 ADR-0020 返回
  `ToolQueryTooBroad`；不得接受 partial view、隐式分页或第二次 Tool 调用。
