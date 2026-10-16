import { onScopeDispose, ref, watch } from "vue"

const RENDER_INTERVAL_MS = 60

export function useStreamingMarkdown(props, render) {
  let renderInterval = RENDER_INTERVAL_MS
  function renderSnapshot(content) {
    const started = performance.now()
    const result = render(content)
    // Full parsing/highlighting of long replies can itself exceed 60ms. Leave
    // room for DOM commits and input between those expensive renders, using the
    // same throttle (no length threshold or second delayed stage).
    renderInterval = Math.max(RENDER_INTERVAL_MS, 2 * (performance.now() - started))
    return result
  }
  const html = ref(renderSnapshot(props.content))
  let lastRenderAt = performance.now()
  let timer = null

  function cancel() {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  function commit(content) {
    cancel()
    html.value = renderSnapshot(content)
    lastRenderAt = performance.now()
  }

  watch([() => props.content, () => props.streaming], ([content, streaming]) => {
    if (!streaming) {
      // Stop/completion bypasses the throttle, including the final partial window.
      commit(content)
      return
    }
    const remaining = renderInterval - (performance.now() - lastRenderAt)
    if (remaining <= 0) commit(content)
    else if (timer === null) {
      // Fixed trailing deadline: continuous updates never postpone a commit.
      // Read the latest complete snapshot rather than retaining the first one.
      timer = setTimeout(() => commit(props.content), remaining)
    }
  })

  onScopeDispose(cancel)
  return html
}
