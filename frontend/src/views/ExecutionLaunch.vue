<script setup lang="ts">
import { ref } from "vue";
import { GroveApiClient } from "../api/client";

const emit = defineEmits<{ (event: "run-opened", runId: string): void }>();
const props = defineProps<{ runId: string | null; config: { baseUrl: string; gatewayToken: string; tenantId: string; principalId: string } }>();
const skillRef = ref("fixture.skill@1");
const question = ref("对本租户当前资产组合进行风险评估");
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
      <label>Question <textarea v-model="question" rows="3" /></label>
      <button type="submit">Submit run</button>
    </form>
    <p class="hint">
      评估范围为该租户当前的全部资产组合（由运维通过 README 的种子 SQL 维护）；
      提交后 Run 视图实时展示执行进度与答案。
    </p>
    <p v-if="result">Submitted: {{ result }}</p>
    <p v-if="error" class="error">{{ error }}</p>
  </section>
</template>
