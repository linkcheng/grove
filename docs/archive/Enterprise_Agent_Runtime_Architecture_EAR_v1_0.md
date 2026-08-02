# Enterprise Agent Runtime Architecture（EAR）v1.0

> **状态：Superseded / 仅供历史追溯。**
>
> 本文的 `pydantic_graph + ExecutionContext + arq` 路线已被
> 历史文档；现行设计见 [GROVE Architecture](../00_GROVE_Architecture.md)。
> 和 accepted ADR 取代。不得据此实现、验收或恢复现行 EAR。

## 一个只关心执行的 Agent Runtime 内核

> 技术路线：PydanticAI（Agent 抽象 + 流式推理）+ pydantic_graph（执行
> 引擎）+ Pydantic Model（数据模型）+ Python Async（运行时）+ arq
> （异步队列 + 定时任务）+ PostgreSQL(+pgvector)（持久化与事件存储）
>
> + Redis（可选事件广播）
>   不为兼容 LangGraph / CrewAI / AutoGen / Semantic Kernel / Dify 等
>   框架设计 Adapter 或 SPI；不提前设计可插拔执行引擎。

---

# 0. 核心原则

> **Runtime 不应该关心业务，而应该只关心执行。**

三条派生原则决定了整个系统哪些概念该留、哪些概念该删：

1. **Registry 不是 Runtime 的核心，它是 Runtime 的入口**——资产治理
   动作收缩为一个只做一件事的 **Resolver**：把业务配置解析成
   `ExecutionPlan`，然后**退出 Runtime 边界**。
2. **Runtime 永远只执行一种对象：`ExecutionPlan`**——Agent、Skill、
   Workflow、Graph、Prompt、Multi-Agent、Swarm、GoalLoop 这些业务
   概念全部在进入 Runtime 之前编译为 `ExecutionPlan`。
3. **能力扩展只保留两类：`Inference` 与 `Tool`**——不再细分中间层；
   新增需求（流式输出、结构化引用、事件观测）优先通过**丰富已有对象
   的数据契约**来满足，而不是新增对象。这个二分是排他的，不是并列
   的：`Inference` 只能思考不能动手，一切外部访问必须经由图节点显式
   的 `Tool` binding 发生（5.2 节），否则"能力扩展只有两类"会在
   `Inference` 内部悄悄长出第三类未被 Runtime 感知的能力。

## 0.1 Runtime 的六个核心概念

```
┌──────────────────────────────────────────────────────────────┐
│                  Business Application                        │
│──────────────────────────────────────────────────────────────│
│ Agent / Workflow / Skill / Prompt / DSL / Graph                │
│ Multi-Agent / Sub-agent / Swarm / GoalLoop                     │
│ Cron / REST / MQ / Webhook（触发方式）                          │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│             Resolve ExecutionPlan（Compile）                  │
│──────────────────────────────────────────────────────────────│
│ Registry（资产治理，Runtime 边界之外）                           │
│ Resolver（唯一职责：编译出 ExecutionPlan）                       │
└──────────────────────────────────────────────────────────────┘

════════════════════════ Runtime Boundary ════════════════════════

┌──────────────────────────────────────────────────────────────┐
│                     Agent Kernel                              │
│──────────────────────────────────────────────────────────────│
│ ExecutionContext（状态）                                        │
│ Executor（pydantic_graph，唯一执行实现）                         │
│ Lifecycle（Pause/Resume/Cancel/Retry/Checkpoint/发出 RuntimeEvent）│
└──────────────────────────────────────────────────────────────┘
                            │  统一契约调用
             ┌──────────────┴──────────────┐
             ▼                             ▼
      InferenceBinding               ToolBinding
     (LLM / Rules / Planner)      (SQL / API / MCP ...)
     返回 ExtensionResult{data, artifacts}
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                  Infrastructure                               │
│──────────────────────────────────────────────────────────────│
│ PostgreSQL / Redis / pgvector / Model API / MCP                 │
│                                                                │
│ RuntimeEvent 的消费者（订阅同一份事件流，不属于 Runtime）：        │
│ SSE（前端实时展示） / Event Store（回放） / Audit / Notify /      │
│ Trace（Langfuse） / Metrics                                    │
└──────────────────────────────────────────────────────────────┘
```

**六个核心概念**：`ExecutionPlan`、`ExecutionContext`、`Executor`、
`Lifecycle`、`Inference`、`Tool`。`RuntimeEvent` 与
`ExecutionArtifact` 不是第七、第八个概念——前者是 `Lifecycle` 既有
"事件通知"职责的正式数据契约，后者是 `Tool`/`Inference` 返回值里
专供展示的可选部分，两者都挂在已有对象上，不新增独立行为主体。

**"Registry" 这个词在本文档里特指且仅特指边界外的资产治理层**（上图
"Resolve ExecutionPlan"层的那一个），管的是 Skill/Workflow/Agent 这类
业务资产的审批、版本、上下架——这是唯一有资格单独叫"Registry"、不
带任何限定词的概念。Runtime 边界**内部**还存在两类字面上也叫"注册表"
但性质完全不同的基础设施级查找表，为避免和边界外的 Registry 混用同一
个词造成误读，本文档统一加限定词区分：**Class Registry**（3.2 节，
节点类/adapter 字符串引用到具体实现类的代码层查找表）与 **Graph
Registry**（3.2 节，拓扑定义的内容寻址存储）。三者的共同点仅仅是字面
上都含"注册"二字，语义、生命周期、读写主体完全不同——业务侧的
Registry 由人审批变更，Class Registry 由 CI 流水线的 Schema diff 拦截
变更，Graph Registry 干脆禁止对已有 key 覆写。

## 0.2 每个概念/契约的存在理由

| 概念/契约                           | 是否是独立对象                                    | 为什么存在                                                                                                                                                                                       |
| ----------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ExecutionPlan`                   | 是                                                | Runtime 唯一认识的执行对象，不理解业务语义                                                                                                                                                       |
| `ExecutionContext`                | 是                                                | 一次执行需要一份可持久化、可恢复的运行时状态                                                                                                                                                     |
| `Executor`                        | 是（唯一实现，不做接口）                          | 实际驱动图执行的引擎，pydantic_graph 一种引擎已覆盖所有图形状需求                                                                                                                                |
| `Lifecycle`                       | 是                                                | 暂停/恢复/取消/完成/失败/Checkpoint/事件发布本质是同一类动作                                                                                                                                     |
| `Inference`                       | 是（Protocol，多实现）                            | LLM/Rules/Planner/Evaluator 存在且将持续新增独立实现                                                                                                                                             |
| `Tool`                            | 是（Protocol，多实现）                            | SQL/HTTP/MCP/Vector 等存在且将持续新增独立实现                                                                                                                                                   |
| `RuntimeEvent`                    | 否，是 Lifecycle 的数据契约                       | 让 SSE/Event Store/Audit/Notify/Trace/Metrics 五个独立消费者复用同一份事件，而不是各自接入 Executor                                                                                              |
| `ExecutionArtifact`               | 否，是 Tool/Inference 返回值的可选部分            | 让 RAG/SQL/GraphDB/OCR/文件生成等多个独立生产者复用同一套"结构化展示物"契约                                                                                                                      |
| `Registry`/`Resolver`           | 不在 Runtime 边界内                               | Runtime 的输入来源，不是 Runtime 本身                                                                                                                                                            |
| `Planning`/`Autonomous` binding | 是（`node_bindings` 第三类 `kind`，见 4.2.2） | `Inference`/`Tool` 的返回值只产出 `data`/`artifacts`，不驱动图的走向；动态拓扑生成（挂起+派生子 Context）与委托执行（事中拦截+挂起）需要执行结果直接触发控制流，这条能力无法用前两者表达 |

---

# 1. 设计原则

| 原则                                 | 说明                                                                                                                                                                                                                                                                                |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Runtime 只认 ExecutionPlan           | 业务概念（含 Multi-Agent/Swarm/GoalLoop）全部在编译期转换为图，Runtime 只看图                                                                                                                                                                                                       |
| Registry 是入口，不是核心            | 治理、审批、版本管理都在 Runtime 之外                                                                                                                                                                                                                                               |
| 没有两个实现就不要抽象接口           | `Executor` 唯一实现不做 Protocol；`Inference`/`Tool` 多实现做 Protocol                                                                                                                                                                                                        |
| Kernel 只保留三个对象                | `ExecutionContext`/`Executor`/`Lifecycle`，不因为新需求（流式、事件、观测）膨胀出新对象                                                                                                                                                                                       |
| 新需求优先丰富数据契约，而非新增对象 | `RuntimeEvent`、`ExecutionArtifact`、`ExtensionResult` 都是这一原则的产物                                                                                                                                                                                                     |
| 能力扩展分两个层次                   | 产出数据/展示物的能力归两类——一切"智能推理"归`Inference`，一切"外部访问"归 `Tool`；需要直接触发 Runtime 控制流（挂起/派生子 Context/拒绝并路由）的能力归第三类，准入条件是"执行结果必须影响图的走向本身"，不满足这条的新需求一律并入前两类，不新增第四个 `kind`（见 4.2.2） |
| 不为兼容其它框架预留抽象             | 技术路线收敛到 Pydantic 全家桶 + arq                                                                                                                                                                                                                                                |
| Gate 分级                            | 是否需要 HITL 只看动作是否不可逆/对外可见                                                                                                                                                                                                                                           |
| 任务执行与页面展示彻底解耦           | 页面通过订阅`RuntimeEvent` 观察执行过程，不直接持有 Executor 引用；页面刷新/离线重连通过 Event Store 回放历史，不丢失任何过程                                                                                                                                                     |

---

# 2. Business Application（Runtime 边界之外）

## 2.1 业务概念与图形化表达

Agent、Skill、Workflow、Prompt、DSL、Multi-Agent、Sub-agent、Swarm、
GoalLoop 都在这一层自由使用，最终都编译成 `pydantic_graph` 的某种
图形状：

| 业务概念                                                | 图形状                                                                                                           |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| 单 Skill                                                | 单节点图                                                                                                         |
| Workflow / 协调者-专家型 Multi-Agent                    | 单向串行的多节点图；"协调多个专家并发工作"发生在协调者节点内部（`asyncio.gather`），不在拓扑上开叉             |
| Sub-agent（需要返回结果，同进程可接受阻塞等待）         | 调用边界内的子图，或包装成一次`Tool` 调用                                                                      |
| Sub-agent（需要返回结果，但不适合阻塞等待，如耗时较长） | 派生独立的子`ExecutionContext`（见 4.4），父节点走 `SuspendNode` 挂起，子任务完成后异步唤醒                  |
| Swarm（移交、不返回）                                   | 普通链式边，`next_node()` 从 A 走到 B 即完成移交；也可以是派生子 `ExecutionContext` 后父节点直接结束，不等待 |
| GoalLoop（自主循环）                                    | 带回边的图，停止条件由节点自身读写迭代计数器判断                                                                 |

**拓扑在编译期强制为单向串行**：`pydantic_graph` 的 `BaseNode.run()`
按定义只返回唯一一个下一节点，引擎本身没有"多个节点同时处于 Active
状态"这件事，因此拓扑级 fork-join 不是这个引擎的自然形状。业务上
需要"同时调用多个 Tool"（例如同时查工商和法院信息），一律封装进
单个节点内部用 `asyncio.gather` 做 I/O 并发，对外仍是一次节点执行、
一次 `state.record()` 单点写入：

```python
# 拓扑依然是单向串行的一个节点，并发只发生在节点内部
async def run(self, ctx: GraphRunContext[GraphState]) -> "SkillNode | End":
    task1 = call_ic_api()
    task2 = call_court_api()
    res1, res2 = await asyncio.gather(task1, task2)
    ctx.state.record(self.node_name, {"ic": res1, "court": res2})
    ...
```

这一约束由 Resolver 在编译期强制（见 3.1），不是节点作者的自觉遵守。
代价是：`asyncio.gather` 内部的子任务不能各自拥有独立的 HITL 网关，
也没有子任务粒度的 checkpoint——进程崩溃发生在 `gather` 执行到一半
时，整个节点需要从头重跑所有子任务（可以用 `asyncio.as_completed`
替代 `gather`，让已完成的子任务提前落一次中间结果到各自命名空间的
key 下，缩小重跑范围，但仍然没有独立 HITL 网关）。如果某个并行分支
确实需要独立的 HITL 网关或独立的失败/重试语义，说明它不该被塞进
`gather`，而应该拆成 4.4 节的子 `ExecutionContext`。

四种模式共用同一个执行引擎、同一套 `RuntimeEvent`，Executor 不需要
识别"这是第几种模式"，只按图定义推进。

## 2.2 触发方式

`Cron`/`REST`/`MQ`/`Webhook` 是同一层的四种触发方式，最终都调用同一
个入口 `Application.submit(business_config, trigger_type, tenant_id)`：

```python
class Application:
    async def submit(self, business_config: dict, mode: str = "sync",
                      trigger_type: str = "http", tenant_id: str = "default",
                      parent_context_id: str | None = None) -> ExecutionContext:
        plan = self.resolver.resolve(business_config, {"tenant_id": tenant_id})
        ...  # 见第 7 章
```

- **REST**：API Gateway 直接调用，`trigger_type="http"`。
- **Webhook**：外部系统回调 HTTP 接口，`trigger_type="event"`。
- **MQ**：订阅消息队列 topic，收到消息即调用 `submit`，
  `trigger_type="event"`。
- **Cron（定时检测派发）**：复用 arq 已有的定时任务能力，不引入
  APScheduler/Celery beat 等额外调度组件：

```python
from arq import cron

async def detect_and_dispatch(ctx):
    """定时检测：扫描满足条件的业务对象，逐一派发执行。
    这段逻辑完全是 Business Application 层的业务代码，
    Runtime 对"定时"和"批量派发"这两件事没有任何专门支持，
    也不需要——它们只是调用 submit() 的次数变多了而已。"""
    candidates = await query_pending_assets()  # 业务查询，不经过 Runtime
    for asset in candidates:
        await application.submit(
            {"skill": "risk_scan", "asset_id": asset.id},
            trigger_type="scheduled",
        )

class WorkerSettings:
    cron_jobs = [cron(detect_and_dispatch, hour={0, 6, 12, 18})]
```

四种触发方式最终产生的 `ExecutionContext` 记录里 `trigger_type`
字段不同，其余处理路径完全一致，Runtime 不区分对待。

---

# 3. Resolver 与 ExecutionPlan

## 3.1 Resolver 的唯一职责

```python
class Resolver:
    """Runtime 边界之外的唯一入口：把业务配置（含已编译好的图定义）
    解析成一份可以直接执行的 ExecutionPlan。Skill 怎么注册、怎么
    发布、怎么审批、怎么做版本管理——这些资产治理能力如果需要，
    应作为独立的 Asset Management 模块建在 Resolver 背后，Runtime
    完全不关心。"""

    def resolve(self, business_config: dict, ctx_hint: dict) -> "ExecutionPlan":
        ...
```

**编译期校验：拒绝拓扑分叉**。`resolve()` 在生成 `ExecutionPlan` 前
必须校验拓扑定义（3.2 节的 Topology DSL）不包含 fork-join 形状——
任意节点的出边数量超过 1（`GoalLoop` 的回边、`HitlPolicy` 的 Reject
回边除外，它们是同一条边在不同条件下走向不同目标，不构成并发分叉）
一律编译失败，把错误消息指回业务配置里那个具体节点。这把 2.1 节
"拓扑在编译期强制为单向串行"从约定变成机械拦截，不依赖图定义作者
的自觉遵守。

## 3.2 ExecutionPlan：唯一穿越 Runtime 边界的对象

```python
class ExtensionResult(BaseModel):
    """Tool/Inference 的统一返回信封：data 供图节点逻辑继续使用，
    artifacts 只供展示，不影响图的推进逻辑。"""
    data: dict
    artifacts: list["ExecutionArtifact"] = []

class ToolBinding(BaseModel):
    kind: Literal["tool"] = "tool"
    adapter: str
    config: dict

class InferenceBinding(BaseModel):
    kind: Literal["inference"] = "inference"
    adapter: str
    config: dict

class HitlPolicy(BaseModel):
    max_reject_count: int = 0
    on_reject_exhausted: Literal["fail"] = "fail"

class ExecutionPlan(BaseModel):
    plan_id: str
    plan_version: str
    graph_ref: str                  # pydantic_graph 图定义引用
    node_bindings: dict[str, "ToolBinding | InferenceBinding | PlanningBinding | AutonomousBinding"]
    hitl_policy: HitlPolicy | None = None
    sync_timeout_ms: int = 5000
    tenant_id: str
    metadata: dict = {}
```

不可变、自包含：一旦生成不再修改，图中每个节点的绑定关系全部固化，
Runtime 运行期间不回头调用 Resolver——**唯一例外是 `kind="planning"`
的节点在其 `allowed_node_classes` 授权范围内编译子拓扑草稿**（4.2.2
节 `resolve_subgraph()`）：这次调用产出的是一份新的、同样不可变的
`ExecutionPlan`（供派生的子 `ExecutionContext` 使用），不是对当前
这份 `ExecutionPlan` 本身的修改，"一旦生成不再修改"这条不变式对
`Plan` 自身依然成立，被突破的只是"运行期不回头调用 Resolver"这半句，
且突破范围被 `allowed_node_classes` 严格限定，不是无差别开放。

**`graph_ref` 指向的是"拓扑声明"，不是业务逻辑**：`graph_ref` 引用
的 Graph 定义只做两件事——声明节点连线（谁接在谁后面、条件分支走哪
条边）、以及每个拓扑节点绑定到哪一个**节点类的字符串引用**（例如
`nodes.risk.RiskAnalysisNode`）。这个字符串引用与 `ToolBinding.adapter`
是同一种模式：Runtime 运行期拿这个字符串去 **Class Registry**（0.1
节已说明，与边界外的资产治理 Registry 是两个不同的东西，这里的
Class Registry 纯粹是代码层面的字符串到具体类/adapter 实现的查找表）
里查当前已注册的具体类，不关心类内部的业务逻辑、变量映射或数据
清洗——那些都在节点类自己的 Python 实现里，不进入 JSON。

**节点代码可以升级，`graph_ref` 因此可以保持稳定**：只要
`nodes.risk.RiskAnalysisNode` 的输入/输出契约（用节点类自带的
Pydantic Model 显式声明）没有发生破坏性变更，内部逻辑修 Bug、调优
都可以直接上线，历史断点任务唤醒时装配出的还是同一份拓扑，只是跑
的是修过 Bug 的新代码，这是安全的。如果发生破坏性变更（删字段、改
类型、新增必填字段），必须注册为新类（`nodes.risk.RiskAnalysisNodeV2`），
旧的 `graph_ref` 继续绑定 V1，新拓扑绑定 V2，版本天然隔离，不需要
在一个 Python 进程里做多版本代码热加载。"非破坏性"不能只靠自觉：
CI 流水线对每个节点类的输入/输出 Model 做 JSON Schema diff，检测到
不兼容变更且类名未跟着升级版本号，直接拦截合并——把"应该开新类"
从约定变成机械拦截。

**`graph_ref` 的不可变性契约**：即使拓扑 DSL 本身是纯数据，`graph_ref`
仍然必须指向 Graph Registry（同样是 0.1 节说明过的、Runtime 内部的
基础设施级存储，不是边界外的资产治理 Registry）里内容寻址、只增不改
的一条记录（同一个 `graph_ref` 在任何时间点解析出的拓扑定义必须是
同一份字节），Graph Registry 对已存在的 key 禁止覆写，变更拓扑连线
一律生成新的 `graph_ref`（与 `plan_version` 一同递增）。这与 Git commit
hash、Docker image digest 是同一个模式——`load_graph(plan.graph_ref)`
是一次引用透明的查询，不是一次可能变化的解析。缺了这条不变式，第 9
章"灾备/降级"里"重启后按 status 从断点恢复"就无法真正保证：如果
Graph Registry 允许原地覆写某个 `graph_ref`，历史 `ExecutionContext`
恢复时执行的就不再是原来提交时的那份拓扑。

**Graph 与 ExecutionPlan 不是重复描述，而是两个变化频率不同的维度**：
Graph 是纯拓扑（节点类型引用、边、分支/回边形状），随 Skill/Workflow
版本演进；`ExecutionPlan` 是"拓扑引用 + 该次提交的具体绑定"（`node_bindings`
/`hitl_policy`/`tenant_id`），随每次提交变化。同一个 Graph 拓扑会被
成千上万个不同租户、不同绑定的 `ExecutionPlan` 复用，把 Graph 内联进
每个 Plan 是纯粹的存储浪费。Compile 层真正的产出是一对——「注册在
`graph_ref` 下的不可变 Graph 对象」+「引用它的 `ExecutionPlan`」——
而不是 `ExecutionPlan` 单独一个输出物；Runtime 运行期对 `graph_ref`
的解析只是查询这个已经在编译期定型的映射，不构成对 Compile 层的
反向依赖。

---

# 4. Agent Kernel

## 4.1 ExecutionContext（状态）

```python
class SuspendType(str, Enum):
    HITL = "hitl"                              # 人工审批/干预
    CHILD_CONTEXT = "child_context_completion"  # 等待子 Context 达到终态
    SIGNAL = "signal"                          # 等待外部信号（如 Webhook 异步回调）
    TOOL_CALL_PERMISSION = "tool_call_permission"  # 见 4.2.2，AutonomousNode
        # 内部单次工具调用命中 ask 规则时挂起，粒度比 HITL 更细（工具调用
        # 级而非节点级），但复用同一套 SuspendContract/唤醒机制，不是
        # 独立的挂起体系

class SuspendContract(BaseModel):
    """挂起的具体意图，替代扁平的 suspend_reason 字符串。
    status 只保留一个 'suspended' 终态，具体是等谁、允许谁唤醒，
    全部下放到这个强类型结构里，不再往 status/graph_state 里塞控制流
    元数据——graph_state 只放业务节点的输出，这个契约只放"我在等什么"。"""
    type: SuspendType
    waiting_for_target_id: str | None = None  # 目前只支持等待单个目标；
        # 如果未来需要"等待 N 个子任务全部完成"，扩展为 list[str] 并
        # 定义清楚是"全部完成才唤醒"还是"任一完成即唤醒"，现在没有这个
        # 需求就不提前引入这个复杂度。
    allowed_triggers: list[str]

    @classmethod
    def default_for(cls, type: SuspendType, target_id: str | None = None) -> "SuspendContract":
        """按 type 派生默认的 allowed_triggers，避免每个调用点手写一份、
        两处定义容易失联。需要突破默认值的场景（目前没有）再显式传参
        覆盖，而不是每次都重复声明。"""
        defaults = {
            SuspendType.HITL: ["hitl_approve", "hitl_reject"],
            SuspendType.CHILD_CONTEXT: ["child_context_completion"],
            SuspendType.SIGNAL: ["external_webhook"],
            SuspendType.TOOL_CALL_PERMISSION: ["tool_call_allow", "tool_call_deny", "tool_call_cancel"],
                # 三态而非二态：deny 是主动拒绝，cancel 是"超时/未获得
                # 明确选择就被中止"，两者下游处理不同（deny 更适合直接
                # 终止或升级人工介入，cancel 更适合重试/降级），参考
                # MCP Elicitation 的 accept/decline/cancel 三态设计，
                # 见 4.2.2.2
        }
        return cls(type=type, waiting_for_target_id=target_id, allowed_triggers=defaults[type])
```

```sql
CREATE TABLE execution_context (
    context_id       UUID PRIMARY KEY,
    parent_context_id UUID REFERENCES execution_context(context_id),  -- 子任务指向父任务，独立任务为 NULL
    root_context_id  UUID,               -- 指向链路最顶层 Context（顶层自身为 NULL 或指向自己），
                                          -- 用于 4.2.2 节 PlanningNode 全局委托预算的 O(1) 查询，
                                          -- 是下方"树状追溯"递归 CTE 的常数时间优化版本，
                                          -- 两者描述同一条血缘关系，不是两套机制
    plan_depth       INT NOT NULL DEFAULT 0,  -- PlanningNode 递归派生深度，顶层为 0，见 4.2.2
    session_id       UUID REFERENCES conversation_session(session_id),  -- 属于哪个跨提交会话，
                                          -- 独立任务为 NULL，见 4.5.1
    plan_id          TEXT NOT NULL,
    plan_version     TEXT NOT NULL,
    mode             TEXT NOT NULL,       -- sync/async/sync_degraded_async
    status           TEXT NOT NULL,       -- queued/running/suspended/completed/failed/cancelled
    suspend_contract JSONB,               -- status=suspended 时必填，SuspendContract 的序列化
    cancel_requested_at TIMESTAMPTZ,      -- 非空表示已收到取消请求；status='running' 时
                                           -- 用于协作式取消的检查点信号，见 4.3 节
    current_node     TEXT,
    reject_count     INT NOT NULL DEFAULT 0,   -- 仅 suspend_contract.type='hitl' 时有意义
    graph_state      JSONB NOT NULL DEFAULT '{}',
    result           JSONB,               -- 终态时按 Graph.output_keys 从 graph_state 切出的
                                          -- 对外结果，供父任务 lifecycle.load_result() 读取，见 4.5.3
    has_autonomous_output BOOLEAN NOT NULL DEFAULT false,  -- AutonomousNode 执行过则置真，
                                          -- 随 SubAgentResult 逐层向父任务传播，见 4.5.3
    trace_id         TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    trigger_type     TEXT NOT NULL DEFAULT 'http',  -- http/event/scheduled
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now(),
    completed_at     TIMESTAMPTZ
);
CREATE INDEX ON execution_context (parent_context_id) WHERE parent_context_id IS NOT NULL;
CREATE INDEX ON execution_context (root_context_id) WHERE root_context_id IS NOT NULL;
```

`status` 只保留一个抽象的挂起终态 `suspended`，不再为每种挂起原因
设计独立的状态值——这条泛化是必须的：`status` 会被前端渲染、Notify
订阅、Audit 记录等多处下游代码读取，如果为每种原因都开一个状态值，
下游代码要么要枚举所有状态值做判断，要么容易漏掉新增的一种。具体
"在等什么、谁能唤醒"全部下放到 `suspend_contract` 这个强类型结构，
`status` 本身保持稳定。`parent_context_id` 是子 `ExecutionContext`
在创建时（`Application.submit()` 的可选参数，见 4.4）就写入的，不是
挂起动作发生后再补写——避免子任务已经在跑、血缘关系还没落库的窗口
期；`Application.submit()` 收到 `parent_context_id` 时强制用父
Context 的 `tenant_id` 覆盖调用方传入的值，不信任调用方自己保证
"子任务和父任务是同一个租户"——这是防止有人（或有 bug）借道子
Context 机制跨租户读写数据的最后一道保险，不是可选项。

## 4.2 Executor（唯一实现，基于 pydantic_graph）

```python
class Executor:
    """Runtime 内唯一的执行实现，不做 Protocol 抽象——
    当前只有 pydantic_graph 这一种执行引擎。"""

    def __init__(self, lifecycle: "Lifecycle"):
        self.lifecycle = lifecycle

    async def run(self, ctx: ExecutionContext, plan: ExecutionPlan) -> ExecutionContext:
        graph: Graph = load_graph(plan.graph_ref)
        state = GraphState.from_context(ctx, plan, emit=self.lifecycle.bind(ctx))
        await self.lifecycle.emit_bound(state, "ExecutionStarted", {})
        try:
            async with graph.iter(state=state) as run:
                async for node in run:
                    # 协作式取消检查点：每个节点边界查一次
                    # cancel_requested_at，不是抢占式 kill。取消请求和真正
                    # 生效之间允许有限延迟（至多一个节点的执行时间），
                    # 与 GoalLoop 的 max_iterations 是同一种"协作式而非
                    # 抢占式"的取向——付出的代价是一次轻量 SELECT，
                    # 与节点本身已有的 I/O 相比可以忽略。
                    if await self.lifecycle.is_cancel_requested(ctx.context_id):
                        await self.lifecycle.cancel_confirmed(ctx, node_name=getattr(node, "node_name", None))
                        return ctx
                    if isinstance(node, SuspendNode):  # PauseNode 更名为 SuspendNode，见下
                        await self.lifecycle.pause(ctx, node.node_name, contract=node.contract)
                        return ctx
                    if isinstance(node, End):
                        await self.lifecycle.complete(ctx)
                        return ctx
        except Exception as e:
            await self.lifecycle.fail(ctx, e)
        return ctx
```

**取消不是抢占式的**：`is_cancel_requested` 检查点只保证"下一个节点
不会开始"，不保证"当前正在进行的远端调用（LLM 推理、Tool 的外部
请求）被真正中断"——这与 7 章 `sync_degraded_async` 超时降级的取舍
是同一个立场：撤回一个已经发出去的外部请求本身不可靠，所以取消
生效后如果该节点的副作用之前已经落地，不做任何回滚，只是不再继续
往下推进图。需要"取消后确保外部副作用也停止"的场景（例如立刻终止
一次昂贵的模型调用），应由具体 `Inference`/`Tool` 实现自行监听
`asyncio.CancelledError` 或超时机制处理，Runtime 内核不做这个层面
的保证。

`SuspendNode`（原 `PauseNode`）携带一个 `contract: SuspendContract`
字段（4.1 节），默认 `SuspendContract.default_for(SuspendType.HITL)`
保持向后兼容——HITL 场景的节点代码不需要改动，只有 4.4 节的跨
Context 编排需要显式构造 `SuspendContract.default_for(SuspendType. CHILD_CONTEXT, target_id=child_ctx.context_id)`。改名是为了让类型
和它的用途在字面上一致：这个节点不只用于等人工确认，是"图执行需要
挂起，等待某个外部条件"的通用语义。

**`GraphState` 与 `ExecutionContext` 的所有权契约**：`GraphState` 不
是第二个持久化状态所有者，而是 `ExecutionContext` 在单次 `Executor.run()`
调用期间的运行期门面——`GraphState.from_context()` 持有 `ctx.graph_state`
（JSONB 字段对应的字典对象）的引用而非拷贝，`state.record()` 等方法
直接原地修改这个被引用的对象；`GraphState` 自身只额外携带不落库的
运行期专属字段（`emit` 闭包、`artifacts_so_far` 这类仅用于拼接
`citation` artifact 引用关系的中间态）。这一契约保证 `Lifecycle.pause`
/`complete`/`fail` 调用 `checkpoint(ctx)` 时读到的 `ctx.graph_state`
必然包含此前所有 `state.record()` 的结果，不需要额外一步"从 GraphState
同步回 ExecutionContext"——因为从来就只有一份数据、两个访问入口。
`ExecutionContext` 持有权威状态，`GraphState` 是为满足 `pydantic_graph`
`state=` 参数类型协议而存在的适配层，二者不构成职责重叠。若未来确
实需要引入不希望持久化、也不希望在 Pause 时保留的纯临时数据，应作
为 `GraphState` 上显式声明、生命周期与 `ctx.graph_state` 完全分离的
独立字段，而不是含糊地共用同一个状态对象。`GraphState` 还需要暴露
`context_id`/`tenant_id` 两个只读透传属性（转发到 `ctx.context_id`/
`ctx.tenant_id`），供 4.4 节"跨 Context 编排"里节点代码派生子任务时
使用，不需要节点持有 `ctx` 本身。

图节点是事件发出的主要位置——`NodeStarted`/`ToolStarted`/
`ToolCompleted`/`StateUpdated`/`NodeCompleted`/`ArtifactEmitted`
都在节点包装逻辑里统一发出，具体的 `Tool`/`Inference` 实现不需要
各自关心事件怎么发：

```python
@dataclass
class SkillNode(BaseNode[GraphState]):
    node_name: str

    async def run(self, ctx: GraphRunContext[GraphState]) -> "SuspendNode | SkillNode | End":
        state = ctx.state
        await state.emit("NodeStarted", {"node": self.node_name})
        binding = state.plan.node_bindings[self.node_name]

        if binding.kind == "tool":
            await state.emit("ToolStarted", {"node": self.node_name, "adapter": binding.adapter})
            result = await tool.call(binding, state)
            await state.emit("ToolCompleted", {"node": self.node_name, "adapter": binding.adapter})
        else:
            result = await inference.invoke(binding, state)  # 内部可能持续 emit InferenceDelta

        state.record(self.node_name, result.data)
        await state.emit("StateUpdated", {"node": self.node_name})
        if result.artifacts:
            await state.emit("ArtifactEmitted", {
                "node": self.node_name,
                "artifacts": [a.model_dump() for a in result.artifacts],
            })
        await state.emit("NodeCompleted", {"node": self.node_name})
        return next_node(result, state)
```

**`SkillNode` 只处理 `kind="tool"`/`"inference"` 两种绑定**，`if/else`
是二选一的封闭分支，不预留第三个 `elif`。`kind="planning"`/`"autonomous"`
不复用这段包装逻辑，各自是独立的节点类（`PlanningNode`/`AutonomousNode`，
见 4.2.2），有自己的 `run()` 实现——原因是这两种绑定的执行结果需要
直接决定返回值是不是 `SuspendNode`（挂起去等子任务或等工具调用审批），
而 `SkillNode.run()` 的返回值只由 `next_node(result, state)` 这一条
路径产生，`result` 只是数据，不可能变成控制流指令。把四种 `kind`
硬塞进同一个 `if/elif/elif/elif` 会让 `SkillNode` 同时承担"数据产出"
和"控制流决策"两种职责，违反 4.2.2 节"能力扩展分两个层次"里划的
那条界限。

单 Skill 执行、跨 Skill 编排、GoalLoop（回边）、Sub-agent（调用边界
子图或包装为 `Tool` 调用）、Swarm（链式移交、不返回）都是这个引擎
处理的不同图形状，事件粒度对四种模式完全一致，前端不需要区分"这是
第几种模式"。

## 4.2.1 评估记录：为什么不做 Graph Executor SPI（LangGraph/Temporal 并列后端）

**动议**：`pydantic_graph` 在 Agent 编排表达力上偏轻，LangGraph 更强；
提议保持 `ExecutionPlan` 作为 IR 不变，在 `Executor` 之上加一层
Graph Executor SPI，`pydantic_graph`/LangGraph/Temporal 作为并列可插拔
执行引擎。

**结论：不做，理由记录如下，供后续复议时参考。**

1. **不满足"两个实现"门槛**：0.1/1 章的抽象接口原则要求先有两个
   具体复用场景才做 Protocol 抽象。目前没有一个已经卡住的业务场景
   证明 pydantic_graph 表达力不足，"偏轻"是预判性判断，不是已发生的
   需求。真正出现具体场景时再评估，不提前铺路。
2. **IR 中立性会先破功**：3.1 节的拓扑分叉校验（拒绝出边数 >1 的
   节点）不是随意约束，它是 pydantic_graph 单一 next node 模型的直接
   产物，换来的是"一节点一次 `state.record()`"的审计/checkpoint 粒度
   保证。LangGraph 的核心优势恰恰是原生 fan-out/fan-in（`Send`、多
   目标边、reducer 合并）。若 `ExecutionPlan` 继续对所有后端统一禁止
   拓扑分叉，挂 LangGraph 得不到这部分表达力，新增后端没有意义；若
   放开限制让 LangGraph 后端支持分叉，`ExecutionPlan` 就不再是单一
   语义的 IR，退化成"校验规则取决于绑定哪个 Executor"——这正是 0 章
   "不为兼容其它框架预留抽象"要拦截的框架耦合，只是耦合对象换了个
   名字。
3. **Suspend/Resume/Checkpoint 需要按后端重新实现，不是薄适配器**：
   `SuspendContract`、协作式取消检查点、节点级幂等键目前都锚定在
   pydantic_graph 的节点边界（`graph.iter()` 的迭代点）。接入 LangGraph
   意味着要在它自己的 checkpointer/interrupt 机制上重新实现一遍等价
   语义，并保证两边对外的 `RuntimeEvent`/状态机行为完全一致——工作量
   与新增一个核心概念相当，不是"多注册一个执行引擎"这么轻。

**如果动机是 Agent 推理表达力不足，正确的落点是 5.2 节已有的
"决策边"模式，而不是换执行引擎**：ReAct 式"模型自主决定下一步"
不建模成某个框架的内部循环，而是 `Inference` 节点产出结构化决策、
图的条件分支路由到 `Tool` 节点、执行完回边——这个模式已经覆盖了
LangGraph 想解决的大部分场景，且每一步都是独立的、被 Runtime 感知
的图节点执行。把 LangGraph 当作 `Inference` 的一个实现直接嵌入同样
不成立：LangGraph Agent 的价值本来就来自它在一次调用内部自主决策

+ 调用工具，这与 5.2 节"`Inference` 内部禁止持有工具"的边界直接
  冲突——这不是 Executor 层的问题，绕开 Executor 换到 Inference 层
  一样会撞上同一条审计边界。

## 4.2.2 动态拓扑扩展方案：PlanningNode 与 AutonomousNode

4.2.1 节否决了"换执行引擎"这条路，但没有回答"模型能力越来越强，
拓扑闭集会不会成为真实瓶颈"这个问题本身。本节记录两种在不同开放
程度上回应这个问题的扩展方案——两者都不新增 `Executor`，都是在
`node_bindings` 里新增 `kind`，Resolver/Lifecycle/事件通道全部复用。
两者与"档位 3"（模型运行时决定执行一个编译期完全未声明过的节点/
工具，Resolver 事前不知道边界）的关系不同，必须分开看待，不能混
用同一套审计语义描述。

### 4.2.2.1 PlanningNode（档位 2 强化版）：闭集从"扁平列表"升级为

"可递归展开的白名单树"

**方案定位**：不是新执行引擎，是 `node_bindings` 的第三种 `kind`，
与 `tool`/`inference` 并列。它把"选工具"的开放度升级为"选节点+
决定连线"，且支持递归，但每一次生成的拓扑草稿都必须重新过
`Resolver` 编译校验——这条闸门是它与档位 3 的本质区别，不是程度
区别。

**数据结构**：

```python
class PlanningBinding(BaseModel):
    kind: Literal["planning"] = "planning"
    adapter: str                          # 如 "subgraph_planner"
    config: dict
    # allowed_node_classes: list[str]     # 可选用的节点类白名单
    # max_depth: int                      # 本节点允许递归派生的深度上限
    # max_nodes_per_plan: int             # 单次草稿最多节点数
    # max_total_delegations: int          # 挂在顶层 Plan 上的全局委托预算

class ExecutionContext(BaseModel):
    ...
    plan_depth: int = 0                   # 派生时 = 父 plan_depth + 1
    root_context_id: str | None = None    # 指向最顶层 Context，用于 O(1) 查询
                                            # 全局委托预算，与 parent_context_id
                                            # （指向直接父级）分开维护
```

`root_context_id` 与 4.4 节"树状追溯"那条 `WITH RECURSIVE agent_tree`
描述的是同一条血缘关系，不是两套并行的追溯机制——递归 CTE 从任意
`parent_context_id` 链出发即可算出完整子树，`root_context_id` 只是
把"从根出发数子孙数量"这个高频查询（每次 `PlanningNode` 执行都要
做一次预算校验）预先摊平成一列，换成 `WHERE root_context_id = :id`
的常数时间查询，避免每次校验都跑一遍递归——审计/树状展示等低频场景
仍然可以用递归 CTE 得到同样的答案，两者互为验证，不是互相替代。

**执行逻辑**：

```python
async def run(self, ctx: GraphRunContext[GraphState]) -> "SuspendNode | End":
    state = ctx.state
    binding = state.plan.node_bindings[self.node_name]

    draft = await inference.invoke(binding, state)   # 模型产出 DraftGraph JSON
    # 草稿本身不单独发事件——它只是编译前的中间结果，是否被接受由
    # 下面 PlanningCompiled/PlanningRejected 之一表达，草稿内容随该
    # 事件的 payload 一并带出，不重复广播一次未经校验的半成品。

    try:
        child_graph, child_plan = resolver.resolve_subgraph(
            draft.data,
            allowed_node_classes=binding.config["allowed_node_classes"],
            max_nodes=binding.config["max_nodes_per_plan"],
            ctx_hint={"tenant_id": state.tenant_id},
        )
    except ResolveError as e:
        await state.emit("PlanningRejected", {"node": self.node_name, "draft": draft.data, "reason": str(e)})
        return FallbackNode(reason=str(e))            # 编译失败是可预期分支，不是异常兜底

    if ctx.plan_depth + 1 > binding.config["max_depth"]:
        await state.emit("PlanningRejected", {"node": self.node_name, "draft": draft.data, "reason": "max_depth exceeded"})
        return FallbackNode(reason="max_depth exceeded")
    if await lifecycle.count_descendants(ctx.root_context_id) >= binding.config["max_total_delegations"]:
        await state.emit("PlanningRejected", {"node": self.node_name, "draft": draft.data, "reason": "delegation budget exceeded"})
        return FallbackNode(reason="delegation budget exceeded")

    await state.emit("PlanningCompiled", {"node": self.node_name, "graph_ref": child_plan.graph_ref})
    child_ctx = await application.submit(
        plan=child_plan,
        parent_context_id=ctx.context_id,
        plan_depth=ctx.plan_depth + 1,
        root_context_id=ctx.root_context_id or ctx.context_id,
    )
    return SuspendNode(contract=SuspendContract.default_for(
        SuspendType.CHILD_CONTEXT, target_id=child_ctx.context_id))
```

三点设计说明：`PlanningCompiled`/`PlanningRejected` 是仅有的两个新增
`RuntimeEventType` 枚举值（6.1 节"业务事件边界"原则下的显式例外，
理由见 6.1 节该原则的修订说明）——它们标记的是"一份新的、审计意义
上独立的子 `ExecutionPlan` 诞生了"和"一次委托请求在编译期被拒绝但
图仍继续往下走"，这两个信号在已有 12 个事件类型里都没有对应语义，
不能靠塞进某个已有事件的 payload 表达清楚；哪怕这次委托最终未发生
（走 `PlanningRejected`）也要留痕，不能被静默吞掉；
编译失败走 `FallbackNode` 而非异常抛出到 `Executor.run()` 的
`except`，与 `HitlPolicy.on_reject_exhausted` 是同一种"被拒绝是
设计内正常分支"的处理方式；深度和预算校验必须放在派生**之前**，
超预算这件事本身不应产生任何执行副作用。

**边界（与档位 3 对照）**：

|                            | PlanningNode                             | 档位 3（自主决策）       |
| -------------------------- | ---------------------------------------- | ------------------------ |
| 执行前是否过 Resolver      | 是，`resolve_subgraph()` 强制校验      | 否                       |
| 触达范围是否可枚举         | 是，白名单 ∪ 递归`max_depth` 层内并集 | 否，理论上无上界         |
| 校验失败的后果             | 走`FallbackNode`，不执行               | 不存在"校验失败"这个概念 |
| 写操作                     | 仍必须走白名单里声明过的`Tool` 节点    | 无此约束                 |
| 审计能否提前回答"会做什么" | 能（静态可达性分析）                     | 只能事后回放             |

审计问题从"这次执行会触达哪些系统"（读一份扁平清单）退化为"这次
执行最坏情况下可能触达哪些系统"（遍历所有可达 `PlanningNode` 的
白名单并集，展开到 `max_depth` 层），需要一个静态可达性分析工具
才能回答，不能假设审计人员自己心算得出——这个工具是落地前必须
交付的一部分，不是可选项。

**注意项**：`allowed_node_classes` 这份白名单开多宽，是业务决策不
是架构决策——白名单窄，这套机制退化成档位 2 的一个花哨包装；白名单
宽到接近整个 Class Registry，审计收敛的价值基本丧失，和档位 3 没有
实质区别。这条线必须由具体业务场景定，不能在架构层面预先给出统一
答案。这条"编译期声明能力范围、不做运行时动态协商发现"的路线，
与 ACP（Agent Communication Protocol）"能力在构建期离线声明，不做
运行时协商发现"（区别于 A2A 那种运行时动态 Agent Card 发现）是同一
个方向的独立选择——EAR 从审计闭集这个诉求出发天然更接近 ACP 的
立场，不是巧合。

### 4.2.2.2 AutonomousNode（档位 3）：明确放弃事前批准，换成事中拦截

+ 事后可回放

**方案定位**：与 `PlanningNode` 平级、不是它的变体，是 `node_bindings`
第四种 `kind`。它不调用 `resolver.resolve_subgraph()`，节点内部具体
执行什么完全不在 EAR 审计粒度内——这是"外挂/委托节点"模式的正式
落地。选择这种 `kind` 是业务方明确放弃"执行前批准"这条底线的信号，
必须在 `ExecutionPlan` 上可见、可审查。

**数据结构**：

```python
class AutonomousBinding(BaseModel):
    kind: Literal["autonomous"] = "autonomous"
    adapter: str
    config: dict
    # readable_scope: list[str]      # 只读数据范围声明，不是白名单节点类
    # forbid_write: bool = True      # 恒为 True，唯一不可配置为 False 的字段
    # budget: dict                   # {"max_tokens": ..., "timeout_s": ...}
```

**Permission Gateway：唯一拦截点，不感知 adapter 内部结构**：

```python
class PermissionGateway:
    """Kernel 向 AutonomousNode 类 adapter 暴露的唯一拦截点。
    adapter 内部推理循环长什么样，Kernel 不感知，只认这一个调用契约。"""
    async def check(self, ctx: GraphRunContext, tool_call_id: str,
                     tool_name: str, tool_args: dict,
                     policy: "PermissionPolicy") -> "PermissionDecision":
        ...
```

接入 `AutonomousNode` 的 adapter 必须满足一条契约义务：内部无论用
什么框架，工具调用发生前必须先 `await gateway.check(...)`，拿到
`DENY` 就不能真的执行，必须把结果喂回底层推理循环当作调用失败处理。
这条义务纳入 5.4 节 Adapter 治理，靠 CI 检查（例如要求 adapter 提供
mock 工具做集成测试，断言 `DENY` 场景下确实未发起真实调用），不靠
自觉遵守。

**状态表：独立于 `graph_state`，不改变 checkpoint 语义**：

```sql
CREATE TABLE tool_call_state (
    tool_call_id     UUID PRIMARY KEY,
    context_id       UUID NOT NULL REFERENCES execution_context(context_id),
    node_name        TEXT NOT NULL,
    tool_name        TEXT NOT NULL,
    tool_args        JSONB NOT NULL,
    is_write         BOOLEAN NOT NULL,   -- adapter 注册工具时机械打标，不信任模型自称
    state            TEXT NOT NULL,      -- pending/asking/allowed/denied/cancelled/finished
    decision         TEXT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    resolved_at      TIMESTAMPTZ
);
```

工具调用状态是高频、短生命周期的写入，混进 `graph_state`（随每次
`state.record()` 累积、随图执行终态归档）会让 checkpoint 语义复杂
化，因此单独开表，不复用 `graph_state`。

**挂起：复用 `SuspendContract`，新增枚举值，不发明第二套挂起机制**：

```python
class SuspendType(str, Enum):
    HITL = "hitl"
    CHILD_CONTEXT = "child_context_completion"
    SIGNAL = "signal"
    TOOL_CALL_PERMISSION = "tool_call_permission"   # 新增
```

`PermissionGateway.check()` 命中 `ask` 规则时，`tool_call_state.state`
置为 `asking`，`AutonomousNode.run()` 返回
`SuspendNode(contract=SuspendContract.default_for(SuspendType.TOOL_CALL_PERMISSION, target_id=tool_call_id))`——整个图执行按已有机制退出，`Executor.run()`
直接返回，走的是和 HITL 挂起、子任务挂起完全一样的处理路径，新增
三个触发类型 `tool_call_allow`/`tool_call_deny`/`tool_call_cancel`
（三态而非二态，`deny` 是用户主动拒绝，`cancel` 是超时或未获得明确
选择——两者语义不同、下游处理也不同，参考 MCP Elicitation
`accept`/`decline`/`cancel` 三态结果）：

```python
async def resolve_tool_call_permission(self, context_id: str, tool_call_id: str,
                                        decision: Literal["allow", "deny", "cancel"]) -> None:
    # 语义上是 hitl_approve/hitl_reject 的兄弟方法，唤醒目标是 tool_call_id
```

`FallbackNode` 收到 `deny` 与收到 `cancel` 应当走不同分支——`deny`
更适合直接终止这条路径或升级人工介入，`cancel`（对应超时未响应）
更适合重试或降级处理，具体路由是业务逻辑，Kernel 只负责把这三种
决策原样透传，不替业务做选择。

**批量确认：多个待决工具调用可以合并进一次挂起，不必逐次往返**：
如果 `AutonomousNode` 内部短时间连续触发多个命中 `ask` 规则的工具
调用，逐次单独挂起（每次单独一次 `SuspendNode`+一次 HITL 往返）在
体验和效率上都不理想。`PermissionGateway` 允许攒批：一个时间窗口内
命中 `ask` 规则的多个 `tool_call_id` 合并进同一次 `ExecutionPaused`，
`payload` 携带待确认列表，一次唤醒批量解决——这不改变挂起机制本身
（`SuspendContract` 不变，仍然是一次 `SuspendNode`），只是
`AutonomousNode.run()` 内部"要不要立即挂起还是先攒一批"的策略问题，
参考 MCP 2026-07-28 规范候选（SEP-2322）"一次往返可以打包一个
elicitation 请求和一个 sampling 请求"的设计动机——避免为每个请求
单独占用一次往返。

`AutonomousNode` 因此必须写成**可重入**：每次 `run()` 进入先查
`tool_call_state` 里有没有一条属于自己、状态是 `allowed`/`denied`/
`cancelled` 但未被消费的记录，有就把决策结果喂回内部推理循环、跳过
已问过的部分继续跑，而不是从头重新发起推理。这套"状态全部落库、
不依赖持有连接，任何进程实例都能接手恢复"的设计，与 MCP SEP-2322
"服务器不再打开回调通道等待客户端响应，状态全部随 payload 走，任意
无状态实例都能接手继续"是同一个方向——不是巧合，是同一类问题的
收敛解，这条设计不需要因为看到外部协议才成立，但可以作为佐证。

**事件：复用已有事件类型 + 更细的 payload，不新增枚举值**：单次工具
调用的"发起/完成"信号直接复用已有的 `ToolStarted`/`ToolCompleted`
（`SkillNode` 用它们表达节点级工具调用，这里用来表达节点内部更细
一层的工具调用，语义是同一类事情，只是粒度不同），payload 里加
`tool_call_id`/`is_write`/`decision` 字段区分。"命中 ask 规则、需要
人工确认"这个时刻**不单独发新事件**——它就是上面那次 `SuspendNode`
触发的 `Lifecycle.pause`，天然发出已有的 `ExecutionPaused`
（`payload.suspend_contract.type == "tool_call_permission"` 足以让
下游区分这是一次工具调用级挂起还是 HITL 挂起，不需要另开事件类型）。
"决策已解决、图恢复"这个时刻同样不单独发新事件——`resolve_tool_call_permission`
唤醒后重新进入 `Executor.run()`，`ExecutionStarted` 会像任何一次
断点恢复一样自然再发一次（4.2 节 `Executor.run()` 顶部无条件
`emit_bound(..., "ExecutionStarted", {})`，恢复与首次运行走的是同一
入口），下游靠"这是 `ExecutionPaused` 之后的下一次 `ExecutionStarted`"
即可推断出"恢复发生了"，不需要专门的 resume 信号。这一段与
`PlanningNode` 的 `PlanningCompiled`/`PlanningRejected` 不同——那两个
确实找不到已有事件能表达，属于 6.1 节例外；这里的四个候选新事件
全部能被已有事件+payload 覆盖，因此全部不新增。

**硬约束：`forbid_write` 恒为 `True` 如何机械落地**：`is_write`
标记不能来自模型自称，必须来自 adapter 在向内部推理框架注册工具时
就打好的静态元数据（与 5.3 节 `Tool` 隐含的读/写分类同一思路）。
`PermissionGateway.check()` 第一步就查 `is_write`，一旦为真，不查
`policy.rules`，直接 `DENY`——`PermissionPolicy` 配了 `allow` 也不能
覆盖这一条，这是唯一一处"配置不能覆盖代码里写死的判断"的地方。
任何要落地的写操作必须回到图里显式的 `Tool` 节点执行，`AutonomousNode`
的产出必须被当作外部输入对待（与用户输入同等信任级别），不能当作
内部节点的可信产出直接向下传递。

**注意项（现实限制，不是可以靠设计消除的问题）**：`AutonomousNode`
"可重入"这条要求能否真正做到，取决于其包装的底层推理框架是否支持
从某次工具调用中断点恢复。若底层框架不支持，挂起后再唤醒只能把
之前的推理过程当上下文重新喂给模型，不是真正从中断的执行帧继续——
这不是 Kernel 能单方面解决的问题，取决于 adapter 选型时有没有选一个
原生支持可恢复执行的底层框架（类似 AgentScope"事件流即可重放状态"
的能力）。这是接入这套机制前必须先确认的前提，设计本身无法保证。

**一条具体、不绑定单一框架的落地路径：把 adapter 实现成 A2A 协议
客户端**。`AutonomousNode` 委托的"EAR 不控制内部过程的外部 Agent"，
如果对方是通过 Agent2Agent（A2A）协议暴露的（框架无关、厂商无关的
开放标准，`context_id` 天然延续对话、Task 完整历史可查），可重入
能力就变成协议层的标准化保证，不再取决于赌某个具体框架有没有做好
断点恢复——`AutonomousNode` 的挂起/唤醒对应 A2A 的
`tasks/get`/`tasks/sendSubscribe` 轮询或流式查询，`tool_call_state`
持有的 `tool_call_id` 对应 A2A 的 `taskId`。这不是要求所有
`AutonomousNode` adapter 都必须走 A2A，只是在"选型时前提不确定"这
条限制之外，提供一个具体、当下就能验证可行性的选项。

### 4.2.2.3 两者的治理注意事项

`kind="autonomous"` 的绑定在 Registry（边界外的资产治理层）的资产
审批环节，应当走比 `kind="planning"` 更高的审批级别——否则这条旁路
车道会变成绕开"事前批准"这一设计初衷的合规后门。这条治理规则需要
业务侧配合，Runtime 只能保证"技术上区分得清楚是哪种 `kind`"，管不了
"谁有权批准使用它"。

两者是平行扩展，不是升级关系：不允许给 `PlanningNode` 加一个"设置
某参数就跳过 Resolver 校验"式的开关把它变成 `AutonomousNode`——那
会让同一节点类型在不同配置下审计语义不一致，审计人员无法只看
`kind` 字段判断这个节点遵不遵守事前批准。`AutonomousNode` 未来若要
把审计粒度做细（例如向 Permission Gateway 接入更完整的规则引擎），
这是它自己内部的增量演进，不影响 `PlanningNode`/`Tool`/`Inference`
三者已有的任何契约。

## 4.3 Lifecycle（暂停/恢复/取消/完成/失败/Checkpoint/事件发布）

```python
class Lifecycle:
    async def checkpoint(self, ctx: ExecutionContext) -> None:
        ctx.updated_at = now()
        await db.upsert("execution_context", ctx)

    async def pause(self, ctx: ExecutionContext, node_name: str,
                     contract: "SuspendContract") -> None:
        ctx.status, ctx.current_node, ctx.suspend_contract = "suspended", node_name, contract
        await self.checkpoint(ctx)
        await self.emit_bound(ctx, "ExecutionPaused", {"node": node_name, "type": contract.type})

    async def resume(self, context_id: str, resume_trigger: str,
                      payload: dict | None = None) -> ExecutionContext:
        """恢复挂起的执行。payload 会合并进 graph_state 的保留 key
        （'_resume_payload'），供恢复后紧接着运行的节点读取——
        HITL 场景是人工确认的结果，跨 Context 场景是子任务的终态。
        本方法只负责一致性校验与状态转换/重新派发，不做业务判断（比如
        子任务失败要不要重试）——那是恢复后节点自己的逻辑。"""
        async with db.transaction():
            # 悲观锁：防止事件驱动的快路径与 Cron 对账兜底路径同时
            # 对同一个 context 做 resume，两边都通过一致性校验后各自
            # 写一次 payload，后写的静默覆盖先写的。
            ctx = await db.fetch_one(
                "SELECT * FROM execution_context WHERE context_id = $1 FOR UPDATE", context_id)
            if ctx.status != "suspended":
                raise InvalidStatusError("当前执行实例未处于挂起状态，无法恢复")

            contract = SuspendContract.model_validate(ctx.suspend_contract)
            if resume_trigger not in contract.allowed_triggers:
                raise InvalidTriggerError(f"非法的恢复触发器: {resume_trigger}")
            if (contract.type == SuspendType.CHILD_CONTEXT
                    and str((payload or {}).get("child_context_id")) != contract.waiting_for_target_id):
                raise SecurityViolationError("恢复信号源与等待目标子任务不匹配")

            if payload:
                ctx.graph_state["_resume_payload"] = payload
            ctx.status, ctx.suspend_contract = "queued", None
            await self.checkpoint(ctx)
        # 入队放在事务提交之后：DB 状态与 arq 派发是两个独立系统的写入，
        # 不能指望它们在一次事务里原子生效。用 context_id 作为 arq
        # job_id 去重，同一个 context 不会被两个 Worker 同时接到任务，
        # 也防止运维手动重试与自动恢复撞车。如果进程恰好在这两步之间
        # 崩溃，会留下一个 status='queued' 但没有实际 arq job 的孤儿
        # context——4.4 节的对账 Cron 需要同时扫描这种情况，不只扫描
        # 卡在 'suspended' 的行。
        await arq_pool.enqueue_job("run_executor", context_id, _job_id=context_id)
        return ctx

    async def cancel(self, context_id: str, reason: str | None = None) -> ExecutionContext:
        """请求取消一次执行，语义是"尽快、协作式"，不是抢占式 kill。
        三种起始状态分别处理：
        - 已在终态（completed/failed/cancelled）：空操作，不是错误——
          对一个已经结束的执行发起取消是正常的时序竞态，不需要报错。
        - queued/suspended：没有活跃 Worker 正在读写这个 context，
          可以直接原地转终态。
        - running：活跃 Worker 正在持有 `ctx.graph_state` 并可能随时
          `checkpoint`，这里不能直接改 status（会和 Executor 自己的
          checkpoint 写竞态）。只落一个 `cancel_requested_at` 标记，
          由 Executor 在下一个节点边界（4.2 节）协作式发现并转终态——
          `ExecutionCancelled` 事件由那时的 `cancel_confirmed()` 发出，
          这里不提前 emit，避免前端在 status 还是 'running' 时就收到
          终态事件。
        """
        async with db.transaction():
            ctx = await db.fetch_one(
                "SELECT * FROM execution_context WHERE context_id = $1 FOR UPDATE", context_id)
            if ctx.status in ("completed", "failed", "cancelled"):
                return ctx
            if ctx.status in ("queued", "suspended"):
                ctx.status, ctx.completed_at = "cancelled", now()
                await self.checkpoint(ctx)
                await self.emit_bound(ctx, "ExecutionCancelled",
                                       {"reason": reason, "from_status": ctx.status})
            else:  # running
                ctx.cancel_requested_at = now()
                await self.checkpoint(ctx)
        return ctx

    async def cancel_confirmed(self, ctx: ExecutionContext, node_name: str | None) -> None:
        """Executor 在节点边界发现 cancel_requested_at 已设置后调用，
        这是取消请求真正生效、状态真正转为终态的唯一入口。"""
        ctx.status, ctx.completed_at = "cancelled", now()
        await self.checkpoint(ctx)
        await self.emit_bound(ctx, "ExecutionCancelled", {"node": node_name, "confirmed": True})

    async def is_cancel_requested(self, context_id: str) -> bool:
        row = await db.fetch_one(
            "SELECT cancel_requested_at FROM execution_context WHERE context_id = $1", context_id)
        return row.cancel_requested_at is not None

    async def complete(self, ctx: ExecutionContext) -> None:
        ctx.status, ctx.completed_at = "completed", now()
        await self.checkpoint(ctx)
        await self.emit_bound(ctx, "ExecutionCompleted", {})

    async def fail(self, ctx: ExecutionContext, error: Exception) -> None:
        ctx.status = "failed"
        await self.checkpoint(ctx)
        await self.emit_bound(ctx, "ExecutionFailed", {"error": str(error)})

    def bind(self, ctx: ExecutionContext):
        """返回一个绑定了当前 ExecutionContext 的 emit 闭包，
        供 GraphState 内部及各图节点直接调用，节点/Tool/Inference
        不需要持有 Lifecycle 实例。"""
        seq = itertools.count()
        async def _emit(event_type: RuntimeEventType, payload: dict, node_name: str | None = None):
            # event_type 类型收紧为 RuntimeEventType（见 6.1），
            # 节点/Tool/Inference 无法用任意字符串塞入业务事件语义。
            # Event Store 与 Event Broadcast 必须收到同一个 RuntimeEvent
            # 实例（含 seq），而不是分别拼装两份不同形状的数据——否则
            # SSE 走广播快路径收到的事件和走 Event Store 回放收到的事件
            # 没有共同的排序/去重依据，6.3 节"先回放再接续"的顺序保证
            # 就是一句空话。seq 在这里生成一次，两个下游各自只负责持久化
            # /转发，不再重新决定顺序。
            event = RuntimeEvent(
                event_id=str(uuid4()),
                context_id=ctx.context_id,
                seq=next(seq),
                event_type=event_type,
                node_name=node_name,
                payload=payload,
                created_at=now(),
            )
            await event_store.append(event)
            await event_broadcast.publish(ctx.context_id, event)
        return _emit
```

`resume()` 抛出的三个异常都是 `AppError` 的子类（沿用既有的
`AppError` 异常体系，不新开一套错误处理路径）：`InvalidStatusError`
（目标 context 当前不处于 `suspended`）、`InvalidTriggerError`
（`resume_trigger` 不在该次挂起的 `allowed_triggers` 白名单里）、
`SecurityViolationError`（`child_context_completion` 场景下，
payload 里的 `child_context_id` 与挂起时记录的 `waiting_for_target_id`
不一致）。三者都是"这次 resume 调用不适用于当前状态"的信号，调用方
（4.4 节的事件订阅者、对账 Cron）应当静默跳过而不是当成系统错误上报——
一个已经被别的路径唤醒过的 context 收到第二次唤醒尝试是正常情况，
不是故障。

`Lifecycle` 只管发布事件到 Event Store + 广播通道，不关心谁订阅、
怎么分发——这部分留给 Infrastructure（第 6 章）。节点/Tool/Inference
一律通过 `SuspendNode`/`Lifecycle` 触发状态转换，不直接执行 SQL——
状态转换只有一条路径（`Lifecycle` 的这几个方法），不会出现节点代码
里插一段裸 `UPDATE` 与 `checkpoint()` 各自写一次、两次写入互不知晓
对方存在的问题。

**Gate 分级**：不可逆/对外可见的动作才需要暂停等待人工确认，逻辑与
之前一致。

**`resume()` 里的悲观锁与 arq 的 `_job_id` 去重解决的是两个不同层面
的问题**：`_job_id` 去重防止同一个 context 被重复派发执行；`FOR UPDATE` 锁防止两个并发的 resume 调用（比如 4.4 节的事件驱动快路径
与 Cron 对账兜底同时触发）都通过一致性校验、都往 `graph_state` 写
一份 payload，造成后写覆盖先写的静默数据丢失——这个锁保护的是校验
与写入之间的临界区，不是执行本身，两者不冗余，都需要保留。

**单 Worker 独占所有权，不需要状态合并**：一个 `context_id` 从
`queued` 到终态，任意时刻只被唯一一个 Worker 进程持有——`arq` 入队
时 `_job_id=context_id` 保证了这一点，同一 context 不会被两个 Worker
同时接到任务。`GraphState` 持有 `ctx.graph_state` 的物理引用（4.2 节）
在这个前提下是安全的：一份数据、一个进程、一个所有者，不存在需要
跨进程合并两份并发修改的场景。真正需要并行执行的场景不靠共享同一份
`ExecutionContext` 解决，而是派生独立的子 `ExecutionContext`，见
下方 4.4 节。

## 4.4 跨 Context 编排：父子关系与挂起恢复

需要真正跨进程并行、或者父任务不适合阻塞等待子任务结果时（2.1 节
"Sub-agent 不适合阻塞等待"/"Swarm"两种情形），不共享 `ExecutionContext`，
而是让父节点派生一个独立的子 `ExecutionContext`，两者各自拥有完整
的 `Lifecycle`/HITL/Checkpoint 能力，用 `parent_context_id` 建立审计
血缘。这不是新增 Runtime 概念——`ExecutionContext` 本身就能表达父子
关系，`Sub-agent`（需要返回结果）与 `Swarm`（移交、不返回）也是 2.1
节已有的业务概念，这里只是给它们补上"跨进程该怎么落地"的具体机制。

**父节点挂起（Sub-agent，需要等待结果）**：

```python
async def run(self, ctx: GraphRunContext[GraphState]) -> "SuspendNode":
    invocation = SubAgentInvocation(                 # 见 4.5.2，显式切片，
        task_instruction="核实资产 xxx 的处置合规性",   # 不整份透传 graph_state
        context_slice={"asset_id": ctx.state.get("IntakeNode")["asset_id"]},
    )
    child_ctx = await application.submit(
        business_config={"skill": "sub_analysis"},
        initial_state=invocation.context_slice,       # Resolver 会校验这份
            # slice 是否覆盖 sub_analysis 这个 Graph 声明的
            # required_context_keys，见 4.5.2
        mode="async",
        tenant_id=ctx.state.tenant_id,
        parent_context_id=ctx.state.context_id,  # 创建时直接带上血缘，不做二次 UPDATE；
            # tenant_id 会被 Application.submit() 强制用父 Context 的
            # 值覆盖（4.1 节），这里传的值仅为可读性，不是实际生效值
    )
    ctx.state.record(self.node_name, {"waiting_for_child": str(child_ctx.context_id)})
    contract = SuspendContract.default_for(SuspendType.CHILD_CONTEXT, target_id=str(child_ctx.context_id))
    return SuspendNode(node_name=self.node_name, contract=contract)
```

**父节点被唤醒后，读取子任务结果（见 4.5.3 `SubAgentResult`）**：

```python
async def run(self, ctx: GraphRunContext[GraphState]) -> "SkillNode | End":
    child_context_id = ctx.state.get(self.node_name)["waiting_for_child"]
    child_result: SubAgentResult = await lifecycle.load_result(child_context_id)  # 新增
    if child_result.status == "failed":
        return FallbackNode(reason=child_result.error)
    ctx.state.record(self.node_name, child_result.data)
    if child_result.has_autonomous_output:            # 污点标记逐层传播，见 4.5.3
        ctx.state.mark_untrusted(self.node_name)       # 后续节点读到这份数据时
            # 需要按外部输入对待，不能假设它和普通节点产出同等可信
    ...
```

**父节点不等待（Swarm，移交、不返回）**：同样调用 `application.submit( parent_context_id=..., initial_state=...)` 派生子任务，父节点直接
`return next_node(...)` 或 `return End()`，不经过 `SuspendNode`——血缘
关系仍然通过 `parent_context_id` 记录，只是父任务不阻塞在这个节点上
等待子任务结果，也因此永远不会调用 `load_result()`——Swarm 模式下
子任务的输出污点标记无法传回父任务，这是"移交、不返回"这个业务语义
本身决定的，不是遗漏。

**唤醒闭环（Business Application 层，不属于 Runtime 内核）**：

```python
async def on_child_task_finished(event: RuntimeEvent):
    """快路径：订阅 ExecutionCompleted/ExecutionFailed 事件，尽快唤醒
    等待中的父任务。这是延迟优化，不是正确性保证——见下方兜底路径。
    一致性校验（触发源是否匹配、trigger 是否被允许）统一收在
    Lifecycle.resume() 里做，这里不重复判断，只负责"发现该唤醒谁"。"""
    row = await db.query(
        "SELECT parent_context_id FROM execution_context WHERE context_id = $1",
        event.context_id)
    if not row or not row.parent_context_id:
        return
    try:
        await lifecycle.resume(
            row.parent_context_id,
            resume_trigger="child_context_completion",
            payload={"child_context_id": str(event.context_id), "child_status": event.event_type},
        )
    except (InvalidStatusError, InvalidTriggerError, SecurityViolationError):
        # 父任务当前不在等这个子任务（可能已经被别的路径唤醒过，
        # 或者压根不是在等它），静默跳过，不是这里的错误。
        pass
```

```python
# 兜底路径：复用 2.2 节已有的 Cron 机制做周期性对账，
# 不依赖事件广播是否真的送达——Event Broadcast 是"尽力而为"的
# 通道（6.1 节对 Notify 的描述同样适用于这里），HITL 的恢复由人工
# 主动调用 REST 接口触发、不依赖事件投递，但 child_context_completion
# 场景没有人在等着点按钮，一旦事件丢失，父任务会无限期卡在
# suspended 状态且没有任何告警——所以这里不能只有快路径。
async def reconcile_suspended_parents(ctx):
    stuck = await db.query("""
        SELECT context_id, suspend_contract->>'waiting_for_target_id' AS child_id
        FROM execution_context
        WHERE status = 'suspended'
          AND suspend_contract->>'type' = 'child_context_completion'
    """)
    for row in stuck:
        child = await db.get("execution_context", row.child_id)
        if child.status in ("completed", "failed"):
            try:
                await lifecycle.resume(row.context_id, resume_trigger="child_context_completion",
                    payload={"child_context_id": str(child.context_id), "child_status": child.status})
            except (InvalidStatusError, InvalidTriggerError, SecurityViolationError):
                pass

    # 同时扫描"孤儿 queued"：resume() 提交事务后、enqueue_job 之前
    # 进程崩溃，会留下 status='queued' 但没有真正 arq job 在跑的行。
    orphaned = await db.query("""
        SELECT context_id FROM execution_context
        WHERE status = 'queued' AND updated_at < now() - interval '5 minutes'
    """)
    for row in orphaned:
        await arq_pool.enqueue_job("run_executor", row.context_id, _job_id=row.context_id)

class WorkerSettings:
    cron_jobs = [cron(detect_and_dispatch, hour={0, 6, 12, 18}),
                 cron(reconcile_suspended_parents, minute=set(range(0, 60, 5)))]
```

`reject_count` 与 `max_reject_count`（`HitlPolicy`）只在
`suspend_contract.type='hitl'` 时有意义，`child_context_completion`
的恢复路径不读写这两个字段——恢复后紧接着运行的节点从
`graph_state['_resume_payload']` 里读 `child_status`，自己决定走成功
分支还是失败分支，Runtime 不替业务判断"子任务失败算不算允许重试的
一次拒绝"。

**树状追溯（Audit 消费者，Phase 7 落地）**：`parent_context_id` 一旦
存在，Multi-Agent 协同树的全貌就是一条递归 CTE，不需要额外的图存储：

```sql
WITH RECURSIVE agent_tree AS (
    SELECT context_id, parent_context_id, tenant_id, status, 1 AS depth
    FROM execution_context
    WHERE context_id = :root_context_id

    UNION ALL

    SELECT child.context_id, child.parent_context_id, child.tenant_id, child.status, tree.depth + 1
    FROM execution_context child
    INNER JOIN agent_tree tree ON child.parent_context_id = tree.context_id
    WHERE tree.depth < 50  -- 防止 GoalLoop 类节点异常递归派生子任务时查询失控，
                           -- 与 9 章"循环资源上限"是同一层防护思路
)
SELECT * FROM agent_tree ORDER BY depth ASC;
```

---

## 4.5 跨提交上下文与 Agent 间数据契约

4.4 节解决的是"同一次业务流程内，父子 `ExecutionContext` 如何协同"；
本节解决两个更外围的问题：同一用户/同一会话跨越多次独立提交时记忆
如何延续，以及父子 `ExecutionContext` 之间传递的数据该长什么形状、
校验到什么程度。三个子节都不新增 Runtime 核心概念，都是在
`Application.submit()`/`ExecutionPlan`/`Graph` 已有的扩展点上补数据
契约。

### 4.5.1 跨提交会话记忆：策略可插拔，Kernel 不理解记忆内容

**新增两个跨越多次提交的持久对象，位于 `ExecutionContext` 之上**：

```python
class ConversationSession(BaseModel):
    session_id: str
    tenant_id: str
    created_at: datetime
    last_context_id: str | None = None

class ExecutionContext(BaseModel):
    ...
    session_id: str | None = None      # 属于哪个会话，独立任务为 NULL
```

```sql
CREATE TABLE conversation_session (
    session_id       UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now(),
    last_context_id  UUID REFERENCES execution_context(context_id)
);

CREATE TABLE conversation_memory (
    session_id        UUID PRIMARY KEY REFERENCES conversation_session(session_id),
    turn_seq          INT NOT NULL DEFAULT 0,   -- 单调递增，见下方并发处理
    summary           TEXT NOT NULL DEFAULT '',
    recent_turns      JSONB NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'ok',  -- ok/stale，见下方失败语义
    last_compaction_error TEXT,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- execution_context.session_id 列已并入 4.1 节主表定义，
-- 迁移脚本需保证 conversation_session 先于 execution_context 建表
-- （或 execution_context 的外键约束延后添加）
```

**策略可插拔：压缩方式是一个普通 adapter，不是 Kernel 写死的算法**——
不同场景需要不同的记忆策略（整体摘要、滑动窗口、关键事实抽取、向量
检索召回都是合理选择，且可能随场景变化），这条能力直接复用已有的
`InferenceBinding`/adapter 机制，不新增抽象层：

```python
class MemoryPolicy(BaseModel):
    binding: InferenceBinding    # adapter 字段决定具体压缩策略，
                                  # 例如 "summary_compactor" /
                                  # "sliding_window" / "key_fact_extractor"
    max_recent_turns: int = 5

class ExecutionPlan(BaseModel):
    ...
    memory_policy: MemoryPolicy | None = None   # 编译期由 Resolver
        # 根据 session/tenant 配置决定用哪个 adapter，Runtime 运行期
        # 不做"该用哪种压缩策略"的判断，与 3.2 节"触达哪些外部系统
        # 不需要运行时重新评估"是同一条原则的延伸
```

**触发方式**：`Lifecycle.complete()` 到达终态时，若 `ctx.plan.memory_policy`
非空且 `ctx.session_id` 非空，直接调用 `inference.invoke(memory_policy.binding, ...)` 压缩本次 `graph_state`、更新 `conversation_memory`——这次调用
发生在图执行**已经结束之后**，不经过 `SkillNode`/图节点，因为它不是
图逻辑的一部分，是 Lifecycle 收尾职责的延伸（与 `complete`/`fail`/
`cancel` 是同一类扩展点）。下一次同一 `session_id` 提交时，
`Application.submit()` 读取 `conversation_memory.summary`/`recent_turns`，
写入新 `ExecutionContext` 的初始 `graph_state['_memory']`——复用已有
的黑板读写机制，图内任何节点通过 `ctx.state.get('_memory')` 读取，
不需要新的读取路径。**Kernel 只负责"提交时塞入、完成时触发压缩"，
不理解压缩出来的内容是什么**，压缩逻辑本身是否合理是业务/adapter
的职责，不是架构问题。

**并发写入：乐观校验，不做会话级串行锁**：同一 `session_id` 下允许
多个 `ExecutionContext` 并发执行（不因为要保护记忆一致性就牺牲吞吐），
`turn_seq` 在 `Application.submit()` 时从 `conversation_session` 当前
值递增分配。压缩 Hook 写入 `conversation_memory` 时做条件更新：

```sql
UPDATE conversation_memory
SET summary = :new_summary, recent_turns = :new_recent_turns,
    turn_seq = :new_turn_seq, status = 'ok', updated_at = now()
WHERE session_id = :session_id AND turn_seq < :new_turn_seq;
```

`turn_seq < :new_turn_seq` 这个条件让更晚提交的一轮永远不会被更早
提交但更晚完成的一轮覆盖——命中 0 行不算错误，是"这一轮的记忆已经
被更新的一轮覆盖，正常丢弃"，与 4.4 节 `resume()` 的三个异常"静默
跳过不算故障"是同一处理哲学，不重新发明一套。

**压缩失败的语义：不影响任务本身终态**：`inference.invoke()` 这次
调用可能超时或报错，失败时只把 `conversation_memory.status` 置为
`stale`、`last_compaction_error` 记下原因，不回滚已经完成的
`ExecutionContext`——任务本身是否成功，与这一轮内容是否被正确记住，
是两条独立的状态，不能因为记忆失败让一个本来成功的任务被标记为
`failed`。`status='stale'` 的会话下次提交时可以选择用 adapter 重试
压缩，或者直接读取仍然是上一次成功压缩的 `summary`（内容旧一轮，
但不是错误状态）。

**已知限制，必须让业务方知情**：记忆内容是模型压缩的产物，天然有损。
如果压缩过程丢了一个关键细节，下一轮对话就"忘了"这件事，且这个
遗忘过程本身不会报错、不会被现有审计机制捕捉——这不是可以靠机械
设计消除的问题，只能靠选择合适的压缩策略（`MemoryPolicy.binding`）
和必要时保留更长的 `recent_turns` 窗口缓解，不能承诺"记忆一定准确"。

### 4.5.2 Sub-agent 输入契约：`context_slice` 机械校验，不是约定

```python
class SubAgentInvocation(BaseModel):
    task_instruction: str       # 交给子任务的具体目标/指令
    context_slice: dict         # 从父 graph_state 里显式挑出的字段
```

"显式挑选、不整份透传"这条约束不能只停留在命名和注释层面——和
6.1 节"约定必须变成类型系统/编译期强制"的一贯标准一致，`Graph`
需要声明自己作为子任务被调用时的输入接口：

```python
class Graph(BaseModel):
    ...
    required_context_keys: list[str] = []   # 必须由调用方提供，缺失则拒绝
    optional_context_keys: list[str] = []   # 可选提供
```

`Resolver.resolve()` 在编译一次子任务提交（`initial_state` 非空）时，
校验 `initial_state` 的 key 集合：必须覆盖 `required_context_keys`
的全部，且不能出现既不在 `required_context_keys` 也不在
`optional_context_keys` 里的多余字段——多余字段直接编译失败，不是
警告后放行。这让子任务的输入接口变成一份可以脱离调用方单独审查的
声明，和 3.1 节"拓扑分叉编译期拒绝"是同一种"约束从注释变成拦截"
的处理方式。

这层校验会让子任务的接口变化产生编译期传导——调用它的所有父任务
若未同步更新 `context_slice`，会在下次编译时失败。这是刻意的：子
任务一旦被多处复用，就应当被当作一个需要维护稳定接口的对象，接口
变化理应传导给调用方，而不是在运行时才发现字段对不上。如果某个子
任务被设计成只服务于一个紧耦合的父任务、从不指望被复用，这层校验
的收益确实有限——这种情况下 `required_context_keys`/`optional_context_keys`
可以按需只声明成一个宽松的通配（比如显式允许任意字段），但默认行为
是严格校验，放宽是需要显式声明的例外，不是默认状态。

### 4.5.3 Sub-agent 输出契约：`output_keys` 与三处失败语义

```python
class Graph(BaseModel):
    ...
    output_keys: list[str] = []   # 完成后 graph_state 里哪些顶层 key
                                    # 算对外可见的结果，其余视为子任务
                                    # 内部工作痕迹，不暴露给父任务

class SubAgentResult(BaseModel):
    status: Literal["completed", "failed"]
    data: dict = {}               # 仅 status="completed" 时有内容
    error: str | None = None      # 仅 status="failed" 时有内容，
                                    # 不与 data 混用
    has_autonomous_output: bool = False   # 见下方信任传递
```

`PlanningNode`（4.2.2 节）动态生成的子图没有预先声明的 `Graph`，
`output_keys` 因此需要作为草稿的一部分由模型显式给出，`resolve_subgraph()`
在编译草稿时一并校验——这是对 4.2.2 节 `PlanningBinding` 草稿 schema
的补充：草稿必须包含 `draft_nodes`/`draft_edges`/`output_keys` 三项，
缺少 `output_keys` 视为草稿不完整，走 `PlanningRejected` 分支，不会
静默生成一个没有任何输出的子任务。

**三处失败语义，逐一定死**：

1. **压缩 Hook 失败** → 见 4.5.1，不影响任务本身终态，记在
   `conversation_memory.status`，与 `SubAgentResult` 无关。
2. **`output_keys` 声明的 key 因条件分支未被写入** → 该字段在
   `SubAgentResult.data` 里省略，**不**让 `Lifecycle.complete()`
   因此失败——条件分支没走到是正常的图执行结果，不应被追认为任务
   失败。父节点读取 `child_result.data` 时必须显式处理"声明过的
   字段可能不存在"，这是父节点的契约义务，`Lifecycle` 不代为兜底。
3. **子任务本身失败（`status="failed"`）** → `data` 恒为空字典，
   错误信息只出现在 `error` 字段，父节点只应在 `status="completed"`
   时读取 `data`，不能假设失败态下 `data` 携带部分结果。

**跨 Context 的信任传递**：`ExecutionContext` 新增一列
`has_autonomous_output BOOLEAN NOT NULL DEFAULT false`，`AutonomousNode`
（4.2.2 节）每次真正执行时将所在 `ExecutionContext` 的这一列置真。
`Lifecycle.complete()` 计算 `result` 切片时，把这一列的值一并折入
`SubAgentResult.has_autonomous_output`——如果这次子任务自己又派生了
更深一层子任务（递归的 Sub-agent），子任务在读取**它自己**的子结果
时，同样要把对方的 `has_autonomous_output` 与自己的 OR 到一起再往
上传播，保证这个标记逐层传递、不会在任何一层中间丢失。父节点拿到
`has_autonomous_output=true` 的结果后，后续处理必须按 4.2.2 节的
既有约束对待这份数据——当作外部输入，不能假设它和普通节点产出
同等可信，任何要落地的写操作必须回到父任务自己图里显式的 `Tool`
节点执行。没有这个标记，"写操作必须回图"这条约束在数据穿过一层
`ExecutionContext` 边界之后就会名存实亡。

---

# 5. Inference 与 Tool

## 5.1 统一返回信封：ExtensionResult 与 ExecutionArtifact

```python
class ExecutionArtifact(BaseModel):
    """结构化展示物，只用于前端渲染，不参与图的推进逻辑。
    type 是开放字符串而非封闭枚举——见下方"类型治理"说明。"""
    id: str
    type: str
    title: str
    payload: dict
```

| Artifact 类型 | 典型生产者                 | 前端展示                                                                               |
| ------------- | -------------------------- | -------------------------------------------------------------------------------------- |
| `retrieval` | RAG Tool（vector adapter） | 知识来源面板（文档、页码、Chunk、Score）                                               |
| `citation`  | Inference（LLM）           | 回答中的引用标记（①②③），引用 ID 关联到同一次执行里更早产生的`retrieval` artifact |
| `table`     | SQL Tool                   | 数据表格                                                                               |
| `chart`     | 分析类 Tool                | 图表                                                                                   |
| `graph`     | GraphDB Tool               | 知识图谱                                                                               |
| `image`     | OCR/Image Tool             | 图片预览                                                                               |
| `document`  | 文件生成 Tool              | PDF/Word 下载                                                                          |

**`citation` 类型 artifact 的 `payload` 字段命名，直接采用 ACP
（Agent Communication Protocol）已有约定，不重新发明**：ACP 用
`CitationMetadata`（来源追溯）和 `TrajectoryMetadata`（多步推理/
工具调用轨迹追踪）这两类专门字段标准化同一场景，`citation` 类型的
`payload` 建议对齐这两个命名（例如 `source_ref`/`source_span` 对应
`CitationMetadata` 的思路），如果未来某个 Tool/Inference 需要暴露
多步推理轨迹本身（而不只是引用来源），新增一个 `trajectory` 类型时
同样沿用这套命名，不需要另起一套。

**类型治理**：`type` 从 `Literal[...]` 收紧为封闭枚举会导致每新增一
种展示类型（例如资产盘活场景后续可能需要的 `map` GIS 可视化）都要
改动 Kernel 的核心 Pydantic 模型，这与"Runtime 不关心业务"存在摩擦。
因此 `type` 定为开放字符串，上表 7 种是已知类型的初始清单。命名冲突
（不同团队各自定义 `gis-map`/`map` 表达同一件事）不需要靠运行时注册
发现服务解决——维护一个共享常量模块（如 `artifact_types.py`），前端
TS 类型生成与后端 Tool/Inference 实现方都从这一份定义 import，新增
类型是对这一个文件的一次 PR；CI 扫描代码里传给 `type=` 的字符串
字面量，不在常量模块登记的直接拦截合并。这比运行时注册中心便宜，
也不需要触碰 `ExecutionArtifact` 的模型定义本身，类型治理始终留在
Runtime 边界之外。与命名治理独立的另一层兜底：前端对未知 `type`
一律走通用 JSON 展示或"暂不支持预览"的兜底面板，不允许空白——新
类型上线但前端渲染器还没跟上时，命名治理再严格也挡不住这一刻，
必须由前端自己的兜底逻辑兜住。

RAG 场景的引用不是 UI 拼出来的，也不是 Runtime 拼出来的：`Tool (vector)` 检索时把命中的知识片段包装成 `retrieval` artifact 写入
`GraphState`；随后 `Inference` 组装 Prompt 时把这些片段連同其
`artifact.id` 一起交给模型，模型输出结构化答案时按 schema 要求
标注引用了哪些 `artifact.id`，`Inference` 据此生成 `citation`
artifact——引用关系全程是数据，不是字符串拼接。

## 5.2 Inference（智能推理，含流式输出）

**边界声明：`pydantic_graph` 是步骤编排层，不是推理引擎；`Inference`
只能思考，不能动手。** 具体约束是：`Inference` 实现内部构造的
PydanticAI `Agent` 禁止注册任何 `@agent.tool`/`@agent.tool_plain`，
`agent.run_stream()`/`agent.run()` 在一次 `invoke()` 调用内只能产出
一次推理结果，不允许它自己发起多轮工具调用循环。理由是审计边界：
`RuntimeEvent` 体系（第 6 章）的 `ToolStarted`/`ToolCompleted`/Audit
覆盖全部建立在"外部访问必须经过图节点的 `Tool` binding"这个前提上；
如果 PydanticAI 的 Agent 在 `run_stream()` 内部自己决定调用工具，
这些调用完全绕开 `state.emit`，不会落 Event Store，也不可能被
`SuspendNode` 拦截做 HITL 确认——那就是一个审计留痕和 HITL 网都覆盖
不到的黑洞，与第 9 章"AI 交互全量留痕"的承诺直接冲突。

真正需要"模型自主决定下一步做什么、要不要调用工具"这类 ReAct 式
行为时，不建模成 `Inference` 内部循环，而是建模成**图层面的决策边**：
一个 `Inference` 节点产出"下一步该调用哪个 Tool/是否已经可以结束"
的结构化判断（`ExtensionResult.data` 里的一个字段，例如
`{"next_action": "call_risk_api", "done": false}`），由图的条件分支
读取这个字段路由到具体的 `Tool` 节点，`Tool` 执行完之后再回边接回
同一个 `Inference` 节点——这正是 4.2 节已经存在的 GoalLoop 回边模式，
不需要新增机制。多轮"思考-行动"在事件粒度上因此和其它图形状完全
一致，每一次工具调用都是一次独立的、被 Runtime 感知的图节点执行,
而不是消失在某一次 `invoke()` 调用内部的黑盒。

```python
class Inference(Protocol):
    async def invoke(self, binding: InferenceBinding, state: "GraphState") -> ExtensionResult: ...
```

覆盖 LLM、Rules、Planner、Evaluator。流式输出复用 PydanticAI 原生的
`run_stream()`，不引入额外的流式框架：

```python
class StreamingLLMInference:
    async def invoke(self, binding: InferenceBinding, state: GraphState) -> ExtensionResult:
        agent = build_pydantic_agent(binding)  # 构造时不注册任何 tool，
                                                # 见上方边界声明
        async with agent.run_stream(deps=build_deps(state)) as stream:
            async for delta in stream.stream_text(delta=True):
                await state.emit("InferenceDelta", {"node": state.current_node, "delta": delta})
            output = await stream.get_output()
        artifacts = extract_citations(output, state.artifacts_so_far)
        return ExtensionResult(data=output.model_dump(), artifacts=artifacts)
```

**这条约束需要机械拦截，不能只靠约定**：CI 流水线对每个 `Inference`
实现做一次构造期检查（构造出 `agent` 后立即断言
`len(agent._function_tools) == 0`，PydanticAI 内部持有已注册工具的
属性名以其版本为准），检测到任何 `Inference` 实现给自己的 Agent
注册了 tool，直接拦截合并——与 3.2 节"非破坏性变更不能只靠自觉,
必须靠 CI Schema diff 机械拦截"是同一个执行力度，不允许这条边界
退化成一条文档里的君子协定。

`InferenceDelta` 只推给 SSE 用于打字机效果展示，不写入
`GraphState`、不进 Event Store 的回放关键路径判断（回放时可以选择
只重放到最后一次完整输出，不必逐字重放）。

## 5.3 Tool（外部能力访问）

```python
class Tool(Protocol):
    async def call(self, binding: ToolBinding, state: "GraphState") -> ExtensionResult: ...
```

| adapter          | 场景                                                                                                                                                                                                                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `postgres`     | 内部数据库直连、业务台账读写                                                                                                                                                                                                                                                          |
| `redis`        | 对话缓存、短期状态                                                                                                                                                                                                                                                                    |
| `vector`       | pgvector 向量检索，产出`retrieval` artifact                                                                                                                                                                                                                                         |
| `graph`        | 知识图谱检索，产出`graph` artifact                                                                                                                                                                                                                                                  |
| `http`         | 内部微服务调用、审批接口                                                                                                                                                                                                                                                              |
| `sdk`          | 对象存储等有官方 SDK 的服务                                                                                                                                                                                                                                                           |
| `mcp`          | ERP/GIS/CAD 等外部/异构系统                                                                                                                                                                                                                                                           |
| `filesystem`   | 文件读写，产出`document` artifact                                                                                                                                                                                                                                                   |
| `sandbox_exec` | 代码/命令执行（按需引入）。`provider`（local/docker/kubernetes/e2b/daytona 等）是 `config` 里的一个字段，在编译期由 Resolver 决定，Runtime 运行期不做"在哪执行"的判断；沙箱会话的建立、复用、清理封装在这一个 adapter 内部（模式与 `mcp` 的 Session Pool 一致），不对图节点暴露 |

全链路追踪（trace_id 内联进 SQL 注释）与失败模式设计（超时/重试/
降级）不变，仍是 `postgres` adapter 的强制默认行为。

**ToolBinding 保持编译期固化，不向 Runtime 开放动态解析**：`node_bindings`
在 Resolver 生成 `ExecutionPlan` 时已完全确定并作为白名单固化（见第
9 章），这是刻意的取舍——Plan 本身就是一份完整的静态审计凭证，审查
一次执行会触达哪些外部系统不需要在运行时重新评估策略。多租户/环境
差异不需要打破这条原则：`Resolver.resolve()` 调用时 `ctx_hint` 已带
有 `tenant_id`，租户级差异应在编译期由 Resolver 查询租户配置后直接
烘焙进具体的 `ToolBinding`，而不是让 Runtime 在执行时判断"这个租户
该用哪个实现"。

真正只能在执行时才能确定的场景（例如 LLM Provider 之间的实时负载均
衡、故障转移到备用供应商），不应建模成"Runtime 决定调用哪个 Tool"，
而应下沉到**adapter 内部自建路由**：例如声明一个 `adapter: "llm_pool"`，
`config` 中携带候选模型列表与权重/降级顺序，`Tool`/`Inference` 的具
体实现负责这一层内部选择。从 `ExecutionPlan` 与审计日志的视角看，
绑定身份自始至终只有一个（`llm_pool`），编译期固化的不变式没有被
打破，只是该 adapter 内部对"具体调用哪个底层模型"这件事拥有执行期
自由度——这与 `Inference`/`Tool` 已经采用的 Protocol 多实现模式是同
一个思路，只是把"多实现"的粒度从"选哪个 adapter"下沉到"某个 adapter
内部选哪个底层实现"。这一模式应作为后续遇到"需要运行时动态选型"
诉求时的标准应对方案，避免演变成给 `ToolBinding` 加运行时 override
开关这类会侵蚀静态执行保证的设计。

---

## 5.4 Adapter 治理

5.3 节的 adapter 清单只回答了"选哪个""内部怎么路由"，没有回答
"adapter 本身作为一份代码资产，谁能新增、谁能改、改坏了怎么发现"——
这条治理链路目前是空白，而 3.2 节已经为**节点类**建好了一套完整机制
（Class Registry + CI 对输入/输出 Model 做 JSON Schema diff + 破坏性
变更强制开新版本类名），adapter 面临的是同一个问题，理应直接复用同
一套机制，而不是另起一套：

**新增 adapter 走代码评审 + 落表**：任何新 adapter 实现必须同时更新
5.3 节的清单表（这张表本身就是 adapter 的权威清单，不是文档摆设），
清单变化作为 PR 的一部分接受代码评审——这与"节点类必须注册进 Class
Registry 才能被 `graph_ref` 引用"是同一个约束粒度，adapter 不应该有
一条绕开评审、直接在某个分支里私自 `register_adapter("my_adapter", ...)`
就能被 `ToolBinding.adapter` 引用的路径。

**Adapter 的 `config` schema 变更遵循与节点类相同的破坏性变更规则**：
已经固化进历史 `ExecutionPlan` 的 `ToolBinding.config` 在断点恢复、
审计回查时必须仍然是原来的语义——如果某个 adapter 修改了 `config`
里某个字段的含义（不是新增字段，而是改变现有字段的解释方式），这
是破坏性变更，必须注册为新的 adapter 名（例如 `mcp` → `mcp_v2`），
旧的 `ToolBinding` 继续绑定旧 adapter 实现，不允许原地修改导致历史
`ExecutionPlan` 的回放结果发生漂移。非破坏性的内部改动（修 Bug、
换底层 SDK 版本、调优超时参数）可以直接在原 adapter 内联上线，判断
标准与 3.2 节完全一致。

**凭证不进 `config` 明文，不进事件/审计流**：涉及外部凭证（数据库
密码、API Key、第三方服务 Token）的 adapter，`config` 字段只允许存
凭证的**引用**（密钥管理服务的 key 名），凭证本体由 adapter 实现在
运行时向密钥管理服务换取，不落进 `ExecutionPlan`——`ExecutionPlan`
作为第 9 章"完整静态审计凭证"要被审计角色查阅，明文凭证混进去是
直接的合规风险，这一点在 SOE 语境下尤其不能含糊。6.1 节 `ToolStarted`
事件的 payload 目前只携带 `adapter` 名称、不携带 `config`，这条约束
需要一直保持——任何后续想在事件里加更多 adapter 调用细节（例如把
`config` 整体带出去方便前端展示），都必须先过一遍"这里面有没有凭证"
的检查，而不是顺手加个字段。这条约束和 MCP Elicitation 规范"表单
模式禁止用于密码/API Key 等敏感凭证、必须走 URL 模式"是同一条底线，
外部协议独立得出了同样的结论，不是 EAR 一家的选择。

**adapter 清单是被治理的资产，不是任由业务侧扩张的开放列表**：谁有
权提交新 adapter、adapter 的安全评审由谁负责（尤其是 `sandbox_exec`
这类直接执行代码/命令的高风险 adapter），是组织流程问题，不是本文档
该回答的架构问题——但架构层面必须保证"有且只有一条注册路径、这条
路径必过 CI 检查"，具体审批人是谁留给团队自己定。

**`kind="autonomous"` 的 adapter 必须实现 `PermissionGateway.check()`
契约，且不能靠自觉**：4.2.2 节要求这类 adapter 在内部每次真正发起
工具调用前，必须先 `await gateway.check(...)`，拿到 `DENY` 就不能
真的执行。这条契约和"新增 adapter 必须同时更新 5.3 节清单表"是同一
类问题——新增/修改一个 `kind="autonomous"` adapter 的 PR，CI 必须
跑一次专门的集成测试：提供一个 mock 工具，配置 `PermissionPolicy`
对它返回 `DENY`，断言 adapter 内部确实没有真正发起这次调用（例如
mock 工具记录了调用次数，断言为 0）——通不过这个测试的 adapter 不
允许合并。这类 adapter 的安全评审级别应高于普通 `Tool`/`Inference`
adapter（4.2.2 节已提到，此处是技术侧的落地要求），具体由谁评审是
组织流程问题，留给团队自己定，但"必须过这个 CI 检查"是架构层面
不能省略的一步。

---

## 5.5 开放协议与 Skill 生态集成

### 5.5.1 与 MCP/ACP/A2A 的关系边界：分层，不是选型

这三类协议不是互相竞争、需要 EAR 选一个"接入"的东西，它们解决的
问题落在 EAR 不同的层级上，混着谈会导致错误的采纳判断：

- **MCP**：Tool 访问层的协议，EAR 现有 `sandbox_exec`/`mcp` 一类
  adapter（5.3 节）已经是 MCP 客户端的角色，不需要额外设计。
- **ACP**：其能力已并入 A2A（Linux Foundation 统一治理，不再独立
  演进），历史设计中"离线声明能力、不做运行时协商发现"这条思路与
  Resolver 编译期固化白名单是同一方向的独立收敛（见 4.2.2.1），
  部分字段命名约定（`CitationMetadata`/`TrajectoryMetadata`）已在
  5.1 节采用，不需要单独跟踪 ACP 后续。
- **A2A**：跨系统、跨框架、可能跨组织的 Agent 间通信协议，EAR 内部
  的 Sub-agent/Swarm/GoalLoop/PlanningNode 这几种多智能体模式，通信
  双方共享同一套 Python 类型定义（`SubAgentInvocation`/`SubAgentResult`，
  4.5 节），结构上不需要 A2A——套用它的 `Part` 多态类型系统只会
  增加没有对应收益的序列化开销。唯一真正需要它的场景是
  `AutonomousNode`（4.2.2.2 节）委托给一个 EAR 不控制内部过程的
  外部 Agent 时，这类通信双方确实不共享类型系统，adapter 实现成
  A2A 客户端是一条具体、当下就能验证的落地路径。**如果未来某个
  Sub-agent 的调用对象实际上不是 EAR 自己的 Skill、而是另一技术栈
  实现的外部服务，这条通信路径的性质就变了，不应该继续套用
  `SubAgentInvocation`/`SubAgentResult` 这套内部契约，应当归类为
  `AutonomousNode` 或让该节点的 adapter 走 A2A**——是否需要重新
  归类取决于具体业务边界，不是架构层面能预先判定的。

### 5.5.2 Skill Registry：接入 agentskills.io 开放 Skill 生态

**先厘清一处命名冲突**：EAR 现有的"Skill"概念（2.1 节业务映射、
`SkillNode`）指的是编译进图里的一个**执行节点**，与 agentskills.io
规范里的"Skill"（一份可渐进加载的 `SKILL.md` 指令包）是完全不同的
两个东西，只是恰好同名。本节讨论的是后者——一套解决"单次推理调用
该在什么时机装载多少上下文"的内容管理约定，与 EAR 的图执行模型不
在同一层，不需要也不应该用它重新定义 EAR 已有的 Skill 概念。

**落点：新增 Skill Registry，与 Class Registry 同层，不进 Runtime
边界**——Runtime 不理解某个 `SKILL.md` 正文写了什么，和 `Graph`/
节点类定义一样属于 0.1 节"不在 Runtime 边界内"的外部资产：

```python
class SkillRef(BaseModel):
    skill_id: str    # 对应 SKILL.md 所在目录名，即 frontmatter 的 name
    version: str

class InferenceBinding(BaseModel):
    ...
    skills: list[SkillRef] = []   # 编译期声明这次调用可能用到哪些
                                   # 开放 Skill，运行时不能引用清单外的
```

`Resolver.resolve()` 校验每个 `skill_id`+`version` 在 Skill Registry
里确实存在，把结果连同解析出的元数据一并烘焙进 `ExecutionPlan`——
这条闭集纪律与 `PlanningBinding.allowed_node_classes` 完全一致，
不能因为 Skill 是"内容"而不是"系统"就放松：触达范围仍然需要在
编译期可枚举（3.2 节）。

**三层加载，直接采用规范定义的分层，只是把"谁在什么时机做"对应到
EAR 的阶段**：

| 层级                                           | 内容            | 发生阶段                                                                               |
| ---------------------------------------------- | --------------- | -------------------------------------------------------------------------------------- |
| 元数据（`name`/`description`，~100 token） | 候选 Skill 清单 | 编译期，Resolver 写入`node_bindings[node].config['skill_catalog']`                   |
| 指令正文（完整`SKILL.md`，<5000 token）      | 具体指令        | 运行期，`Inference` adapter 内部按需决定是否激活、拼入 prompt，Kernel 不参与这个判断 |
| 资源（`scripts`/`references`/`assets`）  | 附加材料        | 运行期，按需，`scripts/` 例外见下                                                    |

**`scripts/` 不能被 `Inference` adapter 直接执行**：这是可执行代码，
若在 `invoke()` 内部直接跑，等于在 Runtime 完全不知情的情况下发生
一次外部访问，违反 1 章"一切外部访问归 `Tool`"的底线。正确做法是
把某个 Skill 的 `scripts/` 目录整体注册成 5.3 节已有的 `sandbox_exec`
类型 `Tool`，`Inference` 只能建议"要不要跑这个脚本"，真正执行必须
回到图里显式的 `Tool` 节点，不能在 `invoke()` 内部悄悄发生。

**Skill Registry 准入检查，不是"格式合规就能用"**：`skills-ref validate` 只检查 frontmatter 格式（命名规则、字段长度限制），不检查
内容质量。进入 Skill Registry 前需要额外一道质量门槛——`description`
必须同时说清楚"做什么"和"什么时候用"（规范自身给出的"好例子/差
例子"标准），可以做成 CI 检查（长度下限、结构化字段要求，或过一次
LLM 评审打分），与 5.4 节 Adapter 治理是同一类"新增资产必须过机械
检查，不能靠人工评审兜底"的思路。这套准入机制同时回填了 4.2.2.1
节的一个缺口——`PlanningNode` 的 `allowed_node_classes` 里每个候选
节点类不需要另起一套描述字段，直接要求它在 Class Registry 里关联
一份符合本节标准的 `SKILL.md`，`PlanningNode` 生成草稿时读取的就是
这份 Skill 的元数据层，与普通 `Inference` 判断"要不要激活某个领域
知识"复用同一套机制，不是两套平行系统。

**已知风险，需要在文档里明确、不能假装不存在**：如果 Skill 是从
agentskills.io 这类开放生态直接拉取而非内部编写，`references`/
`assets` 里的内容是**未经审查的第三方文本**，会被直接拼进模型
prompt，构成一个新的 prompt 注入面。`scripts/` 层面已靠"必须走
`Tool` 节点执行"这条规则挡住执行风险，但内容注入层面，现在只能靠
Skill Registry 准入审查这一道关卡，没有额外的机械拦截——这条限制
需要明确记录，不能让读者误以为格式合规审查顺带解决了内容可信问题。

---

## 6.1 事件类型表

```python
RuntimeEventType = Literal[
    "ExecutionStarted", "NodeStarted", "InferenceDelta",
    "ToolStarted", "ToolCompleted", "StateUpdated",
    "ArtifactEmitted", "NodeCompleted",
    "ExecutionPaused", "ExecutionCompleted", "ExecutionFailed", "ExecutionCancelled",
    "PlanningCompiled", "PlanningRejected",  # 4.2.2 节例外，见下方"业务事件边界"说明
]

class RuntimeEvent(BaseModel):
    """Lifecycle 发出的所有事件共用这一个 schema，
    是 Lifecycle『事件发布』职责的正式数据契约，不是新对象。
    event_type 是封闭枚举，不是开放字符串——见下方"业务事件边界"说明。"""
    event_id: str
    context_id: str
    seq: int
    event_type: RuntimeEventType
    node_name: str | None = None
    payload: dict
    created_at: datetime
```

| Event                  | 前端展示                                                                                               | Trace(Langfuse) | Audit | Notify |
| ---------------------- | ------------------------------------------------------------------------------------------------------ | --------------- | ----- | ------ |
| `ExecutionStarted`   | 开始执行                                                                                               | ✔              | ✔    | ✔     |
| `NodeStarted`        | 当前节点高亮                                                                                           | ✔              | ✔    | 可选   |
| `InferenceDelta`     | 流式输出（打字机）                                                                                     | ✔              | ✘    | ✘     |
| `ToolStarted`        | "正在调用 PostgreSQL/MCP"（含节点级与 AutonomousNode 内部工具调用，见 4.2.2）                          | ✔              | ✔    | ✘     |
| `ToolCompleted`      | 展示耗时、结果摘要（同上，payload 携带`tool_call_id`/`is_write`/`decision` 时区分粒度）          | ✔              | ✔    | ✘     |
| `StateUpdated`       | Context 面板更新                                                                                       | 可选            | ✘    | ✘     |
| `ArtifactEmitted`    | 渲染 Artifact（表格/引用/图表等）                                                                      | 可选            | ✔    | ✘     |
| `NodeCompleted`      | 节点完成                                                                                               | ✔              | ✔    | 可选   |
| `ExecutionPaused`    | 弹出 HITL 确认框（含 AutonomousNode 工具调用权限确认，`payload.suspend_contract.type` 区分具体原因） | ✔              | ✔    | ✔     |
| `ExecutionCompleted` | 最终结果                                                                                               | ✔              | ✔    | ✔     |
| `ExecutionFailed`    | 错误提示                                                                                               | ✔              | ✔    | ✔     |
| `ExecutionCancelled` | 取消提示                                                                                               | ✔              | ✔    | ✔     |
| `PlanningCompiled`   | 展示"已生成子计划"                                                                                     | ✔              | ✔    | 可选   |
| `PlanningRejected`   | 展示"委托被拒绝，走兜底分支"                                                                           | ✔              | ✔    | 可选   |

**业务事件边界**：`event_type` 收紧为一个封闭枚举，而不是
`str`，是为了让"Runtime 不理解业务"从约定变成类型系统强制的结果——
开放字符串无法阻止某个 `SkillNode` 内部直接
`state.emit("RiskThresholdExceeded", {...})`，因为 `emit` 闭包已经
打通到 Event Store/SSE 全链路，对业务开发者是阻力最小的路径，但这
恰好会让 Runtime 反过来理解业务语义。业务级信号不新增 `event_type`，
一律作为已有事件（`ArtifactEmitted`/`StateUpdated`/`ToolCompleted`
等）`payload` 里的结构化字段表达，由 Notify/Audit 等订阅方（Runtime
边界之外）自行解读——例如"资产 X 触发风险阈值"由 Notify 订阅
`ExecutionPaused` 并读取 `payload.node == "risk_gate"` 后自行翻译成
面向业务的通知文案，翻译逻辑始终留在 Business/Application 层，不下
沉进 Runtime。这与"新需求优先丰富数据契约，而非新增对象"是同一条
原则的延伸——业务表达力靠 `payload` 的结构丰富度，不靠枚举扩容。

**`PlanningCompiled`/`PlanningRejected` 是这条原则下的显式例外，
不是松动**：判断一个新信号该不该开新枚举值的标准，不是"这个信号
重不重要"，而是"这是 Runtime 自己的控制流状态变化，还是业务对
Runtime 已知状态变化的语义翻译"。`RiskThresholdExceeded` 属于后者——
它是"节点执行完成"这个 Runtime 事实的业务解读，翻译工作留给
Notify/Audit 做。`PlanningCompiled`/`PlanningRejected` 属于前者——
"一份新的子 `ExecutionPlan` 被编译出来"和"一次委托请求在编译期被
拒绝"是 Runtime 自己的结构性事实，不是任何业务概念的翻译，且已有
12 个事件里没有一个能准确表达（不是"节点失败"，不是"节点完成"，
是"提案被闸门拦下、图仍继续走"这个 4.2.2 节新增的中间状态）——这才
是允许它们进入封闭枚举的理由。4.2.2 节里最初设计还包含
`PlanningDrafted`/`ToolCallRequested`/`ToolCallPermissionAsked`/
`ToolCallPermissionResolved`/`ToolCallCompleted` 等候选事件，逐一
核对后发现全部能被 `ArtifactEmitted`/`ToolStarted`/`ToolCompleted`/
`ExecutionPaused`/`ExecutionStarted` 的既有语义+更细的 payload 覆盖，
因此没有进入这张表——新增枚举值的门槛就是"找不到任何现有事件能
表达"，不是"这个场景是新功能"。

## 6.2 一份事件，五个独立消费者

```
                Executor（经 Lifecycle.emit）
                            │
                  ┌─────────┴─────────┐
                  ▼                   ▼
          Event Store（PG）      Event Broadcast
        execution_events 表      （进程内 / 可选 Redis Pub/Sub）
                  │                   │
        ┌─────────┼─────────┐        │
        ▼         ▼         ▼        ▼
      Audit    Metrics   Trace     SSE（前端实时展示）
    （审计日志）（耗时/成功率）（Langfuse）
```

```sql
CREATE TABLE execution_events (
    event_id     UUID PRIMARY KEY,
    context_id   UUID REFERENCES execution_context(context_id),
    seq          BIGINT NOT NULL,
    event_type   TEXT NOT NULL,
    node_name    TEXT,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON execution_events (context_id, seq);
```

- **Event Store（PostgreSQL）**：每个事件落一行，`(context_id, seq)`
  保证严格有序，用于历史回放。
- **Event Broadcast**：单进程部署时用进程内 `asyncio` 广播即可；
  多 Worker/多实例横向扩展时，才启用 Redis Pub/Sub 把事件广播给所有
  SSE 服务实例——是否启用 Redis 只是 `event_broadcast` 这一个接口
  的两种实现，不影响其余任何对象。
- **Audit**：订阅 `ExecutionStarted`/`NodeStarted`/`ToolStarted`/
  `ToolCompleted`/`ArtifactEmitted`/`NodeCompleted`/终态事件（含
  `ExecutionCancelled`），写入 `agent_invocation_log`，仅审计角色
  可查，按月分区。
- **Notify**：订阅 `ExecutionPaused`/`ExecutionCompleted`/
  `ExecutionFailed`/`ExecutionCancelled`，走邮件/企业微信/Webhook，
  尽力而为不承诺重试。
- **Trace（Langfuse）**：`ExecutionStarted`→trace 开始，
  `NodeStarted`/`NodeCompleted`→span 起止，`InferenceDelta`→
  generation 流式记录，`ToolStarted`/`ToolCompleted`→tool span。
- **Metrics**：从 `ToolCompleted`/`NodeCompleted`/`ExecutionCompleted`
  的时间戳差值统计耗时、从 `ExecutionCompleted`/`ExecutionFailed`
  的比例统计成功率。

## 6.3 SSE：任务执行与页面展示彻底解耦

```
POST /executions  →  Application.submit()  →  arq 入队
                                                   │
                                                   ▼
                                          arq Worker 执行
                                                   │
                                                   ▼
                                    Executor.run() → emit(RuntimeEvent)
                                                   │
                              ┌────────────────────┴───────────────────┐
                              ▼                                        ▼
                     Event Store（execution_events）           Event Broadcast
                              │                                        │
                              └──────────────┬─────────────────────────┘
                                              ▼
                        GET /executions/{context_id}/events（SSE）
                                              │
                                              ▼
                                          前端 Vue
```

```python
@app.get("/executions/{context_id}/events")
async def stream_events(context_id: str):
    async def gen():
        # 1) 回放历史：页面刷新/重新进入也能看到完整过程
        last_seq = -1
        for evt in await event_store.replay(context_id):
            last_seq = evt.seq
            yield sse_format(evt)
        # 2) 接续订阅：只有还在运行中才继续推送后续事件。
        #    回放查询与订阅建立之间存在窗口期，广播通道可能已经在这
        #    期间推过若干事件——用 seq 兜底，而不是假设两步之间无缝衔接：
        #    seq <= last_seq 的广播事件是回放已经覆盖过的，丢弃；
        #    seq > last_seq + 1 说明中间有广播事件丢失（广播是"尽力而为"
        #    通道，6.1 节同样的可靠性边界），此时从 Event Store 补一次
        #    增量查询，而不是留下空洞。
        if await is_still_running(context_id):
            async for evt in event_broadcast.subscribe(context_id):
                if evt.seq <= last_seq:
                    continue
                if evt.seq > last_seq + 1:
                    for gap_evt in await event_store.replay(context_id, after_seq=last_seq):
                        last_seq = gap_evt.seq
                        yield sse_format(gap_evt)
                    if evt.seq <= last_seq:
                        continue
                last_seq = evt.seq
                yield sse_format(evt)
    return EventSourceResponse(gen())
```

任务本身由 arq Worker 执行，与页面连接状态完全独立——浏览器关闭、
刷新、换设备重新打开，都只是"重新发起一次 SSE 订阅 + 回放"，不影响
后台任务的执行与最终结果的持久化。**HITL 的确认/拒绝/恢复/取消动作
用普通 REST 接口**（`POST /executions/{id}/resume`、
`POST /executions/{id}/cancel` 等），不需要 WebSocket；只有出现真正
的多人实时协同编辑场景时才考虑引入 WebSocket。

---

# 7. 同步 / 异步执行

```python
class Application:
    def __init__(self, resolver: Resolver, executor: Executor, lifecycle: Lifecycle):
        self.resolver, self.executor, self.lifecycle = resolver, executor, lifecycle

    async def submit(self, business_config: dict, mode: str = "sync",
                      trigger_type: str = "http", tenant_id: str = "default",
                      parent_context_id: str | None = None) -> ExecutionContext:
        plan = self.resolver.resolve(business_config, {"tenant_id": tenant_id})
        ctx = ExecutionContext.create(plan, mode, trigger_type, tenant_id, parent_context_id)
        await self.lifecycle.checkpoint(ctx)
        if mode == "sync":
            try:
                return await asyncio.wait_for(
                    self.executor.run(ctx, plan), timeout=plan.sync_timeout_ms / 1000)
            except asyncio.TimeoutError:
                # asyncio.wait_for 在抛出 TimeoutError 前已经把内部协程
                # cancel 并等它退出（Python 3.11+ 保证这一点，作为最低
                # 版本约束）——这只保证"我方协程停止运行"，不保证已经
                # 发出去的外部请求（LLM 推理、Tool 的远端调用）被真正
                # 撤回；因此重新派发必须依赖节点级幂等，不能依赖取消
                # 本身是安全的。
                ctx.mode = "sync_degraded_async"
                await self.lifecycle.checkpoint(ctx)
                # 用 context_id 作为 arq job_id 去重（与 4.3 节 resume()
                # 复用同一机制），同一个 context 不会被重复入队执行。
                await arq_pool.enqueue_job("run_executor", ctx.context_id, _job_id=ctx.context_id)
                return ctx
        else:
            await arq_pool.enqueue_job("run_executor", ctx.context_id, _job_id=ctx.context_id)
            return ctx
```

arq worker 消费同一个 `Executor.run()` 入口，`sync`/`async`/
`scheduled` 从提交那一刻起走同一条路径，区别只在"谁在等结果"；前端
无论走哪种提交方式，观察执行过程的方式都是同一个 SSE 接口。

**节点级幂等与重跑语义**：同步降级、`sync_degraded_async` 重新入队
后，Executor 把 `current_node` 视为"未完成"、整个节点重跑，不尝试
恢复某个协程执行到一半的局部变量——这要求每个 Tool/Inference 的
副作用调用都带上以 `(context_id, node_name, attempt)` 派生的幂等键，
即使前一次的远端请求没有被真正撤回、重跑也不会产生重复副作用（重复
扣费、重复写入）。这条discipline 同样适用于 4.4 节子 `ExecutionContext`
的场景——子任务本身也是一次完整的 `Executor.run()`，同样的重跑语义
覆盖到子任务级别，不需要为跨 Context 编排单独设计一套幂等机制。

---

# 8. 数据流示例：资产风险分析 → 盘活方案全流程

```
① Cron 定时检测到待复核资产 → Application.submit(trigger_type="scheduled")
   或用户通过 REST 直接提交 → Resolver.resolve() 编译出 ExecutionPlan
════════════════ Runtime 边界 ════════════════
② Executor 驱动图，emit("ExecutionStarted")
③ SkillNode(risk_analysis)：
     emit("NodeStarted")
     → Tool(postgres) 查询资产数据 → emit("ToolStarted"/"ToolCompleted")
     → Tool(vector) 检索政策知识 → 产出 retrieval artifact
       → emit("ToolCompleted") 携带 artifact，同时 emit("ArtifactEmitted")
     → Inference 流式生成风险评分与 citation artifact
       → 持续 emit("InferenceDelta")，前端打字机展示
     emit("StateUpdated") / emit("NodeCompleted")
④ 风险评分对应的边指向 SuspendNode（超过阈值，contract.type="hitl"）
   → Lifecycle.pause() → emit("ExecutionPaused")
   → Notify 订阅者通知当前用户；前端 SSE 弹出 HITL 确认框
⑤ 当前用户通过 REST 调用 Resume；如需正式审批，节点内经 Tool(http)
   调用业务系统审批接口
⑥ 图继续推进 SkillNode(revitalization_plan) → SkillNode(report)
   → report 节点里 Tool(filesystem) 产出 document artifact
⑦ 到达 End → Lifecycle.complete() → emit("ExecutionCompleted")
   → Audit 写审计日志、Metrics 更新耗时统计、Notify 通知用户
⑧ 全程所有事件已落 Event Store：用户随时刷新页面或换设备重新打开，
   SSE 接口先回放完整 Timeline 再接续未完成的部分
```

---

# 9. 非功能性需求

| 项                   | 设计                                                                                                                                                                                                                                                                                                                                          |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 多租户隔离           | PostgreSQL RLS，`execution_context`/`execution_events`/`agent_invocation_log` 统一加 `tenant_id`，与全链路追踪的 SQL 注释注入复用同一 Tool 执行入口                                                                                                                                                                                   |
| 权限模型             | RBAC；HITL 恢复操作校验其对该`context_id` 的归属权限；Tool 白名单在 `ExecutionPlan.node_bindings` 生成时已固化                                                                                                                                                                                                                            |
| 灾备/降级            | `ExecutionContext` + `graph_state` 持久化，重启后按 `status` 从断点恢复；`execution_events` 保证事件不因进程重启丢失；节点级幂等键（7 章）保证重跑不产生重复副作用                                                                                                                                                                    |
| 并发所有权           | 单个`context_id` 任意时刻只被一个 Worker 进程持有（arq `_job_id=context_id` 去重强制），不存在跨进程合并状态的场景；真正的并行需求通过 4.4 节的子 `ExecutionContext` 物理隔离，不共享 `graph_state`                                                                                                                                   |
| 部署拓扑             | Resolver 与 Kernel 默认同进程部署在 FastAPI 内；arq worker 独立进程；单实例部署时事件广播走进程内，多实例横向扩展时启用 Redis Pub/Sub                                                                                                                                                                                                         |
| 循环资源上限         | GoalLoop 的`max_iterations`，逻辑内聚在图节点内部                                                                                                                                                                                                                                                                                           |
| 事件与通知可靠性边界 | Event Store 保证不丢失（PostgreSQL 持久化）；Notify 分发尽力而为，不承诺多渠道重试；`suspend_contract.type='child_context_completion'` 的唤醒虽然也走事件广播做快路径，但额外有 Cron 周期对账兜底（4.4 节，同时覆盖 resume 事务提交后未及入队的孤儿 `queued` 行）——控制流恢复不能只依赖"尽力而为"的通道，与 Notify 的可靠性等级要求不同 |
| AI 交互审计留痕      | Audit 订阅者写入，仅审计角色可查，按月分区；`parent_context_id` 建立跨 Context 的级联审计关系，子任务可追溯到发起它的父任务                                                                                                                                                                                                                 |
| Artifact 存储        | 体积较大的 Artifact（如生成的文档）落对象存储，`payload` 中只存引用，不把大文件塞进事件流                                                                                                                                                                                                                                                   |

---

# 10. 分期交付路线图

## 10.0 排序原则：先 Runtime，后 Compile

Runtime（`ExecutionPlan`/`ExecutionContext`/`Executor`/`Lifecycle`/
`Inference`/`Tool`）是一个应当保持稳定的执行内核，职责是"执行已经
定义好的计划"；Compile（`Resolver` 与它背后的 Skill/Workflow/DSL/
Multi-Agent 编译前端）负责把各种业务建模转换成统一的 `ExecutionPlan`，
天然会随业务持续演进。因此交付顺序严格遵循：

```
ExecutionPlan（定义统一 IR）
        ↓
ExecutionContext（定义运行时状态）
        ↓
Executor（让一个 Plan 真正跑起来）
        ↓
Inference / Tool（补充能力）
        ↓
Lifecycle（补充状态流转）
        ↓
Persistence（补充恢复能力）
        ↓
Resolver（开始生成 ExecutionPlan）
        ↓
Workflow / Multi-Agent / DSL（不断增加新的编译前端）
```

好处是：`ExecutionPlan` 一旦在 Phase 1 定型，后续每一个阶段都只是
"新增一种编译方式"或"补充内核的一项能力"，没有一个阶段需要回过头
修改前面阶段已经交付的结构——这与编译器（LLVM）、数据库（Execution
Plan）、操作系统（System Call）能长期保持稳定的原因一致：**内核极少
变化，变化发生在内核之外。**

这套 Runtime 是内部工程决策，不需要向业务方频繁演示进度，因此严格
按顺序交付，不做阶段并行——Phase 1-3 全部是内核内部验证，没有对外
可见的业务闭环，直到 Phase 4 才第一次出现"一个真实 Skill 能跑起来"
的演示，这是刻意的取舍：用较晚出现的业务可见性，换取内核结构从一
开始就不需要返工。

## 10.1 各阶段交付物

**Phase 1：Runtime Core**
定义 `ExecutionPlan`（IR）、`ExecutionContext`、`GraphState`，实现
最小 `Executor`，能够执行单节点图。`ExecutionContext` 的表结构一次
性按最终形态建（含 `tenant_id`/`trace_id`/`parent_context_id`/
`suspend_contract` 字段，先填占位值，不启用 RLS/Trace/跨 Context 编排
逻辑），避免后续阶段再做 schema 迁移；`status` 从一开始就用泛化后的
枚举值（`queued`/`running`/`suspended`/`completed`/`failed`/
`cancelled`），不引入后续要改名的 `paused_for_hitl`；`cancel_requested_at`
字段同样在这一阶段就建好、先不启用协作式取消检查，理由与其余占位
字段一致。`Executor.run()` 从第一行代码起
就调用内存态的 `Lifecycle` stub（仅 `complete`/`fail`，不落库、不
支持 Suspend/Resume），否则 Executor 连自己有没有跑完都无法判断。
图节点调用的 `Inference`/`Tool` 用 echo 级 mock 实现（原样返回输入），
只为验证调度与状态流转是否正确。交付物额外包含 2-3 个手写的
`ExecutionPlan` fixture（YAML/JSON），作为本阶段及后续阶段的标准
测试输入——这时还没有 Resolver，Executor 的验证方式和用手写汇编
测试编译器后端是同一个思路。

**Phase 2：Runtime Capability**
把 Phase 1 的 mock 替换成真实实现：`Inference`（先支持 LLM，基于
PydanticAI 单轮问答）、`Tool`（先支持 `postgres`），用 Phase 1 的
fixture 驱动，完成最小业务闭环验证。`ExtensionResult` 从这一阶段起
就按 `{data, artifacts}` 定义，避免后续改返回值签名。5.2 节"`Inference`
不能持有工具"的 CI 构造期检查从第一个 `Inference` 实现落地起就一并
交付，不留一个"先跑起来、审计边界以后再补"的窗口期——这条边界一旦
在没有检查的情况下被某个业务节点悄悄破坏一次，后续再拦截的成本会
高得多。

**Phase 3：Execution Lifecycle**
`Lifecycle` 从 Phase 1 的内存态 stub 升级为完整版：`Suspend`/`Resume`/
`Cancel`/`Checkpoint`，`ExecutionContext` 落库持久化，`Executor` 增加对
`SuspendNode`（带 `contract: SuspendContract` 字段，默认
`SuspendContract.default_for(SuspendType.HITL)`）的识别与处理，
`Lifecycle.resume()` 从这一阶段起就支持 `resume_trigger`/`payload`、
一致性校验（状态、`allowed_triggers`、目标匹配）与重新入队（`arq _job_id=context_id` 去重）；`Lifecycle.cancel()`/`cancel_confirmed()`/
`is_cancel_requested()` 同一阶段一起交付，`Executor` 的节点边界取消
检查点（4.2 节）不拖到后续阶段——取消和挂起/恢复是同一层"控制执行
流转"的能力，没有理由分两个阶段做。这一阶段结束后，挂起/恢复/取消
机制已经是"任意原因挂起、任意来源恢复、来源需通过白名单校验；任意
状态下都可请求取消、按当前状态决定立即生效还是协作式生效"的通用
版本，后续 Phase 5 的 HITL 与跨 Context 编排都只是"使用"它、传不同的
`SuspendContract`，不需要再回头改动 `Lifecycle`。

**Phase 4：Compile Layer**
实现最小 `Resolver`，支持 **Skill → ExecutionPlan** 编译，不引入
Workflow、Multi-Agent、DSL。拓扑定义采用"节点类字符串引用 + 连线"
的最小 DSL（3.2 节），Resolver 在这一阶段就加上拓扑分叉校验（3.1
节，拒绝任何出边数量超过 1 的节点）——校验逻辑越早加入，后续阶段
的图定义就不会因为"当初没校验"而带着分叉拓扑进入生产。这是第一个
对外可见"业务能跑起来"的阶段——`ExecutionPlan` 的结构在 Phase 1
已经定型，Resolver 只需要对齐这个已经稳定的 IR，不影响内核。

**Phase 5：Workflow & Multi-Agent**
扩展 `Resolver`，支持 **Workflow / Multi-Agent → ExecutionPlan**：
增加图回边（GoalLoop 自主循环）、协调者节点内部并发（`asyncio.gather`
封装、拓扑仍是单向串行）、调用边界子图（Sub-agent，同进程阻塞等待）、
链式移交（Swarm），以及 HITL 的完整编译支持（`hitl_policy`、Reject
回边）。同时落地 4.4 节的跨 Context 编排：`Application.submit()` 的
`parent_context_id` 参数、`on_child_task_finished` 事件订阅者（快
路径）、`reconcile_suspended_parents` 对账 Cron（兜底路径，与本阶段
一起交付、不拖到 Phase 6/7，因为控制流恢复的可靠性不能只依赖事件
广播）。这些模式共用 Phase 1 就定型的同一个 `Executor`，共用 Phase 3
就定型的通用 `Lifecycle.suspend/resume`，本阶段只是编译前端与
Application 层订阅者的扩展，内核不变。

**Phase 6：Async Runtime**
接入 arq，实现 `Sync`/`Async` 共用同一个 `Executor.run()` 入口，
`sync_degraded_async` 超时后真正重新入队（`_job_id=context_id` 去重，
复用 Phase 3 的机制）；接入 arq 的 cron job 支持定时检测派发；补上
节点级幂等键（`(context_id, node_name, attempt)`），保证降级重试与
跨 Context 场景下的子任务重跑不产生重复副作用；完善 REST/Webhook/MQ
触发路径与通知链路（`ExecutionPaused`/`ExecutionCompleted`/
`ExecutionCancelled` 等终态
事件的分发）。

**Phase 7：Infrastructure**
落地 `RuntimeEvent` 管道（`Lifecycle.emit()` → Event Store + Event
Broadcast）、`GET /executions/{id}/events` 的 SSE 接口与历史回放；
接入 Audit、Notify、Trace（Langfuse）、Metrics 四个事件订阅者；正式
启用 Phase 1 已经建好字段的多租户 RLS 与全链路追踪；接入流式推理
（`InferenceDelta`）与 `ExecutionArtifact`（`retrieval`/`citation`
等，`type` 命名登记在共享常量模块 + CI 检查，见 5.1 节）的前端展示。
这一阶段只是"启用"和"接入"，不涉及给 `execution_context` 加字段。

**Phase 8（可选）：Asset Management**
如果企业场景确实需要资产治理（Skill 上线审批、版本灰度、评测
Gate、废弃流程），作为 Runtime 之外的独立模块建设，接入 `Resolver`
背后，与 Runtime 保持解耦，不影响前七个阶段交付的任何内核结构。

---

# 11. 总结

EAR v1.0 的六个核心概念——`ExecutionPlan`、`ExecutionContext`、
`Executor`、`Lifecycle`、`Inference`、`Tool`——覆盖了定时/多路触发、
Multi-Agent 系列模式（Sub-agent/Swarm/GoalLoop）、实时事件观测、
RAG 引用展示这些看起来彼此无关的需求，靠的不是不断新增对象，而是
在每个新需求出现时先问："这真的需要一个新对象吗，还是丰富已有对象
的数据契约就够了？"

定时/多路触发是 `Application.submit()` 的不同调用方；四种 Multi-Agent
模式是 `pydantic_graph` 的不同图形状；实时事件观测是 `Lifecycle`
既有职责的正式化（`RuntimeEvent`），一份事件流喂给 SSE/Event Store/
Audit/Trace/Metrics 五个独立消费者；RAG 引用是 `Tool`/`Inference`
返回信封（`ExtensionResult`）里多出来的 `artifacts` 通道；跨进程的
并行编排是 `ExecutionContext` 通过 `parent_context_id` 自引用、
`SuspendNode` 的 `reason` 从"人工确认"泛化到"等待任意外部条件"——
同一个概念、同一个机制换一个触发源，不是第七个概念。六个概念的
数量从头到尾没有变化，Runtime 的边界也没有变化——业务概念在
Resolver 编译期消失，事件与展示在 Infrastructure 层被消费，Kernel
内部始终只有三个对象在工作。技术栈保持收敛：PydanticAI 负责推理与
流式输出、pydantic_graph 负责执行、arq 负责异步与定时任务、
PostgreSQL(+pgvector) 负责持久化、事件存储与检索，Redis 仅在真正
需要横向扩展时才引入，不为单实例场景增加运维负担。
