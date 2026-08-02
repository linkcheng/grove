---
status: accepted
---

# SkillExecutionSpec 保持瘦 ABI

`SkillExecutionSpec` 只暴露 Skill、Graph、Contract、Permission、
Capability、Budget 和 Hash 等 Kernel 必需绑定；详细依赖、Knowledge、
Memory、Tool/Action schema 与 allowlist 固定在 content-addressed
`SkillRuntimeManifest`，Prompt/Model 等以受控 typed policy ref 引用。这样
ABI 不随每种 Runtime 能力扩张，同时 Kernel 仍能按精确 version/hash 恢复，
无需查询 `latest`。

## Consequences

- Manifest 是不可变 Skill artifact，不是新的 Runtime State。
- ABI 禁止自由格式 `extras` 和展开的 framework 配置。
- Manifest、Policy 或 Budget 内容变化都会进入行为与运行 hash。
