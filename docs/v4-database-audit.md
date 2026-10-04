# V4 数据库清理计划与现役审查

> 以下保留清理前的审查快照与计划。用户随后授权完整执行；实现与受控恢复流程见 [V4数据退役](v4-retirement.md)，执行结果以新部署记录为准。

本计划依据 NAS 现役 `579e33f22617`、生产 MySQL `8.0.36` 的只读一致性快照和 bot/网站调用链。快照时间：**2026-10-04 20:42:58 UTC+08:00**。`igng_bot` 仍为23张表。统计会随在线流量增长；报告只含聚合结果，不含群号、账号、消息正文或凭据。

本轮只完成 V4 正常升级的003/004增量迁移，**没有执行数据库清理、DROP、DELETE、TRUNCATE、表重命名或历史正文清空**。部署、校验和回滚点见 [本次 NAS 升级记录](v4-nas-update-20261004-579e33f.md)；首次部署快照另见 [旧记录](v4-nas-deployment-20261004.md)。

## 建议先做什么

先退役4张 V3 专用表，保留全部机械记录、群开关、通用 AI 记录、V4 队列/账本和 Memory。4张候选表合计只有14行，收益主要是去掉失效模型和维护依赖；它们不是当前主要空间来源。`call_logs` 的 InnoDB data_length 约81.6MiB，是本库最大单表，但它仍有消费者和历史记录，不能因 V4 改用通用 AI 表就整表删除。

**当前不能直接删 `context_summaries`。** `igngbot_v3/db.py::mark_message_recalled` 在与 `message_logs`、`message_recall_events` 同一事务中执行 `UPDATE context_summaries`。V4 复用此机械撤回路径；缺表会使真实撤回事务失败/回滚。拆掉 V4 bootstrap 的旧摘要建表并不等于已经拆掉所有依赖。清理前必须把旧摘要失效逻辑移到 V3 专用路径，并用不含该表的真实 SQL 数据库验证撤回。

## 第一批候选退役表

| 表 | 实际行数 | 现存依赖 | 退役前必须完成 |
| --- | ---: | --- | --- |
| `context_summaries` | 5 | V3 模型上下文摘要；V4 Agent 不使用，但复用撤回事务仍更新它，网站两条摘要 API 和群列表 JOIN 仍读取。 | 先将摘要失效逻辑移回 V3 专用路径，再移除网站摘要控件/API/JOIN。 |
| `personality_profiles` | 3 | V4 使用原 reserved2 Social Prompt；站点仍列出人格列表，群查询仍 JOIN。 | 移除无效人格控件、列表查询和关联 JOIN。 |
| `group_personality_configs` | 5 | V4 不按人格表选择 Prompt；网站 PATCH 仍写入/删除该关联。 | 与人格表同时移除网站读写，先解除关联依赖再退役父表。 |
| `system_prompts` | 1 | V4 不读取；V3 SystemPromptStore 与原回滚入口仍依赖，旧建表/seed 已限于 V3。 | 保留到 V3 回滚窗口结束；备份后再退役。 |

移除旧人格或摘要不等于改写 Social Prompt：reserved2 原文继续保留，也不把旧摘要自动灌入长期 Memory。V3 镜像/代码备份继续保留；退役旧表后，V3 回滚必须先恢复对应旧 schema/数据，不能声称仅切镜像即可完整回滚。

## 字段候选与明确保留项

| 字段 | 当前证据 | 清理方案 |
| --- | --- | --- |
| `call_logs.thinking_content` | 3560行，非空0行。V4 不写；旧 UI 和 V3 INSERT/建表仍有契约。 | 低优先级退役候选：先清 UI/API 和 V4 建表契约，结束或适配 V3 回滚后再删列。不是空间清理重点。 |
| `call_logs.system_prompt` | 3560条非空，共22,096,321字节；729个 SHA-256 内容。去重正文约4,963,336字节。 | 优先考虑内容 hash 去重/受限冷归档，保留按原 call ID 查阅的权限入口。重复正文理论差额约16.3MiB；不是可直接释放的磁盘空间。 |
| `call_logs.user_prompt` | 3536条非空，共20,219,826字节；3433个内容，去重正文仍约20,016,016字节。 | 内容大多不同，hash 去重收益很小；若减体积应采用有索引、可检索的历史归档，不能直接置空。 |
| `message_logs.file_url/file_type` | 路径和类型各5657条非空；全部路径都在 `attachments_json[*].stored_path` 中。 | 第二批契约整理候选。先去 ingest 双写、DB INSERT/SELECT 和 V4 image fallback，统一读 attachments，再验证历史图片、文件、转发和网站预览。 |
| `message_logs.audio_file_path` | 15条非空；全部有 attachments 路径覆盖。 | 随媒体契约统一后再退役；当前录入仍写，不能先删列。 |
| `message_logs.audio_transcript` | 当前非空0行，但 ASR 录入仍使用此字段。 | 保留；只有转写正文、状态与显示全部迁移到 attachments 后才重新评估。空表/空列不是死契约的证据。 |
| `call_logs.task_id` | 8条非空，网站调用列表返回 taskId。 | 保留历史关联，先核对外部任务和查阅需求。 |
| `group_configs.is_chat_mode/social_paused` | 9群，1群自主模式开启，硬暂停0群。 | 两列都保留：前者控制自主参与/仅明确呼叫，后者禁止呼叫与发言。旧计划中“移除聊天模式”的条目已经撤销。 |

两类 Prompt 合计 **40.4MiB** 文本，不代表它们可全部删除，也不代表 DROP/OPTIMIZE 能立即释放同等物理空间。附件覆盖仅证明数据库路径重复，不替代实际文件、嵌套转发、OCR/ASR 或旧版本恢复验收。

`message_content/plain_text_content/message_structure/attachments_json` 分别承载原始表示、可检索文本、结构和媒体元数据，均保留；消息 ID、引用、自身回声、来源、撤回状态/时间/操作者字段也保留。

## 调用日志和网站通用 AI 表

`igng_sites.ai_jobs/ai_job_attempts` 是正式用量与任务账本，继续使用。本快照 bot service 有320个 job、324个 attempt；现役原生 v2 记账已出现并完成 `social_turn`，工具续接的多次模型请求属于同一 job，compaction 单独记账。未知 provider 用量继续 NULL，不能把失败/旧记录补造为零账单。

`call_logs` 暂时还不能退役：站点的 calls 列表、两个详情入口和 overview 仍查询它。按既有 `service='igng-bot'` 与旧 `task_key=call_log_id` 主键关联，**3560行中只有319行有旧格式站点映射**。新原生记账按 Session/turn/request 主键，数量不一一对应；不能据此宣称剩余明细已经在站点完整备份。

建议后续把调用管理页面/概览迁到通用 AI 表，同时给旧 call ID 提供受限历史归档索引。两边结果可对账、历史可查且保留原 task_key/request_id 后，再停止 V4 兼容双写。历史迁移与查询适配不能重复累计 tokens，也不能清掉原失败、filter 或 summary 审计。`yunying_ai_records.call_log_id` 仍有当前兼容关联，先保留；通用记账已经不依赖它成功。

## 明确保留的基础表

| 表 | 行数 | 用途 |
| --- | ---: | --- |
| `message_logs` | 40665 | QQ 原始消息、引用、结构化历史、图片/语音和网站聊天镜像。 |
| `message_recall_events` | 156 | 撤回先于原消息时的 tombstone 与补偿状态，不能按 processed 清空。 |
| `group_configs` | 9 | 群名、`is_chat_mode` 和独立 `social_paused`；聊天模式必须保留。 |
| `call_logs` | 3560 | 旧网站列表/详情/概览和未完整映射的历史审计；本轮不列为可直接删除的表。 |
| `mc_ticket_notification_state` | 2 | MC 工单通知游标，删除会丢失通知位置。 |
| `mc_ticket_notification_deliveries` | 116 | MC 通知幂等账本，删除可能重发通知。 |

## 明确保留的 V4 表

| 表 | 行数 | 用途 |
| --- | ---: | --- |
| `yunying_sessions` | 9 | QQ↔官方 DSH Session 映射、恢复状态和呼叫授权。 |
| `yunying_ingress` | 490 | 独立机械记录与 DSH 投递的持久 FIFO、重试和命令幂等。 |
| `yunying_events` | 478 | 原生 Inbox 接纳、消息序列/水位恢复、观察历史和 Memory 来源。 |
| `yunying_sends` | 15 | 稳定 request ID、发送结果与去重，unknown 必须人工核对。 |
| `yunying_ai_records` | 189 | 原生 attempt/task-end 的幂等 outbox；站点故障不丢记账。 |
| `yunying_schema_migrations` | 4 | 001—004 已应用版本/checksum，不能清空或改写历史迁移。 |
| `memory_identities` | 24 | 跨会话 person identity。 |
| `memory_identity_bindings` | 24 | QQ/其他平台绑定和本人共享授权。 |
| `memory_identity_audit` | 24 | 绑定/授权审计。 |
| `memory_documents` | 0 | MySQL 正式 Markdown 长期记忆；当前为空不是废表。 |
| `memory_versions` | 0 | 版本、CAS 更新、回滚。 |
| `memory_sources` | 0 | 原事件与可见性来源链。 |
| `memory_audit` | 5 | Memory 访问、拒绝、修改与遗忘审计。 |

原始 QQ 数据、DSH 官方 Session、MySQL 长期 Memory 是三个独立层次。不能删 DSH 日志当作“context 清理”，不能把 compaction summary 当长期记忆，也不能因 Memory 文档目前为0而删它的正式结构。

## 分阶段执行顺序

1. **消费者和契约修复，无破坏性 SQL。** 在 bot 中将摘要失效处理移到 V3 专用撤回路径；V4 仅更新消息/撤回账本。站点移除摘要/人格 UI、API 和 JOIN，保留群名、聊天模式、权限和历史消息。核对 CLI/旧迁移工具的 system_prompts 依赖，并运行旧表缺失的真实 SQL 撤回测试以及网站群列表/保存测试。这些修改未在本次部署中执行。
2. **冻结并验证第一批归档。** 对4张候选表的 schema、索引、全量记录、行数和 checksum 做受保护 SQL 归档；在隔离库实际恢复验证。建议 V4 从本次升级开始稳定运行至少30天，再结束无需 schema 恢复的 V3 回滚窗口；这是建议，尚未设定自动删除日期。历史记录不自动转成 Memory。
3. **独立、受控的第一批退役 migration。** 归档与消费者验收完成后，逐项列出 `group_personality_configs`、`personality_profiles`、`context_summaries`、`system_prompts` 的退役 SQL。确认具体范围后执行；不把破坏性动作偷偷混入普通启动/升级，不改001—004已应用文件。恢复以归档 schema/数据为依据，不能只新建空表/seed 掩盖丢失。
4. **调用记录统一与 Prompt 归档。** 先让网站主要查询通用 AI 表，完成旧 call ID 查询/权限及 tokens 对账，再停止兼容双写；优先去重 system_prompt，user_prompt 采用可回查的冷归档。候选新引用字段/归档索引和 nullable 策略另做 additive migration，读写切换和数据核对通过后才退役旧正文列。
5. **附件字段统一。** 用 attachments 作为路径/类型/转写的唯一契约，先改写入与读取并对比新旧路径，再测试图片、文件、语音、OCR/ASR、嵌套转发、撤回隐藏和网站预览。之后才退役 `file_url/file_type/audio_file_path`；`audio_transcript` 需另行确认转写迁移完成。保护原附件文件，数据库去重不触发物理文件 GC。
6. **最后建立运行数据保留策略。** 可以从“已 recorded + delivered、超过30天”的 ingress 大 payload 评估冷归档，但必须先建立恢复 checkpoint、可验证的 Memory 来源快照和保留去重记录。`yunying_events`/`yunying_sends`/`yunying_ai_records` 的稳定 ID、序列、水位、hash、结果与关联不可随正文一并清空；pending/unknown 未决记录保留。没有这个前置能力时不执行日期 DELETE 或 TRUNCATE。

每一阶段都单独交付代码/迁移、完整备份 manifest、实际验收和回滚方法。此次只交付计划，没有执行这些清理阶段；也没有修改 `igng_sites` 的通用表 schema、MC 数据或网站源码。

## 依赖依据与清理验收

bot 基线：`579e33f22617465816c6b6335c6fbbba6ef3e5ec`。网站只读基线：`17c14b36b156b7e0179a2ffbb001c66af0a4a940`；其他站点任务工作区未被修改。

- 撤回软依赖：`igngbot_v3/db.py::mark_message_recalled`；V4 通过原 message ingest/client 路径复用。
- 媒体依赖：`igngbot_v3/message_ingest.py`、`db.py::insert_message`、`igngbot_v4/views.py::image_paths`；网站 `apps/igngchat/lib/chatlogs.js` 当前主要读取 attachments/structure。
- 网站旧模型：`apps/account/app/api/admin/yunying/groups/route.js` 的 GET JOIN 与 PATCH 人格写入；`groups/summary/route.js`。
- 网站旧调用页：`calls/route.js`、`calls/[id]/route.js`、`overview/route.js`；正式原生记账：`igngbot_v4/ai_records.py`。
- 本库无外键、view、routine、trigger，仍有上述真实 SQL/恢复/来源软关联，不能用“没有外键”作为可直接删除的依据。

最终清理验收至少包括：旧表缺失时群列表/开关/撤回正常；开关关闭记录仍增长且明确呼叫可工作；附件与旧历史可查；站点 attempt/jobs 不重复计费、unknown 不被变零；Memory 权限和版本/来源完整；相同 QQ↔DSH 映射及官方 Session 重启可恢复；归档在隔离库恢复成功。网站尚未适配、真实 OCR/ASR 和长期运行尚未验收的部分，不能在清理完成报告里略过。
