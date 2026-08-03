from __future__ import annotations

from pathlib import Path

from scripts.check_contract_dependencies import find_violations


def test_contract_spine_has_no_runtime_or_transport_imports() -> None:
    assert find_violations(Path.cwd()) == []
