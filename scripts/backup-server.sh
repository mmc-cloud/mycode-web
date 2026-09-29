#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/root}"
WEB_SERVICE="${WEB_SERVICE:-mycode-web}"
SYNCTHING_CONTAINER="${SYNCTHING_CONTAINER:-syncthing}"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: backup-server.sh [--dry-run] [--output-dir DIR]

Create a private migration backup containing MyCode Web production state,
Let's Encrypt state when present, and Syncthing state when present.

Options:
  --dry-run         Show what would be backed up without stopping services.
  --output-dir DIR  Write the backup to DIR (default: /root).
  -h, --help        Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --output-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --output-dir requires a value" >&2; exit 2; }
            BACKUP_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[ "$(id -u)" -eq 0 ] || {
    echo "ERROR: run this script as root" >&2
    exit 1
}

required_paths=(
    "opt/mycode-web/.env"
    "opt/mycode-web/data"
)

for rel in "${required_paths[@]}"; do
    [ -e "/$rel" ] || {
        echo "ERROR: required production state is missing: /$rel" >&2
        exit 1
    }
done

backup_paths=("${required_paths[@]}")

if [ -d /etc/letsencrypt ]; then
    backup_paths+=("etc/letsencrypt")
fi

if [ -d /home/syncthing_data ]; then
    backup_paths+=("home/syncthing_data")
fi

echo "MyCode server migration backup"
echo "Host: $(hostname)"
echo "Output directory: $BACKUP_DIR"
echo
echo "Included paths:"
printf '  /%s\n' "${backup_paths[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    echo "Dry run only; no services were stopped and no archive was created."
    echo
    du -sh "${backup_paths[@]/#//}" 2>/dev/null || true
    exit 0
fi

install -d -m 0700 "$BACKUP_DIR"

web_was_active=0
syncthing_was_running=0

restore_services() {
    local rc=$?

    if [ "$syncthing_was_running" -eq 1 ]; then
        docker start "$SYNCTHING_CONTAINER" >/dev/null 2>&1 || true
    fi

    if [ "$web_was_active" -eq 1 ]; then
        systemctl start "$WEB_SERVICE" >/dev/null 2>&1 || true
    fi

    return "$rc"
}
trap restore_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if systemctl is-active --quiet "$WEB_SERVICE"; then
    web_was_active=1
    echo
    echo "==> Stopping $WEB_SERVICE for a consistent SQLite/session snapshot"
    systemctl stop "$WEB_SERVICE"
fi

if command -v docker >/dev/null 2>&1 &&    docker inspect "$SYNCTHING_CONTAINER" >/dev/null 2>&1 &&    [ "$(docker inspect -f '{{.State.Running}}' "$SYNCTHING_CONTAINER")" = "true" ]; then
    syncthing_was_running=1
    echo "==> Stopping $SYNCTHING_CONTAINER for a consistent Syncthing snapshot"
    docker stop "$SYNCTHING_CONTAINER" >/dev/null
fi

stamp="$(date +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/mycode-server-backup-$stamp.tar.gz"
checksum="$archive.sha256"

echo "==> Creating $archive"
tar --numeric-owner -C / -czpf "$archive" "${backup_paths[@]}"

echo "==> Writing SHA256"
sha256sum "$archive" | tee "$checksum"

echo
echo "Backup complete."
echo "Archive:  $archive"
echo "Checksum: $checksum"
echo "Size:     $(du -h "$archive" | awk '{print $1}')"
echo
echo "Contents:"
printf '  /%s\n' "${backup_paths[@]}"
echo
echo "This archive contains secrets and private data. Do not commit or upload it to a public repository."
