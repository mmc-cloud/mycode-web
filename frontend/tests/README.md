# Frontend checks

Run from the repository root:

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

The unit tests cover execution ordering, tool associations, permissions, folding,
layout persistence, and streaming Markdown scheduling using a fake clock.
Runtime regressions also check stale Turn status isolation and independent Session
warnings, including legacy history, without adding execution blocks to a completed Turn.
Runtime preview regressions suppress duplicate labels while retaining useful context,
full details, source events, and step counts. The browser suite also checks full-width
Agent replies, unchanged right-aligned user bubbles, and independent horizontal
scrolling for long code and wide tables at desktop and narrow viewport sizes.
Normal streaming renders have a 60ms minimum interval. Expensive renders use
`max(60ms, 2 × last parse duration)` between the end of parsing and the next
render, leaving time for DOM commits and input. An existing trailing deadline is
never postponed by a new snapshot. Completion bypasses the interval.

Run the browser regression with isolated HTTP, SSE and WebSocket fixtures:

```powershell
# Keep this server running in a separate terminal.
npm --prefix frontend run dev -- --port 5178 --strictPort

npx --yes --package @playwright/cli playwright-cli -s=mycode-ui open about:blank --browser msedge
npx --yes --package @playwright/cli playwright-cli -s=mycode-ui run-code --filename frontend/tests/ui-smoke.js --raw
npx --yes --package @playwright/cli playwright-cli -s=mycode-ui close
```

The browser suite returns short/long streaming statistics and checks actual DOM
updates, final snapshots, session isolation, scrolling, code copying, permissions,
folding, and tool/message ordering. Screenshots go to `output/playwright/`.
It exercises the frontend with synthetic events, not a live Agent or SSE network.

## Optional development statistics

While running the Vite dev server, enable/reset counters in the browser console:

```javascript
window.__MYCODE_STREAM_DEBUG__ = {}
```

Inspect that object after streaming:

| Field | Meaning |
| --- | --- |
| `sse` | `console_live` callback count, last arrival time, last/max callback interval in milliseconds |
| `timeline` | Full `buildExecutionGroups` count and total/last/max projection duration in milliseconds |
| `markdown` | Markdown parse, highlight and sanitize count and total/last/max duration in milliseconds |

Only numeric aggregates are retained. Markdown timing excludes Vue DOM updates,
layout and painting; callback intervals include any main-thread blocking and are
not a measurement of server/network timing alone. Timeline boundaries, durable
events, runtime/permission changes and manual folding can all cause projections.
For text-only growth, reset the counters after the live message has mounted.

Disable counters with `delete window.__MYCODE_STREAM_DEBUG__`. They are disabled
by default and removed from production builds by Vite.
