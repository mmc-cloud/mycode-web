import json

import pytest

from app.services.jsonl_runtime import (
    MAX_JSONL_BUFFER_BYTES,
    MAX_JSONL_LINE_BYTES,
    JsonlProtocolError,
    JsonlRuntimeAdapter,
)


def wire(*messages: dict[str, object]) -> bytes:
    return b"".join(
        (
            json.dumps({"version": 1, **message}, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        for message in messages
    )


def test_adapter_handles_fragmented_utf8_jsonl() -> None:
    adapter = JsonlRuntimeAdapter()
    payload = wire(
        {"type": "agent_event", "event": {"type": "text_delta", "content": "你好"}}
    )
    split_at = payload.index("你".encode("utf-8")) + 1

    assert adapter.feed(payload[:split_at]) == ()
    assert adapter.feed(payload[split_at:]) == (
        {
            "version": 1,
            "type": "agent_event",
            "event": {"type": "text_delta", "content": "你好"},
        },
    )
    assert adapter.finish() == ()


def test_adapter_handles_multiple_lines_per_chunk() -> None:
    adapter = JsonlRuntimeAdapter()
    assert adapter.feed(wire({"type": "runtime_ready"}, {"type": "turn_finished"})) == (
        {"version": 1, "type": "runtime_ready"},
        {"version": 1, "type": "turn_finished"},
    )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not-json\n", "invalid_json"),
        (b"[]\n", "invalid_message"),
        (b'{"version":2,"type":"runtime_ready"}\n', "unsupported_version"),
        (b'{"version":true,"type":"runtime_ready"}\n', "unsupported_version"),
        (b'{"version":1}\n', "missing_type"),
        (b"\n", "empty_line"),
    ],
)
def test_adapter_rejects_malformed_or_unsupported_output(
    payload: bytes, code: str
) -> None:
    with pytest.raises(JsonlProtocolError) as raised:
        JsonlRuntimeAdapter().feed(payload)
    assert raised.value.code == code


def test_adapter_rejects_incomplete_line_at_eof() -> None:
    adapter = JsonlRuntimeAdapter()
    assert adapter.feed(b'{"version":1,"type":"runtime_ready"') == ()
    with pytest.raises(JsonlProtocolError, match="incomplete"):
        adapter.finish()


def test_adapter_rejects_fragmented_oversized_buffer_before_parsing() -> None:
    adapter = JsonlRuntimeAdapter()
    half = MAX_JSONL_BUFFER_BYTES // 2
    assert adapter.feed(b"x" * half) == ()
    with pytest.raises(JsonlProtocolError) as raised:
        adapter.feed(b"x" * (half + 1))
    assert raised.value.code == "message_too_large"


def test_adapter_rejects_oversized_complete_line_before_json_decode() -> None:
    payload = (
        b'{"version":1,"type":"runtime_warning","message":"'
        + b"x" * MAX_JSONL_LINE_BYTES
        + b'"}\n'
    )
    with pytest.raises(JsonlProtocolError) as raised:
        JsonlRuntimeAdapter().feed(payload)
    assert raised.value.code == "jsonl_line_too_large"


def test_adapter_accepts_normal_large_utf8_message_below_transport_limit() -> None:
    content = "你好" * 100_000
    payload = wire({"type": "runtime_warning", "message": content})
    assert JsonlRuntimeAdapter().feed(payload) == (
        {"version": 1, "type": "runtime_warning", "message": content},
    )


def test_adapter_encodes_turn_without_collapsing_newlines() -> None:
    encoded = JsonlRuntimeAdapter().encode_turn("turn-1", "第一行\n\n第二行")
    assert json.loads(encoded) == {
        "version": 1,
        "type": "turn",
        "turn_id": "turn-1",
        "content": "第一行\n\n第二行",
    }


@pytest.mark.parametrize(
    ("decision", "wire_decision"),
    [("deny", "reject"), ("once", "once"), ("task", "task"), ("session", "session")],
)
def test_adapter_maps_web_permission_decision_to_core(
    decision: str, wire_decision: str
) -> None:
    encoded = JsonlRuntimeAdapter().encode_permission_response("request-1", decision)  # type: ignore[arg-type]
    assert json.loads(encoded) == {
        "version": 1,
        "type": "permission_response",
        "request_id": "request-1",
        "decision": wire_decision,
    }


def test_adapter_encodes_mcp_trust_response() -> None:
    adapter = JsonlRuntimeAdapter()
    assert json.loads(adapter.encode_mcp_trust_response("trust-1", True)) == {
        "version": 1,
        "type": "mcp_trust_response",
        "request_id": "trust-1",
        "approved": True,
    }
