"""WS-6 6.F.1: the domain-view runtime fact and its projection mapping.

The worker emits ``grove.runtime.domain-view-accepted.v1`` atomically with the
asset-risk terminal transition; the projection maps it to the UI
``domain_view_accepted`` milestone consumed by the Profile renderer.  These
tests pin the closed payload contract, the deterministic emit request, the
pure runtime→UI mapping, and the registry membership that keeps unknown
schemas fail-closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.contracts.canonical import CanonicalModel
from app.observation.facts import (
    DOMAIN_VIEW_ACCEPTED_SCHEMA_REF,
    KNOWN_RUNTIME_PAYLOAD_SCHEMAS,
    RUNTIME_PAYLOAD_REGISTRY,
    DomainViewAcceptedPayload,
    UnknownRuntimeSchemaError,
    build_domain_view_emit_request,
    derive_source_event_id,
    domain_view_to_ui_accepted,
    parse_runtime_payload,
)

RUN_ID = uuid4()
TOOL_REQUEST_ID = uuid4()
OBSERVED_AT = datetime(2026, 8, 26, 8, 0, 0, tzinfo=UTC)
RESULT_HASH = "d" * 64


def _payload() -> DomainViewAcceptedPayload:
    return DomainViewAcceptedPayload(
        kind="domain_view_accepted",
        run_id=RUN_ID,
        tool_request_id=TOOL_REQUEST_ID,
        view_schema_ref="AssetStateView@1",
        observed_at=OBSERVED_AT,
        source_ref="asset.state.postgres",
        result_hash=RESULT_HASH,
        item_count=2,
    )


def test_payload_round_trips_and_is_registered() -> None:
    assert DOMAIN_VIEW_ACCEPTED_SCHEMA_REF in KNOWN_RUNTIME_PAYLOAD_SCHEMAS
    assert RUNTIME_PAYLOAD_REGISTRY[DOMAIN_VIEW_ACCEPTED_SCHEMA_REF] is DomainViewAcceptedPayload
    parsed = parse_runtime_payload(DOMAIN_VIEW_ACCEPTED_SCHEMA_REF, _payload().model_dump(mode="json"))
    assert isinstance(parsed, DomainViewAcceptedPayload)
    assert parsed == _payload()


def test_payload_fails_closed_on_extra_naive_and_malformed() -> None:
    base = _payload().model_dump(mode="json")
    with pytest.raises(ValueError):
        DomainViewAcceptedPayload.model_validate({**base, "sql": "SELECT 1"})
    with pytest.raises(ValueError, match="timezone-aware"):
        DomainViewAcceptedPayload.model_validate({**base, "observed_at": "2026-08-26T08:00:00"})
    with pytest.raises(ValueError):
        DomainViewAcceptedPayload.model_validate({**base, "result_hash": "not-a-hash"})
    with pytest.raises(ValueError):
        DomainViewAcceptedPayload.model_validate({**base, "item_count": -1})
    with pytest.raises(UnknownRuntimeSchemaError):
        parse_runtime_payload("grove.runtime.domain-view.v9", base)


def test_emit_request_is_deterministic_and_distinct_per_event_type() -> None:
    occurred = datetime(2026, 8, 26, 8, 0, 1, tzinfo=UTC)
    request = build_domain_view_emit_request(
        run_id=RUN_ID,
        command_seq=0,
        tool_request_id=TOOL_REQUEST_ID,
        view_schema_ref="AssetStateView@1",
        observed_at=OBSERVED_AT,
        source_ref="asset.state.postgres",
        result_hash=RESULT_HASH,
        item_count=2,
        occurred_at=occurred,
    )
    assert request.payload_schema_ref == DOMAIN_VIEW_ACCEPTED_SCHEMA_REF
    assert request.source == "grove.runtime_worker"
    assert request.source_event_id == derive_source_event_id(RUN_ID, 0, "domain.view.accepted", 0)
    # A different event_type namespace keeps the deterministic id from
    # colliding with lifecycle or node-executed facts of the same command.
    assert request.source_event_id != derive_source_event_id(RUN_ID, 0, "node.executed", 0)
    assert request.event_type == "domain.view.accepted"
    assert isinstance(request.payload, CanonicalModel)
    assert request.payload == _payload()
    assert request.occurred_at == occurred


def test_runtime_fact_maps_losslessly_to_the_ui_payload() -> None:
    ui = domain_view_to_ui_accepted(_payload())
    assert ui.kind == "domain_view_accepted"
    assert ui.run_id == RUN_ID
    assert ui.tool_request_id == TOOL_REQUEST_ID
    assert ui.view_schema_ref == "AssetStateView@1"
    assert ui.observed_at == OBSERVED_AT
    assert ui.source_ref == "asset.state.postgres"
    assert ui.result_hash == RESULT_HASH
    assert ui.item_count == 2
    # The reducer's milestone is the same safe surface, so the whole chain --
    # runtime fact -> UI payload -> DomainViewMilestone -> renderer -- is
    # lossless over exactly these fields.
    from app.observation.reducer import DomainViewMilestone

    milestone = DomainViewMilestone(
        tool_request_id=ui.tool_request_id,
        view_schema_ref=ui.view_schema_ref,
        observed_at=ui.observed_at,
        source_ref=ui.source_ref,
        result_hash=ui.result_hash,
        item_count=ui.item_count,
    )
    assert milestone.view_schema_ref == "AssetStateView@1"
    assert isinstance(milestone.tool_request_id, UUID)
