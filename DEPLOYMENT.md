# 部署说明

仓库仅提供通用配置。请使用自己的服务器、域名、证书和私有 `.env`，不要将生产连接信息或运行数据提交到仓库。

## 空闲服务器：Docker Compose

按照 README 配置 `.env`、域名 DNS 和 Strava 回调，运行 `docker compose up -d --build`。Compose 的 Caddy 使用 80/443；这些端口已经被占用时不要直接启动此代理。

## 已有 Nginx：独立应用服务

示例使用 `/srv/keep-strava`、独立用户 `keep-strava` 和回环内部端口 `19180`。部署前检查 `ss -lntp`，根据自己的机器调整内部端口和所有引用。

将源码部署到该目录，创建虚拟环境并安装依赖：

```bash
cd /srv/keep-strava
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

创建系统用户和私有数据目录，配置 `.env`。运行用户需要读取 `.env`，且应是唯一能写入 `data/` 的应用账号。`deploy/keep-strava.service` 使用 systemd 文件系统保护；数据目录必须在启动前存在。

按实际路径安装服务文件，然后：

```bash
systemctl daemon-reload
systemctl enable --now keep-strava
```

将 `deploy/nginx.conf.example` 中的域名、公开端口和证书路径替换成自己的配置，加入独立 Nginx 站点。不要覆盖其他项目的配置。已有监听器可通过不同 server_name 共用 443。

```bash
nginx -t
systemctl reload nginx
```

HTTPS 证书应覆盖访问主机名或 IP，并独立设置续期。最终回调 URL 必须与实际访问入口一致。如果使用非标准 HTTPS 端口，把该端口写入完整回调 URL，Strava 设置页的授权回调域名仍只填主机名或 IP。

## 验证

```bash
python -m unittest discover -s tests -v
python deploy/verify-server.py --base-url https://sync.example.com
```

第二条从当前目录的私有 `.env` 读取登录配置，验证 HTTPS、登录保护、已登录 API 和 OAuth 跳转参数，不提交 Keep 凭据、不在 Strava 上执行授权或上传。

## 持久化与更新

保留 SQLite 数据目录和 TOKEN_ENCRYPTION_KEY。更新源码后重启本项目服务，网页退出不会清除平台令牌。服务重启时，尚未提交的任务可重试，结果不明的提交应先检查 Strava，已有 upload ID 的记录可刷新处理状态。

`.env`、SSH 密钥、证书私钥、运行数据库和运动文件属于私有部署数据，不能上传。
