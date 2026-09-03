<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { FitAddon } from "@xterm/addon-fit"
import { Terminal } from "@xterm/xterm"
import "@xterm/xterm/css/xterm.css"

import { sessionApiPath, websocketUrl } from "./api"

const props = defineProps({
  sessionId: { type: String, default: "" },
  collapsed: { type: Boolean, default: false },
})

const terminalElement = ref(null)
const status = ref("closed")
const notice = ref("")
let terminal = null
let fitAddon = null
let socket = null
let resizeObserver = null
let dataDisposable = null
let resizeDisposable = null
let generation = 0
let retryTimer = null
let retryCount = 0
const MAX_RECONNECT_ATTEMPTS = 6
const RECONNECT_DELAYS = [500, 1000, 2000, 3000, 3000, 3000]

const statusLabels = {
  starting: "starting",
  queued: "queued",
  ready: "ready",
  reconnecting: "reconnecting",
  closed: "closed",
  error: "error",
}

function send(payload) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload))
}

function sendResize() {
  if (!terminal) return
  send({ type: "resize", cols: terminal.cols, rows: terminal.rows })
}

function fit() {
  if (!fitAddon || !terminalElement.value) return
  if (terminalElement.value.clientWidth === 0 || terminalElement.value.clientHeight === 0) return
  fitAddon.fit()
  sendResize()
}

function destroyTerminal() {
  generation += 1
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer)
    retryTimer = null
  }
  resizeObserver?.disconnect()
  resizeObserver = null
  dataDisposable?.dispose()
  resizeDisposable?.dispose()
  dataDisposable = null
  resizeDisposable = null
  if (socket) {
    socket.onclose = null
    socket.close()
    socket = null
  }
  terminal?.dispose()
  terminal = null
  fitAddon = null
}

function scheduleReconnect(token) {
  if (generation !== token) return
  if (retryCount >= MAX_RECONNECT_ATTEMPTS) {
    status.value = "error"
    notice.value = "Terminal 重连次数已达上限，请手动重试"
    return
  }
  const delay = RECONNECT_DELAYS[retryCount]
  retryCount += 1
  status.value = "reconnecting"
  notice.value = `连接中断，${delay}ms 后重试（${retryCount}/${MAX_RECONNECT_ATTEMPTS}）`
  retryTimer = window.setTimeout(() => {
    retryTimer = null
    if (generation === token) void connect(false)
  }, delay)
}

async function connect(resetRetries = true) {
  destroyTerminal()
  if (resetRetries) retryCount = 0
  if (!props.sessionId || props.collapsed) {
    status.value = "closed"
    return
  }
  await nextTick()
  if (!terminalElement.value) return
  const token = generation
  terminal = new Terminal({
    cursorBlink: true,
    convertEol: true,
    scrollback: 5000,
    fontSize: 13,
    fontFamily: '"Cascadia Code", Consolas, monospace',
    theme: {
      background: "#101612",
      foreground: "#dce7dc",
      cursor: "#b8dfbd",
      selectionBackground: "#36553f",
    },
  })
  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(terminalElement.value)
  status.value = "starting"
  notice.value = ""
  fit()
  dataDisposable = terminal.onData((data) => send({ type: "input", data }))
  resizeDisposable = terminal.onResize(({ cols, rows }) =>
    send({ type: "resize", cols, rows }),
  )
  resizeObserver = new ResizeObserver(fit)
  resizeObserver.observe(terminalElement.value)

  socket = new WebSocket(
    websocketUrl(sessionApiPath(props.sessionId, "/terminal")),
  )
  socket.addEventListener("open", () => {
    if (generation !== token) return
    retryCount = 0
    status.value = "starting"
    notice.value = ""
    sendResize()
  })
  socket.addEventListener("message", (event) => {
    if (generation !== token) return
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === "output") terminal?.write(payload.data || "")
      if (payload.type === "status") {
        status.value = payload.status || "error"
        notice.value = payload.message || ""
        if (payload.status === "ready") {
          fit()
          terminal?.focus()
        }
      }
    } catch (_) {
      notice.value = "Terminal 消息解析失败"
    }
  })
  socket.addEventListener("error", () => {
    if (generation === token) {
      notice.value = "Terminal 连接中断，准备重连…"
    }
  })
  socket.addEventListener("close", (event) => {
    if (generation !== token) return
    socket = null
    if (
      event.code === 1000 ||
      event.code === 1008 ||
      status.value === "error" ||
      status.value === "closed"
    ) {
      if (event.code === 1000 || event.code === 1008) status.value = "closed"
      return
    }
    scheduleReconnect(token)
  })
}

watch(() => props.sessionId, () => void connect())
watch(() => props.collapsed, (collapsed) => {
  if (collapsed) destroyTerminal()
  else void connect()
})

onMounted(() => void connect())
onBeforeUnmount(destroyTerminal)
</script>

<template>
  <div class="terminal-panel-body">
    <div class="terminal-toolbar">
      <span class="terminal-caption">bash · /workspace</span>
      <span class="terminal-status" :data-status="status">{{ statusLabels[status] || status }}</span>
      <span v-if="notice" class="terminal-notice">{{ notice }}</span>
    </div>
    <div ref="terminalElement" class="xterm-host" aria-label="Web Terminal" />
  </div>
</template>
