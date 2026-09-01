from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets


SERVER_ROOT = Path(__file__).resolve().parents[1]
MYCODE_SANDBOX_OPTIONAL_ENV_NAMES = (
    "MYCODE_COMPACT_MODEL",
    "MYCODE_SUBAGENT_MODEL",
    "LLM_CONTEXT_WINDOW_TOKENS",
    "LLM_RESERVED_OUTPUT_TOKENS",
    "LLM_CONTEXT_SAFETY_MARGIN_TOKENS",
    "LLM_MEMORY_CONTEXT_TOKENS",
    "LLM_STREAM_INCLUDE_USAGE",
    "LLM_THINKING_ENABLED",
    "LLM_REASONING_EFFORT",
    "LLM_MAX_OUTPUT_TOKENS",
)


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else default.resolve()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _optional_sandbox_env() -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for name in MYCODE_SANDBOX_OPTIONAL_ENV_NAMES:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            values.append((name, value.strip()))
    return tuple(values)


@dataclass(frozen=True)
class ServerSettings:
    data_dir: Path = field(
        default_factory=lambda: _env_path(
            "MYCODE_SERVER_DATA_DIR", SERVER_ROOT / "data"
        )
    )
    cookie_name: str = "mycode_user"
    cookie_secure: bool = field(
        default_factory=lambda: _env_bool("MYCODE_COOKIE_SECURE", False)
    )
    cookie_max_age_seconds: int = 60 * 60 * 24 * 14
    upload_zip_limit_bytes: int = field(
        default_factory=lambda: _env_int(
            "MYCODE_UPLOAD_ZIP_LIMIT_BYTES", 150 * 1024 * 1024
        )
    )
    workspace_limit_bytes: int = field(
        default_factory=lambda: _env_int(
            "MYCODE_WORKSPACE_LIMIT_BYTES", 2 * 1024 * 1024 * 1024
        )
    )
    workspace_file_limit: int = field(
        default_factory=lambda: _env_int("MYCODE_WORKSPACE_FILE_LIMIT", 20_000)
    )
    text_preview_limit_bytes: int = field(
        default_factory=lambda: _env_int(
            "MYCODE_TEXT_PREVIEW_LIMIT_BYTES", 1024 * 1024
        )
    )
    sandbox_image: str = field(
        default_factory=lambda: os.getenv(
            "MYCODE_SANDBOX_IMAGE", "mycode-sandbox:dev"
        )
    )
    sandbox_max_active: int = field(
        default_factory=lambda: _env_int("SANDBOX_MAX_ACTIVE", 2)
    )
    sandbox_queue_max: int = field(
        default_factory=lambda: _env_int("SANDBOX_QUEUE_MAX", 20)
    )
    sandbox_memory_limit: str = field(
        default_factory=lambda: os.getenv("SANDBOX_MEMORY_LIMIT", "640m")
    )
    sandbox_memory_swap_limit: str = field(
        default_factory=lambda: os.getenv("SANDBOX_MEMORY_SWAP_LIMIT", "1g")
    )
    sandbox_cpus: float = field(
        default_factory=lambda: _env_float("SANDBOX_CPUS", 1.0)
    )
    sandbox_pids_limit: int = field(
        default_factory=lambda: _env_int("SANDBOX_PIDS_LIMIT", 256)
    )
    sandbox_idle_ttl_seconds: int = field(
        default_factory=lambda: _env_int("SANDBOX_IDLE_TTL_SECONDS", 7200)
    )
    runtime_sweep_interval_seconds: int = field(
        default_factory=lambda: _env_int("RUNTIME_SWEEP_INTERVAL_SECONDS", 60)
    )
    session_retention_seconds: int = field(
        default_factory=lambda: _env_int("SESSION_RETENTION_SECONDS", 1209600)
    )
    session_cleanup_interval_seconds: int = field(
        default_factory=lambda: _env_int("SESSION_CLEANUP_INTERVAL_SECONDS", 3600)
    )
    docker_command: str = field(
        default_factory=lambda: os.getenv("MYCODE_DOCKER_COMMAND", "docker")
    )
    docker_host_alias: str = field(
        default_factory=lambda: os.getenv(
            "MYCODE_DOCKER_HOST_ALIAS", "host.docker.internal"
        )
    )
    relay_base_url_for_sandbox: str = field(
        default_factory=lambda: os.getenv(
            "MYCODE_RELAY_BASE_URL_FOR_SANDBOX",
            "http://host.docker.internal:8000/mycode/api/relay/v1",
        ).rstrip("/")
    )
    relay_token: str = field(
        default_factory=lambda: os.getenv("MYCODE_RELAY_TOKEN")
        or secrets.token_urlsafe(32),
        repr=False,
    )
    provider_api_key: str | None = field(
        default_factory=lambda: os.getenv("MYCODE_PROVIDER_API_KEY"),
        repr=False,
    )
    provider_base_url: str = field(
        default_factory=lambda: os.getenv(
            "MYCODE_PROVIDER_BASE_URL", "https://api.openai.com/v1"
        ).rstrip("/")
    )
    model: str = field(
        default_factory=lambda: os.getenv("MYCODE_MODEL", "gpt-5.4")
    )
    sandbox_optional_env: tuple[tuple[str, str], ...] = field(
        default_factory=_optional_sandbox_env
    )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "web-v2.sqlite3"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    def ensure_directories(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
