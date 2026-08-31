from io import BytesIO
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient

from app.config import ServerSettings
from app.main import create_app


class NoopLauncher:
    async def launch(self, session_id, workspace, mycode_state):
        raise AssertionError("Sandbox launch is not expected in API tests.")


def create_test_app(tmp_path: Path):
    return create_app(configured_settings(tmp_path), launcher=NoopLauncher())


def configured_settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        data_dir=tmp_path / "data",
        relay_token="internal-test-token",
        provider_api_key=None,
    )


def test_cookie_user_is_created_and_restored_with_one_session(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        first = client.post("/mycode/api/session")
        first_user_id = client.cookies.get("mycode_user")
        second = client.get("/mycode/api/session")
        second_user_id = client.cookies.get("mycode_user")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]
    assert first_user_id == second_user_id
    assert first_user_id is not None
    for cookie in (first.headers["set-cookie"], second.headers["set-cookie"]):
        assert "mycode_user=" in cookie
        assert "Max-Age=1209600" in cookie
        assert "expires=" in cookie.lower()
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/mycode" in cookie


def test_profile_workspace_tree_content_and_downloads(tmp_path: Path) -> None:
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        client.post("/mycode/api/session")
        profile = client.post(
            "/mycode/api/profile", json={"display_name": "Demo User"}
        )
        upload = client.post(
            "/mycode/api/files/upload",
            data={"archive": "false", "relative_path": "src/hello.py"},
            files={"upload": ("hello.py", b"print('hello')\n", "text/x-python")},
        )
        tree = client.get("/mycode/api/files/tree")
        content = client.get(
            "/mycode/api/files/content", params={"path": "src/hello.py"}
        )
        download = client.get(
            "/mycode/api/files/download", params={"path": "src/hello.py"}
        )
        workspace_zip = client.get("/mycode/api/workspace/download")

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
        client.post("/mycode/api/session")
        response = client.get(
            "/mycode/api/files/content", params={"path": "../outside.txt"}
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "Unsafe workspace path."


def test_zip_upload_extracts_safe_project(tmp_path: Path) -> None:
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("project/README.md", "hello")
    app = create_test_app(tmp_path)
    with TestClient(app) as client:
        client.post("/mycode/api/session")
        response = client.post(
            "/mycode/api/files/upload",
            data={"archive": "true"},
            files={"upload": ("project.zip", archive_bytes.getvalue(), "application/zip")},
        )
        content = client.get(
            "/mycode/api/files/content", params={"path": "project/README.md"}
        )
    assert response.status_code == 201
    assert content.json()["content"] == "hello"
