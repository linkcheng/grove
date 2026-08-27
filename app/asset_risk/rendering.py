"""Asset Risk Reference Profile renderer (WS-6 6.E.3, docs/31 §6).

The Profile owns the renderer for ``AssetStateView@1`` milestones: the
"资产状态已固定" milestone shows only the authorised ``observed_at``, the
record count, completeness and the safe provenance reference.  SQL, table
names, raw tool payloads and asset-authorization differences are never part
of the output surface; the closed render models in ``app.observation.rendering``
make that a structural guarantee rather than a display convention.
"""

from __future__ import annotations

from app.asset_risk.contracts import ASSET_STATE_VIEW_SCHEMA_REF
from app.observation.reducer import DomainViewMilestone
from app.observation.rendering import DomainViewRenderer, RenderedField, RendererRegistry

ASSET_STATE_VIEW_TITLE = "资产状态已固定"


class AssetStateViewRenderer(DomainViewRenderer):
    view_schema_ref = ASSET_STATE_VIEW_SCHEMA_REF
    title = ASSET_STATE_VIEW_TITLE

    def render(self, milestone: DomainViewMilestone) -> tuple[RenderedField, ...]:
        fields = [RenderedField(kind="observed_at", label="观测时间", value=milestone.observed_at.isoformat())]
        if milestone.item_count is not None:
            fields.append(RenderedField(kind="item_count", label="记录数", value=str(milestone.item_count)))
        fields.append(RenderedField(kind="completeness", label="完整性", value="complete"))
        fields.append(
            RenderedField(
                kind="provenance",
                label="数据来源",
                value=f"{milestone.source_ref} @ {milestone.result_hash[:12]}",
            )
        )
        return tuple(fields)


def asset_risk_renderer_registry() -> RendererRegistry:
    """The closed renderer set owned by the Asset Risk Reference Profile."""

    return RendererRegistry((AssetStateViewRenderer(),))


__all__ = [
    "ASSET_STATE_VIEW_SCHEMA_REF",
    "ASSET_STATE_VIEW_TITLE",
    "AssetStateViewRenderer",
    "asset_risk_renderer_registry",
]
