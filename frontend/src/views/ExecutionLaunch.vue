<script setup lang="ts">
import { ref } from "vue";
import { GroveApiClient } from "../api/client";

const emit = defineEmits<{ (event: "run-opened", runId: string): void }>();
const props = defineProps<{ runId: string | null; config: { baseUrl: string; gatewayToken: string; tenantId: string; principalId: string } }>();
const skillRef = ref("fixture.skill@1");
const question = ref("hello");
const result = ref<string | null>(null);
const error = ref<string | null>(null);

async function submit(): Promise<void> {
  error.value = null;
  result.value = null;
  try {
    const client = new GroveApiClient(props.config.baseUrl, {
      gatewayToken: props.config.gatewayToken,
      tenantId: props.config.tenantId,
      principalId: props.config.principalId,
    });
    const handle = await client.submitRun(skillRef.value, question.value);
    result.value = `run ${handle.runId}`;
    emit("run-opened", handle.runId);
  } catch (exc) {
    error.value = String(exc);
  }
}
</script>

<template>
  <section>
    <h2>Execution Launch</h2>
    <form @submit.prevent="submit">
      <label>API base <input v-model="config.baseUrl" placeholder="http://127.0.0.1:8000" /></label>
      <label>Gateway token <input v-model="config.gatewayToken" type="password" /></label>
      <label>Tenant <input v-model="config.tenantId" /></label>
      <label>Principal <input v-model="config.principalId" /></label>
      <label>Skill ref <input v-model="skillRef" /></label>
      <label>Question <input v-model="question" /></label>
      <button type="submit">Submit run</button>
    </form>
    <p v-if="result">Submitted: {{ result }}</p>
    <p v-if="error" class="error">{{ error }}</p>
  </section>
</template>
