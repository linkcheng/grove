---
status: accepted
---

# Multi-Agent 模式是图拓扑而不是新的 Runtime

GROVE 将 Sub-agent、Swarm 和 GoalLoop 编译为 LangGraph subgraph、fan-out/
fan-in 和 bounded loop；它们共享当前 Agent Run 的 lifecycle、checkpoint、
fence 和预算。只有需要独立 SLA、等待、取消或早期 Join 的工作才通过受控
Run Delegation 创建 Child Run，Child Run 仍由同一 Execution Core 执行。该选择
避免第二套 Agent lifecycle、恢复和权限模型，同时保留独立执行单元所需的
隔离。

## Consequences

- 同 Run Sub-agent 默认使用 per-invocation subgraph；同一 per-thread
  namespace 不允许并行调用。
- 普通 `Send`/fan-in 只承诺有界聚合；需可取消早期 `any/quorum` 时使用
  Child Run。
- Run Delegation 必须有 deterministic ID、原子 child run acceptance、
  internal Run Signal 和 reconciliation，不能以线程或 callback 代替。
- PydanticAI 只能提出 typed delegation proposal，不能拥有嵌套 Agent loop。
- `run.delegation` 是可选 deployment capability；未完成真实故障注入验收时
  必须关闭。
