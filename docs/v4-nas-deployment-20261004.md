# 2026-10-04 V4 NAS 部署与实测记录

V4 已替换 NAS 的唯一 V3 bot 消费者，存储根保持 `/vol2/1000/Docker/igngbot`。没有删除生产记录、数据卷或旧镜像。`docker-compose.yml` 与 `docker-compose.v4.yml` 合并为同一个 `igngbot` Compose project；数据库仍是原外部 MySQL，网站仍独立部署。

## 实际运行版本与存储

| 服务 | 实际镜像 / 入口 | 结果 |
| --- | --- | --- |
| bot | `igngbot-v4:e7db6fa38f35`；`python -m igngbot_v4` | healthy；容器名 `igngbot-v3` 为原部署名称，运行代码已与 V4 源码 hash 核对。 |
| yunying | `igngbot-yunying:e7db6fa38f35`；官方 DSH `0.2.1-alpha.1` YunYing Profile | healthy；同一数据库仅一个原生 Profile 租约。 |
| napcat | 原 `snowluma-docker-framework:local` | healthy；未重建登录卷或重启原容器。 |
| media | `igng-bot-media:baseline-20261004-a6bed3f2fa9e` | healthy；同一原镜像 ID 与签名密钥，已纳入 `igngbot` project。 |
| frpc | 原 `igng-bot-media-nas-frpc` | running；原隧道配置与容器未重启；该服务没有 Docker healthcheck。 |

bot image ID：`sha256:1d37eb5d7920356ae5da9d0a24473b883bff659dc5814a5b6b5101abab7ed42f`；DSH image ID：`sha256:ca4be4f4593baf0470042da1935b39c74d7904d3d467cfdafd89c58060cbdeb3`。源码 tag 是修订提交；随后补充的本文件等文档不改变镜像运行代码。

保留 `/data/message_logs` 对应的 `data/message_logs`、`bot-runtime`、`models` 与原 NapCat 三个 named volumes；新增 `dsh-runtime` 保存官方 Session/附件/索引/Profile。两个 V4 capability 端口未映射到主机。另一历史 `igng-media-frpc` 不属于本项目，此次未动它。原媒体容器保留为停止状态的 `igng-bot-media-before-v4-20261004`。

## 必要配置与修复

沿用原网关和 Gemini 模型，经 DSH 官方 `llm-pi-ai` route 调用。Operator patch 在 `dsh-runtime/profiles/yunying/cordis.patch.yml`；凭据仍从原 `.env` 注入，没有 fork 或修改 DSH 核心，没有恢复 V3 的纯文本调用链。当前65536 token上下文是保守部署预算，不作为厂商模型容量声明。

V3 `is_chat_mode=0` 表示“仅定向 @”，不能解释为暂停。V4 对全部授权消息执行 Social Runtime，自主决定发言；独立 `social_paused` 默认0，管理员 `/云萤暂停`、`/云萤继续` 管理。配置变更通过受信控制事件进入同一持久 FIFO，发言/拍一拍执行前再次检查权限。旧聊天模式、人格、摘要网页尚未改为 V4 管理页，相关读写目前不影响 Agent 推理。

媒体服务原先由别的 Compose 启动，挂载了不存在的旧目录。此次保留原 image ID、签名密钥和隧道，把只读挂载改回真实 bot 附件目录；签名密钥与站点现役有效密钥一致，未误用 bot `.env` 中另一个 key。私密 `.env.media` 为0600，不入 Git。

NAS 的原 Bing HTML 没有 `b_algo` 结果，因此先保留 donor 原搜索，在空结果时使用同一安全传输读取 Bing RSS。限制、DNS/逐跳检查和 `query/results` 契约不变；不修改 reserved2 Prompt 或 donor 原文件。RSS 也可能对某条查询返回空，不能承诺所有查询有结果。

## 实际执行与证据

部署使用 `COMPOSE_DIR=<protected-config> BUILD_DIR=/home/lvziw/.cache/igngbot-build/yunying-v4-e7db6fa38f35 V4_VERSION=e7db6fa38f35 bash deploy/deploy-v4-nas.sh up`：既有 Ubuntu VM 构建、压缩导入 NAS、停止旧消费者、checksum migration、Compose 启动/健康检查，全部完成。

| 检查 | 实际结果及边界 |
| --- | --- |
| 本地完整验证 | Python3.12：`YUNYING_TEST_DB=yunying_v4_test YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q`，102 passed，无 skip；Node24：`YUNYING_TEST_DB=yunying_v4_test npm --prefix yunying-dsh test`，25 passed，无 skip。安装、Node check、shell syntax、privacy scan、diff check通过。 |
| 远端 CI | e7db6fa38f35 的 [CI run](https://github.com/igng-dev/igng-bot/actions/runs/37185225640) 成功，含一次性 MySQL8.4 与官方CLI双进程恢复。 |
| 现役模型链 | 官方 DSH AgentLoop/ToolRuntime/JSONL 使用现有网关：1次工具执行、1次工具结果、2条 assistant message、0次失败 attempt。独立部署检查没有向 QQ 发测试消息。 |
| OneBot与自然流量 | get_login_info成功；自然到达的消息/notice持续落库。重启后快照：9 Session ready、全部非暂停、381个 QQ native event均 delivered、411条 ingress均 delivered、42个 AI record均 mirrored、4次自然工具发送均 sent、无 unknown 发送。计数随运行增长，不是风格验收。 |
| 真实 DSH 工具 | 备份日志包含模型实际调用 get_prompt、get_unread、wait_for_messages 和原 MCP QQ send工具。9份日志经官方 Session format catalog严格验证成功。重启留下的待完成工具中断由上游 durable恢复处理，不自行猜测远端结果。 |
| 搜索 | NAS native容器实际调用 `bingSearchWithFallback('DeepSeek Harness')` 返回8条结果，契约为 `query/results`。另一个 Docker查询返回空；没有放宽 SSRF 或伪造结果。模型联网后自然回复仍需真实对话验收。 |
| 现有图片 | 无签名403；原签名200，返回原 WebP；111920字节并与文件 checksum一致。修正挂载后直接复用现有文件。 |
| 生产重启与恢复 | 只停 DSH，Python继续持久收取；备份后启动原Profile。9个映射 hash不变、9个 Session既有事件前缀 hash不变、ready=9、恢复后pending ingress=0；14个 identity/binding保留。不是用空Session重建。 |
| 正式 Memory | Owner只读检索接口成功，生产文档/版本/来源仍为0。跨群本人授权、CRUD/CAS/forget/rollback和群私有隔离在真实临时SQL验证；未在生产写入伪造 Person Memory，不声称生产已有内容恢复。 |
| 媒体/撤回/compaction | Python/Node测试覆盖原媒体、语音、撤回、旧历史与官方真实compaction后继续；线上有自然撤回记录。此次未重新下载/执行真实 OCR/ASR 模型，也未为压缩测试向生产群灌消息。 |

原811版本期间，官方日志记录18次上游 `SERVER` 失败 attempt及12次受控重试，均在e7切换前。修订版到本次重启备份未新增该类失败；当前成功会话/工具发送不代表上游以后不会临时失败。依赖审计当前为1 high / 0 critical（http-cache-semantics）；详见架构文档，未宣称通过全量安全审计。

## 数据库与回滚点

生产 MySQL8.0.36，`igng_bot` 从10张表变为23张。`001_social_agent.sql` 添加12张业务表及migration登记；`002_group_social_pause.sql` 仅添加独立暂停列，旧字段/记录保留。清理判断及软依赖见 [v4-database-audit.md](v4-database-audit.md)。

只读快照2026-10-04 15:25：40579条原始消息、156条撤回、3437条调用日志；4张候选退役表仍为context_summaries5/personality_profiles3/group_personality_configs5/system_prompts1行。原始记录均保留，未执行DROP/DELETE。

- V3原 image ID 固定为 `igngbot-v3:rollback-20261004-e33cc69b193c`，原 Compose/env 与切换前MySQL一致性备份在 `v4-backups/20261004-7f15ecc04ae6`。
- V4重启恢复点在 `v4-backups/20261004-e7db6fa38f35`，包含 `igng_bot.sql.gz`、完整 `dsh-runtime.tar.gz` 与 `SHA256SUMS`。gzip、tar与checksum校验通过；备份期间仅停DSH，OneBot继续写入durable ingress，数据库用单事务快照。原附件仍在原目录，此备份不含附件副本。
- 运行 `bash deploy/deploy-v4-nas.sh rollback` 会停V4两进程、恢复记录的V3镜像/基础Compose并保留全部新MySQL和DSH数据。不要同时启动两代bot，也不要为了回滚清空Memory或Session目录。回滚命令本次未演练，旧镜像和配置已检查存在。

线上自然沉默/@回复质量、长时间稳定性、其他平台Identity绑定、真实OCR/ASR与自动长会话compaction仍需后续观察/人工验收。网站旧控件需要后续适配；本次只检查网站消费者，没有修改网站仓库。
