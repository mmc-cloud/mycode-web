import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt"
        else shutil.which("bash"))


@pytest.mark.parametrize("relative_path", [
    "scripts/bootstrap-server.sh",
    "scripts/backup-server.sh",
    "scripts/deploy-server.sh",
])
def test_server_shell_scripts_parse(relative_path: str) -> None:
    if not BASH or not Path(BASH).is_file():
        pytest.skip("Bash required")
    result = subprocess.run(
        [BASH, "-n", str(ROOT / relative_path)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_backup_script_preserves_migration_state_contract() -> None:
    source = (ROOT / "scripts/backup-server.sh").read_text(encoding="utf-8")
    assert "opt/mycode-web/.env" in source
    assert "opt/mycode-web/data" in source
    assert "etc/letsencrypt" in source
    assert "home/syncthing_data" in source
    assert "umask 077" in source
    assert "tar --numeric-owner" in source
    assert "trap restore_services EXIT" in source


def test_bootstrap_enforces_sandbox_uid_gid_contract() -> None:
    source = (ROOT / "scripts/bootstrap-server.sh").read_text(encoding="utf-8")
    assert "MYCODE_UID=10001" in source
    assert "MYCODE_GID=10001" in source
