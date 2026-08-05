from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine


def _migration_url(api_url: str) -> str:
    return api_url.replace("grove_api:grove_api_ws0", "grove_migration:grove_migration_ws0", 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ws2_rls_hides_other_tenant_rows_for_independent_api_role() -> None:
    """Migration owner seeds identity; a NOSUPERUSER API role sees one tenant only."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get("GROVE_MIGRATION_DATABASE_URL", _migration_url(api_url))
    owner_engine = create_async_engine(migration_url)
    api_engine = create_async_engine(api_url)
    tenant_a = f"it-a-{uuid.uuid4().hex[:16]}"
    tenant_b = f"it-b-{uuid.uuid4().hex[:16]}"
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenant (tenant_id) VALUES (:tenant_id), (:other_id)"),
                {"tenant_id": tenant_a, "other_id": tenant_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO membership (tenant_id, principal_id, user_ref, roles) "
                    "VALUES (:tenant_id, 'user', 'user', '[\"execution.query\"]'::jsonb)"
                ),
                {"tenant_id": tenant_a},
            )
            await connection.execute(
                text(
                    "INSERT INTO workload_principal (tenant_id, principal_id, workload_ref, scopes) "
                    "VALUES (:tenant_id, 'worker', 'worker', '[\"execution.query\"]'::jsonb)"
                ),
                {"tenant_id": tenant_a},
            )
    finally:
        await owner_engine.dispose()

    try:
        async with api_engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_a}
                )
                result = await connection.execute(text("SELECT tenant_id FROM tenant ORDER BY tenant_id"))
                assert {str(value) for value in result.scalars().all()} == {tenant_a}
                with pytest.raises(SQLAlchemyError):
                    await connection.execute(
                        text(
                            "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                            "VALUES (:tenant_id, 'phantom', 'human')"
                        ),
                        {"tenant_id": tenant_a},
                    )
        async with api_engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_a}
                )
                with pytest.raises(SQLAlchemyError):
                    await connection.execute(text("SELECT payload FROM command_payload"))
    finally:
        await api_engine.dispose()

    owner_engine = create_async_engine(migration_url)
    try:
        # Source identity keys are immutable for both principal kinds. Each
        # failed statement runs in its own transaction after the trigger
        # rejects the attempted key rewrite.
        with pytest.raises(SQLAlchemyError):
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE membership SET principal_id = 'renamed' "
                        "WHERE tenant_id = :tenant_id AND principal_id = 'user'"
                    ),
                    {"tenant_id": tenant_a},
                )
        with pytest.raises(SQLAlchemyError):
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE workload_principal SET principal_id = 'renamed' "
                        "WHERE tenant_id = :tenant_id AND principal_id = 'worker'"
                    ),
                    {"tenant_id": tenant_a},
                )
        with pytest.raises(SQLAlchemyError):
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE execution_principal SET principal_id = 'renamed' "
                        "WHERE tenant_id = :tenant_id AND principal_id = 'user'"
                    ),
                    {"tenant_id": tenant_a},
                )
    finally:
        await owner_engine.dispose()

    cleanup_engine = create_async_engine(migration_url)
    try:
        async with cleanup_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM execution_principal WHERE tenant_id IN (:a, :b)"),
                {"a": tenant_a, "b": tenant_b},
            )
            await connection.execute(
                text("DELETE FROM membership WHERE tenant_id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
            )
            await connection.execute(
                text("DELETE FROM workload_principal WHERE tenant_id IN (:a, :b)"),
                {"a": tenant_a, "b": tenant_b},
            )
            await connection.execute(
                text("DELETE FROM tenant WHERE tenant_id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
            )
    finally:
        await cleanup_engine.dispose()
