<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue"

import AppDialog from "./AppDialog.vue"
import ActivityBar from "./ActivityBar.vue"
import ChatMessage from "./ChatMessage.vue"
import Composer from "./Composer.vue"
import FileTree from "./FileTree.vue"
import ExecutionTimeline from "./ExecutionTimeline.vue"
import { useExecutionGroups } from "./execution.js"
import { recordLiveArrival } from "./streamingDebug.js"
import TerminalPanel from "./TerminalPanel.vue"
import { LAYOUT_KEY, loadLayout } from "./layout"
import { useConsoleScroll } from "./useConsoleScroll"
import {
  API_BASE,
  compactApiPath,
  contextStatusApiPath,
  sessionApiPath,
} from "./api"

const ACTIVE_TURN_STATUSES = ["starting", "queued", "running", "waiting_permission", "waiting_mcp_trust"]

const layout = reactive(loadLayout())
const layoutStyle = computed(() => ({
  "--sidebar-width": `${layout.sidebarOpen ? layout.sidebarWidth : 0}px`,
  "--sidebar-splitter-width": `${layout.sidebarOpen ? 8 : 0}px`,
  "--terminal-splitter-height": `${layout.terminalCollapsed ? 0 : 8}px`,
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
const expandedDirectories = reactive(new Set())
const selectedPath = ref("")
const fileContent = ref("")
const consoleEvents = ref([])
const liveConsole = ref(null)
const permission = ref(null)
const permissionSubmission = ref(null)
const permissionSubmissionStatus = computed(() => {
  const submission = permissionSubmission.value
  if (!submission) return ""
  return submission?.sessionId === currentSession.value?.id && submission?.requestId === permission.value?.request_id
    ? submission.status : ""
})
const pendingMcpTrust = ref(null)
const contextStatus = ref(null)
const contextVisible = ref(false)
const contextError = ref("")
const contextPanel = ref(null)
const contextHeading = ref(null)
const contextActions = ref(null)
const contextCloseButton = ref(null)
const contextMaxHeight = ref(360)
let contextOpener = null
let contextResizeObserver = null
const controlInFlight = ref(null)
const compactFeedback = ref("")
const turnStates = ref({})
const expandedGroups = ref({})
const sessionHydrating = ref(false)
const message = ref("")
const sendingSessionIds = reactive(new Set())
const error = ref("")
const lifecycleNotice = ref("")
const dialog = reactive({
  type: null,
  target: null,
  title: "",
  message: "",
  detail: "",
  inputLabel: "",
  inputValue: "",
  confirmText: "确认",
  danger: false,
  busy: false,
})
const outputElement = ref(null)
const outputContent = ref(null)
const consoleScroll = useConsoleScroll(outputElement, outputContent)
const { showJump: showScrollJump } = consoleScroll
const sidebarElement = ref(null)
const activityBar = ref(null)
let eventSource = null
let workspaceTimer = null
let generation = 0
let previewRequest = 0
let treeRequest = 0
let resizeState = null

const sendDisabled = computed(() =>
  sendingSessionIds.has(currentSession.value?.id) || (
    Boolean(currentSession.value?.active_turn_id) &&
    ACTIVE_TURN_STATUSES.includes(currentSession.value?.runtime_status)
  ) || Boolean(currentSession.value?.pending_control) || Boolean(controlInFlight.value),
)
const contextControlDisabled = computed(() => {
  const session = currentSession.value
  if (!session) return true
  if (session.runtime_status !== "idle") return true
  if (controlInFlight.value) return true
  if (session.pending_control) return true
  if (session.active_turn_id) return true
  if (permission.value) return true
  if (pendingMcpTrust.value) return true
  return false
})
const executionGroups = useExecutionGroups({
  events: consoleEvents, live: liveConsole, pendingPermission: permission,
  session: currentSession, states: turnStates, expanded: expandedGroups,
})
const dialogOpen = computed(() => Boolean(dialog.type))
const dialogMode = computed(() => dialog.type === "rename" ? "input" : "confirm")
const dialogConfirmDisabled = computed(() => (
  dialog.type === "rename" && !dialog.inputValue.trim()
))
const emptyConsoleText = computed(() => {
  if (sessionHydrating.value) return "正在加载会话历史…"
  if (currentSession.value?.runtime_status === "stopped") return "Agent 已停止。"
  if (currentSession.value?.runtime_status === "error") return "Agent 运行出错，请查看错误提示。"
  if (currentSession.value?.runtime_status === "idle" && !currentSession.value.active_turn_id &&
      !sendingSessionIds.has(currentSession.value.id)) {
    return "开始与 MyCode 对话，或描述你希望完成的任务。"
  }
  return "等待 Agent 输出…"
})

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
    if (!sessions.value.length && !result.has_created_session) {
      sessions.value = [await request("/sessions", { method: "POST" })]
    }
    if (!sessions.value.length) {
      clearCurrentSession()
      return
    }
    const requested = new URLSearchParams(window.location.search).get("session")
    const target = sessions.value.find((item) => item.id === requested) || sessions.value[0]
    await openSession(target.id, true)
  } catch (reason) { showError(reason) }
}

function clearCurrentSession(replace = true) {
  generation += 1
  consoleScroll.reset()
  sessionHydrating.value = false
  closeDialog()
  eventSource?.close()
  eventSource = null
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
  workspaceTimer = null
  currentSession.value = null
  entries.value = []
  expandedDirectories.clear()
  closePreview()
  consoleEvents.value = []
  liveConsole.value = null
  permission.value = null
  permissionSubmission.value = null
  pendingMcpTrust.value = null
  contextStatus.value = null
  contextVisible.value = false
  contextError.value = ""
  controlInFlight.value = null
  compactFeedback.value = ""
  turnStates.value = {}
  expandedGroups.value = {}
  sessionMenuId.value = null
  const url = new URL(window.location.href)
  url.searchParams.delete("session")
  window.history[replace ? "replaceState" : "pushState"]({}, "", url)
}

async function openSession(sessionId, replace = false) {
  const token = ++generation
  consoleScroll.reset()
  sessionHydrating.value = true
  closeDialog()
  eventSource?.close()
  eventSource = null
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
  const listed = sessions.value.find((item) => item.id === sessionId)
  currentSession.value = listed || { id: sessionId, name: null, runtime_status: "stopped", active_turn_id: null }
  entries.value = []
  expandedDirectories.clear()
  closePreview()
  consoleEvents.value = []
  liveConsole.value = null
  permission.value = null
  permissionSubmission.value = null
  pendingMcpTrust.value = null
  contextStatus.value = null
  contextVisible.value = false
  contextError.value = ""
  controlInFlight.value = null
  compactFeedback.value = ""
  turnStates.value = {}
  expandedGroups.value = {}
  const url = new URL(window.location.href)
  url.searchParams.set("session", sessionId)
  window.history[replace ? "replaceState" : "pushState"]({}, "", url)
  const metadata = loadMetadata(sessionId, token)
  const tree = refreshTree(sessionId, token)
  const history = metadata.then((bootstrap) => {
    if (!bootstrap) return
    return loadConsole(sessionId, token, bootstrap.event_cursor)
  })
  void request(scoped("/activate", sessionId), { method: "POST" })
    .then((result) => {
      if (generation === token && currentSession.value?.id === sessionId) {
        currentSession.value.runtime_status = result.status
      }
    })
    .catch(showError)
  try {
    await Promise.all([metadata, tree, history])
  } catch (reason) {
    if (generation === token && currentSession.value?.id === sessionId) {
      showError(reason)
    }
  } finally {
    if (generation === token && currentSession.value?.id === sessionId) {
      sessionHydrating.value = false
    }
  }
}

async function loadMetadata(sessionId, token) {
  const result = await request(scoped("", sessionId))
  if (generation !== token || currentSession.value?.id !== sessionId) return null
  currentSession.value = result
  permission.value = result.pending_permission
  pendingMcpTrust.value = result.pending_mcp_trust
  contextStatus.value = result.context_status || null
  controlInFlight.value = result.pending_control || null
  if (result.active_turn_id) {
    updateTurnStatus(result.active_turn_id, result.runtime_status)
  }
  const index = sessions.value.findIndex((item) => item.id === sessionId)
  if (index >= 0) sessions.value[index] = result
  return result
}

async function loadConsole(sessionId, token, bootstrapCursor) {
  const result = await request(scoped("/console", sessionId))
  if (
    generation !== token ||
    currentSession.value?.id !== sessionId ||
    !Number.isInteger(bootstrapCursor)
  ) return
  const merged = new Map(result.events.map((event) => [event.id, event]))
  for (const event of consoleEvents.value) merged.set(event.id, event)
  consoleEvents.value = [...merged.values()].sort((left, right) => left.id - right.id)
  scrollConsole()
  connectEvents(sessionId, token, bootstrapCursor)
}

async function createSession() {
  try {
    const created = await request("/sessions", { method: "POST" })
    sessions.value.unshift(created)
    await openSession(created.id)
  } catch (reason) { showError(reason) }
}

function openRenameDialog(session) {
  sessionMenuId.value = null
  closeDialog()
  Object.assign(dialog, {
    type: "rename",
    target: { sessionId: session.id, name: session.name || sessionLabel(session) },
    title: "重命名会话",
    message: "",
    detail: "",
    inputLabel: "会话名称",
    inputValue: session.name || sessionLabel(session),
    confirmText: "保存",
    danger: false,
    busy: false,
  })
}

function openRemoveSessionDialog(session) {
  sessionMenuId.value = null
  closeDialog()
  const title = sessionLabel(session)
  Object.assign(dialog, {
    type: "delete-session",
    target: { sessionId: session.id, title },
    title: "删除会话？",
    message: `确定删除 “${title}” 吗？`,
    detail: "该会话的聊天记录、运行状态和 Terminal 状态将被删除。项目 Workspace 文件不会受到影响。",
    inputLabel: "",
    inputValue: "",
    confirmText: "删除会话",
    danger: true,
    busy: false,
  })
}

function openDeleteEntryDialog(entry) {
  const sessionId = currentSession.value?.id
  if (!sessionId) return
  closeDialog()
  const isDirectory = entry.kind === "directory"
  Object.assign(dialog, {
    type: "delete-entry",
    target: { sessionId, generation, path: entry.path, name: entry.name, kind: entry.kind },
    title: isDirectory ? "删除目录？" : "删除文件？",
    message: `确定删除 “${entry.name}” 吗？`,
    detail: isDirectory ? "该目录及其中的全部内容将被递归删除。" : "",
    inputLabel: "",
    inputValue: "",
    confirmText: "删除",
    danger: true,
    busy: false,
  })
}

function closeDialog() {
  Object.assign(dialog, {
    type: null,
    target: null,
    title: "",
    message: "",
    detail: "",
    inputLabel: "",
    inputValue: "",
    confirmText: "确认",
    danger: false,
    busy: false,
  })
}

async function submitDialog() {
  if (!dialog.target || dialog.busy) return
  const type = dialog.type
  const target = dialog.target
  if (type === "rename" && !dialog.inputValue.trim()) return
  dialog.busy = true
  try {
    if (type === "rename") {
      const name = dialog.inputValue.trim()
      const updated = await request(scoped("", target.sessionId), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      })
      const index = sessions.value.findIndex((item) => item.id === target.sessionId)
      if (index >= 0) sessions.value[index] = updated
      if (currentSession.value?.id === target.sessionId) currentSession.value = updated
      closeDialog()
      return
    }

    if (type === "delete-session") {
      await request(scoped("", target.sessionId), { method: "DELETE" })
      sessions.value = sessions.value.filter((item) => item.id !== target.sessionId)
      const wasCurrent = currentSession.value?.id === target.sessionId
      closeDialog()
      if (!wasCurrent) return
      if (!sessions.value.length) {
        clearCurrentSession()
        return
      }
      await openSession(sessions.value[0].id, true)
      return
    }

    if (type === "delete-entry") {
      await request(`${scoped("/files", target.sessionId)}?path=${encodeURIComponent(target.path)}`, { method: "DELETE" })
      const wasCurrent = currentSession.value?.id === target.sessionId
      if (wasCurrent && (selectedPath.value === target.path || selectedPath.value.startsWith(`${target.path}/`))) {
        closePreview()
      }
      closeDialog()
      await refreshTree(target.sessionId, target.generation)
    }
  } catch (reason) {
    dialog.busy = false
    showError(reason)
  }
}

function clearPendingInteractions() {
  permission.value = null
  permissionSubmission.value = null
  pendingMcpTrust.value = null
  controlInFlight.value = null
}

function applyRuntimeStatus(data) {
  if (!currentSession.value) return
  currentSession.value.runtime_status = data.status
  if (data.status !== "idle") {
    contextStatus.value = null
    compactFeedback.value = ""
  }
  if (data.status === "error" || data.status === "stopped") {
    clearPendingInteractions()
    if (currentSession.value) currentSession.value.pending_control = null
  }
  if (!data.turn_id) return
  updateTurnStatus(data.turn_id, data.status)
  if (ACTIVE_TURN_STATUSES.includes(data.status)) {
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
    recordLiveArrival()
    const data = JSON.parse(event.data)
    liveConsole.value = data.active ? data : null
    scrollConsole()
  })
  eventSource.addEventListener("permission_request", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    if (permission.value?.request_id !== data.request_id) permissionSubmission.value = null
    permission.value = data
    if (data.turn_id) updateTurnStatus(data.turn_id, "waiting_permission")
  })
  eventSource.addEventListener("permission_resolved", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    // A late resolution for an older request must not unlock/clear a new one.
    if (permission.value && data.request_id !== permission.value.request_id) return
    permission.value = null
    permissionSubmission.value = null
    if (data.turn_id) {
      const status = currentSession.value?.runtime_status === "idle"
        ? "idle"
        : "running"
      updateTurnStatus(data.turn_id, status)
    }
  })
  eventSource.addEventListener("mcp_trust_request", (event) => {
    if (generation !== token) return
    pendingMcpTrust.value = JSON.parse(event.data)
  })
  eventSource.addEventListener("mcp_trust_resolved", (event) => {
    if (generation !== token) return
    pendingMcpTrust.value = null
  })
  eventSource.addEventListener("runtime_control", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    if (data.status === "started") {
      controlInFlight.value = data.control || null
      if (currentSession.value) currentSession.value.pending_control = data.control || null
    } else if (controlInFlight.value === data.control) {
      controlInFlight.value = null
      if (currentSession.value) currentSession.value.pending_control = null
    }
  })
  eventSource.addEventListener("context_status", (event) => {
    if (generation !== token) return
    contextStatus.value = JSON.parse(event.data)
  })
  eventSource.addEventListener("compact_result", (event) => {
    if (generation !== token) return
    const data = JSON.parse(event.data)
    if (data.after) contextStatus.value = data.after
    else if (data.before) contextStatus.value = data.before
    compactFeedback.value = compactFeedbackText(data)
  })
  eventSource.addEventListener("workspace_changed", () => {
    if (generation !== token) return
    if (workspaceTimer) window.clearTimeout(workspaceTimer)
    workspaceTimer = window.setTimeout(() => refreshWorkspaceAfterChange(sessionId, token), 250)
  })
  eventSource.addEventListener("runtime_expired", (event) => {
    if (generation !== token) return
    clearPendingInteractions()
    currentSession.value.runtime_status = "stopped"
    lifecycleNotice.value = JSON.parse(event.data).message || "Sandbox 已停止，会话数据已保留"
    window.setTimeout(() => { lifecycleNotice.value = "" }, 8000)
  })
  eventSource.addEventListener("error", (event) => {
    if (generation === token && event.data) {
      showError(JSON.parse(event.data).message || "运行时错误")
    }
  })
  eventSource.addEventListener("runtime_error", (event) => {
    if (generation === token) {
      const data = JSON.parse(event.data)
      showError(data.message || "运行时协议错误")
    }
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

function updateTurnStatus(turnId, status) {
  turnStates.value[turnId] = { status }
}

function scrollConsole() {
  consoleScroll.afterUpdate()
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
  if (!sessionId || generation !== token || currentSession.value?.id !== sessionId) return
  const requestId = ++treeRequest
  try {
    const result = await request(scoped("/files/tree", sessionId))
    if (generation !== token || currentSession.value?.id !== sessionId || requestId !== treeRequest) return
    entries.value = result.entries
    const directories = new Set()
    const files = new Set()
    const visit = (items) => items.forEach((entry) => {
      if (entry.kind === "directory") {
        directories.add(entry.path)
        visit(entry.children || [])
      } else if (entry.kind === "file") files.add(entry.path)
    })
    visit(result.entries)
    for (const path of expandedDirectories) if (!directories.has(path)) expandedDirectories.delete(path)
    if (selectedPath.value && !files.has(selectedPath.value)) closePreview()
  } catch (reason) { if (generation === token && requestId === treeRequest) showError(reason) }
}

function toggleDirectory(path, open) {
  if (open) expandedDirectories.add(path)
  else expandedDirectories.delete(path)
}

function closePreview(restoreFocus = false) {
  const path = selectedPath.value
  previewRequest += 1
  selectedPath.value = ""
  fileContent.value = ""
  if (resizeState?.kind === "workspaceTree") stopResize()
  if (restoreFocus) nextTick(() => {
    const button = [...sidebarElement.value?.querySelectorAll(".file-button") || []]
      .find((element) => element.title === path)
    button?.focus({ preventScroll: true })
  })
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
  if (generation !== token || currentSession.value?.id !== sessionId) return
  const requestId = ++previewRequest
  if (selectedPath.value !== path) fileContent.value = ""
  selectedPath.value = path
  const isCurrent = () => generation === token && currentSession.value?.id === sessionId &&
    requestId === previewRequest && selectedPath.value === path
  try {
    const result = await request(`${scoped("/files/content", sessionId)}?path=${encodeURIComponent(path)}`)
    if (isCurrent()) fileContent.value = result.content
  } catch (reason) {
    if (!isCurrent()) return
    if (reason.status === 404) {
      closePreview()
      return
    }
    showError(reason)
  }
}

async function sendMessage() {
  if (sendDisabled.value) return
  const sessionId = currentSession.value?.id
  const token = generation
  if (!sessionId) return
  const content = message.value.trim()
  if (!content) return
  sendingSessionIds.add(sessionId)
  try {
    const result = await request(scoped("/message", sessionId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    })
    if (generation !== token || currentSession.value?.id !== sessionId) return
    currentSession.value.runtime_status = result.status
    currentSession.value.active_turn_id = result.turn_id
    updateTurnStatus(result.turn_id, result.status)
    contextStatus.value = null
    compactFeedback.value = ""
    message.value = ""
  } catch (reason) {
    if (generation === token && currentSession.value?.id === sessionId) {
      showError(reason)
    }
  } finally {
    sendingSessionIds.delete(sessionId)
  }
}

function openContext(event) {
  contextOpener = event?.currentTarget || contextActions.value?.querySelector("button")
  contextVisible.value = true
  contextError.value = ""
  nextTick(() => {
    if (contextVisible.value) {
      updateContextHeight()
      contextCloseButton.value?.focus({ preventScroll: true })
    }
  })
}

function updateContextHeight() {
  const historyBounds = outputElement.value?.getBoundingClientRect()
  const headingBounds = contextHeading.value?.getBoundingClientRect()
  if (historyBounds && headingBounds) {
    contextMaxHeight.value = Math.max(0, Math.min(360, historyBounds.bottom - headingBounds.bottom - 8))
  }
}

watch(outputElement, (element) => {
  contextResizeObserver?.disconnect()
  if (!element) return
  contextResizeObserver = new ResizeObserver(updateContextHeight)
  contextResizeObserver.observe(element.parentElement)
  updateContextHeight()
}, { flush: "post" })

function closeContext(restoreFocus = true) {
  contextVisible.value = false
  if (restoreFocus) {
    const target = contextOpener?.isConnected && !contextOpener.disabled ? contextOpener : contextHeading.value
    target?.focus({ preventScroll: true })
  }
}

function dismissContext(event) {
  if (!contextVisible.value) return
  if (event.type === "keydown") {
    if (event.key === "Escape" && !dialogOpen.value) {
      event.preventDefault()
      closeContext()
    }
    return
  }
  if (!contextPanel.value?.contains(event.target) && !contextActions.value?.contains(event.target)) closeContext(false)
}

// Permission and trust prompts take priority over this informational overlay.
watch([permission, pendingMcpTrust, dialogOpen], ([permissionRequest, trustRequest, modalOpen]) => {
  if (contextVisible.value && (permissionRequest || trustRequest || modalOpen)) closeContext(false)
}, { flush: "sync" })

async function inspectContext(event) {
  if (contextControlDisabled.value) return
  if (contextVisible.value) {
    closeContext()
    return
  }
  const sessionId = currentSession.value?.id
  const token = generation
  if (!sessionId) return
  controlInFlight.value = "context_status"
  openContext(event)
  compactFeedback.value = ""
  try {
    const result = await request(contextStatusApiPath(sessionId), { method: "POST" })
    if (generation === token && currentSession.value?.id === sessionId) {
      contextStatus.value = result
      currentSession.value.pending_control = null
    }
  } catch (reason) {
    if (generation === token && currentSession.value?.id === sessionId) {
      contextError.value = reason.message
      showError(reason)
    }
  } finally {
    if (generation === token && currentSession.value?.id === sessionId) {
      controlInFlight.value = null
    }
  }
}

async function compactContext(event) {
  if (contextControlDisabled.value) return
  const sessionId = currentSession.value?.id
  const token = generation
  if (!sessionId) return
  controlInFlight.value = "compact"
  openContext(event)
  compactFeedback.value = "正在 Compact…"
  try {
    const result = await request(compactApiPath(sessionId), { method: "POST" })
    if (generation === token && currentSession.value?.id === sessionId) {
      if (result.after) contextStatus.value = result.after
      else if (result.before) contextStatus.value = result.before
      compactFeedback.value = compactFeedbackText(result)
      currentSession.value.pending_control = null
    }
  } catch (reason) {
    if (generation === token && currentSession.value?.id === sessionId) {
      contextError.value = reason.message
      showError(reason)
    }
  } finally {
    if (generation === token && currentSession.value?.id === sessionId) {
      controlInFlight.value = null
    }
  }
}

async function resolvePermission(decision) {
  const sessionId = currentSession.value?.id
  const requestId = permission.value?.request_id
  if (!sessionId || !requestId || permissionSubmissionStatus.value) return
  const token = generation
  permissionSubmission.value = { sessionId, requestId, status: "submitting" }
  const submission = permissionSubmission.value
  const isCurrent = () => generation === token && currentSession.value?.id === sessionId &&
    permission.value?.request_id === requestId && permissionSubmission.value === submission
  try {
    await request(scoped("/permission", sessionId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, request_id: requestId }),
    })
    // HTTP success is not the permission lifecycle resolution. Stay locked
    // until SSE/state synchronization, a new request or session cleanup.
    if (isCurrent()) submission.status = "awaiting_sync"
  } catch (reason) {
    if (!isCurrent()) return
    permissionSubmission.value = null
    showError(reason)
  }
}

async function resolveMcpTrust(approved) {
  if (!pendingMcpTrust.value) return
  try {
    await request(scoped("/mcp-trust"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved,
        request_id: pendingMcpTrust.value.request_id,
      }),
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
  return session.name || "Session"
}

function formatContextTokens(value) {
  return Number.isFinite(value) ? value.toLocaleString() : "—"
}

function contextUsageLabel(status) {
  if (!status || !Number.isFinite(status.estimated_input_tokens) || !Number.isFinite(status.max_input_tokens) || status.max_input_tokens <= 0) {
    return "—"
  }
  const percentage = (status.estimated_input_tokens / status.max_input_tokens) * 100
  return `${Math.round(percentage)}%`
}

function compactFeedbackText(result) {
  const labels = {
    compacted: "Compact 完成",
    skipped: "Compact 跳过",
    failed: "Compact 失败",
  }
  const label = labels[result?.status] || `Compact: ${result?.status || "unknown"}`
  return result?.reason ? `${label}（${result.reason}）` : label
}

function handleHistoryNavigation() {
  const sessionId = new URLSearchParams(window.location.search).get("session")
  if (sessionId && sessionId !== currentSession.value?.id && sessions.value.some((session) => session.id === sessionId)) {
    void openSession(sessionId, true)
  }
}

function selectSidebar(view) {
  layout.sidebarOpen = layout.sidebarView !== view || !layout.sidebarOpen
  layout.sidebarView = view
  sessionMenuId.value = null
  if (resizeState?.kind === "sidebar" || resizeState?.kind === "workspaceTree") stopResize()
  saveLayout()
}

function closeSidebar() {
  layout.sidebarOpen = false
  sessionMenuId.value = null
  if (resizeState?.kind === "sidebar" || resizeState?.kind === "workspaceTree") stopResize()
  saveLayout()
  activityBar.value?.focusView(layout.sidebarView)
}

function toggleTerminal() {
  layout.terminalCollapsed = !layout.terminalCollapsed
  if (layout.terminalCollapsed && resizeState?.kind === "terminal") stopResize()
  saveLayout()
}

function startResize(kind, event) {
  if (event.button !== 0) return
  if (kind === "terminal" && layout.terminalCollapsed) return
  event.preventDefault()
  resizeState = {
    kind,
    startX: event.clientX,
    startY: event.clientY,
    initial: {
      sidebarWidth: sidebarElement.value?.getBoundingClientRect().width || layout.sidebarWidth,
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
  if (resizeState.kind === "sidebar") layout.sidebarWidth = clamp(initial.sidebarWidth + dx, 220, window.innerWidth <= 1000 ? 280 : 520)
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
  saveLayout()
  window.addEventListener("popstate", handleHistoryNavigation)
  document.addEventListener("pointerdown", dismissContext)
  document.addEventListener("keydown", dismissContext)
  document.addEventListener("focusin", dismissContext)
  void initialize()
})
onBeforeUnmount(() => {
  contextResizeObserver?.disconnect()
  document.removeEventListener("pointerdown", dismissContext)
  document.removeEventListener("keydown", dismissContext)
  document.removeEventListener("focusin", dismissContext)
  window.removeEventListener("popstate", handleHistoryNavigation)
  window.removeEventListener("pointermove", resize)
  eventSource?.close()
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
})

</script>

<template>
  <main class="shell ide-shell" :style="layoutStyle">
    <header class="app-header">
      <div class="brand"><h1>MyCode</h1><span class="brand-caption">Web Demo</span></div>
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
    <section class="main-area">
      <ActivityBar ref="activityBar" :active-view="layout.sidebarView" :sidebar-open="layout.sidebarOpen" @select="selectSidebar" />
      <aside v-show="layout.sidebarOpen" id="workbench-sidebar" ref="sidebarElement" class="panel sidebar" :aria-label="`${layout.sidebarView === 'sessions' ? 'Sessions' : 'Workspace'} 侧栏`">
        <section v-show="layout.sidebarView === 'sessions'" class="sidebar-view sessions-panel">
          <div class="panel-heading">
            <h2>Sessions</h2>
            <button class="icon-button" title="收起 Sidebar" aria-label="收起 Sidebar" @click="closeSidebar">‹</button>
          </div>
          <div class="session-list">
            <div v-for="session in sessions" :key="session.id" class="session-entry">
              <button class="session-item" :class="{ active: currentSession?.id === session.id }" @click="openSession(session.id)">
                <span>{{ sessionLabel(session) }}</span><small>{{ session.runtime_status }}</small>
              </button>
              <button class="more-button" title="会话菜单" @click.stop="sessionMenuId = sessionMenuId === session.id ? null : session.id">⋯</button>
              <div v-if="sessionMenuId === session.id" class="session-menu">
                <button @click="openRenameDialog(session)">Rename</button>
                <button class="menu-danger" @click="openRemoveSessionDialog(session)">Delete</button>
              </div>
            </div>
            <button @click="createSession">New Session</button>
          </div>
        </section>

        <section v-show="layout.sidebarView === 'workspace'" class="sidebar-view workspace-panel">
          <div class="panel-heading">
            <h2>Workspace</h2>
            <div class="panel-actions"><button class="secondary compact-button" title="刷新 Workspace" @click="refreshTree()">↻</button><button class="icon-button" title="收起 Sidebar" aria-label="收起 Sidebar" @click="closeSidebar">‹</button></div>
          </div>
          <template v-if="currentSession">
            <div class="workspace-toolbar">
              <label class="button-label">Upload<input type="file" @change="upload($event, false)" /></label>
              <label class="button-label secondary" title="Upload ZIP and extract into Workspace root (/)" aria-label="Upload ZIP and extract into Workspace root (/)">Extract ZIP<input type="file" accept=".zip" @change="upload($event, true)" /></label>
              <a v-if="currentSession" class="button-link" :href="API_BASE + scoped('/workspace/download')">Download</a>
            </div>
            <div class="workspace-body" :class="{ 'has-preview': selectedPath }">
              <nav class="tree-pane" aria-label="Workspace 文件树"><FileTree :entries="entries" :selected="selectedPath" :expanded="expandedDirectories" @toggle="toggleDirectory" @open="openFile" @delete="openDeleteEntryDialog" /><p v-if="!entries.length" class="muted">Workspace 为空</p></nav>
              <div v-if="selectedPath" class="splitter horizontal inner workspace-splitter" title="拖动调整 File Tree 高度" role="separator" aria-label="File Tree 高度" aria-orientation="horizontal" @pointerdown="startResize('workspaceTree', $event)" />
              <article v-if="selectedPath" class="preview"><div class="preview-title"><span :title="selectedPath">{{ selectedPath }}</span><div class="preview-actions"><a :href="`${API_BASE}${scoped('/files/download')}?path=${encodeURIComponent(selectedPath)}`">Download</a><button class="icon-button" title="关闭文件预览" aria-label="关闭文件预览" @click="closePreview(true)">×</button></div></div><pre>{{ fileContent }}</pre></article>
            </div>
          </template>
          <div v-else class="workspace-empty-state">
            <strong>No session selected</strong>
            <p>创建一个 Session 后即可开始使用 Workspace。</p>
          </div>
        </section>
      </aside>
      <div v-if="layout.sidebarOpen" class="splitter vertical sidebar-splitter" title="拖动调整 Sidebar 宽度" role="separator" aria-label="Sidebar 宽度" aria-orientation="vertical" @pointerdown="startResize('sidebar', $event)" />

      <div class="workbench-main" :class="{ 'has-notice': error || lifecycleNotice || pendingMcpTrust }">
        <div v-if="error || lifecycleNotice || pendingMcpTrust" class="notice-area">
          <p v-if="error" class="error-banner">{{ error }}</p>
          <p v-if="lifecycleNotice" class="queue-notice global-notice">{{ lifecycleNotice }}</p>
          <section v-if="pendingMcpTrust" class="mcp-trust-card">
            <strong>MCP Trust required</strong>
            <p>该 Project 请求使用以下 MCP Server。只展示 Core 提供的安全摘要，不包含 secret value。</p>
            <ul>
              <li v-for="server in pendingMcpTrust.servers" :key="`${server.alias}-${server.transport}`">
                <b>{{ server.alias }}</b> · {{ server.transport }}
                <span v-if="server.command"> · {{ server.command }} {{ (server.args || []).join(' ') }}</span>
                <span v-if="server.url_template"> · {{ server.url_template }}</span>
                <small v-if="server.env_keys?.length"> · env: {{ server.env_keys.join(', ') }}</small>
                <small v-if="server.header_keys?.length"> · headers: {{ server.header_keys.join(', ') }}</small>
              </li>
            </ul>
            <div class="permission-actions">
              <button class="danger" @click="resolveMcpTrust(false)">Reject</button>
              <button @click="resolveMcpTrust(true)">Allow</button>
            </div>
          </section>
        </div>
        <section class="panel agent-panel">
          <div ref="contextHeading" class="panel-heading agent-heading" tabindex="-1"><div><h2>Agent</h2><small>{{ currentSession ? sessionLabel(currentSession) : "" }} · {{ currentSession?.runtime_status || "stopped" }}</small></div><div ref="contextActions" class="agent-heading-actions"><button class="secondary compact-button" :disabled="contextControlDisabled" :aria-expanded="contextVisible" aria-controls="context-popover" aria-haspopup="dialog" @click="inspectContext">{{ controlInFlight === 'context_status' ? 'Loading…' : 'Context' }}</button><button class="compact-button" :disabled="contextControlDisabled" :aria-expanded="contextVisible" aria-controls="context-popover" aria-haspopup="dialog" @click="compactContext">{{ controlInFlight === 'compact' ? 'Compacting…' : 'Compact' }}</button></div>
          <section v-if="contextVisible" id="context-popover" ref="contextPanel" class="context-panel" :style="{ '--context-max-height': `${contextMaxHeight}px` }" role="dialog" aria-label="Context" tabindex="-1">
            <div class="context-panel-heading"><strong>Context</strong><span>{{ contextUsageLabel(contextStatus) }} used</span><button ref="contextCloseButton" class="icon-button" title="关闭 Context" aria-label="关闭 Context" @click="closeContext()">×</button></div>
            <p v-if="contextError" class="context-error" role="alert">{{ contextError }}</p>
            <p v-if="compactFeedback" class="context-feedback">{{ compactFeedback }}</p>
            <dl v-if="contextStatus" class="context-grid">
              <div><dt>Estimated Input / Max Input</dt><dd>{{ formatContextTokens(contextStatus.estimated_input_tokens) }} / {{ formatContextTokens(contextStatus.max_input_tokens) }}</dd></div>
              <div><dt>Context Window</dt><dd>{{ formatContextTokens(contextStatus.context_window_tokens) }}</dd></div>
              <div><dt>Reserved Output</dt><dd>{{ formatContextTokens(contextStatus.reserved_output_tokens) }}</dd></div>
              <div><dt>Safety Margin</dt><dd>{{ formatContextTokens(contextStatus.safety_margin_tokens) }}</dd></div>
              <div><dt>Last Provider Prompt Tokens</dt><dd>{{ formatContextTokens(contextStatus.last_provider_prompt_tokens) }}</dd></div>
              <div><dt>Message Count</dt><dd>{{ formatContextTokens(contextStatus.source_message_count) }} / {{ formatContextTokens(contextStatus.model_visible_message_count) }} visible</dd></div>
              <div><dt>Memory Count / Tokens</dt><dd>{{ formatContextTokens(contextStatus.memory_entry_count) }} / {{ formatContextTokens(contextStatus.memory_estimated_tokens) }}</dd></div>
              <div><dt>Compact Status</dt><dd>{{ contextStatus.compact_status || '—' }}</dd></div>
              <div><dt>Compacted Message Count</dt><dd>{{ formatContextTokens(contextStatus.compact_covered_message_count) }}</dd></div>
              <div><dt>Compressed Tool Result Count</dt><dd>{{ formatContextTokens(contextStatus.compressed_tool_result_count) }}</dd></div>
              <div><dt>Estimate Source</dt><dd>{{ contextStatus.estimate_source || '—' }}</dd></div>
            </dl>
            <p v-else class="muted">{{ controlInFlight ? '正在加载 Context…' : 'Context 已变化，请重新查询' }}</p>
          </section>
          </div>
          <div v-if="currentSession" class="console-viewport">
          <div ref="outputElement" class="console-history" tabindex="0" aria-label="Agent 消息历史">
            <div ref="outputContent" class="console-history-content">
            <section v-for="group in executionGroups" :key="`${currentSession.id}:${group.key}`" class="execution-group">
              <template v-for="item in group.items" :key="item.id">
                <ExecutionTimeline v-if="item.kind === 'execution'" :group="item.block" :permission-submission="permissionSubmissionStatus" @toggle="toggleGroup" @resolve="resolvePermission" />
                <ChatMessage v-else :role="item.kind" :content="item.content" :streaming="Boolean(item.streaming)" :data-event-id="item.eventId" />
              </template>
            </section>
            <p v-if="sessionHydrating || !executionGroups.length" class="muted console-empty-state">{{ emptyConsoleText }}</p>
            </div>
          </div>
          <button v-if="showScrollJump" class="scroll-jump" type="button" aria-label="回到底部，继续跟随 Agent 输出" @click="consoleScroll.jump">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6" /></svg>
            回到底部
          </button>
          </div>
          <div v-else class="session-empty-state">
            <strong>No session selected</strong>
            <p>Create a session to start working with MyCode.</p>
            <button @click="createSession">New Session</button>
          </div>
          <p v-if="currentSession?.runtime_status === 'queued' && currentSession?.active_turn_id" class="queue-notice">当前 Sandbox 已满，正在排队</p>
          <Composer v-if="currentSession" v-model="message" :disabled="sendDisabled" :sending="sendingSessionIds.has(currentSession.id)" @send="sendMessage" />
        </section>

        <div v-if="!layout.terminalCollapsed" class="splitter horizontal terminal-splitter" title="拖动调整 Terminal 高度" role="separator" aria-label="Terminal 高度" aria-orientation="horizontal" @pointerdown="startResize('terminal', $event)" />
        <section class="panel terminal-drawer" :class="{ collapsed: layout.terminalCollapsed }">
          <div class="panel-heading terminal-heading" @click="layout.terminalCollapsed && toggleTerminal()"><h2 class="terminal-title"><span class="terminal-icon" aria-hidden="true">&gt;_</span>Terminal</h2><button class="icon-button" :title="layout.terminalCollapsed ? '展开 Terminal' : '折叠 Terminal'" @click.stop="toggleTerminal">{{ layout.terminalCollapsed ? '▲' : '▼' }}</button></div>
          <TerminalPanel v-if="!layout.terminalCollapsed && currentSession" :session-id="currentSession.id" :collapsed="layout.terminalCollapsed" />
        </section>
      </div>
    </section>
  </main>
  <AppDialog
    :open="dialogOpen"
    :mode="dialogMode"
    :title="dialog.title"
    :message="dialog.message"
    :detail="dialog.detail"
    :input-label="dialog.inputLabel"
    :input-value="dialog.inputValue"
    :confirm-text="dialog.confirmText"
    :danger="dialog.danger"
    :busy="dialog.busy"
    :confirm-disabled="dialogConfirmDisabled"
    @update:input-value="dialog.inputValue = $event"
    @cancel="closeDialog"
    @confirm="submitDialog"
  />
</template>
