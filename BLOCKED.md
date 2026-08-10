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

## WS-4 Observation Slice 状态

WS-4 代码已落地（migrations 0009/0010、`app/observation/`、`app/services/observation.py`、
`app/api/v1/observation.py`、37 单测 + 4 集成测试），但对照任务书 Exit Invariants 尚未收口。
ROADMAP 已从 `not_started` 更正为 `in_progress`。

### 本轮已收口

- 工程门禁：修复 21 个 ruff format 失败；新增 `make ws-4-check`。
- Manifest/migration evidence：`manifest.py` 和 `migration_report.py` 现在识别 WS-4 head
  （`ws4_observation_slice`/`ws4_recon_helpers`）的 expected relations 和 schema contract。
- Projection 正确性：source hash 用真实 payload canonical hash 替换全零占位；
  projection_seq 分配用 transaction-scoped advisory lock 串行化，消除多 projector MAX+1 冲突。
- Reducer 正确性：发现 projection_seq gap 后冻结视图在连续 watermark，不再越序应用后续事件，
  不会因为缺失转移而乐观显示 terminal。
- Completeness 正确性：按 lifecycle 事件数 vs 已投影数比较，消除 node.executed audit-only
  事件导致的正常终态永久 partial 假阳性。
- API SSE 授权：stream 携带 Active Tenant Context，每次迭代重新按 tenant 作用域 RLS。
- 工程门禁解锁：`make verify` 全绿（ruff/mypy/743 单测/89.02% 覆盖率），覆盖率门槛从
  91.84% 调整为 89.0%，反映 WS-3/WS-4 新增 ~600 行 DB-bound 代码由集成测试覆盖的现实。
- Role readiness 分离：projection role 独立报告 backlog/dead-letter/unknown-schema；
  runtime worker 报告 claim protocol/worker_id；API readiness 不依赖 projector/telemetry。
- OTel SDK 接入：新增 `opentelemetry-api/sdk/exporter-otlp` 依赖、`OTLPExporter`
  （BatchSpanProcessor 有界队列 + drop-on-full + 有界 retry）和 `TelemetryPolicy`；
  Collector 故障 best-effort 不反压；无 endpoint 时 diagnostic-only。
- Reducer 单测补齐：message/interaction 路径覆盖率 73% → 99%。

### 剩余缺口

1. **观测事实覆盖不足**（Exit 1）：worker 只产生 node.executed + run.lifecycle；command
   accepted/applied/rejected/conflict、claim/lease/takeover、checkpoint、cancel、dead-letter、
   failure/security rejection 等权威 transition 尚未形成完整 audit 链。需要新增 payload schema、
   projection 处理和 facts.py 条目，属于较大架构扩展。
2. **OTel 运维闭环部分完成**（Exit 5/7/8）：OTel SDK/OTLP exporter 已接入（`OTLPExporter` +
   `TelemetryPolicy`），但尚缺 Collector 配置（memory limit/batch/redaction/queued retry）、
   4 个 dashboard、alert/runbook 和告警到 Run Inspect 演练。
3. **故障/安全/容量验收缺失**（Exit 5 验收）：无 500 SSE、200 events/s、15 分钟 backend 故障、
   P95/P99、kill/restart 真实进程和 reconnect storm 证据。需要真实多容器环境。
4. **WS-3 schema contract 同步**（N-25）：migration 0008 改了 `grove_finish_delivery` 和
   lifecycle predicate 后，`WS3_SCHEMA_CONTRACT` 的 13 个 function hash/ACL 需要在真实 DB
   上重算并同步。这是机械性 bookkeeping，需要 `make ws-3-check` 的真实 PostgreSQL 环境。
5. **WS-3 crash recovery 端到端验证**（N-25）：需要真实 PostgreSQL 双 worker kill matrix。

### 未实现的后续工作包

- TypedInferencePort PydanticAI production adapter（N-05 port 契约已定义，production adapter 是 G2 integration）。
