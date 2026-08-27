# WS-6 POC-M 步骤级证据记录（6.F.4）

> 依据：`docs/90` §9 POC-M 与 `docs/31` §7 验收 1–8。本记录是工作包级证据索引，
> **不改变** `docs/90` 的 `evidence_state`——POC-M 关闭与 `verified` 由负责人在
> G3 验收时批准。实现范围：C3 typed read tool（`9805195`）、D 线参考闭环
> （`bfd5289`）、F.1 G3 E2E（`c1bf7f9`）与本文随附 commit。

## 步骤矩阵

| POC-M 步骤 | 状态 | 证据 |
|---|---|---|
| 1a. 注入 filter/search/DSL/all_assets/pagination/sort/SQL/对象/Tenant/scope/limit/extra field/closure 外 ref → provider 与 DB 调用数 0；空/重复/超限 refs 在 DB 前 contract fail | passed | `test_selection_expansion_fields_fail_before_the_provider`、`test_refs_must_match_the_asset_grammar_and_be_unique`、`test_over_ceiling_selection_fails_before_the_provider`（`tests/test_ws6_asset_read_tool.py`：封闭 schema + fake source 计数为 0） |
| 1b. Deployment/Tenant/Manifest `max_asset_refs` 取最小值；高于 ceiling 零创建零调用；合法调低只影响新 Run spec hash | passed（seam 级） | `test_ceilings_tighten_monotonically_and_never_widen`（单调收紧）；submit 侧绑定由 D5 变体证据链承担（`792cec7`），spec-hash 级端到端随 F 线 G3 验收复核 |
| 1c. 篡改 comparator/limit key/attestation 在 run/provider 前拒绝 | ✅ 完成（2026-08-26） | `test_ceiling_tamper_matrix_rejects_before_any_call`：伪造 limit key（closed model 拒绝）、widened comparator（invalid configuration）、冻结模型就地篡改（assignment 拒绝）、construct 走私的 ceiling（tool seam 映射 typed `input_contract_invalid`，source 调用数 0） |
| 1d. golden dataset 容量测量（单资产 P99 row/byte/token/deadline → 最小值 ×0.8 向下取整，closing record 冻结） | ✅ 完成（证据侧，2026-08-26） | `scripts/ws6_capacity_probe.py` 用 golden 形态组合在真实 PostgreSQL 上实测：candidates rows=1024（合同上界）、bytes=102（32,768 预算 / P99 行序列化字节）、context=115（7,168 字符预算 / P99 每资产上下文项）、deadline=1024（1,024 行时 P99 读取时延仍 < 5,000ms deadline）；**closing ceiling = floor(min=102 × 0.8) = 81**。证据 `ci-evidence/ws6-poc-m-capacity.json`（含分布、预算、环境与 report sha256 `d0718030…34cd`、golden dataset hash 绑定）；纯计算 fail-closed 单测 `tests/test_ws6_capacity_probe.py`。当前生产 `manifest_max_asset_refs=16` ≤ 81，处于证据安全侧。`docs/90` 正式 closing record 写入与发布 Manifest 冻结留负责人 G3 验收 |
| 2. 固定 Knowledge Snapshot + 两次 Run 间改资产状态：Citation 相同、View `observed_at`/watermark/hash 可区分；KnowledgePort 不返回当前状态 | passed | C4 v1-pin 测试（citation 逐字节固定）+ 快照区分测试（本文随附：fresh transaction 读到新值）+ D3/G3 E2E 真实环境（Live 状态只经 read tool，Knowledge 快照冻结） |
| 3. 多 statement 间并发更新 source：accepted View 来自同一 READ ONLY REPEATABLE READ snapshot，transaction 不跨 checkpoint | passed | `test_concurrent_commit_between_statements_stays_on_one_snapshot`（本文随附：镜像 adapter 事务语句序列，中途提交的写入对同事务重读不可见；`pg_current_xact_read_only()` 断言）；adapter 补齐 `SET LOCAL transaction_read_only = on`（修复 docstring 与 docs/31 的 READ ONLY 偏差） |
| 4. kill 边界（读取返回前/后 checkpoint 前/checkpoint 后） | 部分覆盖 | A5 已在真实推理图证 claim/checkpoint/finish 三边界 SIGKILL 单写者（`cf39576`）；asset-risk 变体的 read-boundary kill 矩阵未单独执行，留 F 线 |
| 5. row/byte/token/deadline 超限 → 只得到 `asset_state.query_too_broad` / `ToolQueryTooBroad`；View/partial/第二成功/Inference 调用数 0 | passed | `test_oversized_view_is_rejected_as_too_broad`、`test_typed_adapter_failure_passes_through_unchanged`；graph 层 `stage=failed` 不进 inference |
| 6. 不存在/越权/跨 Tenant/竞态删除/重复 ref → 同形状 `ResourceSelectionUnavailable`，无子集/计数/存在性泄露 | passed | `test_missing_asset_yields_selection_unavailable_without_leakage`、`test_cross_tenant_rows_are_invisible_under_rls`（真实 RLS）、`test_partial_delivery_is_rejected_without_subset_leakage`（竞态删除等价类：all-or-nothing） |
| 7. API/事件/UI/日志/metric 不泄露失败 ref、匹配数、SQL、正文；`domain_view_accepted` 只带 safe provenance | passed（结构性） | 封闭契约（`extra="forbid"`）+ 固定 safe_message；F.1 的 domain-view 事实/UI 事件/renderer 均为 closed model（SQL/表名/正文无法进入展示面）；G3 E2E 断言里程碑字段 |
| 8. 已 checkpoint 的 Run 无 refresh-in-place；重新 submit 产生新 Run/spec/authorization/View | passed（结构性） | 命令面为封闭 start/continue/cancel 集合（无 refresh command）；D5 绑定在 submit 时固定，旧 Run spec/hash 不变 |

## 缺口闭环记录

1. ~~容量测量 closing record~~：✅（见 1d；docs/90 §11.3 已写入）。
2. ~~1c attestation 篡改矩阵~~：✅（见 1c 行）。
3. ~~asset-risk kill 边界矩阵~~：✅（见 4 行）。
4. 剩余：`docs/90` evidence_state 的最终翻转语义随发布流程（负责人已批准关闭记录
   写入，见 docs/90 §11.2）。

## 测试与门禁

- `tests/test_ws6_asset_read_tool.py`（9 用例）、`tests/integration/test_ws6_asset_state_source.py`
  （4 用例，含新增快照隔离）、`tests/test_ws6_golden_dataset.py`（3 用例）。
- 本文随附 commit 的 `make verify` 与真实 PostgreSQL integration 结果见 WBS §13。
