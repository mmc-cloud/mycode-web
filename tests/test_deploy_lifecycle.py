"""Execute deployment control flow with shell stubs; never invoke Docker."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/deploy-server.sh").read_text(encoding="utf-8")
BASH = ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt"
        else shutil.which("bash"))


def run_shell(source: str) -> subprocess.CompletedProcess[str]:
    if not BASH or not Path(BASH).is_file():
        pytest.skip("Bash required")
    return subprocess.run([BASH, "-c", source], text=True, capture_output=True)


@pytest.mark.parametrize("changed,guard,old,new,reexec", [
    (True, "0", "old", "new", True),
    (True, "1", "old", "new", False),
    (False, "0", "old", "new", False),
    (True, "0", "same", "same", False),
])
def test_reexec_guard(changed, guard, old, new, reexec):
    block = SCRIPT.split('if [ "$WEB_OLD" != "$WEB_NEW" ] &&', 1)[1]
    block = 'if [ "$WEB_OLD" != "$WEB_NEW" ] &&' + block.split("\nMYCODE_CHANGED=0", 1)[0]
    # Stub exec records the target and exported guard, then terminates the shell.
    result = run_shell(f'''
set -Eeuo pipefail
WEB_OLD={old}; WEB_NEW={new}; WEB_DIR=/updated
MYCODE_DEPLOY_REEXECUTED={guard}
git_in() {{
    [ "$*" = '/updated diff --name-only {old} {new} -- scripts/deploy-server.sh' ]
    {'echo scripts/deploy-server.sh' if changed else ':'}
}}
exec() {{ echo "EXEC:$*:GUARD=$MYCODE_DEPLOY_REEXECUTED"; exit 0; }}
{block}
echo CONTINUED
''')
    assert result.returncode == 0, result.stderr
    assert ("EXEC:bash /updated/scripts/deploy-server.sh:GUARD=1" in result.stdout) == reexec
    assert ("CONTINUED" in result.stdout) != reexec
    assert SCRIPT.index('WEB_NEW="$(') < SCRIPT.index(block.strip()) < SCRIPT.index("MYCODE_CHANGED=0")


@pytest.mark.parametrize("exists,sync_ok", [(True, True), (False, True), (False, False)])
def test_web_python_preparation_and_sync_once(exists, sync_ok):
    functions = SCRIPT.split("sync_web_dependencies()", 1)[1].split("cleanup_sandbox_candidate()", 1)[0]
    result = run_shell(f'''
set -Eeuo pipefail
WEB_DIR=/web; WEB_VENV=/venv; WEB_UV=/uv
WEB_PYTHON={'/bin/bash' if exists else '/missing-python'}
WEB_DEPENDENCIES_SYNCED=0
runuser() {{ echo "SYNC:$*"; {'WEB_PYTHON=/bin/bash' if sync_ok else ':'}; }}
sync_web_dependencies(){functions}
ensure_web_python
sync_web_dependencies
''')
    if sync_ok:
        assert result.returncode == 0, result.stderr
        assert result.stdout.count("SYNC:") == 1
        assert "sync --python 3.11" in result.stdout
        assert "UV_PROJECT_ENVIRONMENT='/venv'" in result.stdout
    else:
        assert result.returncode != 0
        assert "Web Python is not executable" in result.stdout


def test_smoke_uses_prepared_web_python():
    assert 'WEB_VENV="/home/mycode/.venvs/mycode-web"' in SCRIPT
    assert 'WEB_PYTHON="$WEB_VENV/bin/python"' in SCRIPT
    assert 'WEB_UV="/home/mycode/.local/bin/uv"' in SCRIPT
    assert "python3 ./scripts/smoke-sandbox-runtime.py" not in SCRIPT
    build = SCRIPT.index('    bash ./scripts/build-sandbox.sh')
    prepare = SCRIPT.index("    ensure_web_python", build)
    smoke = SCRIPT.index('    "$WEB_PYTHON" ./scripts/smoke-sandbox-runtime.py', prepare)
    assert build < prepare < smoke < SCRIPT.index("    docker tag", smoke)


def test_sse_runtime_status_invalidates_context_only_outside_idle():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    source = (ROOT / "frontend/src/SessionApp.vue").read_text(encoding="utf-8")
    handler = "function applyRuntimeStatus(data) {" + source.split(
        "function applyRuntimeStatus(data) {", 1
    )[1].split("\nfunction connectEvents", 1)[0]
    result = subprocess.run([node, "--input-type=module", "-e", '''
import assert from 'node:assert/strict';
const currentSession = { value: {} };
const contextStatus = { value: null };
const compactFeedback = { value: '' };
function clearPendingInteractions() {}
''' + handler + '''
for (const status of ['idle', 'starting', 'queued', 'running',
  'waiting_permission', 'waiting_mcp_trust', 'stopping', 'stopped', 'error', 'unknown']) {
  const snapshot = { estimated_input_tokens: 123 };
  contextStatus.value = snapshot;
  compactFeedback.value = 'Compact complete';
  applyRuntimeStatus({ status }); // SSE from another tab or replay, without turn_id
  assert.equal(contextStatus.value, status === 'idle' ? snapshot : null);
  assert.equal(compactFeedback.value, status === 'idle' ? 'Compact complete' : '');
}
'''], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
