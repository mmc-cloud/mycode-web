<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue"
import FileTree from "./FileTree.vue"

const api = "/mycode/api"
const displayName = ref("")
const runtimeStatus = ref("stopped")
const entries = ref([])
const selectedPath = ref("")
const fileContent = ref("")
const message = ref("")
const transcript = ref("")
const permission = ref(null)
const error = ref("")
const outputElement = ref(null)
let eventSource = null

async function request(path, options = {}) {
  const response = await fetch(api + path, options)
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      detail = (await response.json()).detail || detail
    } catch (_) {
      // Keep the HTTP fallback message.
    }
    throw new Error(detail)
  }
  const type = response.headers.get("content-type") || ""
  return type.includes("application/json") ? response.json() : response
}

async function initialize() {
  try {
    const session = await request("/session", { method: "POST" })
    displayName.value = session.display_name || ""
    runtimeStatus.value = session.runtime_status
    await refreshTree()
    connectEvents()
  } catch (reason) {
    showError(reason)
  }
}

function connectEvents() {
  eventSource?.close()
  eventSource = new EventSource(api + "/events")
  eventSource.addEventListener("agent_output", (event) => {
    transcript.value += JSON.parse(event.data).content || ""
    nextTick(() => {
      if (outputElement.value) {
        outputElement.value.scrollTop = outputElement.value.scrollHeight
      }
    })
  })
  eventSource.addEventListener("runtime_status", (event) => {
    runtimeStatus.value = JSON.parse(event.data).status
  })
  eventSource.addEventListener("permission_request", (event) => {
    permission.value = JSON.parse(event.data)
  })
  eventSource.addEventListener("permission_resolved", () => {
    permission.value = null
  })
  eventSource.addEventListener("runtime_expired", (event) => {
    permission.value = null
    showError(JSON.parse(event.data).message || "Sandbox 已因长时间无活动而停止")
  })
  eventSource.addEventListener("error", (event) => {
    if (event.data) showError(JSON.parse(event.data).message || "运行时错误")
  })
}

async function saveProfile() {
  try {
    const result = await request("/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: displayName.value }),
    })
    displayName.value = result.display_name || ""
  } catch (reason) {
    showError(reason)
  }
}

async function refreshTree() {
  try {
    entries.value = (await request("/files/tree")).entries
  } catch (reason) {
    showError(reason)
  }
}

async function upload(event, archive) {
  const file = event.target.files?.[0]
  if (!file) return
  const form = new FormData()
  form.append("upload", file)
  form.append("archive", String(archive))
  try {
    await request("/files/upload", { method: "POST", body: form })
    await refreshTree()
  } catch (reason) {
    showError(reason)
  } finally {
    event.target.value = ""
  }
}

async function openFile(path) {
  try {
    const result = await request(`/files/content?path=${encodeURIComponent(path)}`)
    selectedPath.value = path
    fileContent.value = result.content
  } catch (reason) {
    showError(reason)
  }
}

async function sendMessage() {
  const content = message.value.trim()
  if (!content) return
  try {
    const result = await request("/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    })
    runtimeStatus.value = result.status
    transcript.value += `\nyou> ${content}\n`
    message.value = ""
  } catch (reason) {
    showError(reason)
  }
}

async function resolvePermission(allow) {
  try {
    await request("/permission", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow }),
    })
  } catch (reason) {
    showError(reason)
  }
}

function showError(reason) {
  error.value = reason instanceof Error ? reason.message : String(reason)
  window.setTimeout(() => {
    error.value = ""
  }, 8000)
}

onMounted(initialize)
onBeforeUnmount(() => eventSource?.close())
</script>

<template>
  <main class="shell">
    <header>
      <div>
        <p class="eyebrow">LOCAL WEB DEMO</p>
        <h1>MyCode</h1>
      </div>
      <div class="profile">
        <input v-model="displayName" maxlength="80" placeholder="显示名称" />
        <button @click="saveProfile">保存</button>
        <span class="status" :data-status="runtimeStatus">{{ runtimeStatus }}</span>
      </div>
    </header>

    <p v-if="error" class="error-banner">{{ error }}</p>

    <section class="workspace-panel">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">WORKSPACE</p>
          <h2>项目文件</h2>
        </div>
        <button class="secondary" @click="refreshTree">刷新</button>
      </div>
      <div class="upload-actions">
        <label class="button-label">上传文件<input type="file" @change="upload($event, false)" /></label>
        <label class="button-label">上传 ZIP<input type="file" accept=".zip" @change="upload($event, true)" /></label>
        <a class="button-link" :href="api + '/workspace/download'">下载 Workspace</a>
      </div>
      <div class="workspace-body">
        <nav>
          <FileTree :entries="entries" :selected="selectedPath" @open="openFile" />
          <p v-if="entries.length === 0" class="muted">Workspace 为空</p>
        </nav>
        <article class="preview">
          <div class="preview-title">
            <span>{{ selectedPath || "选择一个文本文件" }}</span>
            <a v-if="selectedPath" :href="`${api}/files/download?path=${encodeURIComponent(selectedPath)}`">下载</a>
          </div>
          <pre>{{ fileContent }}</pre>
        </article>
      </div>
    </section>

    <section class="agent-panel">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">AGENT TERMINAL</p>
          <h2>实时输出</h2>
        </div>
      </div>
      <pre ref="outputElement" class="terminal">{{ transcript || "等待消息…" }}</pre>
      <p v-if="runtimeStatus === 'queued'" class="queue-notice">
        当前 Sandbox 已满，正在排队
      </p>
      <aside v-if="permission" class="permission-card">
        <strong>权限确认</strong>
        <p>{{ permission.summary }}</p>
        <dl>
          <template v-for="(value, key) in permission" :key="key">
            <template v-if="key !== 'summary' && key !== 'created_at'">
              <dt>{{ key }}</dt><dd>{{ value }}</dd>
            </template>
          </template>
        </dl>
        <div class="permission-actions">
          <button class="danger" @click="resolvePermission(false)">拒绝</button>
          <button @click="resolvePermission(true)">允许</button>
        </div>
      </aside>
      <form class="composer" @submit.prevent="sendMessage">
        <textarea v-model="message" rows="3" placeholder="告诉 MyCode 要完成什么…" @keydown.ctrl.enter="sendMessage" />
        <button type="submit" :disabled="['queued', 'starting', 'running', 'waiting_permission'].includes(runtimeStatus)">发送</button>
      </form>
    </section>
  </main>
</template>
