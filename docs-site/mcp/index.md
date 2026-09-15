# MCP

MyCode 通过 MCP（Model Context Protocol）接入外部工具与数据源，从而在不修改 Agent 自身的前提下扩展可用能力。

本页用于验证文档站布局、代码高亮与右侧目录（Outline）。

::: warning 内容待补充
本页目前是测试页面。MyCode 的 MCP 配置字段尚未在本文档中确认，下面的 TOML 仅为示意结构，字段名请以正式文档为准。
:::

## MCP Client

MyCode 作为 MCP Client，连接一个或多个 MCP Server，并把 Server 暴露的工具纳入 Agent 的工具集合。

- Server 由用户配置，可来自本地进程或远程服务；
- 工具调用仍受 MyCode 的权限机制约束；
- 项目级 MCP 需要经过信任确认后才会启用。

## Transport

MCP 支持两种传输方式：

- `stdio`：MyCode 以子进程方式启动 MCP Server，通过标准输入输出通信，适合本地工具；
- `Streamable HTTP`：连接远程 MCP Server 的 HTTP 端点，适合部署在服务端的工具。

## 配置

MCP Server 通过配置文件声明，每个 Server 需要指定启动命令或远程地址，以及使用的 transport。

以下为示意配置，用于验证 TOML 语法高亮，字段名待确认：

```toml
# 示意配置，字段名请以正式文档为准

# stdio：以本地子进程方式启动
[mcp_servers.example-stdio]
transport = "stdio"
command = "example-mcp-server"
args = ["--flag"]

# Streamable HTTP：连接远程端点
[mcp_servers.example-http]
transport = "streamable-http"
url = "https://example.com/mcp"
```
