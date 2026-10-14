import assert from "node:assert/strict"
import test from "node:test"
import { buildExecutionGroups } from "../src/execution.js"

const event = (id, type, payload = {}, turn = "a") => ({ id, kind: "tool", content: type, data: { turn_id: turn, event_type: type, event: { type, ...payload } } })
const call = (id, toolId, name = "read_file", turn = "a") => event(id, "tool_call", { tool_call: { ...(toolId === undefined ? {} : { id: toolId }), name, arguments: { path: "README.md" } } }, turn)
const result = (id, toolId, ok = true, turn = "a") => event(id, "tool_result", { tool_result: { ...(toolId === undefined ? {} : { tool_call_id: toolId }), ok, content: "output", error: ok === false ? "permission denied" : null } }, turn)
const permission = (id, data) => ({ id, kind: "permission", content: "真实请求说明", data: { turn_id: "a", request_id: "p", ...data } })
const group = (events, { status = "idle", expanded = {}, pending = null } = {}) => buildExecutionGroups(events, null, pending,
  { active_turn_id: status === "idle" ? null : "a", runtime_status: status }, { a: { status } }, expanded)[0]

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
    [call(1), event(2, "progress"), result(3)],
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
  assert.equal(value.stepCount, 2)
  value = group([...events, permission(4, { decision: "task" })], { pending })
  assert.equal(value.actionablePermission, null)
  assert.equal(value.timeline[0].status, "allowed")
  assert.equal(value.timeline.at(-1).type, "permission_resolved")
  assert.equal(value.stepCount, 2)
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
