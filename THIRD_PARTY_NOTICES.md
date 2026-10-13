# 来源与版权说明

本项目的 Keep 接入方案参考下列公开项目：

- [Anathleticbicyclist 的多平台 → Outbase 同步工具](https://github.com/Anathleticbicyclist/sync-igpsport-magene-onelap-xingzhe-data-to-outbase)：运动读取、分页、导出及同步流程参考。本仓库保留其原有 MIT 许可证和版权声明。
- [yihong0618/running_page](https://github.com/yihong0618/running_page)：Keep API 端点与公开轨迹格式的技术参考。

应用里的 Keep 轨迹解码常量属于公开格式兼容所需的协议常量，不是用户账号、Strava 客户端密钥或平台授权令牌。用户凭据由自己的私有配置和加密数据库提供。

Strava 授权与上传依照其官方 API 文档实现。依赖库及其许可证归各自权利人所有。
