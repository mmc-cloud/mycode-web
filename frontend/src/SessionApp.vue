<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue"

import FileTree from "./FileTree.vue"
import TerminalPanel from "./TerminalPanel.vue"
import { API_BASE, sessionApiPath } from "./api"

const LAYOUT_KEY = "mycode.layout.v1"
const layoutDefaults = {
  sessionsWidth: 220,
  workspaceWidth: 390,
  workspaceTreeHeight: 260,
  terminalHeight: 280,
  sessionsCollapsed: false,
  workspaceCollapsed: false,
  terminalCollapsed: false,
}

function defaultLayout() {
  return { ...layoutDefaults }
}

function loadLayout() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(LAYOUT_KEY) || "")
    if (
      !parsed ||
      !Number.isFinite(parsed.sessionsWidth) ||
      parsed.sessionsWidth < 160 || parsed.sessionsWidth > 360 ||
      !Number.isFinite(parsed.workspaceWidth) ||
      parsed.workspaceWidth < 260 || parsed.workspaceWidth > 720 ||
      !Number.isFinite(parsed.workspaceTreeHeight) ||
      parsed.workspaceTreeHeight < 120 || parsed.workspaceTreeHeight > 600 ||
      !Number.isFinite(parsed.terminalHeight) ||
      parsed.terminalHeight < 160 || parsed.terminalHeight > 560 ||
      typeof parsed.sessionsCollapsed !== "boolean" ||
      typeof parsed.workspaceCollapsed !== "boolean" ||
      typeof parsed.terminalCollapsed !== "boolean"
    ) throw new Error("invalid layout")
    return { ...layoutDefaults, ...parsed }
  } catch (_) {
    return defaultLayout()
  }
}

const layout = reactive(loadLayout())
const layoutStyle = computed(() => ({
  "--sessions-width": `${layout.sessionsCollapsed ? 48 : layout.sessionsWidth}px`,
  "--workspace-width": `${layout.workspaceCollapsed ? 48 : layout.workspaceWidth}px`,
  "--terminal-height": `${layout.terminalCollapsed ? 44 : layout.terminalHeight}px`,
  "--workspace-tree-height": `${layout.workspaceTreeHeight}px`,
}))

function saveLayout() {
  try { window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)) } catch (_) {}
}

const sessions = ref([])
const currentSession = ref(null)
const displayName = ref("")
const settingsOpen = ref(false)
const sessionMenuId = ref(null)
const entries = ref([])
const selectedPath = ref("")
const fileContent = ref("")
const consoleEvents = ref([])
const liveConsole = ref(null)
const permission = ref(null)
const turnStates = ref({})
const expandedGroups = ref({})
const message = ref("")
const error = ref("")
const lifecycleNotice = ref("")
const outputElement = ref(null)
let eventSource = null
let workspaceTimer = null
let generation = 0
let resizeState = null

const sendDisabled = computed(() =>
  ["starting", "queued", "running", "waiting_permission"].includes(
    currentSession.value?.runtime_status,
  ),
)
const executionGroups = computed(() => buildExecutionGroups(
  consoleEvents.value,
  liveConsole.value,
  permission.value,
  currentSession.value,
  turnStates.value,
  expandedGroups.value,
))

async function request(path, options = {}) {
  const response = await fetch(API_BASE + path, options)
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try { detail = (await response.json()).detail || detail } catch (_) {}
    const reason = new Error(detail)
    reason.status = response.status
    throw reason
  }
  if (response.status === 204) return null
  const type = response.headers.get("content-type") || ""
  return type.includes("application/json") ? response.json() : response
}

function scoped(path = "", sessionId = currentSession.value?.id) {
  if (!sessionId) throw new Error("请先选择会话")
  return sessionApiPath(sessionId, path)
}

async function initialize() {
  try {
    const result = await request("/sessions")
    displayName.value = result.display_name || ""
    sessions.value = result.sessions
    if (!sessions.value.length) sessions.value = [await request("/sessions", { method: "POST" })]
    const requested = new URLSearchParams(window.location.search).get("session")
    const target = sessions.value.find((item) => item.id === requested) || sessions.value[0]
    await openSession(target.id, true)
  } catch (reason) { showError(reason) }
}

async function openSession(sessionId, replace = false) {
  const token = ++generation
  eventSource?.close()
  eventSource = null
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
  const listed = sessions.value.find((item) => item.id === sessionId)
  currentSession.value = listed || { id: sessionId, name: null, runtime_status: "stopped", active_turn_id: null }
  entries.value = []
  selectedPath.value = ""
  fileContent.value = ""
  consoleEvents.value = []
  liveConsole.value = null
  permission.value = null
  turnStates.value = {}
  const url = new URL(window.location.href)
  url.searchParams.set("session", sessionId)
  window.history[replace ? "replaceState" : "pushState"]({}, "", url)
  const metadata = loadMetadata(sessionId, token)
  const tree = refreshTree(sessionId, token)
  const history = loadConsole(sessionId, token)
  void request(scoped("/activate", sessionId), { method: "POST" })
    .then((result) => {
      if (generation === token && currentSession.value?.id === sessionId) {
        currentSession.value.runtime_status = result.status
      }
    })
    .catch(showError)
  await Promise.all([metadata, tree, history])
}

async function loadMetadata(sessionId, token) {
  const result = await request(scoped("", sessionId))
  if (generation !== token) return
  currentSession.value = result
  permission.value = result.pending_permission
  if (result.active_turn_id) {
    turnStates.value[result.active_turn_id] = { status: result.runtime_status }
  }
  const index = sessions.value.findIndex((item) => item.id === sessionId)
  if (index >= 0) sessions.value[index] = result
}

async function loadConsole(sessionId, token) {
  const result = await request(scoped("/console", sessionId))
  if (generation !== token) return
  const merged = new Map(result.events.map((event) => [event.id, event]))
  for (const event of consoleEvents.value) merged.set(event.id, event)
  consoleEvents.value = [...merged.values()].sort((left, right) => left.id - right.id)
  connectEvents(sessionId, token, result.event_cursor)
}

async function createSession() {
  try {
    const created = await request("/sessions", { method: "POST" })
    sessions.value.unshift(created)
    await openSession(created.id)
  } catch (reason) { showError(reason) }
}

async function renameSession(session) {
  const name = window.prompt("会话名称", session.name || sessionLabel(session))
  if (name === null || !name.trim()) return
  try {
    const updated = await request(scoped("", session.id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    })
    const index = sessions.value.findIndex((item) => item.id === session.id)
    if (index >= 0) sessions.value[index] = updated
    if (currentSession.value?.id === session.id) currentSession.value = updated
    sessionMenuId.value = null
  } catch (reason) { showError(reason) }
}

async function removeSession(session) {
  sessionMenuId.value = null
  if (!window.confirm("确认删除这个会话？Workspace、MyCode state 和 Console history 将一并删除。")) return
  try {
    await request(scoped("", session.id), { method: "DELETE" })
    sessions.value = sessions.value.filter((item) => item.id !== session.id)
    if (currentSession.value?.id !== session.id) return
    if (!sessions.value.length) sessions.value = [await request("/sessions", { method: "POST" })]
    await openSession(sessions.value[0].id, true)
  } catch (reason) { showError(reason) }
}

function applyRuntimeStatus(data) {
  if (!currentSession.value) return
  currentSession.value.runtime_status = data.status
  if (!data.turn_id) return
  turnStates.value[data.turn_id] = { status: data.status }
  if (["starting", "queued", "running", "waiting_permission"].includes(data.status)) {
    currentSession.value.active_turn_id = data.turn_id
  } else if (currentSession.value.active_turn_id === data.turn_id) {
    currentSession.value.active_turn_id = null
  }
}

function connectEvents(sessionId, token, after) {
  eventSource = new EventSource(
    `${API_BASE}${scoped("/events", sessionId)}?after=${encodeURIComponent(after)}`,
  )
  eventSource.addEventListener("runtime_status", (event) => {
    if (generation === token) applyRuntimeStatus(JSON.parse(event.data))
  })
  eventSource.addEventListener("console_event", (event) => {
    if (generation !== token) return
    liveConsole.value = null
    upsertConsole(JSON.parse(event.data))
  })
  eventSource.addEventListener("console_live", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    liveConsole.value = data.active ? data : null
    scrollConsole()
  })
  eventSource.addEventListener("permission_request", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    permission.value = data
    if (data.turn_id) turnStates.value[data.turn_id] = { status: "waiting_permission" }
  })
  eventSource.addEventListener("permission_resolved", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    permission.value = null
    if (data.turn_id) {
      const status = currentSession.value?.runtime_status === "idle"
        ? "idle"
        : "running"
      turnStates.value[data.turn_id] = { status }
    }
  })
  eventSource.addEventListener("workspace_changed", () => {
    if (generation !== token) return
    if (workspaceTimer) window.clearTimeout(workspaceTimer)
    workspaceTimer = window.setTimeout(() => refreshWorkspaceAfterChange(sessionId, token), 250)
  })
  eventSource.addEventListener("runtime_expired", (event) => {
    if (generation !== token) return
    currentSession.value.runtime_status = "stopped"
    lifecycleNotice.value = JSON.parse(event.data).message || "Sandbox 已停止，会话数据已保留"
    window.setTimeout(() => { lifecycleNotice.value = "" }, 8000)
  })
  eventSource.addEventListener("error", (event) => {
    if (generation === token && event.data) showError(JSON.parse(event.data).message || "运行时错误")
  })
}

function upsertConsole(event) {
  const normalized = {
    id: event.console_id ?? event.id,
    kind: event.kind,
    content: event.content,
    data: event.data || {},
    created_at: event.created_at,
  }
  const index = consoleEvents.value.findIndex((item) => item.id === normalized.id)
  if (index >= 0) consoleEvents.value[index] = normalized
  else consoleEvents.value.push(normalized)
  scrollConsole()
}

function scrollConsole() {
  nextTick(() => {
    if (outputElement.value) outputElement.value.scrollTop = outputElement.value.scrollHeight
  })
}

async function refreshWorkspaceAfterChange(sessionId, token) {
  await refreshTree(sessionId, token)
  if (!selectedPath.value || generation !== token) return
  try { await openFile(selectedPath.value, sessionId, token) } catch (_) {}
}

async function saveProfile() {
  try {
    const result = await request("/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: displayName.value }),
    })
    displayName.value = result.display_name || ""
  } catch (reason) { showError(reason) }
}

async function refreshTree(sessionId = currentSession.value?.id, token = generation) {
  if (!sessionId) return
  try {
    const result = await request(scoped("/files/tree", sessionId))
    if (generation === token) entries.value = result.entries
  } catch (reason) { if (generation === token) showError(reason) }
}

async function upload(event, archive) {
  const file = event.target.files?.[0]
  if (!file) return
  const form = new FormData()
  form.append("upload", file)
  form.append("archive", String(archive))
  try {
    await request(scoped("/files/upload"), { method: "POST", body: form })
    await refreshTree()
  } catch (reason) { showError(reason) }
  finally { event.target.value = "" }
}

async function openFile(path, sessionId = currentSession.value?.id, token = generation) {
  try {
    const result = await request(`${scoped("/files/content", sessionId)}?path=${encodeURIComponent(path)}`)
    if (generation === token) {
      selectedPath.value = path
      fileContent.value = result.content
    }
  } catch (reason) {
    if (reason.status === 404 && generation === token) {
      selectedPath.value = ""
      fileContent.value = ""
      return
    }
    showError(reason)
  }
}

async function deleteEntry(entry) {
  const suffix = entry.kind === "directory" ? "？目录内容将递归删除。" : "？"
  if (!window.confirm(`确认删除 ${entry.name}${suffix}`)) return
  try {
    await request(`${scoped("/files")}?path=${encodeURIComponent(entry.path)}`, { method: "DELETE" })
    if (selectedPath.value === entry.path || selectedPath.value.startsWith(`${entry.path}/`)) {
      selectedPath.value = ""
      fileContent.value = ""
    }
    await refreshTree()
  } catch (reason) { showError(reason) }
}

async function sendMessage() {
  const content = message.value.trim()
  if (!content) return
  try {
    const result = await request(scoped("/message"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    })
    currentSession.value.runtime_status = result.status
    currentSession.value.active_turn_id = result.turn_id
    turnStates.value[result.turn_id] = { status: result.status }
    message.value = ""
  } catch (reason) { showError(reason) }
}

async function resolvePermission(allow) {
  try {
    await request(scoped("/permission"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow }),
    })
  } catch (reason) { showError(reason) }
}

function toggleGroup(group) {
  expandedGroups.value[group.key] = !group.expanded
}

function showError(reason) {
  error.value = reason instanceof Error ? reason.message : String(reason)
  window.setTimeout(() => { error.value = "" }, 8000)
}

function sessionLabel(session) {
  if (session.name) return session.name
  const index = sessions.value.findIndex((item) => item.id === session.id)
  return `Session ${index < 0 ? "" : sessions.value.length - index}`
}

function handleHistoryNavigation() {
  const sessionId = new URLSearchParams(window.location.search).get("session")
  if (sessionId && sessionId !== currentSession.value?.id && sessions.value.some((session) => session.id === sessionId)) {
    void openSession(sessionId, true)
  }
}

function togglePanel(name) {
  layout[`${name}Collapsed`] = !layout[`${name}Collapsed`]
  saveLayout()
}

function startResize(kind, event) {
  if (event.button !== 0) return
  event.preventDefault()
  resizeState = {
    kind,
    startX: event.clientX,
    startY: event.clientY,
    initial: {
      sessionsWidth: layout.sessionsWidth,
      workspaceWidth: layout.workspaceWidth,
      workspaceTreeHeight: layout.workspaceTreeHeight,
      terminalHeight: layout.terminalHeight,
    },
  }
  window.addEventListener("pointermove", resize)
  window.addEventListener("pointerup", stopResize, { once: true })
}

function resize(event) {
  if (!resizeState) return
  const dx = event.clientX - resizeState.startX
  const dy = event.clientY - resizeState.startY
  const initial = resizeState.initial
  if (resizeState.kind === "sessions") layout.sessionsWidth = clamp(initial.sessionsWidth + dx, 160, 360)
  if (resizeState.kind === "workspace") layout.workspaceWidth = clamp(initial.workspaceWidth + dx, 260, 720)
  if (resizeState.kind === "workspaceTree") layout.workspaceTreeHeight = clamp(initial.workspaceTreeHeight + dy, 120, 600)
  if (resizeState.kind === "terminal") {
    const max = Math.min(560, Math.max(180, window.innerHeight * 0.65))
    layout.terminalHeight = clamp(initial.terminalHeight - dy, 160, max)
  }
  saveLayout()
}

function stopResize() {
  resizeState = null
  window.removeEventListener("pointermove", resize)
  saveLayout()
}

function clamp(value, minimum, maximum) {
  return Math.round(Math.max(minimum, Math.min(maximum, value)))
}

onMounted(() => {
  window.addEventListener("popstate", handleHistoryNavigation)
  void initialize()
})
onBeforeUnmount(() => {
  window.removeEventListener("popstate", handleHistoryNavigation)
  window.removeEventListener("pointermove", resize)
  eventSource?.close()
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
})

const toolPrefixes = [
  "活动> ", "轮次> ", "提醒> ", "警告> ", "提示> ", "tool_call> ",
  "tool_result> ", "artifact> ", "context> ", "progress> ", "stop> ", "instructions> ",
]
const knownTools = new Set([
  "run_command", "read_file", "write_file", "apply_patch", "list_dir",
  "delete_file", "move_file", "copy_file", "search", "artifact",
])

function clip(value, limit = 120) {
  const text = String(value || "").trim()
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

function toolLabel(line) {
  let cleaned = line.trim()
  for (const prefix of toolPrefixes) {
    if (cleaned.startsWith(prefix)) {
      cleaned = cleaned.slice(prefix.length).trim()
      break
    }
  }
  const match = cleaned.match(/^([a-z][a-z0-9_.-]*)\s*(?:[:：]\s*|\s+)(.*)$/i)
  if (match && knownTools.has(match[1])) return `${match[1]} · ${clip(match[2])}`
  return clip(cleaned)
}

function turnKeyForEvent(event, fallback) {
  return typeof event.data?.turn_id === "string" && event.data.turn_id
    ? event.data.turn_id
    : fallback || `legacy-${event.id}`
}

function permissionStepFor(event, turnId) {
  const data = event.data || {}
  const detail = data.command || data.command_display || data.summary || event.content
  return {
    id: `permission-${event.id}`,
    key: `${turnId}:${data.command || data.command_display || data.summary || "permission"}`,
    label: clip(detail),
    status: /已允许/.test(event.content) ? "allowed" : /已拒绝/.test(event.content) ? "denied" : "waiting",
    statusLabel: /已允许/.test(event.content) ? "Allowed" : /已拒绝/.test(event.content) ? "Denied" : "Waiting for permission",
    detail: event.content,
    data,
  }
}

function buildExecutionGroups(events, live, pendingPermission, session, states, expanded) {
  const groups = new Map()
  let fallbackTurn = null
  for (const event of events) {
    const key = turnKeyForEvent(event, fallbackTurn)
    if (event.kind === "user") fallbackTurn = key
    if (!groups.has(key)) groups.set(key, { key, events: [], user: null })
    const group = groups.get(key)
    group.events.push(event)
    if (event.kind === "user" && !group.user) group.user = event
  }
  const activeTurn = session?.active_turn_id
  if (activeTurn && !groups.has(activeTurn)) groups.set(activeTurn, { key: activeTurn, events: [], user: null })
  const liveTurn = live?.turn_id || activeTurn
  if (live?.active && liveTurn && !groups.has(liveTurn)) groups.set(liveTurn, { key: liveTurn, events: [], user: null })
  const pendingTurn = pendingPermission?.turn_id || activeTurn
  if (pendingPermission && pendingTurn && !groups.has(pendingTurn)) groups.set(pendingTurn, { key: pendingTurn, events: [], user: null })

  return [...groups.values()].map((group) => {
    const tools = []
    const permissions = []
    const assistants = []
    const errors = []
    for (const event of group.events) {
      if (event.kind === "tool") {
        for (const [index, line] of event.content.split("\n").entries()) {
          if (line.trim()) tools.push({
            id: `tool-${event.id}-${index}`,
            label: toolLabel(line),
            statusLabel: "Completed",
            detail: line,
            data: event.data || {},
          })
        }
      } else if (event.kind === "permission") {
        const step = permissionStepFor(event, group.key)
        const existing = permissions.find((item) => item.key === step.key && item.status === "waiting")
        if (existing && step.status !== "waiting") Object.assign(existing, step)
        else permissions.push(step)
      } else if (event.kind === "assistant") assistants.push(event)
      else if (event.kind === "error") errors.push(event)
    }
    const groupPending = pendingPermission && pendingTurn === group.key ? pendingPermission : null
    if (groupPending) {
      const key = `${group.key}:${groupPending.command || groupPending.command_display || groupPending.summary || "permission"}`
      if (!permissions.some((item) => item.key === key && item.status === "waiting")) {
        permissions.push({
          id: `pending-${group.key}`,
          key,
          label: clip(groupPending.command || groupPending.command_display || groupPending.summary),
          status: "waiting",
          statusLabel: "Waiting for permission",
          detail: groupPending.summary || "Agent 请求权限",
          data: groupPending,
        })
      }
    }
    const groupLive = live?.active && liveTurn === group.key ? live : null
    const state = states[group.key]?.status
    const hasError = errors.length > 0 || state === "error" || groupLive?.kind === "error"
    const active = ["starting", "queued", "running", "waiting_permission"].includes(state) ||
      (group.key === activeTurn && !hasError && session?.runtime_status !== "idle" && session?.runtime_status !== "stopped")
    const status = hasError ? "error" : active && (state === "waiting_permission" || groupPending) ? "waiting" : active ? (state === "queued" ? "queued" : "running") : "completed"
    const statusLabel = { running: "Running", waiting: "Waiting for permission", queued: "Queued", error: "Error", completed: "Completed" }[status]
    const stepCount = tools.length + permissions.length
    const groupExpanded = Object.prototype.hasOwnProperty.call(expanded, group.key)
      ? expanded[group.key]
      : ["running", "waiting", "queued", "error"].includes(status)
    return {
      ...group,
      tools,
      permissions,
      assistants,
      errors,
      live: groupLive,
      pendingPermission: groupPending,
      status,
      statusLabel,
      stepCount,
      expanded: groupExpanded,
      summary: `${stepCount} steps · ${statusLabel}`,
    }
  })
}
</script>

<template>
  <main class="shell ide-shell" :style="layoutStyle">
    <header class="app-header">
      <div class="brand"><p class="eyebrow">LOCAL WEB DEMO</p><h1>MyCode</h1></div>
      <div class="header-session">
        <span class="session-name">{{ currentSession ? sessionLabel(currentSession) : "No session" }}</span>
        <span class="status" :data-status="currentSession?.runtime_status || 'stopped'">{{ currentSession?.runtime_status || "stopped" }}</span>
        <button class="secondary compact-button" @click="settingsOpen = !settingsOpen">Settings</button>
        <div v-if="settingsOpen" class="settings-popover">
          <label>显示名称<input v-model="displayName" maxlength="80" placeholder="显示名称" /></label>
          <button @click="saveProfile">保存</button>
        </div>
      </div>
    </header>
    <div class="notice-area">
      <p v-if="error" class="error-banner">{{ error }}</p>
      <p v-if="lifecycleNotice" class="queue-notice global-notice">{{ lifecycleNotice }}</p>
    </div>

    <section class="main-area">
      <aside class="panel sessions-panel" :class="{ collapsed: layout.sessionsCollapsed }">
        <div class="panel-heading">
          <div v-if="!layout.sessionsCollapsed"><p class="eyebrow">SESSIONS</p><h2>Sessions</h2></div>
          <button class="icon-button" :title="layout.sessionsCollapsed ? '展开 Sessions' : '折叠 Sessions'" @click="togglePanel('sessions')">{{ layout.sessionsCollapsed ? '›' : '‹' }}</button>
        </div>
        <div v-if="!layout.sessionsCollapsed" class="session-list">
          <div v-for="session in sessions" :key="session.id" class="session-entry">
            <button class="session-item" :class="{ active: currentSession?.id === session.id }" @click="openSession(session.id)">
              <span>{{ sessionLabel(session) }}</span><small>{{ session.runtime_status }}</small>
            </button>
            <button class="more-button" title="会话菜单" @click.stop="sessionMenuId = sessionMenuId === session.id ? null : session.id">⋯</button>
            <div v-if="sessionMenuId === session.id" class="session-menu">
              <button @click="renameSession(session)">Rename</button>
              <button class="menu-danger" @click="removeSession(session)">Delete</button>
            </div>
          </div>
          <button @click="createSession">New Session</button>
        </div>
      </aside>
      <div class="splitter vertical" title="拖动调整 Sessions 宽度" @pointerdown="startResize('sessions', $event)" />

      <section class="panel workspace-panel" :class="{ collapsed: layout.workspaceCollapsed }">
        <div class="panel-heading">
          <div v-if="!layout.workspaceCollapsed"><p class="eyebrow">WORKSPACE</p><h2>Workspace</h2></div>
          <div class="panel-actions"><button v-if="!layout.workspaceCollapsed" class="secondary compact-button" @click="refreshTree()">↻</button><button class="icon-button" :title="layout.workspaceCollapsed ? '展开 Workspace' : '折叠 Workspace'" @click="togglePanel('workspace')">{{ layout.workspaceCollapsed ? '›' : '‹' }}</button></div>
        </div>
        <template v-if="!layout.workspaceCollapsed">
          <div class="workspace-toolbar">
            <label class="button-label">Upload<input type="file" @change="upload($event, false)" /></label>
            <label class="button-label secondary">ZIP<input type="file" accept=".zip" @change="upload($event, true)" /></label>
            <a v-if="currentSession" class="button-link" :href="API_BASE + scoped('/workspace/download')">Download</a>
          </div>
          <div class="workspace-body">
            <nav class="tree-pane"><FileTree :entries="entries" :selected="selectedPath" @open="openFile" @delete="deleteEntry" /><p v-if="!entries.length" class="muted">Workspace 为空</p></nav>
            <div class="splitter horizontal inner workspace-splitter" title="拖动调整 File Tree 高度" @pointerdown="startResize('workspaceTree', $event)" />
            <article class="preview"><div class="preview-title"><span>{{ selectedPath || "选择一个文本文件" }}</span><a v-if="selectedPath" :href="`${API_BASE}${scoped('/files/download')}?path=${encodeURIComponent(selectedPath)}`">Download</a></div><pre>{{ fileContent }}</pre></article>
          </div>
        </template>
      </section>
      <div class="splitter vertical" title="拖动调整 Workspace 宽度" @pointerdown="startResize('workspace', $event)" />

      <section class="panel agent-panel">
        <div class="panel-heading agent-heading"><div><p class="eyebrow">AGENT</p><h2>Agent</h2><small>{{ currentSession ? sessionLabel(currentSession) : "" }} · {{ currentSession?.runtime_status || "stopped" }}</small></div></div>
        <div ref="outputElement" class="console-history">
          <section v-for="group in executionGroups" :key="group.key" class="execution-group">
            <article v-if="group.user" class="console-card user-card"><strong>You</strong><pre>{{ group.user.content }}</pre></article>
            <button class="execution-header" @click="toggleGroup(group)"><span>{{ group.expanded ? '▼' : '▶' }} 执行过程</span><span>{{ group.summary }}</span></button>
            <div v-show="group.expanded" class="execution-content">
              <details v-for="step in group.tools" :key="step.id" class="execution-step"><summary><span class="step-icon">✓</span><span>{{ step.label }}</span><small>{{ step.statusLabel }}</small></summary><pre>{{ step.detail }}</pre><pre v-if="Object.keys(step.data).length" class="step-data">{{ JSON.stringify(step.data, null, 2) }}</pre></details>
              <div v-for="step in group.permissions" :key="step.id" class="permission-step" :data-status="step.status"><div><span class="step-icon">{{ step.status === 'waiting' ? '●' : step.status === 'allowed' ? '✓' : '!' }}</span><span>{{ step.label }}</span><small>{{ step.statusLabel }}</small></div><p>{{ step.detail }}</p><dl v-if="Object.keys(step.data).length"><template v-for="(value, key) in step.data" :key="key"><template v-if="key !== 'turn_id'"><dt>{{ key }}</dt><dd>{{ value }}</dd></template></template></dl><div v-if="step.status === 'waiting' && group.pendingPermission" class="permission-actions"><button class="danger" @click="resolvePermission(false)">拒绝</button><button @click="resolvePermission(true)">允许</button></div></div>
              <article v-for="event in group.errors" :key="`error-${event.id}`" class="console-card error-card"><strong>Error</strong><pre>{{ event.content }}</pre></article>
              <article v-if="group.live && group.live.kind !== 'assistant'" class="console-card live-card" :data-kind="group.live.kind"><strong>{{ group.live.kind }}</strong><pre>{{ group.live.content }}</pre></article>
            </div>
            <article v-for="event in group.assistants" :key="`assistant-${event.id}`" class="console-card assistant-card"><strong>Assistant</strong><pre>{{ event.content }}</pre></article>
            <article v-if="group.live && group.live.kind === 'assistant'" class="console-card assistant-card live-card"><strong>Assistant</strong><pre>{{ group.live.content }}</pre></article>
          </section>
          <p v-if="!executionGroups.length" class="muted">等待 Agent 输出…</p>
        </div>
        <p v-if="currentSession?.runtime_status === 'queued'" class="queue-notice">当前 Sandbox 已满，正在排队</p>
        <form class="composer" @submit.prevent="sendMessage"><textarea v-model="message" rows="3" placeholder="告诉 MyCode 要完成什么…" @keydown.ctrl.enter="sendMessage" /><button type="submit" :disabled="sendDisabled">Send</button></form>
      </section>
    </section>

    <div class="splitter horizontal terminal-splitter" title="拖动调整 Terminal 高度" @pointerdown="startResize('terminal', $event)" />
    <section class="panel terminal-drawer" :class="{ collapsed: layout.terminalCollapsed }">
      <div class="panel-heading terminal-heading" @click="layout.terminalCollapsed && togglePanel('terminal')"><div><p class="eyebrow">TERMINAL</p><h2>Terminal <small>{{ layout.terminalCollapsed ? '▲' : '▼' }}</small></h2></div><button class="icon-button" :title="layout.terminalCollapsed ? '展开 Terminal' : '折叠 Terminal'" @click.stop="togglePanel('terminal')">{{ layout.terminalCollapsed ? '▲' : '▼' }}</button></div>
      <TerminalPanel v-if="!layout.terminalCollapsed" :session-id="currentSession?.id || ''" :collapsed="layout.terminalCollapsed" />
    </section>
  </main>
</template>
