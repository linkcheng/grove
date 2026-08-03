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


def test_run_guarded_rejects_custom_awaitable_without_close() -> None:
    class AwaitableWithoutClose:
        def __await__(self) -> Generator[None, None, int]:
            if False:
                yield
            return 1

    with pytest.raises(RuntimeError, match="async callback"):
        run_guarded(lambda: AwaitableWithoutClose())
