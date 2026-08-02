---
status: accepted
scope: core-skill-governance
---

# 单调收紧输入上限复用 ceiling Evaluation evidence

Evaluation evidence 默认只覆盖精确的行为构建。若把每个 Tenant 调低的纯输入数量上限
都变成新的 Evaluation Subject，会为相同执行行为制造大量重复模型与业务评测；但若
允许任意“更小预算”复用 evidence，又会错误覆盖 token、deadline、fan-out 等可能
改变 Graph 或模型行为的配置。

因此只有 Manifest 明确声明为 `monotonic_input_subset` 的输入 admission limit 才能
复用其 evaluated ceiling evidence。Resolver 必须使用 Manifest 固定的 typed limit
schema 和 comparator，重新计算并证明：

1. effective value 为正且逐字段小于等于 evaluated ceiling；
2. 只有 allowlist 中的 input admission limit 发生变化；
3. Graph、schema、Tool/Action closure、权限、模型、Prompt、route、重试、partial/
   selection policy 及其他 budget byte-equivalent；
4. 超限只在首个相关 provider/node 前产生同一 typed contract failure，不触发另一条
   业务路径、截断、降级或自动重写输入。

`SkillExecutionSpec` 使用 `BudgetBinding` 同时固定 evaluated envelope、effective
budget 与由受信任 Resolver 生成的 monotonic-subset attestation。Hash 规则固定为：

```text
evaluation_subject_hash includes budget.evaluation_envelope hash
skill_spec_hash         includes the complete BudgetBinding
```

因此单调调低会改变 `skill_spec_hash` 和运行审计，但不改变
`evaluation_subject_hash`；它复用 ceiling 的模型、业务、性能和安全 evidence，同时
必须通过确定性的 comparator、contract rejection、hash 和前端错误 UX 测试。

调高、未知 limit、未声明为 monotonic、改变非 admission budget，或 comparator/
attestation 无法验证时，不能复用 evidence：Resolver 必须 fail fast，或先发布新的
evaluation envelope/Manifest 并完成完整 Evaluation。调用者、Tenant 配置和模型不能
自报 subset 关系。

## Consequences

- `max_asset_refs` 是 Asset Risk Reference Profile 首个允许该规则的 limit；其 effective 值仍固定
  在每个新 Run 的 spec 中。
- token、cost、deadline、loop、fan-out、Tool call count、结果大小以及会改变
  partial/selection 行为的 limit 默认不具备单调复用资格。
- “数值更小”本身不是证明；只有 Manifest allowlist + typed comparator + Resolver
  attestation 三者同时成立才可复用 ceiling evidence。
- 活动 Run 永远使用原 BudgetBinding；配置变化只影响新 resolve。
- 为避免通用规则引擎，MVP 只实现 `positive_integer_componentwise_lte` comparator；新增
  comparator 必须有真实用例、版本化 contract 和独立验收。
