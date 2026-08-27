// Ported types of the frozen RunInteractionModel contract (docs/06 §6–7).
// The Python reference (app/observation/interaction_model.py) owns the
// semantics; this mirror is what the Vue views consume.

export type ObservationCompleteness = "complete" | "partial" | "stale" | "unavailable";

export type RunUserIntent =
  | { kind: "respond_to_interrupt"; interactionId: string; responsePayloadRef: string }
  | { kind: "decide_action_approval"; actionId: string; approved: boolean }
  | { kind: "cancel_run" }
  | { kind: "fork_run" };

export type IntentOutcome = "accepted" | "rejected" | "conflict";

export interface RunIntentDispatchResult {
  outcome: IntentOutcome;
  errorCode: string | null;
}

export interface UIProjectionEvent {
  eventId: string;
  tenantId: string;
  targetRef: string;
  projectionSeq: number;
  payloadSchemaRef: string;
  payload: Record<string, unknown>;
}

export interface MessageView {
  messageId: string;
  role: string;
}

export interface InteractionView {
  interactionId: string;
  kind: string;
  status: string;
  revision: number;
}

export interface DomainViewMilestone {
  toolRequestId: string;
  viewSchemaRef: string;
  observedAt: string;
  sourceRef: string;
  resultHash: string;
  itemCount: number | null;
}

export interface RunViewState {
  tenantId: string | null;
  runId: string | null;
  status: string | null;
  runRevision: number;
  messages: MessageView[];
  interactions: InteractionView[];
  domainViews: DomainViewMilestone[];
  completeness: ObservationCompleteness;
  lastProjectionSeq: number;
  unknownSchemaCount: number;
  appliedEventCount: number;
}

export interface SnapshotBundle {
  view: RunViewState;
  events: UIProjectionEvent[];
}

export interface InteractionSnapshot {
  view: RunViewState;
  reconnecting: boolean;
  cursor: number;
}

export type UnknownSchemaSink = (schemaRef: string) => void;

export interface RunInteractionAdapter {
  loadSnapshot(): Promise<SnapshotBundle>;
  loadBatch(afterSeq: number, limit: number): Promise<UIProjectionEvent[]>;
  dispatch(intent: RunUserIntent): Promise<RunIntentDispatchResult>;
}

export function emptyViewState(): RunViewState {
  return {
    tenantId: null,
    runId: null,
    status: null,
    runRevision: 0,
    messages: [],
    interactions: [],
    domainViews: [],
    completeness: "complete",
    lastProjectionSeq: 0,
    unknownSchemaCount: 0,
    appliedEventCount: 0,
  };
}
