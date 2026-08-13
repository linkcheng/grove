# BLOCKED

最后更新：2026-08-13

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
- WS-1 owner 已交付统一 safe canonical codec/registry、typed request storage revalidation、
  request/context/artifact invariants 与 zero-user-code/zero-provider 旁路族测试；独立复审未发现未关闭
  P1/P2。
- WS-5 production port 只从 Runtime Worker composition root 构造：root 先调用 isolated cleanroom
  验证签名 release identity，再按 candidate hash 重读 `ProviderBindingManifest`；实际 SDK、PydanticAI、
  adapter、Runtime Build、endpoint、model、schema 与 policy 均与 Manifest 逐项绑定。Graph/Node 只可见
  `TypedInferencePort.infer(request, *, result_type)`。
- SDK retry 固定为零；provider retry 与 schema repair 共享同一个物理 transport attempt/deadline/token/
  cost ledger。Worker-owned 一次性 G2 入口从已验证 Manifest 内部构造 canonical request；真实
  OpenAI-compatible Provider slice 已通过签名 `prompted/json_object` profile，最终请求、structured
  output、usage、pricing 与 transport 观察到的单次物理请求一致。fresh PostgreSQL volume 上的完整
  Runtime Worker 崩溃恢复矩阵为 9 passed，并在验收后清理临时容器与 volume。
- 该检查点只关闭 production inference 子范围。Evaluation/Publication、完整 cleanroom Gate matrix、
  Runtime Worker + PostgreSQL/RLS/PostgresSaver + 外部 issuer 的完整 G2、actual runtime image identity、
  `ImplementationAcceptanceRecord` 与负责人批准仍待后续完成，因此 WS-5 delivery 仅为 `in_progress`；
  当前 Provider 与 PostgreSQL/Worker 证据仍是同一代码的两个集成切片，且测试内签发 fixture 不作为
  外部 trust-root 或完整 G2 证据；本记录不形成 Core、Product、staging 或 production release 结论。
