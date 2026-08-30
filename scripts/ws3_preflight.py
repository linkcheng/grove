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
    # WS-3 preflight verifies the WS-3 authority contract.  WS-4 adds
    # observation tables/functions outside WS-3 authority scope and does not
    # modify WS-3 objects, so the same contract check applies; the extra
    # observation relations are verified separately by ws-4-check.
    _WS3_PREFLIGHT_HEADS = {
        "ws3_runtime_worker_delivery",
        "ws4_observation_slice",
        "ws4_recon_helpers",
        "ws4_authority_audit_emitters",
        "ws3_consumed_provenance_compat",
        "ws6_claim_graph_binding",
        "ws6_asset_risk_state",
        "ws6_domain_view_runtime_event",
        "ws7_lease_cap_300",
        "ws7_message_emit_allowlist",
    }
    if expected_head not in _WS3_PREFLIGHT_HEADS:
        raise WS3PreflightError(
            "workspace Alembic head is "
            f"{expected_head!r}; current runtime-worker delivery gate requires "
            f"one of {sorted(_WS3_PREFLIGHT_HEADS)!r}"
        )
    try:
        actual_head, relations = database_state(database_url)
        if actual_head != expected_head:
            raise WS3PreflightError(
                f"live Alembic head {actual_head!r} does not equal workspace head {expected_head!r}"
            )
        # WS-4 heads add observation relations; verify WS-3 authority relations
        # are present (subset) rather than exact-matching the full open-world set.
        missing = WS3_BUSINESS_RELATIONS - set(relations)
        if missing:
            raise WS3PreflightError(
                f"live business relation set is missing WS-3 authority relations: missing={sorted(missing)!r}"
            )
        # Project the live catalog onto the finite WS-3 authority surface even
        # when later migrations add unrelated objects.  A frozen expected value
        # cannot prove that the live WS-3 objects still satisfy the contract.
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
    if expected_head == "ws3_runtime_worker_delivery":
        # Catalog authority closure is a WS-3 G0 build-evidence tool that
        # exact-matches the open-world public catalog.  Skip for WS-4 heads
        # (observation tables legitimately extend the catalog).
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
