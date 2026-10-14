// Run from frontend/: node tests/bundle-report.mjs
// Compare with: node tests/bundle-report.mjs --without-markdown
// Builds stay in memory. The comparison substitutes an empty Markdown renderer
// only for size measurement; it never writes or changes the production build.
// Module sizes are tree-shaken bytes BEFORE chunk minification, not gzip costs.
import { build } from 'vite'
import { gzipSync } from 'node:zlib'

const result = await build({ logLevel: 'silent', build: { write: false }, plugins: process.argv.includes('--without-markdown') ? [{ name: 'measure-markdown-cost', enforce: 'pre', load(id) { if (id.endsWith('/src/markdown.js')) return 'export const renderMarkdown = () => ""' } }] : [] })
const output = result.output
const groups = {}
for (const chunk of output.filter((item) => item.type === 'chunk')) {
  for (const [id, module] of Object.entries(chunk.modules)) {
    const name = id.includes('/highlight.js/') ? 'highlight.js' : id.includes('/dompurify/') ? 'DOMPurify' : /\/(markdown-it|linkify-it|mdurl|uc\.micro)\//.test(id) ? 'Markdown parser' : id.includes('/@xterm/') ? 'xterm' : id.includes('/@vue/') || id.includes('/vue/') ? 'Vue' : 'app and other'
    groups[name] = (groups[name] || 0) + module.renderedLength
  }
}
console.log(JSON.stringify({ chunks: output.filter((item) => item.type === 'chunk').map((chunk) => ({ file: chunk.fileName, bytes: Buffer.byteLength(chunk.code), gzipBytes: gzipSync(chunk.code).length })), preMinifiedModuleBytes: groups }, null, 2))
