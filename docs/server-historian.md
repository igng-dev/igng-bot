# Minecraft 服务器史官

## 边界

Minecraft 插件采集原始事实 → Bot 固定来源水位、合并按时序保存事件快照 → 两条独立路径：日报只发布确定性统计（不调用模型、不产出自然语言）/ 周报走原生 DSH 多轮调查 → Bot 发布不可覆盖的版本 → Account 后台只读展示。

AI 工具没有 SQL、机械统计、QQ、Social Memory、shell 或其他主机工具。先连续分页扫描完整周期，再按时间/事件前后/玩家/聊天关键词回查；周报读取固定版本的历史周报作为线索，最终证据必须引用本次已读原始事件。聊天、工具数据、历史报告均不可信，不可变成指令。日报的机械统计不会传给模型，周报也不读日报。

## 周报 v2：读者不用再翻译

用户反馈旧周报要“再翻译一遍”：模型读的是原始事件 JSON（UUID、boot_id、英文 DEATH/LAVA、每5分钟一条 PRESENCE），提示词还写死了4条“主线”和“生态/脉络/智能体”之类术语，结果正文夹带代码和“事件显示”腔，提交也只是一段散文。v2 的改法（`igngbot_v4/historian/readable.py`）：

- **事件中文单行**：快照时服务端给每条事件生成 `line` 存进 payload，如 `10-14 周三 21:03 Steve 被僵尸杀死（主世界 120,64,-30）`、`Alex 进服`、`Alex 说：……`、`服务器停止`。事件类型、死因（damage_type / cause）、实体、世界名、踢出原因、原版进度用词典翻成中文；词典里没有的代码原样放括号里（如 `死亡（minecraft:new_thing）`），不猜。时间用报告时区。站点后续可直接显示 `line`。
- **模型视图瘦身**：timeline/context/search/player 每条只返回 `{ref, line, who}`，`who` 是本次 Run 固定的玩家代号（p1、p2…，按首次出现排序，名单存在 manifest.players），页内 `roster` 给出代号对应的名字。UUID、boot_id、capture_seq、content_hash 不进模型。`historian_player` 接收代号或玩家名；`historian_search` 搜中文行（含聊天原文、死因、地点、进度名）。
- **PRESENCE 不进模型**：统计在快照前已用完整来源计算；落库的模型快照不再存 PRESENCE（manifest 记 `source_event_count`、`omitted_event_types`），所以 `event_count` / `scan_through` / `coverage_complete` 只针对模型可见事件，连续分页、不能跳页、只能引用已读事件的保证不变。v2 之前安装的快照没有 `view_version=2`，读取会返回 409，重试即取新快照。
- **提示词**：去掉写死的主线和术语，改为“可以留意的方向，没有就不写”（服务器状况、建设与探索、玩家之间、新人、聊天大事）+ 直白写作规则（先写“谁 + 做了什么 + 结果”，用具体名词/地点/次数/日期，禁比喻和空话，原因不明写“记录里看不出原因”，正文不写代号/UUID/ref/英文代码）+ 差/好对照例句。提示词版本 `server-historian-v3`。
- **提交结构 v2**（`historian_observe` / `historian_submit`）：`headline`（≤50字）、`highlights`（3~6条，每条≤60字，带证据）、`stories`（1~12个：title≤20、gist≤60、timeline 2~8条每条≤60带证据、people 代号、outcome{resolved|ongoing|unknown, text≤40}、certainty、可选故事级证据）、`server_notes`（0~6条）。时间线的“周三 21:03”由服务端按证据时间算出并按时间排序，模型不写。条数下限在事件极少时按事件数放宽（要点 ≥min(3, 事件数)，时间线 ≥min(2, 事件数)）。
- **可读性校验**：正文出现玩家代号、UUID、事件编号、英文事件码/命名空间 ID、fact/inference 字样、空话（如“展开了”“引发了热议”“氛围融洽”“生态”“脉络”）、一句里堆两个以上“可能/似乎”、或超长，一律 400，错误里写明哪个字段、什么问题；DSH client 把 400/409 的说明回传给模型自行修改。看起来像代码的玩家名（如 `CREEPER_FAN`）与 TNT/PVP 等常用词不算违规。
- **存储与兼容**：迁移 `021_historian_report_v2.sql` 给 `historian_runs` 加 `report` JSON 列，保存带 `version: 2` 的完整结构（含服务端算出的时间）。`observations` 改为由 v2 派生的旧格式：一条“本周概览”（headline + 要点）、每个故事一条（summary = gist + 逐行时间线 + 结果）、一条“服务器状况”，证据取并集，现有 `/mc/admin/historian` 不改也能显示。
- **Markdown**：不再拼原始事件 ID；顺序为 headline → 本周要点 → 各故事（gist + 带时间的时间线 + 结果，非 fact 标可信度）→ 服务器状况。
- **往期周报线索**：`historian_previous` 对 v2 报告只给 headline、要点和故事标题；旧报告回退为 observations 的标题/摘要/可信度，不再附事件 ID 与 Markdown。

Report 是服务器+周期+时区的逻辑实体；周报每次执行 Run 都有新 UUID 和独立官方 DSH Session，日报 Run 由 Bot 机械发布、不进入 DSH。失败/租约过期不会替换当前成功版本；重试也是新 Run、新来源快照（周报另起新 Session）。手动展示切换含乐观锁，进行中的生成不能覆盖刚选定的版本。完整 JSONL 在独立 Profile 目录保存，数据库保存报告、证据、统计、来源水位及调用关联。

## 启用前配置（本功能合并不自动部署）

1. 使用包含 `historian-dsh/` 的固定 V4 Python/DSH 镜像；先备份并验证新增迁移 011/012/021（Bot 既有 checksum migration 入口；021 只加一个可空 JSON 列）。MC 连接只需读取采集表，Bot 库与 AI 库分别沿用既有连接。
2. 运维创建 `.env.historian`（gitignore），分别生成强且不同的 `HISTORIAN_ADMIN_SECRET`、`HISTORIAN_DSH_SECRET`。配置 `HISTORIAN_SERVERS` 的服务器 ID → IANA 时区。
3. 通过独立 `server-historian` Profile 的 operator patch 配置现有 DSH provider、模型、认证与真实 contextWindow；不要复制 QQ 人格/工具/会话。共享调用代码和镜像依赖，不共享 Social Agent。
4. 在既有受控 Compose 链路显式添加 `deploy/docker/docker-compose.historian.yml`；预创建持久目录 `historian-runtime` 并按已有 DSH 用户授权。该 overlay 不随普通 V4 部署自动启用。
5. Account 服务端配置 `HISTORIAN_ADMIN_API_URL` 和 admin secret，只用 HTTPS 内网或 loopback 隧道。DSH secret 不进入站点/浏览器。后台路径 `/mc/admin/historian`。
6. 历史 AI 日报：启用新版本前用 `scripts/purge-historian-daily-runs.py`（缺省 dry-run，`--apply` 执行）整行删除旧日报 run；脚本保留 AI 计费账本，遇到 pending/running 或未导出账本会拒绝执行。

可本地启动（完成配置后）：`python -m igngbot_v4.historian`、`sh scripts/run-historian-profile.sh`。官方 CLI 使用本地 link bundle，`historian-dsh` 和已安装依赖的 `yunying-dsh` 必须为同级目录；Dockerfile 已保留这一布局。

## 调度、预算和计费

- 每服业务时区的完整自然日、周一到周一自然周；跨 DST 按日历而非固定24小时处理。结束后默认600秒缓冲。重启有14日有界补调度，唯一 schedule key 防重复。`HISTORIAN_SCHEDULE=0` 可仅保留手工历史任务。日报由 Bot 的机械发布循环直接完成（不依赖 DSH 容器）；周报由 DSH 生成。
- 机械统计固定算法版本、原始来源水位。在线时长按 JOIN/QUIT/KICK 加裁剪推算；缺失 JOIN、未退出、记录器重载、历史秒精度、假人未知均显示质量限制。5分钟 PRESENCE 可提供缺失 JOIN 的下界，陈旧快照不会直接推算到周期末。首次观察是90日可用历史中的首个JOIN，不能冒充注册。D1/D7/D30显示成熟的历史首观队列在当前周期的回访；当前新增队列的未来留存不猜测。
- 周报 DSH 默认最多512步骤、200万token、3600秒，配置变量见示例。超额/失败保留执行，不发布正文；大周期超过采集预算也失败，不悄悄截断原文。官方 compaction 配置显式 headroom，运维仍须提供正确 contextWindow。
- 全部 turn、模型重试和 compaction 复用 NativeAccounting → `yunying_ai_records` outbox → `ai_jobs/ai_job_attempts` 的持久导出、Token 五桶、NULL 未知、现有 new-api 价格。每条关联 Run、Session、阶段。终态 JSONL 重扫恢复丢失投影，不重复计费；恢复时无法还原精确阶段的条目标为 `recovered`。

## 验证与风险

默认 Python / Node 套件有新增周期、统计、原生多步骤、工具隔离、预算测试。`YUNYING_TEST_DB=yunying_v4_test` 加 `YUNYING_RUN_PROFILE_SMOKE=1` 开启固定 loopback SQL 和真实官方 Historian CLI/Python 双进程协议测试，全部使用合成记录与模型 wire fixture。完整环境约定见 `testing.md`。

旧秒精度无法恢复真实同秒顺序；记录器启停不证明游戏进程启停；其他插件广播/AIPlayer 若不经过玩家聊天事件，可能未被采集。队列溢出、断电、历史边界会降低统计准确度。模型是否读懂事件和真实报告质量仍需实际运维验收（v2 的可读性校验只能拦住代码、代号、空话和超长，拦不住写错事实；付费模型影子验收尚未做）；程序覆盖扫描和引用校验不是因果正确性的证明。没有部署、生产迁移或付费模型验收时不能宣称这些完成。
