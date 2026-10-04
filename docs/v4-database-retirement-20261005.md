# V4 数据退役与 NAS 部署实录（2026-10-05）

这次清理已经在生产执行；不是仅有迁移文件或合成测试。网站消费者先上线，机器人使用同一 `igngbot` Compose 与既有数据路径。机械消息/附件/撤回/群模式继续运行，聊天模式控制 DSH 触发，正式任务及请求写入网站 `igng_sites.ai_jobs` / `ai_job_attempts`。真实 Agent 历史及压缩交给官方 DSH Session；MySQL Memory 是独立长期记忆层。

## 实际数据库变化

005 已先以增量方式应用，新增六个受控归档/审计表与 `yunying_ingress.payload_archived_at`。本轮四个运维阶段均为 complete。数据库由迁移后的29表变为24表（18张现役、6张归档/审计）；归档是恢复资料，不应作为下一轮垃圾删除。

| 退役项 | 实际结果与保存方式 |
| --- | --- |
| `group_personality_configs`、`personality_profiles`、`context_summaries`、`system_prompts` | 合计14行与原始 DDL、内容哈希保存在 `yunying_legacy_tables`，原表已退役。旧 summary 没有导入长期 Memory。 |
| `call_logs` | 3,565条完整 metadata、原 ID、task/usage/失败及正文保存在 `yunying_call_history` 与 `yunying_prompt_blobs`；原表退役，旧数字详情链接仍有效。 |
| `message_logs.file_url`、`file_type`、`audio_file_path` | 全40,885行通过附件覆盖检查；5,697行兼容字段值与原定义/位置已归档，三列退役。`audio_transcript` 继续保留。 |
| 完成超过30天的 ingress 正文 | 本次 eligible=0、archived=0，不提前清理。提供显式运维入口，没有新增无人值守删除任务。 |

正文原文共42,316,607字节；内容寻址后为4,164个 blob、24,979,382字节，减少17,337,225字节（约16.5 MiB）的重复正文。NULL/空字符串、原 Prompt 和 thinking 都保留。此数字是逻辑正文去重，不等于已测量的磁盘释放量；完整备份仍保留。

生产启动前再次逐行证明：17张其他现役表的行、列定义及 DDL 不变，40,885条消息的现役字段完全一致，调用和媒体归档可精确重建原数据。Memory、Identity、Session映射/状态、水位、事件、发送和计费去重链未清空。MC通知两表保留。

## 备份与真实回滚演练

配对目录：`/vol2/1000/Docker/igngbot/v4-backups/20261004-160628-pre-retirement-281684780690/`。目录0700、文件0600，含完整 `igng_bot.sql.gz`、官方 `dsh-runtime.tar.gz`、原配置/Compose、镜像/挂载记录、校验及运维证据。gzip完整性和dump结束标记通过，SHA256SUMS在 NAS 上实际校验成功。

完整 SQL 在不同 server UUID 的独立 MySQL8.0.36服务恢复：**29表、46,495行**，全部行、有效列定义、规范DDL、索引、字符集、默认值和AUTO_INCREMENT一致。随后在隔离副本执行全部退役阶段及 `restore --apply`，23张原业务表与46,495行再次完全一致。没有在该副本启动真实 QQ 或模型。

实际 dump/restore 的 `SHOW CREATE TABLE` 会补出由同一 COLLATE 已确定的冗余 CHARACTER SET。证明格式2仅归一化此显示差异，另独立校验实际列定义；引号内默认值、备注不改写。旧证明、真实类型/排序规则/计数器/正文变化仍拒绝。初次差异出现时生产未 DROP，恢复旧版本在线后重新取本次备份，未复用陈旧快照。

官方后端实际读取备份：9个 Session、2,360个事件、无损坏尾部。生产重启后同9个日志的备份字节前缀全部保留，官方读取已有2,361个事件、无损坏尾部。运行期间计数继续变化；上述是检查时快照。SQL恢复不能代替官方Session备份。

附件原目录没有移动或GC，本轮配对包不包含物理附件树副本；附件仍沿用既有NAS存储与备份体系。

## 部署结果

- NAS根：`/vol2/1000/Docker/igngbot`。同一Compose包含bot、YunYing、NapCat、media与frpc；共享MySQL在现有数据库服务，未迁入新容器。
- Python：`igngbot-v4:281684780690`，image ID `sha256:2a86331d1715b1c38edc63ef861a4e096195df58ee65ff5ca055a49981d350c1`。
- DSH：`igngbot-yunying:281684780690`，image ID `sha256:54721b14d09817c3c070be7d35a40b6a1d622fd11da37a5e426d975cda9408e2`。插件源码与此前版本相同；保留官方DSH0.2.1-alpha.1。
- `docker compose ... up -d --wait --wait-timeout 180 --no-build bot yunying`成功。bot、YunYing、NapCat、media健康，frpc原进程继续运行。
- bot容器仍使用兼容名称 `igngbot-v3`，实际镜像及入口为 V4 / `python -m igngbot_v4`，没有第二个V3 Agent或消息消费者。
- 运行中的36个Python/迁移文件与11个Node配置/源码文件逐个SHA256匹配已验证源码；真实Profile包解析到 `/app/yunying-dsh`。镜像构建源281684780690与修訂PR8的提交树完全一致。
- 网站账户中心来自92641af0c3fb445ccdccb7a1f89e0c66c08a17da，经既有 [Actions部署](https://github.com/igng-dev/igng-sites/actions/runs/37213438709)构建、上传、重启和健康检查成功。没有手工上传工作区到VPS。

生产只读验证：OneBot `get_login_info`成功；V4历史接口读取3条记录；10条撤回视图内容/附件正确遮蔽；抽查288个图片、21个表情、5个文件、1个视频路径及全部15个旧语音路径存在，图片view可实际解码。运行后旧表与旧列没有被自动重新创建。最后一次机械记录检查较维护前新增9条消息、18条ingress，Session映射哈希不变，计费outbox和未决发送均为0。

网站实际生产查询：正式321个任务、归档3,565条，原数字历史详情及正文哈希一致，原生任务5个请求详情可读；已知总token为8,959,029、unknown请求0，与清理前快照一致。归档没有重复加入统计。未登录 `/api/admin/yunying/calls` 与 `/groups`均返回401。使用正式部署源码进行生产只读查询，不代表已经人工验收SuperAdmin浏览器交互。

## 自动化验证

| 实际命令/环境 | 结果 |
| --- | --- |
| `python -m pip install -r requirements-dev.txt`，共享venv | 依赖检查通过。 |
| `YUNYING_TEST_DB=yunying_v4_test YUNYING_TEST_DB_PORT=43316 YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q`，独立MySQL8.0.36及官方双进程 | 114 passed，0 skipped，73条已知幂等建表/旧ping warnings。 |
| `YUNYING_TEST_DB=yunying_v4_test YUNYING_TEST_DB_PORT=43316 npm --prefix yunying-dsh test` | 30 passed，0 skipped。 |
| `npm --prefix yunying-dsh run check`、部署/Profile脚本语法、privacy-scan、diff --check | 全部通过。 |
| 网站 `pnpm verify` | 61个根测试及工作区测试、lint、typecheck和11个站点构建通过；[最终CI](https://github.com/igng-dev/igng-sites/actions/runs/37213437606)通过。 |
| 机器人修訂 [PR8 CI](https://github.com/igng-dev/igng-bot/actions/runs/37215664087) | 通过，包括MySQL8.4与官方双进程。 |

reserved2 Social Prompt、QQ工具契约、官方Loop/Compaction、Memory跨群/私有隔离与聊天模式回归没有改写。测试继续覆盖OFF普通来信不启动模型、呼叫稳定处理、同会话串行、wait/wake、搜索、Memory CRUD/权限、撤回/媒体、Compaction续接及双进程重启。真实模型风格、公网付费搜索、真实OCR/ASR模型、长时压测没有因这次SQL/文件验证而重新验收；已有依赖安全告警未在本次扩大处理。

代码保留V3显式回滚入口、机械基础设施和正式网站计费客户端；V4不再使用V3 `ContextManager`、should_reply/纯文本回复链、旧摘要/人格Prompt管理或call_logs双写。donor reserved2行为与Social Prompt沿用此前移植，本次没有重新设计群聊风格。

## 回滚边界

先停止bot与YunYing，用当前新镜像执行 `python -m igngbot_v4.retire restore --apply`，再启动旧V4固定镜像；`deploy/deploy-v4-nas.sh rollback`已串接同一恢复后启动保留的V3镜像。V3固定回滚点是 `igngbot-v3:rollback-20261004-e33cc69b193c`，上一生产V4为 `igngbot-v4:579e33f22617` / `igngbot-yunying:579e33f22617`。恢复已在隔离副本实测，生产没有为演练反复切换V3。

归档恢复保留清理后新增数据；遇到已存在且不同的旧表/字段值拒绝覆盖。完整SQL回灌只作灾难恢复，不能覆盖在线新消息。完整步骤与 guard 见 [操作手册](v4-retirement.md)。清理阶段提供的受控工具不向模型暴露SQL、备份或通用文件读写。

代码集成：[机器人PR6](https://github.com/igng-dev/igng-bot/pull/6)、[恢复证明PR8](https://github.com/igng-dev/igng-bot/pull/8)、[网站PR42](https://github.com/igng-dev/igng-sites/pull/42)均已合并。PR7已被相同内容的线性PR8替代关闭。
