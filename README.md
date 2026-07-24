# IGNGbot v3

v3 将 v2 里“主程序 + AstrBot 插件”的分离式结构整合成单个 Python 程序。

## 当前实现

- 连接 OneBot WebSocket 接收群消息
- 消息落库到 `igng_bot.message_logs`
- 内嵌多轮工具调用 Agent
- 默认给 Agent 最近 10 条消息
- 提供 `get_more_chat_history` 工具按需获取更早聊天记录
- 迁移 MC / IGNG 查询工具
- 支持 `send_sticker` 表情工具
- 保留 `/聊天模式`、`/身份配置` 机械命令
- 支持通过自托管 SearXNG 的 JSON API 联网搜索；聊天模式轻量查询，任务模式允许更多结果和多轮工具调用

## 启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### SearXNG

联网搜索默认请求本机 `http://127.0.0.1:8080` 的 SearXNG JSON API。可以通过 `.env` 调整地址和结果上限：

```dotenv
SEARXNG_ENABLED=1
SEARXNG_BASE_URL=http://127.0.0.1:8080
SEARXNG_IMAGE=dockerproxy.net/searxng/searxng:latest
SEARXNG_CHAT_RESULTS=5
SEARXNG_TASK_RESULTS=8
```

SearXNG 不可用时不会阻止 bot 启动，`web_search` 会返回可识别的错误结果。

项目提供了本地 SearXNG 容器配置：

```bash
cd deploy/searxng
docker compose up -d
```

容器只绑定到本机回环地址，不直接暴露到公网，部署脚本会为本机实例生成随机 secret 并关闭本地 limiter；首次启动后可以用
`http://127.0.0.1:8080/search?q=test&format=json` 检查 JSON 接口。

## 部署

在虚拟机中同步代码后执行：

```bash
chmod +x deploy_vm.sh
./deploy_vm.sh
```

部署脚本会在 Docker Compose 可用时自动启动 `deploy/searxng/docker-compose.yml` 中的本机 SearXNG 容器。
