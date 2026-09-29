# MyCode Web 服务器迁移

本文档用于把生产环境从一台 Linux 服务器迁移到另一台 Ubuntu 服务器。目标是让服务器本身可替换：源码和可重建依赖来自 GitHub，只有生产配置、业务状态和证书需要迁移。

## 1. 固定约束

- 目标系统：Ubuntu。
- /opt/mycode 与 /opt/mycode-web 从 GitHub clone，不复制旧服务器源码目录。
- 宿主机 mycode 用户必须保持 UID/GID 10001:10001。Sandbox 会 bind mount data/sessions/*/mycode_state，数字 UID/GID 不一致会导致 PermissionError。
- mycode-web 当前要求 Uvicorn --workers 1。
- /opt/mycode-web/.env、/opt/mycode-web/data、证书和 Syncthing 数据属于私有状态，不能提交 GitHub。
- 迁移期间旧服务器保持在线，直到新服务器完成 Web、Sandbox、Syncthing、HTTPS 和 CI/CD 验证。

## 2. 需要迁移的私有状态

需要迁移：

    /opt/mycode-web/.env
    /opt/mycode-web/data/
    /etc/letsencrypt/
    /home/syncthing_data/    # 如果继续使用 Syncthing

不需要迁移：

    /opt/mycode
    /opt/mycode-web
    Docker images
    /home/mycode/.venvs
    node_modules / npm cache

这些都可以从 GitHub 或构建流程重新生成。

## 3. 旧服务器：检查与备份

先确认服务正常：

    systemctl is-active docker nginx mycode-web mycode-certbot-renew.timer
    curl -fsS https://mycode.icu/web/api/health

为了得到一致的 SQLite / Session 数据，备份前停止 MyCode Web；如果备份 Syncthing，也同时停止容器：

    systemctl stop mycode-web
    docker stop syncthing 2>/dev/null || true

    STAMP="$(date +%Y%m%d-%H%M%S)"
    tar -C / -czpf "/root/mycode-server-backup-$STAMP.tar.gz" \
      opt/mycode-web/.env \
      opt/mycode-web/data \
      etc/letsencrypt \
      home/syncthing_data

    sha256sum "/root/mycode-server-backup-$STAMP.tar.gz"

    systemctl start mycode-web
    docker start syncthing 2>/dev/null || true

备份包含 API Key、证书私钥和业务数据，按敏感文件处理，不上传到公开仓库。

## 4. 新服务器：执行 bootstrap

在干净 Ubuntu 上以 root 执行：

    curl -fsSL \
      https://raw.githubusercontent.com/mmc-cloud/mycode-web/main/scripts/bootstrap-server.sh \
      -o /tmp/bootstrap-server.sh
    bash /tmp/bootstrap-server.sh

如果已经准备好 GitHub Actions 使用的 deploy 公钥，可以同时传入：

    DEPLOY_PUBLIC_KEY='ssh-ed25519 AAAA... github-actions-mycode-deploy' \
      bash /tmp/bootstrap-server.sh

bootstrap 会完成：

- 安装 Docker、Nginx、SSH 等基础组件。
- 创建 mycode:10001:10001 与 deploy 用户。
- clone mycode 和 mycode-web。
- 安装 uv/Python 环境。
- 构建 Sandbox、Vue Frontend 和 Docs。
- 安装 systemd units、deploy wrapper 与 sudoers。
- 初始化 /var/lib/mycode-deploy/*.sha。
- 如果没有 HTTPS 证书，只安装 HTTP-only ACME 临时 Nginx 配置。

## 5. 新服务器：恢复私有状态

把备份传到新服务器后，以 root 解压：

    tar -C / -xzpf /root/mycode-server-backup-YYYYMMDD-HHMMSS.tar.gz

    chown mycode:mycode /opt/mycode-web/.env
    chmod 0600 /opt/mycode-web/.env
    chown -R mycode:mycode /opt/mycode-web/data

再次确认 UID/GID：

    id mycode

输出必须包含：

    uid=10001(mycode) gid=10001(mycode)

### Syncthing

当前容器约定使用数字 PUID=1001 / PGID=1001。沿用旧数据时：

    chown -R 1001:1001 /home/syncthing_data

    docker run -d \
      --name syncthing \
      --restart always \
      -e PUID=1001 \
      -e PGID=1001 \
      -p 8384:8384 \
      -p 22000:22000/tcp \
      -p 22000:22000/udp \
      -v /home/syncthing_data:/var/syncthing \
      syncthing/syncthing:latest

不要求宿主机一定存在 UID 1001 的同名用户，只要目录数字 ownership 与容器 PUID/PGID 一致。

## 6. HTTPS 与 Nginx

如果已经恢复 /etc/letsencrypt：

    test -f /etc/letsencrypt/live/mycode.icu/fullchain.pem
    test -f /etc/letsencrypt/live/mycode.icu/privkey.pem

    rm -f /etc/nginx/conf.d/mycode-bootstrap.conf
    install -m 0644 /opt/mycode-web/deploy/nginx/mycode.conf /etc/nginx/conf.d/mycode.conf
    nginx -t
    systemctl reload nginx
    systemctl enable --now mycode-certbot-renew.timer

如果没有迁移证书，保留 bootstrap 生成的 HTTP-only 配置。DNS 指向新服务器并确认 TCP 80/443 可达后签发：

    docker run --rm \
      -v /etc/letsencrypt:/etc/letsencrypt \
      -v /var/www/certbot:/var/www/certbot \
      certbot/certbot certonly --webroot \
      --webroot-path /var/www/certbot -d mycode.icu

签发完成后再安装正式 deploy/nginx/mycode.conf。

## 7. 启动与验证

    systemctl enable --now mycode-web
    systemctl status mycode-web --no-pager

    curl -fsS http://127.0.0.1:8000/web/api/health
    curl -fsS https://mycode.icu/web/api/health
    curl -sS -o /dev/null -w '%{http_code}\n' https://mycode.icu/web/

还需要在浏览器实际进入 Web Agent 完成一次对话，确认 Sandbox Runtime、Relay、Session state 和 Provider 都正常。

## 8. GitHub Actions / CD 切换

新服务器 /home/deploy/.ssh/authorized_keys 必须使用 forced command：

    no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty,command="sudo /usr/local/sbin/deploy-mycode" ssh-ed25519 AAAA... github-actions-mycode-deploy

仓库 Secrets 至少更新：

    DEPLOY_HOST=<新服务器 hostname 或 IP>
    DEPLOY_USER=deploy
    DEPLOY_KNOWN_HOSTS=<新服务器 SSH host key>

如果继续使用同一把 GitHub Actions 私钥，DEPLOY_SSH_KEY 不需要改变，但对应公钥必须存在于新服务器 authorized_keys。

建议在新服务器直接读取并核对 host key：

    cat /etc/ssh/ssh_host_ed25519_key.pub

然后触发一次真实 CI/CD，确认 GitHub -> SSH forced command -> deploy-server.sh 全链路成功。

## 9. DNS 切换与回滚

新服务器全部验证完成前不要关闭旧服务器。最终只把 mycode.icu 的 DNS A record 切到新 IP。

切换后再次检查：

    curl -fsS https://mycode.icu/web/api/health
    systemctl --failed --no-legend
    docker ps

如果新服务器出现无法快速修复的问题，把 DNS 指回旧服务器即可回滚。旧服务器建议保留数天再释放。

## 10. Desktop Commander（可选）

Desktop Commander 不属于 MyCode Web 的运行依赖，不放进 bootstrap 的强制安装流程。

需要 ChatGPT 直接管理服务器时再单独安装并授权：

    npm install -g @wonderwhy-er/desktop-commander@0.2.51
    desktop-commander remote

新旧服务器可以同时注册为不同 Remote Device。迁移期间两台机器可以同时在线，分别验证旧机和新机。设备授权信息不要提交 GitHub。
