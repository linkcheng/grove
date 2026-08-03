# GROVE

> Governed Runtime for Observable, Versioned Execution

GROVE 是受治理、可观测、版本化的智能体执行平台：它将经过治理和版本化的
Skill 执行为可靠、可恢复、可审计的 Agent Run。LangGraph 是唯一 Execution
Kernel，PydanticAI 仅作为 Typed Inference adapter。现行架构集版本为
**GROVE v1.0**，采用“总纲 + 权威专题 + ADR + 验收”组织。

从 [GROVE Architecture](./docs/00_GROVE_Architecture.md)
开始阅读。

## 权威性

权威性按主题划分，不使用一个文档覆盖所有问题：

- `CONTEXT.md` 唯一拥有领域词义。
- accepted ADR 唯一拥有对应的难逆架构决策。
- `docs/00` 唯一拥有平台边界、状态所有权和专题地图。
- `docs/00` 指定的权威专题唯一拥有对应协议、字段和状态机。
- `docs/90` 唯一拥有 blocker/evidence 状态、量化预算和关闭记录。
- 归档文档没有规范效力。

真正冲突时必须在同一变更中修正文档，不能靠“优先级解释”长期容忍分叉。

## 阅读路径

Core 实现路径：

1. [总纲](./docs/00_GROVE_Architecture.md)
2. [Platform API](./docs/05_Platform_API.md)
3. [Execution Core](./docs/10_Execution_Core.md)
4. [Observability and Operations](./docs/12_Observability_and_Operations.md)
5. [LangGraph + PydanticAI Integration](./docs/15_LangGraph_PydanticAI_Integration.md)
6. [Canonical Execution Contracts](./docs/16_Canonical_Execution_Contracts.md)
7. [Skill Framework](./docs/20_Skill_Framework.md)
8. [SkillExecutionSpec ABI](./docs/21_SkillExecutionSpec_ABI.md)
9. [Knowledge and Memory](./docs/30_Knowledge_and_Memory.md)
10. [Multi-Agent Orchestration](./docs/17_Multi_Agent_Orchestration.md)（仅对应 Release Track）
11. [P0 Blockers and Acceptance](./docs/90_P0_Blockers_and_Acceptance.md)

Core 实施按 `docs/90` 第 14 节从领域无关 Walking Skeleton 开始，先形成 Core
Release；目标 Business Profile 可以并行发现，但必须在 G3 实现前冻结。

实现 Product MVP 时，先显式选择并冻结一个 Business Profile，再按 `docs/90` 的
MVP Baseline B 与 G3 收集该领域的真实证据。若选择仓库提供的
[Asset Risk Reference Business Profile](./docs/31_Asset_Risk_Reference_Profile.md)，
再执行 POC-M；选择其他领域时建立独立 Profile 和同等级 POC，不继承资产专有规则。

可选 Profile 再阅读 Execution Workspace、Long-Term Memory adapter、Durable
Action、Experience 与 Evolution 专题。Working Memory/Continuation 与 Knowledge
属于默认架构边界，不能因 Long-Term Memory Profile 未启用而跳过。MVP Knowledge
Baseline 包含一个 production adapter、一个不可变 Snapshot 及完整的
source/version/hash/Citation/ACL/outcome 约束；多源 ingestion 与 Long-Term Memory
才属于后续 Release Track。执行时仍会变化的业务状态不进入 Snapshot，而通过
Manifest 固定、受权限约束的 typed read Tool 获取，并以 Run Data View 保存读取
provenance。Core 不理解资产、SQL、单次读取或 partial/selection 策略；资产风控
参考闭环的具体约束只由
[Asset Risk Reference Business Profile](./docs/31_Asset_Risk_Reference_Profile.md)
定义，且只在产品显式选择该 Profile 时适用。

前端实现路径：

1. [总纲](./docs/00_GROVE_Architecture.md)
2. [Platform API](./docs/05_Platform_API.md)
3. [Frontend Interaction Design](./docs/06_Frontend_Interaction_Design.md)
4. [Canonical Execution Contracts](./docs/16_Canonical_Execution_Contracts.md)
5. [Observability and Operations](./docs/12_Observability_and_Operations.md)
6. [P0 Blockers and Acceptance](./docs/90_P0_Blockers_and_Acceptance.md)

## 文档索引

- [Platform API](./docs/05_Platform_API.md)
- [Frontend Interaction Design](./docs/06_Frontend_Interaction_Design.md)
- [Execution Core](./docs/10_Execution_Core.md)
- [Observability and Operations](./docs/12_Observability_and_Operations.md)
- [LangGraph + PydanticAI Integration](./docs/15_LangGraph_PydanticAI_Integration.md)
- [Canonical Execution Contracts](./docs/16_Canonical_Execution_Contracts.md)
- [Multi-Agent Orchestration](./docs/17_Multi_Agent_Orchestration.md)
- [Skill Framework](./docs/20_Skill_Framework.md)
- [SkillExecutionSpec ABI](./docs/21_SkillExecutionSpec_ABI.md)
- [Execution Workspace](./docs/25_Execution_Workspace.md)
- [Knowledge and Memory](./docs/30_Knowledge_and_Memory.md)
- [Asset Risk Reference Business Profile](./docs/31_Asset_Risk_Reference_Profile.md)
- [Durable Action Runtime](./docs/40_Durable_Action_Runtime.md)
- [Experience Projection](./docs/50_Experience_Projection.md)
- [Skill Evaluation, Evolution and Publication](./docs/60_Evolution_and_Publication.md)
- [P0 Blockers and Acceptance](./docs/90_P0_Blockers_and_Acceptance.md)
- [Domain Context](./CONTEXT.md)
- [历史归档：前身 EAR v1.0](./docs/archive/Enterprise_Agent_Runtime_Architecture_EAR_v1_0.md)

当前尚未选择 Product MVP 的目标 Business Profile；Asset Risk 只是参考实现，不能
被代码、配置或测试框架当作隐式默认值。

仓库包含架构设计与 WS-0 工程基线实现；任何 Profile 是否可发布，以 `docs/90` 的
G0～G8、适用 blocker 和不可变 `ImplementationAcceptanceRecord` 为准，不能由文档或
WS-0 完成度推断。

## WS-0 工程基线开发

WS-0 是可重复构建、迁移和启动的工程基线，不是 Core/Product release，也不实现
Contract Spine 或任何业务 Profile。它使用 Python 3.12.12、uv、FastAPI、Pydantic
v2、SQLAlchemy async、Alembic 和 PostgreSQL；Vue 前端留到 WS-4 契约稳定后。

本地先准备 `pgvector-postgis:pg16` 镜像，然后运行：

```bash
uv sync --frozen
make verify
make manifest-check
make integration
make ci
# clean checkout only: strict WS-0 exit evidence
make release-check
```

Compose 固定项目名 `grove-ws0-test`，四个角色共用一个应用镜像但使用独立数据库
凭据；API 提供 `/api/v1/health/live` 和 `/api/v1/health/ready`，其余角色只执行
配置自检并退出，不使用空循环伪装工作进程。`ci-evidence/` 生成运行时 CycloneDX SBOM、
真实迁移往返报告和带内容寻址引用的 canonical `RuntimeBuildManifest`，该目录被 git
忽略。`make ci` 只代表开发检查；`make release-check` 还要求 clean source、完整
CAS evidence 和严格 release gate。签名状态
明确为 `not_configured`，DBOS capability 明确为 disabled。

本地卷若出现 PostgreSQL collation 警告，不影响现有 WS-0 检查；正式 clean-room
验收应使用全新的 `grove-ws0-test` volume，验收脚本不会删除既有数据。
