# MyCode Web Demo

`mycode-web` 是独立于 MyCode 核心仓库的 Web Demo 与部署层。它通过 Docker Sandbox 启动公开的 `mycode agent --continue` CLI，不 import、复制或修改 MyCode Python package。

两个仓库的职责是：

- `mycode-project`（服务器可命名为 `mycode`）：核心 Coding Agent、CLI、Runtime 与工具。
- `mycode-web`：FastAPI、Vue、Workspace、Sandbox 生命周期入口与 LLM Relay。

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

每个 Web Session 的数据位于：

```text
data/sessions/<session_id>/workspace/
data/sessions/<session_id>/mycode_state/
```

HttpOnly Cookie 只标识 Web User；一个 User 可以创建多个彼此隔离的 Session。浏览器通过 `/mycode/?session=<session_id>` 显式选择当前 Session，Workspace、MyCode state、Runtime、Permission、SSE 和 Console history 都按 `session_id` 隔离。首次访问没有 Session 时，Vue 会创建一个；已有 Session 且 URL 未指定时，进入最近活跃的 Session。

进入或切换 Session 后，页面会立即并行恢复 metadata、Workspace、Console history 和 SSE，同时用 `POST /mycode/api/sessions/{session_id}/activate` 在后台 warm Sandbox 与 `mycode agent --continue`。刷新页面或重连 SSE 不停止 Runtime，也不会创建新的 Agent process。

Sandbox 只挂载这两个目录。真实 Provider Key 只存在于 FastAPI Host；Sandbox 仅得到内部 Relay token、Relay URL、模型名以及 Host 明确配置的 MyCode Runtime 参数。

FastAPI 进程内维护一个最多 2 个存活 Sandbox 的 Runtime Pool。空闲 Sandbox 在没有资源竞争时最多 warm 保活 2 小时；容量已满但存在 idle Sandbox 时，会抢占最久未活动的 idle runtime。只有全部 slot 都在 `running` 或 `waiting_permission` 时，新 turn 才进入有界 FIFO Queue。runtime 被抢占或回收不会删除 Workspace、MyCode state 或 SQLite Session，后续仍通过 `mycode agent --continue` 恢复。

当前 CLI 是面向人的文本协议。Web adapter 原样转发 stdout chunk，只识别 `you> ` 和现有 Permission prompt；Browser 多行消息仅在写入 CLI stdin 前折叠为单行，权限回答仍独立写入 `y\n` 或 `n\n`。

Workspace 支持上传、文件树、预览、下载以及安全删除文件/目录。Host 使用 `watchfiles` 观察每个 Session 的真实 Workspace；变化通过该 Session 的 SSE 发出，前端 debounce 后刷新文件树和当前 Preview。Web Agent Console 将用户消息、Assistant/Tool 输出、Permission 和错误保存到 SQLite，每个 Session 最多保留最近 500 条聚合记录。

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

当前 Runtime Pool、FIFO Queue 和调度锁都是 FastAPI 进程内状态，因此第一版部署**必须只运行一个 FastAPI worker**。多个 worker 会各自维护独立容量和 Queue，无法保证全局最多 2 个 Sandbox。本版本不引入 Redis 或共享 Queue。

production 推荐启动命令：

```sh
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

`.env` 至少设置：

```dotenv
MYCODE_PROVIDER_API_KEY=真实密钥
MYCODE_PROVIDER_BASE_URL=https://provider.example.com/v1
MYCODE_MODEL=模型名
```

Sandbox 调度与资源默认值可按需覆盖：

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

`SANDBOX_IDLE_TTL_SECONDS` 只回收进程/容器；`SESSION_RETENTION_SECONDS` 根据 SQLite `last_active_at` 删除真正过期的 Session 目录和元数据。默认分别为 2 小时和 14 天。

不要提交 `.env`。验证 FastAPI：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/mycode/api/health
```

## 构建 Sandbox Image

Dockerfile 使用名为 `mycode` 的 BuildKit context，只从外部 MyCode source 复制 `README.md`、`pyproject.toml`、`uv.lock` 和 `mycode/`。Web 仓库不保存 MyCode 副本，MyCode 根 lock 仍是 Sandbox dependency source of truth。

本机常见目录为 `../mycode-project`：

```powershell
.\scripts\build-sandbox.ps1 -MyCodeSource ..\mycode-project
```

Linux 服务器可以把核心仓库放在 `../mycode`：

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

source path 是命令参数，不写死 Windows 路径或 `/opt/mycode`。Dockerfile 按 `Python/uv -> MyCode lock dependencies -> system tools -> MyCode source` 分层，源码变化不会使 dependency layer 失效。

## 安装并启动 Vue

```powershell
Set-Location .\frontend
npm install
npm run dev
```

浏览器打开 <http://localhost:5173/mycode/>。Vite base 固定为 `/mycode/`，并把 `/mycode/api/*` 代理到 `http://127.0.0.1:8000`。

## 测试

```powershell
uv sync
.\.venv\Scripts\python.exe -m pytest .\tests -q
Set-Location .\frontend
npm run build
```

Server tests 使用临时 SQLite/Workspace 和 fake Sandbox，不访问真实 Provider，也不启动 Docker。

## 当前限制

- Demo 身份只有随机 HttpOnly Cookie，没有正式账号系统。
- FIFO Queue 只保存在 FastAPI 进程内，服务重启后不会恢复排队项。
- SSE history 只在进程内；CLI 文本不是结构化跨进程协议。
- FastAPI 不恢复或接管 crash 前的旧 Sandbox process；startup 会按 `mycode-web.managed=true` label 清理 orphan Sandbox，但保留 Workspace、MyCode state 和 SQLite Session，后续新 Sandbox 通过 `mycode agent --continue` 恢复 Session。
- Linux 部署需另行验证 bind mount UID/GID 与 Docker bridge 到 Relay 的网络边界。

设计原因见 `docs/adr/0001-web-demo-cli-sandbox-relay.md`，实现和验证记录见 `docs/实施记录.md`。
