// RunInteractionModel — TypeScript port of the frozen frontend contract.
// Semantics are owned by app/observation/interaction_model.py and pinned by
// the shared golden fixtures; §7.1 ordering must never diverge.

import {
  type DomainViewMilestone,
  type InteractionSnapshot,
  type InteractionView,
  type RunInteractionAdapter,
  type RunIntentDispatchResult,
  type RunUserIntent,
  type RunViewState,
  type SnapshotBundle,
  type UnknownSchemaSink,
  emptyViewState,
} from "./types";

export const RUN_STATUS_SCHEMA = "grove.ui.run-status-changed.v1";
export const MESSAGE_STARTED_SCHEMA = "grove.ui.message-started.v1";
export const MESSAGE_DELTA_SCHEMA = "grove.ui.message-delta.v1";
export const MESSAGE_COMPLETED_SCHEMA = "grove.ui.message-completed.v1";
export const INTERACTION_UPSERTED_SCHEMA = "grove.ui.interaction-upserted.v1";
export const INTERACTION_RESOLVED_SCHEMA = "grove.ui.interaction-resolved.v1";
export const DOMAIN_VIEW_SCHEMA = "grove.ui.domain-view-accepted.v1";

const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set(["succeeded", "failed", "cancelled"]);

// Bounded reducer: status/revision/message/interaction views plus domain-view
// milestones (6.E.3), unknown-schema and gap semantics.  Renderer selection is
// owned by the Profile registry, never by this reducer.
export function reduceRunView(events: readonly UIEventLike[]): RunViewStateLike {
  const deduped = new Map<string, UIEventLike>();
  for (const event of events) {
    const existing = deduped.get(event.eventId);
    if (existing === undefined || event.projectionSeq < existing.projectionSeq) {
      deduped.set(event.eventId, event);
    }
  }
  const ordered = [...deduped.values()].sort((a, b) => a.projectionSeq - b.projectionSeq);
  if (ordered.length === 0) {
    return emptyViewState();
  }
  let state = emptyViewState();
  const interactions = new Map<string, InteractionView>();
  const domainViews = new Map<string, DomainViewMilestone>();
  let expectedSeq = 0;
  let gapDetected = false;
  let tenant: string | null = null;
  let target: string | null = null;
  for (const event of ordered) {
    const eventTenant = event.tenantId;
    if (tenant === null) {
      tenant = eventTenant;
      target = event.targetRef;
      expectedSeq = event.projectionSeq;
    } else if (eventTenant !== tenant || event.targetRef !== target) {
      // Stream integrity boundary: never surface stale cross-tenant state.
      state = emptyViewState();
      interactions.clear();
      domainViews.clear();
      tenant = eventTenant;
      target = event.targetRef;
      expectedSeq = event.projectionSeq;
      gapDetected = true;
    }
    if (event.projectionSeq !== expectedSeq) {
      gapDetected = true;
      break;
    }
    state = applyEvent(state, event, interactions, domainViews);
    expectedSeq += 1;
  }
  state.interactions = [...interactions.values()].sort((a, b) =>
    a.interactionId < b.interactionId ? -1 : a.interactionId > b.interactionId ? 1 : 0,
  );
  state.domainViews = [...domainViews.values()].sort((a, b) =>
    domainViewKey(a) < domainViewKey(b) ? -1 : domainViewKey(a) > domainViewKey(b) ? 1 : 0,
  );
  state.lastProjectionSeq = ordered[ordered.length - 1].projectionSeq;
  // Mirrors the Python reducer: a non-terminal run is a view that may still
  // change, so it is partial by definition; only a terminal status (with no
  // unknown schemas and no gaps) yields a complete view.
  if (
    state.unknownSchemaCount > 0 ||
    gapDetected ||
    !(state.status !== null && TERMINAL_RUN_STATUSES.has(state.status))
  ) {
    state.completeness = "partial";
  }
  return state;
}

function domainViewKey(milestone: DomainViewMilestone): string {
  return `${milestone.toolRequestId}:${milestone.resultHash}`;
}

type UIEventLike = { eventId: string; tenantId: string; targetRef: string; projectionSeq: number; payloadSchemaRef: string; payload: Record<string, unknown> };
type RunViewStateLike = RunViewState;

function applyEvent(
  state: RunViewState,
  event: UIEventLike,
  interactions: Map<string, InteractionView>,
  domainViews: Map<string, DomainViewMilestone>,
): RunViewState {
  const next: RunViewState = { ...state, messages: [...state.messages], interactions: [] };
  next.tenantId = event.tenantId;
  next.runId = event.targetRef;
  if (event.payloadSchemaRef === RUN_STATUS_SCHEMA) {
    next.status = String(event.payload["status"] ?? next.status ?? "");
    next.runRevision = Number(event.payload["run_revision"] ?? next.runRevision);
  } else if (event.payloadSchemaRef === MESSAGE_STARTED_SCHEMA) {
    next.messages.push({
      messageId: String(event.payload["message_id"] ?? ""),
      role: String(event.payload["role"] ?? "assistant"),
    });
  } else if (
    event.payloadSchemaRef === MESSAGE_DELTA_SCHEMA ||
    event.payloadSchemaRef === MESSAGE_COMPLETED_SCHEMA
  ) {
    // Recognized no-ops for the view: the answer text is assembled from the
    // raw events (model/answerText); recognition keeps completeness honest.
  } else if (event.payloadSchemaRef === INTERACTION_UPSERTED_SCHEMA) {
    const item = event.payload["interaction"] as Record<string, unknown>;
    interactions.set(String(item["interaction_id"] ?? ""), {
      interactionId: String(item["interaction_id"] ?? ""),
      kind: String(item["kind"] ?? "user_input"),
      status: String(item["status"] ?? "pending"),
      revision: Number(item["revision"] ?? 0),
    });
  } else if (event.payloadSchemaRef === INTERACTION_RESOLVED_SCHEMA) {
    const id = String(event.payload["interaction_id"] ?? "");
    const current = interactions.get(id);
    const itemRevision = Number(event.payload["item_revision"] ?? 0);
    // A stale resolution (older revision than the accumulated item) is
    // ignored, mirroring the Python reducer exactly.
    if (current === undefined || itemRevision >= current.revision) {
      interactions.set(id, {
        interactionId: id,
        kind: current?.kind ?? "user_input",
        status: String(event.payload["status"] ?? "resolved"),
        revision: itemRevision,
      });
    }
  } else if (event.payloadSchemaRef === DOMAIN_VIEW_SCHEMA) {
    // docs/06 §7.2: dedup on tool_request_id + result_hash; the milestone
    // carries only the safe projection surface, never the tool payload.
    const milestone: DomainViewMilestone = {
      toolRequestId: String(event.payload["tool_request_id"] ?? ""),
      viewSchemaRef: String(event.payload["view_schema_ref"] ?? ""),
      observedAt: String(event.payload["observed_at"] ?? ""),
      sourceRef: String(event.payload["source_ref"] ?? ""),
      resultHash: String(event.payload["result_hash"] ?? ""),
      itemCount:
        event.payload["item_count"] === null || event.payload["item_count"] === undefined
          ? null
          : Number(event.payload["item_count"]),
    };
    domainViews.set(domainViewKey(milestone), milestone);
  } else {
    next.unknownSchemaCount += 1;
  }
  next.appliedEventCount += 1;
  return next;
}

export class RunInteractionModel {
  private readonly adapter: RunInteractionAdapter;
  private readonly unknownSchemaSink: UnknownSchemaSink | null;
  private readonly backfillBatchLimit: number;
  private applied: UIEventLike[] = [];
  private view: RunViewState = emptyViewState();
  private cursor = 0;
  private reconnecting = false;
  private listeners: Array<() => void> = [];
  private closed = false;
  private opened = false;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    adapter: RunInteractionAdapter,
    options: { unknownSchemaSink?: UnknownSchemaSink; backfillBatchLimit?: number } = {},
  ) {
    this.adapter = adapter;
    this.unknownSchemaSink = options.unknownSchemaSink ?? null;
    this.backfillBatchLimit = options.backfillBatchLimit ?? 100;
    if (this.backfillBatchLimit < 1) {
      throw new Error("backfillBatchLimit must be positive");
    }
  }

  getSnapshot(): InteractionSnapshot {
    return { view: this.view, reconnecting: this.reconnecting, cursor: this.cursor };
  }

  subscribe(listener: () => void): () => void {
    this.requireOpenable();
    this.listeners.push(listener);
    return () => {
      const index = this.listeners.indexOf(listener);
      if (index >= 0) {
        this.listeners.splice(index, 1);
      }
    };
  }

  async open(): Promise<InteractionSnapshot> {
    this.requireOpenable();
    const bundle: SnapshotBundle = await this.adapter.loadSnapshot();
    const replayed = reduceRunView(bundle.events);
    if (!viewEquals(replayed, bundle.view)) {
      throw new Error("snapshot bundle is not replay-stable");
    }
    this.applied = [...bundle.events];
    this.view = bundle.view;
    this.cursor = bundle.view.lastProjectionSeq;
    this.opened = true;
    this.notify();
    return this.getSnapshot();
  }

  applyEvent(event: UIEventLike): Promise<InteractionSnapshot> {
    if (this.closed) {
      return Promise.reject(new Error("interaction model is closed"));
    }
    if (!this.opened) {
      return Promise.reject(new Error("open() must complete before events are applied"));
    }
    return this.enqueue(async () => {
      if (event.projectionSeq <= this.cursor) {
        return this.getSnapshot();
      }
      if (event.projectionSeq === this.cursor + 1) {
        this.applyContiguous(event);
        return this.getSnapshot();
      }
      this.reconnecting = true;
      this.notify();
      await this.backfillUntil(event.projectionSeq);
      // The held event is either already applied via a batch, now contiguous,
      // or still past a remaining gap; it is never applied out of order.
      if (event.projectionSeq === this.cursor + 1) {
        this.applyContiguous(event);
      }
      return this.getSnapshot();
    });
  }

  dispatch(intent: RunUserIntent): Promise<RunIntentDispatchResult> {
    if (this.closed) {
      return Promise.reject(new Error("interaction model is closed"));
    }
    if (!this.opened) {
      return Promise.reject(new Error("open() must complete before intents are dispatched"));
    }
    return this.adapter.dispatch(intent);
  }

  close(): void {
    this.closed = true;
    this.listeners = [];
  }

  private requireOpenable(): void {
    if (this.closed) {
      throw new Error("interaction model is closed");
    }
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.queue.then(operation, operation);
    this.queue = next.catch(() => undefined);
    return next;
  }

  private applyContiguous(event: UIEventLike): void {
    const beforeUnknown = this.view.unknownSchemaCount;
    this.applied.push(event);
    this.view = reduceRunView(this.applied);
    this.cursor = event.projectionSeq;
    if (this.view.unknownSchemaCount > beforeUnknown && this.unknownSchemaSink !== null) {
      this.unknownSchemaSink(event.payloadSchemaRef);
    }
    this.notify();
  }

  private async backfillUntil(neededSeq: number): Promise<void> {
    while (!this.closed && this.cursor + 1 < neededSeq) {
      const batch = await this.adapter.loadBatch(this.cursor, this.backfillBatchLimit);
      if (batch.length === 0) {
        return;
      }
      const ordered = [...batch].sort((a, b) => a.projectionSeq - b.projectionSeq);
      for (const item of ordered) {
        if (item.projectionSeq === this.cursor + 1) {
          this.applyContiguous(item);
        }
      }
    }
    if (this.cursor + 1 >= neededSeq && this.reconnecting) {
      this.reconnecting = false;
      this.notify();
    }
  }

  private notify(): void {
    for (const listener of [...this.listeners]) {
      listener();
    }
  }
}

function viewEquals(left: RunViewState, right: RunViewState): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}
