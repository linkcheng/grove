# Skill Evaluation, Evolution and Publication

> 架构集：GROVE v1.0
> Profile：Skill Evaluation/Publication required；Evolution optional / offline
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> Reference 规范：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)

## 1. 定位

本文是 Skill Evaluation、Evolution Candidate 和 Publication Pipeline 的
权威专题。使用 `Evolution Module`，不使用含义模糊的“在线 Learning
Runtime”。

> **Evolution 是从受治理 Experience 产生并评测 Capability Candidate 的
> 离线过程；它不是 GROVE 运行依赖，也不能自我修改在线 Agent。**

三者职责：

```text
Skill Evaluation = 产生可比较、可审计的证据
Evolution         = 产生 Candidate
Publication       = 根据证据和授权产生 immutable Version
```

Evaluation 可以独立于 Evolution 运行，例如人工创建 Skill Version 的发布
门禁或生产回归。Evolution 不能同时充当自己的唯一评测者和审批者。

Evaluation/Publication 统称 Skill Governance，是所有 production Skill 的
控制面门禁，不是 `capability.evolution`。Evolution Profile 只增加从
Experience 自动产生 Candidate 的能力；关闭 Evolution 不得绕过发布门禁。

## 2. 输入

- draft Skill Version 或 typed Capability Candidate。
- optional eligible `ExperienceManifest` snapshot。
- golden/holdout evaluation dataset。
- 人工或业务 feedback。
- 当前 production Skill/Policy/Prompt version。
- 明确的 optimization objective 与不可降低的 safety/cost threshold。

禁止直接读取未脱敏生产 trace，禁止未经授权跨 tenant 聚合。

## 3. 输出

- `EvaluationEvidenceBundle`
- `PublicationDecision`
- `SkillCandidate`
- `PolicyCandidate`
- `PromptCandidate`
- `KnowledgeCandidate`
- `EvaluationDatasetCandidate`

Candidate 不是 Version：

```text
candidate_id
tenant_id
candidate_type
source_experience_refs
source_version_refs
proposed_artifact_ref
rationale
generator/model/prompt versions
dataset_snapshot_hash
evaluation_suite_ref
status = proposed | evaluating | approved | rejected | published
```

只有 Registry publish 成功后才产生不可变 Version。

## 4. Pipeline

```text
optional Experience Manifests
  → governed dataset snapshot
  → candidate generation
  → typed CapabilityCandidate ─────┐
                                   │
manually authored draft Skill ─────┘
  → static contract/policy checks
  → isolated Evaluation Run
  → baseline differential report
  → safety/cost hard gates
  → authorized approval
  → immutable Registry Version
  → staged rollout for new runs
  → online observation
  → retain / rollback / new Candidate
```

生成者、评测者和审批者必须可审计。高风险 Skill 应职责分离。

每一阶段只消费前一阶段的 immutable reference。失败产生 evidence，不原地
修补 Candidate 后继续沿用旧结果。

## 5. Evaluation Gate

### 5.1 Evaluation Suite

每个 Suite 固定：

```python
class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str
    version: str
    target_skill_contract_ref: str
    golden_dataset_ref: str
    holdout_dataset_ref: str
    adversarial_dataset_ref: str
    deterministic_check_refs: tuple[str, ...]
    rubric_refs: tuple[str, ...]
    metric_definitions: tuple[str, ...]
    hard_gate_policy_ref: str
    regression_policy_ref: str
    sampling_policy_ref: str
```

Dataset snapshot 必须包含 content hash、tenant/purpose/consent、来源版本、
去重策略和 train/holdout 隔离证据。Evolution generator 不得读取 holdout
答案。

### 5.2 Evaluation Run

```python
class EvaluationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_run_id: UUID
    subject_ref: str
    baseline_version_ref: str | None
    suite_ref: str
    dataset_snapshot_hashes: tuple[str, ...]

    evaluation_subject_hash: str
    permission_envelope_hash: str
    run_mode: Literal["live", "replay", "fork_dry_run", "fork_commit"]
    graph_version: str
    canonical_contract_version: str
    prompt_policy_version: str
    model_policy_version: str
    inference_retry_policy_version: str
    budget_evaluation_envelope_hash: str
    evaluator_versions: tuple[str, ...]
    execution_environment_ref: str
    runtime_build_hash: str

    sample_count: int
    repetition_policy_ref: str
    metric_results: tuple["MetricResult", ...]
    failure_case_refs: tuple[str, ...]
    cost_latency_result_ref: str
    decision: Literal["passed", "failed", "inconclusive"]
    evidence_bundle_ref: str
    trusted_issuer: str
    runner_attestation_ref: str
```

模型输出具有随机性，Evaluation 的“可复现”不是要求每次 token 完全一致，
而是固定输入、版本、环境和采样策略，保留原始 result reference，并用重复
采样、置信区间或明确容差判断是否通过。

Evidence 绑定 `evaluation_subject_hash`，不绑定最终每次 run 的
`skill_spec_hash`。后者包含 principal/effective permission/evidence refs，
会造成循环或把同一行为构建误拆成大量评测对象。

同理，Evidence 绑定 evaluated budget envelope，不为每个经形式证明的有效输入上限
收紧重复运行完整 Evaluation。只有 Manifest 声明的 monotonic input admission
limit、受信任 Resolver 的 typed `positive_integer_componentwise_lte` 校验和完整
BudgetBinding 同时
成立时，才可复用 ceiling evidence；effective limit 与 attestation 进入
`skill_spec_hash`，并补充确定性的契约拒绝和 UX 测试。任意其他 budget 变化必须
生成新的 `evaluation_subject_hash`。完整规则见
[ADR-0022](./adr/0022-monotonic-input-limit-tightening-reuses-ceiling-evidence.md)。

Evaluation Subject 必须绑定 `permission_envelope_hash`，覆盖 Skill/
dependency permission ceiling、effect 分类、authorization policy 和
permission preset；只排除具体 tenant/actor 的 effective scope 实例。它还绑定
Continuation/context policy、RoleTemplate/Child HITL routing semantics，以及
Runtime Build 中固定的 adapter interceptor chain/order/failure policy。启用
Execution Workspace 时还必须绑定 workspace policy、bootstrap artifact、
adapter 和 sandbox image hash。

runner attestation 至少签名 evaluation subject、Suite/dataset/environment、
evaluator versions、metric results、decision 和 evidence bundle hash。
Publication 只信任 allowlist issuer 的有效签名；复制或修改数据库行不能
产生通过证据。

### 5.3 Evaluator 顺序

优先使用更可验证的 evaluator：

1. deterministic contract/policy/security checks。
2. 领域规则、SQL oracle 或人工标注 ground truth。
3. 统计指标和 baseline differential。
4. 经校准的 model-as-judge。
5. 高风险样本的人审。

Model-as-judge 不能是唯一 safety gate；judge model/prompt/rubric 也必须固定
版本，并定期对人工标注集校准。

### 5.4 Gate 维度

至少检查：

1. typed input/output contract。
2. golden 和独立 holdout 回归。
3. 权限绕过、Prompt Injection、敏感数据泄漏和 Workspace 隔离/egress。
4. latency、token、Tool/Workspace/Action budget。
5. 不确定性、拒答和失败模式。
6. 与 current production version 的行为差异。
7. business outcome/KPI 与 baseline 的差异。
8. rollout、监控和 rollback 条件。

训练集分数、用户采纳率或单一 reward 不能独立决定发布。

模型不能降低 safety/cost threshold，也不能选择自己的审批者。

Gate 不只看聚合分：

```text
pass =
  all hard safety gates pass
  AND all permission/contract gates pass
  AND quality lower confidence bound ≥ threshold
  AND cost/latency regression within budget
  AND no protected segment exceeds allowed regression
```

任何 hard gate 失败都不能用其他指标的高分抵消。`inconclusive` 需要增加
样本或人工判断，不能按通过处理。

Business KPI 必须记录 outcome source、attribution window、cohort、baseline
和置信区间。采纳率、点赞或历史相关性不能直接证明因果改进；无法排除流量
选择偏差时应标记 `inconclusive`，不能据此自动发布。

### 5.5 Composition Evaluation

子 Skill 已通过 Evaluation 不代表组合 Skill 自动通过。组合版本必须覆盖：

- typed mapping 和 schema mismatch。
- route/loop/fan-out budget。
- permission 与 capability closure。
- 子 Skill 失败传播和 fallback。
- 顺序依赖、重复副作用和 replay。
- 组合后的端到端质量、成本和延迟。

## 6. Publication

```text
submit Candidate
  → Registry validates type and provenance
  → verify Evaluation evidence hash / issuer / attestation
  → authorized approval
  → publish immutable Version
  → update release channel
```

规则：

- 禁止 UPDATE active Skill/Prompt/Policy/Graph 内容。
- 发布产生新 Version 和 content hash。
- 旧 run 按原 `skill_spec_hash` 恢复。
- 新 run 才按 tenant release policy 选择 approved Version。
- rollback 只移动 release channel，不删除历史 Version。
- deprecated Version 继续支持已有 run，直至排空。
- evaluator、candidate generator 和 approver 身份分别进入审计；高风险 gate
  按 policy 强制职责分离。

## 7. Staged Rollout 与在线观测

```text
shadow
  → internal
  → tenant allowlist
  → percentage canary
  → active
```

每一阶段绑定：

```text
eligible tenants
traffic percentage
minimum sample size
observation window
quality/safety/cost guardrails
automatic stop conditions
authorized promotion owner
```

线上 event、feedback 和 business outcome 进入 Runtime/Experience Projection。
Observation API 只读取 evidence 和 projection；它不能直接 publish、rollback
或产生 Candidate。

触发 guardrail 时，release controller 可以停止新流量或回退 channel，但
不能修改 immutable Version。正在运行的 run 保持原 spec，除非显式执行安全
终止策略。

## 8. Registry 策略

MVP Baseline 只使用：

- Skill Registry。
- Knowledge Registry。

Prompt、Graph、Policy、Model、Schema 作为 Skill Version 固定引用的
artifact。不要先建立七个平级 Registry。

未来统一发现使用只读 `Capability Catalog`：

```text
Capability Catalog = authoritative registries 的可重建 discovery projection
```

Catalog 不拥有 Version，不参与 run 恢复，也不能写 active capability。

## 9. 与 SkillExecutionSpec 的关系

不新增第二个 `ExecutionPlan`。沿用 GROVE：

```text
SkillExecutionSpec
  ├─ Skill / Graph / Contract binding
  ├─ SkillRuntimeManifest ref
  ├─ Permission + capability requirements
  ├─ Budget ref
  ├─ typed policy refs
  ├─ evaluation subject/evidence set
  └─ spec hash
```

spec 中不放 `learning_policy`。在线 run 只决定是否允许收集 Experience；
如何反思与优化属于离线 Evolution policy。

`SkillExecutionSpec` 固定 `evaluation_subject_hash` 和内容寻址的
`evaluation_evidence_set`，使 Experience、成本归因和历史恢复能定位当时
的行为构建与发布门禁。ABI 见
[SkillExecutionSpec ABI](./21_SkillExecutionSpec_ABI.md)。

## 10. Memory 与 Evolution

Memory 不驱动 capability mutation。

同一 Experience 可以分别产生：

- `MemoryCandidate`：个体偏好或 episodic context。
- `KnowledgeCandidate`：可成为企业共享事实的内容。
- `SkillCandidate/PolicyCandidate`：可复用流程和方法。

它们使用不同 gate 和所有者，不能通过 Memory 旁路 Skill/Knowledge 发布。

## 11. 安全与治理

- dataset snapshot 固定 tenant、purpose、consent 和 source versions。
- Candidate 保留完整 provenance。
- source 数据撤回时可定位受影响 dataset/candidate/version。
- Evaluation dataset 与 production telemetry 分离权限。
- 防止 feedback poisoning、reward hacking 和 benchmark leakage。
- 高风险 capability 必须 human approval。
- publish、release channel 变更和 rollback 全部审计。
- 防止 dataset contamination、holdout 泄漏和 evaluator prompt injection。
- 评测 Artifact 和 Observation query 均按 tenant/purpose 重新授权。

## 12. 资源隔离

Evolution 是离线 workload：

- 使用独立 PostgreSQL schema/role/pool。
- 有连接数、CPU、I/O 和 batch rate 配额。
- 不在 GROVE request path 上执行 reflection/evaluation。
- 有数据证明需要后再引入 read replica、专用队列或 Learning framework。

Mem0、Letta、Graphiti、DSPy、LangSmith、DeepEval 等工具不能先于协议选型；
只有某个 POC 明确需要时才评估对应 adapter。

## 13. 最小落地顺序

```text
Phase A
  deterministic checks + golden/holdout + manual approval

Phase B
  baseline differential + model judge calibration + evidence API

Phase C
  Experience-driven Candidate in shadow mode

Phase D
  staged rollout + automated stop + governed publication
```

Phase A 不需要独立 Evaluation 服务、向量数据库或训练平台。使用 PostgreSQL
保存 metadata/reference，测试 runner 作为离线 process 即可。

## 14. 被否决的方案

- Learning 直接发布 active Skill。
- 运行中的 thread 自动切换优化版本。
- Memory 自动生成并启用 procedural capability。
- 使用单一 reward 或训练集得分发布。
- 中央 Registry 成为所有资产和 run 恢复的共同所有者。
- 在缺少数据和 POC 前引入独立 Learning 服务。
- 使用一个总分掩盖 safety、permission 或 protected segment 回归。
- Candidate generator 读取 holdout 答案或担任唯一 judge。
