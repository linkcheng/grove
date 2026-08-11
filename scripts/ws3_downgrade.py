#!/usr/bin/env python3
"""Operational WS-3 downgrade wrapper with a fail-closed live-data preflight."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
from app.build.downgrade_preflight import DowngradePreflightError, check_psycopg_connection

from scripts.migration_report import _psycopg_url

DOWNGRADE_TIMEOUT_SECONDS = 60
_TARGET_PATTERN = re.compile(r"^(?:base|[A-Za-z0-9_.+]+|-\d+)$")


def run_downgrade(root: Path, database_url: str, target: str) -> int:
    if _TARGET_PATTERN.fullmatch(target) is None:
        raise ValueError("downgrade target has an invalid shape")
    with psycopg.connect(_psycopg_url(database_url), connect_timeout=10) as connection:
        check_psycopg_connection(connection)
        connection.rollback()

    child_env = {name: value for name, value in os.environ.items() if not name.startswith("GROVE_")}
    child_env["GROVE_DATABASE_URL"] = database_url
    child_env["GROVE_ROLE"] = "api"
    result = subprocess.run(  # noqa: S603 - interpreter, module and command are fixed
        [sys.executable, "-m", "alembic", "downgrade", target],
        cwd=root,
        env=child_env,
        check=False,
        timeout=DOWNGRADE_TIMEOUT_SECONDS,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        return run_downgrade(args.root.resolve(), args.database_url, args.target)
    except (DowngradePreflightError, OSError, psycopg.Error, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"WS-3 downgrade failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
