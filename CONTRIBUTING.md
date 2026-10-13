# 贡献说明

先创建虚拟环境并安装 requirements.txt。提交前运行：

```bash
python -m unittest discover -s tests -v
node --check static/app.js
```

测试应使用隔离数据库和合成数据，不访问真实账号。不要提交 .env、账户令牌、密码、SSH 私钥、证书私钥、生产地址、数据库、运动轨迹或包含凭据的截图。

修改 Keep 接口时注明公开参考来源，并确认分页、时间单位、坐标转换和上传去重行为。不要通过测试自动上传真实运动。
