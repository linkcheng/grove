---
status: accepted
---

# 多租户共享起步并通过 Deployment Cell 分层

GROVE 的 MVP 让多个 Tenant 共享同一 PostgreSQL database 和 module schema。
Tenant-owned 数据强制携带 Tenant key，并同时依靠 tenant 组合约束与 fail-closed
RLS 隔离；在线 API 和 Worker 数据库角色不能拥有 `BYPASSRLS` 或表所有者权限，
缺少可信 Active Tenant Context 时不能访问 Tenant 数据。

当合规、数据驻留、容量或故障域成为真实需求时，将 Tenant 整体放置到独立
Deployment Cell。Cell 可以拥有独立 database、密钥、Worker 池和容量配额，
但继续实现同一平台 contract，不能在业务代码中形成共享与独立两套语义。

## Consequences

- MVP 不建立 per-Tenant schema、动态建库、跨库 join 或双写迁移框架。
- 所有 Tenant-owned relation 都必须以组合外键或等价约束证明两端属于同一
  Tenant；仅依赖查询中的 `WHERE tenant_id = ...` 不合格。
- 数据库连接池必须使用 transaction-scoped Tenant context，并证明连接复用、
  异常回滚和缺失 context 时不会继承或暴露上一 Tenant 的数据。
- migration、backup 和运维特权使用与在线路径分离的角色，不得成为 API 或
  Worker 的通用逃生口。
- 未来 Cell 路由与 Tenant 搬迁需要独立 ADR 和验收证据；当前设计只保留稳定
  contract，不预建迁移机制。
