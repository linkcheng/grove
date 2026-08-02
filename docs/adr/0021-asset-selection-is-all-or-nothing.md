---
status: accepted
scope: asset-risk-reference-profile
---

# Asset selection 采用全有或全无语义

本决策只适用于 Asset Risk Reference Business Profile；Execution Core 只要求 selection/disclosure policy
被版本化和可验证，不把 all-or-nothing 强制给所有业务 Tool。

该 Profile 的 `AssetStateQuery@1` 只要包含任一不存在、当前 Principal/Run Authority
无权访问、跨 Tenant、已删除或在读取 transaction 中不再可见的 `asset_ref`，
`asset.state.read@1` 就必须拒绝整个 selection。canonical failure 固定为
`asset_state.selection_unavailable`，public API 投影为
`ResourceSelectionUnavailable`。

`asset_refs` 是 `AssetStateQuery@1` 唯一 selection 入口，必须非空、唯一且有界。
filter、search、query DSL、`all_assets`、pagination 或 sort 在 contract validation
阶段拒绝，不进入本 ADR 的 selection 解析，也不能由 adapter 转换成隐式 ref 集合。
有效数量上限取 Manifest ceiling、Deployment maximum 与 Tenant maximum 的最小值；
后两者必须分别小于等于其上一级上限，只能收紧并只作用于新 Run；越界配置直接
拒绝，不能静默钳制。最终值必须进入该 Run 的不可变执行绑定。

adapter 必须在生成 View 的同一个短 `READ ONLY REPEATABLE READ` transaction 内，
使用 Active Tenant Context、RLS 和 Resource Scope 解析全部请求引用，并验证
“请求的唯一 ref 数量 = 全部已授权且可见的匹配数量”。不相等时丢弃任何已读取行，
返回 failure；不得返回授权子集、omitted count、失败索引、存在性标记或部分
provenance。输入中的重复 ref 在数据库调用前按 contract 拒绝，避免数量语义含糊。

public error、RuntimeEvent、UI、常规日志和 telemetry 对不存在与无权访问使用完全
相同的 code/message/shape。它们只携带 request digest、correlation ID 和重新选择的
操作建议，不携带 asset ref、内部 ID、匹配数或失败原因。ToolResult failure 的
output、ArtifactRef 和 provenance 均为空；Graph checkpoint failure 后直接终止，
Inference 调用数为 0。

## Consequences

- 被接受的 `AssetStateView@1` 精确覆盖本次 canonical selection 的全部唯一
  `asset_ref`，既没有预算截断，也没有授权过滤造成的隐式 partial result。
- `ResourceSelectionUnavailable` 的 `retry_owner=none`、`retryable=false`；用户必须
  重新选择资产并创建新 Run，不能 resume 或在当前 Run 中删掉失败项继续。
- existence/authorization 的内部实现差异不能进入面向调用者的时延、错误字段或
  metric label 契约；安全测试必须覆盖枚举和 timing 侧信道的合理上界。
- selection policy 属于 Manifest/Evaluation Subject；改成“返回授权子集”必须发布
  新版本并重新进行安全与业务正确性评审，不能用 Tenant 配置切换。
- 新增 filter-based selection 同样必须发布新的 Tool/schema version，并单独确定
  query budget、授权披露、完整性与结果大小策略。
- 任何高于 Manifest `max_asset_refs` 的配置在 resolve 时 fail fast；不能等待 Tool
  adapter 截断、分页或过滤。
- 合法调低按
  [ADR-0022](./0022-monotonic-input-limit-tightening-reuses-ceiling-evidence.md)
  只缩小输入 admission domain：复用 ceiling evidence，保持
  `evaluation_subject_hash`，但新 Run 的 effective binding 与 `skill_spec_hash` 必须
  改变。
