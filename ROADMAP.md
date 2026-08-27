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
| WS-3 | Durable Execution | accepted | implemented | WS-2 | 建立 PostgreSQL claim/lease/fence、checkpoint 和崩溃恢复，使执行保持单写者且不丢已提交事实 | — |
| WS-4 | Observation Slice | accepted | implemented | WS-3 | 建立不反压运行时的 event/audit、可重建投影、SSE/Inspect 与最小可运维观测闭环 | [任务书](docs/work-packages/WS-4-observation-slice.md) |
| WS-5 | Core Release Proof | accepted | verified | WS-4 | MVP 收窄版（2026-08-20）：冻结 MVP-ready Core build——production inference seam、本地签发工具与 clean source `release-check`；完整发布证明移至 WS-7 前置。收窄版退出条件 2026-08-20 满足，`verified` 由负责人 2026-08-26 批准（不形成 Core/Product release 结论，完整发布义务在 WS-7 前置） | [任务书](docs/work-packages/WS-5-core-release-proof.md) |
| WS-6 | Selected Profile E2E | accepted | implemented | WS-5 | Asset Risk Reference（`business-profile.asset-risk@1`）端到端闭环达成：图内真实推理（M1）、gateway 认证、domain-view 全链、typed renderer、golden dataset + 容量 closing record（81）、G3 门控 E2E（Skill 自有指令 + glm-5.3-flash 基线）；human review 按负责人 2026-08-26 批准通过（模型输出稳定性记为已知限制）；M4=implemented，`verified` 待负责人显式批准 | [任务书](docs/work-packages/WS-6-selected-profile-e2e.md) |
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

## 2026-08-20 范围调整（负责人批准）

按负责人当日指示，路线转向 MVP 优先，避免在价值验证（WS-6）之前持续加码发布证明：

- **WS-5 收窄为“MVP-ready Core freeze”**，退出条件为：production inference seam 闭环、
  本地签发工具（`scripts/ws5_issue_provider_binding.py`）可用、clean source 上
  `make release-check` 通过。收窄版不产生 Core `ImplementationAcceptanceRecord`，
  也不形成 Core/Product release 结论。
- **以下原 WS-5 义务显式移至 WS-7 前置**（production 发布门禁），不再阻塞 WS-6：
  30 天等效容量/load/soak、PITR/备份恢复全矩阵、Deployment Role 故障/扩缩容全矩阵、
  G8/POC-H Evaluation/Publication 完整闭环、外部 issuer ceremony、Core
  `ImplementationAcceptanceRecord` 生成与负责人批准。
- **WS-6 成为当前焦点**；其首要工程里程碑是图节点内真实 inference——production
  `TypedInferencePort` 在真实 Run 的图执行中被调用，验证“治理 Skill → LLM 执行”核心闭环。
- 已完成的 WS-2/WS-3/WS-4 安全核心（RLS 租户隔离、claim/lease/fence 单写者、幂等提交、
  审计事实）保持原状；它们是产品主张的一部分，不属于可后置的安全装饰。

### 2026-08-21 D0.1 补充：Business Profile 冻结

- 负责人选定 Asset Risk Reference Profile；冻结记录见
  `docs/work-packages/WS-6-business-profile-freeze.md`（ref `business-profile.asset-risk@1`、
  内容寻址 hash `65705bfc…5b30`）。WS-6 的 C/D/E3/A4 线由此解锁；该冻结是 G3 前置
  条件而非 G3 证据。

### 2026-08-26 WS-6 进度快照

- **已完成**：D0 决策门（Profile 冻结、BigModel provider、gateway 认证选型）；
  A 线全部（6.A.1～6.A.5，里程碑 M1 图内真实推理 E2E 达成，含真实推理图 SIGKILL
  崩溃恢复矩阵）；D 线主体（D2 五节点根图、D3 参考闭环端到端、D4 worker 侧 kernel
  组合、D5 spec 变体证据链）；C1～C3（Knowledge 快照/引用链/typed read tool）；
  E1/E2/E4（RunInteractionModel 契约、Vue 3 骨架、pending interaction + reconnect UX）；
  B1/B2（gateway 认证模式）。证据链见 WBS §13 执行状态。
- **进行中/待办**：E.3 typed renderer（Profile 拥有的 domain renderer）、C.4 POC-E
  步骤级证据、B.3 真实 principal 联测 integration 重跑（环境网络阻塞中）、F 线全部
  （G3 端到端验收）。
- 本快照仅为进度同步，不改变 Spec 状态（任务书 6.0.4 仍未定稿），也不形成 G3、
  Core/Product release 结论。

### 2026-08-26 负责人批准记录

- **WS-5**：收窄版验收通过，Delivery Status → `verified`（不形成 Core/Product
  release 结论；完整发布义务在 WS-7 前置）。
- **WS-6**：human review 按 glm-5.3-flash 复测结果通过（prompted JSON 模式输出
  稳定性/指令泄漏记为已知限制）；任务书（6.0.4）接受，Spec Status → `accepted`；
  docs/90 按提案写入 POC-E/POC-M 关闭记录与容量 closing record（POC-M 两个缺口
  带缺口接受，随后补齐）；M4 达成，Delivery Status → `implemented`；`verified`
  待负责人显式批准。负责人同时指示：查证网关 `response_format=json_schema`
  支持（支持则更新 profile 重签链根治指令泄漏）、补齐 POC-M 两个缺口矩阵。
