from __future__ import annotations

from pathlib import Path

from scripts.check_inference_dependencies import find_inference_dependency_violations


def test_current_application_keeps_provider_construction_in_composition_root() -> None:
    assert find_inference_dependency_violations(Path.cwd()) == []


def test_canonical_field_catalog_reflection_exemption_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "app" / "contracts"
    path.mkdir(parents=True)
    canonical = path / "canonical.py"
    source = """
def _read_model_field_catalog(runtime_type):
    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")
    runtime_fields = runtime_namespace.get("__pydantic_fields__")
    return runtime_fields
"""
    canonical.write_text(source, encoding="utf-8")
    assert find_inference_dependency_violations(tmp_path) == []

    canonical.write_text(source.replace("runtime_type):", "runtime_type, type):", 1), encoding="utf-8")
    assert find_inference_dependency_violations(tmp_path)

    canonical.write_text("type = attacker\n" + source, encoding="utf-8")
    assert find_inference_dependency_violations(tmp_path)

    storage_source = """
def _read_base_model_storage(value, runtime_type):
    base_storage = object.__getattribute__(BaseModel, "__dict__")
    return base_storage
"""
    canonical.write_text(storage_source, encoding="utf-8")
    assert find_inference_dependency_violations(tmp_path) == []

    canonical.write_text("object = attacker\n" + storage_source, encoding="utf-8")
    assert find_inference_dependency_violations(tmp_path)

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
        assert find_inference_dependency_violations(tmp_path)


def test_graph_or_node_cannot_import_private_provider_construction(tmp_path: Path) -> None:
    path = tmp_path / "app" / "execution"
    path.mkdir(parents=True)
    (path / "node.py").write_text(
        "from app.inference.pydantic_ai_adapter import PydanticAIInferencePort\n",
        encoding="utf-8",
    )
    violations = find_inference_dependency_violations(tmp_path)
    assert len(violations) == 1
    assert "private inference dependency" in violations[0]


def test_graph_or_node_cannot_reach_provider_construction_indirectly(tmp_path: Path) -> None:
    path = tmp_path / "app" / "execution"
    path.mkdir(parents=True)
    attacks = {
        "relative.py": "from ..inference.pydantic_ai_adapter import PydanticAIInferencePort\n",
        "importlib_call.py": (
            "from importlib import import_module as load\n"
            "module = load('.'.join(('app', 'inference', 'pydantic_ai_adapter')))\n"
        ),
        "builtins_call.py": "loader = __import__\nmodule = loader('app.inference.contracts')\n",
        "module_cache.py": "import sys\nmodule = sys.modules['app.inference.pydantic_ai_adapter']\n",
        "frame_escape.py": "def load():\n    return load.__globals__['__builtins__']\n",
        "symbol.py": "port_type = PydanticAIInferencePort\n",
        "package.py": "import app.inference as inference\nmodule = getattr(inference, 'contracts')\n",
        "package_symbol.py": "from app.inference import pydantic_ai_adapter as adapter\n",
        "dict_concat.py": (
            "import importlib\n"
            "module = importlib.__dict__['import_' + 'module']('app.inference.' + 'pydantic_ai_adapter')\n"
            "port = module.__dict__['PydanticAI' + 'InferencePort']\n"
            "compose = port.__dict__['_' + 'compose']\n"
        ),
        "join_getattr.py": (
            "import importlib\n"
            "loader = getattr(importlib, ''.join(('import_', 'module')))\n"
            "module = loader('.'.join(('app', 'inference', 'pydantic_ai_adapter')))\n"
            "port = getattr(module, ''.join(('Pydantic', 'AI', 'Inference', 'Port')))\n"
            "compose = getattr(port, ''.join(('_', 'compose')))\n"
        ),
        "importfrom_operator.py": (
            "from importlib import util\n"
            "from operator import methodcaller\n"
            "name = '.'.join(('app', 'inference', 'pydantic_ai_adapter'))\n"
            "module = util.find_spec(name).loader.load_module(name)\n"
            "reader = methodcaller(''.join(('__get', 'attribute__')), ''.join(('Pydantic', 'AI', 'InferencePort')))\n"
            "port_type = reader(module)\n"
        ),
    }
    for filename, source in attacks.items():
        (path / filename).write_text(source, encoding="utf-8")

    violations = find_inference_dependency_violations(tmp_path)
    violated_files = {item.split(":", 1)[0] for item in violations}
    assert violated_files == {f"app/execution/{filename}" for filename in attacks}
