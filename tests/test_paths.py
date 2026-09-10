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
    assert "location ^~ /.well-known/acme-challenge/" in content
    assert "root /var/www/certbot;" in content
    assert "default_type text/plain;" in content
    assert "try_files $uri =404;" in content
    assert "location / {\n        return 301 https://$host$request_uri;\n    }" in content
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


def test_workspace_zip_upload_label_explains_extraction_destination() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/SessionApp.vue"
    ).read_text(encoding="utf-8")

    assert "Extract ZIP" in source
    assert 'title="Upload ZIP and extract into Workspace root (/)"' in source
    assert 'accept=".zip"' in source


def test_frontend_send_is_disabled_during_request_or_active_agent_turn() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/SessionApp.vue"
    ).read_text(encoding="utf-8")

    assert "const sendingSessionIds = reactive(new Set())" in source
    assert "sendingSessionIds.has(currentSession.value?.id)" in source
    assert "if (sendDisabled.value) return" in source
    assert "const sessionId = currentSession.value?.id" in source
    assert "const token = generation" in source
    assert 'scoped("/message", sessionId)' in source
    assert "generation !== token || currentSession.value?.id !== sessionId" in source
    assert "sendingSessionIds.add(sessionId)" in source
    assert "sendingSessionIds.delete(sessionId)" in source
    assert "Boolean(currentSession.value?.active_turn_id)" in source
    assert (
        "currentSession?.runtime_status === 'queued' && "
        "currentSession?.active_turn_id"
    ) in source


def test_frontend_uses_session_bootstrap_cursor_for_sse() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/SessionApp.vue"
    ).read_text(encoding="utf-8")

    assert "const history = metadata.then((bootstrap) =>" in source
    assert "loadConsole(sessionId, token, bootstrap.event_cursor)" in source
    assert "async function loadConsole(sessionId, token, bootstrapCursor)" in source
    assert "connectEvents(sessionId, token, bootstrapCursor)" in source
    assert "connectEvents(sessionId, token, result.event_cursor)" not in source
    assert "currentSession.value?.id !== sessionId" in source


def test_terminal_clipboard_ux_uses_xterm_paste_and_cleans_listeners() -> None:
    terminal = (
        Path(__file__).resolve().parents[1] / "frontend/src/TerminalPanel.vue"
    ).read_text(encoding="utf-8")

    assert "navigator.clipboard.writeText(selection)" in terminal
    assert "navigator.clipboard.readText()" in terminal
    assert "terminal.paste(text)" in terminal
    assert 'const clipboardNotice = ref("")' in terminal
    assert "let clipboardNoticeTimer = null" in terminal
    assert "clipboardNoticeTimer = window.setTimeout" in terminal
    assert "clearClipboardNotice()" in terminal
    assert 'v-if="clipboardNotice"' in terminal
    assert "clipboardNotice.value = message" in terminal
    assert "notice.value = message" not in terminal
    assert "if (generation === token) clearClipboardNotice()" in terminal
    assert "if (generation !== token || !terminal) return" in terminal
    assert 'event.key.toLowerCase()' in terminal
    assert 'if (key === "c") void copySelection()' in terminal
    assert 'else void pasteClipboard()' in terminal
    assert 'addEventListener("pointerup", handlePointerUp)' in terminal
    assert 'addEventListener("contextmenu", handleContextMenu)' in terminal
    assert 'removeEventListener("pointerup", handlePointerUp)' in terminal
    assert 'removeEventListener("contextmenu", handleContextMenu)' in terminal
    assert 'addEventListener("keydown", handleKeyDown, true)' in terminal
    assert 'removeEventListener("keydown", handleKeyDown, true)' in terminal
    assert "socket.send(text)" not in terminal


def test_execution_group_completed_turns_keep_user_toggle_state() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/SessionApp.vue"
    ).read_text(encoding="utf-8")

    assert "const explicitExpanded = expanded[group.key]" in source
    assert "expandedGroups.value[group.key] = !group.expanded" in source
    assert "if (ACTIVE_TURN_STATUSES.includes(previous) && status === \"idle\")" in source
    assert "expandedGroups.value[turnId] = false" in source
    assert "expandedGroups.value = {}" in source


def test_permission_frontend_uses_scoped_decisions_and_runtime_session_hint() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/SessionApp.vue"
    ).read_text(encoding="utf-8")

    assert "async function resolvePermission(decision)" in source
    assert "body: JSON.stringify({ decision, request_id: permission.value?.request_id })" in source
    assert "resolvePermission('deny')" in source
    assert "resolvePermission('once')" in source
    assert "resolvePermission('task')" in source
    assert "resolvePermission('session')" in source
    assert "Runtime 重启后失效" in source
    assert "async function resolveMcpTrust(approved)" in source
    assert "request_id: pendingMcpTrust.value.request_id" in source
    assert "resolvePermission(false)" not in source
    assert "resolvePermission(true)" not in source


def test_frontend_clears_pending_interactions_on_terminal_runtime_events() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/SessionApp.vue"
    ).read_text(encoding="utf-8")

    assert "function clearPendingInteractions()" in source
    assert 'data.status === "error" || data.status === "stopped"' in source
    assert 'eventSource.addEventListener("runtime_expired"' in source
    assert "clearPendingInteractions()" in source
    assert "permission.value = null" in source
    assert "pendingMcpTrust.value = null" in source
    runtime_error_handler = source.split(
        'eventSource.addEventListener("runtime_error"', 1
    )[1].split("\n  })", 1)[0]
    assert "showError" in runtime_error_handler
    assert "clearPendingInteractions()" not in runtime_error_handler


def test_frontend_uses_dom_dialogs_for_session_and_workspace_mutations() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "frontend/src/SessionApp.vue").read_text(encoding="utf-8")
    dialog = (root / "frontend/src/AppDialog.vue").read_text(encoding="utf-8")

    assert 'import AppDialog from "./AppDialog.vue"' in source
    assert "window.confirm" not in source
    assert "window.prompt" not in source
    assert "window.alert" not in source
    assert 'type: "delete-session"' in source
    assert 'type: "delete-entry"' in source
    assert "target.path" in source
    assert "target.sessionId" in source
    assert "dialog.busy = true" in source
    assert "detail: isDirectory ? \"该目录及其中的全部内容将被递归删除。\" : \"\"" in source
    assert "项目 Workspace 文件不会受到影响。" in source
    assert "dialog.inputValue.trim()" in source
    assert "role=\"dialog\"" in dialog
    assert "aria-modal=\"true\"" in dialog
    assert "@keydown.esc.stop.prevent" in dialog
    assert "@keydown.tab=\"handleTab\"" in dialog
    assert "cancelButton.value?.focus()" in dialog
