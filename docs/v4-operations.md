# V4 配置、迁移、验收与回滚

架构与 donor 差异见 [v4-architecture.md](v4-architecture.md)。源码入口默认 V4，生产基础 Compose 仍显式运行 V3；只有 V4 overlay 才切换运行方式。没有自动发布或部署。

## 开发运行

使用 Python 3.12（OCR/ASR 依赖）、Node >=24、pnpm 10.33。Python 虚拟环境与 Node 依赖放设备共享缓存，避免在 NAS worktree 创建依赖树。

```bash
python -m pip install -r requirements-dev.txt
# 安装到缓存目录：先复制 yunying-dsh/package{,-lock}.json，再 npm ci --ignore-scripts。
# 把 yunying-dsh/node_modules 链接到该缓存的 node_modules，不复制源码到持久数据目录。
```

沿用现有 `.env` 的 DB/OneBot/附件/OCR/ASR/网站配置，把 `.env.v4.example` 的参数放入本机受保护环境或 `.env`。DSH 使用启动进程的环境；官方 `plugin add` 和 Python 服务均不会自动读取任意部署 env 文件，启动前须通过服务管理器或 Docker 的 `env_file` 注入配置。示例中的群号、密码、key 均为空；不能把真实配置提交到 git。

必要配置：

| 配置 | 作用 |
| --- | --- |
| `DB_HOST/PORT/USER/PASSWORD/NAME` | 与原 bot 同一 MySQL；生产账号仅给本应用所需表权限。迁移账号需要建表权限。 |
| `YUNYING_INTERNAL_SECRET` | Python 与 Profile 的独立 capability 凭据，至少32字符；两进程相同。 |
| `YUNYING_ALLOW_GROUPS/PRIVATE` | Social Agent 的会话授权边界，逗号分隔群/QQ，为空拒绝；群发言还受现有 `group_configs.is_chat_mode` 控制，原始历史仍保存。 |
| `YUNYING_MODEL_PROVIDER/MODEL` | 默认官方 `deepseek-official` / `deepseek-v4-flash`。 |
| `DEEPSEEK_API_KEY/BASE_URL` | 官方 DSH Messages provider 的凭据/端点；默认 `https://api.deepseek.com/anthropic`。chat/completions 网关改用下述官方 `llm-pi-ai` 配置。 |
| `YUNYING_SEARCH_PROVIDER` | `bing` 使用 donor 原安全搜索；明确设 `deepseek` 可使用官方认证搜索，默认仍是 Bing。 |
| `DEEPSEEK_SEARCH_BASE_URL` | 可选，官方搜索独立端点，默认 `https://api.deepseek.com/anthropic/v1`；不会自动沿用模型 BASE_URL。 |
| `DSH_HOME` | 持久目录，包含官方 Profile、Session、附件与派生索引；生产必须挂卷并备份。 |
| `YUNYING_ADMIN_SECRET` | 可选 Owner/未来网站管理凭据，至少32字符且与 INTERNAL 不同；不向模型提供。 |

默认本机 capability 监听 `127.0.0.1:8787/8788`；Docker 使用内部网络，不映射主机端口。迁移和运行：

```bash
python -m igngbot_v4.migrate
# 独立终端/受控服务，使用同一 DB、secret 与 allowlist：
python -m igngbot_v4
YUNYING_PACKAGE_DIR="$PWD/yunying-dsh" DSH_HOME=/persistent/yunying sh scripts/run-yunying-profile.sh
```

Profile 启动脚本只在初次目录不存在时调用官方 `dsh plugin --profile yunying add`，已有无关 Profile 会拒绝启动。正常启动沿用既有 Profile；升级须保留 `DSH_HOME`，不能重新初始化为临时目录。

## 复用现有 OpenAI 网关

DSH 官方 `llm-pi-ai` 已包含在固定 CLI 依赖中。可以在 `$DSH_HOME/profiles/yunying/cordis.patch.yml` 配置手工 provider route；不要修改 DSH 核心或自动生成的根 `cordis.yml`。本次 NAS 沿用已有 `LLM_LOCAL_BASE_URL/LLM_LOCAL_MODEL/LLM_LOCAL_API_KEY`，模型依然是 Gemini，由 DSH 执行 Agent Loop/工具续接/压缩。

```yaml
- id: llm-pi-ai
  config:
    providers:
      yunying-gateway:
        apiKeyEnv: LLM_LOCAL_API_KEY
        api: openai-completions
        baseURL: https://gateway.example/v1
        models:
          - id: gemini-flash
            contextWindow: 65536
            maxTokens: 8192
            input: [text, image]
            reasoningEfforts: false
        compat:
          supportsDeveloperRole: false
          supportsStore: false
          supportsStrictMode: false
          maxTokensField: max_tokens
        retryPolicy:
          mode: normal
          maxRetries: 2
```

设置 `YUNYING_MODEL_PROVIDER=yunying-gateway`、`YUNYING_MODEL=gemini-flash`。示例 URL 为占位；key 仅通过已有 `.env` 注入。65536 是当前部署使用的保守上下文预算，不代表提供方公布的模型容量。官方 compaction 按此预算管理上下文。

现有网站群聊开关继续有效：`is_chat_mode=0` 暂停，`1` 启用，未配置默认暂停。管理员 `/云萤暂停`、`/云萤继续`、`/聊天模式` 更新同一字段。网站变化通过基础设施监测（5秒）进入持久 FIFO，暂停仍保留 Inbox；恢复无需等待新来信。发送消息与拍一拍在执行前直接读取当前开关，阻止尚在运行的旧回合继续发言。此字段不再承担 V3 的 classifier 选择。网站旧人格/摘要管理页尚待后续替换，不影响固定 reserved2 Prompt。

## NAS 切换

沿用既有 VM 构建→压缩导入 NAS 镜像通道。首次复制 `deploy/docker/.env.v4.example` 为 `.env.v4`，填入启用会话、独立 secrets、官方模型凭据和固定源码版本镜像。既有 `.env` 保留 DB/OneBot/media/网站参数。

```bash
bash deploy/deploy-v4-nas.sh up
bash deploy/deploy-v4-nas.sh status
bash deploy/deploy-v4-nas.sh logs
# 需要回滚时：
bash deploy/deploy-v4-nas.sh rollback
```

`up` 保存原 V3 镜像名和 Compose/env 备份，建立 `dsh-runtime`（1000:1001），构建并导入两个固定版本镜像。停止旧 bot 单消费者后执行 checksum 迁移，再启动现有 `bot` 服务的 V4 入口与 `yunying`，等待健康检查；NapCat/media/frpc 和附件卷不另起一套。部署失败应查看阶段与日志，按 `rollback` 恢复记录的旧镜像；脚本不会自行删除数据或伪装健康。

迁移只有新增表；MySQL DDL 按自身语义提交，迁移中断后 `CREATE IF NOT EXISTS` 可重试，已登记迁移 checksum 不一致会拒绝。后续修改 schema 应新增迁移文件，不能改已应用版本。开发测试只应用于临时数据库；生产切换的实际记录与数据库退役计划见后续部署审查记录。

## 数据备份与恢复

同时备份三层：原 `message_logs`/附件、V4 MySQL 表、完整 `dsh-runtime`。需要一致恢复点时先暂停并停止两个 V4 进程，再用既有数据库备份和 NAS 快照方式备份；运行时直接拷贝 Session 文件不承诺一致性。恢复必须配对同一时间点的映射和官方 Session。映射为 ready 而 Session 缺失会启动失败，必须恢复备份，不能创建空 Session 掩盖丢失。

进程重启时，durable ingress/FIFO、原生 Inbox、发送结果、社会状态、长期记忆和版本均恢复。发送状态 `unknown` 必须人工核对，不自动重发。原始媒体失败也不伪造 OCR/转写。MySQL Memory 忘记是受控软删除，旧版本仍只对 Owner 可见；数据保留/硬删除由后续管理策略决定。

源码本地回滚可使用 `IGNGBOT_RUNTIME=v3 python main.py`。生产回滚脚本停 V4 两个服务、恢复原 Compose 和旧 V3 镜像，保留全部新 MySQL/DSH 数据。V3 不读取新 Memory，也不迁移 DSH summary 回 V3 context；以后再切 V4 时仍使用原 Session。不要同时运行 V3 与 V4 两个 QQ 消费者。

## Owner 管理接口

独立凭据认证的 POST 路由：`/admin/memory/search`、`read`、`versions`、`update`、`rollback`、`forget`；以及 `/admin/identity/bind`。均在 Profile 内网端口；只可供受控网站后端/Owner 使用。未来网站前端不能持有此 secret。

| 路由 | JSON 参数 |
| --- | --- |
| search | `query`，可选 `limit` |
| read / versions | `id` |
| update | `id, expectedVersion, markdown, reason`，可选 `title` |
| rollback | `id, version, expectedVersion, reason`；创建新版本 |
| forget | `id, expectedVersion, reason` |
| identity/bind | `provider, externalId, identityId, reason`；不能覆盖已有不同身份绑定 |

管理修改不改变文档原 scope/visibility/identity。网站还需要自身用户认证、权限与 CSRF/来源验证，本任务只提供管理后端边界，没有改站点仓库或上线网站页面。

## 实际群聊验收

自动化证据与运行命令见 [testing.md](testing.md)。上线前用专用测试群和允许私聊验收：普通连续聊天可沉默；直接 @/引用回应；多人快速来信无重复/并发回复；读取新消息后 wait/wake/潜水；真实搜索结果自然引用；本人授权后跨群记忆可读、更新、忘记，别群私有正文和来源群号不可见；撤回、图片/OCR、语音/ASR、合并转发和旧历史；长会话自动 compaction 后继续；重启后恢复同一 Session、发送账本与 Memory。

本地协议替身可以检验实际程序与协议，无法替代真实账号、模型风格、公网 DNS、OCR 模型首次下载、NAS 卷/容器重启和长期运行验收。
