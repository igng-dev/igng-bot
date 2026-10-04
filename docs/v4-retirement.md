# V4 数据退役与恢复操作

本轮延续机械记录与 Social Agent 分离：消息、附件、撤回、群开关和通用 AI 账本继续使用；DSH 官方 Session 与独立 MySQL Memory 不做清理。reserved2 Social Prompt 没有修改。原调查见 [数据库审查](v4-database-audit.md)。本文件说明受控操作；生产实际结果另行登记。

## 现役契约

V4 使用 `DBHandler(..., legacy_compat=False)`。V3 入口保留默认兼容模式，旧摘要失效、旧附件列和旧调用写入只供 V3 使用。V4 不再创建/写入 `call_logs`：native task/attempt 经 durable outbox 直接进入 `igng_sites.ai_jobs` / `ai_job_attempts`。历史已关联记录沿用原 website job key；未关联的早期请求使用 `dsh-legacy:<record_id>`，不会伪造新 Agent turn 或重复账单。未知 usage 继续为 NULL。

账户站点的群管理只读写 `group_configs` 的群名、聊天模式与硬暂停；摘要接口返回410，人格模板不再提供配置。调用列表默认展示通用 AI 任务，详情包括实际请求；历史入口保留原 `call_logs.id`，数字旧链接仍可查。概览只统计正式通用账本，不把归档记录再算一次。所有接口继续要求 SuperAdmin。

`attachments_json` 与原结构消息成为 V4 附件契约。`message_logs.audio_transcript` 仍保留：即使当前非空行数为0，它也是现役 ASR 输出字段。物理图片、语音和文件不移动、不做 GC。

## 005 增量迁移

`005_retirement_archive.sql` 只新增结构，不自动 DROP 或清空任何数据：

| 表/字段 | 用途 |
| --- | --- |
| `yunying_retirement_batches` | 每阶段操作、备份证明哈希、准备/完成状态与统计；DDL 中断可核查并重试。 |
| `yunying_legacy_tables` | 原始建表语句、精确内容哈希、行数与小表完整记录；大表正文使用独立归档。 |
| `yunying_prompt_blobs` | SHA256 内容寻址，保留原文、NULL/空字符串差异，无 Prompt 改写。 |
| `yunying_call_history` | 原调用 ID、查询索引、完整原 metadata 与三个正文哈希。 |
| `yunying_media_legacy` | 原消息 ID 与三列附件值；供 schema 恢复。 |
| `yunying_ingress_archive` | 已完成且至少30天的 ingress 原 payload 与哈希。 |
| `yunying_ingress.payload_archived_at` | 标记正文已归档；稳定事件 ID、命令结果、状态仍留在原表。 |

归档位于既有受限 `igng_bot` 数据库；现有 DB 账号没有创建其他数据库的权限，本轮不扩权。归档不暴露给模型的 QQ/Memory 工具。它们仍是正式恢复资料，不能作为“无用新表”再次删除。

## 顺序与硬性检查

1. 验证 bot 与网站消费者，先部署网站兼容查询；构建固定 commit 的 bot/DSH 镜像。
2. 停止 YunYing，再停止 bot；使用新镜像运行 `python -m igngbot_v4.ai_records --drain`，完成 durable AI outbox。迁移005保持 additive。
3. 获取同一停机窗口内完整 SQL、官方 `dsh-runtime`、Compose/配置和镜像摘要，保存受保护 checksum manifest。禁止只备份候选表。
4. 把完整 SQL 实际恢复到不同 server UUID 的隔离 MySQL，使用 `igngbot_v4.retire.prove_restore(source, restored, backup, output)` 比较所有表的全部行、DDL、索引、字符集和 AUTO_INCREMENT。生成0600证明，证明与 gzip SQL 必须放在同一受保护目录。
5. 在隔离副本演练每阶段退役及恢复；不连接真实 QQ/模型，不把测试回执当真实群验收。
6. 生产中依次执行下列 dry-run / apply；最后启动同一 Compose 的 bot 与 YunYing，验证 Session 映射、记录增长、历史读取与健康状态。

示例以已经配置好的 Compose operator 容器为执行环境（只挂载受保护备份目录，不把凭据复制进源码）：

```bash
python -m igngbot_v4.retire legacy-tables
python -m igngbot_v4.retire legacy-tables --apply --proof /backup/restore-proof.json
python -m igngbot_v4.retire call-history --apply --proof /backup/restore-proof.json
python -m igngbot_v4.retire media-columns --apply --proof /backup/restore-proof.json
python -m igngbot_v4.retire ingress --days 30 --apply --proof /backup/restore-proof.json
```

`--apply` 拒绝：备份文件哈希不符、证明不属于当前数据库/服务器、候选表数据或 schema 已变化、bot/DSH 仍持租约、AI outbox 未完成，或消息发送仍为 pending/sending/unknown。每次操作取得独立 DB advisory lock。DDL 前先持久归档并逐行校验；归档腐坏或媒体覆盖不足时保留原表/列。中断记录保持 prepared，不能据此宣称阶段完成。

依次退役 `group_personality_configs`、`personality_profiles`、`context_summaries`、`system_prompts` 与 `call_logs`。前三列附件退役前逐条检查 stored_path/type 覆盖。调用正文去重后可以用原 ID 精确还原全部字段；失败、task ID、tokens、response 和 tool_calls 都保留。

Ingress 策略仅归档 `recording_status=recorded`、`delivery_status=delivered`、超过30天的正文，保留同一事件的去重 ID、命令结果和结果状态。当前未满30天的生产记录不提前清理。`yunying_events`、`yunying_sends`、`yunying_ai_records`、Session 状态、水位和 Memory 来源不清空。本轮提供显式运维入口，未设置无人值守定时清理。

## 回滚

停止 bot 与 YunYing，然后运行新镜像的 `python -m igngbot_v4.retire restore --apply`。它校验归档、恢复原 DDL 与原记录、恢复附件列位置/排序规则及 ingress 正文；消息撤回和清理后新增消息仍保留。已有旧表内容不一致时拒绝覆盖；完成过的媒体恢复也不会覆盖随后 V3/管理员的新值。

`deploy/deploy-v4-nas.sh rollback` 已串接上述恢复，再启动保存的 V3 固定镜像与原 base Compose。V3 本身没有 V4 租约，因此恢复前必须明确停掉 V3，不能仅依靠数据库租约检查。

退回上一版 V4 同样需要先恢复这些旧契约，因为上一版仍双写兼容调用/媒体列。恢复前必须停止消费者，并妥善处理未决发送/计费记录。完整 SQL 回灌是最后的灾难恢复路径，不可覆盖在线产生的新消息；优先使用保留当前新数据的精确归档恢复。

官方 DSH Session 始终保留原存储目录与 QQ 映射；SQL 恢复不替代官方 session durability。配对 SQL 与 DSH 备份要一起保存。导入镜像文件和临时配置现采用受保护留档，不硬删。

## 验证

本轮在独立 loopback MySQL8.0.36、官方 DSH0.2.1-alpha.1 上执行完整 Python/Node 套件；具体结果登记于测试和部署记录。新增真实 SQL 测试覆盖旧表缺失时撤回/历史/ASR字段、Prompt 完整还原、任务/未知 tokens 保留、过期入站与 pending 隔离、命令幂等、陈旧证明拒绝、未知发送拒绝、归档腐坏拒绝和中断重试。真实模型风格、真实 OCR/ASR 模型与长期在线压测没有因 SQL fixture 而自动完成。
