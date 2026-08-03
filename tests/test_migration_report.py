from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from scripts import migration_report


def test_migration_subprocess_has_a_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []

    def fake_run(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    migration_report.run_migration(tmp_path, "upgrade head")
    assert calls == [migration_report.MIGRATION_TIMEOUT_SECONDS]


def test_migration_report_writes_content_addressed_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(migration_report, "run_migration", lambda _root, _command: None)
    monkeypatch.setattr(migration_report, "database_state", lambda: ("baseline", []))
    monkeypatch.setattr(migration_report, "migration_head", lambda _root: "baseline")
    output = tmp_path / "ci-evidence" / "migrations.json"

    migration_report.write_report(tmp_path, output)

    payload = output.read_bytes()
    report = json.loads(payload)
    assert report["status"] == "completed"
    digest = hashlib.sha256(payload).hexdigest()
    assert (output.parent / "sha256" / digest / output.name).read_bytes() == payload


def test_migration_report_rejects_database_head_not_in_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(migration_report, "run_migration", lambda _root, _command: None)
    monkeypatch.setattr(migration_report, "database_state", lambda: ("stale", []))
    monkeypatch.setattr(migration_report, "migration_head", lambda _root: "baseline")
    with pytest.raises(migration_report.MigrationReportError, match="does not match Alembic graph"):
        migration_report.write_report(tmp_path, tmp_path / "migrations.json")
