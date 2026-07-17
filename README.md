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

## 启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## 部署

在虚拟机中同步代码后执行：

```bash
chmod +x deploy_vm.sh
./deploy_vm.sh
```
