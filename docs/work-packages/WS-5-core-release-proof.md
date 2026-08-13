# WS-5 Core Release Proof 任务书

任务状态、依赖和交付进度以 [`ROADMAP.md`](../../ROADMAP.md#work-packages) 为准。

## 目标结果

对一个精确且不可变的 Core release candidate，在可复现的 cleanroom reference
environment 中使用真实 PostgreSQL、真实 PostgresSaver 和执行时选定的真实
model/provider，完成 G0～G2、G4～G8、全部适用 blocker/POC、故障、安全、容量、
恢复、运维和治理验证，并生成经批准、内容寻址的 Core
`ImplementationAcceptanceRecord`。记录固定
`business_profile_ref/hash = null`，只形成该 reference environment 的通用 Core
Release 结论，不形成 Business Profile、Product MVP、staging 或 production 发布结论。

## 背景与当前问题

WS-0～WS-4 分别建立 build、contract、tenant command、durable execution 和
observation 工程增量，但这些增量、局部 Gate、测试数量或 `make release-check`
本身都不能形成发布结论。Core Release 需要把同一 source、Runtime Build、migration、
部署拓扑、配置、Capability Profile 和 Reference Target 上的证据合并成一个可复核的
不可变验收对象；任一证据跨 build、缺少外部信任事实、仅使用 mock 或无法复现时都必须
fail closed。

当前阶段还需要补齐仅属于发布闭环的最小 seam：真实
`TypedInferencePort`/PydanticAI integration、离线 Evaluation/Publication、
可信 attestation、Core 验收记录以及 cleanroom Gate 编排。WS-5 不接管 WS-0～WS-4
原有功能所有权；验证发现上游功能缺口时应阻断并回到对应工作包修复，然后针对新 build
重新验收。

当前实施顺序固定为两个阶段：先由 WS-1 owner 补齐统一 safe canonical codec/registry
seam，关闭 inference request/context/artifact 的上游契约缺口；随后由 WS-5 将 production
adapter 构造和真实 provider G2 合并实现、验收。原 Evaluation/Publication 检查点只在新的
inference seam 通过后继续。该顺序不删减 WS-5 production adapter 或 G2 范围。

## 范围

### In Scope

- 定义唯一 release candidate identity，固定 source commit、`uv.lock`/SBOM/signature、
  `RuntimeBuildManifest`、runtime image digest、migration ref/hash、contract/ABI/state
  schema、部署拓扑/config、Capability Profile、Reference Target、cleanroom target
  environment/deployment-cell ref 和所有外部 expected facts。
- 实现最小 production `TypedInferencePort`/PydanticAI adapter；只允许 versioned
  structured-output inference，不允许 executable business Tool/toolset/MCP、Memory、
  durability、隐藏 Agent loop 或 provider object 进入 checkpoint。
- production 模块只向 Graph/Node Adapter 暴露 `TypedInferencePort.infer(request, *,
  result_type)` 深接口；不得公开接受 model client、任意 PydanticAI Model、verified token，
  或允许同一调用方同时提供 candidate facts、expected facts、hash 与 trust root 的 factory。
- `result_type` 只能使用 WS-1 contract owner 发布且编入 Runtime Build 的 canonical schema
  类型；调用方可从 `app.contracts` 导入精确类型，但不得导入或修改 production schema catalog、
  registry、Manifest 或 Provider 构造模块。
- Runtime Worker composition root 只从外部签发、内容寻址的
  `ProviderBindingManifest` 构造 production port。Manifest 至少固定 provider type/profile、
  model identifier、endpoint/config fingerprint、SDK/PydanticAI/adapter/Runtime Build
  version/hash、Model/Retry/Budget/Pricing policy、input/output schema ref/hash、
  `sdk_max_retries = 0` 和不含 secret 的 credential slot ID；production 模块内部创建并拥有
  唯一允许的 SDK client 与 PydanticAI model。
- model/provider 不在 Core 任务书中硬编码；WS-5 执行时必须通过 versioned
  `ModelPolicy`、model identifier、provider adapter ref/hash 和 Runtime Build 精确
  绑定一个真实 provider。凭据只从 cleanroom secret boundary 注入，不进入代码、日志、
  evidence、Manifest 或验收记录。
- 实现隔离的离线 Evaluation/Publication 最小闭环：immutable Suite/Run/evidence
  bundle、baseline differential、hard gate、`inconclusive` fail closed、可信 issuer
  attestation、授权审批、framework baseline publication、bounded rollout/soak plan
  和只移动 channel 的 rollback。
- 在同一不可变 release candidate 上执行 G0～G2、G4～G8；G3 必须明确记录为
  Business Profile release 才适用，不能用 conformance fixture 或 Core evidence
  伪造业务完成。
- 执行 POC-H，以及 POC-C/D/F/G/I 中对 Core Release 适用的步骤；对排除步骤逐项记录
  权威依据、关闭的 capability 和 fail-fast 证据，不能整项静默跳过。
- 对 `docs/90` 的 P0/P1 建立完整 applicability matrix；所有适用 P0 必须
  `verified`，适用 P1 必须 `verified` 或满足有界 waiver 的全部条件，且任何
  waiver 不得破坏必选 Gate。
- 在同一 Runtime Build 和 Reference Target 上执行 Deployment Role 故障/扩缩容矩阵、
  cross-tenant/role/credential/injection 安全矩阵、真实 PostgreSQL 恢复/PITR、
  load/soak/30 天等效容量、telemetry 故障隔离及可行动运维演练。
- 生成、独立验证并批准内容寻址的 Core `ImplementationAcceptanceRecord`；记录包含
  Gate result、blocker closure、evidence CAS refs、known limitations、waiver、
  rollback artifact/result、reviewer 和 approval facts。
- 提供 `make ws-5-check` 及 cleanroom release orchestration；缓存证据只可在所有
  source/build/environment/profile/config/hash 绑定完全相同时复用。
- Gate/POC 发现 WS-0～WS-4 功能缺口时，产出明确 blocker、owner 和失败证据并停止；
  上游修复产生新 release candidate 后，从受影响 Gate 开始重新执行并重新生成最终记录。

### Out of Scope

- 选择或实现 Business Profile、G3、业务 golden dataset、业务质量阈值、production
  Knowledge/Tool/Action adapter、领域 renderer 或 Product MVP
  `ImplementationAcceptanceRecord`；这些属于 WS-6/WS-7。
- 将 Asset Risk Reference Profile、POC-M、资产字段、单次读取、all-or-nothing、
  no-partial 或 `max_asset_refs` 规则提升为 Core 默认值。
- 完整产品 UI、Vue `RunInteractionModel`、领域页面、产品 Run History/Inspect UX；
  WS-5 只复用 WS-4 已验收的 headless projection/SSE/reducer 证据。
- Time Travel、Durable Action、Long-Term Memory、Run Delegation、Execution
  Workspace、Multi-Agent、Trigger、Experience、Evolution 或 Diagnostic Capture；
  未启用 capability 必须未声明、入口 fail fast 且不存在旁路降级。
- 为 Gate 失败顺手扩张或重写 WS-0～WS-4 的功能实现；功能缺口回到其权威工作包修复。
- 引入 broker、新网络服务、独立状态真相、第二条发布/评测管线，或为未来 Profile
  预建通用 registry、connector、管理面和 policy DSL。
- 在仓库、日志、evidence、Manifest 或验收记录中保存 provider credential、真实 secret
  或可复用用户密钥。
- 宣称实际 staging/production 已部署、Product MVP 已完成或任何 production Gate 已由
  reference cleanroom 自动继承。

## 依赖与前置条件

- Roadmap 依赖为 WS-4；WS-0～WS-4 的实现与证据必须能在同一 release candidate 上
  重跑并满足对应 Gate 输入，历史状态或异 build 证据不自动可复用。
- `docs/90` 是 blocker、POC、Reference Target、Gate、适用性和验收记录格式的唯一
  权威；accepted ADR 固定 Kernel、tenant、deployment role、observability 和
  Core/Product release 边界。
- cleanroom runner 必须能使用 fresh PostgreSQL volume、精确 runtime image、独立角色
  credential、受控网络，以及从环境 secret boundary 注入的真实 provider credential。
- trusted issuer/approver allowlist、签名公钥或等价外部 trust root 必须由 release
  environment 提供 expected facts；不得由待验 evidence 或 Manifest self-hash 自证。
- 开始最终验收前必须冻结 release candidate。修复、配置、migration、provider/model、
  topology、Capability Profile 或行为相关 policy 的任何变化都产生新的候选对象。
- WS-5 可以实现发布专属 seam；发现属于 WS-0～WS-4 的功能缺口时，WS-5 状态转为
  blocked，直到对应 owner 修复并提供新的精确候选 build。
- production inference 开工前，WS-1 safe canonical codec/registry 必须从原始 `dict`
  递归验证 exact type、extra、canonical JSON type、size/hash、UTF-8/JSON、depth/node 和
  schema；拒绝完整 moving-ref 等价类与 cross-tenant `context_refs`，保留 omitted、explicit
  null、present 三态，并证明所有非法输入在 provider/callback 调用前失败。

## Exit Invariants

1. 一个 Core `ImplementationAcceptanceRecord` 只证明一个精确 source/build/migration/
   topology/config/Capability Profile/Reference Target/target environment；任何绑定变化
   都使旧记录不适用。
2. 所有 Gate 和 blocker closure 要么在同一候选对象上执行，要么引用绑定完全相同且
   重读 hash 验证通过的内容寻址证据；文件名、测试摘要、缓存命中或 self-hash 不能替代。
3. `business_profile_ref/hash` 保持 `null`，G3 明确不适用于 Core Release；记录和
   报告不得出现 Product MVP、领域质量、staging 或 production 已通过的措辞。
4. 真实 provider 只通过固定 ModelPolicy/adapter/build seam 调用；模型、PydanticAI、
   middleware 和 fixture 都不能获得 Tool、Memory、durability、authorization 或
   lifecycle 所有权。
5. 所有未启用 optional capability 均未声明，所有入口在 provider/node/数据库副作用前
   fail fast，且 mock/fixture 不产生伪成功 evidence。
6. Evaluation evidence 绑定精确 subject、permission envelope、Runtime Build、
   evaluator、Suite/dataset/environment 和 trusted issuer；hard gate failure 或
   `inconclusive` 永远不能 publish。
7. API、Runtime Worker、Projection/Reconciliation、Governance/Evaluation 的 role、
   credential、pool、quota、readiness 和故障域保持分离；离线/telemetry 故障不改变在线
   Run 语义。
8. command、checkpoint、RuntimeEvent/audit 和 projection 在 crash、重复、乱序、takeover、
   stale writer、备份恢复和水平伸缩下保持原 owner 与唯一语义。
9. open/过期/范围不匹配 blocker、缺失 evidence、hash/signature/trust mismatch、
   未满足阈值或未批准记录都稳定失败，不得生成或保留 PASS 别名。
10. rollback 只移动受控 channel 或恢复已知制品/数据库状态，不修改或删除 immutable
    Version/evidence；rollback/roll-forward 结果进入同一验收闭包。
11. secret、credential、业务正文、provider raw response 和 chain-of-thought 不进入
    evidence、telemetry 或验收记录；失败证据同样遵守脱敏和低基数规则。
12. WS-0～WS-4 功能缺口由对应 owner 修复；WS-5 不通过特例、waiver 或 proof tooling
    掩盖功能错误。
13. 每个真实 HTTP send 由唯一 transport ledger 在发送前 reserve；SDK retry 固定为零，
    adapter-owned provider transient retry 与 schema repair 共享同一个 deadline、attempt、token
    和 cost ledger。每个物理请求先计 base cost，响应后追加 token cost；content filter/refusal
    是永久结果且不进入 schema retry；取消原样传播并清理 ContextVar/lock。

## 验收标准

- Release identity：验证 record schema、source/build/migration/topology/config/profile/
  environment/ref/hash 的完整性、canonical bytes 和外部 expected facts；逐项篡改后即使
  重算所有可重算 hash 也必须拒绝。
- G0：fresh source 与固定依赖重建相同 runtime content digest；SBOM/signature、
  migration `upgrade head → downgrade base → upgrade head`、rollback/roll-forward
  和 build Manifest reverse validation 全部通过。
- G1：schema/contract golden、version converter、state-owner/invariant、module
  dependency、role/capability fail-fast 和旁路族测试全部绑定候选 build。
- G2：真实 PostgreSQL/RLS/PostgresSaver 与执行时选定的真实 model/provider、
  production PydanticAI adapter 在 cleanroom 共同工作；mock 只用于 unit test，
  conformance fixture 不进入 production Skill channel 或产生 G3 结论。provider profile/settings、
  endpoint fingerprint、最终请求字段、structured-output 行为、content filter/refusal、usage 与
  pricing 必须与 Manifest 一致；retry/budget 验收以 transport 观察到的物理 HTTP 请求数为准，
  不得以 `Model.request`、Agent run 或 SDK usage request 数替代。
- G4：cross-tenant/public-ID enumeration、RLS/role、request/model/header injection、
  credential/secret disclosure、timing、tenant-switch reset 和 audit evidence 通过。
- G5：POC-I、适用 POC-C、fault matrix、idempotency、stale-writer rejection、
  takeover、projector/reconciliation、backup restore 与 PITR 均满足 `docs/90`
  Reference Target。
- G6：额定 load、soak、30 天等效容量、P50/P95/P99、错误率、pool/queue/memory/storage
  峰值、角色 quota 和 `1 → N → 1` 扩缩容证据满足 Reference Target；不得以平均值或
  更宽松脚本阈值关闭。
- G7：RuntimeEvent/audit 完整，OTel/Collector 故障隔离、四个 dashboard、alert、
  runbook、role readiness、safe Run Inspect 和 on-call drill 可复现。
- G8/POC-H：immutable Suite/Run/evidence、subject/permission envelope、trusted issuer
  attestation、hard gate、sample insufficiency、judge calibration、tamper/cross-tenant
  rejection、审批、bounded rollout/soak 和 rollback 均通过；Core 使用
  `framework-baseline`，不冒充业务 Evaluation。
- POC applicability：POC-C/D/F/G/H/I 每一步均记录 `passed` 或带权威来源的
  `not_applicable`；Time Travel、Action、Child Run、Workspace 和 Profile-specific
  步骤不能通过 mock 标记为 passed。
- Blocker closure：生成完整 P0/P1 applicability matrix；至少覆盖 Core/Common
  production 的 N-03、N-05、N-07、N-08、N-15、N-16、N-18～N-20、N-22、N-23、
  N-25，以及 exact release closure 中适用的 P1。每个关闭项引用真实测试、fault point、
  observed invariant、版本、负载分位数、evidence、reviewer 与时间。
- Role matrix：分别终止 API、Runtime Worker、Projection/Reconciliation，饱和
  Governance/Evaluation，禁用 Collector，交换/扩大数据库 credential，并执行水平伸缩；
  在线语义、恢复时间和连接配额全部满足同一 Reference Target。
- Record：Core `ImplementationAcceptanceRecord` 包含 `docs/90 §12.3` 的全部字段、
  `business_profile_ref/hash = null`、known limitations、waiver、rollback 结果和
  reviewer/approval；独立 verifier 从 CAS 重读所有引用后得出唯一相同结论。
- Negative release：任一 Gate 失败、适用 P0 open/waived、无效 P1 waiver、evidence
  缺失/篡改、issuer/trust mismatch、provider/model/build/config 漂移或上游 blocker
  存在时，`make ws-5-check` 非零退出且不留下可发布 PASS/IAR。
- Final commands：在 fresh-volume cleanroom 中通过 `make verify`、
  `make ws-5-check`、`make cleanroom-check`、`make release-check` 和
  `git diff --check origin/main...HEAD`；运行后清理临时容器和 volume，不接触用户数据。
- Status：只有负责人明确批准内容寻址的最终验收记录后，WS-5 delivery 才可从
  `implemented` 进入 `verified`；代码合并、CI 绿灯或本任务书 accepted 都不自动
  改变该状态。

## 适用 Gate、POC 与发布判断

- Core Release 必选 G0～G2、G4～G8；G3 对 WS-5 为不适用且必须显式记录理由。
- POC-H 全部适用；POC-C/D/F/G/I 按 Core/关闭 capability 的步骤级矩阵执行。
- POC-M 与任何 Profile-specific POC 不适用；POC-A/B/E/J/K/L/X/Y/Z 仅在其对应
  release closure 声明相关 capability 时适用，WS-5 不得为通过验收而临时启用。
- Core release result 只能说明该精确 Core build 在 cleanroom reference environment
  通过通用发布 Gate；部署到任何 staging/production environment 都需要新的
  target-specific 验收记录。

## 状态所有权与信任边界

- Build/Release pipeline 拥有 Runtime Build Manifest、release candidate identity 和
  Core `ImplementationAcceptanceRecord`；业务代码、fixture 和证据生成器不能自批。
- Evaluation runner 产生 evidence，trusted issuer 签发 attestation，授权 approver
  决定 publication；generator/evaluator/approver 身份分别审计。
- 各运行状态继续由其既有 owner 持有；release evidence 只引用事实，不能成为 command、
  checkpoint、authorization、RuntimeEvent 或 projection 的新 owner。
- 外部 expected hash、trust root、provider credential 和 approval authority 来自
  cleanroom release environment；仓库只保存非敏感 contract、allowlist identifier 和
  verifier。
- 固定文件名只作便利别名；CAS bytes/hash 和验收记录中的精确 ref 才是证据身份。

### Release authority 验证进程协议

- 生产验签的唯一入口是一次性 `python -I -m app.releases.cleanroom` 子进程；candidate、provider、
  model、fixture、plugin 和普通应用角色不得在该解释器内加载或执行。`app.releases` 只导出
  canonical contract，不导出 authority object、纯 bytes loader 或进程内 verifier。
- Build/Release supervisor 在受保护的 mount namespace 中预先打开 authority 目录，并通过继承的
  directory FD 传给子进程。verifier 不接受 authority path，不解析 mount 的任一中间目录；三个固定
  authority 文件只允许相对该 FD 使用 `openat(O_NOFOLLOW)` 打开并执行有界完整读取。
- 四个 pin（root public-key SHA-256、精确 policy ref/version/SHA-256）由 supervisor 作为独立部署
  配置传入；candidate、expected facts、Manifest 和被验文件不能覆盖。candidate、expected-facts 与
  issuer-signature 同样通过预打开 regular-file FD 传入，不通过环境变量或 Python object handoff。
- 每个子进程只验证一次：每次从 authority FD 重读 root、policy 和 root signature，验证当前 issuer
  active/revoked 状态后输出一份 canonical `VerifiedReleaseIdentity` bytes 并退出。禁止跨调用缓存
  authority 或已验证 issuer；policy 轮换/吊销后的下一次验证必须使用新 pins 和新 mount snapshot。
- 子进程环境为空且必须同时满足 isolated、ignore-environment、no-user-site 与 safe-path；verifier
  source、依赖和解释器来自 supervisor 固定的精确 runtime image，`/app` 为 root-owned 且对运行用户
  不可写。stdout 只允许成功 canonical bytes，stderr 只允许稳定错误码。若任意非 verifier 代码已能在
  cleanroom 子进程内执行或修改其 runtime artifact，则该进程的 TCB 已失守并必须 fail closed；Python
  private、seal 或 module global 不构成安全边界。

## 故障、安全、容量与回滚

- 所有命令、数据库 statement/lock、provider、exporter、容器、load/soak 和总流程均有
  明确 timeout、资源上限和取消清理；失败不得遗留孤儿事务、容器、volume 或 PASS
  evidence。
- release runner 必须支持逐个 Gate/POC 的独立失败证据，但最终 IAR 只能在全部适用项
  通过后原子发布；部分绿灯不得覆盖旧失败或拼装成 PASS。
- 真实 provider 的网络失败、rate limit、invalid structured output 和 retry exhaustion
  验证唯一 retry owner 与稳定错误契约；不会把 provider 不稳定性包装成业务成功。
- cleanroom 不使用用户数据；测试数据、fixture、provider input/output 和日志遵守最小化、
  脱敏、retention 与删除策略。
- rollback 演练同时覆盖 release channel、runtime artifact 和 database migration；
  历史不可变 artifact/evidence 保留审计，不能用回滚删除不利结果。

## 未决问题

无。

## 来源

### 权威来源

- [GROVE Roadmap](../../ROADMAP.md#work-packages)
- [P0 Blockers、POC、Reference Target、Gate 与 ImplementationAcceptanceRecord](../90_P0_Blockers_and_Acceptance.md)
- [GROVE Architecture：发布判断](../00_GROVE_Architecture.md#10-发布判断)
- [LangGraph + PydanticAI Integration：TypedInferencePort](../15_LangGraph_PydanticAI_Integration.md#7-typedinferenceport)
- [Skill Evaluation, Evolution and Publication](../60_Evolution_and_Publication.md)
- [Observability and Operations](../12_Observability_and_Operations.md)
- [ADR-0001：LangGraph 是唯一 Execution Kernel](../adr/0001-langgraph-execution-kernel.md)
- [ADR-0004：PostgreSQL Execution Driver 单写者](../adr/0004-postgres-execution-driver-single-writer.md)
- [ADR-0007：Active Tenant Context](../adr/0007-bind-authentication-context-to-one-active-tenant.md)
- [ADR-0013：MVP 提供 Inspect 而不提供 Time Travel](../adr/0013-mvp-provides-inspect-not-time-travel.md)
- [ADR-0014：观测性与最小运维是 MVP Foundation](../adr/0014-observability-is-an-mvp-foundation.md)
- [ADR-0015：Telemetry 硬安全包络](../adr/0015-telemetry-is-configurable-within-a-hard-safety-envelope.md)
- [ADR-0023：按角色分进程的模块化单体](../adr/0023-start-with-a-role-separated-modular-monolith.md)
- [ADR-0024：Core Release 不绑定 Business Profile](../adr/0024-product-mvp-binds-one-selected-business-profile.md)
- [GROVE 工程约束：fresh-volume cleanroom 与 evidence 规则](../../AGENTS.md)

### Proposed

无。
