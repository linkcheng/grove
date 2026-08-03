from __future__ import annotations

from pathlib import Path


def test_initial_migration_is_infrastructure_only() -> None:
    revisions = list(Path("alembic/versions").glob("*.py"))
    assert revisions
    content = "\n".join(path.read_text() for path in revisions)
    assert "create_table" not in content
    assert "baseline" in content
