from __future__ import annotations

from typing import Any, cast

import pytest
from app.build import downgrade_preflight
from sqlalchemy.engine import Connection


class _FakeResult:
    def __init__(self, row: tuple[bool, ...]) -> None:
        self._row = row

    def one(self) -> tuple[bool, ...]:
        return self._row


class _FakeConnection:
    def __init__(self, row: tuple[bool, ...]) -> None:
        self._row = row
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> Any:
        self.statements.append(statement)
        return _FakeResult(self._row)


def test_downgrade_preflight_accepts_empty_execution_database() -> None:
    connection = _FakeConnection((False,) * len(downgrade_preflight.INCOMPATIBLE_FACTS))

    downgrade_preflight.check_sqlalchemy_connection(cast(Connection, connection))

    assert connection.statements == [
        downgrade_preflight.PREFLIGHT_LOCK_SQL,
        downgrade_preflight.PREFLIGHT_SQL,
    ]


@pytest.mark.parametrize("fact_index", range(8))
def test_downgrade_preflight_rejects_each_live_execution_fact(fact_index: int) -> None:
    row = [False] * len(downgrade_preflight.INCOMPATIBLE_FACTS)
    row[fact_index] = True
    connection = _FakeConnection(tuple(row))

    with pytest.raises(
        downgrade_preflight.DowngradePreflightError,
        match=rf"^{downgrade_preflight.INCOMPATIBLE_LIVE_DATA_CODE}:.*"
        + downgrade_preflight.INCOMPATIBLE_FACTS[fact_index],
    ):
        downgrade_preflight.check_sqlalchemy_connection(cast(Connection, connection))


def test_alembic_environment_guards_online_and_offline_downgrade() -> None:
    env_source = open("alembic/env.py", encoding="utf-8").read()
    assert "check_sqlalchemy_connection" in env_source
    assert "offline downgrade is prohibited" in env_source


def test_operational_wrapper_is_the_documented_downgrade_entrypoint() -> None:
    wrapper = open("scripts/ws3_downgrade.py", encoding="utf-8").read()
    assert "check_psycopg_connection" in wrapper
    assert "alembic" in wrapper
    assert "downgrade" in wrapper
