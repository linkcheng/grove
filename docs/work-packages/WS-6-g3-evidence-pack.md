# WS-6 G3 证据包（6.F.5，提交负责人验收）

> 性质：工作包级证据索引与 known limitations 汇总，供负责人执行 G3 验收。
> 本文件**不**改变 `docs/90` 的 evidence_state，不形成 Core/Product release 结论，
> 不标记 WS-6 `verified`；以上均需负责人显式批准。
> 汇总时点：2026-08-26（分支 `codex/ws-5-core-release-proof`，HEAD 见 Git 历史）。

## G3 最小证据映射（docs/90 §12.1 G3 行）

| G3 要求 | 状态 | 证据 |
|---|---|---|
| 从认证提交经过所有已声明 Knowledge/Tool/Action seam 到 inference/checkpoint/result/report/UI/Inspect 的 E2E | passed | 门控 `test_g3_vertical_loop_from_gateway_submit_to_inspect`（`c1bf7f9`，真实 PostgreSQL + 真实 GLM + 重签发链）：gateway 401 fail-closed → 认证提交 → Knowledge 快照 → RLS all-or-nothing 资产读取 → 图内真实推理 → typed report → checkpoint → terminal + domain-view 事实 → projection → Observation API（replay-stable）→ RunInteractionModel → Profile renderer → Run Inspect。Effect Class 仅 pure/read，无 Action seam（Profile 未声明，not_applicable） |
| golden dataset | passed（结构域） | `golden.asset-risk-reference@1` 内容寻址冻结 + 确定性结构评估器（`8bdaad2`）；答案质量评审属 human review（见缺口） |
| 业务阈值 | passed（证据侧） | POC-M 1d 容量 closing record：closing ceiling = 81（约束项 result bytes），`ci-evidence/ws6-poc-m-capacity.json`（report sha256 `d0718030…34cd`）；生产 `max_asset_refs=16` 处于证据安全侧。`docs/90` 写入与 Manifest 冻结随负责人验收 |
| human review | **pending（负责人）** | 待对 golden dataset 三用例的真实答案进行业务评审；流程入口与本证据包一并提交 |
| typed reducer/reconnect evidence | passed | 6.F.3（`5278085`）：无中断/乱序+重复/reconnect+backfill 三种投递下 view 与 renderer 输出字节一致；reconnecting 期间零越序应用 |

## POC/工作流记录索引

- POC-E 步骤矩阵（Knowledge Baseline）：[WS-6-poc-e-knowledge-baseline.md](WS-6-poc-e-knowledge-baseline.md)——步骤 1–6 passed（seam 级）、7–11 not_applicable。
- POC-M 步骤矩阵（Asset Risk 专项）：[WS-6-poc-m-asset-risk.md](WS-6-poc-m-asset-risk.md)——1/2/3/5/6/7/8 passed、1b/1d 已注边界；缺口见下。
- WBS 执行状态（A/B/C/D/E/F 全线）：[WS-6-selected-profile-e2e.wbs.md §13](WS-6-selected-profile-e2e.wbs.md#13-执行状态)。
- Profile 冻结：`business-profile.asset-risk@1`（hash `65705bfc…5b30`）。

## 门禁结果（汇总时点）

- `make verify`：1107 passed / 163+ deselected（integration），coverage 89.33%（门槛 89%）。
- `make frontend-check`：34/34 + tsc + build 绿。
- 真实 PostgreSQL integration：156 passed / 9 skipped / exit 0（含 migration 0015 头、快照隔离、SIGKILL 矩阵、RLS/审计链）；容器与 volume 清理无残留。
- 门控真实 provider 套件：G3 E2E 1 passed（重签发链 `~/grove-g2/release-g3`，仓库外；凭据未入仓库/日志/证据）。

## Known limitations（如实清单）

0. **评审准备阶段的两项新发现（2026-08-26 晚）**：
   a. **G2 哨兵指令泄漏（已修复）**：manifest 绑定 factory 此前给所有请求携带
      `Return G2_OK` 一致性哨兵指令，asset-risk 真实业务指令从未到达模型；已改为
      Skill 拥有自己的指令（`ASSET_RISK_INSTRUCTION`），回归测试锁定哨兵不可达
      （`tests/test_ws6_asset_risk_graph.py`）。此前 G3 E2E 绿灯捕获（`c1bf7f9`）为
      修复前代码，**需在新指令下重跑一次以更新证据**——当前被网关时延窗口阻塞
      （见 b）；负责人指定 **glm-5.3-flash** 复测后已在 Skill 自有指令基线上重新捕获
      通过（39.57s，一次通过/一次失败——输出随机性仍在，见评审表稳定性发现）。
   b. **租约预算 vs 真实生成时延**：driver 租约上限 90s、invoke+checkpoint 为不可
      拆分 critical section；网关劣化窗口内真实生成（含重试）常超 70s 预算，G3 E2E
      无法在窗口内完成重验。评审表两次完整捕获证明答案质量随机波动（好/空/乱码），
      供负责人在 human review 中决策（选项见评审表"稳定性发现"节）。flash 复测补充：
      实质答案质量较好，但 prompted JSON 指令文本泄漏进答案（schema 说明/占位符），
      根治需 native JSON schema 模式——**已查证（2026-08-26）：网关伪支持**
      （`response_format=json_schema` 返回 HTTP 200 但不强制约束，输出仍为纯文本；
      json_object 为唯一真实模式）。缓解路径为评审表选项 C（结构评估器加最低格式
      健全性检查，可自动化 fail-closed）。

1. **human review 未执行**（G3 必需，负责人）：golden dataset 答案质量与业务阈值确认。
2. **docs/90 evidence_state 全部未翻转**：POC-E/POC-M/G3 的 `verified` 与发布 Manifest 冻结（含容量 closing record 写入）需负责人批准。
3. **POC-M 剩余缺口**：1c attestation 篡改矩阵、4 的 asset-risk 变体 kill 边界矩阵（A5 已在推理图证方法论）。
4. **已知展示债**：History-Inspect 以原始 JSON 呈现有界 public query 结果（非 UI event 路径）；typed Inspect 摘要待 UI 收敛。
5. **测试环境边界**：容量 closing record 在本机 Docker PostgreSQL 16 测得；换环境需重跑（证据含环境与分布，可复算）。
6. **6.0.4 任务书定稿未完成**：ROADMAP 中 WS-6 Spec 状态仍为 `draft`；验收时一并收敛。
7. **WS-7 前置义务**（不阻塞 G3 验收记录本身，但阻塞 Product release）：30 天容量、PITR 全矩阵、外部 issuer ceremony、Core/Product IAR。
