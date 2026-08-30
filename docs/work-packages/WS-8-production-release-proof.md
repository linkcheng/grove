# WS-8 Production Release Proof 任务书

任务状态、依赖和交付进度以 [`ROADMAP.md`](../../ROADMAP.md#work-packages) 为准。
**spec 状态：draft——负责人接受前不构成批准范围，不改变任何 evidence_state。**

## 目标结果

在 WS-7 的 MVP 功能闭环之上，补齐可投入真实使用的 release 必选证据，形成
docs/90 定义的发布结论链：G4 安全全矩阵、G5 中的 PITR 与 Role 故障/扩缩容
矩阵、30 天等效容量、POC-H Evaluation/Publication 闭环、外部 issuer
ceremony、Core/Product `ImplementationAcceptanceRecord` 与 bounded
rollout/rollback。本包是**验证与发布治理包**，不是功能扩展包。

## 背景与当前问题

- 2026-08-26 负责人方向调整：WS-7 重定义为 MVP Functional Completion，
  安全/性能/发布治理义务整体移入本包（ROADMAP 第 19 行为权威范围）。
- WS-7 已于 2026-08-30 验收合入 main（squash `4382df3`）：输出稳定性 gate、
  typed answer UI、单命令走查栈；四轮真机探针零垃圾泄漏，剩余失败类为
  网关 prompted-JSON 纪律（`invalid_result`），随时段波动。
- WS-7 移交的已知债（BLOCKED.md WS-7 节）：无按次组合选择、message 事实族
  未硬化、E2E 预算常量无机制同步、gate/指令阈值不同源、网关配额未表征、
  走查 UX 无题目展示。

## 范围

### In Scope

（每项挂权威来源；细则以 docs/90 对应行为准）

- **G4 安全全矩阵**（docs/90 §G4 行，line 871）：cross-Tenant/ID
  enumeration、RLS/角色分离复核、injection、credential、timing/disclosure、
  tenant-switch reset、audit evidence。
- **PITR 全矩阵**（N-12，line 169/213）：备份/恢复演练达到 RPO ≤ 5 分钟、
  服务 RTO ≤ 60 分钟；恢复后 lease/fence/checkpoint 单写者一致性验证。
- **Role 故障/扩缩容矩阵**（N-12）：API/runtime/projection/governance
  四角色的故障注入与扩缩容；任一 workload 不耗尽全部连接。
- **30 天等效容量**：以 WS-6 容量探针基线（closing record=81）定义并执行
  等效 soak；**前置输入：网关配额特征表征**（WS-7 探针观测到的
  transient 限流窗口）。
- **输出稳定性根治收尾**（BLOCKED.md WS-7 第 6 条裁定的移交项）：网关
  唯一真实的 `json_object` 模式接线（adapter + 签发选项扩展），替换
  prompted-JSON；重跑四轮口径探针验证严格成功率。
- **POC-H Evaluation/Publication 完整闭环**（docs/90 §393 起）。
- **外部 issuer ceremony**：从本地密钥迁移到正式签发流程。
- **Core/Product IAR 与 bounded rollout/rollback**。
- **`make release-check` 全绿**：clean source、完整 CAS evidence、可发布
  Manifest（含 0016/0017 迁移 hash 链）。
- **WS-7 债项处置**：message 事实族硬化（reducer content 校验、重放/幂等
  测试矩阵）与 E2E 预算常量断言纳入本包；其余债项（组合选择、走查 UX）
  按负责人取舍。

### Out of Scope

- 新业务功能（按次组合选择、多模态输入契约扩展）——另立项。
- WS-7 已批指标放宽的回收（覆盖率 80、租约 300、节点性 integration 保持）。
- 任何 production Gate 的未验证宣称（AGENTS 红线）。

## 依赖与前置条件

- WS-7 已合入 main（`4382df3`）✓。
- WS-6 `verified` 仍待负责人批准（WS-7 任务书未决问题 3，未随本包处置）。
- 真实网关凭据与签发链（runbook 流程）；容量目标需要网关配额数据输入。

## Exit Invariants

1. G4 矩阵全项通过并留 audit evidence。
2. PITR 演练达到 RPO/RTO 量化目标，恢复后单写者/租约一致性不变。
3. 30 天等效容量（含配额表征）达到 docs/90 容量行目标。
4. POC-H 闭环、issuer ceremony、Core/Product IAR 完成并获负责人批准。
5. `json_object` 接线后严格口径探针（每轮 30 run 零失败）达到稳定。
6. `make release-check` 在 clean source 上全绿。

## 验收标准

- docs/90 对应 Gate 的量化证据齐备且经 reverse validation（篡改后拒绝）。
- 负责人批准 Core/Product `ImplementationAcceptanceRecord`。
- 基线命令（`.codex/work-packages.toml` final）：`make verify`、
  `make release-check`、`make cleanroom-check`、
  `git diff --check origin/main...HEAD` 全绿。
- 不宣称任何未验证的 production/security/performance 结论。

## 未决问题

1. 30 天等效容量的目标数字与网关配额假设（需真实配额数据）。
2. 外部 issuer ceremony 的参与方与时间窗。
3. WS-7 债项中组合选择与走查 UX 是否提升为本包必做（默认不做）。
4. rollout/rollback 的目标环境（当前无 staging 定义）。

## 来源

### 权威来源

- [`ROADMAP.md`](../../ROADMAP.md) 第 19 行（范围）与 2026-08-26 方向调整记录。
- [`docs/90_P0_Blockers_and_Acceptance.md`](../90_P0_Blockers_and_Acceptance.md)
  §G4（line 871）、§G5、N-12（line 169/213）、POC-H（§393 起）、容量行。
- `BLOCKED.md` WS-7 节（债项与稳定性裁定）。

### Proposed

- 工程排序建议：网关配额表征 → json_object 接线（先解输出稳定性收尾，
  容量与 soak 才有稳定基线）→ G4/PITR/Role 矩阵 → 容量 soak → POC-H/
  ceremony → IAR/rollout。
- 走查 UX（题目与组合展示）作为 G4 前端侧信道复核的伴生项一并考虑。
