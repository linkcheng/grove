from __future__ import annotations

from pathlib import Path

import pytest
from app.build.manifest import WS3_BUSINESS_RELATIONS, WS3_SCHEMA_CONTRACT, WS4_BUSINESS_RELATIONS
from scripts import ws3_preflight
from scripts.migration_report import MigrationReportError


def test_preflight_rejects_workspace_head_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws3_preflight, "migration_head", lambda _root: "ws3_execution_driver")
    with pytest.raises(ws3_preflight.WS3PreflightError, match="delivery gate requires"):
        ws3_preflight.check(tmp_path, "postgresql://unused")


def test_preflight_rejects_live_head_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws3_preflight, "migration_head", lambda _root: "ws3_runtime_worker_delivery")
    monkeypatch.setattr(ws3_preflight, "database_state", lambda _url: ("ws3_execution_driver", []))
    with pytest.raises(ws3_preflight.WS3PreflightError, match="live Alembic head"):
        ws3_preflight.check(tmp_path, "postgresql://unused")


def test_preflight_rejects_live_schema_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws3_preflight, "migration_head", lambda _root: "ws3_runtime_worker_delivery")
    monkeypatch.setattr(
        ws3_preflight,
        "database_state",
        lambda _url: ("ws3_runtime_worker_delivery", sorted(WS3_BUSINESS_RELATIONS)),
    )
    monkeypatch.setattr(ws3_preflight, "ws3_database_state", lambda _url: {"columns": {"drift": True}})
    with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
        ws3_preflight.check(tmp_path, "postgresql://unused")


def test_preflight_ws4_head_still_reads_live_ws3_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ws3_preflight, "migration_head", lambda _root: "ws4_recon_helpers")
    monkeypatch.setattr(
        ws3_preflight,
        "database_state",
        lambda _url: ("ws4_recon_helpers", sorted(WS4_BUSINESS_RELATIONS)),
    )
    reads = 0

    def read_live_schema(_url: str) -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {**WS3_SCHEMA_CONTRACT, "constraints": {"drift": True}}

    monkeypatch.setattr(ws3_preflight, "ws3_database_state", read_live_schema)
    with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
        ws3_preflight.check(tmp_path, "postgresql://unused")
    assert reads == 1


def test_preflight_normalizes_fail_closed_catalog_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws3_preflight, "migration_head", lambda _root: "ws3_runtime_worker_delivery")
    monkeypatch.setattr(
        ws3_preflight,
        "database_state",
        lambda _url: ("ws3_runtime_worker_delivery", sorted(WS3_BUSINESS_RELATIONS)),
    )
    monkeypatch.setattr(
        ws3_preflight,
        "ws3_database_state",
        lambda _url: (_ for _ in ()).throw(MigrationReportError("catalog closure drift")),
    )
    with pytest.raises(ws3_preflight.WS3PreflightError, match="catalog closure drift"):
        ws3_preflight.check(tmp_path, "postgresql://unused")


def test_preflight_normalizes_database_connection_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws3_preflight, "migration_head", lambda _root: "ws3_runtime_worker_delivery")
    monkeypatch.setattr(
        ws3_preflight,
        "database_state",
        lambda _url: (_ for _ in ()).throw(MigrationReportError("catalog database operation failed")),
    )
    with pytest.raises(ws3_preflight.WS3PreflightError, match="catalog database operation failed"):
        ws3_preflight.check(tmp_path, "postgresql://unused")
