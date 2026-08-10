from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.build.manifest import (
    WS2_BUSINESS_RELATIONS,
    WS3_BUSINESS_RELATIONS,
    WS3_INFRASTRUCTURE_RELATIONS,
    WS3_SCHEMA_CONTRACT,
    WS3_SCHEMA_CONTRACT_VERSION,
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


def test_ws3_function_contract_uses_complete_schema_identity_keys() -> None:
    function_keys = set(WS3_SCHEMA_CONTRACT["functions"])
    acl_keys = set(WS3_SCHEMA_CONTRACT["function_acl"])
    assert all(key.startswith("public.") and "(" in key and key.endswith(")") for key in function_keys)
    assert all(key.startswith("public.") and "(" in key and key.endswith(")") for key in acl_keys)
    assert "public.grove_reconcile_expired_run_command(p_tenant_id text, p_run_id uuid)" in function_keys
    assert "public.grove_reconcile_expired_run_command(p_tenant_id text, p_run_id uuid)" in acl_keys
    assert "public.grove_execution_claim_lifecycle_valid(p_run_status text, p_command_type text)" in function_keys
    assert "public.grove_execution_claim_lifecycle_valid(p_run_status text, p_command_type text)" in acl_keys
    assert all(
        any(name in key for name in ("heartbeat", "consume", "dead_letter", "reconcile"))
        for key in acl_keys
        if "_internal(" in key
    )
    assert all(
        value == "{grove_migration=X/grove_migration}"
        for key, value in WS3_SCHEMA_CONTRACT["function_acl"].items()
        if "_internal(" in key
    )
    assert WS3_SCHEMA_CONTRACT_VERSION == "ws3-execution-authority-v7"
    assert set(WS3_SCHEMA_CONTRACT["authority_relations"]) == {
        "public.tenant",
        "public.membership",
        "public.workload_principal",
        "public.execution_principal",
        "public.execution_spec",
        "public.command_payload",
        "public.agent_run",
        "public.run_command",
        "public.checkpoints",
        "public.checkpoint_blobs",
        "public.checkpoint_writes",
        "public.checkpoint_migrations",
    }
    assert WS3_SCHEMA_CONTRACT["authority_roles"]["grove_api"]["attributes"]["rolbypassrls"] is False
    assert WS3_SCHEMA_CONTRACT["authority_roles"]["grove_migration"]["attributes"]["rolsuper"] is True
    assert all(
        "target_function" in trigger and "target_function_family" in trigger
        for trigger in (
            *WS3_SCHEMA_CONTRACT["agent_run_triggers"].values(),
            *WS3_SCHEMA_CONTRACT["checkpoint_triggers"].values(),
        )
    )
    assert all(
        set(trigger["target_function_family"]) == {trigger["target_function"]["identity"]}
        for trigger in (
            WS3_SCHEMA_CONTRACT["trigger"],
            *WS3_SCHEMA_CONTRACT["agent_run_triggers"].values(),
            *WS3_SCHEMA_CONTRACT["checkpoint_triggers"].values(),
        )
    )


@pytest.mark.parametrize(
    "section",
    (
        "schema_contract_version",
        "columns",
        "constraints",
        "functions",
        "function_acl",
        "trigger",
        "trigger_target_function",
        "trigger_target_function_family",
        "agent_run_triggers",
        "checkpoint_triggers",
        "policies",
        "migration_rows",
        "rls",
        "privileges",
        "database_temp_privileges",
        "table_acl",
        "authority_roles",
        "authority_relations",
        "authority_relation_exclusions",
        "authority_relation_grants",
        "authority_relation_policies",
        "authority_relation_rules",
        "authority_public_functions",
        "authority_dml_targets",
        "authority_object_inventory",
        "authority_constraints",
        "authority_function_acl",
        "authority_acl",
    ),
)
def test_manifest_rejects_tampered_ws3_schema_evidence_after_all_hashes_are_recomputed(
    tmp_path: Path, section: str
) -> None:
    report: dict[str, object] = {
        "head": "ws3_execution_driver",
        "migration_hash": "b" * 64,
        "business_tables": sorted(WS2_BUSINESS_RELATIONS),
        "round_trip": ["upgrade head", "downgrade base", "upgrade head"],
        "schema_contract_version": WS3_SCHEMA_CONTRACT_VERSION,
        "status": "completed",
        "ws3_schema": json.loads(json.dumps(WS3_SCHEMA_CONTRACT)),
    }
    valid_payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    valid_ref, valid_hash = _write_cas_evidence(tmp_path, "migrations.json", valid_payload)
    valid_manifest = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="ws3_execution_driver",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
        migration_report_ref=valid_ref,
        migration_report_hash=valid_hash,
    )
    assert verify_manifest(valid_manifest, root=tmp_path)

    changed = json.loads(json.dumps(report))
    if section == "schema_contract_version":
        changed[section] = "ws2-tenant-commands"
    elif section == "columns":
        changed["ws3_schema"][section]["checkpoints.content_hash"][2] = "1"
    elif section == "constraints":
        changed["ws3_schema"][section]["checkpoints_content_hash_ck"] = "CHECK (content_hash ~ '^[0-9a-f]{63}$')"
    elif section == "functions":
        changed["ws3_schema"][section][
            "public.grove_checkpoint_claim_provenance(p_tenant_id text, p_run_id uuid, p_command_id uuid, "
            "p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_lease_until timestamp with time zone)"
        ]["definition_sha256"] = "c" * 64
    elif section == "function_acl":
        changed["ws3_schema"][section]["public.grove_reject_agent_run_runtime_build_rebinding()"] = "{public=X/public}"
    elif section == "trigger":
        changed["ws3_schema"][section]["enabled"] = "D"
    elif section == "trigger_target_function":
        changed["ws3_schema"]["trigger"]["target_function"]["definition_sha256"] = "c" * 64
    elif section == "trigger_target_function_family":
        changed["ws3_schema"]["trigger"]["target_function_family"]["public.grove_reject_execution_fence_regression()"][
            "definition_sha256"
        ] = "c" * 64
    elif section == "agent_run_triggers":
        changed["ws3_schema"][section]["public.agent_run.agent_run_runtime_build_guard"]["enabled"] = "D"
    elif section == "checkpoint_triggers":
        changed["ws3_schema"][section]["public.checkpoints.checkpoints_authority_guard"]["enabled"] = "D"
    elif section == "policies":
        changed["ws3_schema"][section]["checkpoints_tenant_policy"]["with_check"] = "false"
    elif section == "migration_rows":
        changed["ws3_schema"][section].append(10)
    elif section == "rls":
        changed["ws3_schema"][section]["agent_run"][1] = False
    elif section == "table_acl":
        changed["ws3_schema"][section]["checkpoints.grove_runtime.INSERT"] = False
    elif section == "database_temp_privileges":
        changed["ws3_schema"][section]["grove_runtime"] = True
    elif section == "authority_roles":
        changed["ws3_schema"][section]["grove_api"]["attributes"]["rolbypassrls"] = True
    elif section == "authority_relations":
        changed["ws3_schema"][section]["public.tenant"]["owner"] = "grove_api"
    elif section == "authority_relation_exclusions":
        changed["ws3_schema"][section]["public.execution_principal"]["authority_dml_targets"] = False
    elif section == "authority_relation_grants":
        changed["ws3_schema"][section]["public.checkpoint_migrations"]["table"]["grove_api"]["UPDATE"] = True
    elif section == "authority_relation_policies":
        changed["ws3_schema"][section]["public.run_command"]["public.run_command.run_command_tenant_isolation"][
            "permissive"
        ] = False
    elif section == "authority_relation_rules":
        changed["ws3_schema"][section]["public.run_command"]["public.run_command.run_command_rule"] = {
            "name": "run_command_rule",
            "enabled": "O",
            "definition": "CREATE RULE run_command_rule AS ON UPDATE TO run_command DO INSTEAD NOTHING",
        }
    elif section == "authority_public_functions":
        changed["ws3_schema"][section]["public.grove_active_tenant()"]["definition_sha256"] = "c" * 64
    elif section == "authority_dml_targets":
        changed["ws3_schema"][section]["public.grove_sync_execution_principal()"] = ["public.tenant"]
    elif section == "authority_object_inventory":
        changed["ws3_schema"][section]["public.tenant"]["relpersistence"] = "u"
    elif section == "authority_constraints":
        changed["ws3_schema"][section]["public.tenant.tenant_pkey"]["definition"] = "PRIMARY KEY (status)"
    elif section == "authority_function_acl":
        changed["ws3_schema"][section]["public.grove_active_tenant()"][0]["grantee"] = "public"
    elif section == "authority_acl":
        changed["ws3_schema"][section]["table"]["public.tenant"][0]["privilege"] = "TRUNCATE"
    else:
        changed["ws3_schema"][section]["runtime_claim_execute"] = False
    tampered_payload = (json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n").encode()
    tampered_ref, tampered_hash = _write_cas_evidence(tmp_path, "migrations.json", tampered_payload)
    tampered_manifest = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="ws3_execution_driver",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
        migration_report_ref=tampered_ref,
        migration_report_hash=tampered_hash,
    )
    assert not verify_manifest(tampered_manifest, root=tmp_path)


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
    assert generated["migration"]["head"] == "ws4_recon_helpers"
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


def test_ws3_catalog_authority_report_binds_actual_root_sections_and_counts(tmp_path: Path) -> None:
    seed = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="ws3_runtime_worker_delivery",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    catalog = {
        "compiler_version": seed["catalog_authority_compiler_version"],
        "expected_artifact_hash": seed["catalog_authority_artifact_hash"],
        "expected_root": seed["catalog_authority_expected_root"],
        "actual_root": seed["catalog_authority_expected_root"],
        "sections": seed["catalog_authority_sections"],
        "section_counts": {name: section["count"] for name, section in seed["catalog_authority_sections"].items()},
    }
    report = {
        "status": "completed",
        "head": "ws3_runtime_worker_delivery",
        "migration_hash": "b" * 64,
        "round_trip": ["upgrade head", "downgrade base", "upgrade head"],
        "business_tables": sorted(WS3_BUSINESS_RELATIONS),
        "infrastructure_tables": sorted(WS3_INFRASTRUCTURE_RELATIONS),
        "schema_contract_version": WS3_SCHEMA_CONTRACT_VERSION,
        "ws3_schema": WS3_SCHEMA_CONTRACT,
        "catalog_authority": catalog,
    }
    valid_payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    valid_ref, valid_hash = _write_cas_evidence(tmp_path, "migrations.json", valid_payload)
    manifest = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="ws3_runtime_worker_delivery",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
        migration_report_ref=valid_ref,
        migration_report_hash=valid_hash,
    )
    assert verify_manifest(manifest, root=tmp_path)

    for field, replacement in (
        ("actual_root", "0" * 64),
        ("sections", {}),
        ("section_counts", {}),
    ):
        changed = json.loads(json.dumps(report))
        changed["catalog_authority"][field] = replacement
        payload = (json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ref, digest = _write_cas_evidence(tmp_path / field, "migrations.json", payload)
        tampered = build_manifest(
            root=tmp_path,
            source_commit="abc1234",
            dirty=False,
            python_version="3.12.12",
            uv_lock_hash="a" * 64,
            migration_head="ws3_runtime_worker_delivery",
            migration_hash="b" * 64,
            app_image_id="not_built",
            postgres_image_id="not_resolved",
            migration_report_ref=ref,
            migration_report_hash=digest,
        )
        assert not verify_manifest(tampered, root=tmp_path)


def test_manifest_rejects_forged_catalog_anchor_after_report_cas_and_manifest_rehash(tmp_path: Path) -> None:
    seed = build_manifest(
        root=tmp_path,
        source_commit="abc1234",
        dirty=False,
        python_version="3.12.12",
        uv_lock_hash="a" * 64,
        migration_head="ws3_runtime_worker_delivery",
        migration_hash="b" * 64,
        app_image_id="not_built",
        postgres_image_id="not_resolved",
    )
    catalog = {
        "compiler_version": seed["catalog_authority_compiler_version"],
        "expected_artifact_hash": seed["catalog_authority_artifact_hash"],
        "expected_root": seed["catalog_authority_expected_root"],
        "actual_root": seed["catalog_authority_expected_root"],
        "sections": seed["catalog_authority_sections"],
        "section_counts": {name: section["count"] for name, section in seed["catalog_authority_sections"].items()},
    }
    report = {
        "status": "completed",
        "head": "ws3_runtime_worker_delivery",
        "migration_hash": "b" * 64,
        "round_trip": ["upgrade head", "downgrade base", "upgrade head"],
        "business_tables": sorted(WS3_BUSINESS_RELATIONS),
        "infrastructure_tables": sorted(WS3_INFRASTRUCTURE_RELATIONS),
        "schema_contract_version": WS3_SCHEMA_CONTRACT_VERSION,
        "ws3_schema": WS3_SCHEMA_CONTRACT,
        "catalog_authority": catalog,
    }
    root = tmp_path / "forged"
    forged = json.loads(json.dumps(seed))
    forged_catalog = forged["catalog_authority_sections"]
    forged["catalog_authority_compiler_version"] = "catalog-authority-root-forged"
    forged["catalog_authority_artifact_hash"] = "f" * 64
    forged["catalog_authority_expected_root"] = "e" * 64
    forged_report = json.loads(json.dumps(report))
    forged_report["catalog_authority"] = {
        "compiler_version": forged["catalog_authority_compiler_version"],
        "expected_artifact_hash": forged["catalog_authority_artifact_hash"],
        "expected_root": forged["catalog_authority_expected_root"],
        "actual_root": forged["catalog_authority_expected_root"],
        "sections": forged_catalog,
        "section_counts": {name: section["count"] for name, section in forged_catalog.items()},
    }
    report_payload = (json.dumps(forged_report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ref, digest = _write_cas_evidence(root, "migrations.json", report_payload)
    forged["migration_report_ref"] = ref
    forged["migration_report_hash"] = digest
    forged["manifest_hash"] = hashlib.sha256(canonical_bytes(forged)).hexdigest()

    # The report/CAS and manifest hashes are internally consistent, but their
    # entire catalog anchor is fabricated.  Verification must re-read the
    # source-controlled trusted anchor instead of trusting this self-consistent set.
    assert not verify_manifest(forged, root=root)


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
    assert output.stat().st_mode & 0o777 == 0o644
    assert cas.stat().st_mode & 0o777 == 0o644
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
