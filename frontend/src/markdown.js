import MarkdownIt from "markdown-it"
import DOMPurify from "dompurify"
import hljs from "highlight.js/lib/core"
import bash from "highlight.js/lib/languages/bash"
import css from "highlight.js/lib/languages/css"
import javascript from "highlight.js/lib/languages/javascript"
import json from "highlight.js/lib/languages/json"
import powershell from "highlight.js/lib/languages/powershell"
import python from "highlight.js/lib/languages/python"
import sql from "highlight.js/lib/languages/sql"
import typescript from "highlight.js/lib/languages/typescript"
import xml from "highlight.js/lib/languages/xml"
import yaml from "highlight.js/lib/languages/yaml"

// Keep the browser bundle small; other languages fall back to escaped text.
for (const [name, grammar] of Object.entries({ bash, css, javascript, json, powershell, python, sql, typescript, xml, yaml })) {
  hljs.registerLanguage(name, grammar)
}

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: false })
const defaultValidateLink = markdown.validateLink.bind(markdown)
markdown.validateLink = (url) => {
  if (!defaultValidateLink(url) || /[\u0000-\u0020\\]/.test(url) || url.startsWith("//")) return false
  const scheme = /^([a-z][a-z\d+.-]*):/i.exec(url)
  return !scheme || /^(https?|mailto)$/i.test(scheme[1])
}
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet("rel", "noopener noreferrer")
  return renderer.renderToken(tokens, index, options)
}
// Model output must not trigger image requests. Keep only the escaped caption.
markdown.renderer.rules.image = (tokens, index) => `<span class="markdown-image-placeholder">[图片：${markdown.utils.escapeHtml(tokens[index].content || "无说明")}]</span>`

function renderCode(content, language = "") {
  let code = markdown.utils.escapeHtml(content)
  if (language && hljs.getLanguage(language)) {
    try {
      code = hljs.highlight(content, { language, ignoreIllegals: true }).value
    } catch (_) { /* Incomplete streamed code must remain readable. */ }
  }
  const label = markdown.utils.escapeHtml(language || "text")
  return `<div class="markdown-code-block"><div class="code-block-toolbar"><span>${label}</span><button type="button" class="code-copy-button" data-copy-code aria-label="复制代码" aria-live="polite">复制</button></div><pre><code class="hljs">${code}</code></pre></div>\n`
}

markdown.renderer.rules.fence = (tokens, index) => {
  const token = tokens[index]
  const language = token.info.trim().split(/\s+/)[0].toLowerCase()
  return renderCode(token.content, language)
}
markdown.renderer.rules.code_block = (tokens, index) => renderCode(tokens[index].content)
markdown.renderer.rules.table_open = () => '<div class="markdown-table-scroll" role="region" aria-label="Markdown 表格" tabindex="0"><table>\n'
markdown.renderer.rules.table_close = () => "</table></div>\n"

const sanitizeOptions = {
  ALLOWED_TAGS: ["p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "s", "ul", "ol", "li", "blockquote", "hr", "a", "pre", "code", "span", "div", "button", "table", "thead", "tbody", "tr", "th", "td"],
  ALLOWED_ATTR: ["class", "href", "title", "rel", "start", "type", "role", "tabindex", "data-copy-code"],
  ALLOW_DATA_ATTR: false,
}

export function renderMarkdown(content) {
  // This is the only HTML accepted by MessageContent's v-html. Sanitize last,
  // including our toolbar markup and highlighter output, with no later HTML edits.
  return DOMPurify.sanitize(markdown.render(content), sanitizeOptions)
}
