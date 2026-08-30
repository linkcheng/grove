"""Shared AssetRisk fixture answer: one gate-passing realistic text."""

GATE_PASSING_FIXTURE_ANSWER = (
    "经对照冻结政策语料评估：本组合仅含一项信用资产，敞口一千二百元，未见任何超限证据；"
    "抵押品折价规则适用但视图未载明折扣率数值，依约束不虚构比率。组合整体结论：暂判合规，建议复核。"
)

assert len(GATE_PASSING_FIXTURE_ANSWER) >= 80
