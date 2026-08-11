# BLOCKED

最后更新：2026-08-11

## 当前结论

WS-3 Durable Execution 与 WS-4 Observation Slice 均无未关闭的实现阻塞，交付状态以
[`ROADMAP.md`](ROADMAP.md#work-packages) 中的 `implemented` 为准。`implemented` 只表示工作包
范围已经落地并通过对应工程门禁；它不等于负责人批准的 `verified`，也不形成 Core、Product
或 production release 结论。

## WS-3 实现证据

- PostgreSQL Execution Driver 已覆盖 claim、heartbeat、consume/continue/terminal、cancel、
  dead-letter、lease/fence takeover 与 reconciliation；全部生产 seam 统一 run→command 锁序和
  post-lock authoritative time。
- FencedPostgresSaver 在同一连接/事务内绑定完整 claim identity；stale、expired、forged 或
  takeover 后的旧 writer 均 zero-write。
- `runtime_worker` 已实现 bounded poll → claim → deterministic LangGraph invoke → checkpoint →
  finish/continue/terminal，API 仍只提供 submit/query，不执行 Graph。
- 真实进程 SIGKILL matrix 覆盖 claim、checkpoint、consume 与 continue 前后；第二 worker 恢复后
  保持单写者，已提交 command/checkpoint 不丢失。
- `make ws-3-check` 通过：350 个非集成测试、127 个真实 PostgreSQL 集成测试通过；2 个
  catalog-root 用例因只绑定精确 WS-3 head、数据库已位于 WS-4 head 而按约束跳过。

## WS-4 实现证据

- command、claim/takeover、heartbeat、checkpoint、finish/continue、cancel、dead-letter 与
  reconciliation 已形成 versioned、事务原子的 execution audit fact；未知 schema fail closed。
- Projection/Reconciliation 使用独立角色、最小权限和有界 batch，可从 RuntimeEvent 重建
  watermark/read model；Observation API/SSE 每轮重新授权，按 durable projection cursor 补齐，
  gap 不越序，500 个相同回填请求只共享正在执行的单次读取而不缓存授权结果。
- OTel span/metric/log、低基数 allowlist、有界 exporter、Collector 配置、四个 dashboard、alert、
  runbook 与告警到 safe Run Inspect 演练均已落地；Collector/backend 故障不反压在线 Run。
- Reference Target v1 容量证据通过：201.48 events/s、投影 P95 1.433 s、恢复 6.882 s、500 SSE
  connection、RuntimeEvent→SSE P95 0.547 s；telemetry backend 连续不可用 15 分钟时在线 P95/P99
  退化约 0.81%/8.53%，均在阈值内。
- `make ws-4-check` 通过：141 个非集成测试与 16 个真实 PostgreSQL 集成测试通过。

## 共同工程门禁与后续验证边界

- `make verify` 通过：ruff、format、mypy、776 个非集成测试，branch coverage 89.12%（门槛 89%）。
- fresh-volume cleanroom 曾完成双镜像 runtime-tree digest、双 bootstrap、迁移往返、坏 SQL/锁超时
  注入、四角色自检和 API readiness；完整集成阶段为 145 passed、2 skipped、1 orchestration
  environment failure。该失败的测试变量命名根因已修复，失败用例单独复跑通过。最终整套重跑因
  当前执行环境失去 Docker socket 权限而中断，不能据此声明 `verified`。
- 完整 production provider integration、PITR/恢复、30 天等效容量、精确 Core
  `ImplementationAcceptanceRecord` 与负责人批准属于 WS-5；TypedInferencePort 的 production
  PydanticAI adapter 不是 WS-3/WS-4 实现阻塞。
- catalog authority closure 的历史审查记录已归档到
  `docs/archive/BLOCKED_catalog_authority_history_202608.md`；它是 G0 漂移检测工具，不是
  N-25/WS-3 或 WS-4 release gate。
