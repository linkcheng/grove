# GROVE 工程约束

本文件先定义跨工作包稳定成立的决策规则，再定义当前阶段边界；阶段经验不得覆盖稳定规则。

## 项目基线与阶段边界

- 依赖统一由 `uv` 管理并提交 `uv.lock`，目标 Python 固定为 3.12.12。
- 系统保持按角色启动的模块化单体；角色只能是 `api`、`runtime_worker`、`projection_reconciliation`、`offline_governance`。
- `AsyncEngine`、连接池和其他外部资源由应用 lifespan 集中创建、复用和释放，不得按请求或 Session 重建。
- 角色、schema、数据库 URL、证据模式、镜像 ID 等关键配置必须显式校验；未知值、拼写错误和缺失值 fail closed。
- WS-0 不创建业务表、不启用 DBOS、不添加 broker，也不声明任何生产 Gate 已验证。
- 构建证据只写入被忽略的 `ci-evidence/`；secret 不得进入代码、日志、证据或 Manifest。
- 只实现当前 Spec 要求的最小闭环；不得为后续工作包预建空壳、扩大公开接口或引入额外依赖。

## 开发与审查流程

- 固定流程为 `Spec → Plan → Tests → Minimal Implementation → Integration → Review → Evidence`。
- 开工前先把成功条件、失败语义、状态所有者和阶段出口写清楚；Spec 优先于实现。
- 实现遵循 KISS、YAGNI、fail fast、显式优于魔法，并只修改请求范围内的代码。
- 高风险 seam 开发前必须列出 trust-boundary matrix 和 bypass-family matrix，再按风险片段实施与复审。
- 发现一个旁路时先枚举整个等价类，再做表驱动回归和根因修复；禁止逐个堆特例黑名单。
- 测试按 Murphy 定律覆盖边界值、空值、并发冲突、权限绕过、恶意输入、断网、慢数据库和事务回滚。
- 单元测试不能替代真实边界验证；数据库、权限、锁、迁移和恢复结论必须使用真实 PostgreSQL。
- 最终验收使用 fresh-volume cleanroom，运行后清理临时容器和 volume，不接触用户数据。
- Reviewer 先合并同根 findings；同一根因最多三轮“修复 → 验证 → 复审”，第三轮仍未关闭则写入 `BLOCKED.md`。
- 未关闭 P1/P2、缺少必需证据或验证失败时不得用测试数量、局部绿灯或措辞包装成 PASS。

## 状态所有权与信任

- 每个状态只有一个权威 owner；投影、缓存、snapshot、ledger 和日志不得反向成为事实源。
- Manifest、Spec、closure proof 和可执行制品必须由上层 Spec、可信 issuer 或内容寻址引用提供 expected hash；禁止 self-hash 自证可信。
- 跨进程恢复事实必须持久化 root、完整 graph/closure、resolver version 和 artifact hash，并能在全新进程中仅凭持久化字节重算。
- 不可达、缺失、重复、篡改或仅存在于内存的 proof 一律拒绝，不能用当前进程见过的事实补全。
- 不同信任等级使用不同入口；schema-bearing executable seam 不得与 schema-free Knowledge 等非执行入口合并成万能 helper。
- executable seam 必须要求 exact Manifest、schema registry、外部 hash 和封闭 capability；schema-free seam 仍使用封闭 discriminator。
- fixture auth 只能显式启用并封闭在 development/test/conformance/integration；production/staging 必须拒绝 fixture 身份。
- Active Tenant Context 只能由已认证身份构造，禁止从请求正文、tenant header 或调用方自报字段拼接。
- tenant、human/workload principal、角色和 scope 必须来自实时权威源；外键、guard、不可变 identity key 或 trigger 阻止 phantom/cross-tenant 引用。
- Run authority 的身份与权限事实、permission ceiling/effect/policy、Spec 身份分别建模；只允许批准的 preset，scope 变化不得偷偷改写既有 Spec。

## 契约、类型与错误边界

- 先冻结 canonical bytes 再实现 hash；明确缺失与 `null`、UTC datetime、UUID、排序、分隔符、换行及唯一允许排除的字段。
- 不得为兼容性清理空值、隐式补默认值，或让不同 reader 使用不同序列化规则。
- `extra="forbid"` 只关闭顶层；必须递归证明 nested model、annotation、generic origin、mapping、subclass 和 registry type 的闭包。
- 可执行边界优先要求 exact concrete type；未知类型、duck type、动态代理和未注册实现一律拒绝。
- 依赖边界使用 module + symbol 双向白名单，覆盖 relative/dynamic import、alias、reflection、`sys.modules`、frame/code object 和 subscript 旁路。
- 在任何 normalize、`float()`、equality、hash、membership、sort、property 或用户方法调用前，先验证原始值的 exact type、格式和有界范围。
- 所有非法输入和未知结果必须映射为稳定、文档化的异常契约；不得泄漏偶然的 `OverflowError`、`AttributeError` 或底层 coercion 行为。
- 失败必须证明发生在 provider、callback、node 或数据库写副作用之前，并用调用次数为零或事务状态不变验证。
- 同步 guard 不得猜测任意异步结果；同步与异步入口保持单一、分离语义，并正确处理完整 Awaitable 族和资源清理。
- command 只保存 schema version 与内容寻址 payload ref/hash；公共 response 不复制敏感 payload 或内部 ref/hash，API 数据库角色不得拥有 payload body 的列级读取权限。
- 公开 API 在当前命令阶段只提供 submit/query，在单一事务写 immutable spec/payload/run/start command；不调用 Graph、provider 或 worker，也不提前开放 resume/cancel。
- HTTP 契约保持 401 认证失败、422 语义校验、404 路由不存在、503 依赖不可用；业务失败保持 HTTP 200，并用稳定 business code/error_code 区分；所有错误响应携带 trace/correlation 信息；可重试 503 同时返回响应体 `retry_after` 与 `Retry-After` header。
- 路由只转换预期异常；程序缺陷继续暴露。真实进程输出含 `trace_id`、`duration_ms`、`status` 的结构化完成日志。

## PostgreSQL、并发与恢复

- 租户隔离必须在数据库再次成立：租户表启用 RLS 与 `FORCE ROW LEVEL SECURITY`，事务使用可信 tenant/principal，上线角色只获最小表级和列级权限。
- API、runtime、projection、governance 和 migration 角色的权限必须分离；应用层过滤不能替代 RLS、ACL、约束和 trigger。
- 幂等性是并发协议：以 `tenant + submission key` 获取有总时限的 advisory try-lock，先读既有提交再比较持久化 Spec hash。
- 相同提交返回原结果，不同提交稳定冲突；重试、冲突、异常和超时不得留下 spec、payload、run 或 command 孤儿。
- bootstrap 必须等待正式 postmaster 与稳定 SQL；`psql` 使用 `-X -v ON_ERROR_STOP=1`，连接、statement、lock、容器命令和总重试均设限。
- bootstrap 连跑两次证明幂等，并注入坏 SQL 与锁阻塞，要求在期限内非零退出。
- `agent_run` 与 `run_command` 是 durable fence/lease owner；可整体重建的 snapshot/ledger 不能证明已删除历史或持久化高水位。
- claim 必须在同一事务联合锁定 command 与 run，使用 `FOR UPDATE SKIP LOCKED` 或经过证明的精确等价协议，保证同一 run 单 writer。
- 所有同时锁定 `agent_run` 与 `run_command` 的生产 seam 统一先锁 run、再锁 command：0003 heartbeat、0004 checkpoint authority guard/consume、0005 claim/cancel、0006 dead-letter/reconciliation；禁止以超时重试、吞异常或单函数特例掩盖反向锁序。
- lease-sensitive 的 claim、heartbeat 与 dead-letter seam 必须先取得 run→command 锁，再读取锁后的 authoritative `clock_timestamp()`；随后在同一事务中按“post-lock time → validate → mutate”执行。锁前时间戳不能授权跨锁等待后的写入。
- lock contention、未来 `available_at`、已有匹配 lease 和其他匹配但暂不可领取状态都属于 not-ready，返回 `None` 而非 `VersionUnavailable`。
- 只有所有真正 outstanding work 都与 worker 的 exact runtime build 不匹配时，才返回 `VersionUnavailable`。
- heartbeat CAS 必须绑定 tenant/run/command/seq/digest/runtime build/worker/fence/expected lease，且新 lease 必须严格延长。
- 任一 stale、different、expired 或 forged heartbeat 都必须 zero-write；两张表不得出现 partial lease/fence 更新。
- complete/consume 同样绑定完整 claim identity，不能只校验 command、worker 或 fence 的子集。
- cancel acceptance 必须在同一事务递增 fence 并清 lease；当前候选实现不得用当前 active claim 模拟完成。
- checkpoint applied proof 必须绑定 apply-time exact claim，不能绑定 takeover 后的 active claim。
- checkpoint fence guard 与 checkpoint write 必须在同一事务和同一连接；禁止 Python 先 SELECT、PostgresSaver 再另开事务写入。
- 函数异常、锁超时和任务取消必须整体回滚；任何可见成功都必须同时满足 command/run 两侧状态不变量。

## 证据、构建与发布

- 固定文件名只作便利别名；证据必须内容寻址，Manifest 绑定 CAS 路径与 SHA-256，并在验证时重读文件计算 hash。
- Manifest 验证使用外部 expected facts；即使攻击者重算 evidence hash 和 Manifest hash，语义篡改仍必须被拒绝。
- 迁移报告必须真实执行 `upgrade head → downgrade base → upgrade head`，并从数据库查询 head、关系和安全状态。
- 迁移 hash 使用单一实现，覆盖 `alembic.ini`、`alembic/env.py`、模板、全部 revisions 及其非 Python 资产。
- 真实 `pg_catalog` 证据覆盖 relevant function definition/hash、owner、ACL、`SECURITY DEFINER`、`search_path`、trigger、RLS/FORCE、constraint、column grant 和 policy facts。
- reverse validation 必须逐项篡改 SBOM、migration report 和 schema facts，重算所有可重算 hash 后仍拒绝，且不得覆盖原证据自愈。
- Docker daemon image ID 与 canonical runtime content digest 分别记录和比较，不得把含构建元数据的原始 ID 冒充可复现内容 digest。
- 运行镜像只复制运行所需文件，固定基础镜像 digest、使用非 root 用户，并排除测试、初始化脚本和无关工具。
- `make ci` 只代表开发检查；只有 `make release-check` 才验证 clean source、完整证据和可发布 Manifest，且仍不自动等于产品发布。
- Manifest、文档、日志和最终报告必须准确区分 draft、工作包完成、Core release、Product release 与 production Gate。

## 方法论

以下是从 WS-0 ～ WS-3 多轮迭代中提炼的通用工程方法论。它们是跨工作包、跨项目的经验，优先于具体实现决策。

### 需求权威性

- **需求文档是唯一权威，不是审查记录。** 每个工作包必须从 P0/Gate 文档导出验收标准；审查产物（BLOCKED.md、review cycle 记录）是过程证据，不是需求来源。
- **不要让过程产物变得比代码库还大。** 当审查/阻塞记录的体量超过产品代码时，这是一个危险信号：过程已经脱离了交付目标，变成了自我延续的系统。
- **区分需求范围和实现范围。** N-25 要求"durable fence/lease + crash recovery"，不要求"catalog authority closure"。实现必须严格收敛到需求，不得自行发明更难的目标。

### 开放世界与封闭世界

- **不要在开放世界上证明否定存在。** PostgreSQL catalog 是可扩展的开放世界；试图枚举所有可能对象并证明"不存在未授权成员"在原理上不可收敛——补集永远大于枚举集。
- **用显式 allowlist/deny-list 替代全量枚举。** 如果需要验证一个有限封闭集合，声明有限入口并 deny 其余；不要尝试枚举整个开放世界再逐个排除。
- **区分"有限封闭系统的闭包证明"和"开放世界的完整性声称"。** 一个由 10 个文件组成的 build manifest 可以闭包证明；一个 PostgreSQL catalog 不行。选择验证方法时先问：被验证集合是有限的吗？

### 审查流程设计

- **审查驱动代码，不是代码驱动审查。** 审查的目的是发现实现中的问题并推动修复，不是冻结设计文档后审查文档。优先实现可运行代码并用真实环境验证，再基于验证结果审查。
- **避免棘轮机制。** 一个只收紧不收敛的审查循环——每一轮发现新维度、禁止补丁、要求全新设计周期，而新设计又被同样的方式否决——是系统性死锁。三轮规则应该"停止当前设计 + 允许新设计"，不应"停止当前设计 + 禁止任何新设计"。
- **补丁深度与问题深度匹配。** 一个 schema contract 的 function hash 不匹配是机械性 bookkeeping，不是架构根因失败。用"禁止第四轮补丁"来响应一个 hash 更新需求，是流程对问题的过度反应。
- **分层验证，不要叠加门禁。** G0(build evidence)、G2(integration)、G5(recovery) 是独立的验证层；不要把 G0 工具的完美度变成 G5 的前置门禁。每一层只验证自己的范围内容。

### 实现优先原则

- **可运行代码是第一证据。** 一个跑在真实 PostgreSQL 上的 worker loop + 测试通过，比设计冻结文档更有说服力。
- **Spec → Plan → Tests → Implementation → Integration 是线性流程，不是回环。** 不要在 Tests 前冻结 500 行设计协议再审查三轮；写 RED test，实现到 GREEN，用真实环境验证。
- **真实环境验证胜过 mock。** claim/lease/fence/RLS 的正确性只能用真实 PostgreSQL 证明；in-memory fake driver 适合协议单测，但不能替代集成验证。

### 第一性原理

- **问最小真实需求是什么。** WS-3 的需求是"kill worker 后单写者、command 不丢失"。从这一点出发，实现 claim/heartbeat/consume/checkpoint 足矣。catalog authority closure 不是这个问题的答案。
- **从基本事实推导，而不是复制惯例。** "数据库需要 catalog 完整性证明"听起来合理，但问"为什么"后发现：RLS + 角色分离 + protected function 已经是安全模型，catalog snapshot 只是可选漂移检测。
- **质疑每一个新层。** 每增加一个验证层、一个 hash 绑定、一个 closure 证明，都问：它解决什么真实风险？如果现有层（RLS、trigger、CAS）已经覆盖了，新层是冗余的还是互补的？

### 规避方法清单

| 通用反模式 | 症状 | 规避方法 |
|---|---|---|
| 过程产物膨胀 | BLOCKED.md / review 记录体量接近或超过代码库 | 定期审计：如果过程文档 > 产品代码体量，立即停止审查，回到需求文档重新导出范围 |
| 封闭世界幻觉 | "枚举所有 catalog 对象并证明不存在未授权成员" | 先问被验证集合是否有限封闭；如果是开放世界，改用 deny-by-default + 显式 allowlist |
| 棘轮审查 | 每轮发现新维度、禁止补丁、要求全新设计 | 三轮规则后允许结构不同的新设计；不要在"禁止补丁"和"允许新设计"之间制造死锁 |
| 门禁叠加 | G0 工具的不完美阻塞 G5 验收 | 每层只验证自己的范围；build evidence 工具是 best-effort 漂移检测，不是 release gate |
| 设计优先瘫痪 | 500 行设计协议审查三轮后才允许写代码 | RED test → implementation → real PostgreSQL validation → review；设计协议是输入不是产出 |
| 范围漂移 | 需求是 N-25 durable fence，实现却在追 catalog closure | 每个工作包开始时把 P0 ID 映射到具体验收；发现范围漂移时回到 P0 重新导出 |
| 补丁过度反应 | 一个 hash 不匹配触发"禁止第四轮" | 区分机械性 bookkeeping 和架构根因；hash 更新是前者，信任边界缺失是后者 |

### 特殊问题规避

- **PostgreSQL catalog 证明**：永远不要试图证明 catalog 里"不存在某类对象"。用 RLS + `FORCE ROW LEVEL SECURITY` + 角色权限分离 + protected function 作为安全模型；catalog snapshot 只作漂移检测，不作安全证明。
- **LangGraph worker lease 竞态**：heartbeat 只在 Graph invoke 之前；invoke + checkpoint 是不可拆分 critical section，必须严格小于 lease 减 margin；heartbeat 返回新 claim 后旧 saver 的写入必须 stale fail。
- **migration lifecycle predicate**：每增加一种 command type，必须同步更新 `grove_execution_claim_lifecycle_valid` 的 `(status, type)` pair；否则该 command 无法被 claim。
- **schema contract 同步**：migration 新增/修改 protected function 后，必须更新 `WS3_SCHEMA_CONTRACT` 的 function entry、ACL entry 和 `WS3_AUTHORITY_FUNCTION_TARGETS`；这是机械性 bookkeeping，不是架构决策。

## 工作包阶段约束

- WS-0 只建立构建、迁移、角色启动和证据基线；broker、DBOS、业务表及生产结论保持禁用。
- WS-1 保持 canonical/hash、递归闭包、依赖白名单、schema-free/executable seam 分离及持久化 closure proof。
- WS-3 scope 以 `docs/90_P0_Blockers_and_Acceptance.md` line 1096 为唯一权威：实现 PostgreSQL Execution Driver、claim/lease/fence、LangGraph invocation、PostgresSaver checkpoint 与 crash reconciliation。验收标准是 kill 任一 worker 后只有一个有效写者，已提交 command/checkpoint 不丢失，API 无 Graph 执行能力；对应 N-03、N-05、N-25 与 Gate G2/G5。
- WS-3 已完成：Execution Driver claim/heartbeat/consume/dead-letter/reconciliation（migrations 0003-0007，678 unit tests），FencedPostgresSaver claim-bound checkpoint adapter，execution contracts/state machine，API submit/query only。这些是 N-25 durable fence 的直接实现。
- WS-3 未完成：runtime_worker 进程（bounded poll → claim → LangGraph invoke → checkpoint → consume/continue/terminal loop）、minimal deterministic conformance graph、crash recovery 端到端验证。这是当前 WS-3 的唯一缺口。
- catalog authority closure (`app/build/catalog_authority.py`) 是 G0 build evidence 工具（构建漂移检测），不是 N-25/WS-3 release gate。它的历史 review cycle 记录已归档到 `docs/archive/BLOCKED_catalog_authority_history_202608.md`，不再作为阻塞项；未来改进走正常的 build tooling 迭代，不需要封闭世界枚举闭包证明。
- LangGraph execution kernel 的 graph registry、state schema、version binding 采用 minimal viable 实现：一个固定 pure deterministic conformance graph（node_a → yield → node_b → terminal），exact-match runtime build，未知 version/node/type fail closed。Graph/source/descriptor 的 hash binding 在代码中完成，不预建封闭世界 closure 证明。
- TypedInferencePort 的 PydanticAI production adapter 不在 WS-3 Core 范围内（N-05 定义 port 契约，production adapter 是 G2 integration）。WS-3 worker loop 使用 fixture/deterministic graph，不依赖 provider。
- review 流程取消"三轮规则 + 禁止补丁"棘轮：发现问题时按 `修复 → 真实 PostgreSQL 验证 → 复审` 迭代，同一根因连续三轮未关闭才写入 BLOCKED.md，但不禁止新设计周期；优先实现可运行代码并真实验证，而不是冻结设计文档再审查文档。
- numeric boundary guard 不得回退为转换后检查或单值特例；新增 public entry 必须沿用同一稳定异常边界，并在副作用前拒绝非法输入。
- 每个 Work Package 开始和结束时重新审计 Manifest schema/version、migration evidence、allowlist、阈值及固定阶段措辞。
- 不得机械沿用失真的前序假设，也不得借审计提前实现后续范围；未完成事项明确记录在 `BLOCKED.md`。
- 任何阶段都不得写入 secret、真实外部 URL、凭据或模型 key。
