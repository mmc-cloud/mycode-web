from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import BinaryIO
import zipfile

from app.config import ServerSettings


class WorkspaceError(ValueError):
    pass


class WorkspaceLimitError(WorkspaceError):
    pass


GENERATED_DIRECTORY_NAMES = frozenset(
    {
        ".venv",
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
    }
)


@dataclass(frozen=True)
class WorkspaceStats:
    file_count: int
    total_bytes: int


class WorkspaceService:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    def session_dir(self, session_id: str) -> Path:
        if not session_id or any(character in session_id for character in "/\\"):
            raise WorkspaceError("Invalid session identifier.")
        path = (self.settings.sessions_dir / session_id).resolve()
        _require_within(path, self.settings.sessions_dir.resolve())
        return path

    def ensure_session_directories(self, session_id: str) -> tuple[Path, Path]:
        session_dir = self.session_dir(session_id)
        workspace = session_dir / "workspace"
        mycode_state = session_dir / "mycode_state"
        workspace.mkdir(parents=True, exist_ok=True)
        mycode_state.mkdir(parents=True, exist_ok=True)
        return workspace, mycode_state

    def delete_session_data(self, session_id: str) -> None:
        """Delete one session tree without following links outside the root."""
        if not session_id or any(character in session_id for character in "/\\"):
            raise WorkspaceError("Invalid session identifier.")
        root = self.settings.sessions_dir.resolve()
        target = root / session_id
        if target.parent != root:
            raise WorkspaceError("Path escapes session root.")
        if _is_link(target):
            if target.is_dir() and not target.is_symlink():
                target.rmdir()
            else:
                target.unlink()
            return
        if target.exists():
            shutil.rmtree(target)

    def resolve_file(
        self, session_id: str, relative_path: str, *, must_exist: bool = True
    ) -> Path:
        workspace, _ = self.ensure_session_directories(session_id)
        relative = _safe_relative_path(relative_path)
        target = workspace.joinpath(*relative.parts)
        _reject_symlink_chain(target, workspace)
        resolved = target.resolve(strict=False)
        _require_within(resolved, workspace.resolve())
        if must_exist and (not target.exists() or not target.is_file()):
            raise FileNotFoundError(relative_path)
        if _is_link(target):
            raise WorkspaceError("Symbolic links are not accessible.")
        return target

    def save_upload(
        self,
        session_id: str,
        filename: str,
        source: BinaryIO,
        *,
        archive: bool,
        relative_path: str | None = None,
    ) -> None:
        workspace, _ = self.ensure_session_directories(session_id)
        upload_dir = self.session_dir(session_id) / ".uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".zip" if archive else ".upload"
        fd, temporary_name = tempfile.mkstemp(dir=upload_dir, suffix=suffix)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            upload_limit = (
                self.settings.upload_zip_limit_bytes
                if archive
                else self.settings.workspace_limit_bytes
            )
            _copy_limited(source, temporary, upload_limit)
            if archive:
                self._extract_zip(workspace, temporary)
            else:
                destination_name = relative_path or filename
                target = self.resolve_file(
                    session_id, destination_name, must_exist=False
                )
                stats = self.stats(workspace)
                old_size = target.stat().st_size if target.exists() else 0
                old_count = 1 if target.exists() else 0
                new_size = temporary.stat().st_size
                self._require_limits(
                    stats.file_count - old_count + 1,
                    stats.total_bytes - old_size + new_size,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def tree(self, session_id: str) -> list[dict[str, object]]:
        workspace, _ = self.ensure_session_directories(session_id)
        self.stats(workspace)
        return _tree_entries(workspace, workspace)

    def read_text(self, session_id: str, relative_path: str) -> str:
        path = self.resolve_file(session_id, relative_path)
        size = path.stat().st_size
        if size > self.settings.text_preview_limit_bytes:
            raise WorkspaceLimitError("File is too large for text preview.")
        content = path.read_bytes()
        if b"\x00" in content:
            raise WorkspaceError("Binary files cannot be previewed as text.")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceError("Text preview currently requires UTF-8.") from error

    def delete_path(self, session_id: str, relative_path: str) -> None:
        """Delete one file or directory without allowing root or symlink escape."""
        workspace, _ = self.ensure_session_directories(session_id)
        relative = _safe_relative_path(relative_path)
        target = workspace.joinpath(*relative.parts)
        _reject_symlink_chain(target, workspace, allow_leaf=True)
        _require_within(target, workspace)
        if target == workspace:
            raise WorkspaceError("Workspace root cannot be deleted.")
        if _is_link(target):
            _unlink_link(target)
            return
        if not target.exists():
            raise FileNotFoundError(relative_path)
        if target.is_file():
            target.unlink()
            return
        if target.is_dir():
            _remove_tree_without_following_links(target)
            return
        raise WorkspaceError("Unsupported workspace entry type.")

    def build_workspace_zip(self, session_id: str) -> Path:
        workspace, _ = self.ensure_session_directories(session_id)
        self.stats(workspace)
        download_dir = self.session_dir(session_id) / ".downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=download_dir, suffix=".zip")
        os.close(fd)
        output = Path(name)
        try:
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(workspace.rglob("*")):
                    if _is_link(path):
                        raise WorkspaceError("Workspace contains a symbolic link.")
                    if path.is_file():
                        archive.write(path, path.relative_to(workspace).as_posix())
            return output
        except Exception:
            output.unlink(missing_ok=True)
            raise

    def stats(self, workspace: Path) -> WorkspaceStats:
        count = 0
        total = 0
        for root, directories, files in os.walk(workspace, followlinks=False):
            root_path = Path(root)
            retained_directories: list[str] = []
            for name in directories:
                path = root_path / name
                if _is_link(path):
                    count += 1
                    total += path.lstat().st_size
                    self._require_limits(count, total)
                else:
                    retained_directories.append(name)
            directories[:] = retained_directories
            for name in files:
                path = root_path / name
                count += 1
                total += path.lstat().st_size
                self._require_limits(count, total)
        return WorkspaceStats(count, total)

    def _extract_zip(self, workspace: Path, archive_path: Path) -> None:
        existing = self.stats(workspace)
        stage = Path(tempfile.mkdtemp(dir=self.session_dir(workspace.parent.name)))
        extracted: dict[PurePosixPath, int] = {}
        overwritten_size = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if _is_zip_symlink(info):
                        raise WorkspaceError("ZIP symbolic links are not allowed.")
                    relative = _safe_zip_path(info.filename)
                    if relative is None:
                        continue
                    if relative in extracted:
                        raise WorkspaceError("ZIP contains duplicate file paths.")
                    if _zip_paths_conflict(relative, extracted):
                        raise WorkspaceError("ZIP contains conflicting file paths.")
                    destination = stage.joinpath(*relative.parts)
                    _require_within(destination.resolve(strict=False), stage.resolve())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    target = workspace.joinpath(*relative.parts)
                    _reject_symlink_chain(target, workspace)
                    old_size = target.stat().st_size if target.is_file() else 0
                    if target.exists() and not target.is_file():
                        raise WorkspaceError("ZIP file conflicts with a directory.")
                    overwritten_size += old_size
                    written = 0
                    with archive.open(info, "r") as source, destination.open("wb") as sink:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            projected = (
                                existing.total_bytes
                                - overwritten_size
                                + sum(extracted.values())
                                + written
                            )
                            self._require_limits(
                                existing.file_count + len(extracted) + 1,
                                projected,
                            )
                            sink.write(chunk)
                    extracted[relative] = written

            new_files = sum(
                not workspace.joinpath(*relative.parts).is_file()
                for relative in extracted
            )
            final_size = (
                existing.total_bytes - overwritten_size + sum(extracted.values())
            )
            self._require_limits(existing.file_count + new_files, final_size)
            for relative in extracted:
                source = stage.joinpath(*relative.parts)
                target = workspace.joinpath(*relative.parts)
                _reject_symlink_chain(target, workspace)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise WorkspaceError("Invalid or unsupported ZIP archive.") from error
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _require_limits(self, file_count: int, total_bytes: int) -> None:
        if file_count > self.settings.workspace_file_limit:
            raise WorkspaceLimitError("Workspace file count limit exceeded.")
        if total_bytes > self.settings.workspace_limit_bytes:
            raise WorkspaceLimitError("Workspace size limit exceeded.")


def _copy_limited(source: BinaryIO, destination: Path, limit: int) -> None:
    total = 0
    with destination.open("wb") as sink:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return
            total += len(chunk)
            if total > limit:
                raise WorkspaceLimitError("Upload size limit exceeded.")
            sink.write(chunk)


def _safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized in {".", ".."}
        or not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise WorkspaceError("Unsafe workspace path.")
    return path


def _safe_zip_path(value: str) -> PurePosixPath | None:
    normalized = value.replace("\\", "/")
    if normalized.endswith("/"):
        normalized = normalized.rstrip("/")
        if not normalized:
            return None
        _safe_relative_path(normalized)
        return None
    return _safe_relative_path(normalized)


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _reject_symlink_chain(
    path: Path, root: Path, *, allow_leaf: bool = False
) -> None:
    root = root.resolve()
    current = root
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise WorkspaceError("Path escapes workspace.") from error
    for part in relative.parts:
        current = current / part
        if _is_link(current) and not (allow_leaf and current == path):
            raise WorkspaceError("Symbolic links are not accessible.")
        if current != path and current.exists() and not current.is_dir():
            raise WorkspaceError("A parent path is not a directory.")


def _require_within(candidate: Path, root: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise WorkspaceError("Path escapes workspace.") from error


def _tree_entries(directory: Path, root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if path.name in GENERATED_DIRECTORY_NAMES:
            continue
        relative = path.relative_to(root).as_posix()
        if _is_link(path):
            entries.append({"name": path.name, "path": relative, "kind": "symlink"})
        elif path.is_dir():
            entries.append(
                {
                    "name": path.name,
                    "path": relative,
                    "kind": "directory",
                    "children": _tree_entries(path, root),
                }
            )
        elif path.is_file():
            entries.append(
                {
                    "name": path.name,
                    "path": relative,
                    "kind": "file",
                    "size": path.stat().st_size,
                }
            )
    return entries


def _zip_paths_conflict(
    candidate: PurePosixPath, extracted: dict[PurePosixPath, int]
) -> bool:
    candidate_parents = set(candidate.parents)
    return any(
        existing in candidate_parents or candidate in set(existing.parents)
        for existing in extracted
    )


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _unlink_link(path: Path) -> None:
    """Remove a link/junction itself without touching its target."""
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        path.rmdir()


def _remove_tree_without_following_links(directory: Path) -> None:
    """Recursively remove a real directory while pruning every link leaf."""
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            if _is_link(path):
                _unlink_link(path)
            elif entry.is_dir(follow_symlinks=False):
                _remove_tree_without_following_links(path)
            else:
                path.unlink()
    directory.rmdir()
