from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_has_ws2_tenant_command_relations() -> None:
    database_url = os.environ.get("GROVE_MIGRATION_DATABASE_URL", os.environ["GROVE_DATABASE_URL"])
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert version == "ws3_execution_authority_closure"
            tables = (
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_name <> 'alembic_version' "
                            "AND table_name NOT IN ("
                            "'geography_columns', 'geometry_columns', 'raster_columns', "
                            "'raster_overviews', 'spatial_ref_sys', "
                            "'pg_stat_statements', 'pg_stat_statements_info')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(tables) == {
                "agent_run",
                "checkpoint_blobs",
                "checkpoint_migrations",
                "checkpoint_writes",
                "checkpoints",
                "command_payload",
                "execution_principal",
                "execution_spec",
                "membership",
                "run_command",
                "tenant",
                "workload_principal",
            }
    finally:
        await engine.dispose()
