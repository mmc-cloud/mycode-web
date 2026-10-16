<script setup>
import { onBeforeUnmount } from "vue"
import { renderMarkdown } from "./markdown"
import { useStreamingMarkdown } from "./useStreamingMarkdown.js"
import { finishStreamMeasure, startStreamMeasure } from "./streamingDebug.js"

const props = defineProps({
  content: { type: String, default: "" },
  streaming: { type: Boolean, default: false },
})
const html = useStreamingMarkdown(props, (content) => {
  const started = startStreamMeasure()
  const result = renderMarkdown(content)
  finishStreamMeasure("markdown", started)
  return result
})
const resetTimers = new Set()

function copyWithSelection(text) {
  const previousFocus = document.activeElement
  const textarea = document.createElement("textarea")
  textarea.value = text
  textarea.setAttribute("readonly", "")
  textarea.style.cssText = "position:fixed;left:-9999px;top:0"
  document.body.appendChild(textarea)
  try {
    textarea.select()
    if (!document.execCommand("copy")) throw new Error("Copy unavailable")
  } finally {
    textarea.remove()
    previousFocus?.focus({ preventScroll: true })
  }
}

async function copyCode(event) {
  const button = event.target.closest?.("button[data-copy-code]")
  if (!button || !event.currentTarget.contains(button) || button.disabled) return
  const code = button.closest(".markdown-code-block")?.querySelector("pre code")
  if (!code) return
  // Read textContent so highlighting, HTML entities and whitespace copy exactly.
  const text = code.textContent
  button.disabled = true
  let label = "已复制"
  try {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable")
      await navigator.clipboard.writeText(text)
    } catch (_) {
      copyWithSelection(text)
    }
  } catch (_) {
    label = "复制失败"
  } finally {
    if (button.isConnected) {
      button.disabled = false
      button.textContent = label
      button.setAttribute("aria-label", label === "已复制" ? "代码已复制" : "复制失败，请选择代码手动复制")
      const timer = window.setTimeout(() => {
        resetTimers.delete(timer)
        if (!button.isConnected) return
        button.textContent = "复制"
        button.setAttribute("aria-label", "复制代码")
      }, 2000)
      resetTimers.add(timer)
    }
  }
}

onBeforeUnmount(() => {
  resetTimers.forEach((timer) => window.clearTimeout(timer))
})
</script>

<template>
  <div class="message-content" @click="copyCode" v-html="html" />
</template>
