"""WS-4 projection/reconciliation integration tests against real PostgreSQL.

Seeds authoritative runtime_event + outbox rows, runs the projection role, and
verifies the rebuildable UI read model, watermark advance, outbox relay, the
unknown-schema dead-letter, and a full rebuild from authoritative facts.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.observation.projection import ProjectionReconciler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECTION_URL = "postgresql+psycopg://grove_projection:grove_projection_ws0@127.0.0.1:54329/grove"
MIGRATION_URL = "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove"


def _projection() -> ProjectionReconciler:
    engine = create_async_engine(PROJECTION_URL)
    return ProjectionReconciler(async_sessionmaker(engine, expire_on_commit=False))


async def _seed_tenant(tenant: str) -> None:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
    await engine.dispose()


async def _seed_event(
    tenant: str,
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    run_seq: int,
    schema_ref: str,
    payload: dict[str, object],
    event_type: str = "run.lifecycle",
    source: str = "grove.runtime_worker",
) -> None:
    engine = create_async_engine(MIGRATION_URL)
    occurred = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runtime_event ("
                "event_id, run_seq, tenant_id, run_id, orchestration_id, correlation_id, "
                "causation_id, trace_id, source, source_event_id, event_type, "
                "event_schema_version, payload_schema_ref, payload, occurred_at"
                ") VALUES ("
                ":eid, :rs, :t, :rid, :rid, :corr, NULL, NULL, :src, :seid, :et, "
                "'v1', :schema, CAST(:payload AS jsonb), :occ"
                ")"
            ),
            {
                "eid": event_id, "rs": run_seq, "t": tenant, "rid": run_id,
                "corr": str(run_id), "src": source, "seid": f"{run_id}:{run_seq}",
                "et": event_type, "schema": schema_ref,
                "payload": json.dumps(payload, sort_keys=True), "occ": occurred,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO runtime_event_outbox (tenant_id, run_id, event_id, run_seq, source) "
                "VALUES (:t, :rid, :eid, :rs, :src)"
            ),
            {"t": tenant, "rid": run_id, "eid": event_id, "rs": run_seq, "src": source},
        )
    await engine.dispose()


def _lifecycle_payload(run_id: uuid.UUID, status: str, revision: int) -> dict[str, object]:
    return {"kind": "run_lifecycle", "run_id": str(run_id), "status": status, "run_revision": revision}


async def _ui_events(tenant: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    engine = create_async_engine(MIGRATION_URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT projection_seq, payload_schema_ref, payload "
                    "FROM ui_projection_event WHERE tenant_id = :t AND target_ref = :r ORDER BY projection_seq"
                ),
                {"t": tenant, "r": run_id},
            )
            return [dict(row._mapping) for row in result.fetchall()]
    finally:
        await engine.dispose()


async def _outbox_relayed(tenant: str) -> int:
    engine = create_async_engine(MIGRATION_URL)
    try:
        async with engine.connect() as conn:
            return int((await conn.execute(
                text("SELECT count(*) FROM runtime_event_outbox WHERE tenant_id = :t AND relayed_at IS NOT NULL"),
                {"t": tenant},
            )).scalar_one())
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
class TestProjection:
    async def test_projects_lifecycle_advances_watermark_relays(self) -> None:
        tenant = f"proj-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed_tenant(tenant)
        e1, e2 = uuid.uuid4(), uuid.uuid4()
        await _seed_event(tenant, run_id, e1, 1, "grove.runtime.run-lifecycle.v1",
                          _lifecycle_payload(run_id, "running", 1))
        await _seed_event(tenant, run_id, e2, 2, "grove.runtime.run-lifecycle.v1",
                          _lifecycle_payload(run_id, "succeeded", 1))

        projection = _projection()
        processed = await projection.run_once()
        assert processed >= 2

        events = await _ui_events(tenant, run_id)
        assert [e["projection_seq"] for e in events] == [1, 2]
        assert events[0]["payload"]["status"] == "running"
        assert events[1]["payload"]["status"] == "succeeded"
        assert await _outbox_relayed(tenant) == 2

    async def test_unknown_schema_dead_lettered(self) -> None:
        tenant = f"proj-dl-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed_tenant(tenant)
        await _seed_event(
            tenant, run_id, uuid.uuid4(), 1, "grove.runtime.future.v9",
            {"kind": "future"}, event_type="future.event",
        )
        projection = _projection()
        await projection.run_once()

        engine = create_async_engine(MIGRATION_URL)
        try:
            async with engine.connect() as conn:
                dead = (await conn.execute(
                    text("SELECT count(*) FROM runtime_event_dead_letter WHERE tenant_id = :t"),
                    {"t": tenant},
                )).scalar_one()
                ui = (await conn.execute(
                    text("SELECT count(*) FROM ui_projection_event WHERE tenant_id = :t"),
                    {"t": tenant},
                )).scalar_one()
        finally:
            await engine.dispose()
        assert dead == 1
        assert ui == 0

    async def test_rebuild_from_authoritative_facts(self) -> None:
        tenant = f"proj-rebuild-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed_tenant(tenant)
        await _seed_event(tenant, run_id, uuid.uuid4(), 1, "grove.runtime.run-lifecycle.v1",
                          _lifecycle_payload(run_id, "running", 1))
        await _seed_event(tenant, run_id, uuid.uuid4(), 2, "grove.runtime.run-lifecycle.v1",
                          _lifecycle_payload(run_id, "succeeded", 1))
        projection = _projection()
        await projection.run_once()
        assert len(await _ui_events(tenant, run_id)) == 2

        # Drop the read model and rebuild from runtime_event facts only.
        rebuilt = await projection.rebuild(tenant)
        assert rebuilt == 2
        events = await _ui_events(tenant, run_id)
        assert [e["projection_seq"] for e in events] == [1, 2]

    async def test_idempotent_re_run(self) -> None:
        tenant = f"proj-idem-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed_tenant(tenant)
        await _seed_event(tenant, run_id, uuid.uuid4(), 1, "grove.runtime.run-lifecycle.v1",
                          _lifecycle_payload(run_id, "running", 1))
        projection = _projection()
        await projection.run_once()
        first = len(await _ui_events(tenant, run_id))
        # No new outbox rows; re-running is a no-op for the read model.
        await projection.run_once()
        second = len(await _ui_events(tenant, run_id))
        assert first == second == 1
