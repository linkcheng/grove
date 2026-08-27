"""Immutable, content-addressed Knowledge Snapshot contract (docs/30 §2).

A snapshot is produced once by a trusted publish step and pinned by
ref/version/content-hash; resolvers never accept a moving reference.  The
self-hash covers every item, source version and the ACL policy, so any
post-publication mutation is detectable at load time and fails closed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.contracts.canonical import CanonicalModel, canonical_hash


class KnowledgeSource(CanonicalModel):
    """One governed corpus source frozen into the snapshot."""

    model_config = CanonicalModel.model_config

    source_ref: str = Field(min_length=1, max_length=256)
    source_version: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(min_length=1, max_length=64)
    classification: Literal["public", "internal", "confidential"] = "internal"


class KnowledgeSnapshotItem(CanonicalModel):
    """One citable knowledge entry with its pinned locator."""

    model_config = CanonicalModel.model_config

    item_ref: str = Field(min_length=1, max_length=256)
    source_ref: str = Field(min_length=1, max_length=256)
    locator: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=65_536)
    keywords: tuple[str, ...] = Field(default=(), max_length=64)
    classification: Literal["public", "internal", "confidential"] = "internal"


class KnowledgeAclPolicy(CanonicalModel):
    """Tenant visibility and principal scope for the snapshot."""

    model_config = CanonicalModel.model_config

    visible_tenants: tuple[str, ...] = Field(min_length=1, max_length=1024)
    required_scope: str = Field(min_length=1, max_length=128)

    @field_validator("visible_tenants")
    @classmethod
    def tenants_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("visible_tenants must be unique")
        return value


class KnowledgeSnapshot(CanonicalModel):
    """The immutable published snapshot and its full provenance chain."""

    model_config = CanonicalModel.model_config

    schema_version: Literal["knowledge.snapshot.v1"] = "knowledge.snapshot.v1"
    snapshot_ref: str = Field(min_length=1, max_length=256)
    snapshot_version: str = Field(min_length=1, max_length=64)
    sources: tuple[KnowledgeSource, ...] = Field(min_length=1, max_length=64)
    items: tuple[KnowledgeSnapshotItem, ...] = Field(min_length=1, max_length=4096)
    acl_policy: KnowledgeAclPolicy
    purpose: str = Field(min_length=1, max_length=256)
    retrieval_build_ref: str = Field(min_length=1, max_length=256)
    published_at: datetime
    trusted_issuer: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_snapshot(self) -> KnowledgeSnapshot:
        if len({item.item_ref for item in self.items}) != len(self.items):
            raise ValueError("snapshot items must have unique refs")
        known_sources = {source.source_ref for source in self.sources}
        if any(item.source_ref not in known_sources for item in self.items):
            raise ValueError("snapshot item references an unpublished source")
        if self.snapshot_ref == "latest" or self.snapshot_version == "latest":
            raise ValueError("moving snapshot references are forbidden")
        if self.compute_hash() != self.content_hash:
            raise ValueError("knowledge snapshot content hash mismatch")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return self

    def compute_hash(self) -> str:
        """Hash the canonical snapshot body without the hash field itself."""

        body = self.model_dump(mode="json", exclude={"content_hash"}, exclude_none=True)
        return canonical_hash(body)


__all__ = ["KnowledgeAclPolicy", "KnowledgeSnapshot", "KnowledgeSnapshotItem", "KnowledgeSource"]
