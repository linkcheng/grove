---
status: accepted
scope: asset-risk-reference-profile
---

# Asset Risk Reference Profile 只暴露强类型领域读取 Tool

本决策只固定 Asset Risk Reference Business Profile 的具体 Tool binding 与数据库边界。Execution Core
只提供通用 typed Tool seam，不内建 `asset` resource、SQL adapter 或此处的调用策略。

该 Profile 的 `@1` release 只向 `AssetRiskSkill` 暴露一个 versioned 领域 Tool：

```text
tool_ref          = asset.state.read@1
operation         = asset.state.read
resource_type     = asset
effect_class      = read
input_schema      = AssetStateQuery@1
output_schema     = AssetStateView@1
```

模型只能生成符合 `AssetStateQuery@1` 的领域字段。数据库名、schema、table、column、
join、SQL fragment、排序表达式、Tenant、Principal、Resource Scope、statement
timeout、row/byte limit 和 credential 都不能出现在模型 payload。Node Adapter 与
policy node 校验精确 Tool ref、input schema、Manifest closure 和权限后，才产生
`ToolCommand`。

`AssetStateQuery@1` 唯一的资产选择方式是显式 `asset_refs`，且必须非空、唯一、
有界。`filter/search/query/all_assets/pagination/sort` 不属于该 schema；新增选择方式
必须发布新的 Tool/schema version，不能向 `@1` 追加可选字段或由 adapter 私下解释。
Manifest 固定经过评测的 `max_asset_refs` 硬上限；Deployment/Tenant policy 只能为
新 Run 调低有效值，Resolver 必须把最终值和 policy ref/hash 固定到
`SkillExecutionSpec`。任何调高请求在 resolve 时拒绝。

production adapter 可以在内部使用 PostgreSQL，但只能通过参数化查询、固定 query
template 或等价的安全 query builder 实现，并在最终 seam 执行 Active Tenant
Context、当前 Principal/Run Authority、RLS/Resource Scope、statement timeout、
row/byte limit 与结果 schema 校验。通用 `postgres_query/sql`、数据库 client 或
任意查询 MCP 不进入 Tool Registry，也不提供给模型。

## Consequences

- `asset.state.read@1` 的 Tool ref、operation、resource type、effect、input/output
  schema、limits policy 与 adapter compatibility 固定在内容寻址 Manifest 中。
- unknown/extra field、任意 SQL、closure 外 Tool ref 或模型伪造的安全字段必须在
  database/Tool provider 调用前拒绝；不能尝试“清洗后执行”。
- filter、search、`all_assets`、pagination 或 sort 输入按同一规则在 provider 前
  拒绝；不得转换成资产列表后继续执行。
- input/output 语义改变时发布新的 Tool/schema version 并重新生成 Evaluation
  Subject；只改变等价的内部查询实现仍须更新 Runtime Build 和对应评测证据。
- 提高 `max_asset_refs` 会扩大已评测输入域，必须发布新 Manifest 并重新评测；降低
  按 [ADR-0022](./0022-monotonic-input-limit-tightening-reuses-ceiling-evidence.md)
  复用 ceiling Evaluation evidence，但必须改变新 Run 的
  `skill_spec_hash` 并通过 comparator、contract rejection、hash 与 UX 测试；不能
  改变活动 Run 的已解析上限。
- 领域 Tool 可以在出现真实业务需求后新增，但不能退化成一个参数不断膨胀的万能
  query endpoint。
- 运维人员的 ad-hoc SQL 属于独立、人工授权和审计的管理面，不属于 Agent Run，
  也不在该 Profile 的 Agent Run 范围内。
