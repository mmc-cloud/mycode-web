<script setup>
const props = defineProps({
  activeView: { type: String, required: true },
  sidebarOpen: { type: Boolean, required: true },
})
defineEmits(["select"])

const views = [{ id: "sessions", label: "Sessions" }, { id: "workspace", label: "Workspace" }]
const buttons = []

function focusView(view) {
  buttons[views.findIndex((item) => item.id === view)]?.focus()
}

function moveFocus(event, index) {
  const offsets = { ArrowDown: 1, ArrowUp: -1, Home: -index, End: views.length - 1 - index }
  if (!(event.key in offsets)) return
  event.preventDefault()
  buttons[(index + offsets[event.key] + views.length) % views.length]?.focus()
}

defineExpose({ focusView })
</script>

<template>
  <nav class="activity-bar" aria-label="工作区导航">
    <button
      v-for="(view, index) in views"
      :key="view.id"
      :ref="(element) => { buttons[index] = element }"
      type="button"
      class="activity-button"
      :class="{ active: props.sidebarOpen && props.activeView === view.id }"
      :aria-label="view.label"
      :aria-describedby="`activity-tooltip-${view.id}`"
      aria-controls="workbench-sidebar"
      :aria-expanded="props.sidebarOpen && props.activeView === view.id"
      :aria-pressed="props.sidebarOpen && props.activeView === view.id"
      @click="$emit('select', view.id)"
      @keydown="moveFocus($event, index)"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
        <path v-if="view.id === 'sessions'" d="M4 4h16v12H9l-5 4V4Zm4 4h8M8 12h5" />
        <path v-else d="M8 3h8l4 4v14H8V3Zm8 0v5h4M4 7v14" />
      </svg>
      <span :id="`activity-tooltip-${view.id}`" class="activity-tooltip" role="tooltip">{{ view.label }}</span>
    </button>
  </nav>
</template>
