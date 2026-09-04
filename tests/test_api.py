from io import BytesIO
from pathlib import Path
import asyncio
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import ServerSettings
from app.main import create_app
from app.paths import API_BASE_PATH


class NoopLauncher:
    async def launch(self, session_id, workspace, mycode_state):
        raise AssertionError("Sandbox launch is not expected in API tests.")


class SlowLauncher:
    async def launch(self, session_id, workspace, mycode_state):
        await asyncio.sleep(5)
        raise AssertionError("The activation test should cancel startup on shutdown.")


def create_test_app(tmp_path: Path):
    return create_app(configured_settings(tmp_path), launcher=NoopLauncher())


def configured_settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        data_dir=tmp_path / "data",
        provider_api_key=None,
    )


def test_cookie_user_can_create_and_list_multiple_sessions(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        initial = client.get(f"{API_BASE_PATH}/sessions")
        first = client.post(f"{API_BASE_PATH}/sessions")
        first_user_id = client.cookies.get("mycode_user")
        second = client.post(f"{API_BASE_PATH}/sessions")
        listed = client.get(f"{API_BASE_PATH}/sessions")
        second_user_id = client.cookies.get("mycode_user")

    assert initial.json()["sessions"] == []
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert [item["id"] for item in listed.json()["sessions"]] == [
        second.json()["id"], first.json()["id"]
    ]
    assert first_user_id == second_user_id
    assert first_user_id is not None
    for cookie in (initial.headers["set-cookie"], first.headers["set-cookie"]):
        assert "mycode_user=" in cookie
        assert "Max-Age=1209600" in cookie
        assert "expires=" in cookie.lower()
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/web" in cookie


def test_legacy_mycode_api_path_is_not_registered(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/mycode/api/health")

    assert response.status_code == 404


def test_session_can_be_renamed_and_name_is_returned(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        session = client.post(f"{API_BASE_PATH}/sessions").json()
        renamed = client.patch(
            f"{API_BASE_PATH}/sessions/{session['id']}",
            json={"name": "numpy playground"},
        )
        listed = client.get(f"{API_BASE_PATH}/sessions")

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "numpy playground"
    assert listed.json()["sessions"][0]["name"] == "numpy playground"


def test_terminal_websocket_rejects_other_user_without_session_disclosure(
    tmp_path: Path,
) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as owner, TestClient(app) as stranger:
        session_id = owner.post(f"{API_BASE_PATH}/sessions").json()["id"]
        with pytest.raises(WebSocketDisconnect) as error:
            with stranger.websocket_connect(
                f"{API_BASE_PATH}/sessions/{session_id}/terminal"
            ) as socket:
                socket.receive_json()

    assert error.value.code == 1008


def test_profile_workspace_tree_content_and_downloads(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        session_id = client.post(f"{API_BASE_PATH}/sessions").json()["id"]
        base = f"{API_BASE_PATH}/sessions/{session_id}"
        profile = client.post(
            f"{API_BASE_PATH}/profile", json={"display_name": "Demo User"}
        )
        upload = client.post(
            base + "/files/upload",
            data={"archive": "false", "relative_path": "src/hello.py"},
            files={"upload": ("hello.py", b"print('hello')\n", "text/x-python")},
        )
        tree = client.get(base + "/files/tree")
        content = client.get(
            base + "/files/content", params={"path": "src/hello.py"}
        )
        download = client.get(
            base + "/files/download", params={"path": "src/hello.py"}
        )
        workspace_zip = client.get(base + "/workspace/download")

    assert profile.json() == {"display_name": "Demo User"}
    assert upload.status_code == 201
    assert tree.json()["entries"][0]["name"] == "src"
    assert content.json()["content"] == "print('hello')\n"
    assert download.content == b"print('hello')\n"
    with zipfile.ZipFile(BytesIO(workspace_zip.content)) as archive:
        assert archive.namelist() == ["src/hello.py"]


def test_file_api_rejects_path_traversal(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        session_id = client.post(f"{API_BASE_PATH}/sessions").json()["id"]
        response = client.get(
            f"{API_BASE_PATH}/sessions/{session_id}/files/content",
            params={"path": "../outside.txt"},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "Unsafe workspace path."


def test_zip_upload_extracts_safe_project(tmp_path: Path) -> None:
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("project/README.md", "hello")
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        session_id = client.post(f"{API_BASE_PATH}/sessions").json()["id"]
        base = f"{API_BASE_PATH}/sessions/{session_id}"
        response = client.post(
            base + "/files/upload",
            data={"archive": "true"},
            files={"upload": ("project.zip", archive_bytes.getvalue(), "application/zip")},
        )
        content = client.get(
            base + "/files/content", params={"path": "project/README.md"}
        )
    assert response.status_code == 201
    assert content.json()["content"] == "hello"


def test_session_ownership_and_user_workspace_sharing(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as owner, TestClient(app) as stranger:
        session_a = owner.post(f"{API_BASE_PATH}/sessions").json()["id"]
        session_b = owner.post(f"{API_BASE_PATH}/sessions").json()["id"]
        stranger_session = stranger.post(f"{API_BASE_PATH}/sessions").json()["id"]
        upload = owner.post(
            f"{API_BASE_PATH}/sessions/{session_a}/files/upload",
            data={"archive": "false"},
            files={"upload": ("only-a.txt", b"A", "text/plain")},
        )

        assert upload.status_code == 201
        assert owner.get(
            f"{API_BASE_PATH}/sessions/{session_a}/files/content",
            params={"path": "only-a.txt"},
        ).json()["content"] == "A"
        shared_file = owner.get(
            f"{API_BASE_PATH}/sessions/{session_b}/files/content",
            params={"path": "only-a.txt"},
        )
        assert shared_file.status_code == 200
        assert shared_file.json()["content"] == "A"
        assert stranger.get(
            f"{API_BASE_PATH}/sessions/{session_a}"
        ).status_code == 404
        assert owner.get(
            f"{API_BASE_PATH}/sessions/{stranger_session}"
        ).status_code == 404


def test_delete_file_directory_and_session_data(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        session_id = client.post(f"{API_BASE_PATH}/sessions").json()["id"]
        base = f"{API_BASE_PATH}/sessions/{session_id}"
        for path in ("one.txt", "folder/two.txt"):
            client.post(
                base + "/files/upload",
                data={"archive": "false", "relative_path": path},
                files={"upload": (Path(path).name, b"content", "text/plain")},
            )

        assert client.delete(base + "/files", params={"path": "one.txt"}).status_code == 200
        assert client.delete(base + "/files", params={"path": "folder"}).status_code == 200
        assert client.delete(base + "/files", params={"path": "../outside"}).status_code == 400
        session_root = app.state.services.workspace.session_dir(session_id)
        user_id = client.cookies.get("mycode_user")
        assert user_id is not None
        user_workspace = app.state.services.workspace.workspace_dir(user_id)
        assert client.delete(base).status_code == 204
        assert not session_root.exists()
        assert user_workspace.exists()
        assert client.post(f"{API_BASE_PATH}/sessions").status_code == 201
        assert client.get(base).status_code == 404


def test_console_history_api_is_owned_and_session_scoped(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as owner, TestClient(app) as stranger:
        session_a = owner.post(f"{API_BASE_PATH}/sessions").json()["id"]
        session_b = owner.post(f"{API_BASE_PATH}/sessions").json()["id"]
        app.state.services.database.append_console_event(
            session_a, "assistant", "only a"
        )
        app.state.services.database.append_console_event(
            session_b, "assistant", "only b"
        )
        snapshot_a = owner.get(
            f"{API_BASE_PATH}/sessions/{session_a}/console"
        ).json()
        snapshot_b = owner.get(
            f"{API_BASE_PATH}/sessions/{session_b}/console"
        ).json()
        assert snapshot_a["events"][0]["content"] == "only a"
        assert snapshot_b["events"][0]["content"] == "only b"
        assert snapshot_a["event_cursor"] == 0
        assert snapshot_b["event_cursor"] == 0
        assert stranger.get(
            f"{API_BASE_PATH}/sessions/{session_a}/console"
        ).status_code == 404


def test_console_snapshot_cursor_replays_event_published_before_sse(
    tmp_path: Path,
) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        session_id = client.post(f"{API_BASE_PATH}/sessions").json()["id"]
        snapshot = client.get(
            f"{API_BASE_PATH}/sessions/{session_id}/console"
        ).json()

        async def publish_then_connect():
            published = await app.state.services.events.publish(
                session_id,
                "console_event",
                console_id=1,
                kind="assistant",
                content="between snapshot and SSE",
                data={},
            )
            stream = app.state.services.events.stream(
                session_id, snapshot["event_cursor"]
            )
            replayed = await anext(stream)
            await stream.aclose()
            return published, replayed

        published, replayed = asyncio.run(publish_then_connect())

    assert replayed == published


def test_activate_api_returns_before_slow_runtime_startup(tmp_path: Path) -> None:
    app = create_app(configured_settings(tmp_path), launcher=SlowLauncher())
    with TestClient(app) as client:
        session_id = client.post(f"{API_BASE_PATH}/sessions").json()["id"]
        started = time.monotonic()
        response = client.post(
            f"{API_BASE_PATH}/sessions/{session_id}/activate"
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 202
        assert response.json() == {"status": "starting"}
        assert elapsed < 1
