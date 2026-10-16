// Opt in in the dev console: window.__MYCODE_STREAM_DEBUG__ = {}.
// Only numeric aggregates are kept; no snapshots, event payloads or history.
function metrics() {
  return import.meta.env?.DEV ? globalThis.__MYCODE_STREAM_DEBUG__ : null
}

export function startStreamMeasure() {
  return metrics() ? performance.now() : null
}

export function finishStreamMeasure(name, started) {
  const stats = metrics()
  if (!stats || started === null) return
  const elapsed = performance.now() - started
  const value = stats[name] ||= { count: 0, totalMs: 0, maxMs: 0, lastMs: 0 }
  value.count += 1
  value.totalMs += elapsed
  value.maxMs = Math.max(value.maxMs, elapsed)
  value.lastMs = elapsed
}

export function recordLiveArrival() {
  const stats = metrics()
  if (!stats) return
  const now = performance.now()
  const value = stats.sse ||= { count: 0, lastAt: null, lastIntervalMs: null, maxIntervalMs: 0 }
  if (value.lastAt !== null) {
    value.lastIntervalMs = now - value.lastAt
    value.maxIntervalMs = Math.max(value.maxIntervalMs, value.lastIntervalMs)
  }
  value.count += 1
  value.lastAt = now
}
