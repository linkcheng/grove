# WS-6 Selected Profile E2E 任务书（6.0.4）

任务状态、依赖和交付进度以 [`ROADMAP.md`](../../ROADMAP.md#work-packages) 为准。
本任务书由 [WBS](WS-6-selected-profile-e2e.wbs.md) 收敛而来（语义 diff 见文末），
**负责人于 2026-08-26 批准接受**；Spec Status 已在 ROADMAP 更新为 `accepted`。

## 目标结果

在真实认证、真实 model/provider、真实语料下，对冻结的 Asset Risk Reference
Business Profile（`business-profile.asset-risk@1`，hash `65705bfc…5b30`）跑通
"认证提交 → Graph（含真实 inference）→ checkpoint → typed result/report →
Interaction/UI → Run Inspect" 的完整纵向闭环，形成 G3 证据；不形成 Core/Product
release 结论（属 WS-7）。

## 范围

### In Scope

- 图内真实推理：生产 `TypedInferencePort` 在真实 Run 的图执行中被调用（M1），
  含 Skill 自有指令（非 G2 哨兵）与崩溃恢复矩阵。
- 真实认证与租户：gateway 共享密钥信任边界；fixture 身份封闭语义不变。
- Knowledge Baseline（POC-E MVP 步骤）：不可变 Snapshot、引用链、typed read tool。
- Skill/Profile 真实化：五节点根图、worker 侧 kernel 组合、spec 变体证据链、
  `domain_view_accepted` 发射/投影/renderer 全链。
- 通用前端：RunInteractionModel 契约、Vue 3 三视图组、pending interaction 与
  reconnect UX、Profile 拥有的 typed renderer（未知 schema → partial，无通用
  JSON renderer）。
- G3 证据：门控 E2E、golden dataset（结构域）、容量 closing record、typed
  reducer/reconnect 一致性、human review（负责人）、POC-M 记录、证据包。

### Out of Scope（WS-7 前置）

30 天容量、PITR/备份恢复全矩阵、Deployment Role 故障/扩缩容全矩阵、
G8/POC-H Evaluation/Publication、外部 issuer ceremony、Core/Product IAR、
多 Release Track 能力。

## 退出条件（Exit Invariants）

1. G3 最小证据齐备且通过负责人验收：认证 E2E、golden dataset 结构评估、容量
   closing record（`81`，`ci-evidence/ws6-poc-m-capacity.json`）、typed
   reducer/reconnect 证据、human review 结论。
2. 上述证据基于包含 Skill 自有推理指令的代码（哨兵泄漏修复后）；门控 G3 E2E
   在该代码上重新捕获通过。
3. `make verify` / `make integration` / `make frontend-check` 全绿；记录 known
   limitations（含模型输出稳定性）。
4. ROADMAP/WBS 记录与实际交付一致；未完成事项显式记录。

## 验收标准

- G3 gate 行（docs/90 §12.1）逐项映射见
  [证据包](WS-6-g3-evidence-pack.md)；POC-E/POC-M 步骤矩阵见各自记录。
- `verified` 需负责人显式批准；`docs/90` evidence_state 由负责人翻转。

## 与 WBS 的语义 diff

- **收敛**：WBS §2–§10 的任务表、依赖图、里程碑与估时压缩为本任务书的范围/退出
  条件；执行状态细节保留在 WBS §13，不重复。
- **新增**：退出条件第 2 条（Skill 自有指令基线）与 human review 稳定性发现的
  处置要求——评审表"稳定性发现"节的三个选项由负责人选定后记入验收记录。
- **删除**：WBS 中已被执行 supersede 的估时/风险登记细节（Git 历史留痕）。
- **不变**：范围边界、依赖（WS-5 收窄版）、Out of Scope 清单与 ROADMAP
  2026-08-20 范围调整节一致。
