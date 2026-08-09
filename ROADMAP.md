# GROVE Roadmap

本文件是 GROVE 工作包编号、依赖和状态的唯一注册表。详细需求位于任务书，架构决策
位于 ADR，Gate 与发布规则位于
[`docs/90_P0_Blockers_and_Acceptance.md`](docs/90_P0_Blockers_and_Acceptance.md)。

## Work Packages

| ID | Name | Spec Status | Delivery Status | Depends On | Outcome | Task Book |
|---|---|---|---|---|---|---|
| WS-0 | Build Baseline | accepted | verified | — | 建立可重复构建、迁移和按角色启动的工程基线，不包含业务能力或生产发布结论 | — |
| WS-1 | Contract Spine | accepted | implemented | WS-0 | 建立稳定 canonical hash、封闭契约、权限边界和依赖规则，并在 provider 前拒绝非法执行 | — |
| WS-2 | Tenant-aware Command | accepted | implemented | WS-1 | 建立认证租户上下文、数据库租户隔离以及只持久化 submit/query 的幂等命令入口 | — |
| WS-3 | Durable Execution | accepted | in_progress | WS-2 | 建立 PostgreSQL claim/lease/fence、checkpoint 和崩溃恢复，使执行保持单写者且不丢已提交事实 | — |
| WS-4 | Observation Slice | accepted | not_started | WS-3 | 建立不反压运行时的 event/audit、可重建投影、SSE/Inspect 与最小可运维观测闭环 | [任务书](docs/work-packages/WS-4-observation-slice.md) |
| WS-5 | Core Release Proof | draft | not_started | WS-4 | 对同一 Core build 完成适用 Gate、故障、安全、容量、恢复和治理证明 | — |
| WS-6 | Selected Profile E2E | draft | not_started | WS-5 | 冻结一个 Business Profile，先实现通用 Vue/RunInteractionModel，再完成真实语料、provider、工具和 typed renderer 的端到端验证 | — |
| WS-7 | Product MVP Release | draft | not_started | WS-6 | 对精确产品 build/profile/config 完成业务评估、发布和回滚批准 | — |

## Status Rules

- Spec Status：`draft`、`accepted`、`superseded`。
- Delivery Status：`not_started`、`in_progress`、`blocked`、`implemented`、`verified`。
- 只有负责人可以批准 `accepted` 或 `verified`；代码合并、CI 通过或 demo 成功不自动改变状态。
- 修改 `accepted` 任务书前必须展示语义 diff 并取得负责人明确批准；Git 历史承担变更留痕。
- 已发布编号不得重排或复用；被替代项目标记为 `superseded`。

## Dependency and Release Boundary

```text
WS-0 → WS-1 → WS-2 → WS-3 → WS-4 → WS-5 → WS-6 → WS-7
```

- Business Profile discovery 可以与 WS-2～WS-5 并行，但不得修改 Core contract 迎合单一领域。
- WS-0～WS-4 只是工程增量；WS-5 和 WS-7 只有在精确验收记录批准后才能形成对应发布结论。
