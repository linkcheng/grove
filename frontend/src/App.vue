<script setup lang="ts">
import { reactive, ref, computed, type Component } from "vue";
import ExecutionLaunch from "./views/ExecutionLaunch.vue";
import RunInteraction from "./views/RunInteraction.vue";
import HistoryInspect from "./views/HistoryInspect.vue";
import { assetRiskRendererRegistry } from "./model/assetRiskRenderers";

interface HistoryEntry {
  runId: string;
  submittedAt: string;
}

const view = ref<"launch" | "run" | "history">("launch");
const activeRunId = ref<string | null>(null);
// One shared gateway auth/config store: views never keep their own secrets.
const config = reactive({ baseUrl: "", gatewayToken: "", tenantId: "", principalId: "" });
// Session-local run history: the MVP command surface has no list-runs verb
// (submit/query only), so the launch view registers what this session
// submitted; Inspect still works for any run id typed by hand.
const history = ref<HistoryEntry[]>([]);
// The Business Profile owns the domain-view renderers (docs/31 §6); generic
// views only consume the registry and never guess payloads themselves.
const renderers = assetRiskRendererRegistry();
const components: Record<string, Component> = {
  launch: ExecutionLaunch,
  run: RunInteraction,
  history: HistoryInspect,
};
const current = computed(() => components[view.value]);

function openRun(runId: string): void {
  if (!history.value.some((entry) => entry.runId === runId)) {
    history.value = [{ runId, submittedAt: new Date().toISOString() }, ...history.value];
  }
  activeRunId.value = runId;
  view.value = "run";
}
</script>

<template>
  <header class="grove-header">
    <button :class="{ active: view === 'launch' }" @click="view = 'launch'">Execution Launch</button>
    <button :class="{ active: view === 'run' }" @click="view = 'run'">Run Interaction</button>
    <button :class="{ active: view === 'history' }" @click="view = 'history'">History / Inspect</button>
  </header>
  <main>
    <component
      :is="current"
      :run-id="activeRunId"
      :config="config"
      :renderers="renderers"
      :history="history"
      @run-opened="openRun"
    />
  </main>
</template>
