#!/usr/bin/env python3
"""Fail closed when the WS-1 contract spine imports a runtime boundary.

The checker deliberately uses a closed AST rule set.  It does not attempt to
prove that a string is or is not dynamically generated: a reflection primitive
or one of its aliases is a violation wherever it appears in the spine.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SPINE_ROOTS = ("app/contracts", "app/skill_abi")

# External imports are closed at both the module and symbol level.  An
# ``import hashlib`` would hand the contract spine a module object whose
# namespace includes more than the one operation it needs; only the listed
# symbols may enter the spine.  Internal app imports are handled separately by
# ``_INTERNAL_IMPORT_PREFIXES`` below because their module path is the boundary
# that carries the layer guarantee.
_IMPORT_SYMBOL_ALLOWLISTS: dict[str, dict[str, frozenset[str]]] = {
    "app/contracts": {
        "__future__": frozenset({"annotations"}),
        "collections.abc": frozenset({"Mapping"}),
        "dataclasses": frozenset({"dataclass"}),
        "datetime": frozenset({"UTC", "datetime"}),
        "enum": frozenset({"Enum", "StrEnum"}),
        "hashlib": frozenset({"sha256"}),
        "json": frozenset({"JSONDecodeError", "dumps", "loads"}),
        "math": frozenset({"isfinite"}),
        "pydantic": frozenset(
            {"BaseModel", "ConfigDict", "Field", "TypeAdapter", "field_validator", "model_validator"}
        ),
        "pydantic_core": frozenset({"SchemaValidator"}),
        "re": frozenset({"fullmatch"}),
        "types": frozenset({"UnionType"}),
        "typing": frozenset(
            {"Annotated", "Any", "Generic", "Literal", "TypeVar", "Union", "cast", "get_args", "get_origin"}
        ),
        "uuid": frozenset({"UUID", "uuid4"}),
    },
    "app/skill_abi": {
        "__future__": frozenset({"annotations"}),
        "collections.abc": frozenset({"Callable", "Mapping", "Sequence"}),
        "dataclasses": frozenset({"dataclass"}),
        "datetime": frozenset({"datetime"}),
        "enum": frozenset({"StrEnum"}),
        "hashlib": frozenset({"sha256"}),
        "inspect": frozenset({"isawaitable"}),
        "pydantic": frozenset({"AliasChoices", "BaseModel", "Field", "field_validator", "model_validator"}),
        "re": frozenset({"fullmatch"}),
        "typing": frozenset({"Any", "Literal"}),
        "uuid": frozenset({"UUID"}),
    },
}
_INTERNAL_IMPORT_PREFIXES = {
    "app/contracts": ("app.contracts",),
    "app/skill_abi": ("app.contracts", "app.skill_abi"),
}
_DYNAMIC_MODULES = frozenset({"builtins", "importlib"})
# Attribute names that expose execution frames, code objects, traceback links,
# or the object graph behind functions/classes.  The checker treats the set as
# closed: an occurrence is a violation even when it is reached through an
# alias or appears inside a longer Attribute/Subscript chain.  We do not try
# to evaluate computed strings because the attribute itself is the escape.
_EXECUTION_FRAME_ATTRIBUTES = frozenset(
    {
        # generator objects
        "gi_frame",
        "gi_code",
        "gi_yieldfrom",
        "gi_running",
        "gi_suspended",
        # coroutine objects
        "cr_frame",
        "cr_code",
        "cr_await",
        "cr_origin",
        "cr_running",
        "cr_suspended",
        # asynchronous generator objects
        "ag_frame",
        "ag_code",
        "ag_await",
        "ag_running",
        # traceback objects
        "tb_frame",
        "tb_next",
        "tb_lasti",
        "tb_lineno",
        "__traceback__",
        "with_traceback",
        # frame objects
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_locals",
        "f_trace",
        "f_trace_lines",
        "f_trace_opcodes",
        # function/class/object graph escapes
        "__globals__",
        "__builtins__",
        "__closure__",
        "__code__",
        "__getattribute__",
        "__dict__",
        "__class__",
        "__base__",
        "__bases__",
        "__mro__",
        "__subclasses__",
        "__reduce__",
        "__reduce_ex__",
        "__module__",
    }
)
_REFLECTION_NAMES = (
    frozenset(
        {
            "__builtins__",
            "__import__",
            "eval",
            "exec",
            "compile",
            "vars",
            "globals",
            "locals",
            "getattr",
            "hasattr",
            "setattr",
            "delattr",
            "__getattribute__",
            "__dict__",
            "__globals__",
            "__module__",
        }
    )
    | _EXECUTION_FRAME_ATTRIBUTES
)
# These are the common operator helpers that turn ordinary indexing/attribute
# access into an indirect reflection seam.  Importing the ``operator`` module
# itself is also forbidden below, so both direct and aliased forms are closed.
_OPERATOR_HELPERS = frozenset({"operator", "getitem", "attrgetter", "itemgetter", "methodcaller"})
_FORBIDDEN_NAMES = _REFLECTION_NAMES | _OPERATOR_HELPERS
_FORBIDDEN_CONSTANTS = _REFLECTION_NAMES


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _import_violation_reason(
    module: str | None,
    symbol_allowlist: dict[str, frozenset[str]],
    internal_prefixes: tuple[str, ...],
    *,
    is_module_import: bool,
) -> str | None:
    if module is None:
        return "relative import"
    if _matches_prefix(module, internal_prefixes):
        return None
    if module in symbol_allowlist:
        # ``import module`` is intentionally forbidden even when the module
        # itself is a safe source for a few named symbols.
        return module if is_module_import else None
    root = module.split(".", 1)[0]
    if root in _DYNAMIC_MODULES or root == "operator":
        return "dynamic import"
    return module


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    return ".".join(relative.parts)


def _target_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for item in node.elts for name in _target_names(item))
    return ()


def _import_aliases(tree: ast.AST) -> set[str]:
    """Collect aliases introduced by forbidden module imports."""

    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_root = alias.name.split(".", 1)[0]
                if module_root in _DYNAMIC_MODULES or module_root == "operator":
                    aliases.add(alias.asname or module_root)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_root = node.module.split(".", 1)[0]
            if module_root in _DYNAMIC_MODULES or module_root == "operator":
                for alias in node.names:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _contains_forbidden_reference(node: ast.AST, aliases: set[str]) -> bool:
    """Return whether an expression contains a direct closed-set entrypoint."""

    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in _FORBIDDEN_NAMES | aliases:
            return True
        if isinstance(child, ast.Attribute) and child.attr in _FORBIDDEN_NAMES:
            return True
        if isinstance(child, ast.Constant) and child.value in _FORBIDDEN_CONSTANTS:
            return True
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.value, ast.Dict)
            and not isinstance(child.slice, ast.Constant)
        ):
            # A computed lookup into an inline dictionary can conceal an
            # entrypoint without spelling its final name in one Constant.
            return True
    return False


def _record_assignment_aliases(tree: ast.AST, aliases: set[str], exempt_nodes: set[int]) -> None:
    """Propagate aliases for direct assignments without evaluating strings."""

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if id(node.value) in exempt_nodes:
                    continue
                targets = tuple(name for target in node.targets for name in _target_names(target))
                if _contains_forbidden_reference(node.value, aliases):
                    for name in targets:
                        if name not in aliases:
                            aliases.add(name)
                            changed = True
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if id(node.value) in exempt_nodes:
                    continue
                if _contains_forbidden_reference(node.value, aliases):
                    for name in _target_names(node.target):
                        if name not in aliases:
                            aliases.add(name)
                            changed = True


def _safe_storage_reflection_nodes(path: Path, tree: ast.AST) -> set[int]:
    """Allow only the exact trusted-type/raw-storage reads owned by Canonical.

    This is narrower than allowing reflection names globally: the enclosing
    function, assignment targets, receiver and literal attributes must all
    match.  Any added reflection call remains a violation.
    """

    if path.name != "canonical.py":
        return set()
    expected_by_function = {
        "_safe_model_storage_tree": {
            "storage": "__dict__",
            "extras": "__pydantic_extra__",
            "fields_set": "__pydantic_fields_set__",
        },
        "_is_model_type": {"model_mro": "__mro__"},
        "_assert_strict_model": {"config": "model_config", "fields": "model_fields"},
    }
    exempt: set[int] = set()
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        expected = expected_by_function.get(function.name)
        if expected is None:
            continue
        for statement in ast.walk(function):
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            call = statement.value
            if not isinstance(target, ast.Name) or target.id not in expected or not isinstance(call, ast.Call):
                continue
            attribute = call.func
            receiver = "object" if target.id in {"storage", "extras", "fields_set"} else "type"
            value_name = "value" if receiver == "object" else ("value" if target.id == "model_mro" else "model")
            if (
                not isinstance(attribute, ast.Attribute)
                or attribute.attr != "__getattribute__"
                or not isinstance(attribute.value, ast.Name)
                or attribute.value.id != receiver
                or len(call.args) != 2
                or call.keywords
                or not isinstance(call.args[0], ast.Name)
                or call.args[0].id != value_name
                or not isinstance(call.args[1], ast.Constant)
                or call.args[1].value != expected[target.id]
            ):
                continue
            exempt.update(id(node) for node in ast.walk(call))
    return exempt


def _scan_file(root: Path, path: Path, *, is_contracts: bool) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    layer = "app/contracts" if is_contracts else "app/skill_abi"
    symbol_allowlist = _IMPORT_SYMBOL_ALLOWLISTS[layer]
    internal_prefixes = _INTERNAL_IMPORT_PREFIXES[layer]
    # ``b`` is the conventional short alias used by builtins probes and was
    # historically part of the boundary.  Keep it closed even when it is only
    # a function parameter (there is no safe way to prove what it contains).
    aliases = set(_REFLECTION_NAMES) | {"b", "builtins"}
    aliases.update(_import_aliases(tree))
    exempt_nodes = _safe_storage_reflection_nodes(path, tree)
    _record_assignment_aliases(tree, aliases, exempt_nodes)
    violations: list[str] = []

    def add(node: ast.AST, reason: str) -> None:
        line = getattr(node, "lineno", 0)
        violations.append(f"{path.relative_to(root)}:{line}: {reason}")

    for node in ast.walk(tree):
        if id(node) in exempt_nodes:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name
                reason = _import_violation_reason(
                    imported,
                    symbol_allowlist,
                    internal_prefixes,
                    is_module_import=True,
                )
                if reason is not None:
                    add(alias, reason)
        elif isinstance(node, ast.ImportFrom) and node.level:
            add(node, "relative import")
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported = node.module
            reason = _import_violation_reason(
                imported,
                symbol_allowlist,
                internal_prefixes,
                is_module_import=False,
            )
            if reason is not None:
                add(node, reason)
            elif not _matches_prefix(imported, internal_prefixes):
                allowed_symbols = symbol_allowlist[imported]
                for alias in node.names:
                    if alias.name not in allowed_symbols:
                        add(alias, f"{imported}.{alias.name}")
                    if alias.name in _FORBIDDEN_NAMES or alias.asname in _FORBIDDEN_NAMES:
                        add(alias, "dynamic import")
        elif isinstance(node, ast.ImportFrom) and node.module is None:
            add(node, "relative import")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES | aliases:
            add(node, "dynamic import")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            add(node, "dynamic import")
        elif isinstance(node, ast.Constant) and node.value in _FORBIDDEN_CONSTANTS:
            add(node, "dynamic import")
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Dict)
            and not isinstance(node.slice, ast.Constant)
        ):
            add(node, "dynamic import")

    return violations


def find_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for relative_root in SPINE_ROOTS:
        package_root = root / relative_root
        if not package_root.is_dir():
            continue
        is_contracts = relative_root == "app/contracts"
        for path in sorted(package_root.rglob("*.py")):
            violations.extend(_scan_file(root, path, is_contracts=is_contracts))
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = find_violations(root)
    if violations:
        print("contract spine dependency violations:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
