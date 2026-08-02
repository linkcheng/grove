---
status: accepted
scope: core-deployment
---

# 从按角色分进程的模块化单体开始

GROVE 的逻辑模块用于固定职责、状态所有权和稳定 seam，不等于网络服务。MVP 使用一个
代码库和一组内容寻址发布制品，在进程层按工作负载与故障影响拆分角色：

1. **API Role**：承载 Plan、Execution、Observation HTTP/SSE；保持无状态，只接受、
   查询和投影命令，永不在请求进程执行 Graph。
2. **Runtime Worker Role**：claim Run Command、取得 lease/fence、调用固定版本的
   LangGraph Kernel 并写 checkpoint；不暴露公共执行 HTTP 入口。
3. **Projection/Reconciliation Role**：处理 outbox、Interaction/UI/Inspect 投影、
   gap reconciliation 与 orphan cleanup。MVP 可复用 Worker binary，但必须使用独立
   queue、并发、连接池与资源配额。
4. **Governance/Evaluation Role**：执行离线 Evaluation、Experience/Evolution 和
   Publication control-plane job。MVP 可复用代码库，但使用独立 Worker 与资源池，
   其故障或饱和不能阻断在线 Run。

OpenTelemetry Collector 属于可替换的诊断基础设施，不是应用进程角色，也不拥有
Run、审计或 Projection 状态。MVP 可以共享 PostgreSQL database，但 API、在线
Worker、Projection/Reconciliation、离线 Governance/Evaluation 和运维必须使用
职责最小化的独立 database role、连接池与资源配额；共享数据库不等于共享权限或
无界争用。

模块之间先通过同一进程内的稳定 typed interface 组合。不得因为文档中存在一个
Module 名称，就为其增加网络协议、独立数据库、分布式事务或最终一致性补偿。

物理演进顺序固定为：

1. 在相同 contract 下水平扩展上述进程角色；
2. 为在线、投影和离线工作负载拆分独立池、资源限额与部署单元；
3. 当 Tenant 的合规、容量、数据驻留或故障域需要时，按
   [ADR-0009](./0009-start-shared-and-scale-through-deployment-cells.md) 放入独立
   Deployment Cell；
4. 只有存在可复现证据时才抽取网络服务。

服务抽取至少要满足以下一项，并证明现有角色隔离或 Deployment Cell 不能以更低
复杂度解决：

- 需要独立 SLO、扩缩容曲线或故障域；
- 存在明确的数据所有权、监管或驻留边界；
- 存在长期独立的团队所有权与发布节奏；
- 现有稳定 seam 已经由 contract、故障和兼容测试证明；
- 可测的资源争用无法通过独立进程、连接池、配额或 Cell 消除。

服务抽取必须保留 Canonical Contract、Tenant 语义与唯一状态所有者。若抽取会引入
远程一致性、重复投递、分布式事务、新恢复真相或跨服务授权传播，必须另立 ADR 并
补齐相应 contract、failure、security、load 和 recovery evidence；不能把这些代价
隐藏在 adapter 或 middleware 内。

## Consequences

- MVP 具有独立扩缩容和故障隔离的进程角色，但不承担微服务运维成本。
- 同一 binary 可以承载多个 role entrypoint；生产部署一次只启用声明的角色，禁止
  API 进程因配置漂移顺带执行 Graph 或离线任务。
- API、在线 Worker、Projector/Reconciler 与离线 Governance/Evaluation 的
  readiness、database credential、pool、quota 和 SLO 分别验收。
- 水平扩容和角色故障不能改变 command、fence、checkpoint、event 或 projection
  语义；只有吞吐与可用性可以变化。
- 从模块化单体抽取服务是有证据的演进动作，不是 MVP 完成条件。
