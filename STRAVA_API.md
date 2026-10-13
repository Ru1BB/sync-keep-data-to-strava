# Strava 配置说明

在 [Strava API 设置](https://www.strava.com/settings/api)注册应用，将 Client ID 和 Client Secret 保存在私有 `.env` 中。本项目不使用 API 设置页面上默认的只读令牌，用户需要通过网页 OAuth 授予 `activity:write`。

| 配置 | 用途 |
| --- | --- |
| STRAVA_CLIENT_ID | 应用 ID |
| STRAVA_CLIENT_SECRET | 服务端兑换和刷新令牌的客户端密钥 |
| STRAVA_REDIRECT_URI | OAuth 返回本应用的完整 URL |
| TOKEN_ENCRYPTION_KEY | 加密数据库中的平台令牌 |

示例回调：`https://sync.example.com/`。首页会校验 OAuth state 并兑换授权码；`/auth/strava/callback` 也可作为独立回调路径。

Strava 应用设置的授权回调域名应只填写对应主机名。例如回调 `https://sync.example.com:9443/` 的主机名仍为 `sync.example.com`。回调为服务器 IP 时，只填写该 IP。域名、Client ID 和应用设置不匹配时，Strava 可能报 `redirect_uri invalid`。

Strava 短期访问令牌到期后，应用通过刷新令牌续期，并保存响应中的最新刷新令牌。授权被撤销或失效后，需在网页重新连接。

不要把 Client Secret、访问令牌、刷新令牌写入 Markdown、截图、源码或 Git 提交。`.env` 和运行数据库已被忽略。

官方文档：[授权](https://developers.strava.com/docs/authentication/)、[上传](https://developers.strava.com/docs/uploads/)、[额度](https://developers.strava.com/docs/rate-limits/)。
