from collections import defaultdict

from app.db.database import ConsoleEvent, WebDatabase


class ConsoleRecorder:
    """Persist a bounded, structured projection of runtime events."""

    def __init__(self, database: WebDatabase) -> None:
        self.database = database
        self._buffers: dict[str, str] = defaultdict(str)
        self._buffer_turn_ids: dict[str, str | None] = {}

    def record_event(
        self, session_id: str, event_type: str, data: dict[str, object]
    ) -> tuple[ConsoleEvent, ...]:
        turn_id = _turn_id(data)
        if event_type == "agent_event":
            event = data.get("event")
            if not isinstance(event, dict):
                return ()
            return self._record_agent_event(session_id, event, turn_id)
        if event_type == "turn_finished":
            return self._flush_assistant(session_id)
        if event_type == "user_message":
            return self._append(
                session_id,
                "user",
                str(data.get("content", "")),
                data=_metadata(data, turn_id),
            )
        if event_type == "permission_request":
            return self._append(
                session_id,
                "permission",
                _permission_label(data),
                data=_metadata(data, turn_id),
            )
        if event_type == "permission_resolved":
            labels = {
                "deny": "已拒绝",
                "once": "已允许（仅本次）",
                "task": "已允许（当前任务）",
                "session": "已允许（当前会话）",
            }
            decision = labels.get(
                str(data.get("decision")),
                "已允许" if data.get("allowed") else "已拒绝",
            )
            if data.get("expired"):
                decision = "权限请求已过期"
            return self._append(
                session_id,
                "permission",
                decision,
                data=_metadata(data, turn_id),
            )
        if event_type in {"error", "runtime_error"}:
            return self._append(
                session_id,
                "error",
                str(data.get("message", "Runtime error")),
                data=_metadata(data, turn_id),
            )
        if event_type == "runtime_warning":
            return self._append(
                session_id,
                "tool",
                str(data.get("message", "Runtime warning")),
                data=_metadata(data, turn_id),
            )
        if event_type == "mcp_status":
            return self._append(
                session_id,
                "tool",
                f"MCP {data.get('alias', 'server')}: {data.get('status', 'unknown')}",
                data=_metadata(data, turn_id),
            )
        return ()

    def clear_session(self, session_id: str) -> None:
        self._buffers.pop(session_id, None)
        self._buffer_turn_ids.pop(session_id, None)

    def live_output(self, session_id: str) -> dict[str, object]:
        value = self._buffers.get(session_id, "")
        result: dict[str, object] = {
            "active": bool(value),
            "kind": "assistant",
            "content": value,
        }
        turn_id = self._buffer_turn_ids.get(session_id)
        if turn_id is not None:
            result["turn_id"] = turn_id
        return result

    def _record_agent_event(
        self,
        session_id: str,
        event: dict[str, object],
        turn_id: str | None,
    ) -> tuple[ConsoleEvent, ...]:
        event_name = event.get("type")
        if event_name == "text_delta":
            content = event.get("content", "")
            if not isinstance(content, str) or not content:
                return ()
            previous_turn_id = self._buffer_turn_ids.get(session_id)
            recorded: list[ConsoleEvent] = []
            if self._buffers[session_id] and previous_turn_id != turn_id:
                recorded.extend(self._flush_assistant(session_id))
            self._buffer_turn_ids[session_id] = turn_id
            self._buffers[session_id] += content
            return tuple(recorded)

        recorded = list(self._flush_assistant(session_id))
        if not isinstance(event_name, str):
            return tuple(recorded)
        kind, content = _agent_event_summary(event_name, event)
        if content:
            recorded.extend(
                self._append(
                    session_id,
                    kind,
                    content,
                    data={
                        "turn_id": turn_id,
                        "event_type": event_name,
                        "event": event,
                    },
                )
            )
        return tuple(recorded)

    def _flush_assistant(self, session_id: str) -> tuple[ConsoleEvent, ...]:
        content = self._buffers.pop(session_id, "")
        turn_id = self._buffer_turn_ids.pop(session_id, None)
        if not content:
            return ()
        return self._append(
            session_id,
            "assistant",
            content,
            data={"turn_id": turn_id, "event_type": "text_delta"},
        )

    def _append(
        self,
        session_id: str,
        kind: str,
        content: str,
        *,
        data: dict[str, object] | None = None,
    ) -> tuple[ConsoleEvent, ...]:
        event = self.database.append_console_event(
            session_id, kind, content, data=data
        )
        return () if event is None else (event,)


def _turn_id(data: dict[str, object]) -> str | None:
    value = data.get("turn_id")
    return value if isinstance(value, str) and value else None


def _metadata(data: dict[str, object], turn_id: str | None) -> dict[str, object]:
    metadata = dict(data)
    if turn_id is None:
        metadata.pop("turn_id", None)
    else:
        metadata["turn_id"] = turn_id
    return metadata


def _permission_label(data: dict[str, object]) -> str:
    tool_name = data.get("tool_name") or "Agent"
    action = data.get("action") or data.get("capability") or "permission"
    target = data.get("target")
    label = f"{tool_name}: {action}"
    return f"{label} · {target}" if target else label


def _agent_event_summary(event_name: str, event: dict[str, object]) -> tuple[str, str]:
    if event_name == "tool_call":
        call = event.get("tool_call")
        if isinstance(call, dict):
            return "tool", f"Tool call: {call.get('name', 'unknown')}"
        return "tool", "Tool call"
    if event_name == "tool_result":
        result = event.get("tool_result")
        if isinstance(result, dict):
            status = "completed" if result.get("ok") else "failed"
            return "tool", f"Tool result: {status}"
        return "tool", "Tool result"
    if event_name == "error":
        return "error", str(event.get("error") or event.get("content") or "Agent error")
    if event_name == "stop":
        return "tool", f"Stopped: {event.get('stop_reason', 'unknown')}"
    if event_name in {"context", "progress", "model_retry", "artifact_warning"}:
        return "tool", str(event.get("content") or event_name)
    if event_name == "reasoning_state":
        return "", ""
    return "tool", str(event.get("content") or event_name)
