"""PostgreSQL adapter for asset.state.read@1 (docs/31 §3/§4).

SQL and database objects live ONLY here.  One short READ ONLY REPEATABLE
READ transaction produces the complete view and ends immediately -- it
never spans a checkpoint, inference, interrupt or worker yield.  Active
Tenant Context and RLS are re-asserted at this seam; the model never sees
tenant, scope, limits or SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.asset_risk.contracts import AssetStateEntry, AssetStateQuery, AssetStateView
from app.contracts.canonical import CanonicalFailure, RetryOwner

_STATEMENT_TIMEOUT_MS = 5_000


class PostgresAssetStateSource:
    """Parameterized fixed-template reads over the profile's asset table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(
        self,
        query: AssetStateQuery,
        *,
        tenant_id: str,
        logical_read_key: str,
        tool_request_id: UUID,
    ) -> AssetStateView | CanonicalFailure:
        refs = sorted(set(query.asset_refs))
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
                await session.execute(text("SET LOCAL transaction_isolation = 'repeatable read'"))
                await session.execute(text("SET LOCAL transaction_read_only = on"))
                await session.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant, true)"),
                    {"tenant": tenant_id},
                )
                result = await session.execute(
                    text(
                        "SELECT asset_ref, asset_class, exposure_amount, currency, status, "
                        "COALESCE(source_revision, '') AS source_revision, "
                        "COALESCE(observed_at, now()) AS observed_at "
                        "FROM asset_risk_asset_state "
                        "WHERE asset_ref = ANY(:refs) "
                        "AND tenant_id = current_setting('grove.tenant_id')"
                    ),
                    {"refs": refs},
                )
                rows = result.fetchall()
                if len(rows) != len(refs):
                    await session.rollback()
                    return _unavailable()
                entries = [
                    AssetStateEntry(
                        asset_ref=str(row[0]),
                        asset_class=str(row[1]),
                        exposure_amount=int(row[2]),
                        currency=str(row[3]),
                        status=cast(Any, str(row[4])),
                    )
                    for row in rows
                ]
                revisions = [str(row[5]) for row in rows if str(row[5])]
                observed = max((row[6] for row in rows), key=lambda value: value.timestamp() if value.tzinfo else 0)
                watermark = f"asset-state:{max(revisions) if revisions else 'initial'}"
                observed_at = observed if observed.tzinfo is not None else datetime.now(UTC)
                return AssetStateView(
                    tool_request_id=tool_request_id,
                    logical_read_key=logical_read_key,
                    assets=tuple(entries),
                    observed_at=observed_at.replace(tzinfo=UTC)
                    if observed.tzinfo is None
                    else observed.astimezone(UTC),
                    source_revision_or_watermark=watermark,
                )


def _unavailable() -> CanonicalFailure:
    return CanonicalFailure(
        error_code="asset_state.resource_selection_unavailable",
        failure_class="resource_selection_unavailable",
        retry_owner=cast(RetryOwner, "run_coordination"),
        retryable=False,
        safe_message="the requested asset selection is unavailable",
        detail_ref=None,
    )


__all__ = ["PostgresAssetStateSource"]
