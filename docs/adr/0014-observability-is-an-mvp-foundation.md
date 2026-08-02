---
status: accepted
---

# 观测性与最小运维是 MVP Foundation

GROVE 将身份租户、可靠异步执行、契约版本、观测审计、可靠交互、资源上下文边界、
评测证据和最小生产运维共同定义为不可关闭的 MVP Foundation。它们必须随首个
只读业务闭环实现，不能作为后续 Production Hardening Track 补装。

观测数据分为四类：权威 module state、不可采样的 RuntimeEvent/Audit Fact、
可重建 Product Projection，以及允许有界采样或丢弃的 Diagnostic Telemetry。
GROVE 采用 OpenTelemetry API/SDK + OTLP 作为诊断基线，但 OTel 不拥有恢复、授权、
审计或 UI 权威状态。

## Consequences

- 必需 event/audit outbox 与对应状态原子提交；诊断 exporter/Collector 故障不得
  阻塞或改变 Agent Run。
- MVP 必须交付 correlation、核心 metrics、脱敏结构化日志、四个可行动 dashboard、
  alert、health/readiness、PITR 和恢复演练证据。
- RuntimeEvent、trace、log、metric 和 projection 均不能驱动 Graph 恢复或授权。
- Multi-Agent、Durable Action 等 Track 只扩展同一观测协议；不能建立自己的
  event bus、日志真相或独立 telemetry pipeline。
- 完整业务内容不进入诊断 telemetry；需要内容排障时使用重新授权、受 retention
  约束的 Run Inspect/Artifact 路径。
