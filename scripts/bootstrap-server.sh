#!/usr/bin/env bash
set -Eeuo pipefail

MYCODE_REPO="${MYCODE_REPO:-https://github.com/mmc-cloud/mycode.git}"
WEB_REPO="${WEB_REPO:-https://github.com/mmc-cloud/mycode-web.git}"
MYCODE_DIR="/opt/mycode"
WEB_DIR="/opt/mycode-web"
MYCODE_UID=10001
MYCODE_GID=10001
DEPLOY_STATE_DIR="/var/lib/mycode-deploy"
WEB_VENV="/home/mycode/.venvs/mycode-web"
WEB_UV="/home/mycode/.local/bin/uv"
SANDBOX_IMAGE="${MYCODE_SANDBOX_IMAGE:-mycode-sandbox:dev}"
DOMAIN="${MYCODE_DOMAIN:-mycode.icu}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this script as root"
[ -r /etc/os-release ] || die "/etc/os-release not found"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "Ubuntu is required (found: ${ID:-unknown})"

log "Installing base packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git jq nginx openssh-server rsync sudo tar unzip util-linux

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
    "$(dpkg --print-architecture)" "${VERSION_CODENAME}" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

log "Ensuring mycode user is ${MYCODE_UID}:${MYCODE_GID}"
if getent group mycode >/dev/null; then
  [ "$(getent group mycode | cut -d: -f3)" = "$MYCODE_GID" ] || die "group mycode exists with unexpected GID"
else
  if getent group "$MYCODE_GID" >/dev/null; then
    die "GID $MYCODE_GID is already used by another group"
  fi
  groupadd --gid "$MYCODE_GID" mycode
fi

if id mycode >/dev/null 2>&1; then
  [ "$(id -u mycode)" = "$MYCODE_UID" ] || die "user mycode exists with unexpected UID"
  [ "$(id -g mycode)" = "$MYCODE_GID" ] || die "user mycode exists with unexpected primary GID"
else
  if getent passwd "$MYCODE_UID" >/dev/null; then
    die "UID $MYCODE_UID is already used by another user"
  fi
  useradd --uid "$MYCODE_UID" --gid "$MYCODE_GID" \
    --create-home --home-dir /home/mycode --shell /bin/bash mycode
fi
usermod -aG docker mycode
install -d -m 0750 -o mycode -g mycode /home/mycode

log "Ensuring deploy user"
if ! id deploy >/dev/null 2>&1; then
  useradd --create-home --home-dir /home/deploy --shell /bin/bash deploy
fi
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh

clone_or_verify() {
  local repo="$1" dir="$2"
  if [ -d "$dir/.git" ]; then
    git -C "$dir" remote get-url origin >/dev/null
    return
  fi
  [ ! -e "$dir" ] || die "$dir exists but is not a Git repository"
  git clone "$repo" "$dir"
}

log "Cloning repositories"
clone_or_verify "$MYCODE_REPO" "$MYCODE_DIR"
clone_or_verify "$WEB_REPO" "$WEB_DIR"

log "Installing uv for mycode"
if [ ! -x "$WEB_UV" ]; then
  runuser -u mycode -- env HOME=/home/mycode sh -c \
    'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

log "Preparing MyCode Web Python environment"
runuser -u mycode -- sh -c "
  cd '$WEB_DIR' &&
  UV_PROJECT_ENVIRONMENT='$WEB_VENV' '$WEB_UV' sync --python 3.11
"

log "Building Sandbox image"
cd "$WEB_DIR"
bash ./scripts/build-sandbox.sh ../mycode "$SANDBOX_IMAGE"
"$WEB_VENV/bin/python" ./scripts/smoke-sandbox-runtime.py --image "$SANDBOX_IMAGE"

log "Building Vue frontend"
docker run --rm \
  -v "$WEB_DIR/frontend:/app" -w /app node:20-bookworm-slim \
  sh -c 'npm ci && npm run build'

if [ -f "$WEB_DIR/docs-site/package.json" ]; then
  log "Building documentation site"
  docker run --rm -e CI=true \
    -v "$WEB_DIR/docs-site:/app" -w /app node:22-bookworm-slim \
    sh -c 'npm install -g pnpm@10.33.0 && pnpm install --frozen-lockfile && pnpm run docs:build'
  rm -rf "$WEB_DIR/site/docs"
  mkdir -p "$WEB_DIR/site/docs"
  cp -a "$WEB_DIR/docs-site/.vitepress/dist/." "$WEB_DIR/site/docs/"
fi

log "Installing systemd units"
install -m 0644 "$WEB_DIR/deploy/systemd/mycode-web.service" /etc/systemd/system/mycode-web.service
install -m 0644 "$WEB_DIR/deploy/systemd/mycode-certbot-renew.service" /etc/systemd/system/mycode-certbot-renew.service
install -m 0644 "$WEB_DIR/deploy/systemd/mycode-certbot-renew.timer" /etc/systemd/system/mycode-certbot-renew.timer
systemctl daemon-reload
systemctl enable mycode-certbot-renew.timer

log "Installing deployment entrypoint"
cat >/usr/local/sbin/deploy-mycode <<'WRAPPER'
#!/usr/bin/env bash
set -Eeuo pipefail
exec /usr/bin/flock -w 900 \
  /var/lock/mycode-deploy.lock \
  /usr/bin/bash /opt/mycode-web/scripts/deploy-server.sh
WRAPPER
chmod 0755 /usr/local/sbin/deploy-mycode
printf '%s\n' 'deploy ALL=(root) NOPASSWD: /usr/local/sbin/deploy-mycode' > /etc/sudoers.d/mycode-deploy
chmod 0440 /etc/sudoers.d/mycode-deploy
visudo -cf /etc/sudoers.d/mycode-deploy >/dev/null

if [ -n "${DEPLOY_PUBLIC_KEY:-}" ]; then
  printf '%s %s\n' \
    'no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty,command="sudo /usr/local/sbin/deploy-mycode"' \
    "$DEPLOY_PUBLIC_KEY" > /home/deploy/.ssh/authorized_keys
  chown deploy:deploy /home/deploy/.ssh/authorized_keys
  chmod 0600 /home/deploy/.ssh/authorized_keys
fi

log "Initializing deployment state"
install -d -m 0755 "$DEPLOY_STATE_DIR"
git -C "$MYCODE_DIR" rev-parse HEAD > "$DEPLOY_STATE_DIR/mycode.sha"
git -C "$WEB_DIR" rev-parse HEAD > "$DEPLOY_STATE_DIR/web.sha"

log "Preparing runtime directories"
install -d -m 0755 -o mycode -g mycode "$WEB_DIR/data"
install -d -m 0755 /var/www/certbot /etc/nginx/conf.d

if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] && \
   [ -f "/etc/letsencrypt/live/$DOMAIN/privkey.pem" ]; then
  log "Installing production nginx configuration"
  rm -f /etc/nginx/conf.d/mycode-bootstrap.conf
  install -m 0644 "$WEB_DIR/deploy/nginx/mycode.conf" /etc/nginx/conf.d/mycode.conf
else
  log "Certificate not found; installing HTTP-only ACME bootstrap nginx configuration"
  rm -f /etc/nginx/conf.d/mycode.conf
  cat >/etc/nginx/conf.d/mycode-bootstrap.conf <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 503;
    }
}
NGINX
fi
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl start mycode-certbot-renew.timer

if [ -f "$WEB_DIR/.env" ]; then
  log "Production .env found; starting MyCode Web"
  chown mycode:mycode "$WEB_DIR/.env"
  chmod 0600 "$WEB_DIR/.env"
  chown -R mycode:mycode "$WEB_DIR/data"
  systemctl enable --now mycode-web
else
  log "Production .env is not present; MyCode Web is installed but not started"
fi

cat <<EOF

Bootstrap complete.

Next steps:
  1. Restore /opt/mycode-web/.env and /opt/mycode-web/data.
  2. Restore /etc/letsencrypt or issue a certificate for $DOMAIN.
  3. Install deploy user's authorized key if DEPLOY_PUBLIC_KEY was not supplied.
  4. Replace the temporary HTTP nginx config with deploy/nginx/mycode.conf.
  5. Enable/start mycode-web and verify /web/api/health.

See: $WEB_DIR/deploy/MIGRATION.md
EOF
