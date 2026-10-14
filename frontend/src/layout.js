export const LAYOUT_KEY = "mycode.layout.v2"
export const LEGACY_LAYOUT_KEY = "mycode.layout.v1"
export const layoutDefaults = Object.freeze({
  sidebarView: "sessions",
  sidebarOpen: true,
  sidebarWidth: 320,
  workspaceTreeHeight: 260,
  terminalHeight: 280,
  terminalCollapsed: false,
})

function readObject(value) {
  try {
    const parsed = JSON.parse(value || "null")
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null
  } catch (_) { return null }
}

function bounded(value, minimum, maximum, fallback) {
  return Number.isFinite(value) && value >= minimum && value <= maximum ? value : fallback
}

export function loadLayout(storage) {
  try {
    const source = storage || window.localStorage
    const saved = readObject(source.getItem(LAYOUT_KEY))
    if (saved) {
      return {
        sidebarView: ["sessions", "workspace"].includes(saved.sidebarView) ? saved.sidebarView : layoutDefaults.sidebarView,
        sidebarOpen: typeof saved.sidebarOpen === "boolean" ? saved.sidebarOpen : layoutDefaults.sidebarOpen,
        sidebarWidth: bounded(saved.sidebarWidth, 220, 520, layoutDefaults.sidebarWidth),
        workspaceTreeHeight: bounded(saved.workspaceTreeHeight, 120, 600, layoutDefaults.workspaceTreeHeight),
        terminalHeight: bounded(saved.terminalHeight, 160, 560, layoutDefaults.terminalHeight),
        terminalCollapsed: typeof saved.terminalCollapsed === "boolean" ? saved.terminalCollapsed : layoutDefaults.terminalCollapsed,
      }
    }
    const legacy = readObject(source.getItem(LEGACY_LAYOUT_KEY)) || {}
    const validNavigation = typeof legacy.sessionsCollapsed === "boolean" && typeof legacy.workspaceCollapsed === "boolean"
    // Retain useful heights and visibility, never reinterpret old column widths.
    // Reading the old key is intentionally non-destructive.
    return {
      ...layoutDefaults,
      sidebarView: validNavigation && legacy.sessionsCollapsed && !legacy.workspaceCollapsed ? "workspace" : "sessions",
      sidebarOpen: validNavigation ? !(legacy.sessionsCollapsed && legacy.workspaceCollapsed) : layoutDefaults.sidebarOpen,
      workspaceTreeHeight: bounded(legacy.workspaceTreeHeight, 120, 600, layoutDefaults.workspaceTreeHeight),
      terminalHeight: bounded(legacy.terminalHeight, 160, 560, layoutDefaults.terminalHeight),
      terminalCollapsed: typeof legacy.terminalCollapsed === "boolean" ? legacy.terminalCollapsed : layoutDefaults.terminalCollapsed,
    }
  } catch (_) { return { ...layoutDefaults } }
}
