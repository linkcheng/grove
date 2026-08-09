"""Observation API response payloads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.observation.facts import RuntimeEventView, UIProjectionEventView


class EventListResponse(BaseModel):
    """A cursor-paginated list of runtime or UI projection events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: list[RuntimeEventView] | list[UIProjectionEventView] = Field(default_factory=list)
    next_cursor: int = Field(ge=0)


__all__ = ["EventListResponse"]
