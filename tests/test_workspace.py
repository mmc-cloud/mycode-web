from io import BytesIO
from pathlib import Path
import stat
from unittest.mock import call, patch
import zipfile

import pytest

from app.config import ServerSettings
from app.services.workspace import WorkspaceError, WorkspaceLimitError, WorkspaceService


def service(tmp_path: Path, **limits: int) -> WorkspaceService:
    return WorkspaceService(
        ServerSettings(
            data_dir=tmp_path / "data",
            workspace_limit_bytes=limits.get("workspace_limit_bytes", 1024),
            workspace_file_limit=limits.get("workspace_file_limit", 10),
            upload_zip_limit_bytes=limits.get("upload_zip_limit_bytes", 1024),
        )
    )


def test_default_workspace_limits_match_web_demo_contract(tmp_path: Path) -> None:
    settings = ServerSettings(data_dir=tmp_path)
    assert settings.upload_zip_limit_bytes == 150 * 1024 * 1024
    assert settings.workspace_limit_bytes == 2 * 1024 * 1024 * 1024
    assert settings.workspace_file_limit == 20_000


def test_session_directories_prepare_shared_workspace_and_private_state(
    tmp_path: Path,
) -> None:
    workspace_service = service(tmp_path)

    with patch("app.services.workspace.os.chmod") as chmod:
        workspace, mycode_state = workspace_service.ensure_session_directories(
            "session"
        )

    assert call(workspace, 0o2775) in chmod.call_args_list
    assert call(mycode_state, 0o700) in chmod.call_args_list


def test_sessions_for_one_user_share_workspace_but_not_mycode_state(
    tmp_path: Path,
) -> None:
    workspace_service = service(tmp_path)

    workspace_a, state_a = workspace_service.ensure_session_directories(
        "session-a", user_id="user-a"
    )
    workspace_b, state_b = workspace_service.ensure_session_directories(
        "session-b", user_id="user-a"
    )

    assert workspace_a == workspace_b == workspace_service.workspace_dir("user-a")
    assert state_a != state_b
    (workspace_a / "shared.txt").write_text("shared", encoding="utf-8")
    assert workspace_service.read_text(
        "session-b", "shared.txt", user_id="user-a"
    ) == "shared"


def test_different_users_have_isolated_workspaces(tmp_path: Path) -> None:
    workspace_service = service(tmp_path)
    workspace_a, _ = workspace_service.ensure_session_directories(
        "session-a", user_id="user-a"
    )
    workspace_b, _ = workspace_service.ensure_session_directories(
        "session-b", user_id="user-b"
    )
    (workspace_a / "private.txt").write_text("A", encoding="utf-8")

    assert workspace_a != workspace_b
    with pytest.raises(FileNotFoundError):
        workspace_service.read_text("session-b", "private.txt", user_id="user-b")


def test_deleting_session_data_keeps_user_workspace(tmp_path: Path) -> None:
    workspace_service = service(tmp_path)
    workspace, state = workspace_service.ensure_session_directories(
        "session-a", user_id="user-a"
    )
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    (state / "private.json").write_text("{}", encoding="utf-8")

    workspace_service.delete_session_data("session-a")

    assert workspace.exists()
    assert not state.parent.exists()
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "keep"


def zip_bytes(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> BytesIO:
    result = BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    result.seek(0)
    return result


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/absolute.txt", "C:/windows.txt", "safe/../../escape.txt"],
)
def test_zip_rejects_traversal_and_absolute_paths(tmp_path: Path, name: str) -> None:
    workspace = service(tmp_path)
    with pytest.raises(WorkspaceError, match="Unsafe workspace path"):
        workspace.save_upload(
            "session", "bad.zip", zip_bytes([(name, b"bad")]), archive=True
        )


def test_zip_rejects_symbolic_links(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    workspace = service(tmp_path)
    with pytest.raises(WorkspaceError, match="symbolic links"):
        workspace.save_upload(
            "session", "link.zip", zip_bytes([(link, b"../outside")]), archive=True
        )


def test_zip_upload_limit_is_enforced_while_copying(tmp_path: Path) -> None:
    workspace = service(tmp_path, upload_zip_limit_bytes=10)
    with pytest.raises(WorkspaceLimitError, match="Upload size"):
        workspace.save_upload(
            "session", "large.zip", BytesIO(b"x" * 11), archive=True
        )


def test_workspace_size_limit_is_enforced_during_zip_extraction(tmp_path: Path) -> None:
    workspace = service(tmp_path, workspace_limit_bytes=10)
    with pytest.raises(WorkspaceLimitError, match="size limit"):
        workspace.save_upload(
            "session",
            "large.zip",
            zip_bytes([("large.txt", b"x" * 11)]),
            archive=True,
        )


def test_workspace_file_limit_is_enforced_during_zip_extraction(tmp_path: Path) -> None:
    workspace = service(tmp_path, workspace_file_limit=2)
    with pytest.raises(WorkspaceLimitError, match="file count"):
        workspace.save_upload(
            "session",
            "many.zip",
            zip_bytes(
                [("one.txt", b"1"), ("two.txt", b"2"), ("three.txt", b"3")]
            ),
            archive=True,
        )


def test_zip_rejects_file_parent_conflicts(tmp_path: Path) -> None:
    workspace = service(tmp_path)
    with pytest.raises(WorkspaceError, match="conflicting file paths"):
        workspace.save_upload(
            "session",
            "conflict.zip",
            zip_bytes([("parent", b"file"), ("parent/child.txt", b"child")]),
            archive=True,
        )


def test_existing_workspace_symlink_cannot_escape_boundary(tmp_path: Path) -> None:
    workspace = service(tmp_path)
    root, _ = workspace.ensure_session_directories("session")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This environment cannot create symbolic links.")
    with pytest.raises(WorkspaceError, match="Symbolic links"):
        workspace.resolve_file("session", "link/secret.txt", must_exist=False)


def test_delete_file_directory_and_reject_root_traversal_and_unlink_symlink(
    tmp_path: Path,
) -> None:
    workspace = service(tmp_path)
    root, _ = workspace.ensure_session_directories("session")
    (root / "file.txt").write_text("file", encoding="utf-8")
    (root / "folder").mkdir()
    (root / "folder" / "nested.txt").write_text("nested", encoding="utf-8")

    workspace.delete_path("session", "file.txt")
    workspace.delete_path("session", "folder")
    assert not (root / "file.txt").exists()
    assert not (root / "folder").exists()
    for unsafe in ("", ".", "..", "../outside"):
        with pytest.raises(WorkspaceError):
            workspace.delete_path("session", unsafe)

    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host.")
    workspace.delete_path("session", "link.txt")
    assert not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_stats_tree_and_directory_delete_prune_symlinks(tmp_path: Path) -> None:
    workspace = service(tmp_path)
    root, _ = workspace.ensure_session_directories("session")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep", encoding="utf-8")
    folder = root / "folder"
    folder.mkdir()
    (folder / "local.txt").write_text("local", encoding="utf-8")
    try:
        (folder / "outside-link").symlink_to(outside, target_is_directory=True)
        (root / "dangling").symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host.")

    stats = workspace.stats(root)
    assert stats.file_count == 3
    assert {entry["name"] for entry in workspace.tree("session")} == {
        "dangling",
        "folder",
    }
    workspace.delete_path("session", "dangling")
    workspace.delete_path("session", "folder")

    assert not folder.exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep"


def test_tree_hides_generated_directories_but_stats_still_count_them(
    tmp_path: Path,
) -> None:
    workspace = service(tmp_path)
    root, _ = workspace.ensure_session_directories("session")
    visible_files = (
        root / "src" / "main.py",
        root / "tests" / "test_main.py",
        root / "pyproject.toml",
        root / "uv.lock",
    )
    hidden_files = tuple(
        root / directory / "nested" / "generated.txt"
        for directory in (
            ".venv",
            ".git",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
        )
    )
    for path in visible_files + hidden_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    tree = workspace.tree("session")

    assert {entry["name"] for entry in tree} == {
        "pyproject.toml",
        "src",
        "tests",
        "uv.lock",
    }
    assert {
        child["name"]
        for entry in tree
        if entry["kind"] == "directory"
        for child in entry["children"]
    } == {"main.py", "test_main.py"}
    stats = workspace.stats(root)
    assert stats.file_count == len(visible_files) + len(hidden_files)
    assert stats.total_bytes == sum(
        path.stat().st_size for path in visible_files + hidden_files
    )
