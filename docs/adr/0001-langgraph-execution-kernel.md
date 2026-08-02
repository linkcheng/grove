---
status: accepted
---

# LangGraph 是唯一 Execution Kernel

GROVE 使用 LangGraph 独占 Agent Run lifecycle、状态流转、调度、checkpoint、
interrupt、恢复和 time travel。PydanticAI 仅作为 `TypedInferencePort`
的 production adapter，执行无 Tool、无 Memory、无 durability 的有界
typed model invocation；这样避免两套 Agent loop 和恢复所有权。

## Consequences

- Tool、Knowledge、Memory、Action 和 sub-agent route 都由 LangGraph node
  显式编排。
- PydanticAI 可以在一次 inference 内执行有界格式修复，但 node retry
  policy 属于 LangGraph。
- 启用 PydanticAI executable function tool loop、Memory 或 durable
  integration 视为架构违规；纯 structured-output transport 不属于业务 Tool。
- Canonical Execution Contracts 只稳定 module seam，不把 LangGraph
  降为普通 adapter，也不预建通用 Graph Executor SPI。
