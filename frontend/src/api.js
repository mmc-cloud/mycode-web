export const API_BASE = "/mycode/api"

export function sessionApiPath(sessionId, suffix = "") {
  return `/sessions/${encodeURIComponent(sessionId)}${suffix}`
}

export function websocketUrl(path) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${protocol}//${window.location.host}${API_BASE}${path}`
}
