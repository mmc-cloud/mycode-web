# LLM Relay 压缩响应问题记录：已定位，本地正式修复

## 当前状态（2026-10-14）

**问题已定位；正式修复已在本地实施和验证，等待代码审查。腾讯云仍未部署本次修复，尚未执行修复后的真实 Agent 验证。** 下面的旧诊断记录作为历史快照保留，其中“待验证”结论与临时诊断部署计划已经被本节替代，旧诊断补丁不再用于部署。

用户明确批准的一次腾讯云短请求已得到真实证据（该请求发生在正式修复之前）：HTTP 200，实际 Accept-Encoding 为 `gzip, deflate`，Content-Encoding 为 `gzip`，Content-Type 为 `text/event-stream; charset=utf-8`，原始前缀为 `1F 8B`，Gzip magic 为 true。该请求未部署代码、未修改配置、未重启服务，也未输出凭据或正文。本轮修复没有再发送真实 Provider 请求。

正式根因是原 Relay 使用 `aiter_raw()` 转发编码后的 body，同时响应头白名单删除 Content-Encoding；下游 HTTPX/OpenAI SDK 因而无法正确解压，再按 UTF-8 解码 SSE 时遇到 0x8B。

### 正式修复和清理

- `app/services/relay.py` 改用 `response.aiter_bytes()`，复用 HTTPX 的增量内容解码，保留 `finally: await response.aclose()`，不增加完整响应缓冲或自制解压器。
- 保持 Content-Type/Cache-Control/X-Request-Id/Retry-After 白名单与原状态码。删除上游压缩 Content-Encoding、原压缩 Content-Length 及逐跳 Transfer-Encoding，符合解码后的 body。
- FastAPI 路由未改动；StreamingResponse 的测试确认未添加这些上游头。实际 HTTP 服务器可能生成自己的分块传输头，这是下一跳的正常传输方式，不是照搬上游压缩长度。
- 删除临时前缀观察、诊断 logger/辅助函数、config 配置和 `.env.example` 开关。config.py 和 .env.example 已恢复为原 HEAD 内容，不再要求任何诊断环境变量。
- `tests/test_relay_diagnostics.py` 已移除，其中有效的 Gzip/SSE/状态/清理/无敏感日志覆盖改写并扩展为 `tests/test_relay_streaming.py`；原 `tests/test_relay.py` 的无压缩 FakeResponse 接口更新为 aiter_bytes。
- 新增可选 `tests/test_relay_sdk.py`，通过 Provider MockTransport → 真实 HTTPX 解码 → Relay → 下游 MockTransport → OpenAI SDK 验证。未访问外部服务。

### 正式验证结果

```powershell
.venv/Scripts/python.exe -m pytest tests/test_relay.py -q
# 19 passed

.venv/Scripts/python.exe -m pytest tests/test_relay.py tests/test_relay_streaming.py tests/test_relay_sdk.py -q -rs -p no:cacheprovider
# 52 passed, 1 skipped（Web venv 没有 OpenAI SDK）

../mycode-project/.venv/Scripts/python.exe -m pytest tests/test_relay_sdk.py -q -p no:cacheprovider
# 1 passed；现有 SDK 2.43.0 / HTTPX 0.28.1；无需安装依赖或修改 Core

.venv/Scripts/python.exe -m pytest -q
# 268 passed, 5 skipped；无新增失败

.venv/Scripts/python.exe -m compileall -q app/config.py app/services/relay.py app/api/routes.py tests/test_relay.py tests/test_relay_streaming.py tests/test_relay_sdk.py
# passed

git diff --check
# passed
```

新增 33 项流测试覆盖未压缩/Gzip/deflate SSE、整块/压缩头跨块/逐字节分块、429/500/503 的压缩 JSON、原请求 body 与认证、状态/白名单、取消/提前关闭/网络异常、损坏编码头/CRC/deflate 和后段损坏，以及 FastAPI 响应头与客户端断开清理。首次解压输出到达时，上游还未读完：第一次 SSE event 在 5 个上游块中的第 3 块读完后返回，第二次在第 4 块返回，最终 DONE 在剩余块返回。OpenAI SDK 读到 `Hello`、` World` 两个正常 Delta，无 UnicodeDecodeError。

本轮记录了 25 个无关文件的 SHA256，确认 Vue 源码与测试、前端依赖文件、API routes、Runtime、Web pyproject 和 lock 全部保持原样。没有升级 HTTPX/OpenAI SDK，没有新增依赖，没有修改模型配置、Token、Core、Session/Workspace 或 Sandbox 启动机制。

已知验证边界：

- Web 全量中的四项 Windows 符号链接测试跳过不计为通过；SDK 在 Web venv 的缺依赖跳过已通过上述现有 SDK 环境单独补测。
- 原精确 pytest 命令保留既有 Starlette TestClient `httpx` 弃用警告，及本机 pytest 缓存目录权限警告；不影响测试断言结果。使用 `-p no:cacheprovider` 可避开缓存警告。
- HTTPX 0.28.1 识别出的压缩损坏会抛 DecodingError，本实现不吞异常并关闭 Response；已覆盖 Gzip CRC、无效压缩头和坏 deflate。但该版本仅缺失 Gzip 尾部 trailer 时不保证报错：纯 Mock 实测去掉最后 8 字节仍可返回完整正文。遵照使用既有 HTTPX、禁止自制解压器和不自行升级的范围，本轮没有添加完整性校验。这一底层限制不能被描述成“检测所有截断或损坏”。
- 解码错误可能发生在状态码已发送之后，此时终止流并清理资源，不能再改写已经发送的 HTTP 状态，也不会伪造正常 SSE/DONE。
- 本次是本地协议与生命周期验证，修复后的腾讯云真实 Agent 端到端验证仍待后续部署完成后单独执行。

### 腾讯云正式部署建议（本轮未执行）

上轮已核对生产未部署临时诊断版本。因此正式运行代码仅需更新 `/opt/mycode-web/app/services/relay.py`；不需要更改 Provider/模型/API Key/环境变量，不需要安装依赖、更新 Core、重建 Docker Sandbox 或发布 Vue。测试文件和本记录可随审查纳入源码，但不是运行必需文件。

代码审查通过后，在后续明确授权的维护窗口复核生产版本和目标文件 hash、备份原 relay.py、应用只含 relay.py 的正式补丁、执行 Python 编译检查，再重启 `mycode-web` 并检查 health。服务重启会中断现有 SSE/Terminal，重置进程内 Runtime/Token，可能停止当前 Sandbox，需先结束正在运行的任务。然后执行一次正常 Agent 流式请求确认，不直接加真实 Provider 探测请求。本轮没有执行上述任何远程动作。

若部署失败，恢复此次备份的 relay.py 并在维护窗口重启；不做整仓 reset。不使用旧诊断补丁或旧开关部署计划，也不把尚未审查的前端修改带入后端修复。

---

## 以下为正式修复前的历史诊断快照（旧计划已废弃）

诊断日期：2026-10-14（Asia/Singapore，UTC+8）。本轮仅增加可关闭的诊断与测试，未修改转发策略、模型配置、Vue、MyCode Core，未 Commit/Push/部署/重启生产服务，未发送额外真实模型请求。

## A. 已确认事实

### 腾讯云只读检查

| 项目 | 证据 |
| --- | --- |
| Web 服务 | `mycode-web` active/running；PID 1100881；工作目录 `/opt/mycode-web` |
| 当前 Web HEAD / 部署记录 | `011bf9b3ee6aaa4f61a4dad56e63caf05bd0db27`； tracked 工作区干净 |
| Core HEAD / 部署记录 | `2ea4c9bd57d2771bdd27cee019455fc140abe15e`； tracked 工作区干净 |
| 当前实际进程环境 | Provider `https://api.deepseek.com`；模型 `deepseek-flash` |
| 旧配置备份 | `.env.backup-20261014-082327`：Provider `https://opencode.ai/zen/go/v1`，模型 `mimo-v2.6-flash` |
| 当前配置文件修改时间 | 2026-10-14 08:29:05 +08:00；这是文件修改时间，不等同于精确切换发生时间 |
| 当前进程启动时间 | 2026-10-14 08:31:30 +08:00；NRestarts=0 |
| Sandbox Relay URL | `http://host.docker.internal:8000/web/api/relay/v1` |
| HTTP 客户端版本 | 生产和本地均 HTTPX 0.28.1 / httpcore 1.0.9 |
| 默认 Accept-Encoding | 生产虚拟环境与本地均 `gzip, deflate`；不是示例中的 `gzip, deflate, br` |
| HTTP 代理环境变量 | 当前进程 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 及小写变体都未设置 |
| FastAPI 中间件 | 已检查 `app/main.py`，未安装 GZipMiddleware |
| Nginx | 有全局 `gzip on`，API 代理 buffering/cache off；上述内部 Relay URL 直接到 8000，绕过公网 Nginx |
| 诊断开关 | 当前生产进程 MYCODE_RELAY_DIAGNOSTICS 未设置，生产尚未部署诊断代码 |

生产 `relay.py/routes.py/main.py/config.py/pyproject.toml/uv.lock` 归一化换行后的 SHA256 全部与本地修改前相同。例如原 Relay SHA256：`9d167bef0669614ae280458ef24828f083626bdec7df376737c0a4173186f36e`；原 config SHA256：`59c59cc280674a0899d85cc3b290f8c8e45e43b684feac2071ca017cc9bcccf8`。这是部署文件及运行进程元数据的一致性检查，不是对 Python 内存中每个对象的快照。

未输出 API Key、Authorization、Cookie、完整环境、完整请求头或生产消息正文。环境检查只打印指定的 Provider/模型/Relay/诊断开关，以及代理变量是否存在。

### 真实 Provider 响应证据尚未采集

| 问题 | 当前结论 |
| --- | --- |
| Provider 是否返回压缩数据 | 尚未实测；错误与 Gzip 假设吻合 |
| Provider Content-Encoding | 尚未采集，不能填写 gzip 为生产实测结果 |
| 实际那次请求的 Accept-Encoding | 尚未采集；运行客户端默认值是 gzip, deflate，按当前无覆盖的源码预期如此 |
| 原始响应是否以 1F 8B 开始 | 尚未采集生产流；目前只在本地 Gzip fixture 中实测 |
| Relay 会不会把压缩原文与标识分离 | 源码和本地路由测试已确认这一条件性行为 |

## 字节和响应头链路

| 层 | 当前行为 |
| --- | --- |
| Provider → HTTPX | 实际上游编码未知。`send(stream=True)` 不提前读取整个响应体 |
| HTTPX → Relay | `response.aiter_raw()`：HTTP body 尚未进行 Content-Encoding 解压。HTTP Transfer-Encoding 的分块框架已由 HTTP 栈处理，它不是包含 chunk 长度行的完整线上报文 |
| Relay | 原 Chunk 原样 yield。仅允许 content-type/cache-control/x-request-id/retry-after，因此 content-encoding/content-length/transfer-encoding 不转发 |
| FastAPI StreamingResponse | 接收 bytes 就直接写 ASGI body，没有在这里 UTF-8 解码，也没有自动 Gzip 解压；下一跳重新生成传输分块是正常行为 |
| Sandbox | Docker host 别名连接 FastAPI 的 8000 端口，本链路不经过公网 Nginx |
| OpenAI SDK | 已检查 Core 使用 `OpenAI`；SDK Stream 读取 HTTPX `iter_bytes()`，SSEDecoder 再将行按 UTF-8 严格解码。若 gzip body 没有 Content-Encoding，HTTPX 无法按该编码解压 |

[HTTPX 官方流式文档](https://www.python-httpx.org/async/#streaming-responses) 明确区分 `aiter_raw()` 与内容解码；[OpenAI SDK SSE 源码](https://github.com/openai/openai-python/blob/main/src/openai/_streaming.py) 可核对解码路径。

## B. 根因判断

**已确认：** Relay 存在条件性缺陷：上游返回编码后的响应体时，当前 raw 转发与 Content-Encoding 白名单不一致。生成有效 Gzip SSE 后，经真实 HTTPX MockTransport、LLMRelay 和 FastAPI 路由，输出 bytes 与输入压缩 bytes 完全相同，Content-Encoding 缺失。

**本地机制复现：** 另用现有 Core 虚拟环境 OpenAI SDK 2.43.0 / HTTPX 0.28.1，仅接 MockTransport（真实 Provider 请求数为零）。同一 Gzip SSE、文件头 1F 8B，在缺少 Content-Encoding 时出现 UnicodeDecodeError，位置 1、字节 8B；只在 mock 响应中提供 `Content-Encoding: gzip` 时 SDK 正常读取一个回答 Chunk。本轮没有把这一对照测试方案用于实际 Relay 修复。

**高置信度推断：** Provider 切换后，上游/其代理的响应压缩行为可能暴露了已有的 Relay 缺陷。这同时解释了错误字节和代码链路。

**未确认：** 本次生产失败是否确实收到 gzip、该压缩由 Provider 还是其边缘代理产生、上游是否自己缺失编码标识。1F 8B 仅是签名判断，诊断工具不验证整份数据可解压，也不证明 SSE 格式有效。

## C. 为什么以前可能正常

有证据支持 Provider/模型配置不同：旧备份为 OpenCode/MiMo，当前进程为 DeepSeek/DeepSeek Flash；配置修改与当前启动时间都在本日上午。备份不能单独证明每一次旧请求都成功，或旧 Provider 的 Content-Encoding 一直为空。

Git 历史显示初始 Relay 提交 `d17516a`（2026-08-31）已使用 `aiter_raw()`，当时也没有转发 Content-Encoding；2026-09-07 的 `10fb4a5` 主要增加 OpenCode 标识和 retry-after，没有改变压缩策略。Web lock 仍为 HTTPX 0.28.1，生产和本地安装版本相同。未找到本次故障期间 Relay 压缩实现、HTTPX 版本或显式 HTTP 代理的新增变化证据。

因此，“换 Provider 后压缩策略不同，暴露潜在缺陷”有具体配置证据支持，仍需真实响应日志完成闭环。旧响应头/原始字节历史、Provider 边缘代理变更历史没有采集记录，无法确认，不能编造。

## 临时诊断实现

- `app/config.py` 增加 `relay_diagnostics`，读取 MYCODE_RELAY_DIAGNOSTICS，默认 false，复用现有布尔校验。
- `.env.example` 仅补充 false 的参考值，不覆盖生产 `.env`。
- `app/services/relay.py` 仅累积开头最多两个字节，允许跨 Chunk；不预取后续数据，不改编码/状态/白名单，不创建额外请求。
- 足够两个字节后输出一次 `relay_diagnostics {JSON}`；不足两个字节时，在流结束/取消/关闭中记录已观察前缀，gzip_magic 为 null，避免把不足数据误判成 false。
- 仅输出 upstream_status、upstream_content_type、upstream_content_encoding、upstream_transfer_encoding、upstream_content_length_present、request_accept_encoding、raw_prefix_hex、gzip_magic、forwarded_content_encoding_present。
- Accept-Encoding 取自 HTTPX response.request.headers（包括默认值及 request hook 的变化），不是只看显式 headers 字典。
- 诊断使用 WARNING，适应既有 Uvicorn 输出，无需开启 HTTPX verbose logging。异常 handler 不阻断正常转发；不增加异常详情日志。
- 元数据值限制 256 字符，含控制字符或反射 Provider Key 时仅日志显示 [redacted]，原转发值不改。
- 完整结束、部分关闭、CancelledError 和 ReadError 都保留 response.aclose；默认关闭不输出诊断日志。

## 测试

新增 `tests/test_relay_diagnostics.py`，25 项测试。覆盖普通/Gzip SSE、不同状态码、签名跨 Chunk/空 Chunk、空流/单字节/编码头与签名矛盾、默认关闭、正文/API Key/Cookie/工具参数不入日志、实际 request hook 后 Accept-Encoding、无预取/无多请求、原 Chunk 顺序、取消/关闭/失败/日志 handler 故障下资源关闭，以及 FastAPI 路由原压缩内容保持不变。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_relay.py tests/test_relay_diagnostics.py tests/test_config.py -q -p no:cacheprovider
# 48 passed

.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
# 260 passed, 4 skipped

git diff --check
# passed
```

保留既有 StarletteDeprecationWarning：`httpx` TestClient 兼容路径被弃用，建议 httpx2；本轮未更换测试依赖。针对性诊断测试无跳过。已用 `-rs` 单独核对四项跳过：Workspace 三项和 Lifecycle 一项，均因当前 Windows 主机无法创建符号链接；它们不作为通过证据。

## D. 修复方案比较（仅建议）

| 方案 | 优点 | 兼容性与风险 |
| --- | --- | --- |
| `aiter_bytes()`，解压后转发 | 更贴合 Relay 对下游输出 SSE/JSON 的职责；Gzip/deflate 增量解压，无需整包缓冲 | 必须继续删除 Content-Encoding 与原压缩 Content-Length；br/zstd 依赖可用解码器；坏压缩流会解码失败；上游未声明编码时不能自动识别并修复 |
| raw body + 正确 Content-Encoding | 保持字节透明，解压交给下游 HTTP 客户端；也支持服务端不认识的编码 | 必须保证标识与字节一致，下游要支持对应编码；不能把上游 Transfer-Encoding/Connection 等逐跳头照搬；压缩相关长度/校验元数据需按实际输出处理 |
| 上游 Accept-Encoding: identity | 可用作临时规避或对照，减少压缩协商 | 依赖 Provider/代理遵循协商，不能保证永远无压缩；带宽增加；不是底层 raw/header 一致性问题的完整修复 |

若真实日志确认上游正确标注 gzip 且当前 raw 流有 Gzip 签名，优先建议 `aiter_bytes()` 增量解压后维持下游无 Content-Encoding 的策略。若上游签名为 1F 8B 但 Content-Encoding 缺失，则需要先调查上游错误标注，不能声称直接换 aiter_bytes 就能修复。本轮三种方案都未实施。

## 真实采集部署计划（尚未执行，必须先获得确认）

诊断代码尚未在生产服务内，过去日志没有这些字段。需要短维护窗口，避开正在运行的 Agent 任务，先关闭当前 SSE/Terminal 连接。FastAPI 重启会重置进程内 Runtime/Relay Token/SSE 状态，可能停止现有 Sandbox；不修改持久化会话文件、Workspace、模型 Provider 配置。开启与关闭各需要一次重启，不能承诺不中断连接。

### 1. 审核与准备

仅将本轮 `app/config.py` 和 `app/services/relay.py` 的补丁上传至暂存目录。不要上传 `.env`、不要覆盖 `.env.example` 到生产、不运行完整 deploy-server.sh（它还会 pull/build/部署其他内容），不部署前端或 Core。

审批后先核对生产 HEAD 仍为上述版本、两个原文件 hash 未变、tracked 工作区干净；将两个原文件备份到权限 0700 的 `/var/tmp/mycode-relay-diagnostics-20261014/backup/`，随后：

```bash
cd /opt/mycode-web
git apply --check /var/tmp/mycode-relay-diagnostics-20261014/relay-diagnostics.patch
# check 成功且完成备份后，才执行：
git apply /var/tmp/mycode-relay-diagnostics-20261014/relay-diagnostics.patch
```

补丁只含上述两个 Python 文件，不含修复实现和依赖变化。本地报告之外另提供 UTF-8 patch artifact 供审核；上传、apply 均尚未执行。

### 2. 临时开启与重启

使用 `/run` 下独立环境文件和 systemd runtime drop-in，可避免编辑原生产 `.env` 与模型配置。以下步骤同样需要确认后执行：

```bash
install -d -m 0755 /run/systemd/system/mycode-web.service.d
printf '%s\n' 'MYCODE_RELAY_DIAGNOSTICS=true' > /run/mycode-relay-diagnostics.env
chmod 0600 /run/mycode-relay-diagnostics.env
printf '%s\n' '[Service]' 'EnvironmentFile=/run/mycode-relay-diagnostics.env' \
  > /run/systemd/system/mycode-web.service.d/90-relay-diagnostics.conf
systemctl daemon-reload
systemctl restart mycode-web
systemctl is-active mycode-web
curl --fail --silent http://127.0.0.1:8000/web/api/health
```

新增 EnvironmentFile 位于已有文件之后，仅覆盖诊断开关。启用后，批准范围内通过一次正常 Agent 请求采集；不直接拿真实 Provider Key 发探测请求。一次 Agent 运行可能自身产生多个正常模型调用，诊断工具不增加调用次数。

```bash
# 限定采集窗口，只读取新诊断 JSON，不输出其他请求/消息日志：
journalctl -u mycode-web --since '<确认后的观察窗口起点>' --no-pager -o cat \
  | grep '^relay_diagnostics '
```

核对 Content-Encoding、实际 Accept-Encoding、prefix 和转发标识。若一次请求没有日志，应检查开关/日志路径/是否进入该 Relay 流，不自动追加模型请求。

### 3. 关闭与回滚

成功采集后，在确认的维护窗口删除本轮新增的两个 runtime 文件，daemon-reload 并重启：

```bash
rm -f /run/systemd/system/mycode-web.service.d/90-relay-diagnostics.conf \
      /run/mycode-relay-diagnostics.env
systemctl daemon-reload
systemctl restart mycode-web
systemctl is-active mycode-web
curl --fail --silent http://127.0.0.1:8000/web/api/health
```

若需要撤销代码，从本轮备份恢复两个原文件，再于维护窗口重启。不能使用整仓 reset，也不能覆盖其他生产改动。临时 apply 会留下生产 tracked 修改，后续常规部署脚本会拒绝脏工作区；需要先回滚此补丁，或在后续审查后正式纳入发布。

目前停在本地交付与只读证据阶段，等待用户明确批准是否部署这两份诊断代码、暂时启用开关、安排两次服务重启并观察一次正常 Agent 请求。
