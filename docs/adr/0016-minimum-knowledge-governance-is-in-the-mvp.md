---
status: accepted
scope: mvp-foundation
---

# 最小 Knowledge 治理属于 MVP

首个 MVP 在 Execution Core 内交付最小 Knowledge Baseline，而不是先接入一个不受治理的
RAG、搜索或数据库查询，再把来源、权限与版本控制留到后续补齐。

MVP 只实现一个服务于所选 Business Profile 的 production `KnowledgePort` adapter，
并只消费由受信任发布流程生成的不可变 Knowledge Snapshot。每个 Run 必须在
`SkillExecutionSpec` 中固定精确的 Snapshot ref/version/content hash 和 retrieval
policy；禁止解析 `latest`。Snapshot 或 policy 变化必须产生新的行为绑定并改变
Evaluation Subject。

Knowledge seam 必须从 Active Tenant Context、Principal/Run Authority 和可信
policy node 获得 Tenant、Resource Scope、purpose、deadline 与结果预算。模型、
前端和普通 Middleware 只能提出 typed query intent，不能选择或扩大这些字段。
每个成功 item 必须带可验证 Citation，固定 Snapshot、source version、locator
和 content hash。Outcome 必须区分 `ok`、`empty`、`denied`、`timeout` 与
`unavailable`；后三者是 typed failure，不能伪装成空结果或泄露 source 是否存在。

首个 MVP 不建设通用 connector registry、crawler、持续 ingestion、索引管理 UI、
跨 source query planner 或 Long-Term Memory。只有出现多个真实 source、持续发布
需求或独立数据治理 owner 后，才进入 Knowledge Expansion release；扩展仍复用
同一 Snapshot、Citation、ACL、purpose、budget 和 outcome contract。

## Consequences

- Knowledge Snapshot 发布步骤、单一 production adapter 和 POC-E Knowledge
  Baseline 是 MVP 发布 gate，不是可选 Production Hardening。
- Snapshot 缺失、hash 不匹配或 adapter build 不兼容时，必须在首个 Graph node
  前 fail fast；请求级 ACL/purpose 不满足时必须在调用底层 backend 前返回
  `knowledge.denied`。
- `empty` 只表示已授权查询成功但无匹配项；模型不能将其或任何 typed failure
  补写为企业事实。
- Knowledge 正文不进入 RuntimeEvent、trace、metric 或普通日志；大内容只通过
  重新授权的 `ArtifactRef` 暴露。
- 多源接入与 Long-Term Memory 可以独立演进，但不得把 live connector 状态或
  Memory 内容变成当前 Run 的隐式 Knowledge。
- 一个具体 Knowledge/Tool 组合参考见
  [Asset Risk Reference Business Profile](../31_Asset_Risk_Reference_Profile.md)；
  本 ADR 不拥有资产语义，也不要求 Product MVP 选择该领域。
