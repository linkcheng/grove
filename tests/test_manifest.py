from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.build.manifest import (
    ManifestError,
    RuntimeBuildManifest,
    build_manifest,
    build_manifest_from_workspace,
    canonical_bytes,
    migration_hash,
    verify_manifest,
    write_content_addressed_artifact,
)


def _write_cas_evidence(root: Path, filename: str, payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    ref = f"ci-evidence/sha256/{digest}/{filename}"
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ref, digest


def _valid_sbom(application_version: str, dependencies: dict[str, str]) -> bytes:
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {
            "component": {
                "type": "library",
                "name": "grove",
                "version": application_version,
            }
        },
        "components": [
            {"type": "library", "name": name, "version": version} for name, version in sorted(dependencies.items())
        ],
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _valid_migration_report(head: str, migration_digest: str) -> bytes:
    payload = {
        "head": head,
        "migration_hash": migration_digest,
        "business_tables": [],
        "round_trip": ["upgrade head", "downgrade base", "upgrade head"],
        "status": "completed",
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _release_manifest_with_evidence(
    root: Path,
    *,
    sbom_payload: bytes,
    migration_payload: bytes,
) -> dict[str, object]:
    sbom_ref, sbom_hash = _write_cas_evidence(root, "runtime-sbom.cdx.json", sbom_payload)
    migration_ref, migration_report_hash = _write_cas_evidence(root, "migrations.json", migration_payload)
    return build_manifest(
        root=root,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
        sbom_ref=sbom_ref,
        sbom_hash=sbom_hash,
        migration_report_ref=migration_ref,
        migration_report_hash=migration_report_hash,
        evidence_mode="release",
    )


def test_manifest_is_deterministic_and_hash_verifiable(tmp_path: Path) -> None:
    first = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
    )
    second = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
    )
    assert canonical_bytes(first) == canonical_bytes(second)
    assert verify_manifest(first)
    assert "manifest_hash" in first
    assert "structlog" in first["dependencies"]
    assert "absolute" not in canonical_bytes(first).decode()
    assert "secret" not in canonical_bytes(first).decode()


def test_manifest_rejects_tampering() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="sha256:" + "d" * 64,
    )
    tampered = json.loads(json.dumps(manifest))
    tampered["source"]["commit"] = "tampered"
    with pytest.raises(ManifestError):
        verify_manifest(tampered, raise_on_error=True)


def test_workspace_manifest_has_no_absolute_path_and_missing_lock_fails(tmp_path: Path) -> None:
    root = Path.cwd()
    generated = build_manifest_from_workspace(root)
    assert verify_manifest(generated, root=root)
    assert generated["migration"]["head"] == "ws2_tenant_commands"
    assert str(root) not in canonical_bytes(generated).decode()
    assert verify_manifest(RuntimeBuildManifest.model_validate(generated), root=root)
    with pytest.raises(ManifestError, match="uv.lock"):
        build_manifest_from_workspace(tmp_path)


def test_workspace_manifest_does_not_reuse_local_image_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_APP_IMAGE_ID", "grove-ws0:local")
    monkeypatch.setenv("GROVE_POSTGRES_IMAGE_ID", "pgvector-postgis:pg16")
    generated = build_manifest_from_workspace(Path.cwd())
    assert generated["images"] == {"application": "not_built", "postgres": "not_resolved"}


def test_workspace_manifest_accepts_explicit_immutable_image_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = "sha256:" + "a" * 64
    postgres_id = "sha256:" + "b" * 64
    monkeypatch.setenv("GROVE_APP_IMAGE_ID", app_id)
    monkeypatch.setenv("GROVE_POSTGRES_IMAGE_ID", postgres_id)
    generated = build_manifest_from_workspace(Path.cwd())
    assert generated["images"] == {"application": app_id, "postgres": postgres_id}


def test_manifest_verifier_can_return_false_for_invalid_shape() -> None:
    assert not verify_manifest({"manifest_hash": "not-a-hash"})


def test_manifest_rejects_free_form_capability_status() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    tampered = json.loads(json.dumps(manifest))
    tampered["adapter_capabilities"]["dbos"]["status"] = "maybe"
    assert not verify_manifest(tampered)


@pytest.mark.parametrize(
    ("field", "value"),
    (("sbom_hash", "bad"), ("sbom_ref", "not_generated")),
)
def test_manifest_rejects_unpaired_or_invalid_evidence(field: str, value: str) -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    tampered = json.loads(json.dumps(manifest))
    tampered[field] = value
    if field == "sbom_ref":
        tampered["sbom_hash"] = "b" * 64
    assert not verify_manifest(tampered)


def test_manifest_rejects_mismatched_or_wrong_filename_evidence() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    for ref in (
        f"ci-evidence/sha256/{'a' * 64}/runtime-sbom.cdx.json",
        f"ci-evidence/sha256/{'a' * 64}/other.json",
    ):
        tampered = json.loads(json.dumps(manifest))
        tampered["sbom_ref"] = ref
        tampered["sbom_hash"] = "b" * 64
        assert not verify_manifest(tampered)


def test_manifest_rejects_unknown_schema_and_role_even_after_hash_recalculation() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
    )
    for field, value in (("schema_version", 99), ("roles", ["api", "future_role"])):
        tampered = json.loads(json.dumps(manifest))
        tampered[field] = value
        tampered.pop("manifest_hash")
        tampered["manifest_hash"] = hashlib.sha256(canonical_bytes(tampered)).hexdigest()
        assert not verify_manifest(tampered)


def test_manifest_release_mode_rejects_unbuilt_images() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
        evidence_mode="release",
    )
    assert verify_manifest(manifest)
    assert not verify_manifest(manifest, require_release=True)


def test_release_manifest_rejects_dirty_source() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=True,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
        evidence_mode="release",
    )
    assert verify_manifest(manifest)
    assert not verify_manifest(manifest, require_release=True)
    with pytest.raises(ManifestError, match="source.dirty=false"):
        verify_manifest(manifest, raise_on_error=True, require_release=True)


def test_manifest_binds_content_addressed_evidence_and_rejects_replacement(tmp_path: Path) -> None:
    seed = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    sbom_payload = _valid_sbom(str(seed["application_version"]), dict(seed["dependencies"]))
    sbom_hash = hashlib.sha256(sbom_payload).hexdigest()
    sbom_ref = f"ci-evidence/sha256/{sbom_hash}/runtime-sbom.cdx.json"
    sbom_path = tmp_path / sbom_ref
    sbom_path.parent.mkdir(parents=True)
    sbom_path.write_bytes(sbom_payload)

    migration_payload = _valid_migration_report("baseline", "b" * 64)
    migration_hash_value = hashlib.sha256(migration_payload).hexdigest()
    migration_ref = f"ci-evidence/sha256/{migration_hash_value}/migrations.json"
    migration_path = tmp_path / migration_ref
    migration_path.parent.mkdir(parents=True)
    migration_path.write_bytes(migration_payload)

    manifest = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
        sbom_ref=sbom_ref,
        sbom_hash=sbom_hash,
        migration_report_ref=migration_ref,
        migration_report_hash=migration_hash_value,
    )
    assert verify_manifest(manifest, root=tmp_path)
    assert not verify_manifest(manifest)
    with pytest.raises(ManifestError, match="evidence root"):
        verify_manifest(manifest, raise_on_error=True)

    migration_path.write_bytes(b'{"status":"tampered"}\n')
    assert not verify_manifest(manifest, root=tmp_path)


@pytest.mark.parametrize(
    "report",
    (
        {"status": "completed", "head": "other", "migration_hash": "b" * 64},
        {"status": "completed", "head": "baseline"},
        {"status": "completed", "head": "baseline", "migration_hash": 123},
        {"status": "completed", "head": "baseline", "migration_hash": "c" * 64},
        {"status": "failed", "head": "baseline", "migration_hash": "b" * 64},
    ),
)
def test_manifest_rejects_semantically_invalid_migration_report_after_manifest_rehash(
    tmp_path: Path, report: dict[str, object]
) -> None:
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    ref = f"ci-evidence/sha256/{digest}/migrations.json"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    manifest = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
        migration_report_ref=ref,
        migration_report_hash=digest,
    )
    # The manifest hash and CAS hash are both valid; only the report semantics
    # are forged.  Verification must still reject the evidence.
    assert not verify_manifest(manifest, root=tmp_path)


def test_release_rejects_semantically_invalid_sbom_after_all_hashes_are_recomputed(tmp_path: Path) -> None:
    seed = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    valid = json.loads(_valid_sbom(str(seed["application_version"]), dict(seed["dependencies"])))
    variants: list[bytes] = [b"{}\n", b"not-json\n"]
    for field, value in (
        ("bomFormat", "NotCycloneDX"),
        ("specVersion", "1.4"),
    ):
        changed = json.loads(json.dumps(valid))
        changed[field] = value
        variants.append((json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n").encode())
    for component_field, value in (("name", "not-grove"), ("version", "999")):
        changed = json.loads(json.dumps(valid))
        changed["metadata"]["component"][component_field] = value
        variants.append((json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n").encode())
    missing_dependency = json.loads(json.dumps(valid))
    missing_dependency["components"] = missing_dependency["components"][1:]
    variants.append((json.dumps(missing_dependency, sort_keys=True, separators=(",", ":")) + "\n").encode())
    wrong_version = json.loads(json.dumps(valid))
    wrong_version["components"][0]["version"] = "999"
    variants.append((json.dumps(wrong_version, sort_keys=True, separators=(",", ":")) + "\n").encode())

    migration_payload = _valid_migration_report("baseline", "b" * 64)
    for index, sbom_payload in enumerate(variants):
        root = tmp_path / str(index)
        manifest = _release_manifest_with_evidence(
            root,
            sbom_payload=sbom_payload,
            migration_payload=migration_payload,
        )
        assert not verify_manifest(manifest, root=root, require_release=True)


def test_release_accepts_semantically_valid_evidence(tmp_path: Path) -> None:
    seed = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    manifest = _release_manifest_with_evidence(
        tmp_path,
        sbom_payload=_valid_sbom(str(seed["application_version"]), dict(seed["dependencies"])),
        migration_payload=_valid_migration_report("baseline", "b" * 64),
    )
    assert verify_manifest(manifest, root=tmp_path, require_release=True)


def test_content_addressed_artifact_is_immutable_and_symlink_safe(tmp_path: Path) -> None:
    output = tmp_path / "evidence" / "artifact.json"
    payload = b'{"trusted":true}\n'
    digest = write_content_addressed_artifact(output, payload)
    cas = output.parent / "sha256" / digest / output.name
    assert cas.read_bytes() == payload
    assert write_content_addressed_artifact(output, payload) == digest

    cas.write_bytes(b"conflict")
    with pytest.raises(ManifestError, match="conflicts"):
        write_content_addressed_artifact(output, payload)

    cas.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    cas.symlink_to(outside)
    with pytest.raises(ManifestError, match="symbolic link"):
        write_content_addressed_artifact(output, payload)
    assert outside.read_bytes() == b"outside"


def test_content_addressed_artifact_rejects_symlinked_cas_directory(tmp_path: Path) -> None:
    output = tmp_path / "evidence" / "artifact.json"
    output.parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output.parent / "sha256").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError, match="symbolic link"):
        write_content_addressed_artifact(output, b"payload")
    assert list(outside.iterdir()) == []


def test_manifest_rejects_missing_or_symlinked_cas_evidence(tmp_path: Path) -> None:
    payload = b'{"bomFormat":"CycloneDX","specVersion":"1.5"}\n'
    digest = hashlib.sha256(payload).hexdigest()
    ref = f"ci-evidence/sha256/{digest}/runtime-sbom.cdx.json"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    manifest = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
        sbom_ref=ref,
        sbom_hash=digest,
    )
    path.unlink()
    assert not verify_manifest(manifest, root=tmp_path)
    target = tmp_path / "target"
    target.write_bytes(payload)
    path.symlink_to(target)
    assert not verify_manifest(manifest, root=tmp_path)
    path.unlink()
    outside = tmp_path.parent / "outside-evidence"
    outside.write_bytes(payload)
    path.symlink_to(outside)
    assert not verify_manifest(manifest, root=tmp_path)


def test_workspace_manifest_fails_closed_when_cas_copy_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A temporary workspace with a copied fixed alias but no CAS sibling must
    # not silently become a draft that hides an evidence-generation failure.
    workspace = tmp_path
    (workspace / "uv.lock").write_text("lock")
    evidence = workspace / "ci-evidence"
    evidence.mkdir()
    (evidence / "runtime-sbom.cdx.json").write_bytes(b"sbom")
    monkeypatch.setattr("app.build.manifest._git_output", lambda *_args: "abc1234")
    monkeypatch.setattr("app.build.manifest.migration_head", lambda *_args: "baseline")
    monkeypatch.setattr("app.build.manifest.migration_hash", lambda *_args: "a" * 64)
    with pytest.raises(ManifestError, match="CAS copy is missing"):
        build_manifest_from_workspace(workspace)


def test_workspace_manifest_fails_closed_when_cas_copy_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path
    (workspace / "uv.lock").write_text("lock")
    evidence = workspace / "ci-evidence"
    evidence.mkdir()
    payload = b"sbom"
    (evidence / "runtime-sbom.cdx.json").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    cas = evidence / "sha256" / digest
    cas.mkdir(parents=True)
    (cas / "runtime-sbom.cdx.json").write_bytes(b"replaced")
    monkeypatch.setattr("app.build.manifest._git_output", lambda *_args: "abc1234")
    monkeypatch.setattr("app.build.manifest.migration_head", lambda *_args: "baseline")
    monkeypatch.setattr("app.build.manifest.migration_hash", lambda *_args: "a" * 64)
    with pytest.raises(ManifestError, match="CAS copy hash mismatch"):
        build_manifest_from_workspace(workspace)


def test_workspace_manifest_without_evidence_is_an_explicit_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "uv.lock").write_text("lock")
    monkeypatch.setattr("app.build.manifest._git_output", lambda *_args: "abc1234")
    monkeypatch.setattr("app.build.manifest.migration_head", lambda *_args: "baseline")
    monkeypatch.setattr("app.build.manifest.migration_hash", lambda *_args: "a" * 64)
    generated = build_manifest_from_workspace(tmp_path)
    assert generated["evidence_mode"] == "draft"


def test_release_manifest_requires_content_addressed_evidence() -> None:
    manifest = build_manifest(
        root=Path("."),
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="baseline",
        migration_hash="b" * 64,
        app_image_id="sha256:" + "c" * 64,
        postgres_image_id="sha256:" + "d" * 64,
        evidence_mode="release",
    )
    assert not verify_manifest(manifest, require_release=True)


def test_migration_hash_covers_execution_closure(tmp_path: Path) -> None:
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    for relative_path in ("alembic.ini", "alembic/env.py", "alembic/script.py.mako", "alembic/versions/001.py"):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative_path)

    first = migration_hash(tmp_path)
    (tmp_path / "alembic" / "env.py").write_text("changed env")
    second = migration_hash(tmp_path)
    (tmp_path / "alembic.ini").write_text("changed config")
    third = migration_hash(tmp_path)
    assert len({first, second, third}) == 3


def test_migration_hash_covers_non_python_revision_assets(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    asset = versions / "001_revision.sql"
    asset.write_text("select 1")
    first = migration_hash(tmp_path)
    asset.write_text("select 2")
    assert migration_hash(tmp_path) != first


def test_migration_hash_ignores_interpreter_cache_artifacts(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    (versions / "__pycache__").mkdir(parents=True)
    (versions / "ignored.pyc").write_bytes(b"cache")
    (versions / "__pycache__" / "ignored.py").write_text("cache")
    first = migration_hash(tmp_path)
    (versions / "ignored.pyc").write_bytes(b"changed")
    (versions / "__pycache__" / "ignored.py").write_text("changed")
    assert migration_hash(tmp_path) == first


def test_migration_head_rejects_multiple_heads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeScriptDirectory:
        def get_heads(self) -> list[str]:
            return ["one", "two"]

    monkeypatch.setattr(
        "app.build.manifest.ScriptDirectory.from_config",
        lambda _config: FakeScriptDirectory(),
    )
    from app.build.manifest import migration_head

    with pytest.raises(ManifestError, match="one Alembic head"):
        migration_head(tmp_path)
