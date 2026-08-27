#!/usr/bin/env python3
"""Keep provider construction private to the runtime-worker composition root."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.check_contract_dependencies import (
    _matches_signature,
    _module_rebinds_reflection_names,
    _same_scope_nodes,
    _scope_bound_names,
)

_PRIVATE_MODULES = frozenset(
    {
        "app.inference.ai_config",
        "app.inference.contracts",
        "app.inference.ledger",
        "app.inference.pydantic_ai_adapter",
        "app.inference.schema_catalog",
        "app.inference.transport",
    }
)
_COMPOSITION_MODULE = "app.worker.inference"
_DYNAMIC_GUARD_EXEMPT_MODULES = frozenset({"app.releases.cleanroom"})
_PUBLIC_INFERENCE_SYMBOLS = frozenset({"InferenceError", "InferenceErrorCode", "TypedInferencePort"})
_DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "import_module"})
_MODULE_CACHE_ATTRIBUTES = frozenset({"modules"})
_DYNAMIC_ACCESS_NAMES = frozenset(
    {
        "__import__",
        "attrgetter",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "hasattr",
        "import_module",
        "locals",
        "methodcaller",
        "setattr",
        "vars",
    }
)
_EXECUTION_ESCAPE_ATTRIBUTES = frozenset(
    {
        "__builtins__",
        "__closure__",
        "__code__",
        "__dict__",
        "__getattribute__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "__traceback__",
        "_getframe",
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_locals",
        "tb_frame",
    }
)
_PRIVATE_SYMBOLS = frozenset(
    {
        "InvocationBudget",
        "LedgerTransport",
        "ProviderBindingManifest",
        "ProviderProfilePolicy",
        "PydanticAIInferencePort",
        "schema_catalog",
        "_compose",
        "load_ai_gateway_config",
        "load_provider_binding_manifest",
    }
)


def _is_private_module(module: str | None) -> bool:
    return module in _PRIVATE_MODULES if module is not None else False


def _relative_private_import(node: ast.ImportFrom) -> bool:
    if not node.level or node.module is None:
        return False
    return node.module == "inference" or node.module.startswith("inference.")


def _safe_canonical_reflection_nodes(path: Path, tree: ast.AST) -> set[int]:
    """Allow only Canonical's fixed type/storage reads."""

    if tuple(path.parts[-3:]) != ("app", "contracts", "canonical.py") or not isinstance(tree, ast.Module):
        return set()
    if _module_rebinds_reflection_names(tree):
        return set()
    expected_assignments = {
        ("_is_model_type", "model_mro"): ("type", "value", "__mro__"),
        ("_read_base_model_storage", "base_storage"): ("object", "BaseModel", "__dict__"),
        ("_read_model_field_catalog", "runtime_namespace"): ("type", "runtime_type", "__dict__"),
        ("_assert_strict_model", "config"): ("type", "model", "model_config"),
        ("_assert_strict_model", "fields"): ("type", "model", "model_fields"),
    }
    exempt: set[int] = set()
    functions = (node for node in tree.body if isinstance(node, ast.FunctionDef))
    for function in functions:
        local_bindings = _scope_bound_names(function.body)
        if local_bindings.intersection({"object", "type", "BaseModel"}):
            continue
        if function.name == "_is_model_type":
            signature_is_exact = _matches_signature(function, positional=("value",))
        elif function.name == "_read_base_model_storage":
            signature_is_exact = _matches_signature(function, positional=("value", "runtime_type"))
        elif function.name == "_read_model_field_catalog":
            signature_is_exact = _matches_signature(function, positional=("runtime_type",))
        elif function.name == "_assert_strict_model":
            signature_is_exact = _matches_signature(
                function,
                positional=("model", "seen"),
                positional_defaults=(None,),
                keyword_only=("allow_mapping",),
                keyword_defaults=(True,),
            )
        else:
            continue
        if not signature_is_exact:
            continue
        for node in _same_scope_nodes(function.body):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and (function.name, node.targets[0].id) in expected_assignments
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "__getattribute__"
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == expected_assignments[(function.name, node.targets[0].id)][0]
                and len(node.value.args) == 2
                and isinstance(node.value.args[0], ast.Name)
                and node.value.args[0].id == expected_assignments[(function.name, node.targets[0].id)][1]
                and isinstance(node.value.args[1], ast.Constant)
                and node.value.args[1].value == expected_assignments[(function.name, node.targets[0].id)][2]
                and not node.value.keywords
            ):
                exempt.update(id(child) for child in ast.walk(node.value))
    return exempt


def _safe_existing_dynamic_nodes(path: Path, tree: ast.AST) -> set[int]:
    """Exempt only the existing non-import reflection reads outside capability consumers."""

    allowed_calls: dict[str, set[tuple[str, tuple[object, ...]]]] = {
        "app/api/v1/execution.py": {
            ("getattr", ("fixture_graph_binding", "conformance")),
        },
        "app/auth/context.py": {
            ("getattr", ("settings", None)),
            ("getattr", ("auth_mode", "disabled")),
            ("getattr", ("gateway_auth_token", None)),
        },
        "app/execution/contracts.py": {
            ("vars", ()),
            ("getattr", ("__pydantic_extra__", None)),
        },
        "app/build/manifest.py": {("hasattr", ("O_NOFOLLOW",))},
        "app/observation/reducer.py": {("getattr", ("kind", None))},
    }
    relative = path.as_posix()
    expected = next((value for suffix, value in allowed_calls.items() if relative.endswith(suffix)), set())
    exempt = _safe_canonical_reflection_nodes(path, tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "object"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"__dict__", "__pydantic_extra__", "__pydantic_fields_set__"}
            and path.as_posix().endswith("app/releases/core.py")
        ):
            exempt.update(id(child) for child in ast.walk(node))
            continue
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        function_name = node.func.id
        if function_name not in _DYNAMIC_ACCESS_NAMES:
            continue
        literal_args = tuple(argument.value for argument in node.args[1:] if isinstance(argument, ast.Constant))
        if len(literal_args) == max(0, len(node.args) - 1) and (function_name, literal_args) in expected:
            exempt.update(id(child) for child in ast.walk(node))
            continue
        if (
            path.as_posix().endswith("app/main.py")
            and function_name == "getattr"
            and len(node.args) == 3
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"path", "started_at"}
        ):
            exempt.update(id(child) for child in ast.walk(node))
    return exempt


def find_inference_dependency_violations(root: Path) -> list[str]:
    violations: list[str] = []
    app_root = root / "app"
    for path in sorted(app_root.rglob("*.py")):
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        if module.startswith("app.inference.") or module == _COMPOSITION_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt_nodes = _safe_existing_dynamic_nodes(path, tree)
        for node in ast.walk(tree):
            if id(node) in exempt_nodes:
                continue
            imported: str | None = None
            if isinstance(node, ast.ImportFrom):
                imported = node.module
                if _relative_private_import(node):
                    violations.append(
                        f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: private inference dependency"
                    )
                if module not in _DYNAMIC_GUARD_EXEMPT_MODULES and any(
                    alias.name in _DYNAMIC_ACCESS_NAMES for alias in node.names
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: indirect inference capability access"
                    )
                if imported is not None and imported.split(".", 1)[0] in {"builtins", "importlib", "operator"}:
                    if not (module == "app.build.manifest" and imported == "importlib.metadata"):
                        violations.append(
                            f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: dynamic module import"
                        )
                if imported == "app.inference" and any(
                    alias.name not in _PUBLIC_INFERENCE_SYMBOLS for alias in node.names
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: private inference symbol import"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_private_module(alias.name) or alias.name == _COMPOSITION_MODULE:
                        violations.append(
                            f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: "
                            f"private inference dependency {alias.name}"
                        )
                    elif alias.name == "app.inference":
                        violations.append(
                            f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: inference package object import"
                        )
                    elif alias.name == "importlib" or alias.name == "operator" or alias.name == "builtins":
                        violations.append(
                            f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: dynamic module import"
                        )
            if _is_private_module(imported):
                violations.append(
                    f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: private inference dependency {imported}"
                )
            if imported == _COMPOSITION_MODULE and module != "app.main":
                violations.append(
                    f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: composition dependency {imported}"
                )
            if module in _DYNAMIC_GUARD_EXEMPT_MODULES:
                continue
            if isinstance(node, ast.Name) and node.id in (
                _DYNAMIC_ACCESS_NAMES | _EXECUTION_ESCAPE_ATTRIBUTES | _PRIVATE_SYMBOLS
            ):
                violations.append(
                    f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: indirect inference capability access"
                )
            elif isinstance(node, ast.Attribute):
                if node.attr in _DYNAMIC_IMPORT_NAMES | _EXECUTION_ESCAPE_ATTRIBUTES | _PRIVATE_SYMBOLS:
                    violations.append(
                        f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: indirect inference capability access"
                    )
                elif (
                    node.attr in _MODULE_CACHE_ATTRIBUTES
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: indirect inference capability access"
                    )
            elif isinstance(node, ast.Constant) and (node.value in _PRIVATE_MODULES or node.value in _PRIVATE_SYMBOLS):
                violations.append(
                    f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: indirect inference capability access"
                )
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                constant_parts = [
                    child.value
                    for child in ast.walk(node)
                    if isinstance(child, ast.Constant) and type(child.value) is str
                ]
                if constant_parts:
                    joined = "".join(constant_parts)
                    if any(symbol in joined for symbol in _DYNAMIC_IMPORT_NAMES | _PRIVATE_SYMBOLS) or any(
                        module_name in joined for module_name in _PRIVATE_MODULES
                    ):
                        violations.append(
                            f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}: computed inference capability name"
                        )
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = find_inference_dependency_violations(root)
    if violations:
        print("inference dependency violations:", file=sys.stderr)
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
