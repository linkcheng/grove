---
status: accepted
---

# PostgreSQL Execution Driver 提供可靠唤醒与单写者

Execution Core 使用 PostgreSQL-backed Execution Driver 持久化
start/resume/cancel、internal continue 与 trusted signal command，以 lease、
单调 fencing token 和 reconciliation 驱动 LangGraph invocation。LangGraph
仍独占 graph state、checkpoint 和 lifecycle；Driver 只拥有 command 投递与
执行权租约。
相比把 FastAPI 请求当 worker，这能在进程崩溃后重新唤醒 run；相比默认引入
Redis/Celery，它不新增 broker、双写窗口和第二套 retry 语义。

## Consequences

- API transaction 原子提交 run/spec/command，再异步执行 Graph；可选
  observation outbox 不承担 command 投递。
- 同一 run 任意时刻只有一个有效 fence；过期 worker 的 checkpoint、事件和
  Action durable acceptance 必须被拒绝。
- Driver 必须有 deterministic fake，并通过 crash、lease expiry 和并发 claim
  测试。
- 若未来采用 LangGraph Agent Server，必须以新 ADR 明确它如何完整替代该
  Driver interface 和已有恢复证据。
