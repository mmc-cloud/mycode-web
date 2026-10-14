<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"

const props = defineProps({
  modelValue: { type: String, default: "" },
  disabled: { type: Boolean, default: false },
  sending: { type: Boolean, default: false },
})
const emit = defineEmits(["update:modelValue", "send"])
const input = ref(null)
const composing = ref(false)
const canSend = computed(() => !props.disabled && !composing.value && Boolean(props.modelValue.trim()))
let observer = null
let lastWidth = 0

function fitHeight() {
  const element = input.value
  if (!element) return
  // Reset before measuring so deletion and width changes can shrink the input.
  element.style.height = "0px"
  const style = getComputedStyle(element)
  const minimum = parseFloat(style.minHeight)
  const agent = element.closest(".agent-panel")
  // Keep room for reading when Terminal/Context make the panel short.
  const maximum = Math.min(parseFloat(style.maxHeight), Math.max(minimum, Math.floor((agent?.clientHeight || 600) * 0.3)))
  element.style.height = `${Math.min(maximum, Math.max(minimum, element.scrollHeight))}px`
  element.style.overflowY = element.scrollHeight > maximum ? "auto" : "hidden"
}

function send() {
  if (canSend.value) emit("send")
}

function keydown(event) {
  if (event.key !== "Enter" || !event.ctrlKey) return
  // Some IMEs report keyCode 229 even when isComposing is false.
  if (event.isComposing || composing.value || event.keyCode === 229) return
  event.preventDefault()
  if (!event.repeat) send()
}

watch(() => props.modelValue, () => nextTick(fitHeight))
onMounted(() => {
  fitHeight()
  observer = new ResizeObserver((entries) => {
    const width = input.value?.clientWidth || 0
    if (width === lastWidth && entries.every((entry) => entry.target === input.value)) return
    lastWidth = width
    fitHeight()
  })
  observer.observe(input.value)
  const agent = input.value.closest(".agent-panel")
  if (agent) observer.observe(agent)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <form class="composer" :aria-busy="sending" @submit.prevent="send">
    <textarea
      ref="input"
      :value="modelValue"
      :readonly="sending"
      rows="2"
      aria-label="消息输入"
      aria-describedby="composer-shortcut"
      placeholder="告诉 MyCode 要完成什么…"
      @input="emit('update:modelValue', $event.target.value)"
      @keydown="keydown"
      @compositionstart="composing = true"
      @compositionend="composing = false"
    />
    <div class="composer-footer">
      <span id="composer-shortcut" class="composer-hint">Ctrl + Enter 发送</span>
      <button type="submit" class="composer-send" aria-label="发送消息" title="发送消息 · Ctrl + Enter" :disabled="!canSend">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg>
      </button>
    </div>
  </form>
</template>
