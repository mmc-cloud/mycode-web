from collections import defaultdict

from app.db.database import ConsoleEvent, WebDatabase


_NOISE_LINES = {
    "输入 /exit 或 /quit 退出。",
    "mycode-project 已就绪。",
}
_TOOL_PREFIXES = (
    "活动> ", "轮次> ", "提醒> ", "警告> ", "提示> ",
    "tool_call> ", "tool_result> ", "artifact> ", "context> ",
    "progress> ", "stop> ", "instructions> ",
)
_ERROR_PREFIXES = ("错误> ", "error> ", "session> 错误：", "session> 严重错误：")
_PERMISSION_PREFIXES = (
    "permission> ", "target> ", "reason> ", "message> ",
    "command_display> ", "resolved_path> ", "resolved_cwd> ",
    "path_scope> ", "cwd_scope> ", "command_risk_category> ",
    "command_risk_reason> ",
)
_PERMISSION_PROMPT = (
    "是否批准？[y/yes 本次 | t/task 当前任务 | "
    "s/session 当前会话 | N 拒绝] "
)


class ConsoleRecorder:
    """Project CLI text/events into bounded, per-Session console history."""

    def __init__(self, database: WebDatabase) -> None:
        self.database = database
        self._buffers: dict[str, str] = defaultdict(str)
        self._buffer_turn_ids: dict[str, str | None] = {}
        self._roles: dict[str, str] = defaultdict(lambda: "assistant")

    def record_event(
        self, session_id: str, event_type: str, data: dict[str, object]
    ) -> tuple[ConsoleEvent, ...]:
        turn_id = _turn_id(data)
        if event_type == "agent_output":
            return self._record_output(
                session_id, str(data.get("content", "")), turn_id
            )
        if event_type == "user_message":
            return self._append(
                session_id, "user", str(data.get("content", "")),
                data=_metadata(data, turn_id),
            )
        if event_type == "permission_request":
            return self._append(
                session_id,
                "permission",
                str(data.get("summary") or "Agent 请求权限"),
                data=_metadata(data, turn_id),
            )
        if event_type == "permission_resolved":
            decision_labels = {
                "deny": "已拒绝",
                "once": "已允许（仅本次）",
                "task": "已允许（当前任务）",
                "session": "已允许（当前会话）",
            }
            decision = decision_labels.get(
                data.get("decision"),
                "已允许" if data.get("allowed") else "已拒绝",
            )
            if data.get("expired"):
                decision = "权限请求已过期"
            return self._append(
                session_id, "permission", decision,
                data=_metadata(data, turn_id),
            )
        if event_type == "error":
            return self._append(
                session_id, "error",
                str(data.get("message", "Runtime error")),
                data=_metadata(data, turn_id),
            )
        return ()

    def clear_session(self, session_id: str) -> None:
        self._buffers.pop(session_id, None)
        self._buffer_turn_ids.pop(session_id, None)
        self._roles.pop(session_id, None)

    def live_output(self, session_id: str) -> dict[str, object]:
        value = self._buffers.get(session_id, "")
        kind = self._roles[session_id]
        if value.startswith("assistant> "):
            kind = "assistant"
            value = value[len("assistant> ") :]
        elif value.startswith(_ERROR_PREFIXES):
            kind = "error"
        elif value.startswith(_PERMISSION_PREFIXES):
            value = ""
        elif value.startswith(_TOOL_PREFIXES) or value.startswith("session> "):
            kind = "tool"
        if value in _NOISE_LINES or value in {"you> ", _PERMISSION_PROMPT}:
            value = ""
        result: dict[str, object] = {
            "active": bool(value), "kind": kind, "content": value
        }
        turn_id = self._buffer_turn_ids.get(session_id)
        if turn_id is not None:
            result["turn_id"] = turn_id
        return result

    def _record_output(
        self, session_id: str, text: str, turn_id: str | None
    ) -> tuple[ConsoleEvent, ...]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        previous_turn_id = self._buffer_turn_ids.get(session_id)
        if (
            self._buffers[session_id]
            and previous_turn_id != turn_id
        ):
            recorded = list(self._record_line(
                session_id, self._buffers[session_id], previous_turn_id
            ))
            self._buffers[session_id] = ""
        else:
            recorded = []
        self._buffer_turn_ids[session_id] = turn_id
        buffer = self._buffers[session_id] + normalized
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            recorded.extend(self._record_line(session_id, line, turn_id))
        if buffer.endswith(_PERMISSION_PROMPT):
            prefix = buffer[: -len(_PERMISSION_PROMPT)]
            if prefix:
                recorded.extend(self._record_line(session_id, prefix, turn_id))
            buffer = ""
        if buffer.endswith("you> "):
            prefix = buffer[: -len("you> ")]
            if prefix:
                recorded.extend(self._record_line(session_id, prefix, turn_id))
            buffer = ""
        self._buffers[session_id] = buffer
        return tuple(recorded)

    def _record_line(
        self, session_id: str, line: str, turn_id: str | None
    ) -> tuple[ConsoleEvent, ...]:
        value = line.strip("\r")
        if not value or value in _NOISE_LINES or value == "you> ":
            return ()
        if value.endswith("you> "):
            value = value[: -len("you> ")].rstrip()
            if not value:
                return ()
        if value.startswith("assistant> "):
            self._roles[session_id] = "assistant"
            value = value[len("assistant> ") :]
        elif value.startswith(_ERROR_PREFIXES):
            self._roles[session_id] = "error"
        elif value.startswith(_PERMISSION_PREFIXES) or value == _PERMISSION_PROMPT:
            return ()
        elif value.startswith(_TOOL_PREFIXES) or value.startswith("session> "):
            self._roles[session_id] = "tool"
        return self._append(
            session_id, self._roles[session_id], value,
            data=_metadata({}, turn_id), coalesce=True,
        )

    def _append(
        self, session_id: str, kind: str, content: str, *,
        data: dict[str, object] | None = None, coalesce: bool = False,
    ) -> tuple[ConsoleEvent, ...]:
        event = self.database.append_console_event(
            session_id, kind, content, data=data, coalesce=coalesce
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
