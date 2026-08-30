<script setup lang="ts">
import { ref } from "vue";
import { GroveApiClient } from "../api/client";
import { reduceRunView } from "../model/runInteractionModel";
import { assembleAnswerMessages } from "../model/answerText";
import type { RendererRegistry } from "../model/domainViewRenderer";

interface HistoryEntry {
  runId: string;
  submittedAt: string;
}

const props = defineProps<{
  runId: string | null;
  config: { baseUrl: string; gatewayToken: string; tenantId: string; principalId: string };
  renderers: RendererRegistry;
  history: HistoryEntry[];
}>();

const manualRunId = ref("");
const busy = ref(false);
const error = ref<string | null>(null);
const summary = ref<null | {
  runId: string;
  status: string | null;
  completeness: string;
  commands: { commandId: string; type: string; status: string }[];
  domainViews: ReturnType<RendererRegistry["render"]>[];
  answers: ReturnType<typeof assembleAnswerMessages>;
}>(null);

async function inspect(runId: string): Promise<void> {
  busy.value = true;
  error.value = null;
  summary.value = null;
  try {
    const client = new GroveApiClient(props.config.baseUrl, {
      gatewayToken: props.config.gatewayToken,
      tenantId: props.config.tenantId,
      principalId: props.config.principalId,
    });
    // The observation API caps one batch at 200 events; a run's family is far below that.
    const [run, events] = await Promise.all([client.queryRun(runId), client.loadEvents(runId, 0, 200)]);
    const view = reduceRunView(events);
    summary.value = {
      runId,
      status: view.status ?? String((run as Record<string, unknown>).status ?? "unknown"),
      completeness: view.completeness,
      commands: [],
      domainViews: view.domainViews.map((milestone) => props.renderers.render(milestone)),
      answers: assembleAnswerMessages(events),
    };
  } catch (exc) {
    error.value = String(exc);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section>
    <h2>History / Inspect</h2>

    <div v-if="props.history.length > 0" class="history">
      <h3>本会话提交的 Run</h3>
      <table>
        <thead>
          <tr><th>Run</th><th>提交时间</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in props.history" :key="entry.runId">
            <td class="run-id">{{ entry.runId }}</td>
            <td>{{ new Date(entry.submittedAt).toLocaleString() }}</td>
            <td><button type="button" :disabled="busy" @click="inspect(entry.runId)">Inspect</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <form class="manual" @submit.prevent="inspect(manualRunId)">
      <label>Run id <input v-model="manualRunId" placeholder="00000000-0000-0000-0000-000000000000" /></label>
      <button type="submit" :disabled="busy || manualRunId === ''">Inspect</button>
    </form>

    <p v-if="error" class="error">{{ error }}</p>

    <article v-if="summary" class="typed-summary">
      <h3>Run 摘要</h3>
      <dl>
        <dt>Run</dt><dd class="run-id">{{ summary.runId }}</dd>
        <dt>状态</dt><dd>{{ summary.status }}</dd>
        <dt>完整性</dt><dd>{{ summary.completeness }}</dd>
      </dl>
      <div v-if="summary.answers.length > 0" class="answers">
        <h4>评估答案</h4>
        <article v-for="message in summary.answers" :key="message.messageId">
          <pre class="answer-text">{{ message.content }}</pre>
          <p class="answer-meta">content hash {{ (message.contentHash ?? "").slice(0, 12) }}</p>
        </article>
      </div>
      <p v-else class="hint">该 run 尚无答案消息（未终态或被结构 gate 拒绝）。</p>
      <div v-if="summary.domainViews.length > 0" class="domain-views">
        <h4>Provenance</h4>
        <div v-for="entry in summary.domainViews" :key="entry.viewSchemaRef" class="domain-view">
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
    </article>
  </section>
</template>
