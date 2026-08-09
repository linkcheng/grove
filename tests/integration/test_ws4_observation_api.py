"""WS-4 Observation API service integration tests against real PostgreSQL."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from app.auth.context import ActiveTenantContext, Principal, PrincipalKind
from app.observation.projection import ProjectionReconciler
from app.services import observation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

API_URL = "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove"
PROJECTION_URL = "postgresql+psycopg://grove_projection:grove_projection_ws0@127.0.0.1:54329/grove"
MIGRATION_URL = "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove"


def _ctx(tenant: str) -> ActiveTenantContext:
    return ActiveTenantContext(
        tenant_id=tenant, principal=Principal(principal_id="obs-user", kind=PrincipalKind.HUMAN)
    )


async def _seed(tenant: str, run_id: uuid.UUID, status: str, revision: int) -> None:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, workload_ref, scopes, active) "
                "VALUES (:t, 'obs-worker', 'workload', 'obs', '[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'obs-worker', 'workload') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_spec (tenant_id, skill_spec_hash, spec_ref, spec_payload) "
                "VALUES (:t, :h, :ref, CAST(:spec AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant, "h": "b" * 64, "ref": "execution-spec:" + "b" * 64, "spec": '{"x":1}'},
        )
        await conn.execute(
            text(
                "INSERT INTO agent_run (tenant_id, run_id, submission_id, submission_digest, "
                "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, "
                "runtime_build_ref, runtime_build_hash, status, revision) "
                "VALUES (:t, :rid, :sid, :dig, 'obs-worker', 'workload', :ssh, :ssr, "
                "'b', :rbh, :status, :rev)"
            ),
            {
                "t": tenant, "rid": run_id, "sid": uuid.uuid4(), "dig": run_id.hex.ljust(64, "0")[:64],
                "ssh": "b" * 64, "ssr": "execution-spec:" + "b" * 64, "rbh": "a" * 64,
                "status": status, "rev": revision,
            },
        )
    await engine.dispose()


async def _seed_event(tenant: str, run_id: uuid.UUID, run_seq: int, status: str, revision: int) -> None:
    eid = uuid.uuid4()
    engine = create_async_engine(MIGRATION_URL)
    occurred = datetime.now(UTC)
    payload = {"kind": "run_lifecycle", "run_id": str(run_id), "status": status, "run_revision": revision}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runtime_event (event_id, run_seq, tenant_id, run_id, orchestration_id, "
                "correlation_id, causation_id, trace_id, source, source_event_id, event_type, "
                "event_schema_version, payload_schema_ref, payload, occurred_at) "
                "VALUES (:eid, :rs, :t, :rid, :rid, :corr, NULL, NULL, 'grove.runtime_worker', :seid, "
                "'run.lifecycle', 'v1', 'grove.runtime.run-lifecycle.v1', CAST(:p AS jsonb), :occ)"
            ),
            {"eid": eid, "rs": run_seq, "t": tenant, "rid": run_id, "corr": str(run_id),
             "seid": f"{run_id}:{run_seq}", "p": json.dumps(payload, sort_keys=True), "occ": occurred},
        )
        await conn.execute(
            text("INSERT INTO runtime_event_outbox (tenant_id, run_id, event_id, run_seq, source) "
                 "VALUES (:t, :rid, :eid, :rs, 'grove.runtime_worker')"),
            {"t": tenant, "rid": run_id, "eid": eid, "rs": run_seq},
        )
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
class TestObservationApiService:
    async def test_inspect_and_query_and_stream(self) -> None:
        tenant = f"api-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed(tenant, run_id, "succeeded", 1)
        await _seed_event(tenant, run_id, 1, "running", 1)
        await _seed_event(tenant, run_id, 2, "succeeded", 1)

        projection = ProjectionReconciler(async_sessionmaker(create_async_engine(PROJECTION_URL)))
        await projection.run_once()

        engine = create_async_engine(API_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ctx = _ctx(tenant)

        async with factory() as session:
            view = await observation.inspect(session, ctx, run_id)
        assert view.status == "succeeded"
        assert view.last_run_seq == 2
        assert view.last_projection_seq == 2
        assert view.completeness == "complete"

        async with factory() as session:
            events, cursor = await observation.list_runtime_events(session, ctx, run_id, 0, 10)
        assert [e.run_seq for e in events] == [1, 2]
        assert cursor == 2

        async with factory() as session:
            ui, _ = await observation.list_ui_events(session, ctx, run_id, 0, 10)
        assert [u.projection_seq for u in ui] == [1, 2]

        streamed = []
        async for view_event in observation.stream_ui_events(factory, tenant, run_id, 0):
            streamed.append(view_event)
            if len(streamed) >= 2:
                break
        assert [s.projection_seq for s in streamed] == [1, 2]
        await engine.dispose()

    async def test_cross_tenant_isolation(self) -> None:
        tenant = f"api-iso-{uuid.uuid4().hex[:8]}"
        other = f"api-other-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed(tenant, run_id, "succeeded", 1)
        await _seed_event(tenant, run_id, 1, "succeeded", 1)
        await ProjectionReconciler(async_sessionmaker(create_async_engine(PROJECTION_URL))).run_once()

        engine = create_async_engine(API_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            own = await observation.list_runtime_events(session, _ctx(tenant), run_id, 0, 10)
            cross = await observation.list_runtime_events(session, _ctx(other), run_id, 0, 10)
        await engine.dispose()
        assert len(own[0]) == 1
        assert len(cross[0]) == 0
