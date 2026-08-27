"""Production composition for the AssetRisk kernel (docs/31 §2 closed loop).

Wires the three governed seams -- the pre-published Knowledge snapshot,
the PostgreSQL live-state read tool, and the sealed inference port -- into
the worker-side kernel.  The portfolio input source enumerates the tenant's
current asset refs (the run assesses exactly that portfolio; the QUERY
still selects only by explicit refs inside the tool seam).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.asset_risk.graph import InferenceAnswer
from app.asset_risk.kernel import AssetRiskKernel, TypedInferencePortLike, make_asset_risk_infer_caller
from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool
from app.execution.inference_graph import InferenceRequestFactory
from app.knowledge.adapter import ImmutableSnapshotKnowledgeAdapter
from app.knowledge.builder import KnowledgeSourceDocument, build_knowledge_snapshot
from app.knowledge.snapshot import KnowledgeAclPolicy, KnowledgeSnapshot, KnowledgeSnapshotItem

if TYPE_CHECKING:
    from uuid import UUID

# The reference profile's governed policy corpus: frozen at composition time
# into one immutable, content-addressed Knowledge snapshot.
_REFERENCE_POLICIES = (
    KnowledgeSnapshotItem(
        item_ref="policy.exposure@1",
        source_ref="policies.asset-risk@1",
        locator="doc://policy.exposure",
        title="Board exposure policy",
        content=(
            "Aggregate exposure per asset class must stay within the board-approved "
            "limits; a run is compliant when every asset exposure is below its class limit."
        ),
        keywords=("exposure", "limits", "board"),
        classification="internal",
    ),
    KnowledgeSnapshotItem(
        item_ref="policy.collateral@1",
        source_ref="policies.asset-risk@1",
        locator="doc://policy.collateral",
        title="Collateral policy",
        content=("Collateral haircuts follow the regulatory schedule; frozen assets contribute zero exposure relief."),
        keywords=("collateral", "haircut", "frozen"),
        classification="internal",
    ),
)


def build_reference_knowledge_snapshot(visible_tenants: tuple[str, ...]) -> KnowledgeSnapshot:
    """Publish the reference policy corpus as one immutable snapshot."""

    return build_knowledge_snapshot(
        snapshot_ref="knowledge.asset-risk",
        snapshot_version="v1",
        sources=(
            KnowledgeSourceDocument(
                source_ref="policies.asset-risk@1",
                source_version="2026-08",
                classification="internal",
                items=_REFERENCE_POLICIES,
            ),
        ),
        acl_policy=KnowledgeAclPolicy(visible_tenants=visible_tenants, required_scope="execution.run"),
        purpose="asset risk governance corpus",
        trusted_issuer="grove.asset-risk.publisher",
    )


class PostgresPortfolioInputSource:
    """The run's input: the tenant's current portfolio, bounded by the ceiling."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], max_refs: int) -> None:
        self._session_factory = session_factory
        self._max_refs = max_refs

    async def asset_refs(self, tenant_id: str, run_id: UUID) -> tuple[str, ...]:
        async with self._session_factory() as session:
            await session.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": tenant_id})
            rows = (
                await session.execute(
                    text(
                        "SELECT asset_ref FROM asset_risk_asset_state "
                        "WHERE tenant_id = :t ORDER BY asset_ref LIMIT :limit"
                    ),
                    {"t": tenant_id, "limit": self._max_refs},
                )
            ).fetchall()
        return tuple(str(row[0]) for row in rows)


def compose_asset_risk_kernel(
    *,
    inference_port: TypedInferencePortLike,
    inference_request_factory: InferenceRequestFactory,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    worker_tenant_id: str,
    manifest_max_asset_refs: int = 16,
) -> AssetRiskKernel:
    """Build the full AssetRisk kernel from the production seams."""

    from app.asset_risk.graph import build_asset_risk_graph
    from app.asset_risk.postgres_adapter import PostgresAssetStateSource

    knowledge_port = ImmutableSnapshotKnowledgeAdapter(build_reference_knowledge_snapshot((worker_tenant_id,)))
    ceiling = AssetStateReadCeiling(manifest_max_asset_refs=manifest_max_asset_refs)
    asset_tool = AssetStateReadTool(
        source=PostgresAssetStateSource(runtime_session_factory),
        ceiling=ceiling,
    )
    infer = make_asset_risk_infer_caller(inference_port, inference_request_factory)
    graph = build_asset_risk_graph(
        knowledge_port=knowledge_port,
        asset_tool=asset_tool,
        infer=cast(
            "Callable[[str, UUID, str], Awaitable[InferenceAnswer]]",
            infer,
        ),
    )
    input_source = PostgresPortfolioInputSource(runtime_session_factory, manifest_max_asset_refs)
    return AssetRiskKernel(graph_factory=lambda: graph, input_source=input_source)


__all__ = ["PostgresPortfolioInputSource", "build_reference_knowledge_snapshot", "compose_asset_risk_kernel"]
