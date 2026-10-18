// Browser regression checks with isolated API, SSE and WebSocket fixtures.
// Run from the repository root, using a dedicated browser session:
//   npm --prefix frontend run dev -- --port 5178 --strictPort
//   npx --yes --package @playwright/cli playwright-cli -s=mycode-ui open about:blank --browser msedge
//   npx --yes --package @playwright/cli playwright-cli -s=mycode-ui run-code --filename frontend/tests/ui-smoke.js --raw
//   npx --yes --package @playwright/cli playwright-cli -s=mycode-ui close
// Screenshots are saved under output/playwright/. No real backend is accessed.
// This file is a playwright-cli run-code function, not an @playwright/test spec.
async (page) => {
  const checks = []
  const check = (condition, label) => { if (!condition) throw new Error(label); checks.push(label) }
  const errors = []
  const imageRequests = []
  let markdownPerformance
  let shortStreamingPerformance
  const scrollSamples = []
  const composerHeights = {}
  let failMessage = false
  let holdMessage = false
  let releaseMessage = null
  let nextCreatedHistory = []
  let holdHistory = false
  let releaseHistory = null
  let failContext = false
  let holdContext = false
  let releaseContext = null
  const heldContents = new Map()
  const permissionFixtures = new Map()
  const contextFixture = { estimated_input_tokens: 1000, max_input_tokens: 10000, context_window_tokens: 12000, source_message_count: 4, model_visible_message_count: 4 }
  const waitForFixture = async (ready) => {
    const deadline = Date.now() + 5000
    while (!ready()) {
      if (Date.now() > deadline) throw new Error('Fixture request did not arrive')
      await page.waitForTimeout(10)
    }
  }
  const activity = (view) => page.locator('.activity-bar').getByRole('button', { name: view, exact: true })
  const showSidebar = async (view) => {
    if (await activity(view).getAttribute('aria-expanded') !== 'true') await activity(view).click()
    await page.locator(view === 'Sessions' ? '.sessions-panel' : '.workspace-panel').waitFor({ state: 'visible' })
  }
  try {
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('https://markdown-image-test.invalid/**', async (route) => {
    imageRequests.push(route.request().url())
    await route.fulfill({ contentType: 'image/png', body: '' })
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  const code = 'const message = "<hello> & world";\nconsole.log(message);\n'
  const markdown = '# 实现完成\n\n这里是 **重点** 和 *说明*，包含 `inline_code`。\n\n## 修改内容\n\n- 浅色工作台\n- Markdown 阅读\n\n1. 安装依赖\n2. 执行构建\n\n> 保留业务数据流。\n\n---\n\n| 文件 | 说明 |\n| --- | --- |\n| SessionApp.vue | 消息展示组件接入 |\n| style.css | 统一设计变量 |\n\n```javascript\n' + code + '```\n\n```unknown-language\n<untrusted> & raw_text\n```\n\n[安全链接](https://example.com)\n\n<script>window.__xss=1</script>\n<img src=x onerror="window.__xss=2">\n[恶意链接](javascript:alert(1))\n\n![跟踪图片](https://markdown-image-test.invalid/tracker.png)\n\n![参考图片][tracking]\n\n[tracking]: https://markdown-image-test.invalid/reference.png\n\n[![徽章](https://markdown-image-test.invalid/badge.png)](https://example.com)\n'
  const sessions = [
    { id: 'ui-alpha', name: 'UI 重构演示', runtime_status: 'idle', active_turn_id: null, event_cursor: 10, context_status: contextFixture },
    { id: 'ui-beta', name: '历史会话', runtime_status: 'idle', active_turn_id: null, event_cursor: 20 },
  ]
  const histories = {
    'ui-alpha': [
      { id: 1, kind: 'user', content: '请完成第一轮 UI 重构。', data: { turn_id: 'turn-a' } },
      { id: 2, kind: 'tool', content: 'tool raw **must not render**', data: { turn_id: 'turn-a', event_type: 'tool_call', event: { tool_call: { name: 'read_file', arguments: { path: 'frontend/src/style.css' } } } } },
      { id: 3, kind: 'assistant', content: markdown, data: { turn_id: 'turn-a' } },
    ],
    'ui-beta': [
      { id: 1, kind: 'user', content: '旧会话用户内容 <script>plain</script>', data: { turn_id: 'turn-b' } },
      { id: 2, kind: 'assistant', content: '## 历史记录\n\n从另一个 Session 加载。', data: { turn_id: 'turn-b' } },
    ],
  }
  const requests = []
  const longFileName = 'long_filename_'.repeat(35) + '.md'
  const fileEntries = [{ name: 'README.md', path: 'README.md', kind: 'file', size: 20 }, { name: 'src', path: 'src', kind: 'directory', children: [{ name: 'index.js', path: 'src/index.js', kind: 'file', size: 40 }, { name: longFileName, path: 'src/' + longFileName, kind: 'file', size: 60 }] }, { name: 'npm', path: 'npm', kind: 'symlink' }]
  await page.route('**/web/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace('/web/api', '')
    requests.push({ path, query: url.search, method: route.request().method(), body: route.request().postData() })
    const match = /^\/sessions\/([^/]+)(.*)$/.exec(path)
    let data
    if (path === '/sessions') {
      if (route.request().method() === 'POST') {
        data = { id: 'ui-created', name: 'Session', runtime_status: 'idle', active_turn_id: null, event_cursor: 30 }
        sessions.unshift(data); histories[data.id] = nextCreatedHistory
      } else data = { sessions, has_created_session: true }
    }
    else if (match) {
      const [, id, suffix] = match
      if (!suffix) {
        if (route.request().method() === 'DELETE') {
          sessions.splice(sessions.findIndex((session) => session.id === id), 1)
          await route.fulfill({ status: 204 }); return
        }
        data = sessions.find((session) => session.id === id)
        if (route.request().method() === 'PATCH') data.name = JSON.parse(route.request().postData()).name
      }
      else if (suffix === '/activate') data = { status: 'idle' }
      else if (suffix === '/console') {
        if (holdHistory) await new Promise((resolve) => { releaseHistory = resolve })
        data = { events: histories[id] }
      }
      else if (suffix === '/files/tree') data = { entries: id === 'ui-beta' ? [{ name: 'beta.txt', path: 'beta.txt', kind: 'file', size: 10 }] : fileEntries }
      else if (suffix === '/files/content') {
        const held = heldContents.get(id + ':' + url.searchParams.get('path'))
        if (held) {
          heldContents.delete(id + ':' + url.searchParams.get('path'))
          await new Promise((resolve) => { held.release = resolve })
          if (held.status) { await route.fulfill({ status: held.status, contentType: 'application/json', body: JSON.stringify({ detail: '文件不存在' }) }); return }
        }
        data = { content: held?.content || '# workspace preview' }
      }
      else if (suffix === '/files/upload') {
        fileEntries.push({ name: 'note.txt', path: 'note.txt', kind: 'file', size: 15 }); data = { ok: true }
      }
      else if (suffix === '/files/download' || suffix === '/workspace/download') {
        const filename = suffix === '/files/download' ? url.searchParams.get('path').split('/').pop() : 'workspace.zip'
        await route.fulfill({ contentType: 'application/octet-stream', headers: { 'Content-Disposition': `attachment; filename="${filename}"` }, body: 'download fixture' }); return
      }
      else if (suffix === '/files' && route.request().method() === 'DELETE') {
        const index = fileEntries.findIndex((entry) => entry.path === url.searchParams.get('path'))
        if (index >= 0) fileEntries.splice(index, 1)
        await route.fulfill({ status: 204 }); return
      }
      else if (suffix === '/message') {
        if (holdMessage) await new Promise((resolve) => { releaseMessage = resolve })
        if (failMessage) {
          failMessage = false
          await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '消息发送失败测试' }) }); return
        }
        data = { status: 'running', turn_id: 'stream-turn' }
      }
      else if (suffix === '/permission') {
        const requestId = JSON.parse(route.request().postData()).request_id
        const fixture = permissionFixtures.get(id + ':' + requestId)
        if (fixture?.hold) await new Promise((resolve) => { fixture.release = resolve })
        if (fixture?.status) {
          await route.fulfill({ status: fixture.status, contentType: 'application/json', body: JSON.stringify({ detail: '权限提交失败测试' }) }); return
        }
        data = { ok: true }
      }
      else if (suffix === '/mcp-trust') data = { ok: true }
      else if (suffix === '/context-status') {
        if (holdContext) await new Promise((resolve) => { releaseContext = resolve })
        if (failContext) {
          failContext = false
          await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Context 查询失败测试' }) }); return
        }
        data = contextFixture
      }
      else if (suffix === '/compact') data = { status: 'compacted', reason: '测试完成' }
    }
    if (!data) throw new Error('Unexpected API: ' + path)
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
  })
  await page.addInitScript(() => {
    window.__MYCODE_STREAM_DEBUG__ = {}
    window.__uiTest = { sources: [], sockets: [], sent: [], scrollObservers: [] }
    const NativeResizeObserver = window.ResizeObserver
    window.ResizeObserver = class extends NativeResizeObserver {
      observe(target, options) {
        if (target.classList.contains('console-history')) {
          if (!window.__uiTest.scrollObservers.includes(this)) window.__uiTest.scrollObservers.push(this)
          this.__active = true
        }
        return super.observe(target, options)
      }
      disconnect() { this.__active = false; return super.disconnect() }
    }
    class MockEventSource extends EventTarget {
      constructor(url) { super(); this.url = url; this.closed = false; window.__uiTest.sources.push(this) }
      close() { this.closed = true }
    }
    class MockWebSocket extends EventTarget {
      static OPEN = 1
      constructor(url) {
        super(); this.url = url; this.readyState = 0; window.__uiTest.sockets.push(this)
        setTimeout(() => {
          if (this.readyState === 3) return
          this.readyState = 1; this.dispatchEvent(new Event('open'))
          this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'status', status: 'ready' }) }))
          this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'output', data: 'MyCode terminal ready\r\n$ ' }) }))
        }, 20)
      }
      send(data) { window.__uiTest.sent.push({ url: this.url, data: JSON.parse(data) }) }
      close() { this.readyState = 3 }
    }
    window.EventSource = MockEventSource
    window.WebSocket = MockWebSocket
    window.__uiTest.emit = (type, data) => {
      const source = window.__uiTest.sources.findLast((item) => !item.closed)
      source.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) }))
    }
  })
  await page.goto('http://127.0.0.1:5178/web/')
  const legacyLayout = JSON.stringify({ sessionsWidth: 360, workspaceWidth: 610, sessionsCollapsed: false, workspaceCollapsed: false, terminalHeight: 300, terminalCollapsed: false, workspaceTreeHeight: 240 })
  await page.evaluate((legacy) => { localStorage.removeItem('mycode.layout.v2'); localStorage.setItem('mycode.layout.v1', legacy) }, legacyLayout)
  await page.reload()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  check(await page.locator('.context-panel').count() === 0, 'metadata Context 状态不自动打开浮动面板')
  await page.locator('.terminal-status[data-status="ready"]').waitFor()
  await page.waitForFunction(() => { const el = document.querySelector('.console-history'); return el.scrollHeight - el.clientHeight - el.scrollTop <= 2 })
  check(true, '初始历史异步加载后实际定位底部')
  const migrated = await page.evaluate(() => JSON.parse(localStorage.getItem('mycode.layout.v2')))
  check(migrated.sidebarWidth === 320 && migrated.terminalHeight === 300 && migrated.workspaceTreeHeight === 240, 'v1 高度迁移且旧三栏宽度不作为 Sidebar 宽度')
  check(await page.evaluate(() => localStorage.getItem('mycode.layout.v1')) === legacyLayout, '旧布局数据原样保留')
  check(await page.locator('.activity-bar svg').count() === 2 && await page.locator('.activity-bar').evaluate((el) => el.getBoundingClientRect().width === 48), '固定 48px Activity Bar 使用两枚统一 SVG 图标')
  const navigationBefore = await page.evaluate(() => ({ sources: window.__uiTest.sources.length, sockets: window.__uiTest.sockets.length }))
  await activity('Workspace').hover()
  check(await page.getByRole('tooltip', { name: 'Workspace', exact: true }).isVisible(), '活动栏悬停 Tooltip 可见')
  await activity('Sessions').focus()
  await activity('Sessions').press('ArrowDown')
  check(await activity('Workspace').evaluate((el) => el === document.activeElement), '方向键在活动栏按钮间移动焦点')
  await activity('Workspace').press('Enter')
  check(await page.locator('.workspace-panel').isVisible() && !await page.locator('.sessions-panel').isVisible() && await page.locator('.sidebar-view:visible').count() === 1, 'Enter 切换到唯一 Workspace Sidebar')
  const openAgentWidth = await page.locator('.agent-panel').evaluate((el) => el.getBoundingClientRect().width)
  await activity('Workspace').press('Space')
  check(!await page.locator('.sidebar').isVisible() && await page.locator('.activity-bar').isVisible() && await activity('Workspace').getAttribute('aria-expanded') === 'false', '当前图标 Space 收起 Sidebar，Activity Bar 保持可见')
  const closedAgentWidth = await page.locator('.agent-panel').evaluate((el) => el.getBoundingClientRect().width)
  check(closedAgentWidth - openAgentWidth > 320, 'Sidebar 收起释放空间，Agent 自动扩宽')
  await activity('Workspace').click()
  await activity('Workspace').press('Home')
  check(await activity('Sessions').evaluate((el) => el === document.activeElement && getComputedStyle(el).outlineStyle === 'solid'), 'Home 键与键盘焦点样式')
  await activity('Sessions').press('Enter')
  check(await activity('Sessions').getAttribute('aria-pressed') === 'true' && await activity('Sessions').getAttribute('aria-controls') === 'workbench-sidebar', '当前视图高亮及 ARIA 关联')
  check(await page.evaluate((before) => window.__uiTest.sources.length === before.sources && window.__uiTest.sockets.length === before.sockets, navigationBefore), 'Sidebar 切换未重建 SSE 或 Terminal')
  check(await page.locator('.user-message-text').innerText() === '请完成第一轮 UI 重构。', '用户纯文本气泡')
  check(await page.locator('.message-content h1').count() === 1 && await page.locator('.message-content h2').count() === 1, 'Markdown 标题')
  check(await page.locator('.message-content strong').innerText() === '重点' && await page.locator('.message-content em').innerText() === '说明', '粗体与斜体')
  check(await page.locator('.message-content ul li').count() === 2 && await page.locator('.message-content ol li').count() === 2, '有序与无序列表')
  check(await page.locator('.message-content table tbody tr').count() === 2, 'Markdown 表格')
  check(await page.locator('.message-content blockquote').count() === 1 && await page.locator('.message-content hr').count() === 1, '引用与分隔线')
  check(await page.locator('.message-content .hljs-keyword').count() > 0 && await page.locator('.code-block-toolbar span').first().innerText() === 'javascript', '代码语言与语法高亮')
  check(await page.locator('.markdown-code-block').nth(1).locator('code').innerText() === '<untrusted> & raw_text\n', '未知语言纯文本回退')
  check(await page.locator('.message-content script, .message-content [onerror], .message-content a[href^="javascript:"]').count() === 0 && await page.evaluate(() => !window.__xss), '不可信 HTML 与恶意链接安全')
  check((await page.locator('.message-content').innerText()).includes('<script>window.__xss=1</script>'), '原始 HTML 可见为文本')
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  check(await page.locator('.message-content img').count() === 0 && imageRequests.length === 0, '行内/引用/链接内 Markdown 图片不产生网络请求')
  check((await page.locator('.markdown-image-placeholder').first().textContent()).includes('跟踪图片') && await page.getByRole('link', { name: '安全链接', exact: true }).getAttribute('href') === 'https://example.com', '保留图片说明及普通 HTTPS 链接')
  const xss = await page.evaluate(async () => {
    const { renderMarkdown } = await import('/web/src/markdown.js')
    const samples = [
      '[x](javascript:alert(1))', '[x](JaVaScRiPt:alert(1))', '[x](java&#x73;cript:alert(1))', '[x](vbscript:msgbox(1))',
      '[x](data:text/html;base64,PHNjcmlwdD4=)', '[x](file:///c:/test)', '[x](//evil.example)', '[x](\\\\evil.example)',
      '<svg onload="window.__xss=1"><script>alert(1)</script></svg>', '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
      '```\" onclick=\"alert(1)\n</code><img src=x onerror=alert(1)>\n```',
      '![x](data:image/svg+xml;base64,PHN2Zz4=)', '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">',
    ]
    return samples.map((source) => {
      const root = document.createElement('div'); root.innerHTML = renderMarkdown(source)
      return { safe: !root.querySelector('script, svg, math, iframe, [onclick], [onerror], [onload]') && ![...root.querySelectorAll('[href], [src]')].some((el) => /^(javascript|vbscript|data|file):|^\/\//i.test(el.getAttribute('href') || el.getAttribute('src'))), source }
    })
  })
  check(xss.every((sample) => sample.safe), '13 组 XSS/协议/语言标识攻击样本')
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.getByRole('button', { name: '复制代码', exact: true }).first().click()
  await page.getByRole('button', { name: '代码已复制' }).waitFor()
  check((await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n') === code, '代码复制保留特殊字符与换行')
  await page.evaluate(() => {
    window.__clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
  })
  await page.getByRole('button', { name: '复制代码', exact: true }).first().click()
  await page.locator('.markdown-code-block').nth(1).getByRole('button', { name: '代码已复制' }).waitFor()
  await page.evaluate(() => { delete navigator.clipboard })
  check((await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n') === '<untrusted> & raw_text\n', '无 Clipboard API 的复制回退')
  const header = page.locator('.execution-header').first()
  await header.click()
  check(await header.getAttribute('aria-expanded') === 'true' && await page.locator('.execution-content').first().isVisible(), '执行过程展开')
  await page.locator('.execution-step summary').first().click()
  check((await page.locator('.execution-step pre').first().innerText()).includes('frontend/src/style.css'), '工具参数仍以等宽纯文本展示')
  await header.click()
  check(await header.getAttribute('aria-expanded') === 'false', '执行过程折叠')
  await showSidebar('Sessions')
  await page.getByRole('button', { name: '历史会话 idle' }).click()
  await page.getByRole('heading', { name: '历史记录' }).waitFor()
  check(await page.locator('.message-content').count() === 1 && (await page.locator('.user-message-text').innerText()).includes('<script>plain</script>'), 'Session 切换与历史隔离')
  check(await page.evaluate(() => window.__uiTest.sources.filter((item) => !item.closed).length) === 1, '切换后旧 SSE 已关闭')
  await page.getByRole('button', { name: 'UI 重构演示 idle' }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  await showSidebar('Workspace')
  check(await page.locator('.preview, .workspace-splitter').count() === 0 && await page.locator('.tree-pane').evaluate((el) => Math.abs(el.getBoundingClientRect().height - el.parentElement.getBoundingClientRect().height) < 1), '未选文件时无预览及分割条，文件树占满高度')
  check(await page.locator('.file-tree > li > details').first().evaluate((el) => !el.open), '目录默认折叠')
  check(await page.locator('.link-entry').count() === 1 && (await page.locator('.link-entry').innerText()).includes('符号链接') && await page.locator('.link-entry button, .link-entry a').count() === 0, '符号链接用链接标识且不提供预览入口')
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  check(await page.locator('.preview pre').innerText() === '# workspace preview', 'Workspace 文件预览')
  await page.locator('.file-tree > li > details > summary').first().click()
  await page.evaluate(() => {
    window.__retainedPreview = document.querySelector('.preview')
    window.__retainedDirectory = document.querySelector('.file-tree > li > details')
    window.__retainedMessage = document.querySelector('.chat-message-assistant')
  })
  const switchRequestCount = requests.length
  await showSidebar('Sessions')
  await showSidebar('Workspace')
  check(await page.evaluate(() => window.__retainedPreview === document.querySelector('.preview') && window.__retainedDirectory === document.querySelector('.file-tree > li > details') && window.__retainedDirectory.open && window.__retainedMessage === document.querySelector('.chat-message-assistant')), 'Sidebar 切换保留文件树展开状态与展示 DOM')
  check(await page.locator('.file-button.selected').count() === 1 && await page.locator('.preview pre').innerText() === '# workspace preview' && requests.length === switchRequestCount, 'Sidebar 切换保留已选文件、预览且不请求业务 API')
  await page.locator('.workspace-panel').getByRole('button', { name: '收起 Sidebar', exact: true }).click()
  check(!await page.locator('.sidebar').isVisible() && await activity('Workspace').evaluate((el) => el === document.activeElement), 'Sidebar 标题按钮收起后焦点返回活动栏')
  await activity('Workspace').click()
  check(await page.locator('.file-tree > li > details').first().evaluate((el) => el.open) && await page.locator('.preview pre').innerText() === '# workspace preview' && requests.length === switchRequestCount, 'Sidebar 折叠恢复保留展开目录与预览且不重复请求')
  await Promise.all([page.waitForResponse((response) => response.url().includes('/files/tree')), page.getByTitle('刷新 Workspace', { exact: true }).click()])
  check(await page.locator('.file-tree > li > details').first().evaluate((el) => el.open), '刷新文件树保留仍存在的目录展开状态')
  await page.getByRole('button', { name: '关闭文件预览', exact: true }).click()
  check(await page.locator('.preview, .workspace-splitter, .file-button.selected').count() === 0, '关闭预览清除选择并移除分割条')
  const holdFile = async (path, options = {}, sessionId = 'ui-alpha') => {
    const held = { content: 'STALE_FILE_CONTENT', ...options }
    heldContents.set(sessionId + ':' + path, held)
    await page.locator('.file-button').filter({ hasText: path.split('/').pop() }).click()
    await waitForFixture(() => held.release)
    return async () => {
      const response = page.waitForResponse((response) => response.url().includes('/files/content') && new URL(response.url()).searchParams.get('path') === path)
      held.release()
      await response
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    }
  }
  const releaseClosed = await holdFile('README.md')
  await page.getByRole('button', { name: '关闭文件预览', exact: true }).click()
  await releaseClosed()
  check(await page.locator('.preview, .file-button.selected').count() === 0, '关闭预览后延迟响应不能重新打开预览')
  const releaseOldFile = await holdFile('README.md', { status: 404 })
  await page.locator('.file-button').filter({ hasText: 'index.js' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  await releaseOldFile()
  check(await page.locator('.preview-title > span').textContent() === 'src/index.js', '快速切换文件后旧 404 不清除新预览')
  const releaseSuccess = await holdFile('README.md')
  await page.locator('.file-button').filter({ hasText: 'index.js' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  await releaseSuccess()
  check(await page.locator('.preview pre').textContent() === '# workspace preview', '快速切换文件后旧内容不覆盖新预览')
  const releaseSessionFile = await holdFile('README.md')
  await showSidebar('Sessions')
  await page.getByRole('button', { name: '历史会话 idle' }).click()
  await page.getByRole('heading', { name: '历史记录' }).waitFor()
  await releaseSessionFile()
  await showSidebar('Workspace')
  check(await page.locator('.preview').count() === 0 && await page.locator('.file-button').count() === 1 && await page.locator('.file-button').textContent() === 'beta.txt10 B', 'Session 切换隔离旧预览响应与 Workspace 数据')
  await showSidebar('Sessions')
  await page.getByRole('button', { name: 'UI 重构演示 idle' }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  await showSidebar('Workspace')
  check(await page.locator('.file-tree details').first().evaluate((el) => !el.open), '切换 Session 清理目录展开状态')
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  check(true, '关闭后重新打开文件正常')
  const emit = (type, data) => page.evaluate(({ type, data }) => window.__uiTest.emit(type, data), { type, data })
  const contentRequestsBeforeChange = requests.filter((item) => item.path.endsWith('/files/content')).length
  await Promise.all([page.waitForResponse((response) => response.url().includes('/files/content')), emit('workspace_changed', {})])
  check(requests.filter((item) => item.path.endsWith('/files/content')).length === contentRequestsBeforeChange + 1 && await page.locator('.preview-title > span').textContent() === 'README.md', '真实 Workspace 变动刷新当前预览')
  const releaseMissing = await holdFile('README.md', { status: 404 })
  await releaseMissing()
  check(await page.locator('.preview, .workspace-splitter, .file-button.selected').count() === 0, '当前文件不存在时清除失效预览')
  await page.locator('.file-tree > li > details > summary').first().click()
  await page.locator('.file-button').filter({ hasText: 'index.js' }).click()
  await page.waitForFunction(() => document.querySelector('.preview-title > span')?.textContent === 'src/index.js')
  const directory = fileEntries.find((entry) => entry.path === 'src')
  await page.locator('.file-tree > li > details > summary').first().getByTitle('删除目录', { exact: true }).click()
  const deleteDirectoryDialog = page.getByRole('dialog', { name: '删除目录？', exact: true })
  check((await deleteDirectoryDialog.textContent()).includes('递归删除'), '目录删除保留递归删除确认')
  await deleteDirectoryDialog.getByRole('button', { name: '删除', exact: true }).click()
  await page.locator('.file-tree details').waitFor({ state: 'detached' })
  check(await page.locator('.preview, .file-button.selected').count() === 0, '删除目录时清除其中所选文件的预览')
  fileEntries.push(directory)
  await Promise.all([page.waitForResponse((response) => response.url().includes('/files/tree')), page.getByTitle('刷新 Workspace', { exact: true }).click()])
  await page.locator('.file-tree details').waitFor()
  check(await page.locator('.file-tree details').evaluate((el) => !el.open), '删除再创建同路径目录时已清理展开状态')
  await page.mouse.move(890, 10)
  check(await page.locator('.file-row .tree-delete').first().evaluate((el) => getComputedStyle(el).opacity === '0'), '鼠标未悬停时删除按钮弱化隐藏')
  await page.locator('.file-row .tree-delete').first().focus()
  check(await page.locator('.file-row .tree-delete').first().evaluate((el) => getComputedStyle(el).opacity === '1'), '删除按钮仍可通过键盘聚焦发现并操作')
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  const input = page.locator('.composer textarea')
  const sendButton = page.getByRole('button', { name: '发送消息', exact: true })
  const messageRequests = () => requests.filter((item) => item.path.endsWith('/message')).length
  const height = () => input.evaluate((el) => el.getBoundingClientRect().height)
  const settle = () => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const idle = () => emit('runtime_status', { status: 'idle', turn_id: 'stream-turn' })
  composerHeights.initial = await height()
  await input.fill('第一行')
  await input.press('End')
  await input.press('Enter')
  check(await input.inputValue() === '第一行\n' && messageRequests() === 0, 'Enter 只换行、不发送')
  await input.fill(' \n  \n ')
  await input.press('Control+Enter')
  check(await sendButton.isDisabled() && messageRequests() === 0, '空格与换行不能发送')
  await input.fill('输入增长\n'.repeat(4))
  await settle()
  composerHeights.growing = await height()
  await input.fill('多行输入\n'.repeat(20))
  await settle()
  composerHeights.maximum = await height()
  check(composerHeights.growing > composerHeights.initial && composerHeights.maximum <= 176 && await input.evaluate((el) => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === 'auto'), '自动增高并在八行上限后内部滚动')
  await input.fill('短内容')
  await settle()
  check(await height() === composerHeights.initial, '删除文本自动缩小')
  await page.evaluate(() => navigator.clipboard.writeText('粘贴多行\n'.repeat(12)))
  await input.press('Control+A')
  await input.press('Control+V')
  await settle()
  check((await input.inputValue()).replace(/\r\n/g, '\n') === '粘贴多行\n'.repeat(12) && await height() === composerHeights.maximum, '真实剪贴板粘贴多行后调整高度')
  await input.fill('中文输入中')
  await input.evaluate((el) => {
    el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '中文' }))
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', ctrlKey: true, isComposing: true }))
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', ctrlKey: true, isComposing: false }))
  })
  check(messageRequests() === 0 && await input.inputValue() === '中文输入中' && await sendButton.isDisabled(), 'IME 组合状态和 isComposing 阻止快捷键发送或清空')
  await input.evaluate((el) => {
    el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '中文' }))
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', ctrlKey: true, keyCode: 229 }))
  })
  check(messageRequests() === 0 && await input.inputValue() === '中文输入中', 'IME keyCode 229 兼容路径不会误发送')
  await input.evaluate((el) => el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', ctrlKey: true, repeat: true })))
  check(messageRequests() === 0, '按住快捷键产生的 repeat 不发送')
  await sendButton.click()
  await page.waitForFunction(() => document.querySelector('.composer textarea').value === '')
  check(messageRequests() === 1 && await height() === composerHeights.initial, '点击发送一次，成功清空并恢复初始高度')
  await idle()
  failMessage = true
  const failedDraft = '  失败保留原始输入\n含空格  '
  await input.fill(failedDraft)
  await input.press('Control+Enter')
  await page.waitForFunction(() => document.querySelector('.error-banner')?.textContent === '消息发送失败测试')
  check(await input.inputValue() === failedDraft && messageRequests() === 2, 'Ctrl+Enter 失败时保留原始内容与空白')
  holdMessage = true
  await input.fill('等待发送响应')
  await input.press('Control+Enter')
  await page.waitForFunction(() => document.querySelector('.composer button').disabled)
  await input.press('Control+Enter')
  await input.evaluate((el) => el.closest('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  check(messageRequests() === 3 && await sendButton.isDisabled() && await input.evaluate((el) => el.readOnly), '发送中快捷键与表单提交不能重复请求，输入暂时只读避免成功响应覆盖新组合文本')
  holdMessage = false
  releaseMessage()
  await page.waitForFunction(() => document.querySelector('.composer textarea').value === '')
  await input.fill('运行期间新草稿')
  await input.press('Control+Enter')
  check(messageRequests() === 3 && await sendButton.isDisabled() && await input.inputValue() === '运行期间新草稿' && await input.evaluate((el) => !el.readOnly), 'Runtime 运行中禁用发送且保留可编辑草稿')
  await idle()
  await showSidebar('Sessions')
  await page.getByRole('button', { name: '历史会话 idle' }).click()
  await page.getByRole('heading', { name: '历史记录' }).waitFor()
  check(await input.inputValue() === '运行期间新草稿', 'Session 间继续共享现有草稿')
  await page.getByRole('button', { name: 'UI 重构演示 idle' }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  await showSidebar('Workspace')
  await page.locator('.composer textarea').fill('测试流式更新')
  await page.locator('.composer textarea').press('Control+Enter')
  await page.waitForFunction(() => document.querySelector('.composer button').disabled)
  check(requests.some((request) => request.path === '/sessions/ui-alpha/message' && JSON.parse(request.body).content === '测试流式更新'), '发送 API 与 Ctrl+Enter')
  await emit('console_event', { id: 4, kind: 'user', content: '测试流式更新', data: { turn_id: 'stream-turn' } })
  const chunks = ['## 流式回答\n\n内容开始。\n\n```python\nprint(', '## 流式回答\n\n内容开始。\n\n```python\nprint("完整更新")\n', '## 流式回答\n\n内容开始。\n\n```python\nprint("完整更新")\n```\n\n内容结束。']
  for (const content of chunks) {
    await emit('console_live', { active: true, turn_id: 'stream-turn', kind: 'assistant', content })
    await page.waitForFunction(() => document.querySelector('[aria-busy="true"] .message-content'))
    const expectedCode = content.split('```python\n')[1].split('```')[0]
    await page.waitForFunction((expected) => document.querySelector('[aria-busy="true"] .message-content pre code')?.textContent === expected, expectedCode)
    check(await page.locator('[aria-busy="true"] .message-content pre code').textContent() === expectedCode, '流式未闭合/闭合代码块更新')
  }
  await emit('console_event', { id: 5, kind: 'assistant', content: chunks[2], data: { turn_id: 'stream-turn' } })
  await emit('runtime_status', { status: 'idle', turn_id: 'stream-turn' })
  await page.waitForFunction(() => document.querySelectorAll('.chat-message-assistant').length === 2 && !document.querySelector('[aria-busy="true"]'))
  check(await page.getByRole('heading', { name: '流式回答' }).count() === 1, '流式转正式回答无重复')
  await emit('console_event', { id: 5, kind: 'assistant', content: chunks[2] + '\n\n更新补充。', data: { turn_id: 'stream-turn' } })
  check(await page.locator('.chat-message-assistant').count() === 2, '同 ID 消息更新无重复')
  const history = page.locator('.console-history')
  const jump = page.getByRole('button', { name: '回到底部，继续跟随 Agent 输出', exact: true })
  const position = async (label) => {
    const sample = await history.evaluate((el) => ({ top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight, gap: el.scrollHeight - el.clientHeight - el.scrollTop }))
    scrollSamples.push({ label, ...sample }); return sample
  }
  const bottom = () => page.waitForFunction(() => { const el = document.querySelector('.console-history'); return el.scrollHeight - el.clientHeight - el.scrollTop <= 2 })
  const wheel = async (delta) => {
    const box = await history.boundingBox()
    await page.mouse.move(box.x + 30, box.y + 30)
    await page.mouse.wheel(0, delta)
    await page.waitForTimeout(180)
  }
  await bottom()
  const beforeStream = await position('流式增长前')
  const observersBefore = await page.evaluate(() => window.__uiTest.scrollObservers.length)
  await page.locator('.error-banner').waitFor({ state: 'hidden', timeout: 12000 })
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  await page.locator('.file-tree > li > details').first().evaluate((el) => { el.open = false })
  await emit('console_event', { id: 1001, kind: 'user', content: '验证智能滚动：我会向上阅读历史，请保留阅读位置。', data: { turn_id: 'scroll-turn' } })
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: '## 智能滚动检查\n\n开始输出。' })
  await bottom()
  const longScrollText = '## 智能滚动检查\n\n' + Array.from({ length: 180 }, (_, i) => `### 步骤 ${i + 1}\n\n` + '持续更新 **实现细节**，读取 `frontend/src/SessionApp.vue` 并检查聊天阅读位置。 '.repeat(3) + '\n\n').join('')
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + 'SCROLL_DELAYED_END' })
  await page.waitForFunction(() => document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes('SCROLL_DELAYED_END'))
  await bottom()
  const afterStream = await position('120ms Markdown 合并渲染后')
  check(afterStream.top > beforeStream.top + 1000 && afterStream.gap <= 2, '底部跟随流式 Markdown 实际延迟 DOM 增高')
  await wheel(-650)
  await jump.waitFor()
  const paused = await position('用户向上滚动暂停')
  check(paused.gap > 400 && paused.top < afterStream.top - 400, '用户真实向上滚动后暂停并显示入口')
  for (let i = 1; i <= 5; i += 1) {
    await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + '\n\n追加内容\n\n'.repeat(i * 4) + `PAUSED_${i}` })
    await page.waitForFunction((marker) => document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes(marker), `PAUSED_${i}`)
  }
  const kept = await position('暂停期间五轮流式更新后')
  check(Math.abs(kept.top - paused.top) <= 2 && kept.height > paused.height, '持续输出增高时实际阅读位置保持')
  check(await page.evaluate((count) => window.__uiTest.scrollObservers.length === count && window.__uiTest.scrollObservers.filter((observer) => observer.__active).length === 1, observersBefore), '流式更新不重复创建聊天 ResizeObserver')
  await jump.focus()
  await page.keyboard.press('Shift')
  check(await jump.evaluate((el) => getComputedStyle(el).outlineStyle === 'solid'), '回到底部按钮有键盘焦点样式')
  await jump.press('Enter')
  await bottom()
  check(!await jump.isVisible() && (await position('按钮恢复')).top > kept.top, '按钮 Enter 立即定位最新消息并恢复跟随')
  await wheel(-550)
  const manualStart = await position('手动回底前')
  await wheel(2000)
  await bottom()
  check(!await jump.isVisible() && (await position('手动回底后')).top > manualStart.top, '用户真实向下滚回底部恢复跟随')
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + '\n\n新输出\n\n'.repeat(40) + 'MANUAL_RESUMED' })
  await page.waitForFunction(() => document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes('MANUAL_RESUMED'))
  await bottom()
  check((await position('手动恢复后新输出')).gap <= 2, '恢复后新输出继续实际跟随')
  await wheel(-30)
  await jump.waitFor()
  await page.getByTitle('折叠 Terminal', { exact: true }).click()
  await settle()
  const clamped = await position('主动暂停后布局将位置夹到底部')
  check(clamped.gap <= 2 && !await jump.isVisible(), '布局夹到底部时隐藏无剩余内容的按钮')
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + '\n\n新输出\n\n'.repeat(80) + 'CLAMP_NOT_RESUMED' })
  await page.waitForFunction(() => document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes('CLAMP_NOT_RESUMED'))
  await settle()
  check(Math.abs((await position('布局夹底后继续输出')).top - clamped.top) <= 2 && await jump.isVisible(), '暂停后布局夹底不会恢复跟随，随后新内容不拉动阅读位置')
  await page.getByTitle('展开 Terminal', { exact: true }).click()
  await jump.click()
  await bottom()
  await history.focus()
  await history.press('PageUp')
  await jump.waitFor()
  await page.waitForFunction(() => { const el = document.querySelector('.console-history'); return el.scrollHeight - el.clientHeight - el.scrollTop > 100 })
  await page.waitForTimeout(250)
  await history.press('End')
  await bottom()
  await jump.waitFor({ state: 'hidden' })
  await page.waitForTimeout(250)
  check(!await jump.isVisible(), 'PageUp 暂停后 End 键回底恢复跟随')
  // Keyboard scrolling is also a user gesture; programmatic scrolling alone is not.
  await history.focus()
  await history.press('Home')
  await page.waitForFunction(() => document.querySelector('.console-history').scrollTop === 0)
  await jump.waitFor()
  await page.waitForTimeout(450)
  const layoutReading = await position('顶部键盘阅读')
  const readingCopy = page.getByRole('button', { name: '复制代码', exact: true }).first()
  await readingCopy.scrollIntoViewIfNeeded()
  const beforeCopy = await position('阅读中复制前')
  await readingCopy.click()
  await page.getByRole('button', { name: '代码已复制', exact: true }).waitFor()
  await settle()
  check(Math.abs((await position('阅读中复制后')).top - beforeCopy.top) <= 2 && await jump.isVisible(), '代码复制反馈不改变阅读位置或恢复跟随')
  await history.focus()
  await history.press('Home')
  await page.waitForFunction(() => document.querySelector('.console-history').scrollTop === 0)
  await page.locator('.execution-header').first().click()
  await settle()
  check((await position('执行过程展开')).top === layoutReading.top && await jump.isVisible(), '执行过程展开不抢夺阅读位置或恢复跟随')
  await page.locator('.execution-header').first().click()
  await page.locator('.agent-heading-actions button').filter({ hasText: 'Context' }).click()
  await page.locator('.context-grid').waitFor()
  await settle()
  check((await position('Context 展开')).top === layoutReading.top && await jump.isVisible(), 'Context 展开不抢夺阅读位置或恢复跟随')
  await page.getByTitle('折叠 Terminal', { exact: true }).click()
  await settle()
  check((await position('Terminal 收起')).top === layoutReading.top && await jump.isVisible(), 'Terminal 收起不抢夺阅读位置或恢复跟随')
  await page.getByTitle('展开 Terminal', { exact: true }).click()
  const readingTerminalSplit = await page.locator('.terminal-splitter').boundingBox()
  await page.mouse.move(readingTerminalSplit.x + 100, readingTerminalSplit.y + readingTerminalSplit.height / 2)
  await page.mouse.down()
  await page.mouse.move(readingTerminalSplit.x + 100, readingTerminalSplit.y + readingTerminalSplit.height / 2 - 30)
  await page.mouse.up()
  await settle()
  check((await position('阅读时拖动 Terminal')).top === layoutReading.top && await jump.isVisible(), 'Terminal 拖动不抢夺阅读位置或恢复跟随')
  const restoreTerminalSplit = await page.locator('.terminal-splitter').boundingBox()
  await page.mouse.move(restoreTerminalSplit.x + 100, restoreTerminalSplit.y + restoreTerminalSplit.height / 2)
  await page.mouse.down()
  await page.mouse.move(restoreTerminalSplit.x + 100, restoreTerminalSplit.y + restoreTerminalSplit.height / 2 + 30)
  await page.mouse.up()
  await activity('Workspace').click()
  await settle()
  check((await position('Sidebar 收起')).top === layoutReading.top && await jump.isVisible(), 'Sidebar 收起及文本重排不抢夺阅读位置')
  await activity('Workspace').click()
  const splitter = await page.locator('.sidebar-splitter').boundingBox()
  await page.mouse.move(splitter.x + splitter.width / 2, splitter.y + 80)
  await page.mouse.down(); await page.mouse.move(splitter.x + 55, splitter.y + 80); await page.mouse.up()
  await settle()
  check((await position('Sidebar 拖动')).top === layoutReading.top && await jump.isVisible(), 'Sidebar 拖动不意外恢复跟随')
  await page.setViewportSize({ width: 900, height: 1000 })
  await input.fill('窄面板长草稿\n'.repeat(20))
  await settle()
  await page.locator('.agent-heading-actions button').filter({ hasText: 'Context' }).click()
  await page.locator('.context-grid').waitFor()
  check(await page.locator('.composer').evaluate((el) => el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().bottom <= document.querySelector('.agent-panel').getBoundingClientRect().bottom) && await height() <= composerHeights.maximum, '900px 输入上限、发送栏无溢出或重叠')
  check(await jump.evaluate((el) => { const rect = el.getBoundingClientRect(), viewport = document.querySelector('.console-viewport').getBoundingClientRect(), composer = document.querySelector('.composer').getBoundingClientRect(); return rect.top >= viewport.top && rect.bottom <= viewport.bottom && rect.bottom < composer.top }), '长草稿及 Context 展开时回底按钮仍在聊天区内')
  check((await position('900px 重排及 Composer 增高')).top === layoutReading.top && await jump.isVisible(), '浏览器尺寸与 Composer 增高不抢夺阅读位置')
  await page.screenshot({ path: 'output/playwright/mycode-composer-narrow.png', animations: 'disabled' })
  await page.setViewportSize({ width: 900, height: 720 })
  await settle()
  await page.locator('.context-grid').waitFor()
  check(await page.locator('.context-panel').evaluate((el) => {
    const rect = el.getBoundingClientRect(), composer = document.querySelector('.composer').getBoundingClientRect()
    return rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= composer.top && el.scrollHeight > el.clientHeight
  }), '900×720 Context 位于视口和 Composer 上方，长指标独立滚动')
  await page.screenshot({ path: 'output/playwright/mycode-context-900x720.png', animations: 'disabled' })
  check(await page.locator('.composer').evaluate((el) => el.getBoundingClientRect().bottom <= document.querySelector('.agent-panel').getBoundingClientRect().bottom + 1) && await jump.evaluate((el) => { const rect = el.getBoundingClientRect(), viewport = document.querySelector('.console-viewport').getBoundingClientRect(); return rect.top >= viewport.top && rect.bottom <= viewport.bottom }), '900×720 Context 与长草稿同时存在时仍保留聊天和回底入口')
  await input.fill('')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await emit('runtime_status', { status: 'running', turn_id: 'scroll-turn' })
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + '\n\n仍在输出\n\n'.repeat(50) + 'STILL_OUTPUTTING' })
  await page.waitForFunction(() => document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes('STILL_OUTPUTTING'))
  await settle()
  await page.screenshot({ path: 'output/playwright/mycode-composer-reading.png', animations: 'disabled' })
  // Even a programmatic clamp to the bottom must not resume a paused reader.
  await history.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await settle()
  const programBottom = await position('程序定位底部仍保持暂停')
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + '\n\n仍在输出\n\n'.repeat(80) + 'PROGRAM_NOT_RESUMED' })
  await page.waitForFunction(() => document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes('PROGRAM_NOT_RESUMED'))
  await settle()
  check(Math.abs((await position('程序定位后新增输出')).top - programBottom.top) <= 2 && await jump.isVisible(), '程序 scroll 事件不会错误恢复跟随')
  await jump.click()
  await bottom()
  await emit('runtime_status', { status: 'idle', turn_id: 'scroll-turn' })
  // Keep an old delayed Markdown update and old EventSource in flight at switch.
  await wheel(-500)
  await page.evaluate(() => { window.__oldScrollSource = window.__uiTest.sources.findLast((item) => !item.closed) })
  await emit('console_live', { active: true, turn_id: 'scroll-turn', kind: 'assistant', content: longScrollText + 'OLD_PENDING_RENDER' })
  await showSidebar('Sessions')
  await page.getByRole('button', { name: '历史会话 idle' }).click()
  await page.evaluate(() => window.__oldScrollSource.dispatchEvent(new MessageEvent('console_live', { data: JSON.stringify({ active: true, turn_id: 'old', kind: 'assistant', content: 'OLD_SSE_MUST_NOT_APPEAR' }) })))
  await page.getByRole('heading', { name: '历史记录' }).waitFor()
  await bottom()
  await page.waitForTimeout(200)
  check(!await jump.isVisible() && !(await history.textContent()).includes('OLD_') && (await position('Session 切换完成')).gap <= 2, 'Session 切换重置跟随，旧 SSE/RAF/Markdown 任务不污染新历史')
  await page.getByRole('button', { name: 'UI 重构演示 idle' }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  await bottom()
  await showSidebar('Workspace')
  // Restore this scenario's dragged width before the original persistence checks.
  const restoreSplitter = await page.locator('.sidebar-splitter').boundingBox()
  const restoredSidebar = await page.locator('.sidebar').evaluate((el) => el.getBoundingClientRect().width)
  await page.mouse.move(restoreSplitter.x + restoreSplitter.width / 2, restoreSplitter.y + 80)
  await page.mouse.down()
  await page.mouse.move(restoreSplitter.x + restoreSplitter.width / 2 + 320 - restoredSidebar, restoreSplitter.y + 80)
  await page.mouse.up()
  await input.fill('继续检查实现细节，并运行前端构建。')
  await page.screenshot({ path: 'output/playwright/mycode-composer-normal.png', animations: 'disabled' })
  await page.locator('.agent-heading-actions button').filter({ hasText: 'Context' }).click()
  await page.waitForFunction(() => document.querySelector('.context-grid'))
  check((await page.locator('.context-panel-heading').innerText()).includes('10%'), 'Context 查询可用')
  const contextButton = page.locator('.agent-heading-actions button').first()
  const chatBounds = () => page.locator('.console-viewport').evaluate((el) => ({ y: el.getBoundingClientRect().y, height: el.getBoundingClientRect().height }))
  const contextOpenBounds = await chatBounds()
  check(await contextButton.getAttribute('aria-expanded') === 'true' && await page.getByRole('button', { name: '关闭 Context', exact: true }).evaluate((el) => el === document.activeElement), 'Context 打开具有 ARIA 状态并聚焦关闭按钮')
  await page.screenshot({ path: 'output/playwright/mycode-context-1440.png', animations: 'disabled' })
  await page.keyboard.press('Escape')
  check(await page.locator('.context-panel').count() === 0 && await contextButton.evaluate((el) => el === document.activeElement) && JSON.stringify(await chatBounds()) === JSON.stringify(contextOpenBounds), 'Esc 关闭 Context 恢复焦点且不改变聊天区几何')
  await emit('context_status', { ...contextFixture, estimated_input_tokens: 2000 })
  await emit('compact_result', { status: 'compacted', after: contextFixture })
  check(await page.locator('.context-panel').count() === 0, '关闭后 Context 与 Compact 状态事件仅更新数据，不重新弹出')
  await contextButton.click()
  await page.locator('.context-grid').waitFor()
  check(JSON.stringify(await chatBounds()) === JSON.stringify(contextOpenBounds), '重新打开 Context 不挤压聊天主布局')
  await page.getByRole('button', { name: '关闭 Context', exact: true }).click()
  await contextButton.click()
  await page.locator('.context-grid').waitFor()
  await input.click()
  check(await page.locator('.context-panel').count() === 0 && await input.evaluate((el) => el === document.activeElement), '点击外部关闭 Context，不抢夺 Composer 焦点')
  holdContext = true
  await contextButton.click()
  await waitForFixture(() => releaseContext)
  await page.getByRole('button', { name: '关闭 Context', exact: true }).click()
  holdContext = false
  releaseContext()
  await page.waitForFunction(() => !document.querySelector('.agent-heading-actions button').disabled)
  check(await page.locator('.context-panel').count() === 0, '查询进行中关闭 Context，迟到的 API 响应不重新打开')
  failContext = true
  await contextButton.click()
  await page.locator('.context-error').waitFor()
  check(await page.locator('.context-error').textContent() === 'Context 查询失败测试', 'Context API 错误状态保留')
  await page.getByRole('button', { name: '关闭 Context', exact: true }).click()
  await page.locator('.agent-heading-actions button').filter({ hasText: 'Compact' }).click()
  await page.waitForFunction(() => document.querySelector('.context-feedback')?.textContent.includes('Compact 完成'))
  check(true, 'Compact 操作可用')
  await page.getByRole('button', { name: '关闭 Context', exact: true }).click()
  check(await page.locator('.context-panel').count() === 0 && requests.some((item) => item.path.endsWith('/compact') && item.method === 'POST'), '独立 Compact 按钮主动展示真实 API 结果且可以关闭')
  await showSidebar('Sessions')
  await page.getByRole('button', { name: '历史会话 idle' }).click()
  await page.getByRole('heading', { name: '历史记录' }).waitFor()
  await page.getByRole('button', { name: 'UI 重构演示 idle' }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  // Ordinary replies must throttle too. Counters are opt-in dev diagnostics;
  // reset after mounting the live boundary to isolate text-only updates.
  await emit('runtime_status', { status: 'running', turn_id: 'short-performance-turn' })
  await emit('console_event', { id: 38, kind: 'user', content: '普通流式性能测试', data: { turn_id: 'short-performance-turn' } })
  await emit('console_live', { active: true, kind: 'assistant', turn_id: 'short-performance-turn', content: 'P1_SHORT_START' })
  await page.getByText('P1_SHORT_START', { exact: true }).waitFor()
  shortStreamingPerformance = await page.evaluate(async () => {
    window.__MYCODE_STREAM_DEBUG__ = {}
    let content = ''
    for (let revision = 1; revision <= 40; revision += 1) {
      content = `普通回复 **完整快照** P1_SHORT_REV_${revision}`
      window.__uiTest.emit('console_live', { active: true, kind: 'assistant', turn_id: 'short-performance-turn', content })
      await new Promise((resolve) => setTimeout(resolve, 5))
    }
    await new Promise((resolve) => setTimeout(resolve, 80))
    const stats = structuredClone(window.__MYCODE_STREAM_DEBUG__)
    const latestVisible = document.querySelector('[aria-busy="true"] .message-content')?.textContent.includes('P1_SHORT_REV_40')
    // Ensure there is pending work when the durable final answer replaces live.
    window.__uiTest.emit('console_live', { active: true, kind: 'assistant', turn_id: 'short-performance-turn', content: content + ' pending' })
    await Promise.resolve()
    const final = content + ' FINAL_TAIL'
    window.__uiTest.emit('console_event', { id: 39, kind: 'assistant', content: final, data: { turn_id: 'short-performance-turn' } })
    window.__uiTest.emit('runtime_status', { status: 'idle', turn_id: 'short-performance-turn' })
    await Promise.resolve()
    const finalImmediate = [...document.querySelectorAll('.message-content')].some((el) => el.textContent.includes('P1_SHORT_REV_40 FINAL_TAIL'))
    await new Promise((resolve) => setTimeout(resolve, 100))
    const noLateUpdate = !document.querySelector('[aria-busy="true"]') && ![...document.querySelectorAll('.message-content')].some((el) => el.textContent.includes('P1_SHORT_REV_40 pending'))
    return { ...stats, latestVisible, finalImmediate, noLateUpdate }
  })
  check(shortStreamingPerformance.sse.count === 40 && !shortStreamingPerformance.timeline?.count, 'P1：40 次普通 Live 文本更新不重新投影历史 Timeline')
  check(shortStreamingPerformance.markdown.count > 0 && shortStreamingPerformance.markdown.count <= 10 && shortStreamingPerformance.latestVisible, 'P1：普通回复受到节流且完整呈现最新快照')
  check(shortStreamingPerformance.finalImmediate && shortStreamingPerformance.noLateUpdate, 'P1：最终尾部在下次 Vue 更新立即显示，待渲染快照不迟到覆盖')

  // Measure real Vue DOM updates while snapshots arrive on a 20ms timer.
  // The final answer combines prose, lists and a single ~700-line JS block.
  await emit('runtime_status', { status: 'running', turn_id: 'performance-turn' })
  await emit('console_event', { id: 40, kind: 'user', content: '长文本流式性能测试', data: { turn_id: 'performance-turn' } })
  await showSidebar('Workspace')
  const performanceRun = page.evaluate(async () => {
    const { renderMarkdown } = await import('/web/src/markdown.js')
    window.__MYCODE_STREAM_DEBUG__ = {}
    const latencies = [], timerLags = [], longTasks = [], seen = new Set(), sentAt = new Map()
    const longTaskObserver = new PerformanceObserver((list) => longTasks.push(...list.getEntries().map((entry) => entry.duration)))
    longTaskObserver.observe({ type: 'longtask', buffered: false })
    window.__markdownPerformance = { emitted: 0 }
    let content = '## 大文本流式测试\n\n', revision = 0, codeLines = 0, lastSnapshot = '', expectedTick = performance.now() + 20
    const started = performance.now()
    let resolveFinished
    const finished = new Promise((resolve) => { resolveFinished = resolve })
    const observer = new MutationObserver(() => {
      const element = document.querySelector('[aria-busy="true"] .message-content')
      const match = /PERF_REV_(\d+)/.exec(element?.textContent || '')
      if (!match) return
      const current = Number(match[1])
      if (seen.has(current)) return
      seen.add(current)
      requestAnimationFrame(() => {
        latencies.push(performance.now() - sentAt.get(current))
        if (current === 120) resolveFinished()
      })
    })
    observer.observe(document.querySelector('.console-history'), { subtree: true, childList: true, characterData: true })
    const timer = setInterval(() => {
      const now = performance.now()
      timerLags.push(Math.max(0, now - expectedTick)); expectedTick = now + 20
      revision += 1
      if (revision <= 60) {
        content += `### 区块 ${revision}\n\n` + '这是一段 Coding Agent 实现说明，包含 **重点**、`inline_code` 和读取上下文的步骤。 '.repeat(10) + '\n\n- 保留状态流\n- 验证可见内容\n\n'
      } else {
        if (revision === 61) content += '```javascript\n'
        for (let line = 0; line < 12; line += 1) {
          codeLines += 1
          content += `const item_${codeLines} = { path: "src/module_${codeLines}.js", status: "ready", value: ${codeLines} };\n`
        }
        if (revision === 120) content += '```\n\n流式内容完整结束。\n'
      }
      lastSnapshot = content + `\n\nPERF_REV_${revision}\n`
      sentAt.set(revision, performance.now())
      window.__uiTest.emit('console_live', { active: true, turn_id: 'performance-turn', kind: 'assistant', content: lastSnapshot })
      window.__markdownPerformance.emitted = revision
      window.__markdownPerformance.utf8Bytes = new TextEncoder().encode(lastSnapshot).length
      if (revision === 120) clearInterval(timer)
    }, 20)
    try {
      await Promise.race([finished, new Promise((_, reject) => setTimeout(() => reject(new Error('Long streaming render timed out')), 30000))])
      const live = document.querySelector('[aria-busy="true"] .message-content')
      const expected = document.createElement('div'); expected.innerHTML = renderMarkdown(lastSnapshot)
      const liveComplete = live.innerHTML === expected.innerHTML
      const streamStats = structuredClone(window.__MYCODE_STREAM_DEBUG__)
      window.__uiTest.emit('console_event', { id: 41, kind: 'assistant', content: lastSnapshot, data: { turn_id: 'performance-turn' } })
      window.__uiTest.emit('runtime_status', { status: 'idle', turn_id: 'performance-turn' })
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      const finalElement = [...document.querySelectorAll('.chat-message-assistant .message-content')].find((el) => el.textContent.includes('PERF_REV_120'))
      const sorted = (values) => [...values].sort((a, b) => a - b)
      const p95 = (values) => sorted(values)[Math.ceil(values.length * .95) - 1] || 0
      return {
        characters: lastSnapshot.length, utf8Bytes: new TextEncoder().encode(lastSnapshot).length,
        streamStats,
        updates: revision, codeLines, requestedIntervalMs: 20, renderedSnapshots: seen.size,
        durationMs: performance.now() - started,
        updateToFrameMeanMs: latencies.reduce((a, b) => a + b, 0) / latencies.length,
        updateToFrameP95Ms: p95(latencies), updateToFrameMaxMs: Math.max(...latencies),
        timerLagP95Ms: p95(timerLags), longTasks: longTasks.length, longestTaskMs: Math.max(0, ...longTasks),
        liveComplete, finalComplete: finalElement?.innerHTML === expected.innerHTML,
        finalCount: [...document.querySelectorAll('.message-content')].filter((el) => el.textContent.includes('PERF_REV_120')).length,
        stillStreaming: Boolean(document.querySelector('[aria-busy="true"]')),
      }
    } finally { clearInterval(timer); observer.disconnect(); longTaskObserver.disconnect() }
  })
  const interactionLatencies = []
  const interactionStages = []
  // Exercise the prose stage, then the long-code stage, then the final burst.
  for (const revision of [25, 75, 110]) {
    await page.waitForFunction((value) => window.__markdownPerformance?.emitted >= value, revision)
    interactionStages.push(await page.evaluate(() => ({ ...window.__markdownPerformance })))
    let started = Date.now()
    await page.locator('.composer textarea').fill(`响应测试 ${revision}`)
    interactionLatencies.push(Date.now() - started)
    started = Date.now()
    await activity('Workspace').click()
    interactionLatencies.push(Date.now() - started)
    await showSidebar('Workspace')
  }
  markdownPerformance = { ...await performanceRun, interactionStages, interactionLatenciesMs: interactionLatencies, maxInteractionMs: Math.max(...interactionLatencies) }
  check(markdownPerformance.utf8Bytes >= 100000 && markdownPerformance.updates === 120, '大文本流式规模达到 100KB、120 次更新')
  check(markdownPerformance.liveComplete && markdownPerformance.finalComplete && markdownPerformance.finalCount === 1 && !markdownPerformance.stillStreaming, '大文本流式和最终回答完整且不重复')
  check(markdownPerformance.streamStats.timeline.count === 1 && markdownPerformance.streamStats.markdown.count < 120, 'P1：大文本只在 Live 边界投影一次 Timeline，Markdown 渲染次数少于快照数')
  // Loose responsiveness guard for CI/desktop variance; report actual timings.
  if (markdownPerformance.maxInteractionMs >= 1000 || markdownPerformance.updateToFrameP95Ms >= 500) throw new Error('大文本响应超限: ' + JSON.stringify(markdownPerformance))
  check(true, '大文本期间输入与侧栏交互保持响应')
  // Completion and session switching may arrive before a coalesced render fires.
  await emit('runtime_status', { status: 'running', turn_id: 'pending-render-turn' })
  await emit('console_event', { id: 42, kind: 'user', content: '验证待渲染快照', data: { turn_id: 'pending-render-turn' } })
  await emit('console_live', { active: true, turn_id: 'pending-render-turn', kind: 'assistant', content: '## 渲染等待中\n\n短内容。' })
  await page.getByRole('heading', { name: '渲染等待中' }).waitFor()
  const pendingContent = '## 立即完成\n\n' + '内容 **保持完整**，包含 `code` 与路径。\n\n'.repeat(600) + 'END_PENDING_RENDER'
  await emit('console_live', { active: true, turn_id: 'pending-render-turn', kind: 'assistant', content: pendingContent })
  await emit('console_event', { id: 43, kind: 'assistant', content: pendingContent, data: { turn_id: 'pending-render-turn' } })
  await emit('runtime_status', { status: 'idle', turn_id: 'pending-render-turn' })
  await page.getByRole('heading', { name: '立即完成' }).waitFor()
  await page.waitForTimeout(180)
  check(await page.getByRole('heading', { name: '立即完成' }).count() === 1 && (await page.locator('.message-content').last().textContent()).includes('END_PENDING_RENDER') && await page.locator('[aria-busy="true"]').count() === 0, '待渲染时完成会立即显示完整正式回答并取消定时器')
  await emit('console_live', { active: true, turn_id: 'pending-render-turn', kind: 'assistant', content: '## 会话切换前\n\n短内容。' })
  await page.getByRole('heading', { name: '会话切换前' }).waitFor()
  await emit('console_live', { active: true, turn_id: 'pending-render-turn', kind: 'assistant', content: pendingContent + '\nSESSION_STALE_MARKER' })
  await showSidebar('Sessions')
  await page.getByRole('button', { name: '历史会话 idle' }).click()
  await page.getByRole('heading', { name: '历史记录' }).waitFor()
  await page.waitForTimeout(180)
  check(await page.locator('.message-content').count() === 1 && !(await page.locator('.console-history').textContent()).includes('SESSION_STALE_MARKER'), '会话切换会取消待渲染快照，不污染新历史')
  await page.getByRole('button', { name: 'UI 重构演示 idle' }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  const drag = async (title, dx, dy) => {
    const box = await page.getByTitle(title, { exact: true }).boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down(); await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2 + dy); await page.mouse.up()
  }
  await drag('拖动调整 Sidebar 宽度', 70, 0)
  await showSidebar('Workspace')
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  await drag('拖动调整 File Tree 高度', 0, -35)
  await drag('拖动调整 Terminal 高度', 0, 30)
  const layout = await page.evaluate(() => JSON.parse(localStorage.getItem('mycode.layout.v2')))
  check(layout.sidebarWidth === 390 && layout.workspaceTreeHeight === 205 && layout.terminalHeight === 270 && layout.sidebarView === 'workspace', 'Sidebar、文件树与 Terminal 拖动并保存 v2 布局')
  check(await page.locator('.tree-pane').evaluate((el) => Math.abs(el.getBoundingClientRect().height - 205) < 1), '预览展开时拖动实际改变文件树高度')
  await page.getByRole('button', { name: '关闭文件预览', exact: true }).click()
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  check(await page.locator('.tree-pane').evaluate((el) => Math.abs(el.getBoundingClientRect().height - 205) < 1), '关闭并重新打开预览保留拖动高度')
  await page.getByTitle('折叠 Terminal', { exact: true }).click()
  const collapsedTerminal = await page.locator('.terminal-drawer').evaluate((el) => ({ height: el.getBoundingClientRect().height, background: getComputedStyle(el).backgroundColor, lines: el.querySelectorAll('h2').length, terminal: Boolean(el.querySelector('.xterm')) }))
  check(collapsedTerminal.height === 44 && collapsedTerminal.background === 'rgb(255, 255, 255)' && collapsedTerminal.lines === 1 && !collapsedTerminal.terminal, 'Terminal 收起为 44px 浅色单行工具栏')
  check(await page.locator('.terminal-splitter').count() === 0 && await page.evaluate(() => JSON.parse(localStorage.getItem('mycode.layout.v2')).terminalHeight) === 270, '收起状态移除拖动分隔条且保留展开高度')
  await page.getByTitle('展开 Terminal', { exact: true }).click()
  await page.locator('.terminal-status[data-status="ready"]').waitFor()
  check(await page.locator('.terminal-drawer').evaluate((el) => getComputedStyle(el).backgroundColor === 'rgb(16, 22, 18)'), 'Terminal 展开恢复深色')
  await page.locator('.xterm-helper-textarea').focus()
  await page.keyboard.type('echo hello')
  await page.keyboard.press('Enter')
  check(await page.evaluate(() => window.__uiTest.sent.some((item) => item.data.type === 'input') && window.__uiTest.sent.some((item) => item.data.type === 'resize')), 'Terminal 折叠恢复、输入与 resize 协议')
  await page.reload()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  check(await page.evaluate(() => JSON.parse(localStorage.getItem('mycode.layout.v2')).sidebarWidth === 390) && await page.locator('.workspace-panel').isVisible() && await page.locator('.sidebar').evaluate((el) => el.getBoundingClientRect().width === 390), '刷新后恢复 Sidebar 视图与拖动宽度')
  check(await page.locator('.preview, .workspace-splitter').count() === 0 && await page.evaluate(() => JSON.parse(localStorage.getItem('mycode.layout.v2')).workspaceTreeHeight) === 205, '页面刷新清空文件选择，仍保留预览分割高度配置')
  check(await page.locator('.eyebrow').count() === 0, '移除重复面板标题')
  const preview = '## 工作台布局完成\n\nSessions 与 Workspace 共用一个 Sidebar，Terminal 仅位于右侧。\n\n| 区域 | 布局 |\n| --- | --- |\n| Activity Bar | 始终可见，图标切换 Sidebar |\n| Agent | 正文使用聊天工作区可用宽度 |\n\n```javascript\nconst sidebarView = "workspace";\n```'
  await emit('console_event', { id: 3, kind: 'assistant', content: preview, data: { turn_id: 'turn-a' } })
  await page.getByRole('heading', { name: '工作台布局完成' }).waitFor()
  const trackGeometry = () => page.evaluate(() => {
    const history = document.querySelector('.console-history'), style = getComputedStyle(history)
    const group = history.querySelector('.execution-group'), header = group.querySelector('.execution-header')
    const assistant = group.querySelector('.chat-message-assistant .chat-message-body'), bubble = group.querySelector('.chat-message-user .chat-message-body')
    const left = history.getBoundingClientRect().left + history.clientLeft + parseFloat(style.paddingLeft)
    const right = history.getBoundingClientRect().left + history.clientLeft + history.clientWidth - parseFloat(style.paddingRight)
    return {
      trackWidth: right - left, groupWidth: group.getBoundingClientRect().width, headerWidth: header.getBoundingClientRect().width,
      headerLeftGap: header.getBoundingClientRect().left - left, assistantLeftGap: assistant.getBoundingClientRect().left - left,
      assistantWidth: assistant.getBoundingClientRect().width, bubbleWidth: bubble.getBoundingClientRect().width, bubbleRightGap: right - bubble.getBoundingClientRect().right,
      contentLeftGap: getComputedStyle(group.querySelector('.execution-content')).display === 'none' ? null : group.querySelector('.execution-content').getBoundingClientRect().left - header.getBoundingClientRect().left,
    }
  })
  const assertTrack = (geometry, label) => check(Math.abs(geometry.groupWidth - geometry.trackWidth) < 1 && Math.abs(geometry.headerWidth - geometry.trackWidth) < 1 && Math.abs(geometry.headerLeftGap) < 1 && Math.abs(geometry.assistantLeftGap) < 1 && Math.abs(geometry.bubbleRightGap) < 1 && Math.abs(geometry.assistantWidth - geometry.trackWidth) < 1 && geometry.bubbleWidth <= Math.min(geometry.trackWidth * .75, 760) + 1, label)
  const expandedGeometry = await trackGeometry()
  assertTrack(expandedGeometry, '1440px Sidebar 展开时 Agent 正文填满轨道，用户气泡保持限宽与右对齐')
  await page.locator('.execution-header').first().click()
  check(Math.abs((await trackGeometry()).contentLeftGap) < 1, '展开后的工具详情与执行条左边界对齐')
  await page.locator('.execution-header').first().click()
  await page.locator('.file-button').filter({ hasText: 'README.md' }).click()
  await page.waitForFunction(() => document.querySelector('.preview pre')?.textContent === '# workspace preview')
  check(await page.locator('.tree-pane').evaluate((el) => Math.abs(el.getBoundingClientRect().height - 205) < 1), '刷新后打开文件恢复持久化的文件树高度')
  await page.locator('.file-tree > li > details').first().evaluate((el) => { el.open = false })
  await page.locator('.console-history').evaluate((el) => { el.scrollTop = 0 })
  const workbenchGeometry = () => page.evaluate(() => {
    const rect = (selector) => { const element = document.querySelector(selector); const box = element.getBoundingClientRect(); return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, visible: element.getClientRects().length > 0 } }
    return { activity: rect('.activity-bar'), sidebar: rect('.sidebar'), agent: rect('.agent-panel'), terminal: rect('.terminal-drawer'), main: rect('.main-area'), pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth }
  })
  const assertWorkbench = (geometry, label) => check(geometry.pageWidth <= geometry.viewportWidth && geometry.activity.width === 48 && Math.abs(geometry.activity.top - geometry.main.top) < 1 && Math.abs(geometry.activity.bottom - geometry.terminal.bottom) < 1 && Math.abs(geometry.agent.left - geometry.terminal.left) < 1 && Math.abs(geometry.agent.width - geometry.terminal.width) < 1 && geometry.terminal.left >= geometry.activity.right && (!geometry.sidebar.visible || (geometry.terminal.left >= geometry.sidebar.right && Math.abs(geometry.sidebar.bottom - geometry.activity.bottom) < 1)), label)
  const expandedWorkbench = await workbenchGeometry()
  assertWorkbench(expandedWorkbench, 'Terminal 仅占右侧工作区，Activity Bar/Sidebar 延伸到页面底部')
  await page.screenshot({ path: 'output/playwright/mycode-workbench-expanded.png', animations: 'disabled' })
  await input.fill('继续检查实现细节，并运行前端构建。')
  await page.screenshot({ path: 'output/playwright/mycode-composer-normal.png', animations: 'disabled' })
  await activity('Workspace').click()
  await page.getByTitle('折叠 Terminal', { exact: true }).click()
  const collapsedGeometry = await trackGeometry()
  assertTrack(collapsedGeometry, '1440px Sidebar 收起时 Agent 正文填满轨道，用户气泡位置与限宽不变')
  check(collapsedGeometry.assistantWidth > 760, '宽屏 Agent 正文超过原来的 760px 限制')
  check(collapsedGeometry.trackWidth > 1000 && collapsedGeometry.headerWidth > 1000, '执行过程利用宽轨道而非居中 760px 容器')
  const collapsedWorkbench = await workbenchGeometry()
  assertWorkbench(collapsedWorkbench, 'Sidebar 与 Terminal 收起后活动栏未被遮挡')
  await page.screenshot({ path: 'output/playwright/mycode-workbench-collapsed.png', animations: 'disabled' })
  await page.reload()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  check(await page.locator('.terminal-drawer.collapsed').isVisible() && !await page.locator('.sidebar').isVisible() && await page.locator('.activity-bar').isVisible(), '刷新保留 Sidebar 与 Terminal 收起状态')
  await page.locator('.terminal-heading').click()
  await page.locator('.terminal-status[data-status="ready"]').waitFor()
  check(await page.locator('.terminal-drawer').evaluate((el) => el.getBoundingClientRect().height === 270), '点击工具栏恢复保存的 Terminal 高度')
  await showSidebar('Workspace')
  const longCode = 'const path = "' + 'long_path/'.repeat(100) + '";\n'
  const wideTable = '| ' + Array.from({ length: 20 }, (_, i) => 'Column_' + i).join(' | ') + ' |\n| ' + Array(20).fill('---').join(' | ') + ' |\n| ' + Array(20).fill('Long table content for horizontal scrolling').join(' | ') + ' |'
  await emit('console_event', { id: 6, kind: 'user', content: 'D:\\' + 'long-directory\\'.repeat(80), data: { turn_id: 'long-turn' } })
  await emit('console_event', { id: 7, kind: 'assistant', content: '## 长内容检查\n\n```javascript\n' + longCode + '```\n\n' + wideTable, data: { turn_id: 'long-turn' } })
  await page.getByRole('heading', { name: '长内容检查' }).waitFor()
  const containment = () => page.evaluate(() => {
    const history = document.querySelector('.console-history')
    const group = document.querySelector('.execution-group:last-of-type')
    const pre = group.querySelector('.markdown-code-block pre')
    const table = group.querySelector('.markdown-table-scroll')
    const bubble = group.querySelector('.chat-message-user .chat-message-body')
    const assistant = group.querySelector('.chat-message-assistant .chat-message-body')
    const groupBox = group.getBoundingClientRect(), bubbleBox = bubble.getBoundingClientRect()
    pre.scrollLeft = 50
    table.scrollLeft = 50
    return { page: document.documentElement.scrollWidth <= innerWidth, history: history.scrollWidth <= history.clientWidth + 1, code: pre.scrollWidth > pre.clientWidth && pre.scrollLeft > 0, table: table.scrollWidth > table.clientWidth && table.scrollLeft > 0, bubble: bubble.scrollWidth <= bubble.clientWidth + 1 && bubbleBox.width <= Math.min(groupBox.width * .75, 760) + 1 && Math.abs(bubbleBox.right - groupBox.right) < 1, assistant: Math.abs(assistant.getBoundingClientRect().width - groupBox.width) < 1 }
  })
  check(Object.values(await containment()).every(Boolean), '桌面 Agent 正文填满轨道，长代码与宽表格在自身容器内滚动')
  await page.setViewportSize({ width: 900, height: 1000 })
  check(Object.values(await containment()).every(Boolean), '900px 窄屏内容不撑破 Agent')
  const narrowWorkbench = await workbenchGeometry()
  assertWorkbench(narrowWorkbench, '900px 布局无横向页面滚动与导航遮挡')
  check(narrowWorkbench.sidebar.width <= 280 && narrowWorkbench.agent.width >= 480 && await page.evaluate(() => JSON.parse(localStorage.getItem('mycode.layout.v2')).sidebarWidth) === 390, '900px 自动缩窄 Sidebar 且保留桌面宽度偏好')
  check(await page.locator('.sidebar').evaluate((el) => el.scrollWidth <= el.clientWidth + 1), '长文件名不会撑破窄 Sidebar')
  await page.setViewportSize({ width: 900, height: 720 })
  assertWorkbench(await workbenchGeometry(), '900×720 常见笔记本尺寸无页面横向滚动与导航遮挡')
  await page.setViewportSize({ width: 900, height: 1000 })
  await activity('Workspace').click()
  check(Object.values(await containment()).every(Boolean), '900px 折叠侧栏后布局与内容宽度')
  await page.screenshot({ path: 'output/playwright/mycode-ui-narrow.png' })
  await page.setViewportSize({ width: 1920, height: 1080 })
  check(Object.values(await containment()).every(Boolean), '1920px Agent 正文填满轨道，用户气泡限宽，代码与表格内部滚动')
  await showSidebar('Workspace')
  assertWorkbench(await workbenchGeometry(), '1920px 工作台布局与 Terminal 范围正确')
  check(await page.locator('.sidebar').evaluate((el) => el.getBoundingClientRect().width === 390), '返回桌面宽度后恢复 Sidebar 偏好')
  await page.locator('.agent-heading-actions button').first().click()
  await page.locator('.context-panel').waitFor()
  await emit('runtime_status', { status: 'waiting_permission', turn_id: 'permission-turn' })
  await emit('permission_request', { turn_id: 'permission-turn', request_id: 'permission-1', target: 'D:\\' + 'directory\\'.repeat(50), tool_name: 'write_file', prompt: '允许写入 **plain text** 吗？' })
  await page.waitForFunction(() => document.querySelector('.permission-step[data-status="waiting"]'))
  check(await page.locator('.context-panel').count() === 0 && await page.locator('.agent-heading-actions button:disabled').count() === 2, 'Permission 到达关闭 Context，保持 Context 与 Compact 禁用规则')
  check(await page.locator('.permission-step').isVisible() && await page.getByRole('button', { name: '仅本次', exact: true }).isVisible(), '等待权限及操作按钮保持可见')
  check((await page.locator('.permission-step p:not(.permission-target)').textContent()).includes('**plain text**') && await page.locator('.permission-step strong').count() === 0, '权限内容保持纯文本')
  await page.getByRole('button', { name: '仅本次', exact: true }).click()
  check(requests.some((request) => request.path.endsWith('/permission') && JSON.parse(request.body).decision === 'once'), 'Permission 确认 API 参数保留')
  await emit('permission_resolved', { turn_id: 'permission-turn', request_id: 'permission-1' })
  await emit('runtime_status', { status: 'idle', turn_id: 'permission-turn' })
  await page.locator('.agent-heading-actions button').first().click()
  await page.locator('.context-panel').waitFor()
  await emit('runtime_status', { status: 'waiting_mcp_trust', turn_id: 'permission-turn' })
  await emit('mcp_trust_request', { request_id: 'trust-1', servers: [{ alias: 'test-server', transport: 'stdio', command: 'python', args: ['server.py'], env_keys: ['TEST_KEY'] }] })
  await page.locator('.mcp-trust-card').waitFor()
  check(await page.locator('.context-panel').count() === 0, 'MCP Trust 到达关闭浮动面板，不遮挡重要操作')
  check(await page.getByRole('button', { name: 'Reject', exact: true }).isVisible() && await page.getByRole('button', { name: 'Allow', exact: true }).isVisible(), 'MCP Trust 安全摘要和确认操作可见')
  await page.getByRole('button', { name: 'Allow', exact: true }).click()
  check(requests.some((request) => request.path.endsWith('/mcp-trust') && JSON.parse(request.body).approved === true), 'MCP Trust API 参数保留')
  await emit('mcp_trust_resolved', { request_id: 'trust-1' })
  const permissionExecution = page.locator('.execution-header[data-status="waiting"]').last()
  if (await permissionExecution.getAttribute('aria-expanded') === 'false') await permissionExecution.click()
  await emit('console_live', { active: true, turn_id: 'permission-turn', kind: 'tool', content: '工具日志 **必须为原文**\n{"value":"<img src=x onerror=alert(1)>"}' })
  await page.locator('.live-card[data-kind="tool"]').waitFor()
  check((await page.locator('.live-card pre').textContent()).includes('**必须为原文**'), '实时工具日志保持原文')
  await emit('console_event', { id: 8, kind: 'error', content: '执行错误 <script>plain</script>', data: { turn_id: 'permission-turn' } })
  await emit('runtime_status', { status: 'error', turn_id: 'permission-turn' })
  await page.locator('.error-card').waitFor()
  check(await page.locator('.error-card').isVisible() && await page.locator('.execution-header[data-status="error"]').isVisible(), '执行错误文本与错误状态可见')
  await emit('runtime_error', { message: '模拟运行时错误' })
  await page.locator('.error-banner').waitFor()
  check(await page.locator('.error-banner').innerText() === '模拟运行时错误', '全局错误提示可见')
  const denied = await page.evaluate(() => {
    window.__oldExecCommand = document.execCommand
    document.execCommand = () => false
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.reject(new Error('denied')) } })
    return true
  })
  await page.getByRole('button', { name: '复制代码', exact: true }).last().click()
  await page.getByRole('button', { name: '复制失败，请选择代码手动复制' }).waitFor()
  check(denied, '剪贴板拒绝时显示失败提示')
  await page.evaluate(() => { delete navigator.clipboard; document.execCommand = window.__oldExecCommand })
  await showSidebar('Workspace')
  await page.locator('.workspace-toolbar input[type="file"]').first().evaluate((input) => {
    const transfer = new DataTransfer()
    transfer.items.add(new File(['uploaded fixture'], 'note.txt', { type: 'text/plain' }))
    input.files = transfer.files
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
  const uploaded = page.locator('.file-button').filter({ hasText: 'note.txt' })
  await uploaded.waitFor()
  check(requests.some((item) => item.path.endsWith('/files/upload') && item.method === 'POST' && item.body.includes('note.txt') && item.body.includes('false')), 'Workspace 上传发送原有 multipart API 并刷新树')
  await uploaded.click()
  await page.waitForFunction(() => document.querySelector('.preview-title span')?.textContent === 'note.txt')
  const [fileDownload] = await Promise.all([page.waitForEvent('download'), page.locator('.preview-title').getByRole('link', { name: 'Download' }).click()])
  check(fileDownload.suggestedFilename() === 'note.txt', '选中文件下载链接及浏览器下载正常')
  const [workspaceDownload] = await Promise.all([page.waitForEvent('download'), page.locator('.workspace-toolbar').getByRole('link', { name: 'Download' }).click()])
  check(workspaceDownload.suggestedFilename() === 'workspace.zip', 'Workspace 整体下载正常')
  await page.locator('.file-row').filter({ has: uploaded }).getByTitle('删除文件', { exact: true }).click()
  await page.getByRole('dialog', { name: '删除文件？', exact: true }).getByRole('button', { name: '删除', exact: true }).click()
  await uploaded.waitFor({ state: 'detached' })
  check(await page.locator('.preview, .workspace-splitter, .file-button.selected').count() === 0 && requests.some((item) => item.path.endsWith('/files') && item.method === 'DELETE'), '删除文件保留确认流程并移除被删文件预览')
  await showSidebar('Sessions')
  holdHistory = true
  await page.locator('.sessions-panel').getByRole('button', { name: 'New Session', exact: true }).click()
  await page.getByText('正在加载会话历史…', { exact: true }).waitFor()
  check(!await page.getByText('开始与 MyCode 对话，或描述你希望完成的任务。', { exact: true }).isVisible(), '加载历史时显示加载文案而非空闲引导')
  await waitForFixture(() => releaseHistory)
  holdHistory = false
  releaseHistory()
  await page.getByText('开始与 MyCode 对话，或描述你希望完成的任务。', { exact: true }).waitFor()
  check(true, '空闲且没有聊天记录时显示任务引导')
  await page.locator('.agent-heading-actions button').first().click()
  await page.locator('.context-panel').waitFor()
  check(await page.getByText('开始与 MyCode 对话，或描述你希望完成的任务。', { exact: true }).isVisible(), '空闲 Context 查询不将空会话误判为 Agent 执行中')
  await page.getByRole('button', { name: '关闭 Context', exact: true }).click()
  for (const status of ['starting', 'queued', 'running']) {
    await emit('runtime_status', { status })
    await page.getByText('等待 Agent 输出…', { exact: true }).waitFor()
    check(!await page.getByText('开始与 MyCode 对话，或描述你希望完成的任务。', { exact: true }).isVisible(), status + ' 无消息时不显示空闲引导')
  }
  for (const [status, prompt] of [
    ['stopped', 'Agent 已停止。'],
    ['error', 'Agent 运行出错，请查看错误提示。'],
  ]) {
    await emit('runtime_status', { status })
    await page.getByText(prompt, { exact: true }).waitFor()
    check(await page.getByText('等待 Agent 输出…', { exact: true }).count() === 0, status + ' 无消息时不再提示等待输出')
  }
  await emit('runtime_status', { status: 'idle' })
  check(await page.evaluate(() => new URL(location.href).searchParams.get('session')) === 'ui-created', 'Session 创建与自动打开正常')
  const createdEntry = page.locator('.session-entry').filter({ has: page.getByRole('button', { name: 'Session idle', exact: true }) })
  await createdEntry.getByTitle('会话菜单').click()
  await page.getByRole('button', { name: 'Rename', exact: true }).click()
  const renameDialog = page.getByRole('dialog', { name: '重命名会话', exact: true })
  await renameDialog.getByRole('textbox', { name: '会话名称' }).fill('回归测试会话')
  await renameDialog.getByRole('button', { name: '保存', exact: true }).click()
  const renamedEntry = page.locator('.session-entry').filter({ has: page.getByRole('button', { name: '回归测试会话 idle', exact: true }) })
  await renamedEntry.waitFor()
  check(requests.some((item) => item.path === '/sessions/ui-created' && item.method === 'PATCH' && JSON.parse(item.body).name === '回归测试会话'), 'Session 重命名与列表更新正常')
  await renamedEntry.getByTitle('会话菜单').click()
  await page.getByRole('button', { name: 'Delete', exact: true }).click()
  await page.getByRole('dialog', { name: '删除会话？', exact: true }).getByRole('button', { name: '删除会话', exact: true }).click()
  await page.getByRole('heading', { name: '实现完成', exact: true }).waitFor()
  check(await page.evaluate(() => new URL(location.href).searchParams.get('session')) === 'ui-alpha' && await renamedEntry.count() === 0, 'Session 删除后切回存量会话并恢复历史')
  // Batch 3: chronological execution records, conservative correlation and folds.
  const thirdEvents = []
  const toolEvent = (id, turn, type, payload) => ({ id, kind: 'tool', content: type, data: { turn_id: turn, event_type: type, event: { type, ...payload } } })
  const thirdEmit = async (event) => { thirdEvents.push(event); await emit('console_event', event) }
  const turnGroup = (turn) => page.locator('.execution-group').filter({ has: page.locator('#execution-' + turn) })
  const thirdCall = (id, turn, callId, name = 'read_file', args = { path: 'README.md' }) => toolEvent(id, turn, 'tool_call', { tool_call: { ...(callId ? { id: callId } : {}), name, arguments: args } })
  const thirdResult = (id, turn, callId, ok, extra = {}) => toolEvent(id, turn, 'tool_result', { tool_result: { ...(callId ? { tool_call_id: callId } : {}), ok, content: 'available output', ...extra } })
  const previewEvents = [toolEvent(90, 'preview-check', 'turn', {}), toolEvent(91, 'preview-check', 'model_start', {}),
    toolEvent(92, 'preview-check', 'context', { content: 'Input tokens: 1200 / 8000' }),
    toolEvent(93, 'preview-check', 'model_start', { content: 'model_start: provider/model' })]
  for (const event of previewEvents) await emit('console_event', event)
  const previewGroup = turnGroup('preview-check')
  await previewGroup.locator('.execution-header').click()
  check(await previewGroup.locator('.runtime-step').count() === 4 && (await previewGroup.locator('.execution-header').innerText()).includes('4 steps'), 'Runtime 预览去重保留所有事件和原始步骤数量')
  for (const id of [90, 91]) {
    const step = previewGroup.locator('[data-event-id="event-' + id + '"]')
    check(await step.locator('.runtime-preview').count() === 0 && await step.locator('.step-heading').innerText() === previewEvents[id - 90].content, 'Runtime ' + previewEvents[id - 90].content + ' 摘要仅显示一次事件名称')
    await step.locator(':scope > summary').click()
    check(await step.locator('.step-details > pre').innerText() === previewEvents[id - 90].content, 'Runtime 去重后展开详情仍保留 ' + previewEvents[id - 90].content)
    await step.locator('.step-source > summary').click()
    check(JSON.stringify(JSON.parse(await step.locator('.step-data').innerText())) === JSON.stringify([previewEvents[id - 90]]), 'Runtime 去重后原始事件信息完整保留')
  }
  check(JSON.stringify(await previewGroup.locator('.runtime-preview').allTextContents()) === JSON.stringify(['Input tokens: 1200 / 8000', 'model_start: provider/model']), 'context 与同名事件的实际附加信息继续显示预览')
  await emit('runtime_status', { status: 'running', turn_id: 'third-success' })
  await emit('runtime_status', { status: 'idle', turn_id: 'stale-turn' })
  check((await page.locator('.agent-heading small').innerText()).includes('running'), 'Runtime 修复：旧 Turn 的 idle 不覆盖当前执行状态')
  await thirdEmit(thirdCall(100, 'third-success', 'call-read'))
  const successGroup = turnGroup('third-success')
  check(await successGroup.locator('.execution-header').getAttribute('aria-expanded') === 'true' && await successGroup.locator('.execution-step[data-status="running"]').isVisible(), '第三批：新执行自动展开且 Call 只显示运行图标')
  await thirdEmit(thirdResult(101, 'third-success', 'call-read', true, { content: '<img src=x onerror="window.__toolXss=1"> **literal**' }))
  check(await successGroup.locator('.execution-step').count() === 1 && (await successGroup.locator('.execution-header').innerText()).includes('2 steps'), '第三批：明确 ID 合并 Call/Result，摘要仍计算两个原始步骤')
  check(await successGroup.locator('.execution-step .step-icon').innerText() === '✓' && (await successGroup.locator('.step-target').innerText()) === 'README.md', '第三批：仅 ok true 显示成功，可靠目标显示在摘要')
  await successGroup.locator('.execution-step > summary').click()
  check((await successGroup.locator('.step-details').innerText()).includes('Arguments') && (await successGroup.locator('.step-details').innerText()).includes('Result'), '第三批：参数与结果在同一条工具详情内')
  check(await successGroup.locator('img, script').count() === 0 && (await successGroup.locator('.step-details').innerText()).includes('**literal**') && !await page.evaluate(() => window.__toolXss), '第三批：工具 HTML、JSON 和日志以纯文本展示，无 XSS')
  await emit('console_event', { ...thirdEvents[1], id: 9000, console_id: 101 })
  check(await successGroup.locator('.execution-step').count() === 1 && (await successGroup.locator('.execution-header').innerText()).includes('2 steps'), '第三批：console_id 重放更新同一条步骤，不重复计数')
  await emit('runtime_status', { status: 'idle', turn_id: 'third-success' })
  check(await successGroup.locator('.execution-header').getAttribute('aria-expanded') === 'false', '第三批：正常结束默认收起，不保留自动展开状态')
  await successGroup.locator('.execution-header').click()
  await emit('runtime_status', { status: 'idle', turn_id: 'third-success' })
  check(await successGroup.locator('.execution-header').getAttribute('aria-expanded') === 'true', '第三批：完成后用户手动展开不被 SSE idle 覆盖')

  await emit('runtime_status', { status: 'running', turn_id: 'third-order' })
  await thirdEmit(thirdCall(102, 'third-order', 'first'))
  await thirdEmit(toolEvent(103, 'third-order', 'progress', { content: '真实运行进度' }))
  await thirdEmit(thirdCall(104, 'third-order', 'second', 'run_command', { command: 'pytest' }))
  await thirdEmit(thirdResult(105, 'third-order', 'second', false, { error: 'actual command failure <script>plain</script>' }))
  await thirdEmit(thirdResult(106, 'third-order', 'first', true))
  const orderGroup = turnGroup('third-order')
  check(JSON.stringify(await orderGroup.locator('.execution-content > [data-event-id]').evaluateAll((rows) => rows.map((row) => row.dataset.eventId))) === JSON.stringify(['event-102', 'event-104']), '第三批：明确 ID 的交错结果合并，隐藏 progress 后仍按原始调用顺序展示')
  check(await orderGroup.locator('[data-event-id="event-104"]').getAttribute('open') !== null && (await orderGroup.locator('.step-error').innerText()).includes('actual command failure'), '第三批：失败工具详情自动展开并显示实际原因')
  check(await orderGroup.locator('.execution-header').getAttribute('data-status') === 'running', '第三批：工具失败不将运行中的 Turn 标记成 Error')
  await orderGroup.locator('[data-event-id="event-104"] > summary').click()
  await thirdEmit(toolEvent(107, 'third-order', 'model_retry', { content: '模型正在重试' }))
  check(await orderGroup.locator('[data-event-id="event-104"]').getAttribute('open') === null, '第三批：工具详情手动收起后不被后续 SSE 打开')
  await orderGroup.locator('.execution-header').click()
  await thirdEmit(toolEvent(108, 'third-order', 'artifact_warning', { content: '结果文件不可用' }))
  check(await orderGroup.locator('.execution-header').getAttribute('aria-expanded') === 'false', '第三批：执行中手动折叠不被新运行事件改变')
  await emit('runtime_status', { status: 'idle', turn_id: 'third-order' })
  check(await orderGroup.locator('.execution-header').getAttribute('aria-expanded') === 'false' && (await orderGroup.locator('.execution-header').innerText()).includes('Completed · 有工具失败'), '第三批：完成和工具失败独立显示，保留用户折叠偏好')
  await orderGroup.locator('.execution-header').click()
  check((await orderGroup.locator('.runtime-preview').first().innerText()).includes('模型正在重试'), '第三批：可见运行提示默认显示简短实际信息，完整内容留在详情')
  check(await orderGroup.locator('[data-event-id="event-103"]').count() === 0 && await orderGroup.locator('.runtime-step').count() === 2 && (await orderGroup.locator('.execution-header').innerText()).includes('6 steps'), 'P2：progress 隐藏且不计数，model_retry 和 artifact_warning 保留')

  await thirdEmit(thirdCall(109, 'third-adjacent', 'legacy-id'))
  await thirdEmit(thirdResult(110, 'third-adjacent', null, true))
  const adjacentGroup = turnGroup('third-adjacent')
  await adjacentGroup.locator('.execution-header').click()
  check(await adjacentGroup.locator('.execution-step').count() === 1 && await adjacentGroup.locator('.execution-step').getAttribute('data-status') === 'success', '第三批：现有无结果 ID 协议仅在相邻唯一调用时保守合并')
  for (const fixture of [
    ['third-batch', [thirdCall(111, 'third-batch', 'a'), thirdCall(112, 'third-batch', 'b'), thirdResult(113, 'third-batch', null, true), thirdResult(114, 'third-batch', null, false)]],
    ['third-gap', [thirdCall(115, 'third-gap', 'gap'), thirdResult(117, 'third-gap', null, true)]],
    ['third-interleaved', [thirdCall(118, 'third-interleaved', 'cross'), toolEvent(119, 'third-interleaved', 'context', { content: '输入上下文' }), thirdResult(120, 'third-interleaved', null, true)]],
    ['third-missing', [thirdCall(121, 'third-missing', 'missing')]],
    ['third-unknown', [thirdResult(122, 'third-unknown', null, 'true', { content: 'All tests passed' })]],
  ]) {
    for (const event of fixture[1]) await thirdEmit(event)
    const current = turnGroup(fixture[0])
    await current.locator('.execution-header').click()
    check(await current.locator('.execution-step').count() === fixture[1].length, '第三批：' + fixture[0] + ' 不错误关联')
    check(await current.locator('.execution-step[data-status="unknown"]').count() >= 1, '第三批：' + fixture[0] + ' 存在中性未知状态，不根据自然语言猜成功')
  }
  await thirdEmit(thirdCall(123, 'third-failed', 'fail'))
  await thirdEmit(thirdResult(124, 'third-failed', 'fail', false, { error: 'explicit failure' }))
  check(await turnGroup('third-failed').locator('.execution-header').getAttribute('aria-expanded') === 'true' && await turnGroup('third-failed').locator('.step-error').isVisible(), '第三批：工具失败默认展开相关错误，历史 Turn 仍为 Completed')
  await thirdEmit({ id: 125, kind: 'error', content: 'Runtime stopped unexpectedly <img onerror=alert(1)>', data: { turn_id: 'third-runtime' } })
  check(await turnGroup('third-runtime').locator('.execution-header').getAttribute('data-status') === 'error' && await turnGroup('third-runtime').locator('.error-card').isVisible(), '第三批：真实 Runtime 错误独立标记 Error 并展开')

  await emit('runtime_status', { status: 'running', turn_id: 'third-permission' })
  await thirdEmit(thirdCall(126, 'third-permission', 'write', 'write_file'))
  const permissionGroup = turnGroup('third-permission')
  await permissionGroup.locator('.execution-header').click()
  const pendingThird = { turn_id: 'third-permission', request_id: 'third-p', tool_name: 'write_file', target: 'README.md', prompt: '需要写入权限 <b>纯文本</b>' }
  await emit('permission_request', pendingThird)
  check(await permissionGroup.locator('.execution-header').getAttribute('aria-expanded') === 'false' && await permissionGroup.getByRole('button', { name: '当前任务', exact: true }).isVisible(), '第三批：新权限出现在手动折叠区域外，按钮仍可操作')
  await thirdEmit({ id: 127, kind: 'permission', content: 'write_file: write · README.md', data: pendingThird })
  await emit('console_event', thirdEvents.at(-1))
  check(await permissionGroup.locator('.permission-actions').count() === 1 && await permissionGroup.locator('.permission-step').count() === 1, '第三批：权限请求从 pending 到持久事件及重放只显示一套按钮')
  check(await permissionGroup.locator('.permission-step').getAttribute('data-status') === 'waiting' && (await permissionGroup.locator('.permission-target').innerText()).includes('write_file · README.md'), '第三批：权限卡展示实际工具及目标')
  for (const decision of ['deny', 'once', 'task', 'session']) {
    const scopedRequest = { ...pendingThird, request_id: 'scope-' + decision }
    await emit('permission_request', scopedRequest)
    const buttonText = { deny: '拒绝', once: '仅本次', task: '当前任务', session: '当前会话' }[decision]
    await permissionGroup.getByRole('button', { name: buttonText, exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.permission-feedback')?.textContent.includes('已提交'))
    check(requests.some((request) => request.path.endsWith('/permission') && JSON.parse(request.body).request_id === scopedRequest.request_id && JSON.parse(request.body).decision === decision), '第三批：' + decision + ' 权限作用域及 request_id 参数保持')
    await emit('permission_resolved', { ...scopedRequest, decision })
  }
  await emit('permission_request', pendingThird)
  await thirdEmit({ id: 128, kind: 'permission', content: '已允许（当前任务）', data: { ...pendingThird, decision: 'task', allowed: true } })
  await emit('permission_resolved', { ...pendingThird, decision: 'task' })
  check(await permissionGroup.locator('.permission-actions').count() === 0 && await permissionGroup.locator('.execution-header').getAttribute('aria-expanded') === 'false', '第三批：权限已解决后移除按钮，不改变用户折叠')
  await permissionGroup.locator('.execution-header').click()
  await thirdEmit(thirdResult(129, 'third-permission', 'write', true))
  check(JSON.stringify(await permissionGroup.locator('.execution-content > [data-event-id]').evaluateAll((rows) => rows.map((row) => row.dataset.eventId))) === JSON.stringify(['event-126', 'event-127', 'event-128']), '第三批：请求与批准各保留真实事件位置，Call/Result 可靠合并')
  check(await permissionGroup.locator('.permission-step[data-status="allowed"]').count() === 2 && (await permissionGroup.locator('.execution-header').innerText()).includes('3 steps'), '第三批：权限状态正确更新，生命周期计一个步骤')
  await emit('runtime_status', { status: 'idle', turn_id: 'third-permission' })
  check(await permissionGroup.locator('.execution-header').getAttribute('aria-expanded') === 'true', '第三批：用户展开的执行过程在完成后仍展开')

  const thirdSnapshot = () => page.evaluate(() => [...document.querySelectorAll('.execution-group')].filter((group) => group.querySelector('[id^="execution-third-"]')).map((group) => ({ summary: group.querySelector('.execution-header > span:last-child').textContent.trim(), status: group.querySelector('.execution-header').dataset.status, rows: [...group.querySelectorAll('.execution-content > [data-event-id]')].map((row) => ({ id: row.dataset.eventId, status: row.dataset.status })) })))
  const beforeThirdReload = await thirdSnapshot()
  histories['ui-alpha'].push(...thirdEvents)
  await page.reload()
  await turnGroup('third-permission').waitFor()
  check(JSON.stringify(await thirdSnapshot()) === JSON.stringify(beforeThirdReload), '第三批：历史重载与 SSE 时间线顺序、状态、计数一致')
  for (const viewport of [{ width: 900, height: 720 }, { width: 1440, height: 1000 }, { width: 1920, height: 1080 }]) {
    await page.setViewportSize(viewport)
    const resizedPermission = turnGroup('third-permission')
    if (await resizedPermission.locator('.execution-header').getAttribute('aria-expanded') === 'false') await resizedPermission.locator('.execution-header').click()
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.querySelector('.console-history').scrollWidth <= document.querySelector('.console-history').clientWidth + 1), '第三批：' + viewport.width + '×' + viewport.height + ' 时间线详情不撑破工作台')
    assertWorkbench(await workbenchGeometry(), '第三批：' + viewport.width + '×' + viewport.height + ' 原工作台布局保持')
  }
  await turnGroup('third-permission').scrollIntoViewIfNeeded()
  await page.screenshot({ path: 'output/playwright/mycode-ui-third-timeline.png' })
  // Review fixes: messages remain outside folds and retain their event positions.
  const reviewEvents = []
  const reviewEmit = async (event) => { reviewEvents.push(event); await emit('console_event', event) }
  const reviewMessage = (id, turn, kind, content) => ({ id, kind, content, data: { turn_id: turn } })
  const visualOrder = (group) => group.evaluate((element) => [...element.children].filter((child) => child.matches('.chat-message, .execution-header')).map((child) => child.matches('.execution-header') ? 'execution' : (child.classList.contains('chat-message-user') ? 'user:' : 'assistant:') + child.dataset.eventId))
  await emit('runtime_status', { status: 'running', turn_id: 'review-order' })
  await reviewEmit(reviewMessage(200, 'review-order', 'user', '检查 README.md'))
  await reviewEmit(reviewMessage(201, 'review-order', 'assistant', '## 先读取文件\n\n我先读取文件内容。'))
  await reviewEmit(thirdCall(202, 'review-order', 'review-read'))
  await reviewEmit(thirdResult(203, 'review-order', 'review-read', true))
  await reviewEmit(reviewMessage(204, 'review-order', 'assistant', '## 第一段完成\n\n检查完成，继续执行命令。'))
  const reviewOrder = turnGroup('review-order')
  check(JSON.stringify(await visualOrder(reviewOrder)) === JSON.stringify(['user:200', 'assistant:201', 'execution', 'assistant:204']), '审查修复：Assistant → Tool → Assistant 按真实事件顺序显示')
  await reviewOrder.locator('.execution-header').first().click()
  await reviewEmit(thirdCall(205, 'review-order', 'review-command', 'run_command', { command: 'pytest' }))
  await reviewEmit(thirdResult(206, 'review-order', 'review-command', true))
  await reviewEmit(reviewMessage(207, 'review-order', 'assistant', '## 多段执行完成\n\n两段工具操作均有真实结果。'))
  check(JSON.stringify(await visualOrder(reviewOrder)) === JSON.stringify(['user:200', 'assistant:201', 'execution', 'assistant:204', 'execution', 'assistant:207']), '审查修复：同一 Turn 多段工具与 Assistant 正确交替，结果合并不重复')
  check(await reviewOrder.locator('.execution-header').first().getAttribute('aria-expanded') === 'false' && await reviewOrder.locator('.execution-header').nth(1).getAttribute('aria-expanded') === 'true', '审查修复：新增执行区块默认展开，保留前一区块手动折叠')
  check((await reviewOrder.locator('.execution-header').first().innerText()).includes('4 steps') && (await reviewOrder.locator('.execution-header').first().innerText()).includes('本段 2 steps') && (await reviewOrder.locator('.execution-header').nth(1).innerText()).includes('2 steps') && (await reviewOrder.locator('.execution-header').nth(1).innerText()).includes('同一轮执行'), '审查修复：全 Turn 摘要只出现一次，后续区块使用局部步骤及同一轮标记')
  await reviewOrder.locator('.execution-header').nth(1).click()
  for (const title of ['先读取文件', '第一段完成', '多段执行完成']) check(await reviewOrder.getByRole('heading', { name: title, exact: true }).isVisible(), '审查修复：折叠全部工具仍可阅读 ' + title)
  await emit('console_event', reviewEvents[2])
  await emit('console_event', reviewEvents[4])
  check(await reviewOrder.locator('.execution-step').count() === 2 && await reviewOrder.locator('.chat-message-assistant').count() === 3, '审查修复：SSE 重放 Call/Assistant 不重复且不改变顺序')
  await emit('console_live', { active: true, kind: 'assistant', turn_id: 'review-order', content: '## 顺序流式回复\n\n正式落盘之前的回答。' })
  await reviewOrder.getByRole('heading', { name: '顺序流式回复', exact: true }).waitFor()
  check(await reviewOrder.locator('.chat-message-assistant[aria-busy="true"]').isVisible() && (await visualOrder(reviewOrder)).at(-1) === 'assistant:undefined', '审查修复：流式回复在末尾，工具折叠不会隐藏它')
  await reviewEmit(reviewMessage(208, 'review-order', 'assistant', '## 顺序流式回复\n\n正式落盘之前的回答。'))
  await emit('runtime_status', { status: 'idle', turn_id: 'review-order' })
  check(await reviewOrder.getByRole('heading', { name: '顺序流式回复', exact: true }).count() === 1 && await reviewOrder.locator('.chat-message-assistant[aria-busy="true"]').count() === 0, '审查修复：流式转正式消息去重，位置保持在执行区块之后')
  check(await reviewOrder.locator('.execution-header[aria-expanded="false"]').count() === 2, '审查修复：多区块手动折叠在完成后的状态更新中保持')
  const sessionWarning = { id: 209, kind: 'tool', content: 'Session runtime cleanup warning', data: { code: 'runtime_cleanup_failed', message: 'Session runtime cleanup warning' } }
  await reviewEmit(sessionWarning)
  await page.locator('.runtime-notice[data-event-id="209"]').waitFor()
  check(await reviewOrder.locator('.execution-header').count() === 2 && await reviewOrder.locator('.runtime-notice').count() === 0, 'Runtime 修复：Session 警告独立展示，不增加上一轮执行段数')
  await reviewEmit(reviewMessage(210, 'review-cross', 'assistant', '## 跨消息关联前\n\n先执行一次工具。'))
  await reviewEmit(thirdCall(211, 'review-cross', 'review-cross-call'))
  await reviewEmit(reviewMessage(212, 'review-cross', 'assistant', '## 跨消息关联中\n\n结果稍后到达。'))
  await reviewEmit(thirdResult(213, 'review-cross', 'review-cross-call', true))
  await reviewEmit(thirdCall(214, 'review-cross', 'review-adjacent'))
  await reviewEmit(thirdResult(215, 'review-cross', null, true))
  await reviewEmit(reviewMessage(216, 'review-cross', 'assistant', '## 跨消息关联后\n\n两种关联规则均保留。'))
  const crossGroup = turnGroup('review-cross')
  check(JSON.stringify(await visualOrder(crossGroup)) === JSON.stringify(['assistant:210', 'execution', 'assistant:212', 'execution', 'assistant:216']) && await crossGroup.locator('.execution-step').count() === 2, '审查修复：跨 Assistant 的可靠 Result 仍锚定 Call，无 ID 的相邻关联保持')
  check(await crossGroup.locator('[data-event-id="event-211"]').getAttribute('data-status') === 'success' && await crossGroup.locator('[data-event-id="event-213"]').count() === 0, '审查修复：跨区块关联更新调用状态，不另行显示结果')
  await reviewEmit(toolEvent(217, 'review-order', 'progress', { content: '最终回答已输出' }))
  await reviewEmit(toolEvent(218, 'review-order', 'stop', { stop_reason: 'final_answer' }))
  check((await visualOrder(reviewOrder)).at(-1) === 'assistant:208' && await reviewOrder.locator('.execution-header').count() === 2 && await reviewOrder.locator('.execution-header[aria-expanded="false"]').count() === 2, 'P2：正常结束内部事件不生成尾部执行区块，真实两段执行和手动折叠保留')
  check((await reviewOrder.locator('.execution-header').first().innerText()).includes('4 steps') && await reviewOrder.locator('[data-event-id="event-217"], [data-event-id="event-218"]').count() === 0, 'P2：最终 progress 与 stop(final_answer) 均不显示或计数')
  await reviewEmit(thirdCall(220, 'review-unsafe', 'review-unmatched'))
  await reviewEmit(reviewMessage(221, 'review-unsafe', 'assistant', '中间真实文本'))
  await reviewEmit(thirdResult(222, 'review-unsafe', null, true))
  await reviewEmit(reviewMessage(223, 'review-unsafe', 'assistant', '未强制匹配的最终文本'))
  check(JSON.stringify(await visualOrder(turnGroup('review-unsafe'))) === JSON.stringify(['execution', 'assistant:221', 'execution', 'assistant:223']) && await turnGroup('review-unsafe').locator('.execution-step').count() === 2, '审查修复：不可靠的跨消息 Call/Result 保持独立顺序和归属')
  const reviewSnapshot = () => page.evaluate(() => [...document.querySelectorAll('.execution-group')].filter((element) => element.querySelector('[id^="execution-review-"]')).map((element) => ({ order: [...element.children].filter((child) => child.matches('.chat-message, .execution-header')).map((child) => ({ kind: child.classList.contains('execution-header') ? 'execution' : child.classList.contains('chat-message-user') ? 'user' : 'assistant', id: child.dataset.eventId, summary: child.classList.contains('execution-header') ? child.querySelector('span:last-child').textContent : null })), rows: [...element.querySelectorAll('.execution-content > [data-event-id]')].map((row) => ({ id: row.dataset.eventId, status: row.dataset.status })) })))
  const beforeReviewReload = await reviewSnapshot()
  histories['ui-alpha'].push(...reviewEvents)
  await page.reload()
  await turnGroup('review-unsafe').waitFor()
  check(JSON.stringify(await reviewSnapshot()) === JSON.stringify(beforeReviewReload), '审查修复：历史恢复与 SSE 增量的消息、区块、结果状态和计数一致')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await reviewOrder.scrollIntoViewIfNeeded()
  await page.screenshot({ path: 'output/playwright/mycode-ui-review-order.png' })

  // Review fixes: one submission per request, including the HTTP-success gap.
  const pendingReview = (requestId) => ({ turn_id: 'review-permission', request_id: requestId, tool_name: 'write_file', target: 'README.md', prompt: '审查权限请求 ' + requestId })
  const requestReviewPermission = async (requestId) => {
    await emit('runtime_status', { status: 'waiting_permission', turn_id: 'review-permission' })
    await emit('permission_request', pendingReview(requestId))
    await page.locator('.permission-actions').waitFor()
  }
  const resolveReviewPermission = async (requestId) => emit('permission_resolved', { ...pendingReview(requestId), decision: 'once', allowed: true })
  const postsFor = (sessionId, requestId) => requests.filter((request) => request.path === '/sessions/' + sessionId + '/permission' && JSON.parse(request.body).request_id === requestId)
  const decisions = page.locator('.permission-actions button')
  const feedback = page.locator('.permission-feedback')
  const waitSubmitted = () => page.waitForFunction(() => document.querySelector('.permission-feedback')?.textContent.includes('已提交'))
  const waitPermissionResponse = (sessionId, requestId) => page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/sessions/' + sessionId + '/permission') && response.request().postDataJSON()?.request_id === requestId)
  const finishPermissionResponse = async (responsePromise) => {
    await (await responsePromise).finished()
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  }
  const rapid = { hold: true }
  permissionFixtures.set('ui-alpha:review-rapid', rapid)
  await requestReviewPermission('review-rapid')
  await decisions.evaluateAll((buttons) => { for (const button of buttons) { button.click(); button.click() } })
  await waitForFixture(() => rapid.release)
  check(await page.locator('.permission-actions button:disabled').count() === 4 && (await feedback.innerText()) === '处理中…', '审查修复：首次权限点击立即锁定全部四个按钮并显示处理中')
  check(postsFor('ui-alpha', 'review-rapid').length === 1, '审查修复：同一帧连续点击四种按钮仅发送一个 POST')
  const reviewPermissions = turnGroup('review-permission')
  if (await reviewPermissions.locator('.execution-header').getAttribute('aria-expanded') === 'true') await reviewPermissions.locator('.execution-header').click()
  check(await feedback.isVisible() && await decisions.first().isVisible(), '审查修复：手动折叠后提交状态与权限按钮仍可见')
  rapid.release()
  await waitSubmitted()
  await emit('permission_request', pendingReview('review-rapid'))
  await decisions.evaluateAll((buttons) => buttons.forEach((button) => button.dispatchEvent(new MouseEvent('click', { bubbles: true }))))
  check(await page.locator('.permission-actions button:disabled').count() === 4 && postsFor('ui-alpha', 'review-rapid').length === 1, '审查修复：HTTP 成功和相同权限 SSE 重放均不重新开放提交')
  await requestReviewPermission('review-new')
  check(await page.locator('.permission-actions button:enabled').count() === 4, '审查修复：新 request_id 不受旧请求等待同步状态锁定')
  await decisions.nth(1).click()
  await waitSubmitted()
  await resolveReviewPermission('review-rapid')
  check(await page.locator('.permission-actions button:disabled').count() === 4, '审查修复：旧 request_id 的迟到 resolved 不清理新请求的提交状态')
  await resolveReviewPermission('review-new')
  check(await page.locator('.permission-actions').count() === 0 && await feedback.count() === 0, '审查修复：权限 resolved 清理按钮和提交反馈')
  permissionFixtures.set('ui-alpha:review-retry', { status: 500 })
  await requestReviewPermission('review-retry')
  await decisions.nth(1).click()
  await page.getByText('权限提交失败测试', { exact: true }).waitFor()
  check(await page.locator('.permission-actions button:enabled').count() === 4, '审查修复：权限 API 失败显示错误并恢复全部决策按钮')
  permissionFixtures.delete('ui-alpha:review-retry')
  await decisions.nth(2).click()
  await waitSubmitted()
  check(postsFor('ui-alpha', 'review-retry').length === 2 && JSON.parse(postsFor('ui-alpha', 'review-retry')[1].body).decision === 'task', '审查修复：失败后可重试且保留新的 decision 和原 request_id')
  await resolveReviewPermission('review-retry')
  await page.getByText('权限提交失败测试', { exact: true }).waitFor({ state: 'detached', timeout: 10000 })

  const oldRequest = { hold: true, status: 500 }, newRequest = { hold: true }
  permissionFixtures.set('ui-alpha:review-old', oldRequest)
  permissionFixtures.set('ui-alpha:review-current', newRequest)
  await requestReviewPermission('review-old')
  await decisions.nth(1).click()
  await waitForFixture(() => oldRequest.release)
  await requestReviewPermission('review-current')
  check(await page.locator('.permission-actions button:enabled').count() === 4, '审查修复：旧 POST 尚未返回时新权限仍可提交')
  await decisions.nth(2).click()
  await waitForFixture(() => newRequest.release)
  const oldRequestResponse = waitPermissionResponse('ui-alpha', 'review-old')
  oldRequest.release()
  await finishPermissionResponse(oldRequestResponse)
  check(await page.locator('.permission-actions button:disabled').count() === 4 && (await feedback.innerText()) === '处理中…' && await page.locator('.error-banner').count() === 0, '审查修复：旧请求迟到失败不解锁新请求或污染错误提示')
  newRequest.release()
  await waitSubmitted()
  await resolveReviewPermission('review-current')

  const switchReviewSession = async (name, heading) => {
    await showSidebar('Sessions')
    await page.locator('.session-item').filter({ hasText: name }).click()
    await page.getByRole('heading', { name: heading, exact: true }).waitFor()
  }
  const oldSession = { hold: true }, betaSubmission = { hold: true }
  permissionFixtures.set('ui-alpha:review-session', oldSession)
  permissionFixtures.set('ui-beta:review-session', betaSubmission)
  await requestReviewPermission('review-session')
  await decisions.nth(1).click()
  await waitForFixture(() => oldSession.release)
  await switchReviewSession('历史会话', '历史记录')
  await requestReviewPermission('review-session')
  check(await page.locator('.permission-actions button:enabled').count() === 4, '审查修复：切换 Session 后相同 request_id 的新权限不被旧提交锁定')
  await decisions.nth(2).click()
  await waitForFixture(() => betaSubmission.release)
  const oldSessionResponse = waitPermissionResponse('ui-alpha', 'review-session')
  oldSession.release()
  await finishPermissionResponse(oldSessionResponse)
  check((await feedback.innerText()) === '处理中…' && await page.locator('.permission-actions button:disabled').count() === 4 && postsFor('ui-alpha', 'review-session').length === 1 && postsFor('ui-beta', 'review-session').length === 1, '审查修复：旧 Session 迟到成功不改变新 Session 状态，POST 路径隔离')
  betaSubmission.release()
  await waitSubmitted()
  await resolveReviewPermission('review-session')
  await switchReviewSession('UI 重构演示', '实现完成')
  const staleGeneration = { hold: true, status: 500 }
  permissionFixtures.set('ui-alpha:review-generation', staleGeneration)
  await requestReviewPermission('review-generation')
  await decisions.nth(1).click()
  await waitForFixture(() => staleGeneration.release)
  await switchReviewSession('历史会话', '历史记录')
  await switchReviewSession('UI 重构演示', '实现完成')
  const latestGeneration = { hold: true }
  permissionFixtures.set('ui-alpha:review-generation', latestGeneration)
  await requestReviewPermission('review-generation')
  await decisions.nth(2).click()
  await waitForFixture(() => latestGeneration.release)
  const staleGenerationResponse = waitPermissionResponse('ui-alpha', 'review-generation')
  staleGeneration.release()
  await finishPermissionResponse(staleGenerationResponse)
  check((await feedback.innerText()) === '处理中…' && await page.locator('.permission-actions button:disabled').count() === 4 && await page.locator('.error-banner').count() === 0, '审查修复：切回同一 Session 后相同 request_id 的旧代次响应仍被隔离')
  latestGeneration.release()
  await waitSubmitted()
  await resolveReviewPermission('review-generation')
  for (const [kind, data] of [
    ['permission_resolved', { ...pendingReview('review-expired'), allowed: false, expired: true }],
    ['runtime_status', { status: 'stopped', turn_id: 'review-permission' }],
    ['runtime_status', { status: 'error', turn_id: 'review-permission' }],
    ['runtime_expired', { message: '模拟 Sandbox 过期' }],
  ]) {
    const requestId = kind === 'permission_resolved' ? 'review-expired' : 'review-cleanup-' + (data.status || kind)
    await requestReviewPermission(requestId)
    await decisions.nth(1).click()
    await waitSubmitted()
    await emit(kind, data)
    check(await page.locator('.permission-actions').count() === 0 && await feedback.count() === 0, '审查修复：' + requestId + ' 清理提交状态与操作按钮')
  }
  await requestReviewPermission('review-after-cleanup')
  check(await page.locator('.permission-actions button:enabled').count() === 4, '审查修复：过期、停止和 Runtime error 后的新权限可正常操作')
  await decisions.nth(1).click()
  await waitSubmitted()
  await resolveReviewPermission('review-after-cleanup')
  await emit('runtime_status', { status: 'idle', turn_id: 'review-permission' })
  // Empty bootstrap -> new Session -> asynchronously loaded long Markdown history.
  sessions.splice(0)
  nextCreatedHistory = [{ id: 1, kind: 'assistant', content: '## 新会话历史\n\n' + '初始化长历史内容。\n\n'.repeat(240), data: { turn_id: 'created-history' } }]
  await page.reload()
  await page.locator('.session-empty-state').waitFor()
  check(await input.count() === 0 && await history.count() === 0 && await page.evaluate(() => window.__uiTest.scrollObservers.filter((observer) => observer.__active).length === 0), '无 Session 状态移除输入节点并断开聊天 Observer')
  await page.locator('.session-empty-state').getByRole('button', { name: 'New Session', exact: true }).click()
  await page.getByRole('heading', { name: '新会话历史' }).waitFor()
  await bottom()
  check((await position('无会话创建后长历史')).top > 1000 && !await jump.isVisible() && await page.evaluate(() => window.__uiTest.scrollObservers.filter((observer) => observer.__active).length === 1), '从无 Session 创建后重建唯一 Observer，长历史默认实际定位底部')
  check(errors.length === 0, '浏览器无运行时异常')
  return { count: checks.length, checks, browserErrors: errors, apiRequests: requests.length, imageRequests, composerHeights, scrollSamples, shortStreamingPerformance, markdownPerformance, expandedGeometry, collapsedGeometry, expandedWorkbench, collapsedWorkbench, narrowWorkbench }
  } catch (error) {
    throw new Error(`UI regression failed after "${checks.at(-1) || 'bootstrap'}": ${error.stack || error.message}`)
  }
}
