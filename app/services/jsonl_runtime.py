"""Thin Web-side adapter for the Core 14.5 runtime JSONL wire protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal


JSONL_PROTOCOL_VERSION = 1
MAX_JSONL_LINE_BYTES = 8 * 1024 * 1024
MAX_JSONL_BUFFER_BYTES = 8 * 1024 * 1024
PermissionDecision = Literal["deny", "once", "task", "session"]


class JsonlProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JsonlRuntimeAdapter:
    """Frame Core's byte-oriented stdout and encode Web-to-Core messages."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[dict[str, object], ...]:
        messages: list[dict[str, object]] = []
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline < 0:
                fragment = chunk[start:]
                buffered_size = len(self._buffer) + len(fragment)
                if buffered_size > MAX_JSONL_BUFFER_BYTES:
                    raise JsonlProtocolError(
                        "message_too_large",
                        "Runtime JSONL buffer exceeded the configured limit.",
                    )
                self._buffer.extend(fragment)
                break

            fragment = chunk[start:newline]
            line_size = len(self._buffer) + len(fragment)
            if line_size > MAX_JSONL_LINE_BYTES:
                raise JsonlProtocolError(
                    "jsonl_line_too_large",
                    "Runtime JSONL line exceeded the configured limit.",
                )
            line = bytes(self._buffer) + fragment
            self._buffer.clear()
            messages.append(self._decode_line(line))
            start = newline + 1
        return tuple(messages)

    def finish(self) -> tuple[dict[str, object], ...]:
        if self._buffer:
            try:
                bytes(self._buffer).decode("utf-8")
            except UnicodeDecodeError as error:
                raise JsonlProtocolError(
                    "invalid_utf8", "Runtime output is not UTF-8."
                ) from error
            raise JsonlProtocolError("incomplete_jsonl", "Runtime output ended with an incomplete JSONL line.")
        return ()

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

    @staticmethod
    def _decode_line(line: bytes) -> dict[str, object]:
        if not line.strip():
            raise JsonlProtocolError("empty_line", "Runtime output contained an empty JSONL line.")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise JsonlProtocolError("invalid_utf8", "Runtime output is not UTF-8.") from error
        try:
            payload = json.loads(text)
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
        return payload

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
