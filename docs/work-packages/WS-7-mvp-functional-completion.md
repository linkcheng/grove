# WS-7 MVP Functional Completion 任务书

任务状态、依赖和交付进度以 [`ROADMAP.md`](../../ROADMAP.md#work-packages) 为准。

> **2026-08-26 负责人方向调整**：WS-7 从"Product MVP Release"重定义为
> "MVP Functional Completion"——先不考虑安全与性能验证方面，优先实现 MVP 基础
> 功能验证；原 WS-7 承接的安全/性能/发布治理义务整体移至新增的 WS-8
> （Production Release Proof）。本重定义与 WS-8 拆分由负责人当日指示。
> 已实现的安全核心（RLS、claim/lease/fence、幂等提交、审计链）**不移除**：
> 它们是崩溃恢复与单写者的正确性机制（2026-08-20 范围调整已批准"保持不变"），
> 移除将作废 WS-2/3/4 已验收记录并破坏既有全部测试；本包仅推迟其**验证矩阵**
> （属 WS-8）。开发期流程降摩擦：常规迭代只跑 `make verify`，节点才跑
> integration/release-check。

## 目标结果

让 Asset Risk Reference Profile 的纵向闭环达到**真实可用的 MVP 功能质量**：
提交 → 执行（真实推理）→ 稳定、无泄漏格式的中文风险评估答案 → 可读的 UI
（结果/历史/Inspect）→ 一条命令起全套环境。验收形式为负责人按清单手工走查
通过；不形成 production release 结论（属 WS-8）。

## 背景与当前问题

WS-6 交付了 G3 结构闭环（门控 E2E、golden dataset、renderer、崩溃恢复），但
**功能可用性**存在真实缺口：

1. **答案输出不稳定**（最大功能缺陷）：同指令下答案随机出现"好/空/乱码"
   （prompted JSON 模式指令文本泄漏进答案、`$your_answer` 占位符）；网关
   `json_schema` 已查证为伪支持（HTTP 200 不强制约束）。
2. **UI 是骨架**：Inspect 以原始 JSON 呈现有界查询结果；无提交表单（资产组合
   选择）；History 仅列表。
3. **上手成本**：无一条命令的本地全套启动方式与使用说明。

## 范围

### In Scope

- **输出稳定性根治**：skill 层运行时结构校验 + 有界重试（非空、最小长度、
  不含格式指令泄漏文本——复用 golden 结构评估器作为运行时 gate，即评审表
  选项 C）；校验失败 fail closed 为 typed failure，不输出垃圾答案。
- **答案质量迭代**：基于 human review 基线调优指令/参数；如需可对比或更换
  model（重签链即可，无代码改动）。
- **UI 功能补齐**：提交表单（资产组合选择 + 预览）、Inspect typed 摘要
  （替换 raw JSON，含 provenance 与 typed report 呈现）、History 详情。
- **部署可用性**：单 compose 启动 api/worker/projection + DB 的本地全套、
  gateway 配置指引、README 快速上手。
- **功能回归保护**：以上功能的最小测试集（不要求安全/性能矩阵）。
- **负责人手工验收**：清单化走查（提交→观察→答案→历史→Inspect）。
- **放宽过于严苛的生产指标**（负责人 2026-08-26 澄清的范围）：
  - 覆盖率门槛 89% → 80%（开发期摩擦，MVP 功能优先）；
  - 租约上限 90s → 300s（真实 LLM 生成时延常超 90s，该上限已实际阻塞
    功能验证；正确性语义不变——invoke 预算仍须严格小于租约减 margin）；
  - 全量 integration/release-check 降为节点性检查（已在上文流程规则）。

### Out of Scope

- 安全验证矩阵（G4 全矩阵、PITR、Role 故障/扩缩容）、30 天容量/soak、
  POC-H Evaluation/Publication 完整闭环、外部 issuer ceremony、Core/Product
  IAR、bounded rollout/rollback——**全部属 WS-8**。
- 未启用 Release Track 的能力实现。
- **正确性核心保持不动**（负责人 2026-08-26 澄清）：通用多租户隔离、幂等
  提交、审计、日志一律保留。"移除"仅指**放宽过于严苛的生产级指标/门槛**
  （见 In Scope 的指标放宽项），不涉及正确性代码。

## 依赖与前置条件

- WS-6（已 implemented；`verified` 建议随本包验收一并批准，不阻塞开工）。
- 真实 provider 凭据与签发链（现有 runbook 流程）。

## Exit Invariants

1. 连续多次运行 golden 三用例，答案 100% 通过运行时结构校验（无空/乱码/格式
   泄漏文本到达用户）。
2. 负责人手工走查清单全部通过并留记录。
3. `make verify` 全绿（integration 在节点跑，不逐 commit 要求）。
4. 已知功能债清单（本包未消化的）显式记录移交 WS-8 或后续。

## 验收标准

- 手工走查清单（本包定稿时附）逐项通过；输出稳定性以连续 N 次运行零结构
  失败为准（N 在清单定稿时固定，建议 ≥10）。
- 不宣称任何 production/security/performance 结论。

## 未决问题

1. 运行时结构校验的阈值（最小长度、泄漏文本判定规则）——实现时随清单定稿。
2. UI 补齐深度（三页面到什么程度算"够用"）——建议以走查清单驱动，先最小。
3. 是否随本包批准 WS-6 `verified`。

## 来源

### 权威来源

- [`ROADMAP.md`](../../ROADMAP.md) 2026-08-26 方向调整记录（负责人指示：MVP
  功能优先，安全/性能后置）。
- [`WS-6-human-review-sheet.md`](WS-6-human-review-sheet.md) 稳定性发现、
  [`WS-6-g3-evidence-pack.md`](WS-6-g3-evidence-pack.md) known limitations
  （输出稳定性、Inspect 展示债）。
- [`docs/06_Frontend_Interaction_Design.md`](../06_Frontend_Interaction_Design.md)、
  [`docs/31_Asset_Risk_Reference_Profile.md`](../31_Asset_Risk_Reference_Profile.md) §6。

### Proposed

- 工程排序：输出稳定性 → 部署可用性 → UI 补齐 → 走查验收（稳定性是其余
  一切的前提）。
- 开发期门禁降摩擦规则（verify 常跑、integration 节点跑）写入 AGENTS 的
  阶段约束，属程序性调整不改代码。
