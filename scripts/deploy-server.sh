#!/usr/bin/env bash
set -Eeuo pipefail

MYCODE_DIR="/opt/mycode"
WEB_DIR="/opt/mycode-web"
SERVICE="mycode-web"
HEALTH_URL="http://127.0.0.1:8000/web/api/health"

# CentOS 7 ships an old Git that does not support `git -C`.
git_in() {
    local repo="$1"
    shift
    (
        cd "$repo"
        git "$@"
    )
}

echo "==> Checking repositories"

# Refuse to deploy over tracked local changes.
# Untracked/ignored runtime files such as .env and data/ are not touched.
for repo in "$MYCODE_DIR" "$WEB_DIR"; do
    if [ -n "$(git_in "$repo" status --porcelain -uno)" ]; then
        echo "ERROR: tracked local changes exist in $repo"
        git_in "$repo" status --short
        exit 1
    fi
done

MYCODE_OLD="$(git_in "$MYCODE_DIR" rev-parse HEAD)"
WEB_OLD="$(git_in "$WEB_DIR" rev-parse HEAD)"

echo "==> Updating MyCode Core"
git_in "$MYCODE_DIR" pull --ff-only

echo "==> Updating MyCode Web"
git_in "$WEB_DIR" pull --ff-only

MYCODE_NEW="$(git_in "$MYCODE_DIR" rev-parse HEAD)"
WEB_NEW="$(git_in "$WEB_DIR" rev-parse HEAD)"

MYCODE_CHANGED=0
WEB_CHANGED=0
WEB_RUNTIME_CHANGED=0
SANDBOX_DEF_CHANGED=0
WEB_CHANGED_FILES=""

if [ "$MYCODE_OLD" != "$MYCODE_NEW" ]; then
    MYCODE_CHANGED=1
fi

if [ "$WEB_OLD" != "$WEB_NEW" ]; then
    WEB_CHANGED=1
    WEB_CHANGED_FILES="$(git_in "$WEB_DIR" diff --name-only "$WEB_OLD" "$WEB_NEW")"

    # README/docs-only changes do not require dependency sync, frontend build,
    # service restart, or Sandbox rebuild.
    if printf '%s\n' "$WEB_CHANGED_FILES" | grep -Ev '^(README\.md|docs/)' | grep -q .; then
        WEB_RUNTIME_CHANGED=1
    fi

    # Web repo also owns the Sandbox build definition.
    if printf '%s\n' "$WEB_CHANGED_FILES" | \
        grep -Eq '^(docker/|scripts/build-sandbox\.sh$|scripts/build-sandbox\.ps1$|\.dockerignore$)'; then
        SANDBOX_DEF_CHANGED=1
    fi
fi

echo
echo "MyCode: $MYCODE_OLD -> $MYCODE_NEW"
echo "Web:    $WEB_OLD -> $WEB_NEW"

if [ "$WEB_CHANGED" -eq 1 ]; then
    echo
    echo "Web changed files:"
    printf '%s\n' "$WEB_CHANGED_FILES"
fi

echo

if [ "$MYCODE_CHANGED" -eq 0 ] && [ "$WEB_CHANGED" -eq 0 ]; then
    echo "==> Already up to date. Nothing to deploy."
    exit 0
fi

if [ "$MYCODE_CHANGED" -eq 0 ] && [ "$WEB_RUNTIME_CHANGED" -eq 0 ]; then
    echo "==> Web changes are documentation-only. No runtime deployment needed."
    exit 0
fi

# Rebuild the Sandbox when MyCode Core changes or when the Web-owned
# Sandbox build definition changes.
if [ "$MYCODE_CHANGED" -eq 1 ] || [ "$SANDBOX_DEF_CHANGED" -eq 1 ]; then
    echo "==> Rebuilding Sandbox image"
    cd "$WEB_DIR"

    # The repository may store this script as 0644, so invoke it via bash
    # instead of relying on the executable bit.
    bash ./scripts/build-sandbox.sh ../mycode
else
    echo "==> Sandbox image unchanged; skipping rebuild"
fi

# Only runtime-relevant Web changes require Web dependency/build work.
if [ "$WEB_RUNTIME_CHANGED" -eq 1 ]; then
    echo "==> Syncing Web backend dependencies"
    runuser -u mycode -- sh -c "
        cd '$WEB_DIR' &&
        UV_PROJECT_ENVIRONMENT=/home/mycode/.venvs/mycode-web \
        /home/mycode/.local/bin/uv sync --python 3.11
    "

    echo "==> Building Vue frontend"
    docker run --rm \
        -v "$WEB_DIR/frontend:/app" \
        -w /app \
        node:20-bookworm-slim \
        sh -c 'npm ci && npm run build'
else
    echo "==> Web runtime files unchanged; skipping uv sync and frontend build"
fi

echo "==> Restarting $SERVICE"
systemctl restart "$SERVICE"

echo "==> Waiting for health check"

for i in $(seq 1 15); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        echo
        curl -fsS "$HEALTH_URL"
        echo
        echo "==> Deployment successful"
        echo "MyCode: $MYCODE_NEW"
        echo "Web:    $WEB_NEW"
        exit 0
    fi

    sleep 1
done

echo
echo "ERROR: health check failed after 15 seconds."
systemctl status "$SERVICE" --no-pager || true
journalctl -u "$SERVICE" -n 40 --no-pager || true
exit 1
