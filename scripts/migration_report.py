#!/usr/bin/env python3
"""Run the real migration round trip and write deterministic evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import psycopg
from app.build.manifest import migration_hash, migration_head, write_content_addressed_artifact
from app.core.config import load_settings

ROUND_TRIP = ("upgrade head", "downgrade base", "upgrade head")
MIGRATION_TIMEOUT_SECONDS = 30


class MigrationReportError(RuntimeError):
    """Raised when live database evidence does not match the baseline contract."""


def run_migration(root: Path, command: str) -> None:
    """Run one Alembic command against the configured database."""

    revision, target = command.split(" ", maxsplit=1)
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", revision, target],
        cwd=root,
        check=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
    )


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def database_state() -> tuple[str, list[str]]:
    """Read the applied Alembic head and non-extension public relations."""

    settings = load_settings()
    with psycopg.connect(_psycopg_url(settings.database_url_value()), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
            heads = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT c.relname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND c.relname <> 'alembic_version'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend AS d
                      JOIN pg_extension AS e ON e.oid = d.refobjid
                      WHERE d.classid = 'pg_class'::regclass
                        AND d.objid = c.oid
                        AND d.deptype = 'e'
                  )
                ORDER BY c.relname
                """
            )
            business_tables = [str(row[0]) for row in cursor.fetchall()]
    if len(heads) != 1:
        raise MigrationReportError(f"expected one Alembic head, found {heads!r}")
    return heads[0], business_tables


def write_report(root: Path, output: Path) -> None:
    for command in ROUND_TRIP:
        run_migration(root, command)
    head, business_tables = database_state()
    expected_head = migration_head(root)
    if head != expected_head:
        raise MigrationReportError(f"database head {head!r} does not match Alembic graph head {expected_head!r}")
    if business_tables:
        raise MigrationReportError(f"unexpected non-infrastructure relations: {business_tables!r}")
    report = {
        "head": head,
        "migration_hash": migration_hash(root),
        "business_tables": business_tables,
        "round_trip": list(ROUND_TRIP),
        "status": "completed",
    }
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    write_content_addressed_artifact(output, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("ci-evidence/migrations.json"))
    args = parser.parse_args()
    try:
        write_report(args.root.resolve(), args.output)
    except (
        MigrationReportError,
        OSError,
        psycopg.Error,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
