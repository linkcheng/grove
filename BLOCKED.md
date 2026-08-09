# BLOCKED

最后更新：2026-08-09

## WS-3 Durable Execution 状态

WS-3 scope 以 `docs/90_P0_Blockers_and_Acceptance.md` 为权威（对应 N-03/N-05/N-25，Gate G2/G5）。

### 已完成

- PostgreSQL Execution Driver：claim / heartbeat / consume / dead-letter / expired-lease reconciliation（migrations 0003-0007）。
- `grove_finish_delivery`：原子 consume + ContinueRun 插入（yield）/ 标记 succeeded（terminal）（migration 0008）。
- lifecycle predicate 扩展：`(running, continue)` 现在可以被 claim（migration 0008）。
- FencedPostgresSaver：claim-bound checkpoint 写入，同连接/事务 protected trigger guard。
- Execution contracts / state machine：完整 command types、canonical hash、DeliveryReceipt。
- Minimal deterministic conformance graph：纯两阶段（node_a → yield → node_b → terminal），无外部 IO。
- Runtime worker loop：bounded poll → claim → heartbeat → invoke → checkpoint → finish_delivery。
- API submit/query：单事务持久化 immutable spec/payload/run/start command；不调用 Graph/provider/worker。
- 691 unit tests 全绿；核心集成测试（claim/heartbeat/consume/checkpoint/dead-letter）在真实 PostgreSQL 通过。

### 当前缺口

1. **Schema contract 更新**：migration 0008 新增 `grove_finish_delivery` 和修改 lifecycle predicate body 后，WS3_SCHEMA_CONTRACT 的 function hash/ACL 需要同步更新。目前 13 个 schema fingerprint 集成测试失败（不影响执行行为）。
2. **Crash recovery 端到端验证**：需要真实 PostgreSQL 双 worker kill matrix 测试（claim/checkpoint/consume/continue 前后杀进程）。
3. **完整 G2/G5 gate 验证**：需要 load test、PITR、30 天等效容量测试。

## catalog authority closure（已降级）

历史 review cycle 记录已归档到 `docs/archive/BLOCKED_catalog_authority_history_202608.md`。
它是 G0 build evidence 工具，不是 N-25/WS-3 release gate。

## 未实现的后续工作包

- WS-4 Observation Slice：event/audit outbox、projector、SSE、Run Inspect、OTel。
- TypedInferencePort PydanticAI production adapter（N-05 port 契约已定义，production adapter 是 G2 integration）。
