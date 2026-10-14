import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue"

const BOTTOM_TOLERANCE = 80

// Reading intent is changed only by input in the scroll viewport or the explicit
// jump action. Resize/programmatic scroll events only refresh its geometry.
export function useConsoleScroll(viewport, content) {
  const following = ref(true)
  const remaining = ref(0)
  const showJump = computed(() => !following.value && remaining.value > 1)
  let epoch = 0
  let frame = null
  let tickPending = false
  let observer = null
  let cleanup = null
  let manualUntil = 0
  let manualDirection = 0
  let manualHeight = 0
  let manualViewport = 0
  let draggingScrollbar = false
  let previousTop = 0
  let touchY = null

  function measure() {
    const element = viewport.value
    remaining.value = element ? Math.max(0, element.scrollHeight - element.clientHeight - element.scrollTop) : 0
  }

  function cancel() {
    if (frame !== null) cancelAnimationFrame(frame)
    frame = null
    tickPending = false
  }

  function schedule() {
    if (frame !== null) return
    const token = epoch
    frame = requestAnimationFrame(() => {
      frame = null
      if (token !== epoch) return
      const element = viewport.value
      if (element && following.value) {
        element.scrollTop = element.scrollHeight
        previousTop = element.scrollTop
      }
      measure()
    })
  }

  function afterUpdate() {
    if (tickPending) return
    tickPending = true
    const token = epoch
    nextTick(() => {
      if (token !== epoch) return
      tickPending = false
      schedule()
    })
  }

  function reset() {
    epoch += 1
    cancel()
    manualUntil = 0
    manualDirection = 0
    draggingScrollbar = false
    touchY = null
    previousTop = 0
    following.value = true
    remaining.value = 0
    afterUpdate()
  }

  function jump() {
    manualUntil = 0
    following.value = true
    schedule()
  }

  function intent(direction) {
    const element = viewport.value
    manualHeight = element?.scrollHeight || 0
    manualViewport = element?.clientHeight || 0
    manualDirection = direction
    manualUntil = performance.now() + 400
    if (direction < 0) {
      following.value = false
      cancel()
      measure()
    } else if (element && element.scrollHeight - element.clientHeight - element.scrollTop <= BOTTOM_TOLERANCE) {
      // A downward gesture at an already clamped bottom is still explicit intent.
      following.value = true
      schedule()
    }
  }

  function onScroll() {
    const element = viewport.value
    if (!element) return
    const delta = element.scrollTop - previousTop
    measure()
    // A plain scroll event (browser anchoring, clamping, focus, JS, resize)
    // cannot resume following. Require a current downward user gesture too.
    if (performance.now() < manualUntil && element.scrollHeight === manualHeight && element.clientHeight === manualViewport) {
      if (delta < -1 && (draggingScrollbar || manualDirection < 0)) following.value = false
      if (delta > 1 && (draggingScrollbar || manualDirection > 0) && remaining.value <= BOTTOM_TOLERANCE) {
        following.value = true
      }
      // Native scrollbar capture can swallow pointerup; let its intent expire.
      if (draggingScrollbar && delta) manualUntil = performance.now() + 400
    }
    previousTop = element.scrollTop
  }

  watch([viewport, content], ([element, inner]) => {
    cleanup?.()
    observer?.disconnect()
    cancel()
    epoch += 1
    if (!element || !inner) return
    previousTop = element.scrollTop
    const wheel = (event) => {
      // Internal code/table scrollers should not change the chat's intent.
      let target = event.target
      while (target && target !== element) {
        if (target.scrollHeight > target.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(target).overflowY)) return
        target = target.parentElement
      }
      if (event.deltaY) intent(Math.sign(event.deltaY))
    }
    const key = (event) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.target.closest("button, a, input, textarea, select, summary, [contenteditable='true']")) return
      if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) intent(-1)
      else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) intent(1)
      // Keep Home/End scoped to this viewport, rather than browser focus targets.
      if (event.target === element && event.key === "Home") {
        event.preventDefault()
        element.scrollTop = 0
      } else if (event.target === element && event.key === "End") {
        event.preventDefault()
        jump()
      }
    }
    const pointer = (event) => {
      const rect = element.getBoundingClientRect()
      if (event.target === element && event.clientX >= Math.min(rect.left + element.clientWidth, rect.right - 14)) {
        draggingScrollbar = true
        manualUntil = performance.now() + 1000
        manualHeight = element.scrollHeight
        manualViewport = element.clientHeight
        previousTop = element.scrollTop
      }
    }
    const release = () => { draggingScrollbar = false; manualUntil = 0 }
    const touchStart = (event) => { touchY = event.touches[0]?.clientY ?? null }
    const touchMove = (event) => {
      const nextY = event.touches[0]?.clientY
      if (touchY !== null && nextY !== undefined && nextY !== touchY) intent(Math.sign(touchY - nextY))
      touchY = nextY ?? null
    }
    element.addEventListener("scroll", onScroll, { passive: true })
    element.addEventListener("wheel", wheel, { passive: true })
    element.addEventListener("keydown", key)
    element.addEventListener("pointerdown", pointer)
    element.addEventListener("touchstart", touchStart, { passive: true })
    element.addEventListener("touchmove", touchMove, { passive: true })
    window.addEventListener("pointerup", release)
    window.addEventListener("pointercancel", release)
    cleanup = () => {
      element.removeEventListener("scroll", onScroll)
      element.removeEventListener("wheel", wheel)
      element.removeEventListener("keydown", key)
      element.removeEventListener("pointerdown", pointer)
      element.removeEventListener("touchstart", touchStart)
      element.removeEventListener("touchmove", touchMove)
      window.removeEventListener("pointerup", release)
      window.removeEventListener("pointercancel", release)
    }
    // Observe the actual Markdown wrapper AND viewport, once per DOM binding.
    // The renderer's delayed 120ms commits therefore get their own follow pass.
    observer = new ResizeObserver(schedule)
    observer.observe(inner)
    observer.observe(element)
    afterUpdate()
  }, { flush: "post" })

  onBeforeUnmount(() => {
    epoch += 1
    cancel()
    observer?.disconnect()
    cleanup?.()
  })
  return { showJump, afterUpdate, reset, jump }
}
