import { API_BASE_PATH, WEB_BASE_PATH } from "./base-path"

export { API_BASE_PATH, WEB_BASE_PATH }
export const API_BASE = API_BASE_PATH

export function sessionApiPath(sessionId, suffix = "") {
  return `/sessions/${encodeURIComponent(sessionId)}${suffix}`
}

export function websocketUrl(path) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${protocol}//${window.location.host}${API_BASE}${path}`
}
