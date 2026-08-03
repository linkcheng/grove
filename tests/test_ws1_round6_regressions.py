"""Round-six regressions for closed contract-spine import dependencies."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_contract_dependencies import find_violations


def _spine_root(root: Path, layer: str) -> Path:
    package = root / "app" / layer
    package.mkdir(parents=True)
    return package


@pytest.mark.parametrize(
    "source",
    [
        'import sys\nsys.modules["app.skill_" + "abi"]\n',
        "import sys as s\ns.modules[name]\n",
        "from sys import modules as m\nm[name]\n",
        "def nested(name):\n    import sys as secondary\n    return secondary.modules[name]\n",
        "import os\nos.sys.modules[name]\n",
    ],
)
def test_dependency_checker_rejects_sys_modules_and_secondary_aliases(tmp_path: Path, source: str) -> None:
    (_spine_root(tmp_path, "contracts") / "probe.py").write_text(source, encoding="utf-8")

    assert find_violations(tmp_path)


def test_dependency_checker_rejects_cross_layer_and_unallowlisted_imports(tmp_path: Path) -> None:
    contracts = _spine_root(tmp_path, "contracts")
    skill = _spine_root(tmp_path, "skill_abi")
    (contracts / "reverse.py").write_text("from app.skill_abi import runtime\n", encoding="utf-8")
    (skill / "boundary.py").write_text(
        "import inspect\nfrom app.api import routes\n",
        encoding="utf-8",
    )

    violations = find_violations(tmp_path)

    assert any("app.skill_abi" in violation for violation in violations)
    assert any("inspect" in violation for violation in violations)
    assert any("app.api" in violation for violation in violations)


def test_dependency_checker_accepts_the_current_minimal_import_allowlists(tmp_path: Path) -> None:
    contracts = _spine_root(tmp_path, "contracts")
    skill = _spine_root(tmp_path, "skill_abi")
    (contracts / "legal.py").write_text(
        """from __future__ import annotations
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum, StrEnum
from hashlib import sha256
from json import dumps
from math import isfinite
from re import fullmatch, search
from types import UnionType
from typing import Annotated, Any, Generic, Literal, TypeVar, Union, cast, get_args, get_origin
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from app.contracts import canonical
from app.contracts.canonical import VersionedRef
""",
        encoding="utf-8",
    )
    (skill / "legal.py").write_text(
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
from app.contracts.canonical import VersionedRef
from app.skill_abi.models import SkillRuntimeManifest
from app.skill_abi.runtime import validate_artifact
""",
        encoding="utf-8",
    )

    assert find_violations(tmp_path) == []
    assert find_violations(Path.cwd()) == []
