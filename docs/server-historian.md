# Minecraft 服务器史官

## 边界

Minecraft 插件采集原始事实 → Bot 固定来源水位、合并按时序保存事件快照 → 两条独立路径：确定性统计 / 原生 DSH 多轮调查 → Bot 发布不可覆盖的版本 → Account 后台只读展示。

AI 工具没有 SQL、机械统计、QQ、Social Memory、shell 或其他主机工具。先连续分页扫描完整周期，再按时间/事件前后/玩家/聊天关键词回查；周报读取固定版本的日报和历史报告作为线索，最终证据必须引用本次已读原始事件。聊天、工具数据、历史报告均不可信，不可变成指令。

Report 是服务器+周期+时区的逻辑实体；每次执行 Run 都有新 UUID 和独立官方 DSH Session。失败/租约过期不会替换当前成功版本；重试也是新 Run、新 Session、新来源快照。手动展示切换含乐观锁，进行中的生成不能覆盖刚选定的版本。完整 JSONL 在独立 Profile 目录保存，数据库保存报告、证据、统计、来源水位及调用关联。

## 启用前配置（本功能合并不自动部署）

1. 使用包含 `historian-dsh/` 的固定 V4 Python/DSH 镜像；先备份并验证新增迁移 011/012（Bot 既有 checksum migration 入口）。MC 连接只需读取采集表，Bot 库与 AI 库分别沿用既有连接。
2. 运维创建 `.env.historian`（gitignore），分别生成强且不同的 `HISTORIAN_ADMIN_SECRET`、`HISTORIAN_DSH_SECRET`。配置 `HISTORIAN_SERVERS` 的服务器 ID → IANA 时区。
3. 通过独立 `server-historian` Profile 的 operator patch 配置现有 DSH provider、模型、认证与真实 contextWindow；不要复制 QQ 人格/工具/会话。共享调用代码和镜像依赖，不共享 Social Agent。
4. 在既有受控 Compose 链路显式添加 `deploy/docker/docker-compose.historian.yml`；预创建持久目录 `historian-runtime` 并按已有 DSH 用户授权。该 overlay 不随普通 V4 部署自动启用。
5. Account 服务端配置 `HISTORIAN_ADMIN_API_URL` 和 admin secret，只用 HTTPS 内网或 loopback 隧道。DSH secret 不进入站点/浏览器。后台路径 `/mc/admin/historian`。

可本地启动（完成配置后）：`python -m igngbot_v4.historian`、`sh scripts/run-historian-profile.sh`。官方 CLI 使用本地 link bundle，`historian-dsh` 和已安装依赖的 `yunying-dsh` 必须为同级目录；Dockerfile 已保留这一布局。

## 调度、预算和计费

- 每服业务时区的完整自然日、周一到周一自然周；跨 DST 按日历而非固定24小时处理。结束后默认600秒缓冲。重启有14日有界补调度，唯一 schedule key 防重复。`HISTORIAN_SCHEDULE=0` 可仅保留手工历史任务。
- 机械统计固定算法版本、原始来源水位。在线时长按 JOIN/QUIT/KICK 加裁剪推算；缺失 JOIN、未退出、记录器重载、历史秒精度、假人未知均显示质量限制。5分钟 PRESENCE 可提供缺失 JOIN 的下界，陈旧快照不会直接推算到周期末。首次观察是90日可用历史中的首个JOIN，不能冒充注册。D1/D7/D30显示成熟的历史首观队列在当前周期的回访；当前新增队列的未来留存不猜测。
- DSH 默认最多512步骤、200万token、3600秒，配置变量见示例。超额/失败保留执行，不发布正文；大周期超过采集预算也失败，不悄悄截断原文。官方 compaction 配置显式 headroom，运维仍须提供正确 contextWindow。
- 全部 turn、模型重试和 compaction 复用 NativeAccounting → `yunying_ai_records` outbox → `ai_jobs/ai_job_attempts` 的持久导出、Token 五桶、NULL 未知、现有 new-api 价格。每条关联 Run、Session、阶段。终态 JSONL 重扫恢复丢失投影，不重复计费；恢复时无法还原精确阶段的条目标为 `recovered`。

## 验证与风险

默认 Python / Node 套件有新增周期、统计、原生多步骤、工具隔离、预算测试。`YUNYING_TEST_DB=yunying_v4_test` 加 `YUNYING_RUN_PROFILE_SMOKE=1` 开启固定 loopback SQL 和真实官方 Historian CLI/Python 双进程协议测试，全部使用合成记录与模型 wire fixture。完整环境约定见 `testing.md`。

旧秒精度无法恢复真实同秒顺序；记录器启停不证明游戏进程启停；其他插件广播/AIPlayer 若不经过玩家聊天事件，可能未被采集。队列溢出、断电、历史边界会降低统计准确度。模型是否读懂事件和真实报告质量仍需实际运维验收；程序覆盖扫描和引用校验不是因果正确性的证明。没有部署、生产迁移或付费模型验收时不能宣称这些完成。
