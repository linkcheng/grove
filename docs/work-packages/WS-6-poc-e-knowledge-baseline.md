# WS-6 POC-E 步骤级证据记录（6.C.4）

> 依据：`docs/90` §9 POC-E（MVP Knowledge Baseline 步骤 1～6；步骤 7～11 属
> Long-Term Memory release）。本记录是工作包级证据索引，**不改变** `docs/90`
> 的 `evidence_state`——POC 关闭与 `verified` 由负责人在 F 线 G3 验收时批准。
> 实现范围：`app/knowledge/`（C1/C2，`9df8e44`）、`asset.state.read@1`（C3，
> `9805195`）、AssetRisk 参考闭环（D 线，`bfd5289`）。

## 步骤矩阵

| POC-E 步骤 | 状态 | 证据 |
|---|---|---|
| 1. Snapshot v1 绑定 root Skill Run；发布 v2 后旧 Run 固定 v1，新 Run 用 v2 | passed（seam 级） | `test_citations_pin_v1_across_a_v2_publish`（本记录随附）：发布 v2 后同一 adapter 的两次 retrieve citation 逐字节相同且固定 v1/content hash；新 adapter 服务 v2。kernel 组合时冻结单一 snapshot（`compose_asset_risk_kernel`，`4a265be`）；spec↔snapshot 绑定由 D5 变体证据链承担（`792cec7`）。registry 级"旧 run Spec hash 复核"端到端随 F 线 G3 复核 |
| 2. `ok` item 验证 Snapshot/source version/locator/content hash；篡改/删除/build 不匹配 → 调用数 0 | passed | citation 四元组 + item content hash 断言：`test_adapter_serves_cited_results_from_the_frozen_snapshot`；快照自 hash 覆盖 items/sources/ACL，篡改在 load 时 fail closed：`test_tampered_item_fails_closed_at_validation`；adapter 构造拒绝（provider/retrieve 调用数 0）：`test_adapter_refuses_tampered_snapshot_at_construction`；retrieve 路径篡改 → typed `unavailable`：`test_retrieve_path_tamper_maps_to_unavailable`；build 绑定：`test_builder_binds_source_hashes_into_the_snapshot`、`retrieval_build_ref` 入 result |
| 3. Tenant A / 无 scope / 伪造读取或枚举 Tenant B source 全拒绝且不泄露存在性 | passed | `test_adapter_denies_cross_tenant_and_missing_scope`：denial 为固定 `safe_message`（不区分 source 是否存在）；`KnowledgeRequest` 契约 `extra="forbid"`，ACL 只来自 snapshot `acl_policy`，请求正文/伪造字段无法影响可见性（closed seam） |
| 4. `ok/empty/denied/timeout/unavailable` 五种稳定 outcome；只有 `ok` 携带 items/citations | passed | ok：`test_adapter_serves_cited_results_from_the_frozen_snapshot`；empty 不造事实：`test_adapter_returns_empty_without_inventing_facts`（items=`()`）；denied：见步骤 3；timeout（typed、retryable）：`test_deadline_expiry_maps_to_typed_timeout`；unavailable：`test_retrieve_path_tamper_maps_to_unavailable`。失败 outcome 只携带 `CanonicalFailure`（safe_message），无 items/citations |
| 5. 预算超限 → seam 有界截断或 typed failure；RuntimeEvent/trace/日志无 Knowledge 正文 | passed（seam 级） | `test_budget_truncation_is_bounded_and_flagged`（`max_results` 截断 + `truncated` 标志）；deadline 超限映射 typed `knowledge.timeout`（步骤 4）。graph 节点只保留 `content[:512]` 摘要进 run state（`app/asset_risk/graph.py`）；失败只落 safe_message；WS-4 审计事实契约为有界 closed model，不携带 knowledge 正文 |
| 6. 固定 Knowledge Snapshot、两次 Run 间修改 Live State：citation 相同、read ToolResult 可区分；Live 不经 KnowledgePort；Inspect 可见 Run Data View provenance | passed | citation 跨 run 稳定：步骤 1 v1-pin 测试；read 侧 provenance（`observed_at`/watermark/result hash）：`test_complete_view_carries_full_run_data_view_provenance`、`test_view_hash_is_stable_and_request_bound`（`9805195`）；D3 端到端（`bfd5289`）：Live 资产状态只经 `asset.state.read@1`（RLS、all-or-nothing）进入 Run，Knowledge 快照冻结不变；Run Inspect/侧栏 provenance 由 `domain_view_accepted` 投影 + Profile renderer 呈现（`728059b`） |
| 7～11. Long-Term Memory 扩展（fail fast、historical replay、MemoryCandidate 治理、recall checkpoint、outbox） | not_applicable | Long-Term Memory 是未启用的 Release Track；Profile 冻结记录（`business-profile.asset-risk@1`）明确未声明能力保持 unavailable；N-14/N-17 属 WS-7 后 Memory release 范围 |

## 边界与已知限制

- 步骤 1/5 的 `passed` 为 seam 级单测 + 组合结构保证；跨 registry 发布、观测面
  无正文的完整运行矩阵与 POC-E 关闭记录（`docs/90` evidence_state → `verified`）
  属 F 线 G3 验收与负责人批准范围，本记录不越权宣称。
- 测试文件：`tests/test_ws6_knowledge.py`（13 用例）、`tests/test_ws6_asset_read_tool.py`；
  门禁：`make verify`（本记录随附 commit 的 verify 全绿记录见 WBS §13）。
