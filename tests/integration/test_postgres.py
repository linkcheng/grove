from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.build.manifest import WS2_BUSINESS_RELATIONS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_has_ws2_tenant_command_relations() -> None:
    database_url = os.environ.get("GROVE_MIGRATION_DATABASE_URL", os.environ["GROVE_DATABASE_URL"])
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            from app.build.manifest import migration_head

            assert version == migration_head(_PROJECT_ROOT)
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
            assert WS2_BUSINESS_RELATIONS <= set(tables)
    finally:
        await engine.dispose()
