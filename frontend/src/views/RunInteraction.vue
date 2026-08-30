<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { GroveApiClient, interactionAdapter } from "../api/client";
import { RunInteractionModel } from "../model/runInteractionModel";
import { type RendererRegistry } from "../model/domainViewRenderer";
import { assembleAnswerMessages } from "../model/answerText";
import type { InteractionSnapshot, UIProjectionEvent } from "../model/types";

const props = defineProps<{
  runId: string | null;
  config: { baseUrl: string; gatewayToken: string; tenantId: string; principalId: string };
  renderers: RendererRegistry;
}>();

const snapshot = ref<InteractionSnapshot | null>(null);
const transport = ref<"idle" | "connected" | "reconnecting">("idle");
const unknownSchemas = ref<string[]>([]);
const dispatchOutcome = ref<string | null>(null);
const answerEvents = ref<UIProjectionEvent[]>([]);
let model: RunInteractionModel | null = null;
let stopStream: (() => void) | null = null;

const pendingInteractions = computed(
  () => snapshot.value?.view.interactions.filter((item) => item.status === "pending") ?? [],
);

const renderedDomainViews = computed(() =>
  (snapshot.value?.view.domainViews ?? []).map((milestone) => props.renderers.render(milestone)),
);

const answerMessages = computed(() => assembleAnswerMessages(answerEvents.value));

async function open(): Promise<void> {
  close();
  if (props.runId === null) {
    return;
  }
  const client = new GroveApiClient(props.config.baseUrl, {
    gatewayToken: props.config.gatewayToken,
    tenantId: props.config.tenantId,
    principalId: props.config.principalId,
  });
  model = new RunInteractionModel(interactionAdapter(client, props.runId), {
    unknownSchemaSink: (ref) => unknownSchemas.value.push(ref),
  });
  model.subscribe(() => {
    snapshot.value = model?.getSnapshot() ?? null;
  });
  await model.open();
  snapshot.value = model.getSnapshot();
  const backfill = await client.loadEvents(props.runId, 0, 200);
  answerEvents.value = [...backfill];
  stopStream = client.streamEvents(
    props.runId,
    model.getSnapshot().cursor,
    (event) => {
      void model?.applyEvent(event);
      answerEvents.value = [...answerEvents.value, event];
    },
    (state) => {
      transport.value = state;
    },
  );
}

async function respond(interactionId: string): Promise<void> {
  if (model === null) {
    return;
  }
  const result = await model.dispatch({
    kind: "respond_to_interrupt",
    interactionId,
    responsePayloadRef: `response:${interactionId}`,
  });
  dispatchOutcome.value = `${result.outcome}${result.errorCode ? ` (${result.errorCode})` : ""}`;
}

function close(): void {
  stopStream?.();
  stopStream = null;
  model?.close();
  model = null;
  snapshot.value = null;
  transport.value = "idle";
  unknownSchemas.value = [];
  dispatchOutcome.value = null;
  answerEvents.value = [];
}

onMounted(() => {
  void open();
});
watch(
  () => props.runId,
  () => {
    void open();
  },
);
onBeforeUnmount(close);
</script>

<template>
  <section>
    <h2>Run Interaction</h2>
    <p v-if="runId === null">No run selected — submit one from Execution Launch.</p>
    <template v-else>
      <p>run {{ runId }}</p>
      <div v-if="snapshot" class="state">
        <span class="badge" :class="{ reconnecting: snapshot.reconnecting }">
          {{ snapshot.reconnecting ? "reconnecting (gap backfill)" : "ordered" }}
        </span>
        <span class="badge" :class="transport">stream: {{ transport }}</span>
        <span class="badge">{{ snapshot.view.completeness }}</span>
        <span class="badge">{{ snapshot.view.status ?? "unknown status" }}</span>
        <span class="badge">cursor {{ snapshot.cursor }}</span>
      </div>
      <div v-if="answerMessages.length > 0" class="answers">
        <h3>评估答案</h3>
        <article v-for="message in answerMessages" :key="message.messageId" class="answer">
          <pre class="answer-text">{{ message.content }}</pre>
          <p class="answer-meta">
            {{ message.completed ? "content hash " + (message.contentHash ?? "").slice(0, 12) : "streaming…" }}
          </p>
        </article>
      </div>
      <div v-if="pendingInteractions.length > 0" class="pending">
        <h3>Pending interactions</h3>
        <div v-for="item in pendingInteractions" :key="item.interactionId" class="interaction">
          <span>{{ item.kind }} · revision {{ item.revision }}</span>
          <button type="button" @click="respond(item.interactionId)">Respond</button>
        </div>
        <p v-if="dispatchOutcome" class="dispatch">dispatch: {{ dispatchOutcome }}</p>
      </div>
      <div v-if="renderedDomainViews.length > 0" class="domain-views">
        <h3>Domain views</h3>
        <div v-for="entry in renderedDomainViews" :key="entry.viewSchemaRef" class="domain-view">
          <template v-if="entry.kind === 'rendered'">
            <p class="milestone">{{ entry.title }} · {{ entry.shortResultHash }}</p>
            <dl>
              <template v-for="field in entry.fields" :key="field.kind">
                <dt>{{ field.label }}</dt>
                <dd>{{ field.value }}</dd>
              </template>
            </dl>
          </template>
          <p v-else class="partial">partial · unknown view schema {{ entry.viewSchemaRef }}</p>
        </div>
      </div>
      <p v-if="unknownSchemas.length > 0" class="partial">
        {{ unknownSchemas.length }} unknown schema event(s) marked partial
      </p>
    </template>
  </section>
</template>
