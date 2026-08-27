// WS-6 6.F.3 golden tests ported from tests/test_ws6_f3_projection_consistency.py.
// The interaction view (typed milestones + rendered output) must be identical
// across uninterrupted, reordered/duplicated and reconnect/backfill delivery.

import { describe, expect, it } from "vitest";
import { RunInteractionModel, reduceRunView } from "../src/model/runInteractionModel";
import { assetRiskRendererRegistry } from "../src/model/assetRiskRenderers";
import {
  ASSET_STATE_VIEW_SCHEMA_REF,
} from "../src/model/assetRiskRenderers";
import type { RunIntentDispatchResult, SnapshotBundle, UIProjectionEvent } from "../src/model/types";

const RUN_ID = "00000000-0000-0000-0000-000000000001";
const TOOL_REQUEST_ID = "00000000-0000-0000-0000-00000000004d";
const RESULT_HASH = "c".repeat(64);
const OBSERVED_AT = "2026-08-21T12:00:00+00:00";

let eventCounter = 0;
function freshEventId(): string {
  eventCounter += 1;
  return `event-${eventCounter}`;
}

function statusEvent(seq: number, status: string = "running"): UIProjectionEvent {
  return {
    eventId: freshEventId(),
    tenantId: "tenant-a",
    targetRef: RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: "grove.ui.run-status-changed.v1",
    payload: { kind: "run_status_changed", run_id: RUN_ID, status, run_revision: seq },
  };
}

function messageEvent(seq: number): UIProjectionEvent {
  return {
    eventId: freshEventId(),
    tenantId: "tenant-a",
    targetRef: RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: "grove.ui.message-started.v1",
    payload: { kind: "message_started", message_id: `msg-${seq}`, role: "assistant" },
  };
}

function domainViewEvent(seq: number): UIProjectionEvent {
  return {
    eventId: freshEventId(),
    tenantId: "tenant-a",
    targetRef: RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: "grove.ui.domain-view-accepted.v1",
    payload: {
      kind: "domain_view_accepted",
      run_id: RUN_ID,
      tool_request_id: TOOL_REQUEST_ID,
      view_schema_ref: ASSET_STATE_VIEW_SCHEMA_REF,
      observed_at: OBSERVED_AT,
      source_ref: "asset-state:postgres:rev-42",
      result_hash: RESULT_HASH,
      item_count: 3,
    },
  };
}

function harness(snapshotEvents: UIProjectionEvent[]) {
  const bundle: SnapshotBundle = { view: reduceRunView(snapshotEvents), events: snapshotEvents };
  const batches: UIProjectionEvent[][] = [];
  const dispatchResult: RunIntentDispatchResult = { outcome: "accepted", errorCode: null };
  const model = new RunInteractionModel({
    loadSnapshot: async () => bundle,
    loadBatch: async () => batches.shift() ?? [],
    dispatch: async () => dispatchResult,
  });
  return { model, batches, bundle };
}

describe("6.F.3 projection consistency", () => {
  it("reconnect backfill converges to the uninterrupted replay", async () => {
    const e1 = statusEvent(1);
    const e2 = messageEvent(2);
    const e3 = domainViewEvent(3);
    const e4 = domainViewEvent(4);
    const e5 = statusEvent(5, "succeeded");
    const { model, batches } = harness([{ ...e1 }, { ...e2 }]);
    await model.open();
    let snapshot = await model.applyEvent({ ...e5 });
    expect(snapshot.reconnecting).toBe(true);
    expect(snapshot.cursor).toBe(2);
    expect(snapshot.view.domainViews).toEqual([]);
    expect(snapshot.view.completeness).toBe("partial");

    batches.push([{ ...e3 }, { ...e4 }]);
    snapshot = await model.applyEvent({ ...e5 });
    expect(snapshot.reconnecting).toBe(false);
    expect(snapshot.cursor).toBe(5);
    const expected = reduceRunView([e1, e2, e3, e4, e5]);
    expect(JSON.stringify(snapshot.view)).toBe(JSON.stringify(expected));
    expect(snapshot.view.domainViews).toHaveLength(1);
    expect(snapshot.view.completeness).toBe("complete");
  });

  it("reordered and duplicated delivery is identical", () => {
    const events = [
      statusEvent(1),
      domainViewEvent(2),
      domainViewEvent(3),
      messageEvent(4),
      statusEvent(5, "succeeded"),
    ];
    const direct = reduceRunView(events);
    const shuffled = [...events].reverse();
    const duplicated = [...shuffled, { ...shuffled[0] }, { ...shuffled[shuffled.length - 1] }];
    const reordered = reduceRunView(duplicated);
    expect(JSON.stringify(reordered)).toBe(JSON.stringify(direct));
    expect(direct.domainViews).toHaveLength(1);
    expect(direct.completeness).toBe("complete");
  });

  it("renderer output is stable across replay variants", () => {
    const events = [statusEvent(1), domainViewEvent(2), domainViewEvent(3), statusEvent(4, "succeeded")];
    const registry = assetRiskRendererRegistry();
    const direct = reduceRunView(events).domainViews.map((milestone) => registry.render(milestone));
    const replay = reduceRunView([...events].reverse());
    expect(replay.domainViews.map((milestone) => registry.render(milestone))).toEqual(direct);
    expect(direct).toEqual([
      {
        kind: "rendered",
        viewSchemaRef: ASSET_STATE_VIEW_SCHEMA_REF,
        title: "资产状态已固定",
        shortResultHash: RESULT_HASH.slice(0, 12),
        fields: [
          { kind: "observed_at", label: "观测时间", value: OBSERVED_AT },
          { kind: "item_count", label: "记录数", value: "3" },
          { kind: "completeness", label: "完整性", value: "complete" },
          { kind: "provenance", label: "数据来源", value: `asset-state:postgres:rev-42 @ ${RESULT_HASH.slice(0, 12)}` },
        ],
      },
    ]);
  });

  it("stream replay of a milestone never duplicates the view", async () => {
    const e1 = statusEvent(1);
    const e2 = domainViewEvent(2);
    const { model } = harness([{ ...e1 }, { ...e2 }]);
    await model.open();
    const stale = await model.applyEvent(domainViewEvent(2));
    expect(stale.cursor).toBe(2);
    expect(stale.view.appliedEventCount).toBe(2);
    const redelivered = await model.applyEvent(domainViewEvent(3));
    expect(redelivered.cursor).toBe(3);
    expect(redelivered.view.appliedEventCount).toBe(3);
    expect(redelivered.view.domainViews).toHaveLength(1);
  });
});
