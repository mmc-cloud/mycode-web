<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"

const props = defineProps({
  open: { type: Boolean, default: false },
  mode: { type: String, default: "confirm" },
  title: { type: String, required: true },
  message: { type: String, default: "" },
  detail: { type: String, default: "" },
  inputLabel: { type: String, default: "" },
  inputValue: { type: String, default: "" },
  confirmText: { type: String, default: "确认" },
  cancelText: { type: String, default: "取消" },
  danger: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  confirmDisabled: { type: Boolean, default: false },
})

const emit = defineEmits(["cancel", "confirm", "update:inputValue"])
const panel = ref(null)
const input = ref(null)
const cancelButton = ref(null)
let restoreFocus = null

function focusInitial() {
  nextTick(() => {
    if (!props.open) return
    if (props.mode === "input") input.value?.focus()
    else cancelButton.value?.focus()
  })
}

function handleTab(event) {
  const focusable = [...panel.value?.querySelectorAll(
    "button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex='-1'])",
  ) || []]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function handleCancel() {
  if (!props.busy) emit("cancel")
}

watch(() => props.open, (open) => {
  if (open) {
    restoreFocus = document.activeElement
    focusInitial()
  } else if (restoreFocus?.isConnected) {
    nextTick(() => restoreFocus?.focus())
    restoreFocus = null
  }
})

onMounted(() => {
  if (props.open) {
    restoreFocus = document.activeElement
    focusInitial()
  }
})

onBeforeUnmount(() => {
  if (restoreFocus?.isConnected) restoreFocus.focus()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog-backdrop" @click.self="handleCancel">
      <section
        ref="panel"
        class="app-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        @keydown.esc.stop.prevent="handleCancel"
        @keydown.tab="handleTab"
      >
        <h2>{{ title }}</h2>
        <p v-if="message" class="dialog-message">{{ message }}</p>
        <p v-if="detail" class="dialog-detail">{{ detail }}</p>
        <form v-if="mode === 'input'" class="dialog-form" @submit.prevent="emit('confirm')">
          <label>
            <span>{{ inputLabel }}</span>
            <input
              ref="input"
              :value="inputValue"
              :aria-label="inputLabel"
              @input="emit('update:inputValue', $event.target.value)"
            />
          </label>
          <div class="dialog-actions">
            <button ref="cancelButton" type="button" class="secondary" :disabled="busy" @click="handleCancel">{{ cancelText }}</button>
            <button type="submit" :disabled="busy || confirmDisabled" :class="{ danger }">{{ busy ? '处理中…' : confirmText }}</button>
          </div>
        </form>
        <div v-else class="dialog-actions">
          <button ref="cancelButton" type="button" class="secondary" :disabled="busy" @click="handleCancel">{{ cancelText }}</button>
          <button type="button" :disabled="busy || confirmDisabled" :class="{ danger }" @click="emit('confirm')">{{ busy ? '处理中…' : confirmText }}</button>
        </div>
      </section>
    </div>
  </Teleport>
</template>
