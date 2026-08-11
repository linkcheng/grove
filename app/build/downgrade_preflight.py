"""Fail-closed WS-3 live-data guard for PostgreSQL schema downgrade."""

from __future__ import annotations

from typing import Any

import psycopg
from sqlalchemy.engine import Connection

INCOMPATIBLE_LIVE_DATA_CODE = "WS3_DOWNGRADE_INCOMPATIBLE_LIVE_DATA"
INCOMPATIBLE_FACTS = (
    "command_status_not_pending",
    "command_sequence_nonzero",
    "command_claim_or_retry_state",
    "run_status_not_accepted",
    "run_claim_state",
    "runtime_build_binding",
    "checkpoint_state",
    "command_provenance_state",
)

# The lock is acquired inside the same Alembic transaction that performs the
# downgrade.  It prevents a writer from creating an incompatible fact after
# the read-only preflight but before the first DDL statement.
PREFLIGHT_LOCK_SQL = """
LOCK TABLE public.agent_run, public.run_command, public.checkpoints,
           public.checkpoint_blobs, public.checkpoint_writes
IN SHARE ROW EXCLUSIVE MODE
"""

PREFLIGHT_SQL = """
SELECT
    EXISTS (SELECT 1 FROM public.run_command WHERE status <> 'pending'),
    EXISTS (SELECT 1 FROM public.run_command WHERE command_seq <> 0),
    EXISTS (
        SELECT 1 FROM public.run_command
         WHERE lease_owner IS NOT NULL
            OR lease_until IS NOT NULL
            OR execution_fence IS NOT NULL
            OR attempt_count <> 0
            OR last_error_ref IS NOT NULL
    ),
    EXISTS (SELECT 1 FROM public.agent_run WHERE status <> 'accepted'),
    EXISTS (
        SELECT 1 FROM public.agent_run
         WHERE lease_owner IS NOT NULL
            OR lease_until IS NOT NULL
            OR execution_fence <> 0
    ),
    EXISTS (
        SELECT 1 FROM public.agent_run
         WHERE runtime_build_ref IS NOT NULL
            OR runtime_build_hash IS NOT NULL
    ),
    EXISTS (SELECT 1 FROM public.checkpoints)
        OR EXISTS (SELECT 1 FROM public.checkpoint_blobs)
        OR EXISTS (SELECT 1 FROM public.checkpoint_writes),
    EXISTS (
        SELECT 1 FROM public.run_command
         WHERE consumed_provenance_kind IS NOT NULL
            OR consumed_worker_id IS NOT NULL
            OR consumed_execution_fence IS NOT NULL
            OR consumed_lease_until IS NOT NULL
            OR consumed_claim_provenance_hash IS NOT NULL
            OR superseded_by_command_id IS NOT NULL
            OR superseded_by_provenance_hash IS NOT NULL
    )
"""


class DowngradePreflightError(RuntimeError):
    """Raised before DDL when a live database cannot be downgraded safely."""


def _check_row(row: object) -> None:
    if not isinstance(row, (tuple, list)) or len(row) != len(INCOMPATIBLE_FACTS):
        raise DowngradePreflightError(f"{INCOMPATIBLE_LIVE_DATA_CODE}: preflight_result_invalid")
    if any(type(value) is not bool for value in row):
        raise DowngradePreflightError(f"{INCOMPATIBLE_LIVE_DATA_CODE}: preflight_result_invalid")
    incompatible = [name for name, present in zip(INCOMPATIBLE_FACTS, row, strict=True) if present]
    if incompatible:
        raise DowngradePreflightError(f"{INCOMPATIBLE_LIVE_DATA_CODE}: incompatible_facts={','.join(incompatible)}")


def check_sqlalchemy_connection(connection: Connection) -> None:
    """Check and lock a live database inside the caller's Alembic transaction."""

    connection.exec_driver_sql(PREFLIGHT_LOCK_SQL)
    row = connection.exec_driver_sql(PREFLIGHT_SQL).one()
    _check_row(tuple(row))


def check_psycopg_connection(connection: psycopg.Connection[Any]) -> None:
    """Run the same check through the operational wrapper's psycopg adapter."""

    with connection.cursor() as cursor:
        cursor.execute(PREFLIGHT_LOCK_SQL)
        cursor.execute(PREFLIGHT_SQL)
        row = cursor.fetchone()
    _check_row(row)


__all__ = [
    "INCOMPATIBLE_FACTS",
    "INCOMPATIBLE_LIVE_DATA_CODE",
    "PREFLIGHT_LOCK_SQL",
    "PREFLIGHT_SQL",
    "DowngradePreflightError",
    "check_psycopg_connection",
    "check_sqlalchemy_connection",
]
