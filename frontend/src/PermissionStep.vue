<script setup>
defineProps({ step: { type: Object, required: true }, submissionState: { type: String, default: "" } })
defineEmits(["resolve"])
</script>

<template>
  <div class="permission-step" :data-status="step.status" :data-event-id="step.id">
    <div><span class="step-icon" aria-hidden="true">{{ step.status === 'waiting' ? '◷' : step.status === 'allowed' ? '✓' : step.status === 'denied' ? '!' : '·' }}</span><span>{{ step.label }}</span><small>{{ step.statusLabel }}</small></div>
    <p v-if="step.toolName || step.target" class="permission-target">{{ step.toolName }}<template v-if="step.toolName && step.target"> · </template>{{ step.target }}</p>
    <p>{{ step.detail }}</p>
    <details class="permission-details"><summary>请求详情</summary><pre>{{ JSON.stringify(step.sources, null, 2) }}</pre></details>
    <p v-if="step.actionable && submissionState" class="permission-feedback" role="status">{{ submissionState === 'submitting' ? '处理中…' : '已提交，等待权限状态同步…' }}</p>
    <div v-if="step.actionable" class="permission-actions" :aria-busy="Boolean(submissionState)">
      <button class="danger" :disabled="Boolean(submissionState)" title="拒绝此次操作" @click="$emit('resolve', 'deny')">拒绝</button>
      <button :disabled="Boolean(submissionState)" title="仅批准当前这一次操作" @click="$emit('resolve', 'once')">仅本次</button>
      <button :disabled="Boolean(submissionState)" title="本次 Agent 任务内后续需要确认的操作自动批准" @click="$emit('resolve', 'task')">当前任务</button>
      <button :disabled="Boolean(submissionState)" title="当前运行会话内后续需要确认的操作自动批准；Runtime 重启后失效" @click="$emit('resolve', 'session')">当前会话</button>
    </div>
  </div>
</template>
