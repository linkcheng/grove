<script setup lang="ts">
import { ref } from "vue";
import { GroveApiClient } from "../api/client";

const props = defineProps<{
  runId: string | null;
  config: { baseUrl: string; gatewayToken: string; tenantId: string; principalId: string };
}>();
const runState = ref<Record<string, unknown> | null>(null);

async function inspect(): Promise<void> {
  if (props.runId === null) {
    return;
  }
  const client = new GroveApiClient(props.config.baseUrl, {
    gatewayToken: props.config.gatewayToken,
    tenantId: props.config.tenantId,
    principalId: props.config.principalId,
  });
  runState.value = await client.queryRun(props.runId);
}
</script>

<template>
  <section>
    <h2>History / Inspect</h2>
    <p v-if="runId === null">No run selected.</p>
    <template v-else>
      <form @submit.prevent="inspect">
        <button type="submit">Inspect</button>
        <span class="hint">uses the shared gateway config from Execution Launch</span>
      </form>
      <pre v-if="runState">{{ JSON.stringify(runState, null, 2) }}</pre>
    </template>
  </section>
</template>
