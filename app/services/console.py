from collections import defaultdict

from app.db.database import ConsoleEvent, WebDatabase


_NOISE_LINES = {
    "输入 /exit 或 /quit 退出。",
    "mycode-project 已就绪。",
}
_TOOL_PREFIXES = (
    "活动> ",
    "轮次> ",
    "提醒> ",
    "警告> ",
    "提示> ",
    "tool_call> ",
    "tool_result> ",
    "artifact> ",
    "context> ",
    "progress> ",
    "stop> ",
    "instructions> ",
)
_ERROR_PREFIXES = ("错误> ", "error> ", "session> 错误：", "session> 严重错误：")
_PERMISSION_PREFIXES = (
    "permission> ",
    "target> ",
    "reason> ",
    "message> ",
    "command_display> ",
    "resolved_path> ",
    "resolved_cwd> ",
    "path_scope> ",
    "cwd_scope> ",
    "command_risk_category> ",
    "command_risk_reason> ",
)
_PERMISSION_PROMPT = "是否批准？[y/N] "


class ConsoleRecorder:
    """Project CLI text/events into bounded, per-Session console history."""

    def __init__(self, database: WebDatabase) -> None:
        self.database = database
        self._buffers: dict[str, str] = defaultdict(str)
        self._roles: dict[str, str] = defaultdict(lambda: "assistant")

    def record_event(
        self, session_id: str, event_type: str, data: dict[str, object]
    ) -> tuple[ConsoleEvent, ...]:
        if event_type == "agent_output":
            return self._record_output(session_id, str(data.get("content", "")))
        if event_type == "user_message":
            return self._append(session_id, "user", str(data.get("content", "")))
        if event_type == "permission_request":
            summary = str(data.get("summary") or "Agent 请求权限")
            return self._append(session_id, "permission", summary, data=data)
        if event_type == "permission_resolved":
            decision = "已允许" if data.get("allowed") else "已拒绝"
            if data.get("expired"):
                decision = "权限请求已过期"
            return self._append(session_id, "permission", decision, data=data)
        if event_type == "error":
            return self._append(
                session_id, "error", str(data.get("message", "Runtime error")), data=data
            )
        return ()

    def clear_session(self, session_id: str) -> None:
        self._buffers.pop(session_id, None)
        self._roles.pop(session_id, None)

    def live_output(self, session_id: str) -> dict[str, object]:
        """Return the current non-persisted line for transient browser display."""
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
        return {"active": bool(value), "kind": kind, "content": value}

    def _record_output(self, session_id: str, text: str) -> tuple[ConsoleEvent, ...]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        buffer = self._buffers[session_id] + normalized
        recorded: list[ConsoleEvent] = []
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            recorded.extend(self._record_line(session_id, line))
        if buffer.endswith(_PERMISSION_PROMPT):
            prefix = buffer[: -len(_PERMISSION_PROMPT)]
            if prefix:
                recorded.extend(self._record_line(session_id, prefix))
            buffer = ""
        if buffer.endswith("you> "):
            prefix = buffer[: -len("you> ")]
            if prefix:
                recorded.extend(self._record_line(session_id, prefix))
            buffer = ""
        self._buffers[session_id] = buffer
        return tuple(recorded)

    def _record_line(self, session_id: str, line: str) -> tuple[ConsoleEvent, ...]:
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
        kind = self._roles[session_id]
        return self._append(session_id, kind, value, coalesce=True)

    def _append(
        self,
        session_id: str,
        kind: str,
        content: str,
        *,
        data: dict[str, object] | None = None,
        coalesce: bool = False,
    ) -> tuple[ConsoleEvent, ...]:
        event = self.database.append_console_event(
            session_id, kind, content, data=data, coalesce=coalesce
        )
        return () if event is None else (event,)
