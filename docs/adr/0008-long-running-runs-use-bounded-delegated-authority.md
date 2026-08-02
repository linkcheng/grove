---
status: accepted
---

# 长时 Agent Run 使用有界委托权限

GROVE 不保存或转交发起用户的 credential，也不让异步 Worker 仅凭宽泛服务权限代表
用户。提交 Agent Run 时记录 Initiating Actor，并建立不含 credential、可撤销且
有界的 Run Authority；Worker 以自己的 Workload Principal 完成认证。

运行期间的实际权限是 Worker 能力、Run Authority 与当前 Tenant、资源及授权策略
的交集。`resume`、`approve`、`cancel` 等外部命令由当前命令 Principal 重新授权，
不能把历史 Actor 或审批记录当作当前授权。

## Consequences

- User、Actor、执行 Worker 与授权委托是不同概念，审计记录必须保留其因果关系。
- Agent Run 只保存 Run Authority 引用、授权语义快照和审计证据，不保存 bearer
  token、session credential 或可复用用户密钥。
- Membership、角色或资源策略被撤销后，后续敏感访问和副作用必须按当前状态拒绝；
  启动时的授权快照不能恢复已撤销权限。
- Child Run 必须获得单独、进一步收窄的 Run Authority，不能继承 Parent
  credential 或扩大 Parent 权限。
- Worker compromise 的权限上限仍受其 Workload Principal、Run Authority、
  Tenant policy、Skill permission 和当前资源状态共同约束。
