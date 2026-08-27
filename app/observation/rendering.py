"""Typed domain-view renderer contract (docs/06 §7.2, §15; WS-6 6.E.3).

The reducer accumulates ``domain_view_accepted`` milestones; this module maps
one milestone to a bounded, presentational render model.  Renderers are owned
by a Business Profile and selected strictly by ``view_schema_ref`` through a
closed registry: an unknown ref yields a ``partial`` marker that carries the
schema ref and nothing else.  A generic ``any``/JSON renderer is forbidden —
raw tool payloads, SQL and table names must never reach the display surface.

The output models are closed (``extra="forbid"``) and bounded, so a renderer
physically cannot smuggle extra fields past the contract.  This module is the
pure Python executable reference; the Vue workspace ports it against the same
goldens and page views must consume it rather than guess payloads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.observation.reducer import DomainViewMilestone

MAX_RENDERED_FIELDS = 8
SHORT_HASH_LENGTH = 12


class RenderedField(BaseModel):
    """One safe display row; the value is a pre-formatted string."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["observed_at", "item_count", "completeness", "provenance"]
    label: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=256)


class RenderedDomainView(BaseModel):
    """The typed render result for a renderer-owned ``view_schema_ref``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["rendered"] = "rendered"
    view_schema_ref: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=128)
    fields: tuple[RenderedField, ...] = Field(min_length=1, max_length=MAX_RENDERED_FIELDS)
    short_result_hash: str = Field(min_length=12, max_length=12)


class PartialDomainView(BaseModel):
    """Unknown ``view_schema_ref``: surface as partial, never guess."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["partial"] = "partial"
    view_schema_ref: str = Field(min_length=1, max_length=256)


DomainViewRenderResult = RenderedDomainView | PartialDomainView


class DomainViewRenderer(ABC):
    """A Profile-owned renderer bound to exactly one ``view_schema_ref``."""

    view_schema_ref: ClassVar[str]
    title: ClassVar[str]

    @abstractmethod
    def render(self, milestone: DomainViewMilestone) -> tuple[RenderedField, ...]:
        """Return the bounded safe display rows for one accepted milestone."""


class RendererRegistry:
    """Closed renderer set; unknown refs fail to ``partial``, never to JSON."""

    def __init__(self, renderers: tuple[DomainViewRenderer, ...] | list[DomainViewRenderer]) -> None:
        by_ref: dict[str, DomainViewRenderer] = {}
        for renderer in renderers:
            ref = type(renderer).view_schema_ref
            if ref in by_ref:
                raise ValueError(f"duplicate renderer for view_schema_ref: {ref}")
            by_ref[ref] = renderer
        self._renderers: frozenset[DomainViewRenderer] = frozenset(renderers)
        self._by_ref = by_ref

    @property
    def view_schema_refs(self) -> frozenset[str]:
        return frozenset(self._by_ref)

    def render(self, milestone: DomainViewMilestone) -> DomainViewRenderResult:
        if type(milestone) is not DomainViewMilestone:
            raise TypeError("render requires an exact DomainViewMilestone")
        renderer = self._by_ref.get(milestone.view_schema_ref)
        if renderer is None:
            return PartialDomainView(view_schema_ref=milestone.view_schema_ref)
        return RenderedDomainView(
            view_schema_ref=milestone.view_schema_ref,
            title=type(renderer).title,
            fields=renderer.render(milestone),
            short_result_hash=milestone.result_hash[:SHORT_HASH_LENGTH],
        )


__all__ = [
    "DomainViewRenderResult",
    "DomainViewRenderer",
    "DomainViewMilestone",
    "MAX_RENDERED_FIELDS",
    "PartialDomainView",
    "RenderedDomainView",
    "RenderedField",
    "RendererRegistry",
    "SHORT_HASH_LENGTH",
]
