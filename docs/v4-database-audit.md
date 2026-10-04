# V4 数据库审查与清理计划

本次只读审查 `igng_bot`，未执行 DROP、DELETE 或历史内容清空。证据是生产 MySQL8.0.36 的 information_schema、只读一致性快照中的统计，以及 bot/站点当前调用链。站点代码基线为 `17c14b36b156b7e0179a2ffbb001c66af0a4a940`。统计为时间点值，会随在线消息增长；没有把真实群号、账号、消息正文或凭据写进本报告。

## 结论

V4 不需要旧上下文摘要、旧人格选择和旧系统 Prompt 表参与推理。但四张表仍承载网站旧管理页或 V3 回滚，当前不能直接删除。最大的冗余来自调用日志的重复 Prompt 和附件兼容字段。新增 Session/队列/发送账本/AI 镜像/Memory 表均有明确职责，空表不代表没用。

### 旧表

2026-10-04 11:27:35（Asia/Shanghai），切换前快照：

| 表 | 行数 | 当前用途与判断 | 计划 |
| --- | ---: | --- | --- |
| `context_summaries` | 5 | V3 ContextManager 摘要；V4 不读取。站点 groups/summary API 与 groups JOIN 仍读取；DBHandler 启动仍建表。 | 冻结为 V3 历史，先替换站点查询、拆开 V4 bootstrap，再归档/退役。不能自动转成长期 Memory。 |
| `personality_profiles` | 3 | 当前 bot 的 V3/V4 均无执行引用；站点 groups API 仍列出人格。 | 移除网站无效人格控件与查询，完整归档后退役。 |
| `group_personality_configs` | 5 | 当前 bot 不使用；网站仍 JOIN、写入、删除关联。 | 与人格表一同退役，先断开网站读写。 |
| `system_prompts` | 1 | V4 使用原 reserved2 Prompt；表只服务 V3 回滚和旧迁移脚本。DBHandler 启动仍建表/seed。 | 回滚窗口结束后拆开 bootstrap 与 V3 Prompt 工具，再归档退役。 |
| `group_configs` | 9 | 群名与管理配置仍用。新增 `social_paused` 为明确的 V4 暂停权限；旧 `is_chat_mode` 只是“闲聊/仅艾特”，不可当暂停。 | 保留表、群名/时间与 `social_paused`；网站改为 V4 后再移除 `is_chat_mode`。 |
| `message_logs` | 40,188 | QQ 原始历史、引用、撤回、媒体和网站聊天镜像的基础。 | 保留，媒体列整理见下；历史保留期需另定。 |
| `message_recall_events` | 152 | 先撤回后补入消息时的持久 tombstone 与补偿状态。 | 保留；不能因 processed 就直接删除。 |
| `call_logs` | 3,408 | 站点调用列表、详情和概览仍读；V4 继续写入并镜像 ai_jobs/attempts。 | 保留表；旧 filter/summary 记录是历史审计，考虑冷归档而非即时删除。 |
| `mc_ticket_notification_state` | 2 | MC 工单增量游标。 | 保留，删除会丢失通知位置。 |
| `mc_ticket_notification_deliveries` | 116 | MC 通知去重账本。 | 保留，删除会有重发风险。 |

站点依据：`apps/account/app/api/admin/yunying/groups/route.js:29,45-52,118-121`；`groups/summary/route.js:23`；`calls/route.js:21,78`；`calls/[id]/route.js:21`；`overview/route.js`。聊天媒体依赖 `apps/igngchat/lib/chatlogs.js`。V4 依据：`igngbot_v4/main.py`、`views.py`、`journal.py` 与 `yunying-dsh/src/store.js/runtime.js`。

### 旧字段

| 字段 | 生产快照证据 | 判断与处理 |
| --- | --- | --- |
| `group_configs.is_chat_mode` | 9行，其中1为1；V3 网站文案明确“关闭仍保留定向 @ 回复”。 | V4 推理不读取；属于待退役字段，不能把0解释为暂停。先改网站 API/UI，回滚窗口结束后删除。 |
| `call_logs.thinking_content` | 3,408行中0行非空，0内容字节。 | V4 不写；网站详情仍有可选显示分支。可在移除该分支和旧 bootstrap 后退役，直接删的空间收益很小。 |
| `call_logs.task_id` | 8行有值，网站列表仍返回 taskId。 | 有历史关联，先保留。核对这8条关联是否仍需展示，不能按“V4不写”直接删。 |
| `call_logs.system_prompt/user_prompt` | 非空各3,408行，内容分别22,086,897与20,209,373字节。 | 约40.3MiB文本。旧日志大量重复，V4 不再存完整运行上下文；站点详情仍展示。先把历史 Prompt 按 hash 去重或冷归档并保留查阅入口，再迁移字段。不能混作 Memory。 |
| `message_logs.file_url/file_type` | 5,592行有值；5,592个路径都已出现在 `attachments_json[*].stored_path`。 | 具备整理为 attachments 的基础，但 V4 图片读取还有 file_url fallback，复用 ingest 仍双写，V3 回滚也读它。先删依赖/双写并验历史图片、文件、转发，再退役列。 |
| `message_logs.audio_file_path` | 15行有值，15个路径均在 attachments 中。 | 同样可在媒体契约迁移后退役；当前 ingest 仍写，不能直接删。 |
| `message_logs.audio_transcript` | 0行非空。 | OCR/ASR 流程仍使用、写入此字段，并非死字段。先统一 attachments 中的转写契约与旧记录显示再判断，空值不能作为删列理由。 |
| `message_content/plain_text_content/message_structure/attachments_json` | 文本、消息段与附件分别被模型 view、转发/引用和网站使用。 | 保留。它们分别是原始表示、可检索文本、结构与媒体元数据，不应粗暴合成一个字段。 |
| `msg_id/reply_to_msg_id/is_self/is_recalled/recalled_at/recall_operator_id/message_source` | 去重、引用、自身回声、撤回权限和来源链仍使用。 | 保留。 |

数据量来自 `OCTET_LENGTH` 合计，不等于 InnoDB 实际磁盘回收量；DROP/压缩也不能承诺立即释放表文件空间。附件覆盖是快照匹配，并不替代文件存在、转发嵌套结构或旧站点消费者的验收。

## V4 新结构与保留边界

新增12张业务表加1张 migration 登记表，数据库从10张变为23张；第二条迁移只为 `group_configs` 增加默认0的 `social_paused`，保留旧值并支持 DDL 已提交、checksum 未登记时重试。

| 表 | 为什么要保留 |
| --- | --- |
| `yunying_sessions` | QQ↔官方 DSH Session 映射、provider、provisioning、社会状态和运行态 paused；DSH真实历史仍在官方持久目录。 |
| `yunying_ingress` | OneBot 原事件/媒体预处理后的持久 FIFO、失败重试，不能先删 pending。 |
| `yunying_events` | 原生 Inbox 的稳定消息 ID、序列、水位恢复和 Memory 来源。当前恢复会读取此表，不能直接按 delivered/日期清空。 |
| `yunying_sends` | 幂等发送账本，unknown 不能自动重发；清空会破坏去重。 |
| `yunying_ai_records` | DSH事件↔call_logs↔网站镜像的幂等/outbox；历史回放依赖稳定 record ID。 |
| `memory_identities/memory_identity_bindings/memory_identity_audit` | 跨平台身份、本人共享授权与审计；群私有权限不由昵称决定。 |
| `memory_documents/memory_versions/memory_sources/memory_audit` | Markdown正式记忆、CAS版本/回滚、原事件来源与访问/修改审计。 |
| `yunying_schema_migrations` | 已应用迁移 checksum 与可重试性边界。 |

短期没有记忆文档，只表示尚未写入，不能删 Memory 的空表。MySQL Memory、原始QQ消息和 DSH Session 是三层；不把旧 summary 灌进 Memory，也不把官方 JSONL 改成散落的 Markdown。

## 清理顺序

1. **先改消费者。** 站点移除旧人格、聊天模式和摘要读写；提供受控的 Session状态/明确暂停及 Memory 管理入口。V4 bootstrap 拆出必要消息/配置/调用表，停止创建已退役表/列，V3 仍留独立回滚入口。
2. **保留回滚窗口。** 建议至少稳定运行30天再关闭 V3 回滚；时间是建议，尚未执行。先备份候选表/列与索引、核对行数和 checksum，并验证恢复。归档要在受保护目录/库，不把用户数据提交 Git。
3. **优先减重复体积。** 对历史 call_logs Prompt 做内容 hash 去重或冷归档；站点详情仍能按归档指针查阅。媒体字段先去双写与 fallback，经图片/语音/转发/旧日志验收后再迁移。保留原始历史和审计关系。
4. **再做队列与 outbox 保留策略。** `yunying_ingress` 仅考虑已完成且超过保留期的预处理大 payload；Native事件必须先建立持久恢复 checkpoint 和 Memory 来源快照。发送/AI镜像即使裁掉大正文，也保留稳定 ID、状态、payload hash、message/call_log ID 等去重 tombstone。当前不能直接删 delivered/sent 记录，否则重启回放可能重建/重复写入。
5. **最后才有破坏性 migration。** 逐表/字段列出范围并获得明确授权；先验证网站不再 JOIN/SELECT、程序不会重建，再 DROP。此次没有执行这些操作，原始记录和 Memory 未被清空。

当前 schema 没有外键、view、routine 或 trigger，并不代表没有软关联；网站SQL、Memory sources、发送去重与MC游标都是实际依赖。涉及 `igng_sites`、`mc` 的数据另属站点/MC，不纳入本次清理。
