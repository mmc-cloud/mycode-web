from pathlib import Path

from fastapi.testclient import TestClient

from app.config import ServerSettings
from app.main import create_app
from app.paths import API_BASE_PATH, RELAY_BASE_PATH, WEB_BASE_PATH


class NoopLauncher:
    async def launch(self, session_id, workspace, mycode_state):
        raise AssertionError("Sandbox launch is not expected in path tests.")


def test_web_paths_are_single_source_for_api_and_sandbox_relay(tmp_path: Path) -> None:
    settings = ServerSettings(data_dir=tmp_path)

    assert WEB_BASE_PATH == "/web"
    assert API_BASE_PATH == "/web/api"
    assert RELAY_BASE_PATH == "/web/api/relay/v1"
    assert settings.relay_base_url_for_sandbox.endswith(RELAY_BASE_PATH)


def test_production_cookie_setting_preserves_secure_flag(tmp_path: Path) -> None:
    settings = ServerSettings(
        data_dir=tmp_path,
        cookie_secure=True,
    )
    app = create_app(settings, launcher=NoopLauncher())

    with TestClient(app) as client:
        response = client.get(f"{API_BASE_PATH}/sessions")

    assert response.status_code == 200
    assert "Path=/web" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_local_cookie_setting_leaves_secure_flag_off(tmp_path: Path) -> None:
    settings = ServerSettings(data_dir=tmp_path)
    app = create_app(settings, launcher=NoopLauncher())

    with TestClient(app) as client:
        response = client.get(f"{API_BASE_PATH}/sessions")

    assert response.status_code == 200
    assert "Path=/web" in response.headers["set-cookie"]
    assert "Secure" not in response.headers["set-cookie"]


def test_nginx_template_keeps_web_api_ws_sse_and_legacy_redirects() -> None:
    template = Path(__file__).resolve().parents[1] / "deploy/nginx/mycode.conf"
    content = template.read_text(encoding="utf-8")

    assert "map $http_upgrade $connection_upgrade" in content
    assert "location /web/api/" in content
    assert "proxy_pass http://127.0.0.1:8000;" in content
    assert "proxy_set_header Upgrade $http_upgrade;" in content
    assert "proxy_set_header Connection $connection_upgrade;" in content
    assert "proxy_buffering off;" in content
    assert "location /web/" in content
    assert "alias /opt/mycode-web/frontend/dist/;" in content
    assert "return 301 https://$host$request_uri;" in content
    assert "rewrite ^/mycode/(.*)$ /web/$1 permanent;" in content


def test_frontend_uses_web_base_and_protocol_aware_websocket_builder() -> None:
    root = Path(__file__).resolve().parents[1]
    api = (root / "frontend/src/api.js").read_text(encoding="utf-8")
    vite = (root / "frontend/vite.config.js").read_text(encoding="utf-8")

    assert 'window.location.protocol === "https:" ? "wss:" : "ws:"' in api
    assert "`${protocol}//${window.location.host}${API_BASE}${path}`" in api
    assert 'base: `${WEB_BASE_PATH}/`' in vite
    assert 'proxy: {' in vite
    assert "ws: true" in vite
