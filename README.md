# IGNG Bot V4 · 云萤

云萤是运行在官方 DeepSeek Harness 上的长期在线 Social Agent。每个 QQ 群/私聊有持久 DSH Session；消息和附件始终机械记录；开启聊天模式的群消息进入原生 Inbox，关闭时仅明确 @/回复触发模型轮次，普通消息作为可查询的观察历史。模型通过 QQ 工具决定发言或沉默。运行上下文与压缩交给 DSH，长期 Markdown Memory 以 MySQL 为权威存储。

- [V4 架构、数据库、V3 复用与 donor 差异](docs/v4-architecture.md)
- [配置、迁移、NAS 切换和回滚](docs/v4-operations.md)
- [自动化测试与实际验收边界](docs/testing.md)
- [qq-bridge 原文移植与许可证](yunying-dsh/donor/qq-bridge/PROVENANCE.md)

源码 `main.py` 默认 V4；`IGNGBOT_RUNTIME=v3` 保留成熟 V3 回滚入口。现有生产 base Compose 保持 V3，叠加 `docker-compose.v4.yml` 才切换到 V4；开发合并不会自动部署。

V4 安装需要 Python 3.12、Node >=24，以及固定官方 DSH `0.2.1-alpha.1`。先配置数据库/OneBot/明确允许的会话，运行 `python -m igngbot_v4.migrate`，再分别启动 `python -m igngbot_v4` 与 `scripts/run-yunying-profile.sh`。完整配置见运行文档。

---

# V3 回滚路径与共用基础设施

v3 将 v2 里“主程序 + AstrBot 插件”的分离式结构整合成单个 Python 程序。

## V3 实现（显式回滚入口）

- 连接 OneBot WebSocket 接收群消息
- 消息落库到 `igng_bot.message_logs`
- 纯粹的自然群聊机器人（无外部工具/搜索），由本地轻量 LLM 驱动
- 支持群聊门控与本地模型自然对话
- 聊天完整系统提示词以 `system_prompts.chat` 为数据库权威值，进程启动时加载本地快照并按间隔同步；实际 LLM 调用只读取本地快照
- 保留 `/聊天模式`、`/用户组` 等基础管理命令
- 每次 LLM 调用镜像写入站点 AI 记录库（`igng_sites.ai_jobs` / `ai_job_attempts`）

### 聊天系统提示词

聊天提示词由 `prompts/system.txt` 维护首装默认值，首次初始化数据库时写入现有的 `system_prompts.chat` 记录；记录存在后，正常启动不会用文件覆盖数据库内容。

运行时由 `igngbot_v3/system_prompt_store.py` 负责：

- 先读取 `/data/runtime/system_prompts/chat.txt`（可用 `SYSTEM_PROMPT_CACHE_DIR` 调整路径）；
- 启动时从数据库同步一次，之后按 `SYSTEM_PROMPT_SYNC_INTERVAL_SECONDS` 定时拉取；
- 只接受包含固定人格“亲和”、风险约束和 JSON 输出契约的完整提示词；
- 数据库暂时不可用或内容不合规时继续使用最后一份有效本地快照，不在每条消息处理时访问数据库。

从旧的数据库提示词迁移到当前完整提示词时，先执行 dry-run：

```bash
python scripts/migrate_chat_system_prompt.py
```

确认目标 SHA-256 后，再使用 `--apply --expected-sha256 <当前数据库SHA-256>` 执行受保护迁移。脚本会在 `/data/runtime/system_prompt_migrations/` 留存迁移前备份。

## 部署

生产环境运行在 **NAS `192.0.2.17`** 上，由 `deploy/docker/docker-compose.yml` 定义的单个
compose 工程统一编排四个服务：

| 服务 | 作用 |
| --- | --- |
| `bot` | 本程序（由本仓库构建镜像） |
| `napcat` | SnowLuma/NapCat QQ 客户端，提供 OneBot HTTP + WebSocket |
| `media` | 附件只读签名分发服务（供 IGNG 站点展示聊天记录媒体） |
| `frpc` | 将 `media` 发布到公网 VPS 的隧道 |

工程目录 `/vol2/1000/Docker/igngbot`，附件数据在 `/vol2/1000/Docker/igngbot/data/message_logs`
（NAS 本地 bind mount，**不再使用 CIFS/SMB**）。

```bash
cd deploy
cp docker/.env.example docker/.env   # 首次：填入真实配置
./deploy-nas.sh up                   # 同步 + 构建/导入镜像 + 启动
./deploy-nas.sh status               # 查看状态与日志
./deploy-nas.sh logs                 # 跟踪 bot 日志
./deploy-nas.sh down                 # 停止（保留数据与卷）
```

镜像构建说明：NAS 无法直连 `deb.debian.org` / `pypi.org`，且开发机没有 Docker，
因此 `bot` 镜像由 VM（`BUILD_HOST`，默认 `ubuntu-vm`）构建后压缩传输到 NAS；
Dockerfile 内已固定可用的 apt / pip 镜像源。`media` 与 `frpc` 无依赖，直接在 NAS 构建。

### 容器内路径约定

| 变量 | 容器内路径 | 用途 |
| --- | --- | --- |
| `MESSAGE_ROOT` | `/data/message_logs` | 附件根目录（bind 到 NAS 数据目录） |
| `LOCAL_STORAGE` | `/data/runtime` | 日志、提示词快照、临时文件 |
| `MEDIA_ASR_MODEL_DIR` | `/data/models` | faster-whisper 模型缓存 |

数据库中保存的是**容器内绝对路径**（`/data/message_logs/<群>/<日期>/<文件>`）。
路径必须以 `message_logs` 结尾段出现，因为 IGNG 站点据此切分并生成媒体 URL。

`STORAGE_REQUIRE_MOUNT=1` 时，附件根目录缺失会**直接启动失败**，而不是静默回退到本地目录——
后者会造成“机器人看似正常、附件却全部不可见”的隐蔽故障。

`LEGACY_PATH_PREFIXES` 列出历史前缀（VM CIFS 挂载点、NAS 宿主路径、旧本地回退目录），
用于把旧数据行重新锚定到当前根目录。

### 迁移与回滚

历史数据路径回填脚本：

```bash
python deploy/migrations/rewrite_media_paths.py            # dry-run（默认）
python deploy/migrations/rewrite_media_paths.py --apply    # 执行
```

完整回滚预案见 `deploy/rollback/cutover-state.md`。

## 图片 OCR 与语音转文字

收到图片或语音后，bot 会在写入消息记录和调用聊天 LLM 前提取文字：图片默认使用本地 `rapidocr-onnxruntime`，语音默认使用本地 `faster-whisper`。提取结果会以 `[图片OCR]` 或 `[语音转写]` 标记追加到消息正文，因此文本模型也能直接看到结果。

语音模型默认在第一条语音消息时懒加载；NAS 无法直连 `huggingface.co`，因此 compose 中固定
`HF_ENDPOINT=https://hf-mirror.com`，模型缓存到 `./models` 后即可离线复用。QQ 常见的
SILK/AMR 语音会在存在 `ffmpeg` 时先转成 16 kHz 单声道 WAV。若使用远程服务，将 provider
改为 `vision_llm` 或 `remote`，并填写对应的 `MEDIA_*_BASE_URL`、`MEDIA_*_API_KEY` 和模型名。
处理失败时附件仍会正常保存，bot 不会伪造转写内容。

## 消息记录与云萤主动消息

- `message_logs.is_self = 1` 表示云萤主动发送的消息，`sender_id` 使用 `BOT_USER_ID`。
- `message_source` 区分 `ai`、`command`、`auto_plus_one`、`notification`、`media` 和 `onebot_event` 等来源。
- HTTP 主动发送成功后立即记录；OneBot `message_sent` 事件作为补偿路径，并通过 `(group_id, msg_id)` 去重。
- 私聊消息使用负数 `group_id` 作为独立会话键，例如 QQ `456` 对应 `-456`。
