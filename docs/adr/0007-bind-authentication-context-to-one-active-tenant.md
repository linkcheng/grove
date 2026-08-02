---
status: accepted
---

# 认证上下文绑定唯一 Active Tenant

GROVE 将 Tenant 定义为数据、策略、密钥、配额和审计的最高隔离单位。同一 User
可以通过独立 Membership 参与多个 Tenant，但每个会话和请求的可信认证上下文
只能绑定一个 Active Tenant。Tenant 切换必须重新建立认证上下文，不能接受业务
请求中的 `tenant_id` 作为 Tenant 选择或切换依据。

这样可以让跨 Tenant 访问成为显式的身份上下文变更，而不是散落在 API、缓存、
投影或运行命令中的可选过滤条件，并避免前端状态或过期连接把一个 Tenant 的命令
错误提交到另一个 Tenant。

## Consequences

- Agent Run、Child Run、Interaction、Artifact、command、cache、stream 和审计记录
  都必须归属认证上下文中的唯一 Tenant。
- Tenant 切换后，旧 Tenant 的 stream、cache、pending command 和 continuation
  context 必须失效；携带旧上下文的命令必须拒绝，不能自动纠正。
- Membership 只在其 Tenant 内承载角色和 scope；User 的全局身份不产生跨 Tenant
  权限。
- 内部 worker 和服务身份也必须携带不可混淆的 Tenant 上下文，不能以缺省 Tenant
  或可选过滤条件运行。
