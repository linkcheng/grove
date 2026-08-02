---
status: accepted
scope: mvp-baseline
---

# MVP 不启用 Multi-Agent 语义

本决策限定最小 Product MVP Baseline 的发布范围，不从 Execution Core 删除 Multi-Agent
topology 协议，也不要求后续 Product release 永久保持单 Agent。

所选 Business Profile 只有一个 Agent Binding、一个固定 root Skill 和一个 root
LangGraph。Graph 可以使用普通 Execution Subgraph 模块化代码，但不能因此引入
Sub-agent 角色、fixed-version child Skill 委派、Supervisor、Swarm、GoalLoop、
RoleTemplate、Join、Child Run 或协作 topology。

Multi-Agent 在单 Agent 的租户隔离、授权、恢复、事件投影和评测闭环成立后分级
增加：先启用同 Run bounded fan-out，再启用 bounded GoalLoop，最后才启用拥有
独立 lifecycle 的 Child Run。

## Consequences

- `run.delegation` 在最小 MVP 部署中关闭，任何 Child Run 请求 fail fast。
- MVP 前端不请求或展示 Parent/Child topology，也不提供 Child interaction
  routing；相关 typed event 和投影随 Multi-Agent Profile 一起交付。
- POC-J、N-21、N-27 及 topology convergence 不阻断最小 Product MVP。
- 每一级 Multi-Agent 发布都必须新增确定性 reducer、总预算、取消、恢复和评测
  证据，不能一次性开放全部模式。
