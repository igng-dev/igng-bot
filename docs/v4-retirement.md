# V4 数据退役与恢复操作

本文件延续机械记录与 Social Agent 分离：消息、附件、撤回、群开关和通用 AI 账本继续使用；DSH 官方 Session 与独立 MySQL Memory 文档/版本不清空，Memory 身份/审计表与恢复归档在回滚窗口关闭后按受控阶段退役。reserved2 Social Prompt 没有修改。原调查见 [数据库审查](v4-database-audit.md)。本文件说明受控操作；生产实际结果另行登记。

## 现役契约

V4 使用 `DBHandler(..., legacy_compat=False)`。V3 入口保留默认兼容模式，旧摘要失效、旧附件列和旧调用写入只供 V3 使用。V4 不再创建/写入 `call_logs`：native task/attempt 经 durable outbox 直接进入 `igng_sites.ai_jobs` / `ai_job_attempts`。历史已关联记录沿用原 website job key；未关联的早期请求使用 `dsh-legacy:<record_id>`，不会伪造新 Agent turn 或重复账单。未知 usage 继续为 NULL。

账户站点的群管理只读写 `group_configs` 的群名、聊天模式与硬暂停；摘要接口返回410，人格模板不再提供配置。调用列表只读正式通用账本，详情包括实际请求；旧 `legacy-<id>`/纯数字链接按原网站 job key（`ai_jobs.task_key=<id>`）解析，没有映射的早期记录只在冷备中保留。概览只统计正式通用账本，不把归档记录再算一次。所有接口继续要求 SuperAdmin。

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

归档位于既有受限 `igng_bot` 数据库；现有 DB 账号没有创建其他数据库的权限，本轮不扩权。归档不暴露给模型的 QQ/Memory 工具。回滚窗口内它们是正式恢复资料；窗口关闭后由受控 `memory-legacy` / `archive-tables` 阶段退役（见下），退役前必须保留配对冷备。

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
python -m igngbot_v4.retire memory-legacy --apply --proof /backup/restore-proof.json
# memory-legacy 修改了版本与归档表：重新取全库备份与证明，再退役恢复层
python -m igngbot_v4.retire archive-tables --apply --proof /backup/restore-proof-2.json
```

`--apply` 拒绝：备份文件哈希不符、证明不属于当前数据库/服务器、候选表数据或 schema 已变化、bot/DSH 仍持租约、AI outbox 未完成，或消息发送仍为 pending/sending/unknown。每次操作取得独立 DB advisory lock。DDL 前先持久归档并逐行校验；归档腐坏或媒体覆盖不足时保留原表/列。中断记录保持 prepared，不能据此宣称阶段完成。

依次退役 `group_personality_configs`、`personality_profiles`、`context_summaries`、`system_prompts` 与 `call_logs`。前三列附件退役前逐条检查 stored_path/type 覆盖。调用正文去重后可以用原 ID 精确还原全部字段；失败、task ID、tokens、response 和 tool_calls 都保留。

Ingress 策略仅归档 `recording_status=recorded`、`delivery_status=delivered`、超过30天的正文，保留同一事件的去重 ID、命令结果和结果状态。当前未满30天的生产记录不提前清理。`yunying_events`、`yunying_sends`、`yunying_ai_records`、Session 状态、水位和 Memory 来源不清空。本轮提供显式运维入口，未设置无人值守定时清理。

## 关闭回滚窗口（memory-legacy / archive-tables）

只在确认不再回滚 V3、且网站已部署为不读取归档表之后执行：

- `memory-legacy`：要求镜像已应用 010（`memory_versions.sources` 存在，否则拒绝）。先把 `memory_sources` 回填进对应版本的 `sources` JSON（只填 NULL，不覆盖新写入），校验无遗漏后把小表归档进 `yunying_legacy_tables` 并 DROP `memory_identities`、`memory_identity_bindings`、`memory_identity_audit`、`memory_sources`、`memory_audit`，最后删除 `memory_documents.identity_id` 列与 `memory_person` 索引；`memory_documents`/`memory_versions` 保留。
- `archive-tables`：要求 `memory-legacy`、`legacy-tables`、`call-history` 均已完成（对应表不存在），且没有 `payload_archived_at` 未还原的行。它逐表对照当前证明后 DROP `yunying_prompt_blobs`、`yunying_call_history`、`yunying_media_legacy`、`yunying_legacy_tables`、`yunying_ingress_archive`、`context_summaries`、`system_prompts`，最后 DROP `yunying_retirement_batches` 自身。

`memory-legacy` 会修改 `memory_versions` 与 `yunying_legacy_tables`，因此 `archive-tables` 必须使用 memory-legacy 之后重新生成的全库备份与证明；两个阶段同样受租约、AI outbox 与未决发送检查约束。执行完成后 `retire restore` 不再可用，恢复只能依赖配对冷备。

## 回滚

停止 bot 与 YunYing，然后运行新镜像的 `python -m igngbot_v4.retire restore --apply`。它校验归档、恢复原 DDL 与原记录、恢复附件列位置/排序规则及 ingress 正文；消息撤回和清理后新增消息仍保留。已有旧表内容不一致时拒绝覆盖；完成过的媒体恢复也不会覆盖随后 V3/管理员的新值。

`deploy/deploy-v4-nas.sh rollback` 已串接上述恢复，再启动保存的 V3 固定镜像与原 base Compose。V3 本身没有 V4 租约，因此恢复前必须明确停掉 V3，不能仅依靠数据库租约检查。

退回上一版 V4 同样需要先恢复这些旧契约，因为上一版仍双写兼容调用/媒体列。恢复前必须停止消费者，并妥善处理未决发送/计费记录。完整 SQL 回灌是最后的灾难恢复路径，不可覆盖在线产生的新消息；优先使用保留当前新数据的精确归档恢复。

官方 DSH Session 始终保留原存储目录与 QQ 映射；SQL 恢复不替代官方 session durability。配对 SQL 与 DSH 备份要一起保存。导入镜像文件和临时配置现采用受保护留档，不硬删。

## 验证

本轮在独立 loopback MySQL8.0.36、官方 DSH0.2.1-alpha.1 上执行完整 Python/Node 套件；具体结果登记于测试和部署记录。新增真实 SQL 测试覆盖旧表缺失时撤回/历史/ASR字段、Prompt 完整还原、任务/未知 tokens 保留、过期入站与 pending 隔离、命令幂等、陈旧证明拒绝、未知发送拒绝、归档腐坏拒绝和中断重试；关闭窗口阶段另有来源回填不覆盖新写入、身份列/索引删除、前置阶段拒绝与恢复层退役的用例。真实模型风格、真实 OCR/ASR 模型与长期在线压测没有因 SQL fixture 而自动完成。

### MySQL 恢复证明的 schema 表示

真实 MySQL8.0.36 dump/restore 演练暴露：相同有效列定义在 `SHOW CREATE TABLE` 中可能补出 `CHARACTER SET utf8mb4`，而原表示仅有同一 `COLLATE utf8mb4_general_ci`。校验现在仅归一化排序规则已经确定的冗余字符集声明，另逐列校验 information_schema 的类型、默认值、空值性、字符集/排序规则、EXTRA、备注与生成表达式哈希。索引、表参数、约束与 AUTO_INCREMENT 仍由完整规范 DDL 校验；引号内的默认值/备注不做替换。依据 [MySQL8.0列字符集规范](https://dev.mysql.com/doc/refman/8.0/en/charset-column.html)。证明格式升级为2，拒绝缺少独立列定义校验的旧证明。归档中的原建表原文不改写。

发现表示差异时没有继续退役生产数据；先恢复旧 V4 在线，再补充回归与完整验证。重新进入清理窗口时须重新生成 SQL/DSH 配对备份与全库证明，不能沿用恢复在线前的陈旧消息快照。
