# WS-8 Production Release Proof 任务书（草案）

任务状态、依赖和交付进度以 [`ROADMAP.md`](../../ROADMAP.md#work-packages) 为准。
**本文件为工程草案，未经负责人批准不改变任何范围或 evidence_state。**

> 来源：2026-08-26 负责人方向调整——WS-7 重定义为 MVP Functional
> Completion（功能优先），原 WS-7 承接的安全/性能/发布治理义务整体移入本包。
> ROADMAP 第 19 行为本包的权威范围定义。

## 目标结果

在 WS-7 的功能完整闭环之上，补齐**可投入真实使用的 release 必选证据**：
G4 安全全矩阵、G5 恢复矩阵中的 PITR/Role 部分、30 天等效容量、POC-H
Evaluation/Publication、外部 issuer ceremony、Core/Product IAR 与 bounded
rollout/rollback。验收形式为 docs/90 对应 Gate 的量化证据与负责人批准的
`ImplementationAcceptanceRecord`；本包形成的是 release 结论，不是功能扩展。

## 背景与当前基线

- WS-7 已交付：Asset Risk 纵向闭环的功能质量（输出稳定性 gate、走查栈、
  typed answer 呈现）与放宽后的开发期指标。
- 已知输入（来自 WS-7 走查与 BLOCKED.md WS-7 债项，供范围取舍参考）：
  1. 无按次资产组合选择（intent 契约扩展，属功能项，默认**不在**本包）；
  2. message 事实族的硬化（reducer content 校验、重放/幂等测试矩阵）；
  3. E2E 预算常量（invoke budget/lease）无机制同步；
  4. 网关限流/配额特征未表征（影响容量目标定义）。

## 范围

### In Scope（按 ROADMAP 定义导出）

- **G4 安全全矩阵**：cross-Tenant/ID enumeration、RLS/role 分离复核、
  injection、credential、timing/disclosure、tenant-switch reset、audit
  evidence（docs/90 §G4 行）。
- **PITR 全矩阵**：RPO ≤ 5 分钟、服务 RTO ≤ 60 分钟的备份/恢复演练与
  报告（N-12）；含恢复后 lease/fence/checkpoint 一致性验证。
- **Role 故障/扩缩容矩阵**：API/runtime/projection/governance 角色的
  故障注入与扩缩容行为；任一 workload 不耗尽全部连接（N-12）。
- **30 天等效容量**：以 WS-6 容量探针（closing record=81）为基础定义
  等效 soak 目标并执行；网关配额特征表征作为前置输入。
- **POC-H Evaluation/Publication 完整闭环**（docs/90 §393 起）。
- **外部 issuer ceremony**：从本地密钥迁移到正式签发流程；Core/Product
  `ImplementationAcceptanceRecord`。
- **bounded rollout/rollback**：发布与回滚的受控路径验证。
- **`make release-check` 全绿**：clean source、完整 CAS evidence、
  可发布 Manifest（含 0016/0017 迁移 hash 链）。

### Out of Scope

- 新业务功能（含按次组合选择、多模态输入契约扩展）——另立项。
- WS-7 已批指标放宽的回收紧（覆盖率 80、租约 300 保持）。

## 依赖与前置条件

- WS-7 全部提交合入 main 且负责人完成走查验收（Exit Invariant 2）。
- WS-6 `verified` 状态由负责人定夺（未决问题 3）。

## Exit Invariants（草案，负责人定稿）

1. G4 矩阵全项通过并留 audit evidence。
2. PITR 演练达到 RPO/RTO 量化目标，恢复后单写者/租约一致性不变。
3. 30 天等效容量证据（含网关配额表征）达到 docs/90 容量行目标。
4. POC-H 闭环、issuer ceremony、Core/Product IAR 完成并批准。
5. `make release-check` 在 clean source 上全绿。

## 未决问题（负责人定稿时回答）

1. 30 天等效容量的目标数字与网关配额假设（需要真实配额数据输入）。
2. 外部 issuer ceremony 的参与方与时间窗。
3. WS-7 债项中哪些提升为本包必做（默认仅 message 事实族硬化）。
4. rollout/rollback 的目标环境（当前无 staging 定义）。

## 来源

- [`ROADMAP.md`](../../ROADMAP.md) 第 19 行（权威范围）与 2026-08-26 方向调整记录。
- [`docs/90_P0_Blockers_and_Acceptance.md`](../90_P0_Blockers_and_Acceptance.md)
  §G4/G5、N-12、§393 POC-H、容量行。
- WS-7 BLOCKED.md 已知功能债（范围取舍输入）。
