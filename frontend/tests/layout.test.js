import assert from "node:assert/strict"
import test from "node:test"
import { LAYOUT_KEY, LEGACY_LAYOUT_KEY, layoutDefaults, loadLayout } from "../src/layout.js"

const storage = (values) => ({ getItem: (key) => values[key] ?? null })

test("fresh layout uses a single Sessions sidebar", () => {
  assert.deepEqual(loadLayout(storage({})), layoutDefaults)
})

test("v1 migration preserves useful heights, never column widths or legacy data", () => {
  const legacy = JSON.stringify({ sessionsWidth: 360, workspaceWidth: 700, sessionsCollapsed: true, workspaceCollapsed: false, terminalHeight: 390, terminalCollapsed: true, workspaceTreeHeight: 310 })
  const values = { [LEGACY_LAYOUT_KEY]: legacy }
  assert.deepEqual(loadLayout(storage(values)), { ...layoutDefaults, sidebarView: "workspace", terminalHeight: 390, terminalCollapsed: true, workspaceTreeHeight: 310 })
  assert.equal(values[LEGACY_LAYOUT_KEY], legacy)
  assert.equal(values[LAYOUT_KEY], undefined)
})

test("v1 collapsed navigation remains closed with default sidebar width", () => {
  assert.deepEqual(loadLayout(storage({ [LEGACY_LAYOUT_KEY]: JSON.stringify({ sessionsCollapsed: true, workspaceCollapsed: true }) })), { ...layoutDefaults, sidebarOpen: false })
})

test("v2 wins over v1 and restores the full new layout", () => {
  const saved = { sidebarView: "workspace", sidebarOpen: false, sidebarWidth: 450, terminalHeight: 420, terminalCollapsed: true, workspaceTreeHeight: 350 }
  assert.deepEqual(loadLayout(storage({ [LAYOUT_KEY]: JSON.stringify(saved), [LEGACY_LAYOUT_KEY]: JSON.stringify({ terminalHeight: 200 }) })), saved)
})

test("malformed v2 falls back to v1 without losing valid heights", () => {
  assert.equal(loadLayout(storage({ [LAYOUT_KEY]: "{bad", [LEGACY_LAYOUT_KEY]: JSON.stringify({ terminalHeight: 350 }) })).terminalHeight, 350)
})

test("invalid v2 fields and unknown keys never enter live state", () => {
  assert.deepEqual(loadLayout(storage({ [LAYOUT_KEY]: JSON.stringify({ sidebarView: "other", sidebarOpen: "false", sidebarWidth: 900, terminalHeight: -2, workspaceTreeHeight: "600", terminalCollapsed: 1, extra: "ignore", sessionsWidth: 300 }) })), layoutDefaults)
})

test("invalid legacy parameters individually fall back to safe defaults", () => {
  assert.deepEqual(loadLayout(storage({ [LEGACY_LAYOUT_KEY]: JSON.stringify({ terminalHeight: 900, terminalCollapsed: "true", workspaceTreeHeight: -1 }) })), layoutDefaults)
})

test("non-object data and unavailable local storage are safe", () => {
  for (const data of ["null", "[]", '"text"', "1"]) assert.deepEqual(loadLayout(storage({ [LAYOUT_KEY]: data, [LEGACY_LAYOUT_KEY]: data })), layoutDefaults)
  assert.deepEqual(loadLayout({ getItem: () => { throw new Error("storage denied") } }), layoutDefaults)
})
