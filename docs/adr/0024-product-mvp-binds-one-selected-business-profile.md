---
status: accepted
scope: core-release-governance
---

# Product MVP 显式绑定一个 Business Profile

Execution Core release 证明通用运行能力，不预设资产、客户、合同或其他业务领域，也不能
仅凭 Core 测试宣称产品 MVP 已完成。每个 Product MVP release 必须在实现前显式选择
一个内容寻址、版本固定的 Business Profile，并把其 `business_profile_ref/hash`、
Capability closure、Evaluation Subject 与 G3 evidence 固定到
`ImplementationAcceptanceRecord`。

所选 Business Profile 必须拥有自己的 typed input/output、root Skill/Graph、
Knowledge/Tool/Action binding、Effect Class、交互与 UI projection、业务质量阈值、
golden dataset、人工评审规则和 Profile-specific POC。Core 的 G0～G2、G4～G8 与
该 Profile 的 G3 一起构成 Product MVP 验收；通用 mock 场景、另一个领域的 golden
dataset 或人工 demo 不能代替目标 Profile 的真实 E2E。

最小 MVP Baseline 按 ADR-0011/0012 选择只依赖 Execution Core、只含 `pure/read` effect、
单一 root Agent Binding 且不启用 Multi-Agent 的 Profile。若真实首个场景必须依赖
Durable Action、Run Delegation、Execution Workspace 或其他 optional capability，
产品可以选择它，但相应 Release Track、blocker、POC 和验收 gate 会成为该 Product
MVP 的前置条件；不得用普通 Tool、线程、mock 或静默降级绕过。

[Asset Risk Reference Business Profile](../31_Asset_Risk_Reference_Profile.md) 只是
仓库提供的一个具体参考实现，不是隐式默认值。只有 Product MVP 明确选择它时，
POC-M 与资产专有 ADR 才阻断该发布；选择其他领域时，必须建立自己的 Business
Profile 与同等级 G3 evidence，且不得继承资产的单次读取、all-or-nothing、无 partial
或 `max_asset_refs` 规则。

## Consequences

- Core release、Capability Profile release 与 Product MVP release 的结论分开；前
  两者不能冒充已验证的业务产品。
- Product MVP 不允许 `business_profile_ref=latest`、空值或平台内置资产默认值。
- 同一 Core build 可以支持多个 Business Profile，但每个产品发布分别评测、验收和
  生成 `ImplementationAcceptanceRecord`。
- 替换 Business Profile、业务 schema、业务 closure 或质量阈值会产生新的
  Evaluation Subject 和验收记录，不能复用另一领域的 G3 结论。
- 为控制首版复杂度，优先选择 Core-only 只读 Profile；这是交付策略，不是资产领域
  偏好，也不把只读限制上提为 Execution Core 永久规则。
