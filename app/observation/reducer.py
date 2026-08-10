"""Headless, deterministic UI projection reducer for the Observation contract.

The reducer is a pure function of a ``UIProjectionEvent`` stream.  It is the
single place that turns an ordered (or re-ordered) projection stream into a
canonical ``RunViewState``.  It owns no I/O and no clock; replaying the same
events always yields byte-identical state, which is the WS-4 golden-fixture
contract.

Semantics (WS-4 acceptance):
  * duplicate ``event_id`` rows collapse to the lowest ``projection_seq``;
  * out-of-order input is sorted by ``projection_seq`` before application, so a
    replay never applies events out of commit order;
  * a ``projection_seq`` gap freezes the view at the last contiguously-applied
    seq and marks it ``partial``; events past the gap are never applied, so a
    missing transition can never produce an unjustified terminal status;
  * an unrecognised payload schema is counted and never crashes the reducer;
  * a tenant switch clears previously accumulated message/interaction state so a
    stale cross-tenant view cannot leak into the new tenant.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.canonical import UIProjectionEvent
from app.observation.facts import (
    KNOWN_UI_PROJECTION_SCHEMA_REFS,
    TERMINAL_RUN_STATUSES,
    ObservationCompleteness,
    PublicRunStatus,
)


class MessageView(BaseModel):
    """Accumulated view of one assistant/user/system message stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    role: str = Field(min_length=1, max_length=32)
    content_schema_ref: str | None = None
    delta_count: int = Field(default=0, ge=0)
    last_delta_seq: int = Field(default=-1, ge=-1)
    completed: bool = False
    content_hash: str | None = None


class InteractionView(BaseModel):
    """Accumulated view of one interaction item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: UUID
    kind: str = Field(min_length=1, max_length=32)
    status: str = Field(min_length=1, max_length=32)
    revision: int = Field(ge=0)


class RunViewState(BaseModel):
    """The canonical, replay-stable output of the headless reducer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str | None = None
    run_id: UUID | None = None
    status: PublicRunStatus | None = None
    run_revision: int = Field(default=0, ge=0)
    messages: tuple[MessageView, ...] = ()
    interactions: tuple[InteractionView, ...] = ()
    completeness: ObservationCompleteness = "complete"
    last_projection_seq: int = Field(default=0, ge=0)
    unknown_schema_count: int = Field(default=0, ge=0)
    applied_event_count: int = Field(default=0, ge=0)


def _empty_state() -> RunViewState:
    return RunViewState()


def _dedup_and_sort(events: Sequence[UIProjectionEvent[Any]]) -> list[UIProjectionEvent[Any]]:
    """Collapse duplicate event ids to the lowest projection_seq, then sort."""

    seen: dict[Any, UIProjectionEvent[Any]] = {}
    for event in events:
        existing = seen.get(event.event_id)
        if existing is None or event.projection_seq < existing.projection_seq:
            seen[event.event_id] = event
    return sorted(seen.values(), key=lambda item: item.projection_seq)


def _terminal_run_status(status: PublicRunStatus) -> bool:
    return status in TERMINAL_RUN_STATUSES


def reduce_run_view(events: Sequence[UIProjectionEvent[Any]]) -> RunViewState:
    """Reduce one projection stream into a replay-stable ``RunViewState``.

    An empty stream yields an empty, ``complete`` view (no evidence of any run
    yet).  A non-empty stream is de-duplicated, sorted by ``projection_seq``,
    gap-checked, and applied in commit order.  The result depends only on the
    set of input events, never on their arrival order, so two replays of the
    same golden stream must be byte-identical.
    """

    ordered = _dedup_and_sort(events)
    if not ordered:
        return _empty_state()

    tenant_id: str | None = None
    run_id: UUID | None = None
    status: PublicRunStatus | None = None
    run_revision = 0
    messages: dict[UUID, dict[str, Any]] = {}
    interactions: dict[UUID, InteractionView] = {}
    unknown_schema_count = 0
    applied = 0
    gap_detected = False
    expected_seq = 0
    last_projection_seq = 0

    for event in ordered:
        event_tenant = event.meta.tenant_id
        event_target = event.target_ref
        if tenant_id is None:
            tenant_id = event_tenant
            run_id = event_target
            expected_seq = event.projection_seq
        elif event_tenant != tenant_id or event_target != run_id:
            # A tenant or target switch is a stream integrity boundary: clear
            # any previously accumulated view so a stale cross-tenant state can
            # never surface for the new tenant.
            # any previously accumulated view so a stale cross-tenant state can
            # never surface for the new tenant, then restart the contiguous
            # expectation at the new stream's first projection_seq.
            messages = {}
            interactions = {}
            status = None
            run_revision = 0
            tenant_id = event_tenant
            run_id = event_target
            gap_detected = True
            expected_seq = event.projection_seq

        # Do not apply events past a projection_seq gap.  The missing event may
        # mutate run state, so applying later events would be out of order and
        # could surface a terminal status that the absent transition has not
        # yet justified.  The view freezes at the contiguous watermark and
        # remains ``partial`` until the gap is repaired.
        if event.projection_seq != expected_seq:
            gap_detected = True
            break
        expected_seq = event.projection_seq + 1
        last_projection_seq = event.projection_seq

        if event.payload_schema_ref not in KNOWN_UI_PROJECTION_SCHEMA_REFS:
            unknown_schema_count += 1
            applied += 1
            continue

        payload = event.payload
        kind = getattr(payload, "kind", None)
        if kind == "run_status_changed":
            status = payload.status
            run_revision = payload.run_revision
        elif kind == "message_started":
            _message_started(messages, payload)
        elif kind == "message_delta":
            _message_delta(messages, payload)
        elif kind == "message_completed":
            _message_completed(messages, payload)
        elif kind == "interaction_upserted":
            _interaction_upserted(interactions, payload)
        elif kind == "interaction_resolved":
            _interaction_resolved(interactions, payload)
        # ``domain_view_accepted`` and ``child_status_changed`` are recognised
        # schemas but carry no run-view accumulation in this slice; they remain
        # visible through the projection stream without altering run state.
        applied += 1

    final_status = status
    completeness: ObservationCompleteness
    if unknown_schema_count > 0 or gap_detected:
        completeness = "partial"
    elif final_status is not None and _terminal_run_status(final_status):
        completeness = "complete"
    else:
        # A non-terminal run is, by definition, a view that may still change.
        completeness = "partial"

    message_views = tuple(
        MessageView(**data) for data in sorted(messages.values(), key=lambda item: str(item["message_id"]))
    )
    interaction_views = tuple(sorted(interactions.values(), key=lambda item: str(item.interaction_id)))

    return RunViewState(
        tenant_id=tenant_id,
        run_id=run_id,
        status=final_status,
        run_revision=run_revision,
        messages=message_views,
        interactions=interaction_views,
        completeness=completeness,
        last_projection_seq=last_projection_seq,
        unknown_schema_count=unknown_schema_count,
        applied_event_count=applied,
    )


def _message_started(messages: dict[UUID, dict[str, Any]], payload: Any) -> None:
    message_id = payload.message_id
    current = messages.get(message_id, {"message_id": message_id})
    current.update(
        {
            "message_id": message_id,
            "role": payload.role,
            "content_schema_ref": payload.content_schema_ref,
        }
    )
    messages[message_id] = current


def _message_delta(messages: dict[UUID, dict[str, Any]], payload: Any) -> None:
    message_id = payload.message_id
    current = messages.get(
        message_id,
        {"message_id": message_id, "role": "assistant", "delta_count": 0, "last_delta_seq": -1},
    )
    if payload.delta_seq > current.get("last_delta_seq", -1):
        current["delta_count"] = current.get("delta_count", 0) + 1
        current["last_delta_seq"] = payload.delta_seq
    messages[message_id] = current


def _message_completed(messages: dict[UUID, dict[str, Any]], payload: Any) -> None:
    message_id = payload.message_id
    current = messages.get(
        message_id,
        {"message_id": message_id, "role": "assistant", "delta_count": 0, "last_delta_seq": -1},
    )
    current["completed"] = True
    current["content_hash"] = payload.content_hash
    current["last_delta_seq"] = max(current.get("last_delta_seq", -1), payload.last_delta_seq)
    messages[message_id] = current


def _interaction_upserted(interactions: dict[UUID, InteractionView], payload: Any) -> None:
    item = payload.interaction
    interactions[item.interaction_id] = InteractionView(
        interaction_id=item.interaction_id,
        kind=item.kind,
        status=item.status,
        revision=item.revision,
    )


def _interaction_resolved(interactions: dict[UUID, InteractionView], payload: Any) -> None:
    current = interactions.get(payload.interaction_id)
    if current is None or payload.item_revision >= current.revision:
        interactions[payload.interaction_id] = InteractionView(
            interaction_id=payload.interaction_id,
            kind=current.kind if current is not None else "user_input",
            status=payload.status,
            revision=payload.item_revision,
        )


__all__ = [
    "InteractionView",
    "KNOWN_UI_PROJECTION_SCHEMA_REFS",
    "MessageView",
    "RunViewState",
    "reduce_run_view",
]
