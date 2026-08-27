from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import httpx
import pytest
from app.auth.context import _context_from_token
from app.schemas.execution import ExecutionIntent
from app.services.execution import _build_fixture_spec
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine


def _migration_url(api_url: str) -> str:
    return api_url.replace("grove_api:grove_api_ws0", "grove_migration:grove_migration_ws0", 1)


def _body(submission_id: uuid.UUID, question: str = "hello", *, expected: str | None = None) -> dict[str, Any]:
    intent: dict[str, Any] = {
        "intent_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"intent:{submission_id}:{question}")),
        "skill_ref": "fixture.skill@1",
        "input": {"question": question},
        "constraints": {},
    }
    body: dict[str, Any] = {"submission_id": str(submission_id), "intent": intent}
    if expected is not None:
        body["expected_skill_spec_hash"] = expected
    return body


async def _post(client: httpx.AsyncClient, token: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(
        "/api/v1/executions/submit",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_http_postgres_submit_is_idempotent_and_concurrent() -> None:
    api_database_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get("GROVE_MIGRATION_DATABASE_URL", _migration_url(api_database_url))
    api_base_url = os.environ.get("GROVE_API_BASE_URL", "http://127.0.0.1:8000")
    tenant_id = f"it-submit-{uuid.uuid4().hex[:16]}"
    other_tenant_id = f"it-other-{uuid.uuid4().hex[:16]}"
    owner_engine = create_async_engine(migration_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenant (tenant_id) VALUES (:tenant_id), (:other_tenant_id)"),
                {"tenant_id": tenant_id, "other_tenant_id": other_tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO membership (tenant_id, principal_id, user_ref, roles) "
                    "VALUES (:tenant_id, 'human', 'human', '[\"execution.submit\", \"execution.query\"]'::jsonb)"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO membership (tenant_id, principal_id, user_ref, roles) "
                    "VALUES (:tenant_id, 'other', 'other', '[\"execution.query\"]'::jsonb)"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO membership (tenant_id, principal_id, user_ref, roles) "
                    "VALUES (:tenant_id, 'human', 'human', '[\"execution.query\"]'::jsonb)"
                ),
                {"tenant_id": other_tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO workload_principal (tenant_id, principal_id, workload_ref, scopes) "
                    "VALUES (:tenant_id, 'worker', 'worker', '[\"execution.submit\", \"execution.query\"]'::jsonb)"
                ),
                {"tenant_id": tenant_id},
            )
    finally:
        await owner_engine.dispose()

    try:
        async with httpx.AsyncClient(base_url=api_base_url, timeout=15.0) as client:
            submission_id = uuid.uuid4()
            body = _body(submission_id)
            first = await _post(client, f"fixture:{tenant_id}:human", body)
            assert first.status_code == 200, first.text
            first_payload = first.json()
            assert first_payload["code"] == 0, first_payload
            first_data = first_payload["data"]
            assert first_data["status"] == "accepted"
            run_id = first_data["run_id"]
            command_id = first_data["command_id"]
            old_spec_hash = first_data["skill_spec_hash"]

            retry = await _post(client, f"fixture:{tenant_id}:human", body)
            assert retry.status_code == 200, retry.text
            assert retry.json()["data"] == first_data

            different_digest = await _post(client, f"fixture:{tenant_id}:human", _body(submission_id, "different"))
            assert different_digest.status_code == 200
            assert different_digest.json()["code"] == 40901

            changed_plan = await _post(
                client,
                f"fixture:{tenant_id}:human",
                _body(uuid.uuid4(), expected="0" * 64),
            )
            assert changed_plan.status_code == 200
            assert changed_plan.json()["code"] == 40904

            # A retry must consult the committed run before resolving the
            # current authorization context. Shrinking scopes changes a newly
            # resolved spec, but the old submission remains bound to its
            # persisted hash and does not create orphan artifacts.
            shrink_engine = create_async_engine(migration_url)
            try:
                async with shrink_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE membership SET roles = '[\"execution.submit\"]'::jsonb "
                            "WHERE tenant_id = :tenant_id AND principal_id = 'human'"
                        ),
                        {"tenant_id": tenant_id},
                    )
            finally:
                await shrink_engine.dispose()
            current_context = _context_from_token(f"fixture:{tenant_id}:human")
            current_intent = ExecutionIntent.model_validate(body["intent"])
            current_spec_hash = _build_fixture_spec(
                current_context, current_intent, ("execution.submit",)
            ).skill_spec_hash
            assert current_spec_hash != old_spec_hash
            old_expected_retry = await _post(
                client,
                f"fixture:{tenant_id}:human",
                _body(submission_id, expected=old_spec_hash),
            )
            assert old_expected_retry.status_code == 200, old_expected_retry.text
            assert old_expected_retry.json()["data"] == first_data
            current_expected_retry = await _post(
                client,
                f"fixture:{tenant_id}:human",
                _body(submission_id, expected=current_spec_hash),
            )
            assert current_expected_retry.status_code == 200
            assert current_expected_retry.json()["code"] == 40904

            restore_engine = create_async_engine(migration_url)
            try:
                async with restore_engine.begin() as connection:
                    await connection.execute(
                        text(
                            'UPDATE membership SET roles = \'["execution.submit", "execution.query"]\'::jsonb '
                            "WHERE tenant_id = :tenant_id AND principal_id = 'human'"
                        ),
                        {"tenant_id": tenant_id},
                    )
            finally:
                await restore_engine.dispose()

            lock_submission_id = uuid.uuid4()
            lock_engine = create_async_engine(migration_url)
            lock_connection = await lock_engine.connect()
            await lock_connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"grove.ws2.submit:{tenant_id}:{lock_submission_id}"},
            )
            lock_started = asyncio.get_running_loop().time()
            try:
                lock_timeout_response = await _post(
                    client,
                    f"fixture:{tenant_id}:human",
                    _body(lock_submission_id, "lock-timeout"),
                )
            finally:
                await lock_connection.close()
                await lock_engine.dispose()
            lock_elapsed = asyncio.get_running_loop().time() - lock_started
            assert lock_elapsed < 5.0
            assert lock_timeout_response.status_code == 503, lock_timeout_response.text
            lock_timeout_payload = lock_timeout_response.json()
            assert lock_timeout_payload["code"] == 50302
            assert lock_timeout_payload["error_code"] == "DependencyUnavailable"
            assert lock_timeout_payload["retry_after"] == 1
            assert lock_timeout_response.headers["retry-after"] == "1"

            concurrent_body = _body(uuid.uuid4(), "concurrent")
            responses = await asyncio.gather(
                *(_post(client, f"fixture:{tenant_id}:human", concurrent_body) for _ in range(10))
            )
            assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
            run_ids = {response.json()["data"]["run_id"] for response in responses}
            assert len(run_ids) == 1

            workload = await _post(client, f"fixture:{tenant_id}:worker:workload", _body(uuid.uuid4(), "workload"))
            assert workload.status_code == 200, workload.text
            assert workload.json()["code"] == 0

            queried_run = await client.get(
                f"/api/v1/executions/runs/{run_id}",
                headers={"Authorization": f"Bearer fixture:{tenant_id}:human"},
            )
            assert queried_run.status_code == 200, queried_run.text
            command_receipt = queried_run.json()["data"]["commands"][0]
            assert "payload_ref" not in command_receipt and "payload_hash" not in command_receipt
            queried_command = await client.get(
                f"/api/v1/executions/commands/{command_id}",
                headers={"Authorization": f"Bearer fixture:{tenant_id}:human"},
            )
            assert queried_command.status_code == 200, queried_command.text
            assert "payload_ref" not in queried_command.json()["data"]

            same_tenant_other_principal = await client.get(
                f"/api/v1/executions/runs/{run_id}",
                headers={"Authorization": f"Bearer fixture:{tenant_id}:other"},
            )
            assert same_tenant_other_principal.status_code == 200
            assert same_tenant_other_principal.json()["error_code"] == "RunNotFound"
            cross_tenant = await client.get(
                f"/api/v1/executions/runs/{run_id}",
                headers={"Authorization": f"Bearer fixture:{other_tenant_id}:human"},
            )
            assert cross_tenant.status_code == 200
            assert cross_tenant.json()["error_code"] == "RunNotFound"
            missing = await client.get(
                f"/api/v1/executions/runs/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer fixture:{tenant_id}:human"},
            )
            assert missing.status_code == 200
            assert missing.json()["error_code"] == "RunNotFound"

            # Revocation is read from the tenant database on every request;
            # both submit and query fail closed after the membership is
            # deactivated.
            revoke_engine = create_async_engine(migration_url)
            try:
                async with revoke_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE membership SET active = FALSE "
                            "WHERE tenant_id = :tenant_id AND principal_id = 'human'"
                        ),
                        {"tenant_id": tenant_id},
                    )
            finally:
                await revoke_engine.dispose()
            revoked_query = await client.get(
                f"/api/v1/executions/runs/{run_id}",
                headers={"Authorization": f"Bearer fixture:{tenant_id}:human"},
            )
            assert revoked_query.status_code == 200
            assert revoked_query.json()["error_code"] == "PermissionDenied"
            revoked_submit = await _post(client, f"fixture:{tenant_id}:human", _body(uuid.uuid4(), "revoked"))
            assert revoked_submit.status_code == 200
            assert revoked_submit.json()["error_code"] == "PermissionDenied"

        # Tenant artifacts are retained rather than cascaded through the
        # immutable guards. Deleting a tenant with runs/specs/payloads is
        # therefore an explicit integrity failure.
        restrict_engine = create_async_engine(migration_url)
        try:
            with pytest.raises(SQLAlchemyError):
                async with restrict_engine.begin() as connection:
                    await connection.execute(
                        text("DELETE FROM tenant WHERE tenant_id = :tenant_id"), {"tenant_id": tenant_id}
                    )
        finally:
            await restrict_engine.dispose()

        stats_engine = create_async_engine(migration_url)
        try:
            async with stats_engine.begin() as connection:
                orphan_spec = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM execution_spec s "
                            "LEFT JOIN agent_run r ON r.tenant_id = s.tenant_id "
                            "AND r.skill_spec_hash = s.skill_spec_hash "
                            "AND r.skill_spec_ref = s.spec_ref "
                            "WHERE s.tenant_id = :tenant_id AND r.run_id IS NULL"
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).scalar_one()
                orphan_payload = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM command_payload p "
                            "LEFT JOIN run_command c ON c.tenant_id = p.tenant_id "
                            "AND c.payload_ref = p.payload_ref "
                            "AND c.payload_hash = p.payload_hash "
                            "AND c.command_schema_version = p.command_schema_version "
                            "WHERE p.tenant_id = :tenant_id AND c.command_id IS NULL"
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).scalar_one()
                assert orphan_spec == 0
                assert orphan_payload == 0
        finally:
            await stats_engine.dispose()
    finally:
        cleanup_engine = create_async_engine(migration_url)
        try:
            async with cleanup_engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE TABLE run_command, agent_run, command_payload, execution_spec, "
                        "execution_principal, membership, workload_principal, "
                        "asset_risk_asset_state, tenant"
                    )
                )
        finally:
            await cleanup_engine.dispose()
