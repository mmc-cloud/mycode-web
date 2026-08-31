# ADR-0001：Web Demo 使用 CLI Adapter、Session Sandbox 与宿主 LLM Relay

## 状态

已接受，适用于第一版本地 Web Demo。

## 背景

MyCode 当前公开入口是长期运行的 `mycode agent --continue`：CLI 用 stdin 接收多轮用户消息，通过 stdout/stderr 输出面向人的终端文本，并由 `TerminalConfirmer` 阻塞等待 `y/N`。当前版本尚无稳定的 JSONL 或 AgentEvent 跨进程协议。Web Demo 同时必须隔离用户 Workspace 和真实 Provider API Key，又不能为 Web 重写 MyCode Agent Loop、Permission Policy、Tool 或 Context。

Web Demo 已从 MyCode 核心仓库拆分为独立 `mycode-web` 仓库，因此 Sandbox build 也必须从可配置的外部 MyCode source 获取 CLI 源码和 dependency lock，不能在 Web 仓库维护副本。

## 决策

1. `mycode-web` 是独立 Web Application 和 Git 仓库，拥有自己的 FastAPI/Vue 依赖、数据目录、测试和文档。
2. FastAPI 不 import MyCode package；一个活跃 Web Session 对应一个 Docker Sandbox 和一个长期 MyCode CLI 进程。
3. Sandbox image 使用 Docker BuildKit named context `mycode`。调用方显式传入 MyCode source path；Dockerfile 只复制其 `README.md`、`pyproject.toml`、`uv.lock` 与 `mycode/`。
4. Sandbox 只挂载当前 Session 的 `workspace` 与 `mycode_state`；后者映射到运行用户的 `~/.mycode`。
5. Web adapter 按 stdout byte chunk 增量解码并原样发布。小状态机只识别当前 `you> `、Permission 字段与 `是否批准？[y/N] `。
6. Browser User Message 的原文保留在 Web 层；只有写入 CLI stdin 的 payload 把 CRLF/LF、连续换行和相邻空白规范化为一个空格，确保一个 Browser message 只产生一个 CLI turn。
7. 浏览器权限按钮只向原 CLI stdin 写 `y\n` 或 `n\n`，不经过消息规范化，也不改变 Permission Policy。
8. Sandbox 使用内部 Relay token 和 OpenAI-compatible Relay URL；FastAPI 使用 Host Provider 配置转发 streaming response，真实 Key 不进入 Sandbox。
9. Workspace 是项目文件事实源，SQLite 只保存 Web User/Session 元数据；文件 API 和 ZIP extraction 必须执行 boundary、symlink 和容量检查。

## 影响

优点：Web 与 MyCode 源码和依赖真正独立；本机和 Linux 服务器只需提供不同 source path；MyCode lock 继续决定 Sandbox 依赖；后续可以在 adapter 边界替换结构化协议。

代价：第一版仍依赖人类终端文案；FastAPI 重启不能接管旧 stdin/stdout；SSE replay 只在内存中；Docker host alias 和 bind mount 权限需按部署平台验证。

## 暂不实施

本 ADR 不引入 JSONL Presenter、TUI、自然语言权限判断、Permission Scope、Queue、Sandbox 自动回收、Nginx/HTTPS、生产账号或服务器部署。
