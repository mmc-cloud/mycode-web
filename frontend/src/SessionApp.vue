<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue"
import FileTree from "./FileTree.vue"

const api = "/mycode/api"
const sessions = ref([])
const currentSession = ref(null)
const displayName = ref("")
const entries = ref([])
const selectedPath = ref("")
const fileContent = ref("")
const consoleEvents = ref([])
const message = ref("")
const permission = ref(null)
const error = ref("")
const lifecycleNotice = ref("")
const liveConsole = ref(null)
const outputElement = ref(null)
let eventSource = null
let workspaceTimer = null
let generation = 0
const sendDisabled = computed(() =>
  ["starting", "queued", "running", "waiting_permission"].includes(
    currentSession.value?.runtime_status,
  ),
)

async function request(path, options = {}) {
  const response = await fetch(api + path, options)
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

function scoped(path = "") {
  if (!currentSession.value) throw new Error("请先选择会话")
  return `/sessions/${encodeURIComponent(currentSession.value.id)}${path}`
}

async function initialize() {
  try {
    const result = await request("/sessions")
    displayName.value = result.display_name || ""
    sessions.value = result.sessions
    if (!sessions.value.length) {
      sessions.value = [await request("/sessions", { method: "POST" })]
    }
    const requested = new URLSearchParams(window.location.search).get("session")
    const target = sessions.value.find((item) => item.id === requested) || sessions.value[0]
    await openSession(target.id, true)
  } catch (reason) {
    showError(reason)
  }
}

async function openSession(sessionId, replace = false) {
  const token = ++generation
  eventSource?.close()
  eventSource = null
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
  const listed = sessions.value.find((item) => item.id === sessionId)
  currentSession.value = listed || { id: sessionId, runtime_status: "stopped" }
  entries.value = []
  selectedPath.value = ""
  fileContent.value = ""
  consoleEvents.value = []
  liveConsole.value = null
  permission.value = null
  const url = new URL(window.location.href)
  url.searchParams.set("session", sessionId)
  window.history[replace ? "replaceState" : "pushState"]({}, "", url)
  const metadata = loadMetadata(sessionId, token)
  const tree = refreshTree(sessionId, token)
  const history = loadConsole(sessionId, token)
  void request(`/sessions/${encodeURIComponent(sessionId)}/activate`, { method: "POST" })
    .then((result) => {
      if (generation === token && currentSession.value?.id === sessionId) {
        currentSession.value.runtime_status = result.status
      }
    })
    .catch(showError)
  await Promise.all([metadata, tree, history])
}

async function loadMetadata(sessionId, token) {
  const result = await request(`/sessions/${encodeURIComponent(sessionId)}`)
  if (generation !== token) return
  currentSession.value = result
  permission.value = result.pending_permission
  const index = sessions.value.findIndex((item) => item.id === sessionId)
  if (index >= 0) sessions.value[index] = result
}

async function loadConsole(sessionId, token) {
  const result = await request(`/sessions/${encodeURIComponent(sessionId)}/console`)
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

async function removeSession(session) {
  if (!window.confirm("确认删除这个会话？Workspace、MyCode state 和 Console history 将一并删除。")) return
  try {
    await request(`/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" })
    sessions.value = sessions.value.filter((item) => item.id !== session.id)
    if (currentSession.value?.id !== session.id) return
    if (!sessions.value.length) sessions.value = [await request("/sessions", { method: "POST" })]
    await openSession(sessions.value[0].id, true)
  } catch (reason) { showError(reason) }
}

function connectEvents(sessionId, token, after) {
  eventSource = new EventSource(
    `${api}/sessions/${encodeURIComponent(sessionId)}/events?after=${encodeURIComponent(after)}`,
  )
  eventSource.addEventListener("runtime_status", (event) => {
    if (generation !== token) return
    currentSession.value.runtime_status = JSON.parse(event.data).status
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
    if (generation === token) permission.value = JSON.parse(event.data)
  })
  eventSource.addEventListener("permission_resolved", () => {
    if (generation === token) permission.value = null
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
  try {
    await openFile(selectedPath.value, sessionId, token)
  } catch (_) {}
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
    const result = await request(`/sessions/${encodeURIComponent(sessionId)}/files/tree`)
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
    const result = await request(`/sessions/${encodeURIComponent(sessionId)}/files/content?path=${encodeURIComponent(path)}`)
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

function showError(reason) {
  error.value = reason instanceof Error ? reason.message : String(reason)
  window.setTimeout(() => { error.value = "" }, 8000)
}

function handleHistoryNavigation() {
  const sessionId = new URLSearchParams(window.location.search).get("session")
  if (
    sessionId &&
    sessionId !== currentSession.value?.id &&
    sessions.value.some((session) => session.id === sessionId)
  ) {
    void openSession(sessionId, true)
  }
}

onMounted(() => {
  window.addEventListener("popstate", handleHistoryNavigation)
  void initialize()
})
onBeforeUnmount(() => {
  window.removeEventListener("popstate", handleHistoryNavigation)
  eventSource?.close()
  if (workspaceTimer) window.clearTimeout(workspaceTimer)
})
</script>

<template>
  <main class="shell session-shell">
    <header>
      <div><p class="eyebrow">LOCAL WEB DEMO</p><h1>MyCode</h1></div>
      <div class="profile">
        <input v-model="displayName" maxlength="80" placeholder="显示名称" />
        <button @click="saveProfile">保存</button>
        <span class="status" :data-status="currentSession?.runtime_status || 'stopped'">
          {{ currentSession?.runtime_status || "stopped" }}
        </span>
      </div>
    </header>
    <p v-if="error" class="error-banner">{{ error }}</p>
    <p v-if="lifecycleNotice" class="queue-notice">{{ lifecycleNotice }}</p>

    <aside class="session-panel">
      <div class="panel-heading"><div><p class="eyebrow">SESSIONS</p><h2>会话</h2></div><button @click="createSession">新建会话</button></div>
      <div class="session-list">
        <button v-for="(session, index) in sessions" :key="session.id" class="session-item" :class="{ active: currentSession?.id === session.id }" @click="openSession(session.id)">
          <span>Session {{ sessions.length - index }}</span><small>{{ session.runtime_status }}</small>
        </button>
        <button v-if="currentSession" class="danger session-delete" @click="removeSession(currentSession)">删除当前会话</button>
      </div>
    </aside>

    <section class="workspace-panel">
      <div class="panel-heading"><div><p class="eyebrow">WORKSPACE</p><h2>项目文件</h2></div><button class="secondary" @click="refreshTree()">刷新</button></div>
      <div class="upload-actions">
        <label class="button-label">上传文件<input type="file" @change="upload($event, false)" /></label>
        <label class="button-label">上传 ZIP<input type="file" accept=".zip" @change="upload($event, true)" /></label>
        <a v-if="currentSession" class="button-link" :href="api + scoped('/workspace/download')">下载 Workspace</a>
      </div>
      <div class="workspace-body">
        <nav><FileTree :entries="entries" :selected="selectedPath" @open="openFile" @delete="deleteEntry" /><p v-if="!entries.length" class="muted">Workspace 为空</p></nav>
        <article class="preview">
          <div class="preview-title"><span>{{ selectedPath || "选择一个文本文件" }}</span><a v-if="selectedPath" :href="`${api}${scoped('/files/download')}?path=${encodeURIComponent(selectedPath)}`">下载</a></div>
          <pre>{{ fileContent }}</pre>
        </article>
      </div>
    </section>

    <section class="agent-panel">
      <div class="panel-heading"><div><p class="eyebrow">WEB AGENT CONSOLE</p><h2>会话记录</h2></div></div>
      <div ref="outputElement" class="console-history">
        <article v-for="event in consoleEvents" :key="event.id" class="console-card" :data-kind="event.kind"><strong>{{ { user: 'You', assistant: 'Assistant', tool: 'Tool', permission: 'Permission', error: 'Error' }[event.kind] || event.kind }}</strong><pre>{{ event.content }}</pre></article>
        <article v-if="liveConsole" class="console-card" :data-kind="liveConsole.kind"><strong>{{ { assistant: 'Assistant', tool: 'Tool', error: 'Error' }[liveConsole.kind] || liveConsole.kind }}</strong><pre>{{ liveConsole.content }}</pre></article>
        <p v-if="!consoleEvents.length && !liveConsole" class="muted">等待 Agent 输出…</p>
      </div>
      <p v-if="currentSession?.runtime_status === 'queued'" class="queue-notice">当前 Sandbox 已满，正在排队</p>
      <aside v-if="permission" class="permission-card">
        <strong>权限确认</strong><p>{{ permission.summary }}</p>
        <dl><template v-for="(value, key) in permission" :key="key"><template v-if="key !== 'summary' && key !== 'created_at'"><dt>{{ key }}</dt><dd>{{ value }}</dd></template></template></dl>
        <div class="permission-actions"><button class="danger" @click="resolvePermission(false)">拒绝</button><button @click="resolvePermission(true)">允许</button></div>
      </aside>
      <form class="composer" @submit.prevent="sendMessage">
        <textarea v-model="message" rows="3" placeholder="告诉 MyCode 要完成什么…" @keydown.ctrl.enter="sendMessage" />
        <button type="submit" :disabled="sendDisabled">发送</button>
      </form>
    </section>
  </main>
</template>
