# 归档层退役与 NAS 部署实录（2026-10-07）

用户在确认网站消费者切换（IGNG站点 PR #109）与 Memory 契约精简（机器人 PR #19）之后，授权关闭 V3 回滚窗口并删除恢复归档。本次在生产实际执行：数据库由 27 表收敛为 14 张现役表；所有 DDL 走受控退役阶段（PR #21），执行前有全库备份与隔离恢复证明。

## 实际数据库变化

| 退役项 | 行数 | 结果与保存方式 |
| --- | ---: | --- |
| `memory_identities` / `memory_identity_bindings` / `memory_identity_audit` | 41 / 41 / 41 | 归档进 `yunying_legacy_tables` 后 DROP；跨群身份解析早已改走 `igng_sites.user_qqs`。 |
| `memory_sources` / `memory_audit` | 0 / 5 | 来源先回填进 `memory_versions.sources`（本次 0 条，均无待回填），随后 DROP。 |
| `memory_documents.identity_id` 列 + `memory_person` 索引 | — | DROP；个人记忆以 `person_qq` 为唯一键（PR #19 已切换）。 |
| `context_summaries` / `system_prompts` | 0 / 1 | V3 回滚入口重建的空壳/种子；归档进 `yunying_legacy_tables` 后 DROP。 |
| `message_logs.audio_file_path`（V3 重建的空列） | 0 条非空 | 记录后手动 DROP；原值仍在 `yunying_media_legacy` 归档与备份中。 |
| `yunying_prompt_blobs` | 4,164 | DROP（历史 Prompt 去重正文，仅恢复用）。 |
| `yunying_call_history` | 3,565 | DROP（网站 PR #109 已上线，不再读取；旧 `legacy-<id>` 链接改由 `ai_jobs.task_key` 解析）。 |
| `yunying_media_legacy` | 5,697 | DROP（三列退役值，仅 schema 恢复用）。 |
| `yunying_legacy_tables` | 11 | DROP（V3 表与媒体列的恢复归档）。 |
| `yunying_ingress_archive` | 0 | DROP；无未还原的 `payload_archived_at` 行。 |
| `yunying_retirement_batches` | 5 | 记录完成后 DROP（退役日志自身）。 |

保留：`message_logs.audio_transcript`（现役 ASR 字段）、`memory_documents` / `memory_versions`（含 `sources`）、全部机械记录、会话、事件、发送与计费表。最终 14 表：`group_configs`、`mc_ticket_notification_deliveries`、`mc_ticket_notification_state`、`memory_documents`、`memory_versions`、`message_logs`、`message_recall_events`、`yunying_ai_records`、`yunying_events`、`yunying_ingress`、`yunying_schema_migrations`、`yunying_sends`、`yunying_session_events`、`yunying_sessions`。

## 备份与恢复证明

配对目录：`/vol2/1000/Docker/igngbot/v4-backups/20261007T031108Z-pre-archive-retirement/`

| 阶段 | 内容 | 结果 |
| --- | --- | --- |
| `stage1`（memory-legacy 之前） | `igng_bot.sql.gz`（sha256 `2c8c1f93…d4c12`）、`dsh-runtime.tar.gz`（sha256 `8a868164…105cb`）、`restore-proof.json` | 隔离 `mysql:8.0.36` 恢复：**27 表、71,289 行**逐表行/DDL/索引/字符集一致。 |
| `stage2`（archive-tables 之前） | `igng_bot.sql.gz`（sha256 `781663527…0ae3d`）、`dsh-runtime.tar.gz`、`restore-proof.json` | 隔离恢复：**20 表、71,168 行**一致。 |
| `window/` | 各阶段 dry-run/apply 输出、启动前后容器与镜像状态 | 原始证据。 |

隔离恢复服务运行在构建机 `ubuntu-vm`（`igngbot-retirement-restore-20261007`，仅 loopback 13317），通过 SSH 隧道做证明；生产未连接测试实例。

## 执行顺序（实际命令）

```bash
# 构建并载入镜像（bot + yunying，tag b7c7466）
COMPOSE_DIR=<受保护配置> BUILD_DIR=/home/lvziw/.cache/igngbot-build/yunying-v4-b7c7466 V4_VERSION=b7c7466 bash deploy/deploy-v4-nas.sh images
COMPOSE_DIR=<受保护配置> V4_VERSION=b7c7466 bash deploy/deploy-v4-nas.sh sync
# NAS：停 bot/yunying，应用 009/010 增量迁移，备份 stage1 + 证明
docker compose ... run -T --rm --no-deps --entrypoint python bot -m igngbot_v4.migrate
docker compose ... run -T --rm --no-deps -v <stage1>:/backup:ro --entrypoint python bot -m igngbot_v4.retire legacy-tables --apply --proof /backup/restore-proof.json
docker compose ... run -T --rm --no-deps -v <stage1>:/backup:ro --entrypoint python bot -m igngbot_v4.retire memory-legacy --apply --proof /backup/restore-proof.json
# 备份 stage2 + 证明
docker compose ... run -T --rm --no-deps -v <stage2>:/backup:ro --entrypoint python bot -m igngbot_v4.retire archive-tables --apply --proof /backup/restore-proof.json
# 启动并等待健康
docker compose ... up -d --wait --wait-timeout 180 --no-build bot yunying
```

两个记录在案的手动操作（均在授权删除范围内、且被 stage1 备份覆盖）：删除 `yunying_legacy_tables` 中 10-05 已归档的 `context_summaries`/`system_prompts` 原始行（否则重建空壳与旧归档哈希冲突导致阶段拒绝），以及删除 V3 重建的空 `audio_file_path` 列（`media-columns` 阶段只接受完整三列集，记录后手动清理）。

## 部署结果

- bot：`igngbot-v4:b7c7466`（image ID `sha256:29c7ce36ed0c…`），healthy；入口 `python -m igngbot_v4`。
- yunying：`igngbot-yunying:b7c7466`（image ID `sha256:217bc03b77f5…`），healthy。
- 上一版 `8ff799d9424e` 镜像保留在 NAS（回滚需配合备份恢复，见下）。
- 生产只读验证：9 个 Session 全部 ready；3,643 条 ingress 全部 `delivered+recorded`，0 积压；启动窗口内短暂 `DSH delivery deferred` 在 yunying 就绪后自动追平；`yunying_ai_records` 未镜像 0、未决发送 0；真实来信继续入库与投递。
- 网站：account 站已部署（PR #109，Actions run `37562437527` 成功），调用列表与旧编号详情只读 `ai_jobs`。

## 部署前置问题与修复

首次 account/mc 部署失败于远端 `Invalid database runtime manifest`：运行时清单键集在 #104 加入 `AI_RECORDS_NAMESPACE` 后只在 `sync_database_env=true` 时重新下发，旧清单（31 键）与新校验（32 键）不符。使用 `gh workflow run deploy.yml -f app=account -f sync_database_env=true` 重跑后成功；mc 等其他站点如需部署同样要带该参数。

## 自动化验证

| 命令/环境 | 结果 |
| --- | --- |
| `python -m pytest tests -q`（离线，本任务 worktree） | 122 passed / 16 skipped |
| `YUNYING_TEST_DB=yunying_v4_test YUNYING_TEST_DB_PORT=43316 YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q`（独立 loopback MariaDB 11.8.6 + 官方 DSH 双进程） | 138 passed / 0 failed |
| `npm run check`、`YUNYING_TEST_DB=... npm test` | 通过；53 passed / 0 skipped |
| [机器人 PR #21 CI](https://github.com/igng-dev/igng-bot/actions/runs/37564192055) | verify 通过 |
| [网站 PR #109 CI](https://github.com/igng-dev/igng-sites/actions/runs/37560892153) | verify / MySQL migration tests / Repo guards 全部通过 |

## 回滚边界

- `retire restore` 不再可用（归档表已删除）。恢复只能使用 `stage1`/`stage2` 完整 SQL 冷备；回灌会覆盖清理后产生的新消息，必须在停机窗口按灾难恢复流程执行。
- 退回上一版 V4 镜像（`8ff799d9424e`）**必须先恢复 `stage1`**：该版本会在每封来信写入身份/绑定/审计表，直接运行会遇到缺表错误。
- 官方 DSH Session 与附件目录未移动；`dsh-runtime.tar.gz` 与 SQL 必须配对保存。
- 镜像导入 tar 保留在 `v4-backups/image-imports/`；隔离恢复容器的数据目录保留在构建机 `ubuntu-vm:/home/lvziw/.cache/igngbot-retirement-20261007/restore-data`（可另行清理）。
