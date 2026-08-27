"""WS-6 D: PostgresAssetStateSource against the real profile table.

Real PostgreSQL, RLS and grants; no provider involved (never gated).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.asset_risk.contracts import AssetStateQuery
from app.asset_risk.postgres_adapter import PostgresAssetStateSource
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

API_URL = os.environ.get("GROVE_DATABASE_URL", "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove")
MIGRATION_URL = os.environ.get(
    "GROVE_MIGRATION_DATABASE_URL",
    API_URL.replace("grove_api:grove_api_ws0", "grove_migration:grove_migration_ws0"),
)
RUNTIME_URL = API_URL.replace("grove_api:grove_api_ws0", "grove_runtime:grove_runtime_ws0")


def _role_url(base: str, user: str, password: str) -> str:
    return base.replace("grove_api:grove_api_ws0", f"{user}:{password}")


async def _seed(tenant: str, refs: tuple[str, ...]) -> None:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        for ref in refs:
            await conn.execute(
                text(
                    "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                    "currency, status, source_revision, observed_at) VALUES (:t, :ref, 'credit', 100, 'CNY', "
                    "'active', :rev, :obs) ON CONFLICT DO NOTHING"
                ),
                {"t": tenant, "ref": ref, "rev": f"rev-{ref}", "obs": datetime(2026, 8, 21, tzinfo=UTC)},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_complete_selection_reads_through_rls_with_provenance() -> None:
    tenant = f"ws6-src-{uuid4().hex[:10]}"
    refs = ("asset.alpha", "asset.beta")
    await _seed(tenant, refs)
    engine = create_async_engine(RUNTIME_URL)
    source = PostgresAssetStateSource(async_sessionmaker(engine, expire_on_commit=False))
    try:
        outcome = await source.read(
            AssetStateQuery(asset_refs=refs),
            tenant_id=tenant,
            logical_read_key="asset.state.read:key",
            tool_request_id=uuid4(),
        )
    finally:
        await engine.dispose()
    assert not isinstance(outcome, Exception)
    from app.contracts.canonical import CanonicalFailure

    assert not isinstance(outcome, CanonicalFailure)
    assert outcome.asset_refs == frozenset(refs)
    assert outcome.source_revision_or_watermark.startswith("asset-state:rev-")
    assert outcome.observed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_missing_asset_yields_selection_unavailable_without_leakage() -> None:
    tenant = f"ws6-src-{uuid4().hex[:10]}"
    await _seed(tenant, ("asset.present",))
    engine = create_async_engine(RUNTIME_URL)
    source = PostgresAssetStateSource(async_sessionmaker(engine, expire_on_commit=False))
    try:
        outcome = await source.read(
            AssetStateQuery(asset_refs=("asset.present", "asset.absent")),
            tenant_id=tenant,
            logical_read_key="asset.state.read:key",
            tool_request_id=uuid4(),
        )
    finally:
        await engine.dispose()
    from app.contracts.canonical import CanonicalFailure

    assert isinstance(outcome, CanonicalFailure)
    assert outcome.failure_class == "resource_selection_unavailable"
    assert "asset.absent" not in outcome.safe_message


@pytest.mark.asyncio
async def test_cross_tenant_rows_are_invisible_under_rls() -> None:
    owner_tenant = f"ws6-src-{uuid4().hex[:10]}"
    other_tenant = f"ws6-src-{uuid4().hex[:10]}"
    await _seed(owner_tenant, ("asset.only",))
    engine = create_async_engine(RUNTIME_URL)
    source = PostgresAssetStateSource(async_sessionmaker(engine, expire_on_commit=False))
    try:
        outcome = await source.read(
            AssetStateQuery(asset_refs=("asset.only",)),
            tenant_id=other_tenant,
            logical_read_key="asset.state.read:key",
            tool_request_id=uuid4(),
        )
    finally:
        await engine.dispose()
    from app.contracts.canonical import CanonicalFailure

    assert isinstance(outcome, CanonicalFailure)
    assert outcome.failure_class == "resource_selection_unavailable"


@pytest.mark.asyncio
async def test_concurrent_commit_between_statements_stays_on_one_snapshot() -> None:
    """docs/31 §7.3 / POC-M 3: the accepted view comes from exactly one
    READ ONLY REPEATABLE READ snapshot.

    The adapter's transaction shape is mirrored statement-for-statement
    (same GUCs, same order); a writer commits an update between the
    snapshot-defining statement and the data SELECT.  Under repeatable
    read the in-transaction re-SELECT keeps the pre-commit values; a fresh
    transaction afterwards sees the new value -- which is also the
    two-run distinguishing semantics of §7.6.
    """

    tenant = f"ws6-snap-{uuid4().hex[:10]}"
    refs = ("asset.snap-a", "asset.snap-b")
    await _seed(tenant, refs)
    runtime_engine = create_async_engine(RUNTIME_URL)
    migration_engine = create_async_engine(MIGRATION_URL)
    try:
        factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await session.execute(text("SET LOCAL transaction_isolation = 'repeatable read'"))
                await session.execute(text("SET LOCAL transaction_read_only = on"))
                read_only = (
                    await session.execute(text("SELECT current_setting('transaction_read_only')::boolean"))
                ).scalar_one()
                assert read_only is True
                await session.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant, true)"),
                    {"tenant": tenant},
                )
                # The snapshot is now fixed (first query executed).  Commit a
                # concurrent update of asset.snap-b behind the read's back.
                async with migration_engine.begin() as writer:
                    await writer.execute(
                        text(
                            "UPDATE asset_risk_asset_state SET exposure_amount = 500 "
                            "WHERE tenant_id = :t AND asset_ref = 'asset.snap-b'"
                        ),
                        {"t": tenant},
                    )
                before = (
                    await session.execute(
                        text(
                            "SELECT asset_ref, exposure_amount FROM asset_risk_asset_state "
                            "WHERE asset_ref = ANY(:refs) "
                            "AND tenant_id = current_setting('grove.tenant_id')"
                        ),
                        {"refs": list(refs)},
                    )
                ).fetchall()
                again = (
                    await session.execute(
                        text(
                            "SELECT asset_ref, exposure_amount FROM asset_risk_asset_state "
                            "WHERE asset_ref = ANY(:refs) "
                            "AND tenant_id = current_setting('grove.tenant_id')"
                        ),
                        {"refs": list(refs)},
                    )
                ).fetchall()
                assert again == before
                before_map = {str(row[0]): int(row[1]) for row in before}
                assert before_map["asset.snap-b"] == 100  # pre-commit snapshot
        # A fresh transaction sees the committed update: the two reads are
        # distinguishable exactly through observed value / watermark.
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant, true)"),
                    {"tenant": tenant},
                )
                after = (
                    await session.execute(
                        text(
                            "SELECT exposure_amount FROM asset_risk_asset_state "
                            "WHERE tenant_id = current_setting('grove.tenant_id') "
                            "AND asset_ref = 'asset.snap-b'"
                        ),
                    )
                ).scalar_one()
                assert after == 500
    finally:
        await runtime_engine.dispose()
        await migration_engine.dispose()
