"""WS-6 F.1: the G3 vertical loop over the AssetRisk profile (gated).

Gateway-authenticated submit -> real worker executing the asset-risk kernel
(knowledge snapshot, RLS asset read, real provider inference, typed report,
checkpoint) -> projection of the domain-view fact -> the frozen
RunInteractionModel consuming the real projection stream through the
Profile renderer -> safe Run Inspect.  Everything skips without the issued
release chain and the real gateway credential; a pass cannot be claimed
without the gate.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from app.asset_risk.rendering import ASSET_STATE_VIEW_SCHEMA_REF, asset_risk_renderer_registry
from app.contracts.canonical import CanonicalModel
from app.execution import PostgresExecutionDriver
from app.observation.facts import UI_PROJECTION_SCHEMA_REF
from app.observation.interaction_model import RunInteractionModel, SnapshotBundle
from app.observation.projection import ProjectionReconciler
from app.observation.reducer import RunViewState, reduce_run_view
from app.worker.inference import production_inference_lifespan
from app.worker.loop import RuntimeWorker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

RUNTIME_URL = os.environ.get(
    "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
)
MIGRATION_URL = os.environ.get(
    "GROVE_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove",
)
API_URL = os.environ.get("GROVE_DATABASE_URL", "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove")
BUILD_HASH = "e" * 64
# Test-only shared-secret fixture for the gateway trust boundary; it gates no
# real credential (the provider credential stays in the issued release chain).
GATEWAY_TOKEN = "g3-gateway-shared-secret-0123456789"  # noqa: S105

GATEWAY_HEADERS = {
    "x-grove-gateway-auth": GATEWAY_TOKEN,
    "x-grove-principal-kind": "workload",
}


def _release_chain_configured() -> bool:
    return all(
        os.environ.get(name)
        for name in (
            "AI_GATEWAY_RELEASE_AUTHORITY_DIR",
            "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
            "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH",
            "AI_GATEWAY_RELEASE_SIGNATURE_PATH",
            "AI_GATEWAY_PROVIDER_MANIFEST_PATH",
            "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256",
            "AI_GATEWAY_RELEASE_POLICY_REF",
            "AI_GATEWAY_RELEASE_POLICY_VERSION",
            "AI_GATEWAY_RELEASE_POLICY_SHA256",
        )
    )


async def _seed_tenant_and_assets(tenant: str, asset_refs: tuple[str, ...]) -> None:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) VALUES (:t, 'g3-portal', 'workload', 'g3-e2e', "
                '\'["execution.submit", "execution.query"]\'::jsonb, true) ON CONFLICT DO NOTHING'
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'g3-portal', 'workload') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        for ref in asset_refs:
            await conn.execute(
                text(
                    "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                    "currency, status, source_revision) VALUES (:t, :ref, 'credit', 1000, 'CNY', 'active', 'rev-1') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"t": tenant, "ref": ref},
            )
    await engine.dispose()


def _build_gateway_app() -> Any:
    from app.core.config import Role, Settings
    from app.main import create_app
    from pydantic import SecretStr

    settings = Settings(
        role=Role.API,
        app_env="integration",
        database_url=SecretStr(API_URL),
        auth_mode="gateway",
        gateway_auth_token=SecretStr(GATEWAY_TOKEN),
        fixture_graph_binding="asset_risk",
    )
    return create_app(settings)


async def _submit(client: httpx.AsyncClient, tenant: str) -> dict[str, Any]:
    submission_id = uuid4()
    intent = {
        "intent_id": str(uuid4()),
        "skill_ref": "fixture.skill@1",
        "input": {"question": "g3 vertical loop"},
        "constraints": {},
    }
    response = await client.post(
        "/api/v1/executions/submit",
        headers={**GATEWAY_HEADERS, "x-grove-tenant-id": tenant, "x-grove-principal-id": "g3-portal"},
        json={"submission_id": str(submission_id), "intent": intent},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0, payload
    data: dict[str, Any] = payload["data"]
    return data


async def _drive_projection(tenant: str) -> None:
    projection_url = os.environ.get(
        "WS4_PROJECTION_DATABASE_URL",
        "postgresql+psycopg://grove_projection:grove_projection_ws0@127.0.0.1:54329/grove",
    )
    engine = create_async_engine(projection_url)
    reconciler = ProjectionReconciler(async_sessionmaker(engine, expire_on_commit=False))
    try:
        for _ in range(50):
            projected = await reconciler.run_once()
            if projected == 0 and (await reconciler.health())["backlog"] == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        await engine.dispose()


async def _fetch_ui_bundle(client: httpx.AsyncClient, tenant: str, run_id: UUID) -> SnapshotBundle:
    """Load the snapshot + full stream exactly as the frontend adapter does."""

    headers = {**GATEWAY_HEADERS, "x-grove-tenant-id": tenant, "x-grove-principal-id": "g3-portal"}
    snapshot_response = await client.get(f"/api/v1/observations/runs/{run_id}/ui/snapshot", headers=headers)
    assert snapshot_response.status_code == 200, snapshot_response.text
    view = RunViewState.model_validate(snapshot_response.json()["data"])
    events_response = await client.get(
        f"/api/v1/observations/runs/{run_id}/ui", headers=headers, params={"after_projection_seq": 0, "limit": 200}
    )
    assert events_response.status_code == 200, events_response.text
    raw_events = events_response.json()["data"]["events"]
    events: list[Any] = [_ui_event_from_view(item, tenant) for item in raw_events]
    return SnapshotBundle(view=view, events=tuple(events))


def _ui_event_from_view(item: dict[str, Any], tenant: str) -> Any:
    from app.contracts.canonical import ContractMeta, ProjectionSourceRef, UIProjectionEvent
    from app.observation.facts import ui_payload_adapter

    return UIProjectionEvent[CanonicalModel](
        meta=ContractMeta(
            contract_name="ui.projection",
            contract_version="v1",
            message_id=UUID(item["event_id"]),
            tenant_id=tenant,
            correlation_id=f"g3:{item['event_id']}",
        ),
        event_id=UUID(item["event_id"]),
        target_kind="run",
        target_ref=UUID(item["target_ref"]),
        projection_seq=int(item["projection_seq"]),
        payload_schema_ref=item["payload_schema_ref"],
        payload=ui_payload_adapter().validate_python(item["payload"]),
        source_refs=(
            ProjectionSourceRef(
                source_kind="runtime_event",
                source_ref=f"g3:{item['event_id']}",
                source_hash="0" * 64,
                source_seq=int(item["projection_seq"]),
                source_schema_ref=UI_PROJECTION_SCHEMA_REF,
            ),
        ),
        projected_at=datetime.fromisoformat(str(item["projected_at"])),
    )


@pytest.mark.asyncio
async def test_g3_vertical_loop_from_gateway_submit_to_inspect() -> None:
    if os.environ.get("GROVE_RUN_PROVIDER_G3") != "1" or not _release_chain_configured():
        pytest.skip("set GROVE_RUN_PROVIDER_G3=1 with the issued release chain and gateway env")

    from app.asset_risk.composition import compose_asset_risk_kernel

    tenant = f"ws6-g3-{uuid4().hex[:8]}"
    run_marker = uuid4().hex[:8]
    asset_refs = (f"asset.{run_marker}a", f"asset.{run_marker}b")
    await _seed_tenant_and_assets(tenant, asset_refs)

    app = _build_gateway_app()
    transport = httpx.ASGITransport(app=app)
    # ASGITransport does not run the app lifespan; enter it explicitly so the
    # engines/session factories exist exactly as in the deployed process.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://g3-test") as client:
            # Unauthenticated and credential-combining requests fail closed first.
            anonymous = await client.post(
                "/api/v1/executions/submit",
                headers={"x-grove-tenant-id": tenant, "x-grove-principal-id": "g3-portal"},
                json={"submission_id": str(uuid4()), "intent": {}},
            )
            assert anonymous.status_code == 401
            combined = await client.post(
                "/api/v1/executions/submit",
                headers={
                    **GATEWAY_HEADERS,
                    "x-grove-tenant-id": tenant,
                    "x-grove-principal-id": "g3-portal",
                    "authorization": "Bearer fixture:x:y",
                },
                json={"submission_id": str(uuid4()), "intent": {}},
            )
            assert combined.status_code == 401

            submitted = await _submit(client, tenant)
            assert submitted["status"] == "accepted"
            run_id = UUID(submitted["run_id"])

        # The submitted run carries the fixture release bundle's runtime
        # build hash; the worker and its claim must match that authority,
        # while the inference lifespan keeps the issued chain's build hash.
        seed_engine = create_async_engine(MIGRATION_URL)
        async with seed_engine.begin() as conn:
            run_build_hash = (
                await conn.execute(
                    text("SELECT runtime_build_hash FROM agent_run WHERE tenant_id = :t AND run_id = :r"),
                    {"t": tenant, "r": run_id},
                )
            ).scalar_one()
        await seed_engine.dispose()
        assert run_build_hash

    runtime_engine = create_async_engine(RUNTIME_URL)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "test"),
            runtime_build_hash=BUILD_HASH,
        ) as (port, request_factory):
            kernel = compose_asset_risk_kernel(
                inference_port=port,
                inference_request_factory=request_factory,
                runtime_session_factory=async_sessionmaker(runtime_engine, expire_on_commit=False),
                worker_tenant_id=tenant,
            )
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=tenant,
                worker_id="g3-e2e-worker",
                runtime_build_hash=run_build_hash,
                database_url=RUNTIME_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
                asset_risk_kernel=kernel,
                poll_interval=0.01,
                # The driver caps leases at 300s (WS-7 raised the 90s cap
                # because real generation regularly exceeded it) and
                # invoke+checkpoint is the unsplittable critical section, so
                # real generation must fit inside the lease minus a margin.
                # The runtime gate may retry the answer up to
                # 1 + max_schema_retries times (flash chain: 3 attempts,
                # each a full generation), so the invoke budget spans the
                # worst case: 3 x ~60s + checkpoint overhead = 200s; the
                # lease keeps a 10s margin above it. Per-request deadline
                # stays 80s from the issued chain.
                invoke_budget_seconds=200.0,
            )
            claim = await driver.claim(
                worker_id="g3-e2e-worker", runtime_build_hash=run_build_hash, tenant_id=tenant, lease_seconds=240.0
            )
            assert claim is not None
            assert claim.graph_binding.graph_ref == "graph.asset-risk@1"
            await worker._process_claim(claim)

        status_engine = create_async_engine(MIGRATION_URL)
        async with status_engine.begin() as conn:
            status = (
                await conn.execute(
                    text("SELECT status FROM agent_run WHERE tenant_id = :t AND run_id = :r"),
                    {"t": tenant, "r": run_id},
                )
            ).scalar_one()
            domain_view_facts = (
                await conn.execute(
                    text(
                        "SELECT payload FROM runtime_event WHERE tenant_id = :t AND run_id = :r "
                        "AND payload_schema_ref = 'grove.runtime.domain-view-accepted.v1'"
                    ),
                    {"t": tenant, "r": run_id},
                )
            ).fetchall()
        await status_engine.dispose()
        assert status == "succeeded"
        assert len(domain_view_facts) == 1

        await _drive_projection(tenant)

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://g3-test") as client:
                bundle = await _fetch_ui_bundle(client, tenant, run_id)
                assert reduce_run_view(bundle.events) == bundle.view  # replay-stable

                async def load_snapshot() -> SnapshotBundle:
                    return bundle

                async def load_batch(after_seq: int, limit: int) -> list[Any]:
                    del after_seq, limit
                    return []

                from app.observation.interaction_model import RunIntentDispatchResult

                async def dispatch(intent: object) -> RunIntentDispatchResult:
                    del intent
                    return RunIntentDispatchResult(outcome="rejected", error_code="not_used_in_g3")

                model = RunInteractionModel(
                    snapshot_loader=load_snapshot,
                    batch_loader=load_batch,
                    intent_dispatcher=dispatch,
                )
                snapshot = await model.open()
                view = snapshot.view
                assert view.status == "succeeded"
                assert view.completeness == "complete"
                assert len(view.domain_views) == 1
                milestone = view.domain_views[0]
                assert milestone.view_schema_ref == ASSET_STATE_VIEW_SCHEMA_REF
                assert milestone.item_count == len(asset_refs)

                rendered = asset_risk_renderer_registry().render(milestone)
                assert rendered.kind == "rendered"
                assert rendered.title == "资产状态已固定"
                field_kinds = [field.kind for field in rendered.fields]
                assert field_kinds == ["observed_at", "item_count", "completeness", "provenance"]

                inspect_response = await client.get(
                    f"/api/v1/observations/runs/{run_id}/inspect",
                    headers={**GATEWAY_HEADERS, "x-grove-tenant-id": tenant, "x-grove-principal-id": "g3-portal"},
                )
                assert inspect_response.status_code == 200, inspect_response.text
                inspected = inspect_response.json()["data"]
                assert inspected["run_id"] == str(run_id)
                assert inspected["status"] == "succeeded"
    finally:
        await runtime_engine.dispose()
