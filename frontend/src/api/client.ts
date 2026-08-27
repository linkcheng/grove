// HTTP/SSE adapter for the RunInteractionModel transport seams.
// SSE is consumed via fetch streaming (not EventSource) because gateway
// authentication requires request headers EventSource cannot set.

import {
  type RunInteractionAdapter,
  type RunIntentDispatchResult,
  type RunUserIntent,
  type SnapshotBundle,
  type UIProjectionEvent,
} from "../model/types";
import { reduceRunView } from "../model/runInteractionModel";

export interface GroveAuth {
  gatewayToken: string;
  tenantId: string;
  principalId: string;
}

export class GroveApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly auth: GroveAuth,
  ) {}

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return {
      "Content-Type": "application/json",
      "X-Grove-Gateway-Auth": this.auth.gatewayToken,
      "X-Grove-Tenant-ID": this.auth.tenantId,
      "X-Grove-Principal-ID": this.auth.principalId,
      ...extra,
    };
  }

  async submitRun(skillRef: string, question: string): Promise<{ runId: string; commandId: string }> {
    const submissionId = crypto.randomUUID();
    const response = await fetch(`${this.baseUrl}/api/v1/executions/submit`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({
        submission_id: submissionId,
        intent: {
          intent_id: crypto.randomUUID(),
          skill_ref: skillRef,
          input: { question },
          constraints: {},
        },
      }),
    });
    const body = (await response.json()) as { data?: { run_id?: string; command_id?: string } };
    if (!response.ok || !body.data?.run_id || !body.data.command_id) {
      throw new Error(`submit failed: ${response.status}`);
    }
    return { runId: body.data.run_id, commandId: body.data.command_id };
  }

  async queryRun(runId: string): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}/api/v1/executions/runs/${runId}`, {
      headers: this.headers(),
    });
    const body = (await response.json()) as { data?: Record<string, unknown> };
    return body.data ?? {};
  }

  async loadEvents(runId: string, afterSeq: number, limit: number): Promise<UIProjectionEvent[]> {
    const response = await fetch(
      `${this.baseUrl}/api/v1/observations/runs/${runId}/ui?after_projection_seq=${afterSeq}&limit=${limit}`,
      { headers: this.headers() },
    );
    const body = (await response.json()) as { data?: { events?: UIApiEventView[] } };
    return (body.data?.events ?? []).map(fromApiEvent);
  }

  // Backfill then realtime tail using the durable projection cursor.
  streamEvents(
    runId: string,
    afterSeq: number,
    onEvent: (event: UIProjectionEvent) => void,
    onConnectionState?: (state: "connected" | "reconnecting") => void,
  ): () => void {
    const controller = new AbortController();
    void (async () => {
      let cursor = afterSeq;
      while (!controller.signal.aborted) {
        try {
          const response = await fetch(
            `${this.baseUrl}/api/v1/observations/runs/${runId}/ui/stream?after_projection_seq=${cursor}`,
            { headers: this.headers({ Accept: "text/event-stream" }), signal: controller.signal },
          );
          if (!response.ok || response.body === null) {
            throw new Error(`stream failed: ${response.status}`);
          }
          onConnectionState?.("connected");
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          for (;;) {
            const { done, value } = await reader.read();
            if (done) {
              break;
            }
            buffer += decoder.decode(value, { stream: true });
            let boundary = buffer.indexOf("\n\n");
            while (boundary >= 0) {
              const frame = buffer.slice(0, boundary);
              buffer = buffer.slice(boundary + 2);
              const dataLine = frame
                .split("\n")
                .find((line) => line.startsWith("data:"));
              if (dataLine !== undefined) {
                const event = fromApiEvent(JSON.parse(dataLine.slice(5).trim()) as UIApiEventView);
                cursor = Math.max(cursor, event.projectionSeq);
                onEvent(event);
              }
              boundary = buffer.indexOf("\n\n");
            }
          }
        } catch (error) {
          if (controller.signal.aborted) {
            return;
          }
          // Transport failure is distinct from a projection-sequence gap:
          // surface it, then retry after a bounded pause while the model's
          // reconnecting/backfill semantics own ordered recovery.
          onConnectionState?.("reconnecting");
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      }
    })();
    return () => controller.abort();
  }
}

interface UIApiEventView {
  event_id: string;
  tenant_id: string;
  target_ref: string;
  projection_seq: number;
  payload_schema_ref: string;
  payload: Record<string, unknown>;
}

function fromApiEvent(row: UIApiEventView): UIProjectionEvent {
  return {
    eventId: row.event_id,
    tenantId: row.tenant_id,
    targetRef: row.target_ref,
    projectionSeq: row.projection_seq,
    payloadSchemaRef: row.payload_schema_ref,
    payload: row.payload,
  };
}

export function interactionAdapter(client: GroveApiClient, runId: string): RunInteractionAdapter {
  return {
    async loadSnapshot(): Promise<SnapshotBundle> {
      const events = await client.loadEvents(runId, 0, 1000);
      return { view: reduceRunView(events), events };
    },
    loadBatch(afterSeq: number, limit: number) {
      return client.loadEvents(runId, afterSeq, limit);
    },
    async dispatch(intent: RunUserIntent): Promise<RunIntentDispatchResult> {
      // No interaction/action transport exists in the MVP command surface
      // (submit/query only); intents normalize to a stable rejection.
      void intent;
      return { outcome: "rejected", errorCode: "INTENT_TRANSPORT_UNAVAILABLE" };
    },
  };
}
