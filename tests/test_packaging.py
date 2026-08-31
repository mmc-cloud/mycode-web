from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_excludes_private_and_runtime_web_files() -> None:
    patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".env",
        "**/.env",
        ".venv",
        "**/.venv",
        "data",
        "frontend/node_modules",
        "frontend/dist",
        "**/__pycache__",
        "**/.pytest_cache",
        "**/*.pyc",
        "*.zip",
    } <= patterns


def test_gitignore_excludes_private_and_runtime_web_files() -> None:
    patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".env",
        "**/.venv/",
        "data/",
        "frontend/node_modules/",
        "frontend/dist/",
        "**/__pycache__/",
        "**/*.pyc",
        "**/.pytest_cache/",
        "*.zip",
    } <= patterns


def test_dockerfile_uses_named_mycode_context_and_root_lock() -> None:
    dockerfile = (ROOT / "docker/Dockerfile.sandbox").read_text(
        encoding="utf-8"
    )
    dependency_copy = dockerfile.index(
        "COPY --from=mycode README.md pyproject.toml uv.lock ./"
    )
    dependency_sync = dockerfile.index(
        "uv sync --locked --no-dev --no-install-project"
    )
    source_copy = dockerfile.index(
        "COPY --from=mycode mycode /opt/mycode-source/mycode"
    )
    assert dependency_copy < dependency_sync < source_copy
    assert '"httpx>=' not in dockerfile
    assert '"openai>=' not in dockerfile
    assert "pip install --no-deps" not in dockerfile


def test_build_scripts_accept_external_mycode_source() -> None:
    powershell = (ROOT / "scripts/build-sandbox.ps1").read_text(encoding="utf-8")
    shell = (ROOT / "scripts/build-sandbox.sh").read_text(encoding="utf-8")
    for script in (powershell, shell):
        assert "--build-context" in script
        assert "mycode=" in script
        assert "../mycode-project" not in script
        assert "/opt/mycode" not in script
