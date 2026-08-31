from pathlib import Path

from app.config import MYCODE_SANDBOX_OPTIONAL_ENV_NAMES, ServerSettings


def test_optional_mycode_runtime_env_is_read_without_server_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    for name in MYCODE_SANDBOX_OPTIONAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_RESERVED_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "false")

    settings = ServerSettings(data_dir=tmp_path, relay_token="token")

    assert settings.sandbox_optional_env == (
        ("LLM_RESERVED_OUTPUT_TOKENS", "8192"),
        ("LLM_STREAM_INCLUDE_USAGE", "false"),
    )


def test_missing_optional_mycode_runtime_env_is_not_injected(
    tmp_path: Path, monkeypatch
) -> None:
    for name in MYCODE_SANDBOX_OPTIONAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    settings = ServerSettings(data_dir=tmp_path, relay_token="token")

    assert settings.sandbox_optional_env == ()
