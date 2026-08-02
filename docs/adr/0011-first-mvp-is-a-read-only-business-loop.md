---
status: accepted
scope: mvp-baseline
---

# MVP 从一个只读 Business Profile 起步

最小 Product MVP 按
[ADR-0024](./0024-product-mvp-binds-one-selected-business-profile.md) 显式选择一个
Business Profile。为控制首版恢复、授权与副作用复杂度，该 Baseline 只选择声明
`pure/read` Effect Class、仅依赖 Execution Core 的业务闭环；这不预设资产或其他领域，
也不把只读限制施加给 Execution Core 或后续启用 Durable Action 的 Product release。

所选 Profile 读取已授权企业 Knowledge，执行一个固定 root Skill，按需通过安全
interrupt 收集人工补充输入，并生成 typed result 或 report artifact；它不修改企业
系统或外部世界状态。具体 Skill、Tool、Graph、业务结果和 golden dataset 由该
Profile 拥有，不进入 Core。

MVP 不启用 Durable Action、DBOS 或写 credential。模型或 Skill 请求 external
effect 时必须返回 `CapabilityUnavailable`；不能把写操作包装成普通 Tool、后台
线程或直接 SDK 调用。

## Consequences

- 最小 MVP Baseline 只需证明租户隔离、授权、异步执行、崩溃恢复、interrupt/resume、
  typed event/UI、上下文压缩和评测闭环，不承担副作用 exactly-once 语义。
- HITL 只用于补充输入、选择只读分析路径或终止 Run，不构成外部 Action 审批。
- Durable Action 的幂等交接、审批、防重、unknown reconciliation 和 HA 验收
  延后到独立 Profile，不能阻断最小 Product MVP。
- Operation Catalog 与 Skill Runtime Manifest 必须在执行前证明 MVP closure
  中不存在 external effect；仅依赖运行时拒绝不合格。
- 若首个真实场景必须写外部系统，产品必须把 Durable Action Release Track 及其
  blocker/POC 纳入发布范围；不能把该场景伪装成只读 Baseline。
