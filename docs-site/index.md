# MyCode Docs

MyCode 是一个使用 Python 实现、面向个人开发者的可扩展终端 Coding Agent。

本文档以 MyCode Core 为主体，介绍它的 Agent Loop、Tool System、Context、Memory、Session、SubAgent、MCP/Skill 与 Evaluation。MyCode Web 是独立的展示与调用层，本文档只把它作为 Core 的一个在线体验入口进行说明。


## 开始

* **[项目总览](/01-overview/)**
  了解 MyCode 的项目定位、整体架构、核心模块以及各部分之间的关系。

* **[快速开始](/00-getting-started/)**
  安装并运行 MyCode，了解 PyPI、CLI 和基础配置。

## 核心机制

* **[Agent 与 Runtime](/02-agent/)**
  Agent Loop、Tool Calling、Runtime 控制、验证与任务终止。

* **[Tool 系统](/03-tools/)**
  Tool Schema、Registry、参数校验、执行调度、Permission 与 HITL。

* **[Context / Memory / Session](/04-context/)**
  Context Window、ToolResult、Artifact 外部化、历史压缩与长期记忆。

* **[SubAgent](/05-subagent/)**
  Manager / Agent-as-tool、上下文隔离、任务委派、并发与会话持久化。

## 扩展机制

* **[Skills](/06-mcp-skill/skill)**
  Skill Discovery、Progressive Disclosure（渐进式披露）、按需加载、资源读取以及受控脚本执行。

* **[MCP](/06-mcp-skill/mcp)**
  MCP Client、stdio、Streamable HTTP，以及外部 MCP Tool 如何进入 MyCode 的 Tool System。

## 工程与评测

* **[Evaluation / Harbor](/07-evaluation/)**
  固定回归任务、重复评测、失败 Trial 与 Tool Trajectory 分析，以及如何用评测结果驱动 Agent 迭代。

* **[MyCode 演进与设计取舍](/08-evolution/)**
  记录 MyCode 开发过程中遇到的问题、方案演进、失败尝试以及最终的设计选择。

---

MyCode Core 可以独立运行，并已发布为 PyPI 包；Web Demo 是在线体验入口。

* [MyCode Core · GitHub](https://github.com/mmc-cloud/mycode)
* [PyPI 包](https://pypi.org/project/mycode-coding-agent/)
* [Web Demo](https://mycode.icu/web/)
* [项目主页](https://mycode.icu/)
