# MyCode Web 运行环境

你当前运行在 MyCode Web 提供的临时 Linux Docker Sandbox 中。这是供用户体验 Coding Agent 的轻量开发环境。

## 工作目录与持久化

- `/workspace` 是用户项目的根目录。项目源码、配置和项目依赖应保存在这里。
- 同一 Web 用户的不同 Session 共享 Workspace，但 Agent 对话状态相互独立。
- Sandbox 可能因空闲或资源限制被回收并重新创建。Workspace 中的文件会保留，容器临时文件系统中的修改不会保留。

## 开发工具与依赖

- 环境预装 Python 3.11、pip、uv、Node.js 24、npm、npx、Git、Bash、curl、wget、ripgrep（rg）和 jq。
- Python 项目优先使用 Workspace 内的 `.venv` 管理依赖。
- JavaScript 项目优先通过 `npm install` 安装本地依赖，使用 `npm run` 或 `npx` 执行项目工具。
- 不要假设其他编程语言、编译器或系统服务已经安装；必要时先检查。
- 当前环境使用非 root 用户，通常不能执行需要管理员权限的系统级安装。
- 避免不必要的大型依赖下载和资源密集型操作。

## Terminal 与命令执行

- Agent 命令工具和 Web Terminal 共享 Workspace 与预装软件，但属于独立进程。
- 不要假设某个 Shell 中执行的 `cd`、`export` 或环境激活操作会自动影响其他命令进程。
- 优先使用明确的工作目录和项目级命令完成任务。
