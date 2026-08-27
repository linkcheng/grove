"""Reference RunInteractionModel: the frozen frontend interaction contract.

This module is the executable specification of ``docs/06`` §6–7: snapshot
composition, projection-sequence ordering, gap recovery, unknown-schema
safety, intent dispatch normalization and listener semantics.  It is pure and
deterministic with injected transport seams; the production adapter speaks
HTTP/SSE while contract tests use in-memory doubles.  The Vue implementation
(WS-6 6.E.2) ports this module against the same golden fixtures -- page views
must consume ``RunInteractionModel`` and never run their own SSE/reducer.

Frozen semantics (§7.1): ``projection_seq <= cursor`` is a duplicate and is
ignored; ``== cursor + 1`` is validated and applied; ``> cursor + 1`` marks
``reconnecting`` and backfills in bounded batches without ever applying
events past the gap.  Unknown payload schemas go to the safe telemetry sink
and surface as ``partial`` views, never as rendered guesses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.canonical import CanonicalModel, UIProjectionEvent
from app.observation.reducer import RunViewState, reduce_run_view

type UnknownSchemaSink = Callable[[str], None]
type RunUserIntent = RespondToInterrupt | DecideActionApproval | CancelRun | ForkRun

BACKFILL_BATCH_LIMIT = 100


class RespondToInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["respond_to_interrupt"] = "respond_to_interrupt"
    interaction_id: UUID
    response_payload_ref: str = Field(min_length=1, max_length=256)


class DecideActionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["decide_action_approval"] = "decide_action_approval"
    action_id: UUID
    approved: bool


class CancelRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cancel_run"] = "cancel_run"


class ForkRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fork_run"] = "fork_run"


class RunIntentDispatchResult(BaseModel):
    """Normalized transport outcome only; accepted never means completed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["accepted", "rejected", "conflict"]
    error_code: str | None = Field(default=None, max_length=64)


class SnapshotBundle(BaseModel):
    """Adapter invariant: the view must be replay-stable from its events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view: RunViewState
    events: tuple[UIProjectionEvent[CanonicalModel], ...]


class InteractionSnapshot(BaseModel):
    """Presentation-facing state: reducer view plus transport health."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view: RunViewState
    reconnecting: bool
    cursor: int = Field(ge=0)


type SnapshotLoader = Callable[[], Awaitable[SnapshotBundle]]
type BatchLoader = Callable[[int, int], Awaitable[Sequence[UIProjectionEvent[CanonicalModel]]]]
type IntentDispatcher = Callable[[RunUserIntent], Awaitable[RunIntentDispatchResult]]


class RunInteractionModel:
    """One run's interaction state machine; the only SSE/reducer consumer."""

    def __init__(
        self,
        *,
        snapshot_loader: SnapshotLoader,
        batch_loader: BatchLoader,
        intent_dispatcher: IntentDispatcher,
        unknown_schema_sink: UnknownSchemaSink | None = None,
        backfill_batch_limit: int = BACKFILL_BATCH_LIMIT,
    ) -> None:
        if backfill_batch_limit < 1:
            raise ValueError("backfill_batch_limit must be positive")
        self._snapshot_loader = snapshot_loader
        self._batch_loader = batch_loader
        self._intent_dispatcher = intent_dispatcher
        self._unknown_schema_sink = unknown_schema_sink
        self._backfill_batch_limit = backfill_batch_limit
        self._lock = asyncio.Lock()
        self._applied: list[UIProjectionEvent[CanonicalModel]] = []
        self._view = RunViewState()
        self._cursor = 0
        self._reconnecting = False
        self._listeners: list[Callable[[], None]] = []
        self._closed = False
        self._opened = False

    def get_snapshot(self) -> InteractionSnapshot:
        return InteractionSnapshot(
            view=self._view,
            reconnecting=self._reconnecting,
            cursor=self._cursor,
        )

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        if self._closed:
            raise RuntimeError("interaction model is closed")
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def open(self) -> InteractionSnapshot:
        self._require_openable()
        bundle = await self._snapshot_loader()
        replayed = reduce_run_view(bundle.events)
        if replayed != bundle.view:
            raise RuntimeError("snapshot bundle is not replay-stable")
        self._applied = list(bundle.events)
        self._view = bundle.view
        self._cursor = bundle.view.last_projection_seq
        self._opened = True
        self._notify()
        return self.get_snapshot()

    async def apply_event(self, event: UIProjectionEvent[CanonicalModel]) -> InteractionSnapshot:
        """Apply one transport event under the frozen §7.1 ordering."""
        self._require_openable()
        if not self._opened:
            raise RuntimeError("open() must complete before events are applied")
        async with self._lock:
            if event.projection_seq <= self._cursor:
                return self.get_snapshot()
            if event.projection_seq == self._cursor + 1:
                self._apply_contiguous(event)
                return self.get_snapshot()
            self._reconnecting = True
            self._notify()
            await self._backfill_until(event.projection_seq)
            # After backfill the held event is either already applied (it
            # arrived through a batch), now contiguous and applied here, or
            # still past a remaining gap; it is never applied out of order.
            if event.projection_seq == self._cursor + 1:
                self._apply_contiguous(event)
            return self.get_snapshot()

    async def dispatch(self, intent: RunUserIntent) -> RunIntentDispatchResult:
        self._require_openable()
        if not self._opened:
            raise RuntimeError("open() must complete before intents are dispatched")
        return await self._intent_dispatcher(intent)

    def close(self) -> None:
        self._closed = True
        self._listeners.clear()

    def _require_openable(self) -> None:
        if self._closed:
            raise RuntimeError("interaction model is closed")

    def _apply_contiguous(self, event: UIProjectionEvent[CanonicalModel]) -> None:
        before_unknown = self._view.unknown_schema_count
        self._applied.append(event)
        self._view = reduce_run_view(self._applied)
        self._cursor = event.projection_seq
        if self._view.unknown_schema_count > before_unknown and self._unknown_schema_sink is not None:
            self._unknown_schema_sink(event.payload_schema_ref)
        self._notify()

    async def _backfill_until(self, needed_seq: int) -> None:
        while not self._closed and self._cursor + 1 < needed_seq:
            batch = await self._batch_loader(self._cursor, self._backfill_batch_limit)
            if not batch:
                return
            for item in sorted(batch, key=lambda entry: entry.projection_seq):
                if item.projection_seq == self._cursor + 1:
                    self._apply_contiguous(item)
        if self._cursor + 1 >= needed_seq and self._reconnecting:
            self._reconnecting = False
            self._notify()

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()


__all__ = [
    "BACKFILL_BATCH_LIMIT",
    "CancelRun",
    "DecideActionApproval",
    "ForkRun",
    "InteractionSnapshot",
    "RespondToInterrupt",
    "RunInteractionModel",
    "RunIntentDispatchResult",
    "RunUserIntent",
    "SnapshotBundle",
]
