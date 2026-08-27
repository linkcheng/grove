// Golden contract tests ported from tests/observation/test_interaction_model.py.
// The Python reference owns the semantics; this port must never diverge.

import { describe, expect, it } from "vitest";
import {
  RunInteractionModel,
  RUN_STATUS_SCHEMA,
  reduceRunView,
} from "../src/model/runInteractionModel";
import { emptyViewState, type RunIntentDispatchResult, type SnapshotBundle, type UIProjectionEvent } from "../src/model/types";

const RUN_ID = "00000000-0000-0000-0000-000000000001";
const UNKNOWN_SCHEMA = "grove.ui.unknown.v9";

function statusEvent(seq: number, schemaRef: string = RUN_STATUS_SCHEMA): UIProjectionEvent {
  return {
    eventId: `event-${seq}-${Math.random().toString(36).slice(2)}`,
    tenantId: "tenant-a",
    targetRef: RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: schemaRef,
    payload: { kind: "run_status_changed", run_id: RUN_ID, status: "running", run_revision: seq },
  };
}

class Harness {
  bundle: SnapshotBundle;
  batches: UIProjectionEvent[][] = [];
  batchCalls: Array<[number, number]> = [];
  unknown: string[] = [];
  dispatchResult: RunIntentDispatchResult = { outcome: "accepted", errorCode: null };

  constructor(snapshotEvents: UIProjectionEvent[]) {
    this.bundle = { view: reduceOrEmpty(snapshotEvents), events: snapshotEvents };
  }

  model(batchLimit = 100): RunInteractionModel {
    return new RunInteractionModel(
      {
        loadSnapshot: async () => this.bundle,
        loadBatch: async (afterSeq: number, limit: number) => {
          this.batchCalls.push([afterSeq, limit]);
          return this.batches.shift() ?? [];
        },
        dispatch: async () => this.dispatchResult,
      },
      { unknownSchemaSink: (ref) => this.unknown.push(ref), backfillBatchLimit: batchLimit },
    );
  }
}

function reduceOrEmpty(events: UIProjectionEvent[]) {
  return reduceRunView(events) ?? emptyViewState();
}

describe("RunInteractionModel contract", () => {
  it("open binds a replay-stable snapshot and cursor", async () => {
    const harness = new Harness([statusEvent(1), statusEvent(2)]);
    const snapshot = await harness.model().open();
    expect(snapshot.cursor).toBe(2);
    expect(snapshot.reconnecting).toBe(false);
    expect(snapshot.view.appliedEventCount).toBe(2);
  });

  it("open rejects a non-replay-stable snapshot", async () => {
    const harness = new Harness([statusEvent(1), statusEvent(2)]);
    harness.bundle = {
      view: { ...harness.bundle.view, appliedEventCount: 9 },
      events: harness.bundle.events,
    };
    await expect(harness.model().open()).rejects.toThrow("replay-stable");
  });

  it("applies a contiguous event and advances the cursor", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    await model.open();
    const snapshot = await model.applyEvent(statusEvent(2));
    expect(snapshot.cursor).toBe(2);
    expect(snapshot.view.appliedEventCount).toBe(2);
  });

  it("ignores duplicate and older sequences", async () => {
    const harness = new Harness([statusEvent(1), statusEvent(2)]);
    const model = harness.model();
    await model.open();
    await model.applyEvent(statusEvent(2));
    const snapshot = await model.applyEvent(statusEvent(1));
    expect(snapshot.cursor).toBe(2);
    expect(snapshot.view.appliedEventCount).toBe(2);
    expect(harness.batchCalls).toEqual([]);
  });

  it("marks reconnecting on a gap and backfills in order", async () => {
    const harness = new Harness([statusEvent(1), statusEvent(2)]);
    const model = harness.model();
    await model.open();
    const held = statusEvent(5);
    const pending = await model.applyEvent(held);
    expect(pending.reconnecting).toBe(true);
    expect(pending.cursor).toBe(2);
    harness.batches = [[statusEvent(3)], [statusEvent(4), held]];
    const snapshot = await model.applyEvent(held);
    expect(snapshot.cursor).toBe(5);
    expect(snapshot.reconnecting).toBe(false);
    expect(snapshot.view.appliedEventCount).toBe(5);
    expect(harness.batchCalls).toEqual([
      [2, 100],
      [2, 100],
      [3, 100],
    ]);
  });

  it("stays reconnecting without backfill and never applies past a gap", async () => {
    const harness = new Harness([statusEvent(1), statusEvent(2)]);
    const model = harness.model();
    await model.open();
    const snapshot = await model.applyEvent(statusEvent(6));
    expect(snapshot.reconnecting).toBe(true);
    expect(snapshot.cursor).toBe(2);
    const later = await model.applyEvent(statusEvent(7));
    expect(later.cursor).toBe(2);
    expect(later.view.appliedEventCount).toBe(2);
  });

  it("reports unknown schemas to the sink and marks the view partial", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    await model.open();
    const snapshot = await model.applyEvent(statusEvent(2, UNKNOWN_SCHEMA));
    expect(snapshot.view.completeness).toBe("partial");
    expect(snapshot.view.unknownSchemaCount).toBe(1);
    expect(harness.unknown).toEqual([UNKNOWN_SCHEMA]);
  });

  it("respects the bounded backfill batch limit", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model(2);
    await model.open();
    harness.batches = [[statusEvent(2), statusEvent(3)], [statusEvent(4)]];
    const snapshot = await model.applyEvent(statusEvent(4));
    expect(snapshot.cursor).toBe(4);
    expect(snapshot.reconnecting).toBe(false);
    expect(harness.batchCalls).toEqual([[1, 2]]);
  });

  it("normalizes dispatch outcomes only", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    await model.open();
    for (const outcome of ["accepted", "rejected", "conflict"] as const) {
      harness.dispatchResult = { outcome, errorCode: "E1" };
      const result = await model.dispatch({ kind: "cancel_run" });
      expect(result.outcome).toBe(outcome);
      expect(result.errorCode).toBe("E1");
    }
  });

  it("dedupes stream replays against snapshot events", async () => {
    const events = [statusEvent(1), statusEvent(2)];
    const harness = new Harness(events);
    const model = harness.model();
    await model.open();
    for (const event of events) {
      await model.applyEvent(event);
    }
    const snapshot = await model.applyEvent(statusEvent(3));
    expect(snapshot.cursor).toBe(3);
    expect(snapshot.view.appliedEventCount).toBe(3);
  });

  it("notifies subscribers until unsubscribed", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    const calls: number[] = [];
    const unsubscribe = model.subscribe(() => calls.push(1));
    await model.open();
    unsubscribe();
    await model.applyEvent(statusEvent(2));
    expect(calls).toEqual([1]);
  });

  it("close is idempotent and guards every entry point", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    await model.open();
    model.close();
    model.close();
    await expect(model.applyEvent(statusEvent(2))).rejects.toThrow("closed");
    await expect(model.dispatch({ kind: "cancel_run" })).rejects.toThrow("closed");
    expect(() => model.subscribe(() => undefined)).toThrow("closed");
  });

  it("requires open before events and intents", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    await expect(model.applyEvent(statusEvent(1))).rejects.toThrow("open");
    await expect(model.dispatch({ kind: "cancel_run" })).rejects.toThrow("open");
  });

  it("reduces an empty stream to the empty complete view", () => {
    const state = reduceRunView([]);
    expect(state).toEqual(emptyViewState());
  });
});

// --- Interaction projection semantics (ported from tests/observation/test_reducer.py) ---

import { INTERACTION_RESOLVED_SCHEMA, INTERACTION_UPSERTED_SCHEMA } from "../src/model/runInteractionModel";

function upsertEvent(seq: number, interactionId: string, kind = "user_input", revision = 0): UIProjectionEvent {
  return {
    eventId: `upsert-${seq}-${Math.random().toString(36).slice(2)}`,
    tenantId: "tenant-a",
    targetRef: RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: INTERACTION_UPSERTED_SCHEMA,
    payload: {
      kind: "interaction_upserted",
      interaction: { interaction_id: interactionId, kind, status: "pending", revision },
    },
  };
}

function resolveEvent(seq: number, interactionId: string, itemRevision: number, status = "resolved"): UIProjectionEvent {
  return {
    eventId: `resolve-${seq}-${Math.random().toString(36).slice(2)}`,
    tenantId: "tenant-a",
    targetRef: RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: INTERACTION_RESOLVED_SCHEMA,
    payload: { kind: "interaction_resolved", interaction_id: interactionId, item_revision: itemRevision, status },
  };
}

describe("interaction projection", () => {
  it("upserts pending interactions and exposes them sorted by id", () => {
    const state = reduceRunView([
      upsertEvent(1, "11111111-1111-1111-1111-111111111111"),
      upsertEvent(2, "00000000-0000-0000-0000-000000000002", "permission_request", 3),
    ]);
    expect(state.interactions.map((item) => item.interactionId)).toEqual([
      "00000000-0000-0000-0000-000000000002",
      "11111111-1111-1111-1111-111111111111",
    ]);
    expect(state.interactions[0]?.kind).toBe("permission_request");
    expect(state.interactions[0]?.revision).toBe(3);
  });

  it("resolves a pending interaction at an equal-or-newer revision", () => {
    const state = reduceRunView([
      upsertEvent(1, "i-1", "user_input", 2),
      resolveEvent(2, "i-1", 2),
    ]);
    expect(state.interactions).toEqual([{ interactionId: "i-1", kind: "user_input", status: "resolved", revision: 2 }]);
  });

  it("ignores a stale resolution with an older item revision", () => {
    const state = reduceRunView([
      upsertEvent(1, "i-1", "user_input", 5),
      resolveEvent(2, "i-1", 4),
    ]);
    expect(state.interactions[0]?.status).toBe("pending");
    expect(state.interactions[0]?.revision).toBe(5);
  });

  it("resolves without a prior upsert using the default kind", () => {
    const state = reduceRunView([resolveEvent(1, "i-9", 1, "expired")]);
    expect(state.interactions).toEqual([{ interactionId: "i-9", kind: "user_input", status: "expired", revision: 1 }]);
  });

  it("keeps pending interactions visible through the model snapshot", async () => {
    const harness = new Harness([statusEvent(1)]);
    const model = harness.model();
    await model.open();
    await model.applyEvent(upsertEvent(2, "i-1"));
    const snapshot = model.getSnapshot();
    const pending = snapshot.view.interactions.filter((item) => item.status === "pending");
    expect(pending).toHaveLength(1);
    expect(pending[0]?.interactionId).toBe("i-1");
  });
});
