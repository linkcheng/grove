# WS-6 Selected Profile E2E — WBS（工作分解结构）

> 性质：规划稿，供 WS-6 任务书定稿；Spec 状态以 [`ROADMAP.md`](../../ROADMAP.md#work-packages)
> 为准（当前 `draft`）。依据：docs/90 §13 MVP Baseline B、§14.2 Gate 映射、docs/06
> 前端契约、2026-08-20 范围调整（容量/PITR/role 全矩阵属 WS-7）、2026-08-20 整体审计发现。

## 1. 目标与边界

**目标**：冻结一个 Business Profile，在真实认证、真实 model/provider、真实语料下跑通
“认证提交 → Graph（含真实 inference）→ checkpoint → typed result/report → Interaction/UI
→ Run Inspect” 的完整纵向闭环，并形成 G3 证据。

**Out of scope（WS-7 前置，不因 G3 顺手实现）**：30 天等效容量、PITR/备份恢复全矩阵、
Deployment Role 故障/扩缩容全矩阵、Core/Product `ImplementationAcceptanceRecord`、
多 Release Track（Long-Term Memory、Durable Action 等）能力。

## 2. 分解总览与依赖

```text
D0 决策门（Profile/provider/认证选型）
  ├─→ A 图内真实推理（Kernel 收口）⭐关键路径，不依赖 Profile 选择，可立即开工
  │      └─→ D Skill/Profile 真实化 ──┐
  ├─→ B 真实认证与租户（与 A 并行） ──┼─→ F G3 端到端验收
  ├─→ C Knowledge Baseline（POC-E） ─┤      （依赖 A+B+C+D+E 全部）
  └─→ E 通用前端（Vue/RunInteractionModel，SSE 契约已稳即可开工）
```

## 3. D0 决策门（1～2 人日，负责人参与）

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.0.1 | Business Profile 选择与冻结 | 内容寻址 `business_profile_ref/hash`（Asset Risk Reference 或自选领域+独立 Profile 文档）；负责人批准记录 | 0.5d |
| 6.0.2 | 真实 model/provider 确定 | endpoint/model/credential slot 定案；用 `scripts/ws5_issue_provider_binding.py` 签发（密钥仓库外） | 0.5d |
| 6.0.3 | 认证方案选型 | MVP Baseline B 要求“真实认证与 Tenant 上下文”：OIDC/JWT/网关注入三选一，输出 ADR 级决策 | 0.5d |
| 6.0.4 | WS-6 任务书定稿 | 从本 WBS 收敛为 accepted 任务书，登记 ROADMAP | 0.5d |

## 4. Workstream A：图内真实推理（Kernel 收口）⭐

收敛 2026-08-20 审计发现 2（生产 loop 节点直调 vs ADR-0001 "LangGraph 唯一 Kernel"），
并首次验证核心价值假设。**不依赖 Profile 选择，D0 期间即可开工。**

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.A.1 | 生产 loop 收敛到 compiled `graph.ainvoke` | `worker/loop.py` 替换 node 直调；现有 SIGKILL 矩阵与全量门禁保持绿 | 1d |
| 6.A.2 | graph registry 最小扩展：inference node | 新增 versioned 图节点调用 `TypedInferencePort.infer`；未知 version/node/type fail closed；不扩大 registry 公开面 | 1～2d |
| 6.A.3 | 真实 provider 驱动 | worker 经 `production_inference_lifespan` + 签发链构造 port，单节点图真实调用 | 1d |
| 6.A.4 | 端到端链路验证 | submit → claim → ainvoke(真实 infer) → checkpoint → finish_delivery → SSE；每节点物理请求=1、usage/pricing 入审计事实 | 1d |
| 6.A.5 | 崩溃恢复在真实 inference 图上重跑 | kill 时点扩展到 infer 前/后；单写者、已提交事实不丢失 | 1d |

**里程碑 M1**：图内真实推理 E2E 通过 — “治理 Skill → LLM 执行”核心闭环首次验证。

## 5. Workstream B：真实认证与租户（3～5 人日，与 A 并行）

收敛审计发现 1（生产认证未实现，当前 fail closed 503）。

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.B.1 | 认证 adapter 契约 | `auth_mode` 扩展真实认证值；Active Tenant Context 仍只能服务端构造，请求正文/header 自报继续拒绝 | 1d |
| 6.B.2 | 按选型实现 adapter | OIDC/JWT 验签或网关注入信任边界；fixture 身份封闭语义不变（production 拒绝） | 1.5～2d |
| 6.B.3 | 真实 principal 联测 | RLS/grants 与真实身份联测；401/403/422 语义、跨租户隔离、contextvar 每请求重置回归 | 1～2d |

## 6. Workstream C：Knowledge Baseline / POC-E（5～8 人日，依赖 6.0.1）

MVP Knowledge Baseline：一个 production adapter、一个不可变 Snapshot、完整约束链。

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.C.1 | production Knowledge adapter | ✅ 完成（`9df8e44`）：ImmutableSnapshotKnowledgeAdapter——单一冻结快照、文档/关键词检索、租户可见性+scope 拒绝、Citation 全链（snapshot/source version/locator/hash）、ok/empty 区分、预算与截断 | 2d |
| 6.C.2 | 不可变 Snapshot + 引用链 | ✅ 完成（`9df8e44`）：KnowledgeSnapshot 契约（self-hash 覆盖 items/sources/ACL，latest 禁止，item→source 闭包）+ 受信 builder（唯一铸 hash 处，产出经公共构造器复核）；9 测试覆盖篡改/越权/空结果矩阵 | 2d |
| 6.C.3 | typed read tool + Run Data View | ✅ 完成（`9805195`）：asset.state.read@1 强制层（闭集 schema 预 provider 拒绝、单调收紧上限、all-or-nothing 无子集泄露、字节预算）+ PG adapter（RLS/repeatable read/固定模板）；Run Data View provenance 入 spine ToolResult；9 测试；profile 表 migration 随 D 线 | 1～2d |
| 6.C.4 | POC-E 步骤级证据 | ✅ 完成：步骤级 passed/not_applicable 矩阵见 [WS-6-poc-e-knowledge-baseline.md](WS-6-poc-e-knowledge-baseline.md)（步骤 1～6 passed，7～11 not_applicable——Long-Term Memory track 未启用）；随附补齐 5 个缺口回归（timeout/unavailable outcome、truncated 标志、v2 发布后 v1 citation 固定、locator/content hash 断言收紧）；不改变 docs/90 evidence_state，关闭记录留 F 线负责人批准 | 0.5～1d |

## 7. Workstream D：Skill/Profile 真实化（8～12 人日，依赖 6.0.1 + A）

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.D.1 | Profile 文档冻结 | typed input/output、root Skill/Graph、Knowledge/Tool closure、Effect Class、交互/UI、业务质量阈值、human review 流程 | 1～2d |
| 6.D.2 | root Skill/Graph 真实实现 | ✅ 核心完成（`db9cf67`）：固定五节点流（validate→knowledge→asset read→inference→typed report）compiled kernel，seam 级 fail-closed 退出，`asset_risk_report.v1` 绑定 provenance/view hash；6 测试。剩余：SkillRuntimeManifest 内容寻址与 spec/evidence 链（随 6.D.4/D.5） | 3～4d |
| 6.D.3 | 真实语料与 golden dataset | ✅ E2E 完成（`bfd5289`）：docs/31 §2 参考闭环端到端贯通——submit（asset-risk 绑定）→ claim → 不可变 Knowledge 快照检索 → RLS all-or-nothing 资产读取 → 真实 glm-4.7 图内推理 → typed report → checkpoint → terminal + 审计链（42s 真实环境）；golden dataset 形式化冻结随 F 线 G3 | 2～3d |
| 6.D.4 | 权限/Effect 真实化 | ✅ 完成（`4a265be`）：worker 侧 AssetRiskKernel（graph factory + typed input source + sealed inference caller）进注册表第三 kind；无组合 fail-closed dead-letter（asset_risk_unavailable）；asset_risk claim 全链路 dispatch 测试通过 terminal | 1～2d |
| 6.D.5 | 依赖漂移防护 | ✅ 完成（`792cec7`）：asset-risk 图变体 + 逐 preset 评估证据进 fixture 发布闭包（subject hash 绑定图绑定）；证据加载器变体感知，同一评估门禁双向把关（A4 无证据被拒、本版有证据通过）；submit 侧 `fixture_graph_binding` 开关 fail-closed | 1d |

## 8. Workstream E：通用前端（10～15 人日，SSE 契约已稳即可开工）

按 docs/06：深 `RunInteractionModel` module，先通用后 Profile。

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.E.1 | RunInteractionModel 契约冻结 | typed reducer、snapshot+SSE 合并、reconnect/partial 语义 golden tests | 2d |
| 6.E.2 | Vue 3 骨架 | Execution Launch、Run Interaction、History/Inspect 三页面组 | 3～4d |
| 6.E.3 | typed renderer | ✅ 完成（`728059b`）：renderer 接口 + 封闭注册表（`app/observation/rendering.py` 及 TS port）；Profile 拥有的 `AssetStateView@1` renderer（“资产状态已固定”里程碑，仅 observed_at/记录数/完整性/safe provenance）；未知 `view_schema_ref` → partial marker（仅 schema ref，无 payload 回显，无通用 JSON renderer）；reducer 累积 `domain_view_accepted`（tool_request_id+result_hash 去重、tenant 切换清空）；附带对齐 TS port completeness 语义（非终态/tenant 切换 partial） | 2～3d |
| 6.E.4 | 交互与恢复 UX | pending interaction、reconnect、断线补齐（复用 WS-4 coalescer 语义）；接入 B 的真实认证 | 2～3d |
| 6.E.5 | 不做清单守住 | ✅ 审计完成（2026-08-26）：前端仅三视图组（Launch/Run Interaction/History-Inspect），无 Graph Studio、通用 inbox、通知中心、运营大屏；无 `v-html`；interaction 投影路径无通用 JSON renderer（未知 schema/event → partial marker，E.3 封闭注册表）。已知展示债：History-Inspect 以原始 JSON 呈现有界的 public run query 结果（非 UI event 路径，数据面已被 API 契约裁剪），typed Inspect 摘要随 F 线 UI 收敛 | — |

## 9. Workstream F：G3 端到端验收（5～8 人日，依赖 A+B+C+D+E）

| 编号 | 任务 | 产出/验收 | 估时 |
|---|---|---|---|
| 6.F.1 | G3 纵向闭环 | 认证提交 → 全部已声明 seam → inference/checkpoint/result/report/UI/Inspect 的 E2E | 2d |
| 6.F.2 | 业务质量与人工评审 | golden dataset 阈值 + human review 流程证据 | 1～2d |
| 6.F.3 | typed reducer/reconnect 证据 | UI 投影 reconnect/乱序/重复下的一致性 | 1d |
| 6.F.4 | Profile-specific POC | Asset Risk 则含 POC-M；自选领域同等级 POC | 0.5～1d |
| 6.F.5 | 证据包与验收记录 | 步骤级 passed/not_applicable 矩阵 + known limitations；进入负责人验收 | 0.5～1d |

**里程碑 M4** = WS-6 `implemented`；`verified` 需负责人批准（ROADMAP 规则）。

## 10. 里程碑与估时汇总

| 里程碑 | 内容 | 累计估时 | 关键路径 |
|---|---|---|---|
| M0 | D0 决策齐（Profile/provider/认证/任务书） | 1～2d | ✅ |
| M1 | 图内真实推理 E2E（A） | 4～7d | ✅ |
| M2 | 真实身份 + Knowledge Baseline（B+C） | 与 M1 并行 +3～5d | — |
| M3 | Profile 真实闭环 + 通用 UI（D+E） | +18～27d | ✅（D 在关键路径） |
| M4 | G3 验收（F） | +5～8d | ✅ |

总量约 **35～50 人日**；单人流关键路径约 **28～38 人日**。A/B/E 三条线在 D0 后可并行。

## 11. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| Profile 选择悬置 | 阻塞 C/D | A/B/E 均已解耦先行；M1 不依赖 Profile |
| 真实 provider 不稳定/凭据缺失 | 阻塞 A 验证 | 签发工具就绪；本地 transport 协议测试先行（不产生 G3 结论） |
| 认证选型引入外部 IdP 依赖 | 阻塞 B 联测 | D0.3 三选一含“网关注入”最低依赖选项 |
| 文档义务漂移（N-10/N-29 容量类） | 与 WS-7 边界混淆 | 以 ROADMAP 2026-08-20 范围调整节为准；G3 证据不含容量声明 |
| typed renderer 膨胀为通用渲染 | 违反 docs/06 禁令 | 6.E.3 验收明确“未知=partial 不猜测” |

## 12. 前置与交接

- **前置**：WS-5 收窄版已达成退出条件（待负责人批准 `verified`）；签发工具链与
  production inference seam 可直接复用。
- **交接 WS-7**：容量/PITR/role 全矩阵、Evaluation/Publication（若 G3 暴露 Skill
  进化需求则提升优先级）、Product MVP `ImplementationAcceptanceRecord`。

## 13. 执行状态

| 任务 | 状态 | 证据 |
|---|---|---|
| 6.0.2 真实 model/provider 确定 | ✅ 完成（2026-08-21） | BigModel `https://open.bigmodel.cn/api/coding/paas/v4` / `glm-5.2` / `gateway-primary`；仓库外密钥（`~/grove-g2/keys`）签发绑定链；真实 G2 冒烟通过：physical_sends=1、sentinel G2_OK、usage 187+41 tokens、cost 2µs 级记账；凭据只经 `.env`（gitignored），未进仓库/日志/证据 |
| 6.0.1 Business Profile 选择与冻结 | ✅ 完成（2026-08-21） | 负责人选定 Asset Risk Reference（"A"）；冻结记录 `docs/work-packages/WS-6-business-profile-freeze.md`，ref `business-profile.asset-risk@1`，hash `65705bfc…5b30`；POC-M 与 8 个资产 ADR 全部接受核对 |
| 6.A.1 生产 loop 收敛 compiled `graph.ainvoke` | ✅ 完成（`0106226`） | `make verify` 1015 passed/89.33%；`make integration` 真实 PostgreSQL 152 passed（含 SIGKILL 矩阵与审计链），exit 0 |
| 6.A.2 图注册表 + inference node | ✅ 完成（`6dbe0a1`） | registry fail-closed（unknown/inference_unavailable dead-letter）、infer 节点经 compiled kernel、manifest 以 request-factory 注入不越 composition 边界、fixture 防漂移测试；verify 1026 passed/89.39%、integration 152 passed |
| 6.A.3 路由数据通路（migration 0013 + contract v9 + driver 解析） | ✅ 完成（`ea373d2`） | claim 权威函数带出 graph 三元组（空三元组=命令级 dead-letter）；roundtrip hash 稳定；verify 1026/89.39%、integration 152 passed |
| 6.A.4 真实 provider E2E | ✅ 完成（`1db77b6`，M1 达成） | 门控集成测试：graph.inference@1 run → claim 路由断言 → 真实 GLM-5.2 图内推理 → checkpoint → terminal；审计链 + 真实 token 数断言；两次复验通过。submit 侧绑定开关被评估门禁正确拒绝（evidence subject hash 绑定精确 spec），归入 D 线 |
| 6.A.5 真实推理图崩溃矩阵 | ✅ 完成（`cf39576`） | claim/checkpoint/finish 三边界 SIGKILL + worker B 恢复；审计链逐边界断言、单条 infer token 证据存活；helper 以“过期前 heartbeat 续租”镜像生产语义；门控套件 4 passed（真实 PG + 签发链 + glm-5.2） |
| 6.B.1/6.B.2 gateway 认证模式 | ✅ 完成（`a4f0f40`） | 共享密钥信任边界 + 常数时间比较 + 凭据组合拒绝；verify 1037/89.41%；本 commit 的 integration 重跑因构建网络劣化待补（见 BLOCKED.md） |
| 6.B.3 真实 principal 全量联测 | ✅ 完成（2026-08-26） | 单测覆盖依赖分发矩阵（`a4f0f40`）；网络恢复后完整 integration 重跑两次：首轮 153 passed/2 failed，两失败根因定位并修复（`676f3a6`：0014 新表 TRUNCATE 清单同步、zero-write 矩阵 setup claim 超时预算分离），复跑 **155 passed/8 skipped、exit 0**，容器清理无残留。B 线（gateway 认证 + 真实 principal 联测）完整落地 |
| 6.E.1 RunInteractionModel 契约冻结 | ✅ 完成（`e788a7c`） | 纯 Python 可执行参考实现 + 14 个 golden 测试（§7.1 排序/缺口/未知 schema/意图归一化/生命周期）；verify 1051/89.51%；integration 重跑同受网络阻塞（纯新增模块，零运行路径触碰） |
| 6.E.2 Vue 3 骨架 | ✅ 完成（`86207fa`） | Vite+Vue+TS+Vitest 工作区（lock 固定）；TS 移植与 Python golden 一一对应（14/14）；fetch 流式 SSE（EventSource 无法带 gateway 头）；三视图组；`make frontend-check` 入 `make ci`（verify 保持纯 Python） |
| 6.E.4 pending interaction + reconnect UX | ✅ 完成（`cc4852f`） | interaction 投影类型移植（upsert/resolve 按 revision，5 个 golden 对齐 Python reducer）；传输连接态与序列缺口态分离双徽标；Respond 意图归一化展示；认证配置集中；frontend-check 19/19 + tsc + build 绿 |
| 6.E.3 typed renderer | ✅ 完成（`728059b`） | Python 参考实现 + TS 移植 + Vue 接入（App.vue 注入 Profile registry，通用视图只消费）；`make verify` 1088 passed / coverage 89.27%；`make frontend-check` 30/30 + tsc + build 绿 |
| 6.C.4 POC-E 步骤级证据 | ✅ 完成（2026-08-26） | [WS-6-poc-e-knowledge-baseline.md](WS-6-poc-e-knowledge-baseline.md)：步骤 1～6 passed（seam 级）、7～11 not_applicable；补齐 timeout/unavailable/truncated/v1-pin/locator+hash 五个缺口回归（`tests/test_ws6_knowledge.py` 13 用例）；docs/90 evidence_state 不变，关闭留 F 线 |
| 6.E.5 不做清单守住 | ✅ 审计完成（2026-08-26） | 三视图组之外无预建；无 `v-html`；interaction 路径无通用 JSON renderer；已知债：Inspect 原始 JSON 呈现有界 query 结果，随 F 线收敛 |
| 6.0.4 任务书定稿 | ✅ 完成（2026-08-26）：负责人批准任务书（[WS-6-selected-profile-e2e.md](WS-6-selected-profile-e2e.md)），ROADMAP Spec Status → `accepted` |
| 6.F.2 human review | ✅ 完成（2026-08-26）：负责人按 glm-5.3-flash 复测结果批准通过（`WS-6-human-review-sheet.md`）；prompted JSON 输出稳定性/指令泄漏记为已知限制（网关 json_schema 已查证为伪支持） |
| 6.F.4 POC-M 缺口矩阵 | ✅ 完成（2026-08-26）：1c 篡改矩阵 + kill 边界矩阵（3/3 passed，checkpoint 后恢复零重读——暴露并修复 takeover 整图重跑缺口）；docs/90 §11.2/11.3 关闭记录已写入 |
| M4 | ✅ WS-6 `implemented`（2026-08-26 负责人批准；`verified` 待显式批准） |
| 6.F.*（G3 验收） | 6.F.1 主体 ✅ 完成（2026-08-26）：`domain_view_accepted` 发射链全通——migration 0015 扩 emit 函数 schema 白名单（升级/降级对称）+ ws3 preflight 允许头/迁移兼容 pinned head/`WS6_MIGRATION_HEADS` 三处机械同步（`57a16b1`）+ facts 载荷/构造器/纯映射 + projector 分支与 rebuild 覆盖 + worker terminal 事务内原子发射（`108e2de`）；**门控 G3 E2E 实跑通过（1 passed，真实环境）**：gateway 认证提交（含匿名/凭据组合 401 fail-closed）→ fixture bundle 解析 asset-risk spec → claim → 真实 GLM 图内推理 → typed report → checkpoint → terminal + domain-view 事实 → projection（projection 角色）→ Observation API snapshot/events（replay-stable）→ RunInteractionModel → AssetRisk renderer（"资产状态已固定"里程碑）→ Run Inspect；adapter 指纹漂移后按 runbook 重签发链（`~/grove-g2/release-g3`，仓库外）。6.F.3 ✅（`5278085`）。6.F.2 部分完成（`8bdaad2`）：golden dataset 冻结（`golden.asset-risk-reference@1`，内容寻址）+ 确定性结构评估器（3 用例含篡改 fail-closed）；剩余容量测量 closing record 与 human review（负责人）。6.F.4 记录完成：[WS-6-poc-m-asset-risk.md](WS-6-poc-m-asset-risk.md) 步骤矩阵 + 已知缺口（容量 closing record、attestation 篡改矩阵、asset-risk kill 边界）。6.F.5 ✅ 证据包完成（2026-08-26）：[WS-6-g3-evidence-pack.md](WS-6-g3-evidence-pack.md)——G3 最小证据映射、POC 记录索引、门禁结果与 known limitations（含 POC-M 1d 容量 closing record=81，`ci-evidence/ws6-poc-m-capacity.json`）；POC-M 1d ✅（探针 + fail-closed 单测 + 真实 PG 实跑）。提交负责人验收 | 剩余需负责人：human review、docs/90 evidence_state、6.0.4 任务书定稿 |
