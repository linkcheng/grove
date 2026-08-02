---
status: accepted
---

# Agent 是 Skill Composition + Policy

Agent 只是 Application 面向场景的 root Skill Composition 与 Policy Bundle
绑定，不作为独立 Capability 复制 Skill contract、Graph、Permission 或
Evaluation。这样能力版本、评测和演化只有一个权威所有者；Agent provenance
在 `SkillExecutionSpec` 中固定，对外能力发现属于 Plan API，执行和观测分别
属于 Execution API 与 Observation API。

## Consequences

- MVP Baseline 不建立 Agent Registry；命名 Agent 使用 immutable Application
  Binding，公开 alias/release pointer 可以移动。
- 新能力必须发布为 Skill Version，不能通过新增 Agent 配置绕过 Evaluation。
- 动态 route 只能在当前 `SkillExecutionSpec` 引用的精确 Manifest closure
  内进行。
