# IGNGbot v3

v3 将 v2 里“主程序 + AstrBot 插件”的分离式结构整合成单个 Python 程序。

## 当前实现

- 连接 OneBot WebSocket 接收群消息
- 消息落库到 `igng_bot.message_logs`
- 纯粹的自然群聊机器人（无外部工具/搜索），由本地轻量 LLM 驱动
- 支持群聊门控、性格配置与附加系统提示词管理
- 保留 `/聊天模式`、`/性格`、`/系统提示词`、`/用户组` 等基础管理命令

## 启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 图片 OCR 与语音转文字

收到图片或语音后，bot 会在写入消息记录和调用聊天 LLM 前提取文字：图片默认使用本地 `rapidocr-onnxruntime`，语音默认使用本地 `faster-whisper`。提取结果会以 `[图片OCR]` 或 `[语音转写]` 标记追加到消息正文，因此文本模型也能直接看到结果。

相关模型依赖已经列在 `requirements.txt`。语音模型默认在第一条语音消息时懒加载；生产环境建议提前设置 `MEDIA_ASR_MODEL_DIR` 并准备好 `MEDIA_ASR_MODEL`（默认 `small`），避免首条语音等待模型下载。QQ 常见的 SILK/AMR 语音会在存在 `ffmpeg` 时先转成 16 kHz 单声道 WAV。若使用远程服务，将 provider 改为 `vision_llm` 或 `remote`，并填写对应的 `MEDIA_*_BASE_URL`、`MEDIA_*_API_KEY` 和模型名。处理失败时附件仍会正常保存，bot 不会伪造转写内容。


## 部署

在虚拟机中同步代码后执行：

```bash
chmod +x deploy_vm.sh
./deploy_vm.sh
```
