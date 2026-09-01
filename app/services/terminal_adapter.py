from dataclasses import dataclass


USER_PROMPT = "you> "
PERMISSION_PROMPT = "是否批准？[y/N] "


@dataclass(frozen=True)
class TerminalSignal:
    type: str
    data: dict[str, object]


class TerminalOutputAdapter:
    """Small adapter for the current human-oriented CLI text protocol."""

    _permission_fields = {
        "permission": "summary",
        "target": "target",
        "reason": "reason",
        "message": "message",
        "command_display": "command",
        "resolved_path": "resolved_path",
        "resolved_cwd": "resolved_cwd",
        "path_scope": "path_scope",
        "cwd_scope": "cwd_scope",
        "command_risk_category": "risk_category",
        "command_risk_reason": "risk_reason",
    }

    def __init__(self) -> None:
        self._buffer = ""
        self._permission: dict[str, object] = {}
        self.awaiting_permission = False

    def feed(self, text: str) -> list[TerminalSignal]:
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        signals: list[TerminalSignal] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._observe_line(line)

        if (
            not self.awaiting_permission
            and self._buffer.endswith(PERMISSION_PROMPT)
        ):
            prefix = self._buffer[: -len(PERMISSION_PROMPT)]
            if prefix:
                self._observe_line(prefix)
            self._buffer = ""
            self.awaiting_permission = True
            signals.append(
                TerminalSignal("permission_request", dict(self._permission))
            )

        if not self.awaiting_permission and self._buffer.endswith(USER_PROMPT):
            prefix = self._buffer[: -len(USER_PROMPT)]
            if prefix:
                self._observe_line(prefix)
            self._buffer = ""
            signals.append(TerminalSignal("ready", {}))

        if len(self._buffer) > 256_000:
            self._buffer = self._buffer[-4096:]
        return signals

    def resolve_permission(self) -> None:
        self.awaiting_permission = False
        self._permission = {}
        self._buffer = ""

    @property
    def pending_permission(self) -> dict[str, object] | None:
        if not self.awaiting_permission:
            return None
        return dict(self._permission)

    def _observe_line(self, line: str) -> None:
        if line.startswith("permission> "):
            self._permission = {}
        for prefix, key in self._permission_fields.items():
            marker = prefix + "> "
            if line.startswith(marker):
                self._permission[key] = line[len(marker) :]
                return
