# 测试入口

先安装 `pip install -r requirements-dev.txt`，再执行 `python -m pytest tests -q`。

测试使用替身数据库、OneBot 与媒体服务，不读取部署凭据、不向真实 QQ 发送消息。通过测试只能说明离线契约通过；真实 QQ、数据库、模型与 NAS 验收另行记录。
