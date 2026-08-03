# GROVE Progress

最后更新：2026-08-03

## 当前状态

- 阶段：WS-0 Build Baseline 已完成。
- 已验收基线：当前 WS-0 单一提交。
- 当前阻断：无。
- 当前工作树：WS-0 实现及根目录 Node 占位文件清理已纳入提交。
- 发布边界：已达到本地 WS-0 release-check 标准，尚不宣称 Product Release。

## 已交付

- FastAPI 应用基线、统一响应、trace ID 和结构化日志。
- PostgreSQL async adapter、四角色凭据和 Alembic migration baseline。
- Docker/Compose、非 root 应用容器、PostGIS + pgvector PostgreSQL 16 镜像。
- SBOM、migration report、内容寻址证据和 runtime build manifest。
- dirty source、证据篡改、非法 role/schema 的 fail-closed release gate。
- 使用隔离 PostgreSQL volume 的 clean-room 验收与自动资源清理。

## 最新验收基准

- `make verify`：85 passed、1 deselected，branch coverage 89.43%。
- 真实 PostgreSQL integration：1 passed。
- `make release-check`：通过。
- Manifest：`evidence_mode=release`、`source.dirty=false`。
- 两次无缓存应用镜像构建：runtime tree digest 一致。

## 已知限制

- Manifest signing：`not_configured`。
- DBOS capability：`disabled`。
- Vue 3 前端尚未实现。
- 当前只完成 WS-0/Core 基础能力，完整产品能力尚未实现。

## 下一步

1. 确定下一个工作包及可观察、可测试的验收标准。
2. 从 WS-1 开始实现业务核心链路。
