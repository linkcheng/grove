"""Asset Risk Reference Profile tool contracts (docs/31 §3, ADR-0018..0022).

The Profile owns these schemas; the Execution Core never learns asset
fields, SQL or selection policy.  ``AssetStateQuery@1`` selects assets only
by explicit refs -- filter/search/pagination/sort/all_assets are not part
of the schema and therefore fail closed as unknown fields before any
provider or database call.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.contracts.canonical import CanonicalModel, canonical_hash

ASSET_REF_PATTERN = r"^asset\.[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
TOOL_REF = "asset.state.read@1"
OPERATION = "asset.state.read"
# The typed read tool's output view schema: pinned into the run's domain-view
# facts and selected by the Profile-owned UI renderer.
ASSET_STATE_VIEW_SCHEMA_REF = "AssetStateView@1"

FailureClass = Literal[
    "input_contract_invalid",
    "resource_selection_unavailable",
    "tool_query_too_broad",
    "tool_unavailable",
]


class AssetStateQuery(CanonicalModel):
    """``AssetStateQuery@1``: explicit, unique, bounded asset selection."""

    asset_refs: tuple[str, ...] = Field(min_length=1, max_length=1024)

    @field_validator("asset_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for ref in value:
            if not re.fullmatch(ASSET_REF_PATTERN, ref):
                raise ValueError("asset ref does not match the AssetStateQuery@1 grammar")
        if len(set(value)) != len(value):
            raise ValueError("asset_refs must be unique")
        return value

    @model_validator(mode="after")
    def reject_selection_expansion(self) -> AssetStateQuery:
        # The closed schema already rejects extra fields; this guard makes the
        # intent explicit for the equivalence class (all_assets/filter/search).
        dumped = self.model_dump()
        forbidden = {"filter", "search", "query", "all_assets", "limit", "offset", "sort", "order_by"}
        if forbidden & set(dumped):
            raise ValueError("AssetStateQuery@1 does not accept selection expansion fields")
        return self


class AssetStateEntry(CanonicalModel):
    """One typed asset row as observed inside the read transaction."""

    asset_ref: str = Field(min_length=1, max_length=256)
    asset_class: str = Field(min_length=1, max_length=64)
    exposure_amount: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=8)
    status: Literal["active", "frozen", "retired"] = "active"


class AssetStateView(CanonicalModel):
    """``AssetStateView@1``: the run-scoped, all-or-nothing data view."""

    tool_request_id: UUID
    logical_read_key: str = Field(min_length=1, max_length=256)
    assets: tuple[AssetStateEntry, ...] = Field(min_length=1, max_length=1024)
    observed_at: datetime
    source_revision_or_watermark: str = Field(min_length=1, max_length=256)

    @field_validator("observed_at")
    @classmethod
    def observed_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("assets")
    @classmethod
    def assets_unique(cls, value: tuple[AssetStateEntry, ...]) -> tuple[AssetStateEntry, ...]:
        refs = [item.asset_ref for item in value]
        if len(set(refs)) != len(refs):
            raise ValueError("asset entries must be unique per view")
        return value

    @property
    def asset_refs(self) -> frozenset[str]:
        return frozenset(item.asset_ref for item in self.assets)

    def result_content_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json", exclude={"logical_read_key", "tool_request_id"}))


__all__ = [
    "ASSET_REF_PATTERN",
    "ASSET_STATE_VIEW_SCHEMA_REF",
    "AssetStateEntry",
    "AssetStateQuery",
    "AssetStateView",
    "FailureClass",
    "OPERATION",
    "TOOL_REF",
]
