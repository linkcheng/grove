# Experience Projection

> 架构集：GROVE v1.0
> Profile：optional
> 上位文档：[GROVE Architecture](./00_GROVE_Architecture.md)
> Reference 规范：[Canonical Execution Contracts](./16_Canonical_Execution_Contracts.md)

## 1. 定位

Experience Projection 把一次 Agent Run 的已治理执行事实整理成可供 Memory
和 Evolution 使用的引用清单。

> **Experience 是可删除、可重建、可版本化的离线投影，不是 GROVE 的恢复
> 日志。**

GROVE 不等待 projector 才完成或恢复 run。projector 停机只导致 Experience
滞后，不改变在线执行结果。

RuntimeEvent 是运维事实，不是可直接学习的数据资产；只有经过 tenant、
purpose、consent、redaction、retention 和版本固定后的 Experience，才能
作为 Memory/Evaluation/Evolution 的输入。

## 2. 输入与输出

输入：

- 已脱敏 RuntimeEvent。
- Artifact reference。
- Trace reference。
- action receipt reference。
- user/business feedback。
- evaluation result。

输出：

- `ExperienceManifestRef`。

Observation API 可以读取已授权的 Manifest view，但不因此取得 Experience
的所有权，也不能从 read path 触发 Memory/Evolution 写入。

不直接消费：

- LangGraph/DBOS 内部系统表。
- 未脱敏的全部 trace。
- secret 或 credential。
- 未经 tenant/purpose 授权的数据。

## 3. Experience Manifest

Manifest 不复制完整 input、trace 和 artifact 正文：

```python
class ExperienceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experience_id: UUID
    manifest_version: str
    content_hash: str
    tenant_id: str
    source_run_id: UUID
    orchestration_id: UUID
    parent_run_id: UUID | None
    parent_delegation_id: UUID | None
    trigger_ref: str | None
    trigger_version: str | None
    trigger_hash: str | None
    trigger_occurrence_id: str | None

    skill_id: str
    skill_version: str
    skill_spec_hash: str
    skill_runtime_manifest_ref: ArtifactRef
    evaluation_subject_hash: str
    release_evidence_refs: tuple[EvaluationEvidenceRef, ...]
    graph_version: str

    input_ref: ArtifactRef | None
    trace_ref: TraceRef | None
    action_receipt_refs: tuple[ArtifactRef, ...]
    artifact_refs: tuple[ArtifactRef, ...]
    result_ref: ArtifactRef | None
    feedback_refs: tuple[ArtifactRef, ...]
    evaluation_refs: tuple[ArtifactRef, ...]

    collection_policy_version: str
    redaction_policy_version: str
    consent_basis: str | None
    purpose: str
    schema_version: str
    status: Literal["incomplete", "complete", "revoked"]
    missing_ref_kinds: tuple[str, ...]
    source_watermark: int
    supersedes_ref: ExperienceManifestRef | None
    revocation_reason_ref: ArtifactRef | None
    created_at: datetime
```

引用目标依法删除后，Manifest 不得保存或恢复正文。

`experience_id` 标识 `{tenant, source run, collection policy}` 的稳定 lineage；
每次补齐或撤回都插入新的 immutable `manifest_version/content_hash`。当前可见
版本由独立 Experience Head 指向，禁止 UPDATE 历史 Manifest。

orchestration 字段只保存本次 run 的稳定协作 provenance，不把 Parent/Child
完整 State 或全部事件复制进 Manifest。Parent 与 Child 各自生成独立
Experience；跨 run 归因必须显式使用相同 `orchestration_id` 和
delegation reference，并分别通过 purpose/consent/tenant 授权。trigger
字段要么全为空，要么固定同一受信任 Trigger Definition version/hash 和
occurrence；不能只保留可移动 alias。

`release_evidence_refs` 表示该行为构建启动前通过的 Skill Evaluation；
`evaluation_refs` 表示本次 run 的 outcome/feedback evaluation。两者不能
混为同一个“评分”。

`source_watermark` 是 Experience source inbox 为该 lineage 分配的单调提交
游标，覆盖 event、feedback 和 evaluation reference；它不是 `occurred_at`
或仅取 RuntimeEvent `run_seq`。

## 4. Interface

```python
class ExperienceProjector(Protocol):
    async def project(self, run_id: UUID) -> ExperienceManifestRef: ...
```

implementation 隐藏：

- run 终态/eligibility 判断。
- event 去重和排序。
- artifact/feedback/evaluation reference resolve。
- redaction、purpose、consent、retention。
- retry 和 reconciliation。

调用者不应了解如何拼接 event。

## 5. 投影协议

稳定幂等键：

```text
{tenant_id}:{run_id}:{collection_policy_version}
```

该 key 唯一确定 Experience lineage 和 Head，不代表 lineage 中只能有一个
Manifest Version。

流程：

```text
runtime outbox / stable event IDs
  → async projector
  → authorize collection policy
  → resolve references
  → redact and purpose-filter
  → insert immutable Manifest version
  → CAS Experience Head to new version
  → emit manifest-ready event
```

规则：

1. 至少一次投递，stable source ID 去重。
2. 默认只收集 completed 或显式允许的 run。
3. Manifest 固定 Skill/Graph/Prompt/Model/Tool/Policy 版本。
4. 投影前执行 secret/PII redaction。
5. 保存 tenant、purpose、consent 和 retention。
6. 缺失非关键 event 时生成 `incomplete` version；对账补齐后生成
   `complete` version 并前移 Head。
7. 不从 RuntimeEvent 反向恢复 LangGraph、Memory 或 Action。
8. projector backlog 不得占尽 GROVE 数据库连接。

最小 Head 投影：

```sql
CREATE TABLE experience_head (
    tenant_id                  TEXT NOT NULL,
    source_run_id              UUID NOT NULL,
    collection_policy_version TEXT NOT NULL,
    experience_id              UUID NOT NULL,
    head_version               TEXT NOT NULL,
    head_content_hash          TEXT NOT NULL,
    head_status                TEXT NOT NULL,
    head_source_watermark      BIGINT NOT NULL,
    revision                   BIGINT NOT NULL,
    PRIMARY KEY (
        tenant_id, source_run_id, collection_policy_version
    )
);
```

并发 projector 只能通过 `revision` CAS 前移 Head。相同 canonical bytes/hash
返回现有 version；不同内容创建后继 version。撤回创建 `revoked` tombstone
version 并前移 Head，同时触发 derived dataset/cache 的 lineage 删除。

CAS 还必须满足单调状态与 watermark：

```text
incomplete(w1) → incomplete(w2 ≥ w1) → complete(w3 ≥ w2) → revoked
complete(w1)   → complete(w2 ≥ w1) → revoked
revoked        → no transition in the same policy lineage
```

迟到的低 watermark 或 `incomplete` version 不能覆盖 complete Head；
`revoked` 不能被普通重投影复活。重新获得合法 consent 时必须使用新的
collection policy version/lineage，而不是回退 tombstone。

## 6. Artifact

Artifact 是 run 产生的 durable output，不是 event 或 checkpoint。

初期：

- metadata/reference 存 PostgreSQL。
- 小型结构化内容可存 JSONB。
- 大型二进制仅在实际出现容量需求时接入 object storage。

每个 reference 包含：

```text
artifact_id
tenant_id
content_hash
media_type
schema_version
storage_ref
producer_run_id
retention_policy
sensitivity
```

Experience 只保存 reference，不复制 artifact。

## 7. 数据与权限

- tenant namespace 强制隔离。
- collection policy 不能扩大原始 source ACL。
- Experience consumer 使用独立用途权限。
- 数据集导出需要审计、脱敏和 retention。
- 跨 tenant 聚合默认禁止；只有显式匿名化合同才允许。
- `forget/revoke` 必须传播至 Manifest reference、derived dataset 和 cache。

## 8. 消费者

### Memory Curator

只读取当前、已授权且 `status=complete` 的 eligible Head，输出
`MemoryCandidate`。它不能直接写 active Memory。

### Evolution Module

只读取当前、已授权且 `status=complete` 的 eligible Head，并固定 Manifest
snapshot 后输出 `CapabilityCandidate`。它不能修改 active Registry Version。

两个 consumer 不能直接订阅未脱敏的全部 RuntimeEvent 绕过 Manifest
governance。

## 9. 失败语义

| 故障 | 行为 |
|---|---|
| projector 停机 | GROVE 正常运行；记录 backlog |
| event 重复/乱序 | stable ID 去重；按权威引用重建 |
| event 永久缺失 | reconciliation；Head 指向 immutable incomplete version |
| 迟到的旧 projection | 保留 immutable version，但 Head CAS 按 watermark/status 拒绝回退 |
| artifact 已删除 | 不恢复正文；标记 unavailable |
| policy/consent 撤回 | 创建 revoked tombstone、前移 Head 并传播撤回 |
| schema 升级 | 新 Manifest schema version；显式 migrator |

## 10. 被否决的方案

- 新建统一 Event Store 作为所有 runtime 的恢复真相。
- Experience 复制完整 input、trace 和 artifact。
- Memory/Learning 直接消费所有原始 event。
- projector 与 GROVE 在线事务做分布式 ACID。
- 为尚不存在的大型 artifact 预先引入对象存储基础设施。
- 原地更新 incomplete Manifest 或依法删除的历史正文。
