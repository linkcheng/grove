---
status: accepted
---

# Time travel 创建新的 Agent Run

除只读 inspect 外，replay、fork dry-run 和 fork commit 都从公开 checkpoint
reference 创建新的 Agent Run，并重新 resolve `SkillExecutionSpec`、授权和预算。
禁止把 `fork_dry_run` 原地改成 `fork_commit`，因为 run mode 参与 permission
计算，原地提升会破坏 immutable spec、审计和副作用幂等命名空间。

resolve 以 source spec 的精确 Graph/State schema/Contract/
SkillRuntimeManifest/RuntimeBuildManifest 和 Model、Prompt、retry、Knowledge、
Memory、routing、redaction policy 为锚，
不解析当前 alias 或 `latest`；同时重新计算当前 authorization/run-mode
policy、主体、租户和预算交集。当前 fork API 不支持切换 Runtime/行为
build；未来若
引入，必须另建带显式目标、匹配 Evaluation evidence 和 checkpoint migrator
的 migration command。

## Consequences

- 每个 time-travel run 有新的 `run_id/thread_id/submission_id/spec_id`，并保存
  `source_run_id/source_checkpoint_ref`。
- 要提交 dry-run 的结果，调用者从选定 dry-run checkpoint 再创建一个
  `fork_commit` run。
- replay 默认使用 recorded-result adapter；缺少必需录制结果时 fail fast，
  不能退回真实模型或外部调用。
- source run 永不被回退、覆盖或追加新的 branch state。
