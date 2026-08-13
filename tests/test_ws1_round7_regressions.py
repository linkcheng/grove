"""Round-seven regressions for exact import symbols and awaitable guards."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Generator
from pathlib import Path
from types import coroutine
from typing import Any, cast

import pytest
from app.skill_abi.capability import run_guarded
from scripts.check_contract_dependencies import find_violations


def _spine_root(root: Path, layer: str) -> Path:
    package = root / "app" / layer
    package.mkdir(parents=True)
    return package


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic import ImportString, TypeAdapter\nTypeAdapter(ImportString)\n",
        "import pydantic\npydantic.ImportString\n",
        'import typing\ntyping.sys.modules["x"]\n',
        'from typing import sys as s\ns.modules["x"]\n',
        'import enum\nenum.sys.modules.get("x")\n',
        "from pydantic import TypeAdapter as TA, ImportString as IS\nTA(IS)\n",
        "def nested():\n    from pydantic import ImportString as Secondary\n    return Secondary\n",
        "def nested():\n    import enum as secondary\n    return secondary.sys.modules\n",
    ],
)
def test_dependency_checker_rejects_module_and_symbol_escape_edges(tmp_path: Path, source: str) -> None:
    (_spine_root(tmp_path, "contracts") / "probe.py").write_text(source, encoding="utf-8")

    assert find_violations(tmp_path)


@pytest.mark.parametrize(
    ("layer", "source"),
    [
        (
            "contracts",
            """from __future__ import annotations
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum, StrEnum
from hashlib import sha256
from json import dumps
from math import isfinite
from re import fullmatch
from types import UnionType
from typing import Annotated, Any, Generic, Literal, TypeVar, Union, cast, get_args, get_origin
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_core import SchemaValidator
""",
        ),
        (
            "skill_abi",
            """from __future__ import annotations
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from re import fullmatch
from typing import Any, Literal
from uuid import UUID
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
""",
        ),
    ],
)
def test_dependency_checker_accepts_only_exact_safe_symbols(tmp_path: Path, layer: str, source: str) -> None:
    (_spine_root(tmp_path, layer) / "legal.py").write_text(source, encoding="utf-8")

    assert find_violations(tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic import ImportString\n",
        "from typing import sys\n",
        "from enum import sys\n",
        "from hashlib import new\n",
        "from re import compile\n",
    ],
)
def test_dependency_checker_rejects_unallowlisted_symbols(tmp_path: Path, source: str) -> None:
    (_spine_root(tmp_path, "contracts") / "probe.py").write_text(source, encoding="utf-8")

    assert find_violations(tmp_path)


def test_dependency_checker_rejects_dunder_reflection_names(tmp_path: Path) -> None:
    source = """from pydantic import BaseModel
value = BaseModel.__globals__
value = BaseModel.__module__
"""
    (_spine_root(tmp_path, "contracts") / "probe.py").write_text(source, encoding="utf-8")

    violations = find_violations(tmp_path)

    assert len(violations) >= 2
    assert all("dynamic import" in violation for violation in violations)


def test_run_guarded_rejects_types_coroutine_generator_and_closes_it() -> None:
    @coroutine
    def provider() -> Generator[str, None, None]:
        yield "never consumed"

    result = provider()
    assert inspect.isawaitable(result)
    assert not isinstance(result, Awaitable)

    with pytest.raises(RuntimeError, match="async callback"):
        run_guarded(lambda: result)

    assert result.gi_frame is None


def test_run_guarded_rejects_native_coroutine_and_closes_it() -> None:
    async def provider() -> int:
        return 1

    result = provider()
    assert inspect.isawaitable(result)

    with pytest.raises(RuntimeError, match="async callback"):
        run_guarded(lambda: result)

    assert cast(Any, result).cr_frame is None


def test_run_guarded_preserves_plain_synchronous_generator() -> None:
    def provider() -> Generator[int, None, None]:
        yield 1

    result = run_guarded(provider)

    assert list(result) == [1]
