---
status: accepted
---

# Telemetry 只能在硬安全包络内配置

GROVE 允许通过 versioned Telemetry Policy 配置 sampling、retention、exporter、
安全 attribute、redaction 和 alert threshold，但平台硬安全底线不可放宽。
credential、token、secret、signed URL 和 chain-of-thought 永不采集；常规 OTel、
metric 和 log 不保存业务正文，也不能建立可信 Tenant、Principal 或授权上下文。

Telemetry Policy 不影响 Skill 行为或权限，因此不进入 SkillExecutionSpec 和
Evaluation Subject。Runtime Build 固定 resolver/redactor/OTel 实现，每个新
signal 记录实际 policy version；策略收紧对后续 signal 立即生效。

完整产品若出现真实敏感排障需求，可以单独交付 Diagnostic Capture release。
它不是 Skill runtime Capability；Diagnostic Capture Session 必须审批、限时、限
Tenant/Run/组件/字段、限预算，并把获批字段投影写入独立治理存储，而不是普通
telemetry backend。

## Consequences

- MVP 实现 Telemetry Policy 和安全默认值，但不实现 Diagnostic Capture Session。
- Tenant 配置只能在 Platform/Deployment 上限内收窄或选择，不得增加禁止字段或
  高基数 metric label。
- 提高日志级别不能成为敏感内容采集的旁路；未启用 capture 时返回
  `CapabilityUnavailable`。
- Diagnostic Capture 上线前必须新增独立 blocker、POC、权限、retention、删除和
  sink failure 验收，不能因本 ADR 已接受就视为可发布。
