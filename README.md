# MyCode Web

`mycode-web` 是独立于 MyCode Core 的 Web 与 deployment layer。它不 import、复制或修改 MyCode Python package，而是通过公开 CLI `mycode agent --continue` 驱动 Core。Web repo 与 MyCode Core repo 独立维护。

真实 Provider API Key 只由 Host/FastAPI 持有。Docker Sandbox 仅获得内部 Relay token、Relay URL、模型名，以及 Host 明确传入的 MyCode Runtime 参数；Provider 凭据不会进入 Sandbox。

## 核心能力

- Multi-session：一个 Web User 可以创建、列出、切换和删除多个 Session。
- Session isolation：Workspace、MyCode state、Runtime、Permission、SSE 和 Console history 按 `session_id` 隔离。
- Warm Runtime：进入 Session 后后台预热 Sandbox 与长期运行的 `mycode agent --continue`。
- Workspace：支持上传、文件树、文本预览、文件/目录删除以及文件和完整 Workspace 下载。
- Workspace 自动刷新：Agent 修改文件后，页面自动刷新文件树和当前 Preview。
- Agent Console：同时提供实时增量输出和可持久化、可恢复的聚合历史。
- Permission interaction：Browser 提供独立的 Allow/Reject UI，并映射到 CLI 权限输入。
- Runtime Pool / FIFO Queue：在受控资源上管理并发 Session Runtime。
- Session persistence：Runtime 回收后保留 Session 数据，后续通过 `--continue` 恢复。

## 架构

```text
Browser /mycode/
  -> Vue 3 + Vite
  -> FastAPI /mycode/api/*
  -> Docker Sandbox
  -> long-lived `mycode agent --continue`
  -> FastAPI OpenAI-compatible Relay
  -> Provider
```

每个 Session 的持久数据位于：

```text
data/sessions/<session_id>/workspace/
data/sessions/<session_id>/mycode_state/
```

Sandbox 只挂载对应 Session 的这两个目录。

## Session 与 Runtime 生命周期

HttpOnly Cookie 只标识 Web User。一个 User 可以拥有多个 Session，并通过 `/mycode/?session=<session_id>` 选择当前 Session。首次访问没有 Session 时，前端自动创建一个；URL 未指定 Session 时进入最近活跃的 Session。

Session 支持 create、list、switch 和 delete。进入或切换 Session 后，前端立即调用 activate，并在后台启动或复用 Sandbox 与 `mycode agent --continue`。浏览器刷新不会停止 Runtime，也不会创建重复 Agent process。

Runtime 状态包括：`starting`、`idle`、`running`、`waiting_permission`、`queued`、`stopped` 和 `error`。

Runtime Pool、FIFO Queue 和调度锁都位于单个 FastAPI 进程内。最大 active Sandbox、Queue 上限和 idle TTL 由配置决定，admission 遵循以下规则：

1. capacity 未满时，为 Session activate Runtime；
2. capacity 已满但存在 idle Runtime 时，驱逐最久未活动的 idle Runtime；
3. capacity 已满且没有可驱逐的 idle Runtime 时，新请求进入有界 FIFO Queue。

`starting`、`running` 和 `waiting_permission` 都可能占用 active capacity。Runtime 可因 idle TTL、capacity eviction、explicit stop、process exit 或 lifecycle cleanup 停止，但这些操作不会删除 Workspace、`mycode_state` 或 SQLite Session metadata。再次 activate 时会启动新 Sandbox，并通过 `mycode agent --continue` 恢复 Session。

Workspace watcher 跟随 active Runtime 生命周期，而不是永久附着在保留的 Session 上。Runtime starting/active 时 watcher 启动；Runtime 被回收或停止时 watcher 停止；Session 再次 activate 时 watcher 重启；Session delete 和 application shutdown 会清理 watcher。

## 页面初始化

进入 Session 后，metadata、Workspace tree 和 activate 可以并行执行。Console 与 fresh SSE 使用明确的 snapshot + cursor 协议：

1. 前端先请求 `/console`；
2. FastAPI 先记录该 Session 的 EventHub `latest_id`，再读取 SQLite Console history；
3. `/console` 返回 SQLite snapshot 和 `event_cursor`；
4. 前端通过 `/events?after=<event_cursor>` 建立 fresh SSE。

语义上，`<= event_cursor` 的稳定 Console history 来自 SQLite snapshot，`> event_cursor` 的增量由 SSE replay/live stream 提供，从而避免 Console snapshot 与 SSE 初始化之间丢事件或整批重复历史。

网络断线重连时，浏览器携带的 `Last-Event-ID` 用于补发错过的 replayable stable events；`Last-Event-ID` 的优先级高于 query parameter `after`。

## Workspace 与安全边界

Workspace 支持 upload、file tree、preview、download、delete file 和 delete directory。Host 使用 `watchfiles` 监听真实 Workspace；发生变化时发布该 Session 的 `workspace_changed` SSE，前端 debounce 后刷新文件树和当前 Preview。

Symlink/junction 安全语义：

- tree/stats 可以识别并显示 link，但不会 follow link target；
- symlink 和 dangling symlink 可以删除，删除只作用于 link 本身；
- 删除包含 symlink 的普通目录时，不会递归进入 link target；
- Workspace 外部 target 不会因 Workspace 删除操作被修改；
- read、preview 和 download 拒绝通过 symlink 访问 target；
- ZIP upload 拒绝 archive 中的 symlink entry。

## Console、Permission 与 SSE

CLI 仍是面向人的文本协议，不是 PTY，也不是 JSONL 或正式的跨进程 machine protocol。Web adapter 识别现有 `you> ` 和 Permission prompt；Browser 多行消息只在写入 CLI stdin 前折叠为单行，原始消息仍用于 Web 展示和持久化。

Console 分为两层：

- Live output：CLI stdout chunk 可产生 live-only 的 `console_live` SSE，浏览器在 newline/prompt 前即可看到增量内容。该数据只用于瞬态 UI，不写 SQLite，也不进入长期 replay history。
- Stable history：ConsoleRecorder 在 newline/prompt 等稳定边界聚合输出并形成 `console_event`，再写入 SQLite。它不会为每个 token 创建数据库记录；每个 Session 只保留有限数量的最近历史。页面刷新后通过 `/console` 恢复，稳定事件到达时前端会替换或清理 transient live card，避免重复显示。

Browser 使用独立的 Allow/Reject UI 处理 Permission，并分别向 CLI stdin 写入 `y\n` 或 `n\n`。

SSE replay buffer 位于 FastAPI 进程内。Replayable stable events 可以进入 buffer；raw `agent_output` 和 `console_live` 是 live-only，不长期保留。Fresh connection 使用 snapshot `event_cursor`，reconnect 使用 `Last-Event-ID`，且 `Last-Event-ID` 优先于 `after`。服务重启后 replay buffer 不恢复；Console 历史恢复依赖 SQLite，而不是 SSE history。

## 环境要求

- Python 3.11+
- uv
- Docker Desktop 或支持 BuildKit named contexts 的 Docker
- Node.js 20.19+ 或 22.12+ 与 npm
- OpenAI Chat Completions compatible Provider

## 配置与启动 FastAPI

在 `mycode-web` 根目录执行：

```powershell
Copy-Item .\.env.example .\.env
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

`.env` 至少设置：

```dotenv
MYCODE_PROVIDER_API_KEY=真实密钥
MYCODE_PROVIDER_BASE_URL=https://provider.example.com/v1
MYCODE_MODEL=模型名
```

以下是 repo default 示例值，不是固定架构参数，production 可以通过 `.env` 覆盖：

```dotenv
SANDBOX_MAX_ACTIVE=2
SANDBOX_QUEUE_MAX=20
SANDBOX_MEMORY_LIMIT=640m
SANDBOX_MEMORY_SWAP_LIMIT=1g
SANDBOX_CPUS=1.0
SANDBOX_PIDS_LIMIT=256
SANDBOX_IDLE_TTL_SECONDS=7200
RUNTIME_SWEEP_INTERVAL_SECONDS=60
SESSION_RETENTION_SECONDS=1209600
SESSION_CLEANUP_INTERVAL_SECONDS=3600
```

`SANDBOX_IDLE_TTL_SECONDS` 只回收 Runtime 进程/容器；`SESSION_RETENTION_SECONDS` 根据 SQLite `last_active_at` 删除真正过期的 Session 数据和 metadata。不要提交 `.env`。

Runtime Pool、FIFO Queue、Runtime locks 和 SSE replay state 都是单 FastAPI 进程内状态，因此当前部署要求 `--workers 1`：

```sh
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

多 Worker 部署需要外置共享 runtime coordination、Queue 和 event state；当前实现不依赖 Redis 等共享协调服务。

验证 FastAPI：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/mycode/api/health
```

## 构建 Sandbox Image

Dockerfile 使用名为 `mycode` 的 BuildKit context，只从外部 MyCode source 复制 `README.md`、`pyproject.toml`、`uv.lock` 和 `mycode/`。Web repo 不保存 MyCode source 副本；MyCode project dependencies 和 `uv.lock` 是 Sandbox dependency source of truth。

Windows：

```powershell
.\scripts\build-sandbox.ps1 -MyCodeSource ..\mycode-project
```

Linux：

```sh
./scripts/build-sandbox.sh ../mycode
```

也可直接执行：

```powershell
docker buildx build --load `
  --build-context mycode=..\mycode-project `
  --file .\docker\Dockerfile.sandbox `
  --tag mycode-sandbox:dev .
```

MyCode source path 由命令参数传入，不写死本机路径。Dockerfile 按 `Python/uv -> MyCode lock dependencies -> system tools -> MyCode source` 分层，源码变化不会使 dependency layer 失效。

## 安装并启动 Vue

```powershell
Set-Location .\frontend
npm install
npm run dev
```

浏览器打开 <http://localhost:5173/mycode/>。Vite base 固定为 `/mycode/`，并把 `/mycode/api/*` 代理到 `http://127.0.0.1:8000`。

## Production Deployment

当前实现已经在 Linux production topology 上完成真实链路验证：

```text
Browser
  -> Nginx /mycode/
  -> Vue static assets / FastAPI /mycode/api/*
  -> Docker Sandbox
  -> mycode agent --continue
  -> FastAPI internal Relay
  -> Provider
```

验证范围包括 Linux Host、Docker Sandbox、bind mount UID/GID、Docker bridge 到 FastAPI Relay、真实 Provider request、systemd、Nginx、单 FastAPI worker 和 Browser E2E。README 不记录公网地址、真实 Provider 地址、密钥或服务器私有信息。

## 测试

```powershell
uv sync
.\.venv\Scripts\python.exe -m pytest .\tests -q
Set-Location .\frontend
npm run build
```

Server tests 使用临时 SQLite/Workspace 和 fake Sandbox，不访问真实 Provider，也不启动 Docker。Linux production 与 Browser E2E 属于独立的部署验证，不应由本地 fake tests 替代。

## 当前限制

- 身份只有随机 HttpOnly Cookie，没有正式账号或登录系统。
- Runtime Pool、FIFO Queue、scheduler locks 和 SSE replay buffer 是单 FastAPI 进程内状态。
- 服务重启后 Queue 和 SSE replay history 不恢复；Console history 由 SQLite 恢复。
- CLI 是面向人的文本协议，不是正式的结构化 machine protocol。
- FastAPI 不接管 crash 前遗留的旧 Sandbox process；startup 只按 `mycode-web.managed=true` label 清理 orphan Sandbox。
- Orphan cleanup 不删除 Workspace、`mycode_state` 或 SQLite Session；后续可通过 `mycode agent --continue` 恢复。

设计原因见 `docs/adr/0001-web-demo-cli-sandbox-relay.md`，实现和验证记录见 `docs/实施记录.md`。
