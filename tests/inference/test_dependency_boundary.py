from __future__ import annotations

from pathlib import Path

from scripts.check_inference_dependencies import find_inference_dependency_violations


def test_current_application_keeps_provider_construction_in_composition_root() -> None:
    assert find_inference_dependency_violations(Path.cwd()) == []


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
