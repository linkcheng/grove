"""Round-nine regressions for exact contract-spine import allowlists."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from scripts.check_contract_dependencies import (
    _IMPORT_SYMBOL_ALLOWLISTS,
    _INTERNAL_IMPORT_PREFIXES,
    _matches_prefix,
    find_violations,
)

ImportPair = tuple[str, str | None]


def _external_imports(root: Path, layer: str) -> set[ImportPair]:
    """Collect every external module/symbol pair using the checker boundary."""

    package_root = root / layer
    internal_prefixes = _INTERNAL_IMPORT_PREFIXES[layer]
    imports: set[ImportPair] = set()
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _matches_prefix(alias.name, internal_prefixes):
                        imports.add((alias.name, None))
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level > 0:
                # Relative imports are checker violations, so retain a
                # comparable sentinel rather than silently dropping them.
                relative_module = node.module or "<none>"
                imports.update((f"<relative:{node.level}:{relative_module}>", alias.name) for alias in node.names)
                continue
            module = node.module
            if module is None:
                # ``ast.parse`` cannot produce this shape for an absolute
                # import, but synthetic trees should not become external.
                continue
            if module == "__future__":
                continue
            if _matches_prefix(module, internal_prefixes):
                continue
            imports.update((module, alias.name) for alias in node.names)
    return imports


def _allowlist_imports(layer: str) -> set[ImportPair]:
    """Flatten the checker-owned allowlist without copying its symbols."""

    return {
        (module, symbol)
        for module, symbols in _IMPORT_SYMBOL_ALLOWLISTS[layer].items()
        if module != "__future__"
        for symbol in symbols
    }


def _assert_exact_imports(layer: str, actual: set[ImportPair], allowed: set[ImportPair]) -> None:
    missing = sorted(allowed - actual, key=repr)
    unexpected = sorted(actual - allowed, key=repr)
    assert not missing and not unexpected, f"{layer}: missing={missing!r}, unexpected={unexpected!r}"


@pytest.mark.parametrize("layer", tuple(_IMPORT_SYMBOL_ALLOWLISTS))
def test_contract_spine_external_imports_match_checker_allowlist(layer: str) -> None:
    """Keep production imports and the checker allowlist equal in both directions."""

    _assert_exact_imports(layer, _external_imports(Path.cwd(), layer), _allowlist_imports(layer))


def test_external_imports_counts_all_relative_imports_and_checker_rejects_them(tmp_path: Path) -> None:
    """Relative imports must not disappear before the checker sees them."""

    package = tmp_path / "app" / "contracts"
    package.mkdir(parents=True)
    (package / "probe.py").write_text(
        """from . import x
from .app.contracts import x
from .__future__ import x
from ..foo import x
from __future__ import annotations
from app.contracts import canonical
""",
        encoding="utf-8",
    )

    actual = _external_imports(tmp_path, "app/contracts")
    relative = {
        ("<relative:1:<none>>", "x"),
        ("<relative:1:app.contracts>", "x"),
        ("<relative:1:__future__>", "x"),
        ("<relative:2:foo>", "x"),
    }

    assert actual == relative
    assert ("__future__", "annotations") not in actual
    assert ("app.contracts", "canonical") not in actual
    assert actual != _allowlist_imports("app/contracts")
    with pytest.raises(AssertionError):
        _assert_exact_imports("app/contracts", actual, _allowlist_imports("app/contracts"))

    violations = find_violations(tmp_path)
    assert len(violations) == 4
    assert all("relative import" in violation for violation in violations)
