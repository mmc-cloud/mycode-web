"""Thin Web-side adapter for the Core 14.5 runtime JSONL wire protocol."""

from __future__ import annotations

import codecs
import json
from collections.abc import Mapping
from typing import Literal


JSONL_PROTOCOL_VERSION = 1
PermissionDecision = Literal["deny", "once", "task", "session"]
_WIRE_PERMISSION_DECISIONS = {"reject", "once", "task", "session"}


class JsonlProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JsonlRuntimeAdapter:
    """Frame Core's byte-oriented stdout and encode Web-to-Core messages."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""

    def feed(self, chunk: bytes) -> tuple[dict[str, object], ...]:
        try:
            text = self._decoder.decode(chunk)
        except UnicodeDecodeError as error:
            raise JsonlProtocolError("invalid_utf8", "Runtime output is not UTF-8.") from error
        return self._feed_text(text)

    def finish(self) -> tuple[dict[str, object], ...]:
        try:
            text = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise JsonlProtocolError("invalid_utf8", "Runtime output is not UTF-8.") from error
        messages = self._feed_text(text)
        if self._buffer:
            raise JsonlProtocolError("incomplete_jsonl", "Runtime output ended with an incomplete JSONL line.")
        return messages

    def encode_turn(self, turn_id: str, content: str) -> bytes:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id must not be empty.")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message must not be empty.")
        return self._encode({"type": "turn", "turn_id": turn_id, "content": content})

    def encode_permission_response(
        self, request_id: str, decision: PermissionDecision
    ) -> bytes:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must not be empty.")
        if decision not in {"deny", "once", "task", "session"}:
            raise ValueError("Unsupported permission decision.")
        wire_decision = "reject" if decision == "deny" else decision
        return self._encode(
            {
                "type": "permission_response",
                "request_id": request_id,
                "decision": wire_decision,
            }
        )

    def encode_mcp_trust_response(self, request_id: str, approved: bool) -> bytes:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must not be empty.")
        if not isinstance(approved, bool):
            raise ValueError("approved must be a boolean.")
        return self._encode(
            {
                "type": "mcp_trust_response",
                "request_id": request_id,
                "approved": approved,
            }
        )

    def encode_close(self) -> bytes:
        return self._encode({"type": "close"})

    def _feed_text(self, text: str) -> tuple[dict[str, object], ...]:
        self._buffer += text
        messages: list[dict[str, object]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if not line.strip():
                raise JsonlProtocolError("empty_line", "Runtime output contained an empty JSONL line.")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise JsonlProtocolError("invalid_json", "Runtime output is not valid JSON.") from error
            if not isinstance(payload, dict):
                raise JsonlProtocolError("invalid_message", "Runtime output must be a JSON object.")
            if payload.get("version") != JSONL_PROTOCOL_VERSION or isinstance(
                payload.get("version"), bool
            ):
                raise JsonlProtocolError("unsupported_version", "Unsupported JSONL protocol version.")
            if not isinstance(payload.get("type"), str) or not payload["type"].strip():
                raise JsonlProtocolError("missing_type", "Runtime output requires a string type.")
            messages.append(payload)
        return tuple(messages)

    @staticmethod
    def _encode(message: Mapping[str, object]) -> bytes:
        payload = {"version": JSONL_PROTOCOL_VERSION, **dict(message)}
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
