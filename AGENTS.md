# GROVE WS-0 工程约束

- 依赖通过 uv 管理，提交 `uv.lock`，目标 Python 3.12.12。
- 应用是按角色启动的模块化单体；角色只能是 `api`、`runtime_worker`、`projection_reconciliation`、`offline_governance`。
- WS-0 不创建业务表，不启用 DBOS，不添加 broker，也不声明生产 Gate 已验证。
- 构建证据只能写入被忽略的 `ci-evidence/`，secret 不得进入日志或 manifest。

## 长期工程经验

- **生命周期集中管理**：`AsyncEngine`、连接池和其他外部资源由应用 lifespan 创建、复用和释放；禁止在每次请求或 Session 创建时重复建立资源。
- **关键配置 fail closed**：角色、schema、数据库 URL、证据模式和镜像 ID 必须显式校验。未知值、拼写错误、dirty source 或缺失证据不得静默回退或返回成功。
- **错误边界和观测性分层**：路由层只转换预期的基础设施异常；程序缺陷继续暴露。真实进程必须输出结构化请求完成日志，至少包含 `trace_id`、`duration_ms` 和 `status`，不能只依赖测试注入的 logger handler。
- **证据内容寻址**：固定文件名只能作为便利别名；Manifest 必须绑定内容寻址路径和 SHA-256，并在验证时读取文件重新计算 hash。篡改证据或重算 Manifest hash 后仍必须被拒绝。
- **迁移证据来自真实数据库**：迁移报告必须实际执行 `upgrade head → downgrade base → upgrade head`，并从数据库查询 head 和关系状态。迁移 hash 统一实现，覆盖 `alembic.ini`、`alembic/env.py`、模板和全部 revisions。
- **明确可复现边界**：Docker daemon 的原始 image ID 可能包含 `CreatedAt` 等构建元数据；需要定义并比较 canonical runtime content digest，同时记录原始 image ID，不得把二者混为一谈。
- **运行镜像最小化**：镜像只复制运行时所需文件，排除测试、数据库初始化脚本和无关工具；使用固定基础镜像 digest 和非 root 用户，并通过实际容器检查验证。
- **测试覆盖反向路径**：除正常流程外，必须测试非法 role、篡改 Manifest/SBOM、数据库不可达、慢探针、权限隔离和真实 PostgreSQL 扩展加载；集成验收不得用 mock DB 冒充真实验证。
- **区分开发 CI 与发布门禁**：`make ci` 只代表开发检查；`make release-check` 才能验证 clean source、完整证据和可发布 Manifest。开发 CI 通过不等于 WS-0 或产品发布完成。
- **控制设计和变更范围**：保持 `create_app` 等入口职责清晰但不过度拆分；优先修复根因，不为未来需求预留空壳。冻结文件、白名单和禁止提交等约束必须保持不变，无法完成的事项记录在 `BLOCKED.md`。

## WS-1 Contract Spine 经验

- **信任必须由外部权威锚定**：Manifest、Spec、closure proof 和其他可执行制品不能用自身携带或重新计算的 hash 自证可信；执行 seam 必须接收并核验由上层 Spec、可信 issuer 或内容寻址引用固定的 expected hash。
- **先冻结 canonical bytes，再实现 hash**：明确字段缺失与显式 `null`、UTC datetime、UUID、排序、分隔符、换行和唯一允许排除的字段；禁止为兼容性清理空值、隐式补默认值或在不同 reader 中使用不同序列化规则。
- **封闭契约需要递归正向证明**：`extra="forbid"` 只关闭顶层字段，不足以证明安全。必须递归验证 nested model、annotation、generic origin、mapping、subclass 和 registry type；可执行边界优先要求精确 concrete type，未知类型一律拒绝。
- **依赖边界使用 module + symbol 白名单**：禁止只靠跨层 module 黑名单。生产代码的实际外部 import 集合必须与白名单双向相等，并覆盖 relative/dynamic import、alias、reflection、`sys.modules`、frame/code object 和 subscript 等逃逸路径。
- **发现一个旁路就修复整个等价类**：Reviewer 发现 `getattr`、dynamic import、duck type 或某种 Awaitable 绕过后，Worker 必须先枚举同族变体，再提交表驱动回归测试和通用根因修复；禁止逐个增加特例黑名单。
- **所有失败必须证明发生在 provider 前**：未知 ABI/version、hash mismatch、closure violation、缺 capability、`ASK`、`DENY` 和 Disabled adapter 都要用 callback/provider/node 调用次数为 0 的测试证明 fail closed，不能只断言异常类型。
- **跨进程恢复不能依赖内存事实**：dependency closure 等 proof 必须固定 root、完整 graph、closure、resolver version 和 artifact hash，并能在全新进程中仅依靠持久化字节重算；不可达、缺失、重复或篡改节点全部拒绝。
- **同步与异步接口保持单一语义**：不要让纯同步 guard 猜测或接收任意异步结果。确需防御时使用完整 awaitable 判定，并覆盖 native coroutine、generator-based coroutine、ABC/custom awaitable 和资源清理；Runtime 使用独立 async interface。
- **不同信任等级使用不同入口**：schema-bearing executable seam 必须要求 exact Manifest、schema registry 和外部 hash；schema-free Knowledge 等非执行入口保持独立且使用封闭 discriminator。不要为了复用把不同信任等级合并成万能 helper。
- **高风险 seam 分阶段独立审查**：开发前先建立 trust-boundary matrix 和 bypass-family matrix；按 canonical/hash、schema closure、Manifest/Spec、permission/guard、dependency boundary 等风险片段逐段完成 Luna 实现与 Sol 审查，不等整个 Work Package 完成后才首次攻击面检查。
- **审查发现按根因收敛**：Reviewer 首轮先盘点完整攻击面并合并同根 findings；同一根因最多三轮“修复 → 验证 → 复审”，第三轮仍未关闭则记录为 blocked，禁止无限补丁或强行宣称 PASS。
- **每个 Work Package 重新审计阶段假设**：检查 Manifest schema/version、migration evidence、allowlist、测试阈值和 `WS-*` 固定文案是否仍与当前阶段一致；不得机械沿用已经失真的 WS-0/前序工作包假设，也不得因此提前实现后续范围。

## WS-2 Tenant-aware Command 经验

- **认证夹具必须显式启用且环境封闭**：本地测试身份不能成为默认认证路径。只有显式 fixture mode 才能接受夹具，production/staging 必须拒绝启动或拒绝请求；Active Tenant Context 只能由已认证身份构造，禁止从请求正文、tenant header 或调用方自报字段拼接。
- **租户隔离必须在数据库再次成立**：应用层过滤不是安全边界。所有租户业务表同时启用 RLS 与 `FORCE ROW LEVEL SECURITY`，事务内使用可信上下文设置 tenant/principal，并给运行角色最小表级和列级权限；用真实 PostgreSQL 分别证明跨租户读、写、冲突探测和敏感列读取都失败。
- **身份与授权引用必须有权威源**：提交前校验 tenant、human/workload principal、角色和 scope 的实时有效性；数据库通过外键、同步 guard、不可变 identity key 或 trigger 阻止 phantom principal、跨租户引用和事后改写身份，不能只信 API 已经检查过。
- **权限事实、权限上限与 Spec 身份分离**：Run authority 固定 tenant、principal、认证强度、scope 和 revision；permission envelope 只描述 ceiling/effect/policy。仅允许明确批准的 permission preset，scope 变化不得偷偷改变已有 Spec 或产生孤儿 Spec。
- **公开命令面保持最小且只负责持久化**：WS-2 只开放 submit/query；API 在单个事务中写入 immutable spec、payload、run 和 start command，禁止调用 Graph、provider 或 worker，也不预实现 resume/cancel。使用依赖边界测试和调用次数为零的断言证明失败发生在副作用之前。
- **幂等性是并发协议，不是唯一索引补丁**：以 `tenant + submission key` 获取有界 advisory try-lock，先查询既有提交，再比较持久化 Spec hash；完全相同则返回原结果，不同则稳定冲突。锁等待必须有总时限，重试、冲突和超时路径都不得留下 Spec、payload、run 或 command 孤儿记录。
- **命令与响应不得复制敏感 payload**：command 只保存 schema version、内容寻址的 payload ref/hash；公共 receipt 不返回内部 ref/hash，API 角色也不能直接读取 payload body。通过响应 schema、数据库列权限和集成测试三层共同约束。
- **可执行发布闭包必须类型化并由外部证据锚定**：输入、输出、skill、agent、runtime build、graph/contracts、授权策略和评估证据都使用精确类型及内容寻址引用；提交时核对 expected hash、registry subject、issuer/suite/run。未知 constraint、未知 subject、草稿 build 或缺失 evidence 必须在任何数据库写入前失败。
- **错误契约必须同时服务客户端与运维**：认证失败用 401、语义校验用 422、路由不存在用 404、依赖不可用用 503，业务冲突可在 HTTP 200 中返回稳定 business code；所有失败携带 trace/correlation 信息。可重试的 503 同时返回响应体 `retry_after` 和 `Retry-After` header，并用真实锁超时路径验证，而非只测手工抛出的异常。
- **数据库启动与迁移门禁必须有硬边界**：等待正式 postmaster 和 SQL 稳定后再 bootstrap；`psql` 使用 `-X -v ON_ERROR_STOP=1`，连接、statement、lock、容器命令和重试总时长全部设限。正常 bootstrap 连跑两次验证幂等，并注入坏 SQL 与锁阻塞，要求在期限内非零退出。
- **迁移和构建证据必须双向、精确且不可自愈**：迁移验证覆盖 WS-2 精确 relation 集合，并在真实数据库执行 `upgrade head → downgrade base → upgrade head`；reverse validation 逐项重算内容与 hash，分别篡改 SBOM、migration report 等证据并确认被拒绝且原件未被脚本覆盖。
- **最终验收必须使用 fresh-volume cleanroom**：单元测试通过不能替代真实边界验证。至少执行全量静态检查与覆盖率、全新 PostgreSQL volume 的 RLS/API 集成、重复 bootstrap、迁移往返、故障注入和证据篡改；测试结束清理临时容器/volume，不接触用户数据。
- **阶段成果不得冒充发布结论**：Manifest 中准确表达 draft/release 状态；WS-2、开发 CI 或集成门禁通过都不等于 WS-5 release 或 production gate 已验证。文档、日志与最终报告必须使用同一阶段措辞。
- **高风险审查应先列矩阵再写代码**：开发前列出 auth source、tenant context、RLS role、idempotency lock、artifact/evidence closure、error/retry 和 bootstrap timeout 的信任边界与反向测试；Luna 分片实现后由 Sol 按矩阵攻击式复审，finding 以根因和旁路族收敛，避免末期反复补丁。
