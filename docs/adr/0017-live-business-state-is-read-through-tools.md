---
status: accepted
scope: core
---

# Live Business State 通过只读 Tool 获取

GROVE 只把经过受信任发布、可固定版本的制度、规则和文档语料视为 Knowledge
Snapshot。客户、资产、订单或其他领域对象的当前属性、状态、指标等在 Run 执行时
仍可能变化的 Live Business State，必须通过 Manifest 固定且 Effect Class 为
`read` 的 Tool 读取，不通过 KnowledgePort 查询。

分类依据是时间语义，不是底层技术：数据库导出物经过治理发布并形成不可变
Snapshot 后可以成为 Knowledge；同一数据库的当前行在执行时读取则仍是
Live Business State。Knowledge Citation 只证明发布内容的 Snapshot/source
version；它不能被复用于声明某次实时读取结果。

成功的 read `ToolResult` 必须保存读取来源、`observed_at`、可用的 source
revision/watermark 和 canonical result hash。小结果随 checkpoint 保存，大结果
使用内容寻址 `ArtifactRef`；Run Inspect 因而能够说明该次 Run 实际看到了什么。
Tool adapter 在 seam 使用可信 Principal/Run Authority 重新校验 Tenant、Resource
Scope、operation 和当前授权，不能只依赖 Graph 或模型输入。

## Consequences

- 一个业务 Skill 可以同时依赖固定的政策/规则 Knowledge Snapshot 与当前领域状态
  read Tool；两者不合并为通用“数据检索”接口。
- 当前业务数据不进入 `SkillExecutionSpec` 或 Evaluation Subject；Tool ref、schema、
  effect、policy 和 adapter build 进入，实际 `ToolResult` 属于该 Run 的状态。
- 更新 Live Business State 不产生 Knowledge Snapshot version；如需可引用的历史
  基线，必须通过独立治理发布形成新的 Snapshot。
- read Tool failure、timeout、denied 与空结果必须保持可判定，模型不能用旧
  Knowledge 或猜测填补当前状态。
- Tool 的 public input 必须是 Manifest 固定的强类型领域接口；不能由“使用 Tool”
  推导出开放任意 SQL。底层查询仍是 adapter 私有实现。ADR-0018 只固定首个
  Asset Risk Reference Business Profile 的具体 Tool。
