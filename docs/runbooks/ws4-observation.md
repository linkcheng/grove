# WS-4 Observation 运维手册

本手册只使用 dashboard、角色 health 和安全 Run Inspect。禁止直接查询生产数据库、提高
敏感日志级别或从 projection/telemetry 反向驱动 Run。

## 通用处置顺序

1. 确认 API 与 Runtime Worker health 独立为 ready；记录告警时间窗和低基数状态。
2. 在对应 dashboard 判断故障域：runtime、projection、SSE 或 telemetry。
3. 使用已授权的 `GET /api/v1/observations/runs/{run_id}/inspect` 查看公开状态、watermark、
   completeness 和 unknown schema 数；不得读取 raw checkpoint、payload 或内部 fence。
4. 只有 projection/SSE 故障时，重启对应角色并等待 watermark 收敛；不得重启或取消 Run。
5. 恢复后确认告警关闭、Run 仍推进、projection gap 为 0，并记录开始/恢复时间。

## API 或 Runtime 推进异常

Owner：`core-runtime`。先检查数据库 readiness 和 command advancement；若数据库不可用，按
依赖故障处理。若只有 projection/Collector 异常，Runtime 仍应推进，不升级为执行故障。

## Projection lag 或 dead-letter

Owner：`core-observation`。确认 backlog、watermark、unknown schema 和 dead-letter。重启
`projection_reconciliation` 后要求 120 秒内从 authoritative fact 重建；unknown schema
保持 partial，禁止猜测或手工改写 read model。

## SSE 延迟或重连风暴

Owner：`core-api`。检查 connection 数、backfill latency 和 gap。客户端从最后确认的
`projection_seq` 重连；服务端每轮使用短事务重新授权。不得扩大无界 buffer。

## Collector 或 backend 不可用

Owner：`platform-observability`。确认 exporter drop/failure 与 queue saturation 可见。在线
请求与 worker 不依赖 Collector readiness；优先恢复 backend，禁止把 exporter 改为同步阻塞。

## 演练验收

演练依次停用 Collector、终止 projector、断开 SSE client，再恢复。每次均记录：告警、
dashboard、授权 Inspect、Run advancement、watermark 恢复和总时长。演练结果只能声明 WS-4
工程证据，不等于 Core/Product release。
