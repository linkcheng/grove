"""WS-4 Observation API service integration tests against real PostgreSQL."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

import pytest
from app.auth.context import ActiveTenantContext, Principal, PrincipalKind
from app.observation.projection import ProjectionReconciler
from app.services import observation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

API_URL = os.environ.get("WS4_API_DATABASE_URL", "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove")
PROJECTION_URL = os.environ.get(
    "WS4_PROJECTION_DATABASE_URL",
    "postgresql+psycopg://grove_projection:grove_projection_ws0@127.0.0.1:54329/grove",
)
MIGRATION_URL = os.environ.get(
    "WS4_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove",
)


def _ctx(tenant: str) -> ActiveTenantContext:
    return ActiveTenantContext(tenant_id=tenant, principal=Principal(principal_id="obs-user", kind=PrincipalKind.HUMAN))


async def _seed(tenant: str, run_id: uuid.UUID, status: str, revision: int) -> None:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO membership (tenant_id, principal_id, principal_kind, user_ref, roles, active) "
                "VALUES (:t, 'obs-user', 'human', 'obs-user', '[\"execution.query\"]'::jsonb, true) "
                "ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'obs-user', 'human') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) VALUES (:t, 'obs-worker', 'workload', "
                "'obs', '[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
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
                "VALUES (:t, :rid, :sid, :dig, 'obs-user', 'human', :ssh, :ssr, "
                "'b', :rbh, :status, :rev)"
            ),
            {
                "t": tenant,
                "rid": run_id,
                "sid": uuid.uuid4(),
                "dig": run_id.hex.ljust(64, "0")[:64],
                "ssh": "b" * 64,
                "ssr": "execution-spec:" + "b" * 64,
                "rbh": "a" * 64,
                "status": status,
                "rev": revision,
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
            {
                "eid": eid,
                "rs": run_seq,
                "t": tenant,
                "rid": run_id,
                "corr": str(run_id),
                "seid": f"{run_id}:{run_seq}",
                "p": json.dumps(payload, sort_keys=True),
                "occ": occurred,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO runtime_event_outbox (tenant_id, run_id, event_id, run_seq, source) "
                "VALUES (:t, :rid, :eid, :rs, 'grove.runtime_worker')"
            ),
            {"t": tenant, "rid": run_id, "eid": eid, "rs": run_seq},
        )
    await engine.dispose()


async def _seed_ui_events(tenant: str, run_id: uuid.UUID, count: int) -> None:
    engine = create_async_engine(MIGRATION_URL)
    projected_at = datetime.now(UTC)
    rows = [
        {
            "tenant": tenant,
            "run_id": run_id,
            "event_id": uuid.uuid4(),
            "seq": seq,
            "correlation_id": str(run_id),
            "payload": json.dumps(
                {
                    "kind": "run_status_changed",
                    "run_id": str(run_id),
                    "status": "succeeded",
                    "run_revision": seq,
                }
            ),
            "source_refs": json.dumps(
                [
                    {
                        "source_kind": "runtime_event",
                        "source_ref": f"runtime-event:{run_id}:{seq}",
                        "source_hash": "a" * 64,
                        "source_seq": seq,
                        "source_schema_ref": "grove.runtime.run-lifecycle.v1",
                    }
                ]
            ),
            "projected_at": projected_at,
        }
        for seq in range(1, count + 1)
    ]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ui_projection_event (tenant_id, target_kind, target_ref, event_id, "
                "projection_seq, contract_version, correlation_id, payload_schema_ref, payload, "
                "source_refs, projected_at) VALUES (:tenant, 'run', :run_id, :event_id, :seq, 'v1', "
                ":correlation_id, 'grove.ui.run-status-changed.v1', CAST(:payload AS jsonb), "
                "CAST(:source_refs AS jsonb), :projected_at)"
            ),
            rows,
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

        async with factory() as session:
            snapshot = await observation.snapshot(session, ctx, run_id)
        assert snapshot.status == "succeeded"
        assert snapshot.completeness == "complete"
        assert snapshot.last_projection_seq == 2

        streamed = []
        async for view_event in observation.stream_ui_events(factory, ctx, run_id, 0):
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

    async def test_same_tenant_different_principal_is_not_authorized(self) -> None:
        tenant = f"api-owner-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed(tenant, run_id, "succeeded", 1)
        await _seed_event(tenant, run_id, 1, "succeeded", 1)
        await ProjectionReconciler(async_sessionmaker(create_async_engine(PROJECTION_URL))).run_once()

        engine = create_async_engine(API_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        other = ActiveTenantContext(
            tenant_id=tenant,
            principal=Principal(principal_id="different-user", kind=PrincipalKind.HUMAN),
        )
        async with factory() as session:
            inspect = await observation.inspect(session, other, run_id)
            events, cursor = await observation.list_runtime_events(session, other, run_id, 0, 10)
            snapshot = await observation.snapshot(session, other, run_id)
        await engine.dispose()

        assert inspect.completeness == "unavailable"
        assert events == []
        assert cursor == 0
        assert snapshot.completeness == "unavailable"

    async def test_snapshot_marks_bounded_truncation_partial(self) -> None:
        tenant = f"api-snapshot-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed(tenant, run_id, "succeeded", 1001)
        await _seed_ui_events(tenant, run_id, 1001)

        engine = create_async_engine(API_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            snapshot = await observation.snapshot(session, _ctx(tenant), run_id)
        await engine.dispose()

        assert snapshot.status == "succeeded"
        assert snapshot.last_projection_seq == 1000
        assert snapshot.applied_event_count == 1000
        assert snapshot.completeness == "partial"

    async def test_stream_reauthorizes_after_live_membership_revocation(self) -> None:
        tenant = f"api-revoke-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _seed(tenant, run_id, "running", 1)
        await _seed_event(tenant, run_id, 1, "running", 1)
        await ProjectionReconciler(async_sessionmaker(create_async_engine(PROJECTION_URL))).run_once()

        engine = create_async_engine(API_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        stream = observation.stream_ui_events(factory, _ctx(tenant), run_id, 0)
        first = await anext(stream)
        assert first.projection_seq == 1

        migration = create_async_engine(MIGRATION_URL)
        async with migration.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE membership SET active = false "
                    "WHERE tenant_id = :t AND principal_id = 'obs-user' AND principal_kind = 'human'"
                ),
                {"t": tenant},
            )
        await migration.dispose()

        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await engine.dispose()
