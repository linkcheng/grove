"""Trusted publish step: build an immutable snapshot from governed sources.

The builder is the only component that mints ``content_hash``; every later
reader recomputes and compares (fail closed).  Source documents are plain
canonical records so a deployment can govern them as versioned artifacts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.contracts.canonical import canonical_hash
from app.knowledge.snapshot import (
    KnowledgeAclPolicy,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
    KnowledgeSource,
)


class KnowledgeSourceDocument(BaseModel):
    """One source file as governed on disk before publishing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str = Field(min_length=1, max_length=256)
    source_version: str = Field(min_length=1, max_length=128)
    classification: Literal["public", "internal", "confidential"] = "internal"
    items: tuple[KnowledgeSnapshotItem, ...] = Field(min_length=1, max_length=4096)

    @field_validator("items")
    @classmethod
    def items_match_source(
        cls, value: tuple[KnowledgeSnapshotItem, ...], info: ValidationInfo
    ) -> tuple[KnowledgeSnapshotItem, ...]:
        source_ref = info.data.get("source_ref")
        if isinstance(source_ref, str):
            for item in value:
                if item.source_ref != source_ref:
                    raise ValueError("item source_ref must equal the document source_ref")
        return value


def build_knowledge_snapshot(
    *,
    snapshot_ref: str,
    snapshot_version: str,
    sources: tuple[KnowledgeSourceDocument, ...],
    acl_policy: KnowledgeAclPolicy,
    purpose: str,
    trusted_issuer: str,
    retrieval_build_ref: str = "retrieval.document-keyword.v1",
    published_at: datetime | None = None,
) -> KnowledgeSnapshot:
    """Mint one immutable snapshot over the governed source documents."""

    if not sources:
        raise ValueError("a knowledge snapshot requires at least one source")
    if len({source.source_ref for source in sources}) != len(sources):
        raise ValueError("source refs must be unique")
    frozen_sources = tuple(
        KnowledgeSource(
            source_ref=source.source_ref,
            source_version=source.source_version,
            content_hash=canonical_hash([item.model_dump(mode="json", exclude_none=True) for item in source.items]),
            classification=source.classification,
        )
        for source in sources
    )
    items: list[KnowledgeSnapshotItem] = []
    for source in sources:
        items.extend(source.items)
    if len({item.item_ref for item in items}) != len(items):
        raise ValueError("item refs must be unique across sources")
    published = published_at if published_at is not None else datetime.now(UTC)
    draft = KnowledgeSnapshot.model_construct(
        schema_version="knowledge.snapshot.v1",
        snapshot_ref=snapshot_ref,
        snapshot_version=snapshot_version,
        sources=frozen_sources,
        items=tuple(items),
        acl_policy=acl_policy,
        purpose=purpose,
        retrieval_build_ref=retrieval_build_ref,
        published_at=published,
        trusted_issuer=trusted_issuer,
        content_hash="0" * 64,
    )
    body = draft.model_dump(mode="json", exclude={"content_hash"}, exclude_none=True)
    # Construct the final snapshot through the public (validating) constructor
    # so the minted hash is itself verified by the frozen contract.
    return KnowledgeSnapshot.model_validate({**body, "content_hash": canonical_hash(body)})


__all__ = ["KnowledgeSourceDocument", "build_knowledge_snapshot"]
