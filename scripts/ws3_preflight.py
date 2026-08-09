#!/usr/bin/env python3
"""Read-only WS-3 schema preflight for the execution-authority candidate gate."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.build.manifest import (
    WS3_BUSINESS_RELATIONS,
    WS3_SCHEMA_CONTRACT,
    WS3_SCHEMA_CONTRACT_VERSION,
    migration_head,
)

from scripts.migration_report import MigrationReportError, catalog_authority_state, database_state, ws3_database_state


class WS3PreflightError(RuntimeError):
    """Raised when a live database is not the exact workspace WS-3 contract."""


def check(root: Path, database_url: str) -> None:
    """Validate the live head and catalog contract without changing the database."""

    expected_head = migration_head(root)
    if expected_head != "ws3_runtime_worker_delivery":
        raise WS3PreflightError(
            "workspace Alembic head is "
            f"{expected_head!r}; current runtime-worker delivery gate requires "
            "'ws3_runtime_worker_delivery'"
        )
    try:
        actual_head, relations = database_state(database_url)
        if actual_head != expected_head:
            raise WS3PreflightError(
                f"live Alembic head {actual_head!r} does not equal workspace head {expected_head!r}"
            )
        if set(relations) != WS3_BUSINESS_RELATIONS:
            raise WS3PreflightError(
                "live business relation set does not match WS-3 contract: "
                f"expected={sorted(WS3_BUSINESS_RELATIONS)!r}, actual={relations!r}"
            )
        live_schema = ws3_database_state(database_url)
    except MigrationReportError as exc:
        # Keep the preflight boundary stable even when the catalog reader
        # rejects a fail-closed identity/ACL/trigger closure before it can
        # produce a comparable state object.
        raise WS3PreflightError(f"live schema does not match {WS3_SCHEMA_CONTRACT_VERSION}: {exc}") from exc
    if live_schema != WS3_SCHEMA_CONTRACT:
        raise WS3PreflightError(
            f"live schema does not match {WS3_SCHEMA_CONTRACT_VERSION} (columns/constraints/functions/"
            "triggers/policies/RLS/migration rows/ACL are compared exactly)"
        )
    try:
        catalog_authority_state(database_url)
    except MigrationReportError as exc:
        raise WS3PreflightError(f"live catalog authority root does not match external v1 facts: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    try:
        check(args.root.resolve(), args.database_url)
    except (OSError, ValueError, MigrationReportError, WS3PreflightError) as exc:
        print(f"WS-3 schema preflight failed: {exc}")
        return 1
    print("WS-3 schema preflight passed: live head and schema contract match workspace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
