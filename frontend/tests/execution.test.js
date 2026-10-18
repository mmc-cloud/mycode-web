import assert from "node:assert/strict"
import test from "node:test"
import { nextTick, ref } from "vue"
import { buildExecutionGroups, useExecutionGroups } from "../src/execution.js"

const event = (id, type, payload = {}, turn = "a") => ({ id, kind: "tool", content: type, data: { turn_id: turn, event_type: type, event: { type, ...payload } } })
const call = (id, toolId, name = "read_file", turn = "a") => event(id, "tool_call", { tool_call: { ...(toolId === undefined ? {} : { id: toolId }), name, arguments: { path: "README.md" } } }, turn)
const result = (id, toolId, ok = true, turn = "a") => event(id, "tool_result", { tool_result: { ...(toolId === undefined ? {} : { tool_call_id: toolId }), ok, content: "output", error: ok === false ? "permission denied" : null } }, turn)
const stop = (id, reason, turn = "a") => ({ ...event(id, "stop", { stop_reason: reason }, turn), content: `Stopped: ${reason ?? "unknown"}` })
const permission = (id, data) => ({ id, kind: "permission", content: "真实请求说明", data: { turn_id: "a", request_id: "p", ...data } })
const group = (events, { status = "idle", expanded = {}, pending = null } = {}) => buildExecutionGroups(events, null, pending,
  { active_turn_id: status === "idle" ? null : "a", runtime_status: status }, { a: { status } }, expanded)[0]

test("runtime labels suppress only identical previews while retaining events, details and counts", () => {
  const events = [event(1, "turn"), event(2, "model_start"), event(3, "context"),
    event(4, "model_start", { content: "  model_start\n " })]
  const before = JSON.stringify(events)
  const value = group(events)
  assert.deepEqual(value.timeline.map((step) => step.label), ["turn", "model_start", "context", "model_start"])
  assert.deepEqual(value.timeline.map((step) => step.preview), ["", "", "", ""])
  assert.deepEqual(value.timeline.map((step) => step.detail), ["turn", "model_start", "context", "  model_start\n "])
  assert.deepEqual(value.timeline.map((step) => step.sources), events.map((source) => [source]))
  assert.equal(value.stepCount, events.length)
  assert.equal(JSON.stringify(events), before)
})

test("runtime previews retain extra context, errors, differing case and truncated content", () => {
  const longContext = "context " + "budget ".repeat(30)
  const events = [event(1, "context", { content: "Input tokens: 1200 / 8000" }),
    event(2, "model_start", { content: "model_start: provider/model" }),
    event(3, "turn", { content: "Turn" }),
    event(4, "model_retry", { error: "Retry after timeout", content: "model_retry" }),
    event(5, "context", { content: longContext })]
  const value = group(events)
  assert.deepEqual(value.timeline.map((step) => step.preview), ["Input tokens: 1200 / 8000", "model_start: provider/model", "Turn", "Retry after timeout", longContext.slice(0, 119) + "…"])
  assert.equal(value.timeline.at(-1).detail, longContext)
  assert.deepEqual(value.timeline.map((step) => step.sources), events.map((source) => [source]))
  assert.equal(value.stepCount, events.length)
})

test("explicit IDs pair interleaved results at call positions and preserve all available source data", () => {
  const events = [call(1, "one"), event(2, "context", { content: "budget" }), call(3, "two", "run_command"), result(4, "two", false), result(5, "one")]
  const value = group(events)
  assert.deepEqual(value.timeline.map((step) => step.id), ["event-1", "event-2", "event-3"])
  assert.deepEqual(value.timeline.map((step) => step.status), ["success", "info", "failed"])
  assert.deepEqual(value.timeline[0].sources, [events[0], events[4]])
  assert.match(value.timeline[2].error, /permission denied/)
  assert.equal(value.stepCount, 5)
  assert.equal(value.status, "completed")
  assert.equal(value.expanded, true)
})

test("current id-less result format pairs only adjacent, unique, gap-free calls", () => {
  const events = [call(1, "one"), result(2), call(3), result(4)]
  assert.deepEqual(group(events).timeline.map((step) => step.status), ["success", "success"])
  assert.equal(group(events).stepCount, 4)
  for (const input of [
    [call(1), result(3)], // omitted Console event
    [call(1), call(2), result(3), result(4)],
    [result(1), call(2), result(3)],
    [call(1), result(2, "different")],
    [call(1, "truncated..."), result(2)],
    [call(1), event(2, "tool_result", { tool_result: { name: "write_file", ok: true } })],
    [call(1), event(2, "tool_result", { tool_result: "[nested value omitted]" })],
    [event(1, "tool_call", {}), result(2)],
  ]) assert.equal(group(input).timeline.length, input.length)
})

test("duplicate, conflicting, missing and reverse IDs never force a match", () => {
  for (const events of [
    [call(1, "same"), call(2, "same"), result(3, "same")],
    [call(1, "same"), result(2, "same"), result(3, "same")],
    [result(1, "same"), call(2, "same")],
    [call(1, "same"), result(2, "other")],
    [event(1, "tool_call", { tool_call: { id: "same", tool_call_id: "other", name: "read_file" } }), result(2, "same")],
  ]) assert.equal(group(events).timeline.length, events.length)
  const groups = buildExecutionGroups([call(1, "same"), result(2, "same", true, "b")], null, null, null)
  assert.equal(groups.length, 2)
  assert.equal(groups[0].timeline[0].status, "unknown")
  assert.equal(groups[1].timeline[0].sources.length, 1)
})

test("only literal ok true or false produces a terminal tool status", () => {
  for (const ok of [null, undefined, "true", "false", 1, 0]) {
    assert.equal(group([call(1), event(2, "tool_result", { tool_result: { ok, content: "All tests passed" } })]).timeline[0].status, "unknown")
  }
  assert.equal(group([call(1)], { status: "running" }).timeline[0].status, "running")
  assert.equal(group([call(1)]).timeline[0].status, "unknown")
  assert.equal(group([result(1, undefined, false)]).timeline[0].status, "failed")
  assert.equal(group([event(1, "tool_result", { tool_result: { content: "All tests passed" } })]).timeline[0].status, "unknown")
})

test("runtime events and errors keep chronological positions and tool failure does not mean turn error", () => {
  const events = [call(1, "one"), permission(2, {}), permission(3, { decision: "once" }), result(4, "one", false),
    event(5, "model_retry"), { id: 6, kind: "error", content: "runtime broke", data: { turn_id: "a" } }, event(7, "stop")]
  assert.deepEqual(group(events).timeline.map((step) => step.id), ["event-1", "event-2", "event-3", "event-5", "event-6", "event-7"])
  assert.equal(group(events).status, "error")
  assert.equal(group(events).stepCount, 5) // four tool/runtime events + one permission lifecycle
  assert.equal(group([call(1), result(2, undefined, false)]).status, "completed")
  assert.equal(group([call(1)], { status: "stopped" }).status, "stopped")
})

test("permission lifecycle retains resolution order and exposes exactly one actionable request", () => {
  const pending = { turn_id: "a", request_id: "p", tool_name: "write_file", target: "README.md" }
  const events = [permission(1, {}), event(2, "progress"), permission(3, {})]
  let value = group(events, { status: "waiting_permission", pending, expanded: { a: false } })
  assert.equal(value.expanded, false)
  assert.equal(value.timeline.filter((step) => step.actionable).length, 1)
  assert.equal(value.actionablePermission.id, "event-3")
  assert.equal(value.stepCount, 1) // the permission lifecycle; progress is hidden
  value = group([...events, permission(4, { decision: "task" })], { pending })
  assert.equal(value.actionablePermission, null)
  assert.equal(value.timeline[0].status, "allowed")
  assert.equal(value.timeline.at(-1).type, "permission_resolved")
  assert.equal(value.stepCount, 1)
  assert.equal(group([permission(1, {})]).timeline[0].status, "unknown")
  assert.equal(group([permission(1, { allowed: false, expired: true })]).timeline[0].status, "expired")
  const synthetic = group([], { status: "waiting_permission", pending })
  assert.equal(synthetic.actionablePermission.status, "waiting")
  assert.equal(synthetic.stepCount, 1)
})

test("folding defaults follow real state while explicit user choices always win", () => {
  assert.equal(group([call(1)], { status: "running" }).expanded, true)
  assert.equal(group([call(1), result(2)]).expanded, false)
  assert.equal(group([call(1), result(2, undefined, false)]).expanded, true)
  assert.equal(group([call(1), result(2, undefined, false)], { expanded: { a: false } }).expanded, false)
  assert.equal(group([call(1), result(2)], { expanded: { a: true } }).expanded, true)
  assert.equal(group([call(1)], { status: "waiting_permission" }).status, "waiting")
  assert.equal(group([call(1)], { status: "waiting_mcp_trust" }).statusLabel, "Waiting for MCP trust")
})

test("replayed/upserted SSE and history have the same timeline without mutating inputs", () => {
  const events = [call(1, "one"), result(2, "one"), event(3, "artifact_warning", { content: "<img onerror=alert(1)>" })]
  const before = JSON.stringify(events)
  const expected = group(events)
  assert.deepEqual(group([events[2], events[0], events[1], events[1]]), expected)
  assert.equal(JSON.stringify(events), before)
  assert.equal(expected.timeline.at(-1).detail, "<img onerror=alert(1)>")
})

const message = (id, kind, content = `${kind}-${id}`) => ({ id, kind, content, data: { turn_id: "a" } })
const blocks = (value) => value.items.filter((item) => item.kind === "execution").map((item) => item.block)

test("session warnings remain independent of the previous completed execution, including legacy history", () => {
  const base = [message(1, "user"), call(2, "read"), result(3, "read"), message(4, "assistant", "final answer")]
  const warnings = [
    { code: "runtime_cleanup_failed", message: "cleanup warning" },
    { event_type: "runtime_warning", scope: "session", message: "new warning" },
    { code: "runtime_cleanup_failed", turn_id: "a" }, // Old cleanup rows could carry a Turn ID.
    { event_type: "runtime_warning", scope: "session", turn_id: "a" },
  ]
  for (const data of warnings) {
    const warning = { id: 5, kind: "tool", content: "session warning", data }
    const events = [...base, warning]
    const groups = buildExecutionGroups(events, null, null, { active_turn_id: null, runtime_status: "idle" })
    assert.equal(groups.length, 2)
    assert.deepEqual(groups[0], group(base))
    assert.equal(blocks(groups[0])[0].blockCount, 1)
    assert.equal(groups[1].items[0].kind, "notice")
    assert.equal(groups[1].items[0].content, warning.content)
    assert.deepEqual(groups[1].items[0].sources, [warning])
    assert.equal(blocks(groups[1]).length, 0)
    assert.deepEqual(buildExecutionGroups([...events].reverse().concat(warning), null, null,
      { active_turn_id: null, runtime_status: "idle" }), groups)
  }
})

test("turn-scoped warnings remain in the matching execution and session notices do not absorb later turns", () => {
  const events = [message(1, "user"), call(2, "one"), result(3, "one"), message(4, "assistant"),
    { id: 5, kind: "tool", content: "session warning", data: { scope: "session" } },
    { ...message(6, "user"), data: { turn_id: "b" } },
    call(7, "two", "read_file", "b"), result(8, "two", true, "b"),
    { id: 9, kind: "tool", content: "turn warning", data: { turn_id: "b", scope: "turn", event_type: "runtime_warning" } }]
  const groups = buildExecutionGroups(events, null, null, null)
  assert.deepEqual(groups.map((value) => value.key), ["a", "runtime:5", "b"])
  assert.equal(blocks(groups[0])[0].blockCount, 1)
  assert.equal(groups[1].items[0].kind, "notice")
  assert.equal(groups[2].timeline.at(-1).detail, "turn warning")
  assert.equal(groups[2].stepCount, 3)
})

test("normal completion bookkeeping after the final assistant creates no extra block", () => {
  const events = [call(1, "read"), result(2, "read"), message(3, "assistant", "final answer"),
    event(4, "progress"), stop(5, "final_answer")]
  const original = JSON.stringify(events)
  const value = group(events)
  assert.deepEqual(value.items.map((item) => item.kind), ["execution", "assistant"])
  assert.deepEqual(value.timeline.map((step) => step.id), ["event-1"])
  assert.equal(value.stepCount, 2)
  assert.equal(value.status, "completed")
  assert.equal(blocks(value).length, 1)
  assert.equal(blocks(value)[0].stepCount, 2)
  assert.equal(blocks(value)[0].blockCount, 1)
  assert.equal(value.items.at(-1).content, "final answer")
  assert.deepEqual(value.events, events)
  assert.equal(value.events[3], events[3])
  assert.equal(JSON.stringify(events), original)
})

test("progress and final_answer stop add no visible steps or blocks even without tools", () => {
  const progress = event(1, "progress")
  delete progress.data.event_type // also accept the structured payload type
  for (const events of [[progress], [stop(2, "final_answer")], [progress, stop(2, "final_answer")]]) {
    const value = group(events)
    assert.equal(value.stepCount, 0)
    assert.deepEqual(value.timeline, [])
    assert.deepEqual(value.items, [])
    assert.deepEqual(value.events, events)
    const answerOnly = group([message(0, "assistant"), ...events])
    assert.deepEqual(answerOnly.items.map((item) => item.kind), ["assistant"])
    assert.equal(answerOnly.stepCount, 0)
  }
})

test("hidden events do not change conservative ID-less pairing or erase raw event gaps", () => {
  for (const hidden of [event(2, "progress"), stop(2, "final_answer")]) {
    const events = [call(1), hidden, result(3)]
    const value = group(events)
    assert.deepEqual(value.timeline.map((step) => step.id), ["event-1", "event-3"])
    assert.deepEqual(value.timeline.map((step) => step.status), ["unknown", "success"])
    assert.deepEqual(value.timeline.map((step) => step.sources.length), [1, 1])
    assert.equal(value.timeline[0].association, null)
    assert.equal(value.stepCount, 2)
    assert.equal(value.events[1], hidden)
  }
  // An actual adjacent pair still associates with hidden events elsewhere.
  const value = group([event(1, "progress"), call(2), result(3), stop(4, "final_answer")])
  assert.equal(value.timeline.length, 1)
  assert.equal(value.timeline[0].sources.length, 2)
  assert.equal(value.stepCount, 2)
})

test("real multi-block execution keeps message boundaries, numbering and independent folds", () => {
  const events = [message(1, "assistant"), call(2, "read"), result(3, "read"),
    message(4, "assistant"), event(5, "progress"), call(6, "edit", "edit_file"), result(7, "edit"),
    message(8, "assistant"), event(9, "progress"), stop(10, "final_answer")]
  const value = group(events, { expanded: { a: true, "a:after-message-4": false } })
  assert.deepEqual(value.items.map((item) => item.kind), ["assistant", "execution", "assistant", "execution", "assistant"])
  assert.deepEqual(value.items.filter((item) => item.kind === "assistant").map((item) => item.eventId), [1, 4, 8])
  assert.deepEqual(blocks(value).map((block) => block.timeline.map((step) => step.id)), [["event-2"], ["event-6"]])
  assert.deepEqual(blocks(value).map((block) => [block.blockNumber, block.blockCount, block.stepCount, block.expanded]),
    [[1, 2, 2, true], [2, 2, 2, false]])
  assert.equal(value.stepCount, 4)
})

test("abnormal and unspecified stop reasons remain visible with their original details", () => {
  for (const reason of ["max_turns", "model_error", "context_overflow", "cancelled", "unknown", undefined, null]) {
    const source = stop(2, reason)
    const value = group([message(1, "assistant"), source])
    assert.deepEqual(value.items.map((item) => item.kind), ["assistant", "execution"])
    assert.equal(value.timeline[0].type, "stop")
    assert.equal(value.timeline[0].detail, source.content)
    assert.equal(value.timeline[0].sources[0], source)
    assert.equal(value.stepCount, 1)
    assert.equal(blocks(value)[0].stepCount, 1)
  }
})

test("permissions, failures, warnings, errors and reliable cross-message pairs survive filtering", () => {
  const pending = { turn_id: "a", request_id: "p" }
  const events = [call(1, "read"), event(2, "progress"), message(3, "assistant"), result(4, "read", false),
    permission(5, {}), stop(6, "final_answer"), permission(7, { decision: "once" }),
    event(8, "artifact_warning"), event(9, "model_retry"),
    { id: 10, kind: "error", content: "Runtime error", data: { turn_id: "a" } },
    message(11, "assistant"), event(12, "progress"), stop(13, "final_answer")]
  const value = group(events, { pending })
  assert.deepEqual(value.timeline.map((step) => step.id), ["event-1", "event-5", "event-7", "event-8", "event-9", "event-10"])
  assert.deepEqual(value.timeline[0].sources, [events[0], events[3]])
  assert.equal(value.timeline[0].status, "failed")
  assert.equal(value.timeline[1].status, "allowed")
  assert.equal(value.actionablePermission, null)
  assert.equal(value.status, "error")
  assert.equal(value.hasToolFailure, true)
  assert.equal(value.stepCount, 5) // call + result + two warnings + one permission lifecycle
  assert.deepEqual(blocks(value).map((block) => block.stepCount), [2, 3])
  assert.equal(value.items.at(-1).eventId, 11)
})

test("SSE prefixes, redelivery and restored history agree after normal completion filtering", () => {
  const events = [message(1, "user"), call(2, "read"), result(3, "read"), message(4, "assistant"),
    event(5, "progress"), stop(6, "final_answer"), call(7, "other", "write_file", "b"),
    stop(8, "max_turns", "b")]
  const input = reactiveGroups([])
  input.live.value = null
  input.session.value = { id: "s", active_turn_id: null, runtime_status: "idle" }
  const replay = []
  for (const source of events) {
    replay.push(source, source)
    input.events.value.push(source, source)
    const restored = buildExecutionGroups([...events.filter((event) => event.id <= source.id)].reverse(), null, null, null, {}, { a: false })
    assert.deepEqual(input.groups.value, restored)
    assert.deepEqual(buildExecutionGroups(replay, null, null, null, {}, { a: false }), restored)
  }
  const final = input.groups.value
  assert.deepEqual(final.map((value) => value.key), ["a", "b"])
  assert.deepEqual(final[0].items.map((item) => item.kind), ["user", "execution", "assistant"])
  assert.equal(final[0].stepCount, 2)
  assert.deepEqual(final[0].events, events.slice(0, 6))
  assert.equal(final[1].timeline.at(-1).type, "stop")
})

test("user, assistant and execution alternate at durable event positions", () => {
  const value = group([message(1, "user"), message(2, "assistant"), call(3, "read"), result(4, "read"), message(5, "assistant")])
  assert.deepEqual(value.items.map((item) => item.kind), ["user", "assistant", "execution", "assistant"])
  assert.deepEqual(value.items.filter((item) => item.kind !== "execution").map((item) => item.eventId), [1, 2, 5])
  assert.equal(blocks(value)[0].timeline.length, 1)
  assert.equal(blocks(value)[0].stepCount, 2)
})

test("multiple blocks use independent folds and count each original step once", () => {
  const events = [call(1, "one"), result(2, "one"), message(3, "assistant"), call(4, "two"), result(5, "two", false), message(6, "assistant")]
  let value = group(events)
  const [first, second] = blocks(value)
  assert.deepEqual(value.items.map((item) => item.kind), ["execution", "assistant", "execution", "assistant"])
  assert.equal(first.expanded, false)
  assert.equal(second.expanded, true)
  assert.equal(first.blockNumber, 1)
  assert.equal(second.blockNumber, 2)
  assert.equal(first.blockCount, 2)
  assert.equal(blocks(value).reduce((sum, block) => sum + block.stepCount, 0), value.stepCount)
  assert.match(first.summary, /^4 steps/)
  assert.match(second.summary, /^2 steps.*同一轮执行/)
  value = group(events, { status: "running", expanded: { [first.key]: true, [second.key]: false } })
  assert.deepEqual(blocks(value).map((block) => block.expanded), [true, false])
  value = group(events, { expanded: { [first.key]: true, [second.key]: false } })
  assert.deepEqual(blocks(value).map((block) => block.expanded), [true, false])
})

test("explicit correlation across an assistant message keeps only the call anchor", () => {
  const value = group([call(1, "one"), message(2, "assistant"), result(3, "one"), call(4, "two"), message(5, "assistant")])
  assert.deepEqual(value.items.map((item) => item.kind), ["execution", "assistant", "execution", "assistant"])
  assert.deepEqual(blocks(value).map((block) => block.timeline.map((step) => step.id)), [["event-1"], ["event-4"]])
  assert.deepEqual(blocks(value).map((block) => block.stepCount), [2, 1])
  assert.equal(blocks(value)[0].timeline[0].status, "success")
  const unmatched = group([call(1), message(2, "assistant"), result(3)])
  assert.deepEqual(unmatched.items.map((item) => item.kind), ["execution", "assistant", "execution"])
  assert.equal(blocks(unmatched)[0].timeline[0].status, "unknown")
})

test("a permission lifecycle split by messages counts once and belongs to the actionable block", () => {
  const pending = { turn_id: "a", request_id: "p", tool_name: "write_file" }
  const events = [call(1), message(2, "assistant"), permission(3, {})]
  const value = group(events, { status: "waiting_permission", pending, expanded: { "a:after-message-2": false } })
  assert.equal(blocks(value)[0].actionablePermission, null)
  assert.equal(blocks(value)[1].expanded, false)
  assert.equal(blocks(value)[1].actionablePermission.requestId, "p")
  const resolved = group([...events, message(4, "assistant"), permission(5, { decision: "once" })])
  assert.deepEqual(blocks(resolved).map((block) => block.stepCount), [1, 1, 0])
  assert.equal(resolved.stepCount, 2)
})

test("live output remains last and becoming durable preserves folding identity", () => {
  const events = [call(1), message(2, "assistant")]
  const session = { runtime_status: "running", active_turn_id: "a" }
  const live = { active: true, kind: "tool", turn_id: "a", content: "plain live log" }
  let value = buildExecutionGroups(events, live, null, session)[0]
  const lastBlock = blocks(value).at(-1)
  assert.equal(lastBlock.live.content, live.content)
  value = buildExecutionGroups([...events, call(3)], null, null, session, {}, { [lastBlock.key]: false })[0]
  assert.equal(blocks(value).at(-1).key, lastBlock.key)
  assert.equal(blocks(value).at(-1).expanded, false)
  value = buildExecutionGroups(events, { ...live, kind: "assistant" }, null, session)[0]
  assert.equal(value.items.at(-1).streaming, true)
  assert.equal(value.items.at(-1).kind, "assistant")
  value = group([...events, message(3, "assistant", live.content)])
  assert.equal(value.items.filter((item) => item.content === live.content).length, 1)
})

test("incremental/replayed events and restored history produce the same ordered items", () => {
  const events = [message(1, "user"), call(2, "one"), message(3, "assistant"), result(4, "one"), call(5), message(6, "assistant")]
  let replay = []
  for (const event of events) {
    replay.push(event, event)
    assert.deepEqual(group(replay).items, group(events.filter((item) => item.id <= event.id)).items)
  }
  assert.deepEqual(group([...events].reverse()).items, group(events).items)
  const revised = { ...events[2], content: "updated assistant" }
  assert.equal(group([...events, revised]).items.filter((item) => item.eventId === 3).length, 1)
  assert.equal(group([...events, revised]).items.find((item) => item.eventId === 3).content, revised.content)
})

function reactiveGroups(events = [message(1, "user"), call(2, "one"), result(3, "one")]) {
  const input = {
    events: ref(events), live: ref({ active: true, kind: "assistant", turn_id: "a", content: "start" }),
    pendingPermission: ref(null), session: ref({ id: "session-1", active_turn_id: "a", runtime_status: "running" }),
    states: ref({}), expanded: ref({ a: false }),
  }
  return { ...input, groups: useExecutionGroups(input) }
}

test("high-frequency assistant snapshots reuse historical projection and execution block props", async () => {
  let sourceReads = 0
  const source = call(2, "one")
  Object.defineProperty(source, "id", { get() { sourceReads += 1; return 2 }, enumerable: true })
  const input = reactiveGroups([message(1, "user"), source, result(3, "one")])
  const initial = input.groups.value[0]
  const reads = sourceReads
  for (let i = 0; i < 100; i += 1) {
    input.live.value = { ...input.live.value, content: `snapshot-${i}` }
    await nextTick()
    const updated = input.groups.value[0]
    assert.equal(updated.items.at(-1).content, `snapshot-${i}`)
    assert.equal(updated.timeline, initial.timeline)
    assert.equal(updated.items[0], initial.items[0])
    assert.equal(blocks(updated)[0], blocks(initial)[0])
    assert.equal(blocks(updated)[0].expanded, false)
  }
  // Every full projection reads source IDs; the real composable must read none.
  assert.equal(sourceReads, reads)
})

test("history, runtime, permission, folds and live boundaries still invalidate projection", () => {
  const input = reactiveGroups()
  const previous = input.groups.value[0].timeline
  input.expanded.value.a = true
  assert.notEqual(input.groups.value[0].timeline, previous)
  assert.equal(blocks(input.groups.value[0])[0].expanded, true)
  input.events.value.push(call(4, "two"))
  assert.equal(input.groups.value[0].timeline.at(-1).status, "running")
  input.states.value.a = { status: "stopped" }
  assert.equal(input.groups.value[0].status, "stopped")
  assert.equal(input.groups.value[0].timeline.at(-1).status, "unknown")
  input.states.value.a.status = "waiting_permission"
  input.pendingPermission.value = { turn_id: "a", request_id: "p", tool_name: "write_file" }
  assert.equal(input.groups.value[0].actionablePermission.requestId, "p")
  input.events.value.push(permission(5, { decision: "once" }))
  assert.equal(input.groups.value[0].actionablePermission, null)
  input.pendingPermission.value = null
  input.live.value = { active: true, kind: "tool", turn_id: "a", content: "tool log" }
  assert.equal(blocks(input.groups.value[0]).at(-1).live.content, "tool log")
  input.live.value = { ...input.live.value, content: "updated tool log" }
  assert.equal(blocks(input.groups.value[0]).at(-1).live.content, "updated tool log")
  input.live.value = { active: true, kind: "assistant", turn_id: "b", content: "other turn" }
  assert.equal(input.groups.value.at(-1).key, "b")
  assert.equal(input.groups.value.at(-1).items.at(-1).content, "other turn")
  input.live.value = null
  assert.equal(input.groups.value.length, 1)
  assert.equal(input.groups.value[0].items.some((item) => item.streaming), false)
})

test("reactive Assistant → Tool → Assistant order matches full projection and survives session reset", () => {
  const input = reactiveGroups([message(1, "user")])
  const expected = () => buildExecutionGroups(input.events.value, input.live.value, input.pendingPermission.value,
    input.session.value, input.states.value, input.expanded.value)
  assert.deepEqual(input.groups.value, expected())
  input.live.value = null
  input.events.value.push(message(2, "assistant", "before tool"), call(3, "one"))
  input.live.value = { active: true, kind: "tool", turn_id: "a", content: "running" }
  assert.deepEqual(input.groups.value, expected())
  const blockKey = blocks(input.groups.value[0]).at(-1).key
  input.expanded.value[blockKey] = false
  input.live.value = null
  input.events.value.push(result(4, "one"))
  input.live.value = { active: true, kind: "assistant", turn_id: "a", content: "after tool" }
  assert.deepEqual(input.groups.value, expected())
  assert.deepEqual(input.groups.value[0].items.map((item) => item.kind), ["user", "assistant", "execution", "assistant"])
  assert.equal(blocks(input.groups.value[0]).at(-1).expanded, false)
  input.live.value = null
  input.events.value.push(message(5, "assistant", "final content"))
  input.session.value.runtime_status = "idle"
  input.session.value.active_turn_id = null
  assert.deepEqual(input.groups.value, expected())
  assert.equal(input.groups.value[0].items.at(-1).content, "final content")
  input.events.value = []
  input.states.value = {}
  input.expanded.value = {}
  input.session.value = { id: "session-2", active_turn_id: null, runtime_status: "stopped" }
  assert.deepEqual(input.groups.value, [])
})
