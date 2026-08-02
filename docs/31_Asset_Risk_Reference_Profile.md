# Asset Risk Reference Business Profile

> 架构集：GROVE v1.0
> 通用运行时：[Execution Core](./10_Execution_Core.md)
> 通用契约：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)  
> Knowledge 边界：[Knowledge and Memory](./30_Knowledge_and_Memory.md)  
> 发布证据：[P0 Blockers and Acceptance](./90_P0_Blockers_and_Acceptance.md)

## 1. 定位与所有权

本 Profile 是 GROVE 提供的一个**具体参考 Business Profile**，唯一拥有资产风控
场景的 Skill、Tool、Graph、数据完整性、前端呈现和验收约束。只有产品发布显式选择
本 Profile 时，这些约束和 POC-M 才进入该发布的 G3；它不是 Execution Core 的默认业务、
产品 MVP 的固定领域或新的 Runtime Capability，也不向 Core 增加 `AssetPort`、SQL
接口或资产专用状态。

分层边界固定为：

| 层 | 负责 | 不负责 |
|---|---|---|
| Execution Core | typed Tool seam、Manifest closure、Run Data View、checkpoint、通用失败与事件协议 | 资产字段、SQL、单次读取、partial/selection 策略 |
| Asset Risk Reference Profile | `AssetRiskSkill`、`asset.state.read@1`、Graph、完整性与交互策略 | 通用 Tool 执行框架、租户鉴权实现 |
| PostgreSQL adapter | 参数化查询、RLS、事务隔离、statement/row/byte limits | 模型决策、Graph 路由、公共领域协议 |

未来客户、采购、合规或运维场景复用 Core seam，并发布自己的 Tool Manifest 与业务
Profile；不得复制本 Profile 的资产约束为全局规则。只有两个以上真实 Profile 出现
相同且稳定的语义后，才评估是否上提到 Core。

## 2. 参考业务闭环

本 Profile 的 `@1` 发布闭环固定为：

```text
已授权政策/规则 Knowledge Snapshot
  + 当前资产状态 read Tool
  → AssetRiskSkill@1
  → 可选的人机补充输入
  → typed risk result + report artifact
```

该 Profile 只声明 Execution Core capability，只允许 `pure` 与 `read` Effect Class。不启用
Durable Action、Execution Workspace、Run Delegation、Long-Term Memory、Experience、
Evolution 或 Multi-Agent 语义。外部写请求返回 `CapabilityUnavailable`，不能退化为
普通 Tool 调用。

政策、制度和规则来自固定版本的 Knowledge Snapshot；执行时仍会变化的资产状态
只通过本 Profile 的 read Tool 获取。两者不能合并为“通用检索”。

## 3. 固定 Tool Binding

内容寻址 Manifest 固定以下 binding：

```text
tool_ref                       = asset.state.read@1
operation                      = asset.state.read
resource_type                  = asset
effect_class                   = read
input_schema                   = AssetStateQuery@1
output_schema                  = AssetStateView@1
max_logical_successes_per_run  = 1
max_asset_refs                 = <POC-M derived positive integer; no default>
partial_result_policy          = reject
selection_policy               = all_or_nothing
```

`AssetStateQuery@1` 唯一的资产选择方式是显式 `asset_refs`；列表必须非空、唯一且
不超过 Manifest 固定上限。`filter`、`search`、query DSL、`all_assets`、分页、排序
和隐式扩大 selection 的字段均不属于 `@1` schema，必须按 unknown/extra field 在
Tool provider 前拒绝。模型 payload 也不得包含 SQL、database/schema/table/column/
join、Tenant、
Principal、Resource Scope、credential、statement timeout 或 row/byte limit。
unknown/extra field、closure 外 Tool ref 和伪造的可信字段在 Tool provider 或数据库
调用前拒绝，不能“清洗后执行”。

`max_asset_refs` 的配置采用单向收紧：

```text
deployment.max_asset_refs <= manifest.max_asset_refs
tenant.max_asset_refs     <= deployment effective maximum

effective_max_asset_refs
  = min(
      manifest.max_asset_refs,
      optional deployment maximum,
      optional tenant maximum
    )
```

Manifest 值是经过评测的硬上限。Deployment/Tenant policy 可以为后续新 Run 调低，
不能调高；高于上一级上限的配置直接无效，不能靠 `min` 静默钳制。缺失可选配置时
使用上一级上限。Manifest 将 `max_asset_refs` 声明为
`monotonic_input_subset/positive_integer_componentwise_lte`。Resolver 把 ceiling、
最终值、policy ref/hash 与 subset attestation 固定到本次
`SkillExecutionSpec.budget`，活动 Run 不随配置变化。合法调低保持 ceiling 的
`evaluation_subject_hash/evidence_set`，但改变 `skill_spec_hash`。超过有效上限属于
`InputContractInvalid`，在 Tool provider 和数据库调用前失败。提高 Manifest 硬上限
必须发布新的 Manifest、改变 Evaluation Subject 并重新评测，不能只改环境变量或
Tenant 配置。

具体整数不是架构常量。POC-M 使用 golden dataset 的单资产 P99 row、result byte、
context token 占用和目标 deadline 内的压力测试容量计算四个候选上限，取最小值后再
乘 `0.8` 安全系数并向下取整。最终正整数、原始分布、预算、测试环境与报告 hash
写入 `docs/90` closing record，并冻结进发布 Manifest。证据或整数缺失时 Profile
不可发布；实现中不得回退到环境变量默认值或“无限制”。

SQL 与数据库对象只存在于 PostgreSQL adapter。adapter 使用参数化固定 query
template 或等价安全 query builder，并在最终 seam 重新强制 Active Tenant Context、
Principal/Run Authority、RLS/Resource Scope、deadline、statement/row/byte limits 与
输出 schema。通用 `postgres_query/sql`、数据库 client 和任意查询 MCP 不进入
Tool Registry。

## 4. Graph 与时间语义

本 Profile 的 `@1` root Graph 固定为：

```text
validate_input
  → optional collect_missing_input / exact InterruptRef resume
  → retrieve_policy_knowledge
  → read_asset_state
  → checkpoint accepted ToolResult[AssetStateView@1]
  → inference / risk_analysis
  → typed_report
```

每个 Run 只允许一次逻辑 `asset.state.read@1` success。adapter 在一个短生命周期的
`READ ONLY REPEATABLE READ` transaction 内生成完整 `AssetStateView@1`，随后立即
结束 transaction；transaction 不能跨 checkpoint、Inference、interrupt 或 worker
yield。

`read_asset_state` 使用稳定 logical read key 与 `tool_request_id`：

- 读取成功且 checkpoint 已提交：恢复时数据库调用数必须为 0，后续 node 复用该 View。
- 读取成功但 checkpoint 前 worker 崩溃：允许按同一 logical key 有界物理重读，最终
  只能有一个 accepted result。
- Inference 只能在 accepted View checkpoint 之后启动。
- 需要刷新资产状态时提交新 Run；不提供 refresh-in-place，也不能用 resume 刷新。

`AssetStateView@1` 是 Run-scoped Data View，不是 Knowledge Snapshot、共享 cache 或
长生命周期数据库 session。成功结果至少保存 `observed_at`、可用的 source
revision/watermark、canonical result hash、logical read key 与 `tool_request_id`；
小结果进入 checkpoint，大结果使用内容寻址 `ArtifactRef`。

## 5. 完整性与选择语义

### 5.1 预算超限

`AssetStateView@1` 只有完整成功语义，不定义 `truncated`、`partial`、
`next_cursor` 或“前 N 条”。row、result bytes、context token 或 deadline 任一可信
预算超限时：

```text
canonical code = asset_state.query_too_broad
public error   = ToolQueryTooBroad
retry_owner    = none
retryable      = false
```

失败不携带 View、partial Artifact、provenance 或真实总量，不启动 Inference。UI 只
显示安全 `limit_kind` 与缩小范围后创建新 Run 的建议。

### 5.2 资产选择不可用

任一 `asset_ref` 不存在、不可见、无权访问、跨 Tenant、已删除或在读取 transaction
中不再可见时，整个 selection 失败：

```text
canonical code = asset_state.selection_unavailable
public error   = ResourceSelectionUnavailable
retry_owner    = none
retryable      = false
```

adapter 在生成 View 的同一 transaction 内验证“请求的唯一 ref 数量 = 全部已授权且
可见的匹配数量”。不相等时丢弃已读取行，不返回授权子集、omitted count、失败索引、
存在性差异或 partial provenance。不存在与未授权对 API、事件、UI、日志和 metric
使用同一 shape；用户只能重新选择并创建新 Run。

## 6. 前端投影

Core 只投影通用 typed event `domain_view_accepted`。本 Profile 的 projector/renderer
根据 `view_schema_ref=AssetStateView@1` 将其呈现为“资产状态已固定”里程碑，并只显示
授权后的 `observed_at`、记录数、完整性和安全 provenance reference；不显示 SQL、
表名、原始 Tool payload 或资产授权差异。

预算超限映射为 `ToolQueryTooBroad`；选择不可用映射为
`ResourceSelectionUnavailable`。前端根据 Profile 提供资产语境的安全文案，但不能
改变失败码、重试语义或在当前 Run 中删除失败项继续。

空、重复、超过 effective `max_asset_refs` 或使用未支持 selection 字段时映射为
`InputContractInvalid`。前端保留原选择并显示安全 field violation 与当前允许上限，
由用户显式修改后重新 submit；不得自动截断列表、删除资产或拆成多个 Run。

## 7. 发布验收

以下证据全部通过后，本 Profile 才可发布；证据状态只在 `docs/90` 维护：

1. **Schema closure**：注入 filter/search/query DSL、`all_assets`、pagination、
   sort、SQL、数据库对象、Tenant/scope/limit、extra field 或 closure 外 Tool ref，
   Tool provider 与数据库调用数均为 0；空、重复或超限 `asset_refs` 在数据库前失败。
   尝试把 Deployment/Tenant 上限配置到 Manifest ceiling 之上时，resolve/run 创建数
   为 0；合法调低后仅新 Run 使用新有效值，保持 ceiling 的
   `evaluation_subject_hash/evidence_set` 但产生不同 `skill_spec_hash`，活动 Run
   保持原 spec/hash。篡改 comparator、limit key 或 attestation 同样 fail fast。
2. **Tenant/selection**：分别混入不存在、同 Tenant 越权、跨 Tenant、竞态删除和
   重复 ref；除重复 ref 在 seam 前 contract fail 外，其余都只产生同形状
   `ResourceSelectionUnavailable`，无子集、计数和存在性泄露。
3. **一致性**：多个固定 statement 之间并发更新 source，accepted View 仍来自同一
   `READ ONLY REPEATABLE READ` snapshot，transaction 不跨 checkpoint。
4. **故障恢复**：checkpoint 前 crash 可有界重读但只接受一个结果；checkpoint 后
   takeover 的数据库调用数为 0。
5. **预算边界**：分别超过 row/byte/token/deadline，只得到
   `ToolQueryTooBroad`；View、partial Artifact、第二次逻辑 Tool success 与 Inference
   调用数均为 0。
6. **时间与来源**：固定同一 Knowledge Snapshot，在两次 Run 间修改资产状态；
   Citation 保持相同，而 View 的 `observed_at`、revision/watermark 或 hash 能区分
   两次真实读取。
7. **Inspect/telemetry**：Run Inspect 可说明该 Run 看到的 View 和 provenance；
   RuntimeEvent、trace、metric 与普通日志不含资产正文、asset ref、SQL 或内部限制。
8. **刷新语义**：原 Run 没有刷新命令；新 submit 产生新的 Run/spec/authorization、
   Knowledge binding 与 Asset State View。

## 8. 非目标与演进规则

- 不提供通用 SQL、任意过滤 DSL、数据库探索 Agent 或运维 ad-hoc 查询。
- `AssetStateQuery@1` 不增加 filter/search/all-assets 兼容字段；真实过滤需求出现后
  发布 `@2` 或独立领域 Tool，并重新设计预算、授权披露与完整性语义。
- Deployment/Tenant 只拥有收紧旋钮；提高 `max_asset_refs` 必须发布新 Manifest 并
  重跑对应 golden、边界、性能与安全评测。
- 不把单次读取、拒绝 partial 或 all-or-nothing 强制给其他业务 Profile。
- 不在同一 contract version 内用 Tenant 配置切换完整性或选择语义。
- 业务字段、查询模式或语义变化发布新的 Tool/schema version，并重新生成
  Evaluation Subject 与 golden dataset。
- 新增第二个领域 Tool 必须有真实业务用例；不得把 `asset.state.read@1` 扩成万能
  query endpoint。

## 9. 对应 ADR

- [ADR-0011：MVP 从一个只读 Business Profile 起步](./adr/0011-first-mvp-is-a-read-only-business-loop.md)
- [ADR-0012：MVP 不启用 Multi-Agent](./adr/0012-first-mvp-has-no-multi-agent-semantics.md)
- [ADR-0024：Product MVP 显式绑定一个 Business Profile](./adr/0024-product-mvp-binds-one-selected-business-profile.md)
- [ADR-0018：只暴露强类型领域读取 Tool](./adr/0018-mvp-exposes-only-a-typed-domain-read-tool.md)
- [ADR-0019：一个 Run 接受一个 Asset State View](./adr/0019-one-run-accepts-one-asset-state-view.md)
- [ADR-0020：拒绝 partial Asset State View](./adr/0020-mvp-rejects-partial-asset-state-views.md)
- [ADR-0021：Asset selection 全有或全无](./adr/0021-asset-selection-is-all-or-nothing.md)
- [ADR-0022：单调收紧输入上限复用 ceiling evidence](./adr/0022-monotonic-input-limit-tightening-reuses-ceiling-evidence.md)
