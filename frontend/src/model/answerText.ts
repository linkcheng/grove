// Answer assembly from the UI projection stream (WS-7).
// The worker publishes the gated typed answer as one assistant message:
// message.started + ordered message.delta events + message.completed
// (content_hash over the concatenated deltas).  Views join the deltas back
// into text; hash verification stays with the projection contract.

import type { UIProjectionEvent } from "./types";

// Closed schema refs mirrored from app/observation/facts.py; suffix matching
// would be open-ended where the backend contract is exact.
const MESSAGE_STARTED_REF = "grove.ui.message-started.v1";
const MESSAGE_DELTA_REF = "grove.ui.message-delta.v1";
const MESSAGE_COMPLETED_REF = "grove.ui.message-completed.v1";

export interface AssembledMessage {
  messageId: string;
  role: "assistant";
  content: string;
  contentHash: string | null;
  completed: boolean;
}

export function assembleAnswerMessages(events: UIProjectionEvent[]): AssembledMessage[] {
  const started = new Map<string, boolean>();
  const deltas = new Map<string, Map<number, string>>();
  const completed = new Map<string, string>();
  for (const event of events) {
    const payload = event.payload as Record<string, unknown>;
    const messageId = String(payload.message_id ?? "");
    if (messageId === "") {
      continue;
    }
    if (event.payloadSchemaRef === MESSAGE_STARTED_REF) {
      started.set(messageId, true);
    } else if (event.payloadSchemaRef === MESSAGE_DELTA_REF) {
      const seq = Number(payload.delta_seq ?? 0);
      const text = String(payload.safe_delta ?? "");
      const perMessage = deltas.get(messageId) ?? new Map<number, string>();
      if (!perMessage.has(seq)) {
        perMessage.set(seq, text);
      }
      deltas.set(messageId, perMessage);
    } else if (event.payloadSchemaRef === MESSAGE_COMPLETED_REF) {
      completed.set(messageId, String(payload.content_hash ?? ""));
    }
  }
  const messages: AssembledMessage[] = [];
  for (const messageId of started.keys()) {
    const perMessage = deltas.get(messageId);
    const content =
      perMessage === undefined ? "" : [...perMessage.keys()].sort((a, b) => a - b).map((seq) => perMessage.get(seq) ?? "").join("");
    messages.push({
      messageId,
      role: "assistant",
      content,
      contentHash: completed.get(messageId) ?? null,
      completed: completed.has(messageId),
    });
  }
  return messages;
}
