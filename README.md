# Keep → Strava 个人同步网页

单用户自托管工具：连接 Keep 与 Strava，检查最近两个月的跑步、骑行和徒步记录，预览、勾选后确认上传到 Strava。

## 项目起源
  本人记录徒步和跑步运动使用小米手表+小米运动健康，运动数据平台使用outbase+strava。每次需要手动从小米运动健康打包到strava非常麻烦并且会发生时间偏移的现象。在outbase交流群里受到迈进OB项目的启发，通过官方同步功能先把数据从小米运动健康自动同步到keep，再从keep上传到strava。妙哉妙哉。

## 功能

- 中文网页、单用户密码登录。
- Keep 手机号与密码登录，只保存加密会话令牌；不保存 Keep 密码。
- Strava 官方 OAuth 授权，申请 `activity:write`，自动刷新短期访问令牌。
- 按中国时区查询最近两个日历月，包含起始日期全天。
- GPX 轨迹转换、Keep 中国境内 GCJ-02 坐标转换为 WGS-84。
- SQLite 同步账本、稳定外部 ID、逐条上传与异步处理状态查询。
- 已成功同步记录自动跳过，失败记录可重试，进程重启保留同步状态。
- 手动触发检查；上传须再次确认，没有后台定时同步。

Keep 接入依赖非官方接口，可能因平台变更失效。无 GPS 轨迹的记录不能生成可上传的 GPX。Strava 的授权、运动类型和接口额度以官方文档及你的应用设置为准。

## 快速部署：Docker Compose

需要 Docker Compose、指向服务器的域名，以及空闲的 80/443 端口。已有其他项目占用这些端口时，使用现有反向代理，参考 [部署说明](DEPLOYMENT.md)。

1. 复制 `.env.example` 为 `.env`，填写网页密码、两个独立密钥、域名和 Strava Client ID/Secret。
2. 配置 `STRAVA_REDIRECT_URI` 为实际 HTTPS 网页首页，例如 `https://sync.example.com/`。
3. 在 [Strava API 设置](https://www.strava.com/settings/api)中保存授权回调主机名，例如 `sync.example.com`。不要填协议、端口或路径。
4. 启动服务：

```bash
docker compose up -d --build
```

Caddy 自动申请 HTTPS 证书，应用和数据保存在独立容器及数据卷中。不要删除数据卷或更换已有 `TOKEN_ENCRYPTION_KEY`，否则令牌无法解密。

### 生成密钥

安装 Python 依赖后执行以下命令，并将结果仅保存在私有 `.env`：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

第一条生成 `APP_SECRET_KEY`，第二条生成 `TOKEN_ENCRYPTION_KEY`。`APP_PASSWORD` 请单独设置。

## 使用

登录网页后连接 Keep，再通过 Strava 页面完成授权。点击“检查 Keep”查看最近两个月记录，选择后确认上传。Strava 处理完成并返回活动 ID 后才标记成功。处理中可刷新状态；失败时查看原因再重试。

退出网页不会删除平台授权。点击对应平台的“断开”按钮才会清除本工具保存的令牌。

## 本地开发与测试

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

本地 HTTP 开发时，在私有 `.env` 中将 `COOKIE_SECURE=false`，配置本地回调 URL，运行 `python app.py`。测试不需要真实 Keep 或 Strava 凭据，不访问真实运动接口：

```bash
python -m unittest discover -s tests -v
node --check static/app.js
```

## 配置与数据

凭据配置见 [Strava 配置说明](STRAVA_API.md)，独立服务及 Nginx 示例见 [部署说明](DEPLOYMENT.md)。SQLite 默认保存在 `data/`，可通过 `DATA_DIR` 指定目录。GPS 轨迹负载只在处理过程中保存在内存中，不写入 SQLite。

仓库不包含真实账号信息、客户端密钥、令牌、SSH 私钥、数据库、运动文件或生产部署记录。贡献时也请检查暂存内容，忽略规则不能防止强制添加敏感文件。

## 来源与许可证

参考项目和协议实现来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。保留原仓库的 [MIT 许可证](LICENSE)及原作者版权声明。
