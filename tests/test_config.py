from pathlib import Path

from app.config import MYCODE_SANDBOX_OPTIONAL_ENV_NAMES, ServerSettings


def test_optional_mycode_runtime_env_is_read_without_server_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    for name in MYCODE_SANDBOX_OPTIONAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_RESERVED_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "false")

    settings = ServerSettings(data_dir=tmp_path)

    assert settings.sandbox_optional_env == (
        ("LLM_RESERVED_OUTPUT_TOKENS", "8192"),
        ("LLM_STREAM_INCLUDE_USAGE", "false"),
    )


def test_missing_optional_mycode_runtime_env_is_not_injected(
    tmp_path: Path, monkeypatch
) -> None:
    for name in MYCODE_SANDBOX_OPTIONAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    settings = ServerSettings(data_dir=tmp_path)

    assert settings.sandbox_optional_env == ()


def test_sandbox_capacity_lifecycle_and_resource_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    names = (
        "SANDBOX_MAX_ACTIVE",
        "SANDBOX_QUEUE_MAX",
        "SANDBOX_MAX_ACTIVE_PER_USER",
        "SANDBOX_MEMORY_LIMIT",
        "SANDBOX_MEMORY_SWAP_LIMIT",
        "SANDBOX_CPUS",
        "SANDBOX_PIDS_LIMIT",
        "SANDBOX_IDLE_TTL_SECONDS",
        "RUNTIME_SWEEP_INTERVAL_SECONDS",
        "SESSION_RETENTION_SECONDS",
        "SESSION_CLEANUP_INTERVAL_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    settings = ServerSettings(data_dir=tmp_path)

    assert settings.sandbox_max_active == 2
    assert settings.sandbox_queue_max == 20
    assert settings.sandbox_max_active_per_user == 5
    assert settings.sandbox_memory_limit == "640m"
    assert settings.sandbox_memory_swap_limit == "1g"
    assert settings.sandbox_cpus == 1.0
    assert settings.sandbox_pids_limit == 256
    assert settings.sandbox_idle_ttl_seconds == 7200
    assert settings.runtime_sweep_interval_seconds == 60
    assert settings.session_retention_seconds == 1209600
    assert settings.session_cleanup_interval_seconds == 3600


def test_per_user_sandbox_limit_uses_env_int_validation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SANDBOX_MAX_ACTIVE_PER_USER", "3")
    assert ServerSettings(data_dir=tmp_path).sandbox_max_active_per_user == 3

    monkeypatch.setenv("SANDBOX_MAX_ACTIVE_PER_USER", "0")
    try:
        ServerSettings(data_dir=tmp_path)
    except ValueError as error:
        assert "SANDBOX_MAX_ACTIVE_PER_USER" in str(error)
    else:
        raise AssertionError("Expected invalid per-user limit to be rejected")
