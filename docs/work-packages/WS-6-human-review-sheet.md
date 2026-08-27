# WS-6 Human Review 评审表（golden dataset 答案质量）

> 由 `scripts/ws6_human_review_sheet.py` 在真实环境生成（真实 PostgreSQL + 真实 provider，
> 签发链门控）；golden dataset hash `976809da9facee415c3fec84c98966bd3cda39094fc3c27ee566c2a6ec559156`，执行时间 2026-08-26T16:23:01.195883+00:00。
> 评审对象：模型答案的业务质量。结构完整性已由确定性评估器覆盖（`8bdaad2`），不在本表。
> 每例给出：Run 实际看到的组合、引用的冻结政策、模型答案、docs/31 §7 业务预期。
> **请逐例在"负责人评审"栏填写 pass / fail + 意见。**

## 评审输入（固定组合，可复现）

| asset_ref | asset_class | exposure_amount (CNY) | status |
|---|---|---|---|
| `asset.golden.credit-1` | credit | 1200 | active |
| `asset.golden.collateral-1` | collateral | 800 | frozen |

## 冻结政策语料（所有 Run 引用同一 Knowledge Snapshot）

- `policy.exposure@1` — Board exposure policy
- `policy.collateral@1` — Collateral policy

## 逐例评审

### golden.asset-risk.credit-single@1

**Run 看到的资产组合**：

| asset_ref | class | exposure | status |
|---|---|---|---|
| `asset.golden.credit-1` | credit | 1200 | active |

**引用知识条目数**：2（冻结语料为 2 条政策）

**模型答案**：

```text
响应模式所要求的唯一哨兵（必需键）为“answer”，其值必须是字符串；此外模式不接受任何其他属性。以下同时给出本轮受治理评估：一、哨兵判定依据——模式定义仅为 {"additionalProperties": false
```

**业务预期（docs/31 §7 基线）**：

答案应以信用敞口政策为依据，就单一信用资产 1,200 CNY 是否在董事会批准的类别限额内给出明确判断或明确说明所需限额信息；不得编造未引用的事实。

**负责人评审**：

- [ ] pass
- [ ] fail（意见：____________________）

---

### golden.asset-risk.collateral-single@1

**Run 看到的资产组合**：

| asset_ref | class | exposure | status |
|---|---|---|---|
| `asset.golden.collateral-1` | collateral | 800 | frozen |

**引用知识条目数**：2（冻结语料为 2 条政策）

**模型答案**：

```text
响应模式要求的准确哨兵字段为："answer"。以下基于本次运行已接受的资产状态视图与冻结政策语料作治理化资产风险评估：

【资产1：asset.golden.collateral-1｜敞口金额：800】
- 敞口评估：按政策条目“各资产类别的汇总敞口必须保持在董事会批准的限额之内；当每项资产敞口均低于其类别限额时，运行合规”（政策引证：知识条目第2条）。该资产为本视图中唯一资产，敞口为800；视图内未提供任何具体的董事会限额数值，依据‘不虚构限制或事实’的约束，不杜撰数字，仅按规则本身判断——不存在可证实的超限情形，初步判定合规（引证：董事会限额条款）。
- 抵押品折价：按政策条目“抵押品折价遵循监管折扣表；被冻结资产提供零敞口缓释”（政策引证：知识条目第1条）。该资产未被标记为冻结状态，故不适用零缓释规则，应按监管折扣表执行折价；但视图未载明具体折扣率数值，依约束不虚构比率，仅确认折价规则的适用性（引证：抵押品折价条款）。

【组合整体结论】
本视图仅含一项资产（敞口800），在现有引用政策项下无超限或零缓释情形可被证实，组合整体暂判合规；但因董事会类别限额及监管折扣表的具体数值均未随语料给出，无法进行定量精算核验。建议补充相应限额与折扣率的具名政策数值后复核，以将定性合规升级为完整定量确认。
```

**业务预期（docs/31 §7 基线）**：

—

**负责人评审**：

- [ ] pass
- [ ] fail（意见：____________________）

---

### golden.asset-risk.mixed-portfolio@1

**Run 看到的资产组合**：

| asset_ref | class | exposure | status |
|---|---|---|---|
| `asset.golden.credit-1` | credit | 1200 | active |
| `asset.golden.collateral-1` | collateral | 800 | frozen |

**引用知识条目数**：2（冻结语料为 2 条政策）

**模型答案**：

```text
$your_answer
```

**业务预期（docs/31 §7 基线）**：

答案应同时覆盖两条政策：组合内信用资产限额判断 + frozen 抵押资产零减免；对组合给出整体结论，不得遗漏任一资产。

**负责人评审**：

- [ ] pass
- [ ] fail（意见：____________________）

---

## 评审结论（全部用例 pass 方可关闭 G3 human review 项）

- 评审人：____________________
- 日期：____________________
- 结论：☐ 全部通过  ☐ 存在 fail（列出用例与处理决定）

