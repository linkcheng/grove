# BLOCKED

最后更新：2026-08-14

## 当前结论

WS-3 Durable Execution 与 WS-4 Observation Slice 均无未关闭的实现阻塞，交付状态以
[`ROADMAP.md`](ROADMAP.md#work-packages) 中的 `implemented` 为准。`implemented` 只表示工作包
范围已经落地并通过对应工程门禁；它不等于负责人批准的 `verified`，也不形成 Core、Product
或 production release 结论。

## WS-3 实现证据

- PostgreSQL Execution Driver 已覆盖 claim、heartbeat、consume/continue/terminal、cancel、
  dead-letter、lease/fence takeover 与 reconciliation；全部生产 seam 统一 run→command 锁序和
  post-lock authoritative time。
- FencedPostgresSaver 在同一连接/事务内绑定完整 claim identity；stale、expired、forged 或
  takeover 后的旧 writer 均 zero-write。
- `runtime_worker` 已实现 bounded poll → claim → deterministic LangGraph invoke → checkpoint →
  finish/continue/terminal，API 仍只提供 submit/query，不执行 Graph。
- 真实进程 SIGKILL matrix 覆盖 claim、checkpoint、consume 与 continue 前后；第二 worker 恢复后
  保持单写者，已提交 command/checkpoint 不丢失。
- `make ws-3-check` 通过：350 个非集成测试、127 个真实 PostgreSQL 集成测试通过；2 个
  catalog-root 用例因只绑定精确 WS-3 head、数据库已位于 WS-4 head 而按约束跳过。

## WS-4 实现证据

- command、claim/takeover、heartbeat、checkpoint、finish/continue、cancel、dead-letter 与
  reconciliation 已形成 versioned、事务原子的 execution audit fact；未知 schema fail closed。
- Projection/Reconciliation 使用独立角色、最小权限和有界 batch，可从 RuntimeEvent 重建
  watermark/read model；Observation API/SSE 每轮重新授权，按 durable projection cursor 补齐，
  gap 不越序，500 个相同回填请求只共享正在执行的单次读取而不缓存授权结果。
- OTel span/metric/log、低基数 allowlist、有界 exporter、Collector 配置、四个 dashboard、alert、
  runbook 与告警到 safe Run Inspect 演练均已落地；Collector/backend 故障不反压在线 Run。
- Reference Target v1 容量证据通过：201.48 events/s、投影 P95 1.433 s、恢复 6.882 s、500 SSE
  connection、RuntimeEvent→SSE P95 0.547 s；telemetry backend 连续不可用 15 分钟时在线 P95/P99
  退化约 0.81%/8.53%，均在阈值内。
- `make ws-4-check` 通过：141 个非集成测试与 16 个真实 PostgreSQL 集成测试通过。

## 共同工程门禁与后续验证边界

- `make verify` 通过：ruff、format、mypy、776 个非集成测试，branch coverage 89.12%（门槛 89%）。
- fresh-volume cleanroom 曾完成双镜像 runtime-tree digest、双 bootstrap、迁移往返、坏 SQL/锁超时
  注入、四角色自检和 API readiness；完整集成阶段为 145 passed、2 skipped、1 orchestration
  environment failure。该失败的测试变量命名根因已修复，失败用例单独复跑通过。最终整套重跑因
  当前执行环境失去 Docker socket 权限而中断，不能据此声明 `verified`。
- 完整 production provider integration、PITR/恢复、30 天等效容量、精确 Core
  `ImplementationAcceptanceRecord` 与负责人批准属于 WS-5；TypedInferencePort 的 production
  PydanticAI adapter 不是 WS-3/WS-4 实现阻塞。
- catalog authority closure 的历史审查记录已归档到
  `docs/archive/BLOCKED_catalog_authority_history_202608.md`；它是 G0 漂移检测工具，不是
  N-25/WS-3 或 WS-4 release gate。

## WS-5 Core Release Proof 检查点 1：release authority blocker 已 supersede（2026-08-12）

- 原 blocker 不是过期记录：旧入口允许同一调用方整体替换 candidate、expected facts、policy、
  root、issuer 与 anchor，并在重算普通 hash 后完成整体重锚。
- Authority owner 已由任务书明确为 Build/Release pipeline；旧记录中“authority owner 未明确”的
  表述不准确。实际缺口是 cleanroom 加载协议、不可由 candidate 覆盖的 root/policy pins、签名、
  key rotation 与 revocation 语义。
- 后续复审证明本检查点仍错误地把 Python private loader、module seal 与进程内 authority cache 当作
  安全边界；同解释器代码可整体替换它们，缓存 authority 还会延迟 issuer revocation。本段原
  “blocker 已解除”结论作废，由下方检查点 2 取代。
- Trust policy 由 Ed25519 root key 签署并固定完整 issuer key；active issuer 使用独立 domain
  separation 签署 exact canonical expected-facts bytes。策略支持 active/revoked 状态与
  vN → vN+1 重叠 → vN+2 吊销轮换，禁止 moving ref、latest 或旧 policy fallback。
- 所有导出 JSON reader 统一执行大小、深度、节点数、duplicate-key 与 canonical bytes 检查；ref
  使用正向 grammar 拒绝空、dot、尾随和 moving-alias 变体；identity、expected-facts、trust-policy、
  authority-policy 四类文档均冻结完整 golden bytes/hash。
- 当时验证：release authority 专项 `85 passed`；`make verify` 为 `884 passed`、`153 deselected`，
  coverage `89.13%`。固定 cleanroom pins 后，整体重锚、替换 root/issuer、自签 policy、facts
  篡改重算 hash、revoked/unlisted/wrong-key、cross-domain signature 均 fail closed。
- 上述旧测试结果不能覆盖同进程整体重锚，因此本检查点不再解除 WS-5 release identity authority
  seam；它未形成 Core、Product、staging 或 production release，也未标记 WS-5 `verified`。

## WS-5 Core Release Proof 检查点 2：进程级 authority seam（2026-08-12）

- 生产 authority object、纯 bytes loader、module seal 和全局 cache 已删除；`app.releases` 只保留
  canonical contracts。生产唯一入口改为无 plugin 的一次性 isolated cleanroom CLI；runtime image
  中 verifier source/dependencies 为 root-owned 且对运行用户不可写。
- Build/Release supervisor 传入预打开 authority directory FD、三个输入 FD 和四个独立 pins；verifier
  不接受 mount path，因此不解析中间目录 symlink。authority 固定文件相对 dirfd 使用
  `openat(O_NOFOLLOW)`，每次调用重新完整读取并验证当前 policy/signature/revocation。
- ref 使用总长 512、segment 128 的有界正向 grammar；moving aliases 后追加 `+ - _ . / @ :` 的等价族
  均拒绝。canonical reader 在退出 `except` 后抛稳定错误，完整 `cause/context` 链和 traceback 不含输入。
- 当前验证：release authority 专项 `58 passed`；`make verify` 为 `858 passed`、`153 deselected`，
  coverage `89.26%`；最终 runtime image 构建成功，运行用户不能写 verifier/source/site-packages，
  isolated verifier 与普通应用入口均可从该镜像加载。
- 本检查点仍不形成 Core、Product、staging 或 production release，也不标记 WS-5 `verified`；只有
  独立复审与完整门禁均通过后才能把本进程级 seam 作为后续 WS-5 验收输入。

## WS-5 Production Inference v2 检查点（2026-08-13）

- 旧候选把 production trust、Provider 构造和运行调用暴露为调用方可组合的
  factory/token/model-client 浅接口，无法证明唯一 Provider config、SDK retry、物理 HTTP
  request、usage/pricing 与跨租户 context 边界；该候选只保留为历史 blocked evidence，未进入
  `main`，不得作为 production/G2 证据。
- 历史记录曾记载 WS-1 owner 已交付统一 safe canonical codec/registry、typed request storage
  revalidation、request/context/artifact invariants 与 zero-user-code/zero-provider 旁路族测试；其中“无未关闭
  P1/P2”的判断已被 2026-08-13 新复核 supersede/作废；该 WS-1 raw canonical reader blocker 已由
  c8df251 关闭，但不恢复旧 production PASS、G2 或 release 结论。
- WS-5 production port 只从 Runtime Worker composition root 构造：root 先调用 isolated cleanroom
  验证签名 release identity，再按 candidate hash 重读 `ProviderBindingManifest`；实际 SDK、PydanticAI、
  adapter、Runtime Build、endpoint、model、schema 与 policy 均与 Manifest 逐项绑定。Graph/Node 只可见
  `TypedInferencePort.infer(request, *, result_type)`。
- SDK retry 固定为零；provider retry 与 schema repair 共享同一个物理 transport attempt/deadline/token/
  cost ledger。Worker-owned 一次性 G2 入口从已验证 Manifest 内部构造 canonical request；真实
  OpenAI-compatible Provider slice 已通过签名 `prompted/json_object` profile，最终请求、structured
  output、usage、pricing 与 transport 观察到的单次物理请求一致。fresh PostgreSQL volume 上的完整
  Runtime Worker 崩溃恢复矩阵为 9 passed，并在验收后清理临时容器与 volume。
- 历史记录中“该检查点只关闭 production inference 子范围”的结论已被 2026-08-13 新复核 supersede/作废；
  Evaluation/Publication、完整 cleanroom Gate matrix、
  Runtime Worker + PostgreSQL/RLS/PostgresSaver + 外部 issuer 的完整 G2、actual runtime image identity、
  `ImplementationAcceptanceRecord` 与负责人批准仍待后续完成，因此 WS-5 delivery 仅为 `in_progress`；
  当前 Provider 与 PostgreSQL/Worker 证据仍是同一代码的两个集成切片，且测试内签发 fixture 不作为
  外部 trust-root 或完整 G2 证据；本记录不形成 Core、Product、staging 或 production release 结论。

### 旧未提交候选的选择性迁移记录（2026-08-13）

- 原历史候选 stash 对象为 `7bbdd7e9437ad2137ccd09ba3c911ded733d2031`，选择性迁移后已删除。其旧
  `app/inference/adapter.py` 及 factory/binding 私有接口测试未恢复，因为该设计已被 v2
  composition-root/transport 架构取代。
- 仅把仍适用于 v2 公共边界的回归迁入当前测试：canonical manifest bytes、缺失 usage fail-closed、
  总请求大小、role/context/artifact ref 保真，以及 schema/provider 交错重试的统一计数。
- 这些回归是当前检查点的可执行 RED 证据，不构成 PASS、G2、Core、Product、staging 或 production
  release 结论；须由后续修复关闭并重新通过完整门禁。

### WS-5 开工前 baseline/upstream blockers（2026-08-13，历史复核）

- 历史复核精确 base 为 `c99ce7903f686a376536bb843e4c250e8f2e7eeb`（当时 HEAD=`origin/main`）；当时记录的分支为 `codex/ws-5-core-release-proof`。
- `BLOCKED.md`、三份 inference 专项测试 diff 是既有 WS-5 RED/审计输入，必须保留，不代表基线已通过。
- 历史复核中的上游 WS-1 canonical blocker（owner：WS-1 contract/canonical owner）：manifest 额外 whitespace 配合其自身 `expected_hash` 可被 `SafeCanonicalCodec`/loader 接受，复现为 1 个 RED。
- 历史复核解除条件：WS-1 使原始字节与 canonical bytes 严格一致并通过 safe-codec/manifest 回归；在新候选上重跑基线。
- 历史复核的 c99 无损数据：ruff/format/mypy 通过，949 passed、155 deselected，但 coverage `87.12% < 89%`，`make verify` 仍失败（owner：产生 c99 混合变更的 WS-1 canonical / WS-5 production-inference 基线集成 owner）。
- 历史记录解除条件：必须在本 Goal 外独立修复基线，产出 clean 新候选，使 `make verify` ≥ 89% 且 `make release-check` 通过，再重新固定 WS-5 起点；本 Goal 不补覆盖。
- 历史复核记录的四类 adapter RED（角色/context/ref、请求大小、交错 retry ledger、缺失 usage）保持原样，仍属 WS-5；只有前置 blocker 解除后才继续，不能包装为 PASS。
- 该历史复核不形成 Core、Product、staging 或 production release 结论。

## 2026-08-14 同步后复核

- 当前分支为 `codex/ws-5-core-release-proof`，HEAD=`origin/main`=`c8df251f91e65f5c052e8569fc3f40c57308b78f`；c99 证据保留为上述历史复核。
- WS-1 raw canonical reader blocker 已由 c8df251 关闭；WS-1/manifest/dependency 专项 `194 passed`，9 个 noncanonical/self-hash 探针均在 `schema callback=0` 前拒绝，oversized canonical request provider sends=0。
- 当前 WS-5 仍有 3 个根因、4 个 test failures：missing/null usage 两参数；roles/context/content_schema_ref/ArtifactRef 丢失；schema/provider 交错 retry 后 `schema_retries=0`。oversized request 已关闭。
- 剩余 owner/解除条件：WS-5 production-inference owner 修复三根因并通过回归；baseline/integration owner 在本 Goal 外使 clean c8df main 的 `make verify` coverage ≥ 89% 且 `make release-check` 通过；不得降低阈值或补基线覆盖。
- clean c8df main 的 `make verify` 为 ruff/format/mypy 通过、980 passed、155 deselected，但 coverage `87.27% < 89%` 非零退出；`release-check` 经 `verify`，前置条件未关闭。本复核不形成 Core、Product、staging 或 production release 结论。

## 2026-08-20 执行前复核补充

- 复现确认 4 个 RED：missing/null usage 两参数、roles/context/content_schema_ref/ArtifactRef 丢失、
  schema/provider 交错 retry 后 `schema_retries=0`；`tests/inference` 目录单跑与全量一致。
- 新发现第 4 根因（原记录未覆盖）：`test_present_context_survives_typed_request_revalidation`
  在 coverage 模式全量运行下稳定失败（`InferenceError: deadline_exceeded`），无 coverage 时通过，
  `tests/inference` 单跑也通过。定位：`InvocationBudget` 时钟自构造起算，adapter 的
  `asyncio.timeout(budget.remaining_seconds)` 把 canonical 校验与 PydanticAI schema/Agent 构建等
  本地非 provider CPU 时间计入 `deadline_ms`，环境变慢（coverage 插桩）即误报 DEADLINE_EXCEEDED。
  该缺陷属于 deadline 语义未闭合，与上述 3 根因同属 WS-5 production-inference owner 修复范围。
- 当前树（含未提交 RED 证据）实测：`tests -m "not integration" --cov` 为 5 failed、980 passed，
  coverage 87.36%；无 coverage 运行为 4 failed。

## 2026-08-20 修复检查点：四个根因关闭与基线门禁恢复

- 分支 `codex/ws-5-core-release-proof` 三个新 commit：
  `5b76d83`（固化 RED 证据与本 flaky 记录）、`15e189d`（四根因修复）、`a9d3719`（协议单测补门禁）。
- 根因关闭方式（`15e189d`）：
  - missing/null usage：`LedgerTransport` 对 2xx 成功响应缺失/`null`/空 usage fail closed 为
    `PROVIDER_PERMANENT`；非 2xx 错误响应容忍缺失 usage（provider 错误体常无 usage），存在的 usage 仍记账。
  - canonical 保真：adapter 以 role-preserving 多消息构造替换单串拍平（system/user/assistant 直映射，
    tool 映射为 user），`content_schema_ref`/context/context_refs/input schema ref 以有界 provenance 行
    进入 provider 请求；golden 测试断言 wire roles 与完整 ref/hash 编码。
  - schema_retries：改由共享 ledger 推导（`physical_sends − agent.run 调用数`），不再依赖只反映最后一次
    agent.run 的 `usage.requests`；provider 失败 run 内已发生的 schema retry 不再丢失。
  - deadline 语义：决策为 `deadline_ms` 只计 provider 交互时间——ledger 时钟在首次 `reserve_send` 起算
    （ledger 保持唯一 deadline 权威），删除 adapter 中包裹 `agent.run` 的第二层墙钟窗口（本地
    校验/schema 构建不再消耗 deadline；挂起 send 由 per-send httpx timeout 与 ledger reserve 检查约束）。
    两个确定性 transport 级回归分别固定“本地准备不消耗 deadline”与“deadline 首启于首次发送、跨发送过期”。
- 基线 coverage 门禁（`a9d3719`）：`worker/inference.py` 29.34%→90.91%、`observation/projection.py`
  37.50%→97.66%，均为 fake/协议单测（真实签名链 + 真 verifier 子进程 + MockTransport；scripted session），
  非 coverage 灌水；阈值未调整。附带修复：reconciler 错误退避睡眠期间收到 shutdown 会以未捕获
  `ProjectionShutdown` 逃出 `run()` 并崩溃角色入口（`_run_projection_loop` 不捕获该异常），现改为干净停止，
  回归已固定。该修复属 WS-4 模块的 shutdown hygiene，已在 commit message 中披露。
- 验证：`make verify` 通过（ruff/format/mypy、1010 passed、155 deselected、coverage 89.33% ≥ 89%）。
  coverage 模式全量重跑 3 次以上未见 `test_present_context_survives_typed_request_revalidation` 复发。
  随后 `make integration` 在真实 PostgreSQL/Compose 上通过：152 passed、3 skipped（含 runtime_worker
  SIGKILL 崩溃恢复矩阵与签名 G2 gateway 本地 transport 切片），manifest-check 与 reverse validation
  通过，容器清理完成、无残留；`release verification deferred: source.dirty=true` 系当时工作树含
  未提交 BLOCKED.md 更新，cleanroom `release-check` 需在干净树上按 WS-5 验收流程另行执行。
- 本检查点关闭 2026-08-14 复核记录的 3 根因/4 RED 与 coverage 基线 blocker；不形成 Core、Product、
  staging 或 production release 结论，也不标记 WS-5 `verified`。Evaluation/Publication、完整 cleanroom
  Gate matrix、外部 issuer 的完整 G2、`ImplementationAcceptanceRecord` 与负责人批准仍待完成，
  WS-5 delivery 保持 `in_progress`。

## 2026-08-20 路线调整：WS-5 收窄为 MVP-ready Core freeze（负责人批准）

- 负责人当日指示按 MVP 方向调整路线：优化现有代码、移除不必须阻碍、优先价值验证。
- 决策记录见 `ROADMAP.md`“2026-08-20 范围调整”与 WS-5 任务书“2026-08-20 范围修订”节：
  30 天容量、PITR/role 全矩阵、G8/POC-H Evaluation/Publication、外部 issuer ceremony、
  Core IAR 与负责人批准整体移至 WS-7 前置，不再阻塞 WS-6。
- 收窄版 WS-5 剩余事项：本地签发工具（`scripts/ws5_issue_provider_binding.py`）+ runbook、
  clean source `make release-check` 通过。完成后 WS-5 按 ROADMAP 规则进入负责人验收流程。
- WS-6 成为当前焦点；首要工程里程碑为图节点内真实 inference（production TypedInferencePort
  在真实 Run 中被调用）。上一节“仍待完成”的发布证明义务由 WS-7 承接，本节 supersede 其排期语义。
- 收窄版 WS-5 退出条件已全部满足（2026-08-20）：(1) production inference seam 闭环
  （`15e189d`，含 usage fail-closed、canonical 保真、统一 retry ledger 与 deadline 语义）；
  (2) 本地签发工具 `scripts/ws5_issue_provider_binding.py` + runbook 落地（`03ff4f6`），
  其产物经真实 cleanroom verifier 验证并支撑 G2 smoke（5 个端到端用例）；
  (3) clean source `make release-check` 通过（verify 1015 passed/coverage 89.33%、
  manifest reverse validation、真实 PostgreSQL integration 152 passed、
  `--require-release` manifest 验证 valid）。WS-5 delivery 进入负责人验收流程；
  按 ROADMAP 规则，`verified` 状态需负责人显式批准。
