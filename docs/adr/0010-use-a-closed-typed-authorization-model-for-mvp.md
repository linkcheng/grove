---
status: accepted
---

# MVP 使用封闭的强类型授权模型

GROVE 的 MVP 使用受版本控制的 Operation Catalog 和平台支持的 typed Resource
Scope。Authorization Role 只是 operation/scope 的命名集合；Tenant 不能提交
任意表达式、脚本、ReBAC 图或自定义 policy DSL。未知 operation、attribute、
resource type 或 policy version 必须 fail closed。

所有 module 通过同一个 Authorization Port 提交由 Principal、Active Tenant
Context、Operation、ResourceRef、RunMode、AuthStrength 和可选 Run Authority
组成的 typed request。Port 只返回 `ALLOW` 或 `DENY` 及 DecisionRef；只有授权
通过后，Permission Preset 才决定 `AUTO`、`ASK` 或 `DENY` 的交互姿态。

## Consequences

- Authorization Decision、Permission Preset、人工 Approval 和业务前置条件是
  四种不同事实，不能相互替代。
- Role 不能包含可执行逻辑；需要的新条件必须先成为平台版本化的 typed rule。
- Prompt、Tool、Middleware、Graph node 和前端都不能解释或扩大权限。
- 将来采用 OPA、Cedar 或外部 PDP 时，只能作为 Authorization Port 的实现；
  typed request、fail-closed 语义和审计 DecisionRef 保持不变。
- MVP 的功能边界由 Operation Catalog 明确暴露；不支持的策略需求返回不可用或
  拒绝，不能退回临时条件字符串。
