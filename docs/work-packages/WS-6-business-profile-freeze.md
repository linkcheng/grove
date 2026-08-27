# WS-6 Business Profile 冻结记录（D0.1）

## 冻结决定

- **决定**：采用仓库提供的 [Asset Risk Reference Business Profile](../31_Asset_Risk_Reference_Profile.md)。
- **批准**：负责人于 2026-08-21 明确选定（"A"），接受 POC-M 与该 Profile 引用的全部资产 ADR。
- **路径理由**：负责人确认以参考场景先行验证平台价值闭环；未选择自选领域，因此不存在待起草的独立 Profile 文档。

## 内容寻址身份

| 字段 | 值 |
|---|---|
| `business_profile_ref` | `business-profile.asset-risk@1` |
| `business_profile_version` | `1` |
| `business_profile_hash` | `65705bfc35221c1295b643c8ae7d043d7d3185679264a3850f0072bc35ce5b30` |
| 来源文档 | `docs/31_Asset_Risk_Reference_Profile.md`（冻结时全文 sha256） |
| 复算命令 | `shasum -a 256 docs/31_Asset_Risk_Reference_Profile.md` |

**失效规则**：来源文档在冻结后发生任何字节变化即本记录失配；必须重新冻结并产生新的 ref/hash，历史绑定不自动沿用（与 release candidate 失效规则一致）。

## 接受的范围

- Profile 约束（`docs/31`）：参考业务闭环、`AssetRiskSkill@1`、`asset.state.read@1` typed read Tool、
  Graph 与时间语义、all-or-nothing 完整性、预算边界、前端投影与发布验收 1–8 全部生效。
- 引用 ADR（已核对全部存在）：ADR-0011、ADR-0012、ADR-0018、ADR-0019、ADR-0020、ADR-0021、ADR-0022、ADR-0024。
- POC-M 与资产专项验收随 G3 执行（属 WS-6 F 线），本冻结是其前置条件而非证据。
- 未声明的能力（Durable Action、Execution Workspace、Run Delegation、Long-Term Memory、
  Experience、Evolution、Multi-Agent）保持 unavailable；该 Profile 只允许 `pure`/`read` Effect Class。

## 解锁与边界

- 解锁：C 线（Knowledge Baseline / POC-E）、D 线（Skill/Profile 真实化与 golden dataset）、
  E3（Profile 拥有的 typed renderer）、A4（真实 provider 图内推理的 Profile run）。
- 本记录不形成 G3、Core/Product release 结论；`verified` 仍按 ROADMAP 规则由负责人在验收后批准。

## 待办确认（不阻塞开工，D 线内关闭）

- golden dataset 语料来源：参考数据集起步或替换为真实资产数据（负责人后续确认）。
- 业务质量阈值：以 `docs/31` §7 验收 1–8 为基线，D 线 6.D.1 冻结时可收紧不可放宽。
