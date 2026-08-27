"""Frozen golden dataset and deterministic structural evaluator (6.F.2).

The golden set pins the reference profile's closed-loop structure: portfolio
selections, the frozen policy corpus size, and the typed report's binding
invariants.  The evaluator is deliberately structural -- LLM answer quality is
NOT judged here; it belongs to the owner-run human review recorded with the
G3 evidence pack.  Every check is deterministic and content-addressed so the
same dataset hash appears in the evaluation evidence.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.asset_risk.contracts import ASSET_REF_PATTERN
from app.contracts.canonical import canonical_hash

GOLDEN_DATASET_REF = "golden.asset-risk-reference@1"
REPORT_KIND = "asset_risk_report.v1"


class GoldenCase(BaseModel):
    """One portfolio case: the run input and its expected closed-loop shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_ref: str = Field(min_length=1, max_length=128)
    asset_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    expected_knowledge_items: int = Field(ge=0)

    @field_validator("asset_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        for ref in value:
            if not re.fullmatch(ASSET_REF_PATTERN, ref):
                raise ValueError("golden asset ref does not match the profile grammar")
        if len(set(value)) != len(value):
            raise ValueError("golden asset refs must be unique")
        return value


# The frozen reference portfolio: two single-asset cases plus one mixed
# portfolio, all against the two-item policy corpus composed into the kernel.
GOLDEN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        case_ref="golden.asset-risk.credit-single@1",
        asset_refs=("asset.golden.credit-1",),
        expected_knowledge_items=2,
    ),
    GoldenCase(
        case_ref="golden.asset-risk.collateral-single@1",
        asset_refs=("asset.golden.collateral-1",),
        expected_knowledge_items=2,
    ),
    GoldenCase(
        case_ref="golden.asset-risk.mixed-portfolio@1",
        asset_refs=("asset.golden.credit-1", "asset.golden.collateral-1"),
        expected_knowledge_items=2,
    ),
)


def golden_dataset_hash() -> str:
    """Content-address the frozen dataset; the hash travels with evidence."""

    return canonical_hash(
        {
            "dataset_ref": GOLDEN_DATASET_REF,
            "cases": [case.model_dump(mode="json") for case in GOLDEN_CASES],
        }
    )


class GoldenCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check: Literal[
        "report_kind",
        "view_schema_ref",
        "view_closure",
        "item_count",
        "view_hash_binding",
        "knowledge_items",
    ]
    passed: bool


class GoldenCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_ref: str
    dataset_hash: str
    checks: tuple[GoldenCheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


class GoldenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_hash: str
    case_results: tuple[GoldenCaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.case_results)


def evaluate_typed_report(
    case: GoldenCase,
    *,
    asset_view: dict[str, Any],
    report: dict[str, Any],
) -> GoldenCaseResult:
    """Evaluate one run's typed artifacts against the frozen expectations."""

    checks: list[GoldenCheckResult] = []
    checks.append(GoldenCheckResult(check="report_kind", passed=report.get("kind") == REPORT_KIND))
    provenance = report.get("asset_provenance")
    checks.append(
        GoldenCheckResult(
            check="view_schema_ref",
            passed=isinstance(provenance, dict)
            and provenance.get("source_ref") == "asset.state.postgres"
            and isinstance(provenance.get("result_content_hash"), str)
            and len(provenance["result_content_hash"]) == 64,
        )
    )
    view_refs = {entry.get("asset_ref") for entry in asset_view.get("assets", ())}
    checks.append(GoldenCheckResult(check="view_closure", passed=view_refs == set(case.asset_refs)))
    checks.append(
        GoldenCheckResult(check="item_count", passed=len(asset_view.get("assets", ())) == len(case.asset_refs))
    )
    checks.append(
        GoldenCheckResult(check="view_hash_binding", passed=report.get("asset_view_hash") == _view_hash(asset_view))
    )
    checks.append(
        GoldenCheckResult(
            check="knowledge_items",
            passed=report.get("knowledge_items") == case.expected_knowledge_items,
        )
    )
    return GoldenCaseResult(
        case_ref=case.case_ref,
        dataset_hash=golden_dataset_hash(),
        checks=tuple(checks),
    )


def _view_hash(view: dict[str, Any]) -> str:
    """Mirror the graph's typed-report view binding (asset_risk.graph)."""

    import hashlib
    import json

    body = {key: view[key] for key in sorted(view) if key not in {"logical_read_key", "tool_request_id"}}
    return hashlib.sha256(
        b"grove.asset-risk.view.v1\x00"
        + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


__all__ = [
    "GOLDEN_CASES",
    "GOLDEN_DATASET_REF",
    "GoldenCase",
    "GoldenCaseResult",
    "GoldenCheckResult",
    "GoldenSummary",
    "REPORT_KIND",
    "evaluate_typed_report",
    "golden_dataset_hash",
]
