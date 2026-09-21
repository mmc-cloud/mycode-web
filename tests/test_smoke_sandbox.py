from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "smoke_sandbox_runtime", ROOT / "scripts/smoke-sandbox-runtime.py"
)
assert SPEC is not None and SPEC.loader is not None
SMOKE = module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


def message(message_type: str) -> dict[str, object]:
    return {"version": 1, "type": message_type}


def test_smoke_success_requires_ready_closed_and_zero_exit() -> None:
    SMOKE.validate_smoke_messages(
        [message("runtime_ready"), message("runtime_closed")], 0
    )


@pytest.mark.parametrize(
    ("messages", "return_code", "expected"),
    [
        ([message("runtime_ready")], 0, "runtime_closed"),
        ([message("runtime_closed")], 0, "runtime_ready"),
        ([message("runtime_ready"), message("runtime_closed")], 1, "non-zero"),
    ],
)
def test_smoke_rejects_incomplete_or_failed_process(
    messages: list[dict[str, object]], return_code: int, expected: str
) -> None:
    with pytest.raises(SMOKE.SmokeError, match=expected):
        SMOKE.validate_smoke_messages(messages, return_code)


def test_smoke_rejects_invalid_jsonl() -> None:
    with pytest.raises(SMOKE.SmokeError, match="valid UTF-8 JSONL"):
        SMOKE.parse_smoke_jsonl([b"not-json\n"])
