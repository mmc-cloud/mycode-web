// Display projection only: never infer success, test outcomes or file diffs from logs.
const ACTIVE = ["starting", "queued", "running", "waiting_permission", "waiting_mcp_trust"]
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {}
const text = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2)
const preview = (value) => {
  const line = text(value).replace(/\s+/g, " ").trim()
  return line.length > 120 ? `${line.slice(0, 119)}…` : line
}
// The server truncates strings at 16,000 characters. Truncated identifiers are not keys.
const identifier = (value) => typeof value === "string" && value && value.length < 16000 &&
  !value.endsWith("...") && !value.startsWith("[nested value") ? value : null
const payloadFor = (event) => object(event.data?.event)
const typeFor = (event) => event.data?.event_type || payloadFor(event).type || "runtime_event"
const callId = (event) => {
  const call = object(payloadFor(event).tool_call)
  const ids = [call.id, call.tool_call_id].filter((value) => value != null)
  return ids.length && ids.every((value) => identifier(value) && value === ids[0]) ? ids[0] : null
}
const resultId = (event) => identifier(object(payloadFor(event).tool_result).tool_call_id)
const hasId = (event) => typeFor(event) === "tool_call"
  ? object(payloadFor(event).tool_call).id != null || object(payloadFor(event).tool_call).tool_call_id != null
  : object(payloadFor(event).tool_result).tool_call_id != null
const labels = { success: "执行成功", failed: "执行失败", running: "执行中", unknown: "状态未知 / 结果缺失", info: "Info" }
const titles = { read_file: "读取文件", write_file: "写入文件", edit_file: "编辑文件", run_command: "执行命令", glob: "查找文件", grep: "搜索内容" }

function targetFor(call) {
  const args = object(call.arguments)
  for (const key of ["path", "file_path", "command", "pattern", "query", "url"]) {
    if (typeof args[key] === "string" && args[key]) return args[key]
  }
  return ""
}

export function structuredToolStep(event, active, resultEvent = null, association = null) {
  const payload = payloadFor(event)
  const type = typeFor(event)
  const isCall = type === "tool_call"
  const isTool = isCall || type === "tool_result"
  const call = object(payload.tool_call)
  const result = resultEvent ? object(payloadFor(resultEvent).tool_result) : type === "tool_result" ? object(payload.tool_result) : null
  const status = result?.ok === true ? "success" : result?.ok === false ? "failed" :
    isCall && !resultEvent && active ? "running" : isTool ? "unknown" : "info"
  const name = isCall ? call.name : object(payload.tool_result).name
  const detail = result ? [text(result.error), text(result.content)].filter(Boolean).join("\n\n") : text(payload.error || payload.content || event.content)
  return {
    id: `event-${event.id}`, kind: isTool ? "tool" : "runtime", type, status,
    label: isTool ? titles[name] || name || "工具结果（未关联调用）" : type === "runtime_event" ? "运行事件" : type,
    toolName: name || "", target: isCall ? targetFor(call) : "", preview: !isTool ? preview(detail) : "",
    statusLabel: labels[status], error: result?.ok === false ? text(result.error || result.content || event.content) : "",
    arguments: isCall ? text(call.arguments ?? {}) : null,
    result: result ? text(result) : null, detail, association,
    // Keep both bounded source events available, including IDs and metadata.
    sources: resultEvent ? [event, resultEvent] : [event],
  }
}

function toolPairs(events) {
  const calls = new Map(), results = new Map(), pairs = new Map(), consumed = new Set()
  const add = (map, id, index) => { if (id) map.set(id, [...(map.get(id) || []), index]) }
  events.forEach((event, index) => {
    if (event.kind !== "tool") return
    if (typeFor(event) === "tool_call") add(calls, callId(event), index)
    if (typeFor(event) === "tool_result") add(results, resultId(event), index)
  })
  // Explicit, unique IDs can correlate interleaved calls. Never match across turns.
  for (const [id, callIndexes] of calls) {
    const resultIndexes = results.get(id) || []
    if (callIndexes.length !== 1 || resultIndexes.length !== 1) continue
    const c = callIndexes[0], r = resultIndexes[0]
    const call = object(payloadFor(events[c]).tool_call), result = object(payloadFor(events[r]).tool_result)
    if (r <= c || (result.name && result.name !== call.name)) continue
    pairs.set(c, { index: r, association: "明确的 tool_call_id" })
    consumed.add(r)
  }
  const outstanding = new Set()
  let ambiguous = false
  events.forEach((event, index) => {
    if (event.kind !== "tool") return
    const type = typeFor(event)
    if (type === "tool_call") outstanding.add(index)
    if (type !== "tool_result") return
    if (consumed.has(index)) {
      for (const [call, pair] of pairs) if (pair.index === index) outstanding.delete(call)
      return
    }
    const previous = events[index - 1]
    const previousIndex = index - 1
    const result = object(payloadFor(event).tool_result)
    const call = object(payloadFor(previous || {}).tool_call)
    // With no result ID, adjacency alone is insufficient: reject gaps, batches,
    // orphan results, conflicting names/IDs and any earlier unresolved call.
    if (!ambiguous && !hasId(event) && previous?.kind === "tool" && typeFor(previous) === "tool_call" &&
      outstanding.size === 1 && outstanding.has(previousIndex) && !pairs.has(previousIndex) &&
      typeof call.name === "string" && Boolean(call.name) && payloadFor(event).tool_result === result &&
      (!hasId(previous) || callId(previous)) && Number.isInteger(previous.id) && event.id === previous.id + 1 &&
      (!result.name || result.name === call.name)) {
      pairs.set(previousIndex, { index, association: "相邻且唯一的调用 / 结果" })
      consumed.add(index)
      outstanding.delete(previousIndex)
    } else ambiguous = true
  })
  return { pairs, consumed }
}

export function permissionStepFor(event) {
  const data = object(event.data)
  const resolved = data.decision != null || typeof data.allowed === "boolean" || data.expired === true
  const status = data.expired ? "expired" : data.decision === "deny" || data.allowed === false ? "denied" :
    ["once", "task", "session"].includes(data.decision) || data.allowed === true ? "allowed" : resolved ? "unknown" : "waiting"
  return {
    id: `event-${event.id}`, kind: "permission", type: resolved ? "permission_resolved" : "permission_request",
    requestId: identifier(data.request_id), status, actionable: false,
    label: resolved ? status === "allowed" ? "用户已批准" : status === "denied" ? "用户已拒绝" : status === "expired" ? "权限请求已过期" : "权限请求已处理" : "需要操作权限", toolName: text(data.tool_name),
    target: text(data.target || data.action), detail: text(resolved ? event.content : data.prompt || data.reason || event.content),
    statusLabel: { waiting: "Waiting for permission", allowed: "Allowed", denied: "Denied", expired: "Expired", unknown: "Unknown" }[status],
    sources: [event],
  }
}

export function buildExecutionGroups(events, live, pendingPermission, session, states = {}, expanded = {}) {
  const groups = new Map()
  let fallbackTurn = null
  // Console IDs are durable event sequence numbers; SSE redelivery updates one row.
  const unique = new Map()
  for (const event of events) unique.set(event.id, event)
  const ordered = [...unique.values()].sort((a, b) => a.id - b.id)
  const ensure = (key) => {
    if (!groups.has(key)) groups.set(key, { key, events: [], user: null })
    return groups.get(key)
  }
  for (const event of ordered) {
    const key = identifier(event.data?.turn_id) || fallbackTurn || "runtime"
    if (event.kind === "user") fallbackTurn = key
    const group = ensure(key)
    group.events.push(event)
    if (event.kind === "user" && !group.user) group.user = event
  }
  const activeTurn = session?.active_turn_id
  const liveTurn = live?.turn_id || activeTurn
  const pendingTurn = pendingPermission?.turn_id || activeTurn
  if (activeTurn) ensure(activeTurn)
  if (live?.active && liveTurn) ensure(liveTurn)
  if (pendingPermission && pendingTurn) ensure(pendingTurn)

  return [...groups.values()].map((group) => {
    const groupLive = live?.active && liveTurn === group.key ? live : null
    const state = states[group.key]?.status || (group.key === activeTurn ? session?.runtime_status : null)
    const active = ACTIVE.includes(state)
    const { pairs, consumed } = toolPairs(group.events)
    const timeline = [], assistants = [], permissionRequests = new Set()
    let permissionCount = 0, toolCount = 0
    for (const [index, event] of group.events.entries()) {
      if (event.kind === "tool") {
        toolCount += 1
        if (!consumed.has(index)) {
          const pair = pairs.get(index)
          timeline.push(structuredToolStep(event, active, pair ? group.events[pair.index] : null, pair?.association))
        }
      } else if (event.kind === "permission") {
        const step = permissionStepFor(event)
        if (!step.requestId || !permissionRequests.has(step.requestId)) permissionCount += 1
        if (step.requestId) permissionRequests.add(step.requestId)
        timeline.push(step)
      } else if (event.kind === "error") {
        timeline.push({ id: `event-${event.id}`, kind: "error", status: "failed", label: "Runtime error", detail: text(event.content), sources: [event] })
      } else if (event.kind === "assistant") assistants.push(event)
    }
    // Resolution remains at its original position; the request row loses controls.
    for (const step of timeline.filter((item) => item.kind === "permission" && item.type === "permission_request")) {
      const resolutions = timeline.filter((item) => item.kind === "permission" && item.requestId && item.requestId === step.requestId && item.type === "permission_resolved" &&
        item.sources[0].id > step.sources[0].id)
      if (resolutions.length) {
        step.status = resolutions.at(-1).status
        step.statusLabel = `请求已处理 · ${resolutions.at(-1).statusLabel}`
      } else if (!active) {
        step.status = "unknown"
        step.statusLabel = "请求已结束 / 状态未知"
      }
    }
    let actionablePermission = null
    const pendingId = identifier(pendingPermission?.request_id)
    if (pendingPermission && pendingTurn === group.key && pendingId &&
      !timeline.some((item) => item.kind === "permission" && item.requestId === pendingId && item.type === "permission_resolved")) {
      actionablePermission = timeline.findLast((item) => item.kind === "permission" && item.requestId === pendingId)
      if (!actionablePermission) {
        actionablePermission = permissionStepFor({ id: `pending-${pendingId}`, data: pendingPermission, content: "Agent 请求权限" })
        timeline.push(actionablePermission)
        permissionCount += 1
      }
      Object.assign(actionablePermission, { status: "waiting", statusLabel: "Waiting for permission", actionable: true })
    }
    const hasRuntimeError = timeline.some((item) => item.kind === "error") || state === "error" || groupLive?.kind === "error"
    const hasToolFailure = timeline.some((item) => item.kind === "tool" && item.status === "failed")
    const status = actionablePermission ? "waiting" : hasRuntimeError ? "error" : ["waiting_permission", "waiting_mcp_trust"].includes(state) ? "waiting" :
      active ? state === "queued" ? "queued" : "running" : state === "stopped" ? "stopped" : "completed"
    const statusLabel = state === "waiting_mcp_trust" && status === "waiting" ? "Waiting for MCP trust" :
      { running: "Running", queued: "Queued", waiting: "Waiting for permission", error: "Error", stopped: "Stopped", completed: "Completed" }[status]
    const explicitExpanded = expanded[group.key]
    const groupExpanded = explicitExpanded === undefined ? ACTIVE.includes(state) || hasRuntimeError || hasToolFailure || Boolean(actionablePermission) : explicitExpanded
    // Preserve original semantics: raw tool/runtime events + permission lifecycles,
    // not merged rows, assistant messages or a count of tool invocations.
    const stepCount = toolCount + permissionCount
    const summary = `${stepCount} steps · ${statusLabel}${hasToolFailure ? " · 有工具失败" : ""}`
    // Messages are boundaries, not children of a folding execution container.
    // A correlated Result is represented only at its Call anchor, even if it
    // arrives after a message. Its original step count belongs to that block.
    const items = [], blocks = []
    const byEventId = new Map(timeline.map((step) => [step.sources[0].id, step]))
    function executionBlock() {
      if (items.at(-1)?.kind === "execution") return items.at(-1).block
      const block = { key: blocks.length ? `${group.key}:after-${items.at(-1).id}` : group.key, timeline: [], stepCount: 0, live: null }
      blocks.push(block)
      items.push({ kind: "execution", id: block.key, block })
      return block
    }
    const countedPermissions = new Set()
    function appendStep(step) {
      const block = executionBlock()
      // Block identity follows the preceding message boundary, so streamed
      // logs becoming durable steps keep the same manual folding choice.
      block.timeline.push(step)
      if (step.kind === "tool" || step.kind === "runtime") block.stepCount += step.sources.length
      if (step.kind === "permission" && (!step.requestId || !countedPermissions.has(step.requestId))) {
        block.stepCount += 1
        if (step.requestId) countedPermissions.add(step.requestId)
      }
    }
    for (const event of group.events) {
      if (event.kind === "user" || event.kind === "assistant") {
        items.push({ id: `message-${event.id}`, kind: event.kind, content: event.content, eventId: event.id })
      } else if (byEventId.has(event.id)) appendStep(byEventId.get(event.id))
    }
    for (const step of timeline) {
      if (!group.events.some((event) => event.id === step.sources[0].id)) appendStep(step)
    }
    if (groupLive?.kind !== "assistant" && groupLive) executionBlock().live = groupLive
    if (!blocks.length && (active || hasRuntimeError)) executionBlock()
    if (groupLive?.kind === "assistant") {
      items.push({ id: `live-${group.key}`, kind: "assistant", content: groupLive.content, streaming: true })
    }
    for (const [index, block] of blocks.entries()) {
      const choice = expanded[block.key]
      const failed = block.timeline.some((step) => step.status === "failed") || block.live?.kind === "error"
      const pending = block.timeline.find((step) => step.actionable) || null
      Object.assign(block, {
        turnKey: group.key, status, statusLabel, actionablePermission: pending,
        expanded: choice === undefined ? active || failed || Boolean(pending) ||
          (index === 0 && hasRuntimeError && !timeline.some((step) => step.kind === "error")) : choice,
        blockNumber: index + 1, blockCount: blocks.length, turnStepCount: stepCount,
        summary: index === 0 ? `${summary}${blocks.length > 1 ? ` · 本段 ${block.stepCount} steps` : ""}` : `${block.stepCount} steps · ${statusLabel} · 同一轮执行`,
      })
    }
    return {
      ...group, items, timeline, assistants, live: groupLive, actionablePermission, status, statusLabel, stepCount,
      expanded: groupExpanded, hasToolFailure,
      summary,
    }
  })
}
