<script setup>
import { reactive } from "vue"
import PermissionStep from "./PermissionStep.vue"

defineProps({ group: { type: Object, required: true }, permissionSubmission: { type: String, default: "" } })
defineEmits(["toggle", "resolve"])
const detailChoices = reactive({})
const isOpen = (step) => detailChoices[step.id] ?? step.status === "failed"
const icons = { success: "✓", failed: "!", running: "◌", unknown: "·", info: "·" }
function toggleDetail(step) { detailChoices[step.id] = !isOpen(step) }
</script>

<template>
  <button class="execution-header" :data-status="group.status" :aria-expanded="group.expanded" :aria-controls="`execution-${group.key}`" @click="$emit('toggle', group)">
    <span>{{ group.expanded ? '▼' : '▶' }} 执行过程<template v-if="group.blockCount > 1">（第 {{ group.blockNumber }}/{{ group.blockCount }} 段）</template></span><span title="步骤包含工具、运行提示和权限请求；一次工具调用及结果共计两步">{{ group.summary }}</span>
  </button>
  <div v-show="group.expanded" :id="`execution-${group.key}`" class="execution-content">
    <template v-for="step in group.timeline" :key="step.id">
      <PermissionStep v-if="step.kind === 'permission' && (group.expanded || !step.actionable)" :step="step" :submission-state="permissionSubmission" @resolve="$emit('resolve', $event)" />
      <details v-else-if="step.kind !== 'permission'" class="execution-step" :class="{ 'runtime-step': step.kind === 'runtime', 'error-card': step.kind === 'error' }" :data-status="step.status" :data-event-id="step.id" :data-kind="step.kind" :open="isOpen(step)">
        <summary @click.prevent="toggleDetail(step)">
          <span class="step-icon" aria-hidden="true">{{ icons[step.status] }}</span>
          <span class="step-heading"><span>{{ step.label }}</span><span v-if="step.target" class="step-target" :title="step.target">{{ step.target }}</span><span v-if="step.preview" class="runtime-preview">{{ step.preview }}</span></span>
          <small>{{ step.statusLabel || 'Failed' }}</small>
        </summary>
        <p v-if="step.error" class="step-error">{{ step.error }}</p>
        <div class="step-details">
          <p v-if="step.toolName" class="step-tool-name">Tool: {{ step.toolName }}</p>
          <template v-if="step.arguments !== null && step.arguments !== undefined"><h3>Arguments</h3><pre>{{ step.arguments }}</pre></template>
          <template v-if="step.result !== null && step.result !== undefined"><h3>Result</h3><pre>{{ step.result }}</pre></template>
          <pre v-if="step.kind !== 'tool'">{{ step.detail }}</pre>
          <p v-if="step.association" class="step-association">关联依据：{{ step.association }}；记录按调用位置展示，结果更新该记录的状态。</p>
          <details class="step-source"><summary>原始事件（后端可能截断）</summary><pre class="step-data">{{ JSON.stringify(step.sources, null, 2) }}</pre></details>
        </div>
      </details>
    </template>
    <article v-if="group.live && group.live.kind !== 'assistant'" class="console-card live-card" :data-kind="group.live.kind"><strong>{{ group.live.kind }}</strong><pre>{{ group.live.content }}</pre></article>
  </div>
  <!-- A new permission must remain operable even after a manual collapse. -->
  <PermissionStep v-if="!group.expanded && group.actionablePermission" :step="group.actionablePermission" :submission-state="permissionSubmission" @resolve="$emit('resolve', $event)" />
</template>
