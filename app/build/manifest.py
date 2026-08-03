"""Canonical, content-addressed RuntimeBuildManifest generation and verification."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import __version__
from app.contracts.canonical import canonical_bytes as canonical_contract_bytes
from app.core.config import Role

MANIFEST_SCHEMA_VERSION = 1
ManifestMode = Literal["draft", "release"]
EVIDENCE_PLACEHOLDER = "not_generated"
EVIDENCE_REF_PATTERN = re.compile(r"^ci-evidence/sha256/([0-9a-f]{64})/[A-Za-z0-9_.-]+$")
MIGRATION_EXECUTION_FILES = (
    "alembic.ini",
    "alembic/env.py",
    "alembic/script.py.mako",
)
DEPENDENCY_NAMES = (
    "fastapi",
    "pydantic",
    "pydantic-ai-slim",
    "sqlalchemy",
    "structlog",
    "psycopg",
    "alembic",
    "langgraph",
    "langgraph-checkpoint-postgres",
)


class ManifestError(ValueError):
    """Raised when a manifest cannot be verified."""


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit: str = Field(min_length=1, max_length=128)
    dirty: bool


class MigrationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    head: str = Field(min_length=1, max_length=128)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application: str = Field(min_length=1, max_length=128)
    postgres: str = Field(min_length=1, max_length=128)

    @field_validator("application", "postgres")
    @classmethod
    def validate_image_reference(cls, value: str) -> str:
        if value in {"not_built", "not_resolved"}:
            return value
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("image reference must be a sha256 image ID or an explicit draft placeholder")
        return value


class AdapterCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    status: Literal["disabled", "enabled", "not_configured"]
    reason: str = Field(min_length=1, max_length=128)


class SigningInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_configured"]
    reference: str | None = Field(default=None, max_length=256)


def _validate_evidence_reference(
    ref: str,
    digest: str,
    label: str,
    expected_filename: str,
) -> None:
    if ref == EVIDENCE_PLACEHOLDER and digest == EVIDENCE_PLACEHOLDER:
        return
    if ref == EVIDENCE_PLACEHOLDER or digest == EVIDENCE_PLACEHOLDER:
        raise ValueError(f"{label} evidence ref and hash must be provided together")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{label} evidence hash must be a sha256 digest")
    match = EVIDENCE_REF_PATTERN.fullmatch(ref)
    if match is None or match.group(1) != digest:
        raise ValueError(f"{label} evidence ref must contain its sha256 digest")
    if Path(ref).name != expected_filename:
        raise ValueError(f"{label} evidence ref must name {expected_filename}")


class RuntimeBuildManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1)
    evidence_mode: ManifestMode
    source: SourceInfo
    python: str = Field(min_length=1, max_length=32)
    uv_lock_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: dict[str, str]
    migration: MigrationInfo
    sbom_ref: str = Field(default=EVIDENCE_PLACEHOLDER, min_length=1, max_length=256)
    sbom_hash: str = Field(default=EVIDENCE_PLACEHOLDER, max_length=64)
    migration_report_ref: str = Field(default=EVIDENCE_PLACEHOLDER, min_length=1, max_length=256)
    migration_report_hash: str = Field(default=EVIDENCE_PLACEHOLDER, max_length=64)
    images: ImageInfo
    roles: tuple[Role, ...]
    adapter_capabilities: dict[str, AdapterCapability]
    application_version: str = Field(min_length=1, max_length=32)
    schema_contract_version: str = Field(min_length=1, max_length=32)
    signing: SigningInfo
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, value: tuple[Role, ...]) -> tuple[Role, ...]:
        expected = tuple(Role)
        if value != expected:
            raise ValueError("manifest roles must contain the canonical four roles in order")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> RuntimeBuildManifest:
        _validate_evidence_reference(self.sbom_ref, self.sbom_hash, "SBOM", "runtime-sbom.cdx.json")
        _validate_evidence_reference(
            self.migration_report_ref,
            self.migration_report_hash,
            "migration report",
            "migrations.json",
        )
        return self


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_payload(manifest: RuntimeBuildManifest | dict[str, Any]) -> dict[str, Any]:
    if type(manifest) is RuntimeBuildManifest:
        data = manifest.model_dump(mode="json")
    elif type(manifest) is dict:
        data = dict(manifest)
    else:
        raise TypeError("runtime build manifest must be an exact RuntimeBuildManifest or dict")
    data.pop("manifest_hash", None)
    return data


def canonical_bytes(manifest: RuntimeBuildManifest | dict[str, Any]) -> bytes:
    """Serialize a manifest without timestamps, paths, or non-deterministic whitespace."""

    payload = _canonical_payload(manifest)
    return canonical_contract_bytes(payload, exclude_fields=("manifest_hash",))


def _manifest_with_hash(payload: dict[str, Any]) -> dict[str, Any]:
    without_hash = dict(payload)
    without_hash.pop("manifest_hash", None)
    without_hash["manifest_hash"] = _sha256(canonical_bytes(without_hash))
    # Validate generated manifests at the boundary so an invalid image or evidence
    # reference can never be emitted as apparently usable JSON.  Draft placeholders
    # remain valid here; the strict release verifier rejects them explicitly.
    RuntimeBuildManifest.model_validate(without_hash)
    return without_hash


def verify_manifest(
    manifest: RuntimeBuildManifest | dict[str, Any],
    *,
    root: Path | None = None,
    raise_on_error: bool = False,
    require_release: bool = False,
) -> bool:
    """Verify schema and content hash; optionally raise a precise error for CLI use."""

    try:
        if type(manifest) is RuntimeBuildManifest:
            parsed = manifest
        elif type(manifest) is dict:
            parsed = RuntimeBuildManifest.model_validate(manifest)
        else:
            raise ManifestError("runtime build manifest must be an exact RuntimeBuildManifest or dict")
        if require_release:
            if parsed.source.dirty:
                raise ManifestError(
                    "release verification requires source.dirty=false; draft evidence is not publishable"
                )
            if parsed.evidence_mode != "release":
                raise ManifestError("release verification requires evidence_mode=release")
            if parsed.images.application in {"not_built", "not_resolved"} or parsed.images.postgres in {
                "not_built",
                "not_resolved",
            }:
                raise ManifestError("release verification requires resolved image IDs")
            if root is None:
                raise ManifestError("release verification requires an evidence root")
        has_content_addressed_evidence = any(
            ref != EVIDENCE_PLACEHOLDER for ref in (parsed.sbom_ref, parsed.migration_report_ref)
        )
        if root is None and has_content_addressed_evidence:
            raise ManifestError("evidence verification requires an evidence root")
        if root is not None:
            _verify_evidence_files(parsed, root, require_release=require_release)
        expected = _sha256(canonical_bytes(parsed))
        if parsed.manifest_hash != expected:
            raise ManifestError("manifest hash mismatch")
    except (ManifestError, OSError, ValueError, TypeError) as exc:
        if raise_on_error:
            if isinstance(exc, ManifestError):
                raise
            raise ManifestError(str(exc)) from exc
        return False
    return True


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def migration_hash(root: Path) -> str:
    """Hash the complete Alembic execution closure in one canonical implementation."""

    digest = hashlib.sha256()
    relative_files = set(MIGRATION_EXECUTION_FILES)
    versions_root = root / "alembic" / "versions"
    # Revisions are normally Python modules, but the execution closure is the
    # whole revisions directory.  Include every regular source file (including
    # nested revision assets) while excluding interpreter cache artifacts.
    relative_files.update(
        path.relative_to(root).as_posix()
        for path in versions_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    for relative in sorted(relative_files):
        path = root / relative
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def migration_head(root: Path) -> str:
    """Return the single head from Alembic's actual revision graph."""

    config = AlembicConfig(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise ManifestError(f"expected one Alembic head, found {heads!r}")
    return heads[0]


def write_content_addressed_artifact(output: Path, payload: bytes) -> str:
    """Write an artifact and its immutable sibling, returning the digest."""

    digest = _sha256(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ManifestError("evidence output directory must be a regular directory")

    sha_root = output.parent / "sha256"
    _ensure_regular_directory(sha_root, "evidence CAS root")
    digest_directory = sha_root / digest
    _ensure_regular_directory(digest_directory, "evidence CAS digest directory")
    content_path = digest_directory / output.name
    _write_immutable_cas_file(content_path, payload)
    _atomic_replace_alias(output, payload)
    return digest


def _ensure_regular_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ManifestError(f"{label} must not be a symbolic link")
    try:
        path.mkdir()
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise ManifestError(f"{label} must be a regular directory")


def _write_immutable_cas_file(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ManifestError("evidence CAS file must not be a symbolic link")
    if path.exists():
        if not path.is_file():
            raise ManifestError("evidence CAS path must be a regular file")
        if path.read_bytes() != payload:
            raise ManifestError("existing evidence CAS content conflicts with its digest path")
        return

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        _write_immutable_cas_file(path, payload)
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_replace_alias(output: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _workspace_evidence(root: Path, filename: str) -> tuple[str, str]:
    fixed_path = root / "ci-evidence" / filename
    if not fixed_path.is_file():
        return EVIDENCE_PLACEHOLDER, EVIDENCE_PLACEHOLDER
    digest = _sha256(fixed_path.read_bytes())
    ref = f"ci-evidence/sha256/{digest}/{filename}"
    content_path = root / ref
    if not content_path.is_file():
        raise ManifestError(f"evidence CAS copy is missing for {filename}: {ref}")
    if _sha256(content_path.read_bytes()) != digest:
        raise ManifestError(f"evidence CAS copy hash mismatch for {filename}: {ref}")
    return ref, digest


def _verify_evidence_files(manifest: RuntimeBuildManifest, root: Path, *, require_release: bool) -> None:
    root = root.resolve()
    for label, ref, expected in (
        ("SBOM", manifest.sbom_ref, manifest.sbom_hash),
        ("migration report", manifest.migration_report_ref, manifest.migration_report_hash),
    ):
        if ref == EVIDENCE_PLACEHOLDER:
            if require_release:
                raise ManifestError(f"release verification requires {label} evidence")
            continue
        path = root / ref
        if not path.is_file():
            raise ManifestError(f"{label} evidence file is missing: {ref}")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ManifestError(f"{label} evidence file escapes evidence root: {ref}") from exc
        if path.is_symlink():
            raise ManifestError(f"{label} evidence file must be a regular CAS file: {ref}")
        actual = _sha256(path.read_bytes())
        if actual != expected:
            raise ManifestError(f"{label} evidence hash mismatch")
        if label == "SBOM":
            _verify_sbom(path, manifest)
        elif label == "migration report":
            _verify_migration_report(path, manifest)


def _load_evidence_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{label} must be a JSON object")
    return payload


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _verify_sbom(path: Path, manifest: RuntimeBuildManifest) -> None:
    """Verify the minimum CycloneDX semantics bound by a release manifest."""

    payload = _load_evidence_object(path, "SBOM")
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise ManifestError("SBOM must be CycloneDX 1.5")
    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        raise ManifestError("SBOM metadata.component must be an object")
    if component.get("name") != "grove" or component.get("version") != manifest.application_version:
        raise ManifestError("SBOM root component does not match the manifest application")

    components = payload.get("components")
    if not isinstance(components, list):
        raise ManifestError("SBOM components must be a list")
    available: dict[str, set[str]] = {}
    for item in components:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
        ):
            raise ManifestError("SBOM components must contain string name and version fields")
        available.setdefault(_normalize_package_name(item["name"]), set()).add(item["version"])
    for name, version in manifest.dependencies.items():
        if version not in available.get(_normalize_package_name(name), set()):
            raise ManifestError(f"SBOM dependency does not match manifest: {name}")


def _verify_migration_report(path: Path, manifest: RuntimeBuildManifest) -> None:
    """Verify migration evidence semantics in addition to its CAS bytes."""

    payload = _load_evidence_object(path, "migration report")
    if payload.get("status") != "completed":
        raise ManifestError("migration report status must be completed")
    report_head = payload.get("head")
    if not isinstance(report_head, str) or report_head != manifest.migration.head:
        raise ManifestError("migration report head does not match manifest migration head")
    report_hash = payload.get("migration_hash")
    if not isinstance(report_hash, str) or report_hash != manifest.migration.hash:
        raise ManifestError("migration report hash does not match manifest migration hash")
    if payload.get("round_trip") != ["upgrade head", "downgrade base", "upgrade head"]:
        raise ManifestError("migration report round trip is incomplete")
    if payload.get("business_tables") != []:
        raise ManifestError("migration report contains unexpected business tables")


def _dependency_versions() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in DEPENDENCY_NAMES:
        try:
            values[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            values[name] = "not_installed"
    return values


def build_manifest(
    *,
    root: Path,
    source_commit: str,
    dirty: bool,
    python_version: str,
    uv_lock_hash: str,
    migration_head: str,
    migration_hash: str,
    app_image_id: str,
    postgres_image_id: str,
    sbom_ref: str = EVIDENCE_PLACEHOLDER,
    sbom_hash: str = EVIDENCE_PLACEHOLDER,
    migration_report_ref: str = EVIDENCE_PLACEHOLDER,
    migration_report_hash: str = EVIDENCE_PLACEHOLDER,
    evidence_mode: ManifestMode = "draft",
) -> dict[str, Any]:
    """Build the stable baseline manifest from explicit, already-resolved inputs."""

    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "evidence_mode": evidence_mode,
        "source": {"commit": source_commit, "dirty": dirty},
        "python": python_version,
        "uv_lock_hash": uv_lock_hash,
        "dependencies": _dependency_versions(),
        "migration": {"head": migration_head, "hash": migration_hash},
        "sbom_ref": sbom_ref,
        "sbom_hash": sbom_hash,
        "migration_report_ref": migration_report_ref,
        "migration_report_hash": migration_report_hash,
        "images": {"application": app_image_id, "postgres": postgres_image_id},
        "roles": tuple(role.value for role in Role),
        "adapter_capabilities": {
            "dbos": {"enabled": False, "status": "disabled", "reason": "not_installed"},
        },
        "application_version": __version__,
        "schema_contract_version": "ws0-baseline",
        "signing": {"status": "not_configured", "reference": None},
    }
    # The root is intentionally used only for local hashing; it never enters the manifest.
    if not root:
        raise ManifestError("manifest root is required")
    return _manifest_with_hash(payload)


def build_manifest_from_workspace(root: Path) -> dict[str, Any]:
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        raise ManifestError("uv.lock is required")
    source_commit = _git_output(root, "rev-parse", "HEAD")
    dirty = bool(_git_output(root, "status", "--porcelain=v1"))
    uv_hash = _sha256(lock_path.read_bytes())
    app_image_id = _image_id_or_default("GROVE_APP_IMAGE_ID", "not_built")
    postgres_image_id = _image_id_or_default("GROVE_POSTGRES_IMAGE_ID", "not_resolved")
    sbom_ref, sbom_digest = _workspace_evidence(root, "runtime-sbom.cdx.json")
    migration_report_ref, migration_report_digest = _workspace_evidence(root, "migrations.json")
    resolved_images = re.fullmatch(r"sha256:[0-9a-f]{64}", app_image_id) and re.fullmatch(
        r"sha256:[0-9a-f]{64}", postgres_image_id
    )
    evidence_mode: ManifestMode = (
        "release"
        if not dirty
        and resolved_images
        and sbom_ref != EVIDENCE_PLACEHOLDER
        and migration_report_ref != EVIDENCE_PLACEHOLDER
        else "draft"
    )
    return build_manifest(
        root=root,
        source_commit=source_commit,
        dirty=dirty,
        python_version="3.12.12",
        uv_lock_hash=uv_hash,
        migration_head=migration_head(root),
        migration_hash=migration_hash(root),
        app_image_id=app_image_id,
        postgres_image_id=postgres_image_id,
        sbom_ref=sbom_ref,
        sbom_hash=sbom_digest,
        migration_report_ref=migration_report_ref,
        migration_report_hash=migration_report_digest,
        evidence_mode=evidence_mode,
    )


def _image_id_or_default(name: str, default: str) -> str:
    import os

    value = os.environ.get(name, "").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    # Local tags are intentionally not evidence.  A tag can point at an image
    # built from a different source/lock state; only an explicit immutable ID
    # may be paired with a manifest, and dirty source remains draft regardless.
    return default
