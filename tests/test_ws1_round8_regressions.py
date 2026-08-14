"""Round-eight regressions for execution-frame and object-graph escapes."""

from __future__ import annotations

import asyncio
import runpy
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from app.skill_abi.capability import run_guarded
from scripts.check_contract_dependencies import find_violations


def _spine_root(root: Path, layer: str) -> Path:
    package = root / "app" / layer
    package.mkdir(parents=True)
    return package


_FRAME_ESCAPE_SOURCES = (
    """
def reverse():
    yield None

loaded = reverse().gi_frame.f_builtins["__im" + "port__"]("app.skill_abi.runtime", None, None, ["runtime"])
""",
    """
async def reverse():
    return None

coro = reverse()
try:
    loaded = coro.cr_frame.f_builtins["__im" + "port__"]("app.skill_abi.runtime", None, None, ["runtime"])
finally:
    coro.close()
closed_frame = coro.cr_frame
""",
    """
try:
    raise RuntimeError("probe")
except RuntimeError as exc:
    loaded = exc.__traceback__.tb_frame.f_builtins["__im" + "port__"](
        "app.skill_abi.runtime", None, None, ["runtime"]
    )
""",
    """
async def reverse():
    yield None

async def resolve():
    stream = reverse()
    alias = stream
    frame = alias.ag_frame
    try:
        return frame.f_builtins["__im" + "port__"]("app.skill_abi.runtime", None, None, ["runtime"])
    finally:
        await alias.aclose()

loaded = asyncio.run(resolve())
""",
)


@pytest.mark.parametrize("source", _FRAME_ESCAPE_SOURCES)
def test_dependency_checker_rejects_executable_frame_escape_and_python_demo_runs(tmp_path: Path, source: str) -> None:
    """The AST gate blocks a payload that Python itself can otherwise execute."""

    probe = _spine_root(tmp_path, "contracts") / "reverse.py"
    probe.write_text(source, encoding="utf-8")

    violations = find_violations(tmp_path)

    assert violations

    namespace = runpy.run_path(str(probe), run_name="reverse_probe", init_globals={"asyncio": asyncio})
    assert cast(ModuleType, namespace["loaded"]).__name__ == "app.skill_abi.runtime"
    if "closed_frame" in namespace:
        assert namespace["closed_frame"] is None


_FORBIDDEN_ESCAPE_ATTRIBUTES = (
    # generator
    "gi_frame",
    "gi_code",
    "gi_yieldfrom",
    "gi_running",
    "gi_suspended",
    # coroutine
    "cr_frame",
    "cr_code",
    "cr_await",
    "cr_origin",
    "cr_running",
    "cr_suspended",
    # async generator
    "ag_frame",
    "ag_code",
    "ag_await",
    "ag_running",
    # traceback
    "tb_frame",
    "tb_next",
    "tb_lasti",
    "tb_lineno",
    "__traceback__",
    "with_traceback",
    # frame
    "f_back",
    "f_builtins",
    "f_code",
    "f_globals",
    "f_locals",
    "f_trace",
    "f_trace_lines",
    "f_trace_opcodes",
    # function/class/object
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
)


@pytest.mark.parametrize("attribute", _FORBIDDEN_ESCAPE_ATTRIBUTES)
def test_dependency_checker_rejects_every_execution_frame_attribute(tmp_path: Path, attribute: str) -> None:
    source = f"value = object.{attribute}\n"
    (_spine_root(tmp_path, "skill_abi") / "probe.py").write_text(source, encoding="utf-8")

    violations = find_violations(tmp_path)

    assert violations
    assert any("dynamic import" in violation for violation in violations)


@pytest.mark.parametrize("attribute", _FORBIDDEN_ESCAPE_ATTRIBUTES)
def test_dependency_checker_rejects_forbidden_attribute_subscripts(tmp_path: Path, attribute: str) -> None:
    source = f"value = object[{attribute!r}]\n"
    (_spine_root(tmp_path, "contracts") / "subscript.py").write_text(source, encoding="utf-8")

    violations = find_violations(tmp_path)

    assert violations
    assert any("dynamic import" in violation for violation in violations)


def test_dependency_checker_rejects_nested_and_subscript_frame_chains(tmp_path: Path) -> None:
    source = """
def reverse():
    yield None

generator = reverse()
frame = generator.gi_frame
value = frame.f_builtins["__import__"]
"""
    (_spine_root(tmp_path, "contracts") / "nested.py").write_text(source, encoding="utf-8")

    violations = find_violations(tmp_path)

    assert len(violations) >= 3
    assert all("dynamic import" in violation for violation in violations)


def test_dependency_checker_keeps_current_spine_attributes_legal(tmp_path: Path) -> None:
    source = """
from inspect import isawaitable

value.ref
value.version
value.content_hash
value.manifest_hash
value.model_dump
value.model_copy
value.close
isawaitable(value)
"""
    (_spine_root(tmp_path, "skill_abi") / "legal.py").write_text(source, encoding="utf-8")

    assert find_violations(tmp_path) == []
    assert find_violations(Path.cwd()) == []


def test_dependency_checker_allows_only_the_exact_canonical_storage_reader(tmp_path: Path) -> None:
    canonical = _spine_root(tmp_path, "contracts") / "canonical.py"
    source = """
def _read_base_model_storage(value, runtime_type):
    base_storage = object.__getattribute__(BaseModel, "__dict__")
    dict_descriptor = base_storage["__dict__"]
    extras_descriptor = base_storage["__pydantic_extra__"]
    fields_set_descriptor = base_storage["__pydantic_fields_set__"]
    storage = dict_descriptor.__get__(value, runtime_type)
    extras = extras_descriptor.__get__(value, runtime_type)
    fields_set = fields_set_descriptor.__get__(value, runtime_type)
    return storage, extras, fields_set
"""
    canonical.write_text(source, encoding="utf-8")
    assert find_violations(tmp_path) == []

    canonical.write_text(source.replace("value, runtime_type", "value, runtime_type, object", 1), encoding="utf-8")
    assert find_violations(tmp_path)

    canonical.write_text("object = attacker\n" + source, encoding="utf-8")
    assert find_violations(tmp_path)

    canonical.write_text(
        source.replace(
            'fields_set_descriptor = base_storage["__pydantic_fields_set__"]',
            'attacker_descriptor = base_storage["__class__"]',
        ),
        encoding="utf-8",
    )
    assert find_violations(tmp_path)


def test_dependency_checker_allows_only_the_exact_canonical_field_catalog_read(tmp_path: Path) -> None:
    canonical = _spine_root(tmp_path, "contracts") / "canonical.py"
    source = """
def _read_model_field_catalog(runtime_type):
    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")
    runtime_fields = runtime_namespace.get("__pydantic_fields__")
    return runtime_fields
"""
    canonical.write_text(source, encoding="utf-8")
    assert find_violations(tmp_path) == []

    canonical.write_text(source.replace('"__dict__"', '"__class__"'), encoding="utf-8")
    assert find_violations(tmp_path)

    canonical.write_text("type = attacker\n" + source, encoding="utf-8")
    assert find_violations(tmp_path)

    skill_abi_canonical = _spine_root(tmp_path, "skill_abi") / "canonical.py"
    skill_abi_canonical.write_text(source, encoding="utf-8")
    assert find_violations(tmp_path)

    binding_variants = (
        source.replace("runtime_type):", "runtime_type=None):", 1),
        source.replace(
            "def _read_model_field_catalog(runtime_type):",
            "def wrapper():\n    def _read_model_field_catalog(runtime_type):",
        )
        .replace("    runtime_namespace", "        runtime_namespace")
        .replace("    runtime_fields", "        runtime_fields")
        .replace("    return runtime_fields", "        return runtime_fields"),
        source.replace(
            '    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
            '    import builtins as type\n    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
        ),
        source.replace(
            '    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
            '    def type():\n        pass\n    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
        ),
        source.replace(
            '    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
            '    class object:\n        pass\n    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
        ),
        source.replace(
            '    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")',
            (
                "    def child(type):\n"
                '        runtime_namespace = type.__getattribute__(runtime_type, "__dict__")\n'
                "        return runtime_namespace\n"
                '    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")'
            ),
        ),
        """
def _read_model_field_catalog(runtime_type):
    try:
        raise ValueError
    except ValueError as type:
        runtime_namespace = type.__getattribute__(runtime_type, "__dict__")
    return runtime_namespace
""",
        """
def _read_model_field_catalog(runtime_type):
    match runtime_type:
        case type:
            runtime_namespace = type.__getattribute__(runtime_type, "__dict__")
    return runtime_namespace
""",
        """
def _assert_strict_model(model, seen=None, *, allow_mapping=1):
    config = type.__getattribute__(model, "model_config")
    fields = type.__getattribute__(model, "model_fields")
    return config, fields
""",
    )
    for variant in binding_variants:
        canonical.write_text(variant, encoding="utf-8")
        assert find_violations(tmp_path)


def test_run_guarded_rejects_custom_awaitable_without_close() -> None:
    class AwaitableWithoutClose:
        def __await__(self) -> Generator[None, None, int]:
            if False:
                yield
            return 1

    with pytest.raises(RuntimeError, match="async callback"):
        run_guarded(lambda: AwaitableWithoutClose())
