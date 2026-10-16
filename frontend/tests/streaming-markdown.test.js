import assert from "node:assert/strict"
import test from "node:test"
import { effectScope, nextTick, reactive } from "vue"
import { useStreamingMarkdown } from "../src/useStreamingMarkdown.js"

function fixture(t, content = "start", streaming = true, renderCostMs = 0) {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 })
  t.mock.method(performance, "now", () => Date.now())
  const props = reactive({ content, streaming })
  const renders = []
  const scope = effectScope()
  const html = scope.run(() => useStreamingMarkdown(props, (source) => {
    renders.push({ source, at: Date.now() })
    if (renderCostMs) t.mock.timers.tick(renderCostMs)
    return `rendered:${source}`
  }))
  t.after(() => scope.stop())
  const tick = async (ms) => { t.mock.timers.tick(ms); await nextTick() }
  return { props, renders, scope, html, tick }
}

test("ordinary streaming text renders at fixed 60ms deadlines under continuous updates", async (t) => {
  const { props, renders, html, tick } = fixture(t)
  for (let i = 1; i <= 100; i += 1) {
    props.content = `snapshot-${i}`
    await nextTick()
    await tick(5)
  }
  assert.deepEqual(renders.map((render) => render.at), [0, 60, 120, 180, 240, 300, 360, 420, 480])
  await tick(40)
  assert.equal(html.value, "rendered:snapshot-100")
  assert.equal(renders.at(-1).at, 540)
  assert.equal(renders.length, 10)
})

test("16K threshold has no second throttle and each commit uses the latest complete snapshot", async (t) => {
  const { props, renders, tick } = fixture(t, "x".repeat(15999))
  props.content += "a"
  await nextTick()
  await tick(20)
  props.content += "b".repeat(10000)
  await nextTick()
  await tick(39)
  assert.equal(renders.length, 1)
  await tick(1)
  assert.equal(renders.length, 2)
  assert.equal(renders.at(-1).source, props.content)
})

test("expensive rendering adapts the single throttle while completion still bypasses it", async (t) => {
  const { props, renders, html, tick } = fixture(t, "long initial", true, 40)
  props.content = "long pending"
  await nextTick()
  await tick(60)
  assert.equal(renders.length, 1)
  await tick(20)
  assert.equal(renders.length, 2)
  assert.equal(renders.at(-1).at, 120)
  props.content = "long final tail"
  props.streaming = false
  await nextTick()
  assert.equal(html.value, "rendered:long final tail")
})

test("completion and stop immediately flush final content and cancel trailing work", async (t) => {
  const { props, html, renders, tick } = fixture(t)
  props.content = "pending snapshot"
  await nextTick()
  await tick(5)
  props.content = "final **complete** tail"
  props.streaming = false
  await nextTick()
  assert.equal(html.value, "rendered:final **complete** tail")
  assert.equal(renders.at(-1).at, 5)
  await tick(200)
  assert.equal(renders.length, 2)
  // Stopping may only change streaming, with no additional content snapshot.
  props.streaming = true
  props.content = "stopped tail"
  await nextTick()
  props.streaming = false
  await nextTick()
  assert.equal(html.value, "rendered:stopped tail")
  const count = renders.length
  await tick(200)
  assert.equal(renders.length, count)
})

test("non-streaming edits and the first snapshot after an idle interval render immediately", async (t) => {
  const { props, html, tick } = fixture(t, "old", false)
  props.content = "stable edit"
  await nextTick()
  assert.equal(html.value, "rendered:stable edit")
  await tick(100)
  props.streaming = true
  props.content = "new live"
  await nextTick()
  assert.equal(html.value, "rendered:new live")
})

test("unmount/session disposal cancels pending renders and a new scope starts clean", async (t) => {
  const { props, html, renders, scope, tick } = fixture(t)
  props.content = "old pending session"
  await nextTick()
  scope.stop()
  const newScope = effectScope()
  const nextProps = reactive({ content: "new session", streaming: true })
  const nextHtml = newScope.run(() => useStreamingMarkdown(nextProps, (source) => source))
  try {
    await tick(100)
    assert.equal(renders.length, 1)
    assert.equal(html.value, "rendered:start")
    assert.equal(nextHtml.value, "new session")
    props.content = "late old update"
    await nextTick()
    await tick(100)
    assert.equal(renders.length, 1)
  } finally { newScope.stop() }
})
