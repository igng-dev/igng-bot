# 群聊日报（每日群聊分析）

按「群 × 业务时区自然日」生成一份日报：机械统计（确定性、不调用模型）+ 原生 DSH 多步调查得出的结构化分析。同一份报告同时给管理员和群成员看。只按天分析，不做周报。

## 边界

原始聊天 `message_logs` → 平台适配器固定水位、生成去标识快照 → Python 机械统计（不进入模型）→ 独立 `chat-analyst` DSH Profile 逐页读完全天、回查、提交带证据的结构化日报 → Python 校验后发布不可覆盖的版本 → 站点只读展示，管理操作走内部 admin API。

- **平台抽象**：报告身份是 `(platform, conversation_type, external_id, period_start, timezone)`，与 `yunying_sessions` 的会话三元组一致。`igngbot_v4/chat_analysis/source.py` 的适配器负责把各平台原始行归一为同一种消息形状；目前只有 `qq` + `group`（`group_id > 0`，私聊永不分析）。新增平台只需实现同一 `snapshot()` 契约并在 `service.main()` 登记。
- **去标识**：账号号码只留在服务端快照的 participants 名单里。模型只看到别名（`u1`、`u2`……，机器人为 `bot`）与显示名；撤回的消息完全不进快照，只计数。统计结果不传给模型，模型的结论也不改写统计。
- **显示名**：迁移 `019_message_sender_name.sql` 给 `message_logs` 增加 `sender_name`（入库时写入群名片/昵称快照，纯数字回退不算名字）。旧行为 NULL，按「同群更早的快照 → `群友` + 号码末四位」回退，统计的 quality 会标出 `display_name_unknown_for_some_members`。同一迁移加 `(group_id, created_at)` 索引供按天范围读取；两步都在已存在时跳过。
- **工具面**：`chat_timeline / chat_context / chat_search / chat_member / chat_previous / chat_phase / chat_observe / chat_submit` 加官方 `skill`。没有发送、SQL、shell、网络、Memory、QQ 状态或其他主机工具；工具执行 guard 校验 Agent 身份与白名单。
- **证据**：每条结论必须引用本次运行**已读过**的当天消息 ref；金句只由模型给 ref，原文与说话人由服务端从快照摘录；称号只能给非机器人成员，且证据须含本人发言；最终提交前必须按时间顺序读完全天。校验失败的 400/409 原因回传给模型以便修正，5xx 细节不回传。
- **往日线索**：`chat_previous` 只读本群此前最多 7 天已发布的 AI 日报的标题/摘要/话题名/待跟进，作为「后续」线索；证据仍须是当天消息。

## 报告内容（`chat_analysis_runs.result`）

| 字段 | 内容 |
| --- | --- |
| `headline` / `summary` / `summary_refs` | 一句话标题、3~6 句概述及证据 |
| `mood` | 氛围标签、描述、确定性、证据 |
| `topics[]` | 1~10 个话题：标题、起因→发展→结论、参与者别名、确定性、证据 |
| `quotes[]` | 0~8 条金句：`ref`、服务端摘录的 `text`/`speaker`/`at`/`source_id`、模型点评 |
| `titles[]` | 0~10 个今日称号：成员别名、≤16 字称号、理由、证据（正面或中性，每人一个） |
| `relations[]` | 0~8 条互动 |
| `open_items[]` | 0~10 条待跟进：`question` / `plan` / `announcement` / `request` |
| `slang[]` | 0~8 个新梗观察 |
| `names` | 别名 → 显示名（站点渲染用） |

`certainty` 取 `fact` / `inference` / `speculation`。`markdown` 是服务端由结构化结果渲染的纯文本版本，正文不含号码与 ref。当天没有群友发言（只有机器人或全部撤回）时，Python 直接发布 `analysis='empty'` 的报告，不调用模型。

## 状态机、调度与预算

`pending → preparing（Python 快照+统计，租约）→ ready → running（DSH，租约）→ completed | failed`。失败或租约过期不会替换当前展示版本；重试是新 Run、新快照、新 Session。手动切换展示版本带乐观锁，进行中的生成不覆盖刚选定的版本。

- 调度：`chat_analysis_settings.enabled=TRUE` 的群，按其时区在自然日结束 + `CHAT_ANALYSIS_BUFFER_SECONDS`（默认 1800）后建任务；重启补调度最近 `CHAT_ANALYSIS_CATCH_UP_DAYS`（默认 3）天，唯一 schedule key 防重复。`CHAT_ANALYSIS_SCHEDULE=0` 只保留手工任务。默认所有群关闭，由站点开关。
- 预算（默认）：300 步、300 万 token、2700 秒、单次输出 8192 token、单日最多 60000 条消息；超额失败，保留统计快照，不悄悄截断。DSH worker 串行处理，每个 Run 独立官方 Session。
- 记账：全部 turn、重试与 compaction 复用 `NativeAccounting`（task type `chat_daily_analysis`）→ `yunying_ai_records` → `ai_jobs/ai_job_attempts`；`chat_analysis_calls` 关联 Run 与阶段；终态 JSONL 重扫补齐丢失的投影，不重复计费。

## 内部 API（`127.0.0.1:8792`，仅内网/隧道）

`/admin/*` 用 `CHAT_ANALYSIS_ADMIN_SECRET`，`/worker/*` 用 `CHAT_ANALYSIS_DSH_SECRET`（两者必须不同且 ≥32 字符）。admin 请求体必须带 `actor`（`site:` 前缀，可审计）：

| action | 参数 | 说明 |
| --- | --- | --- |
| `settings` | — | 列出各群开关与时区 |
| `configure` | `platform`、`conversation_type`、`external_id`、`enabled`、`timezone?` | 开关某群；开启要求该群有记录 |
| `create` | 同上三元组、`period_start`、`request_key` | 手工生成某天（幂等） |
| `retry` | `run_id`、`request_key` | 重试失败的 Run |
| `select` | `report_id`、`run_id`、`expected_run_id` | 切换展示版本（CAS） |

站点读库直接查 `chat_analysis_reports / chat_analysis_runs`（不读 `chat_analysis_messages`、`lease_token`、`request_key`），费用经 `chat_analysis_calls.task_key` 查 AI 库。**成员可见范围由站点决定**：本人绑定的 QQ 在该群有过发言记录才可查看该群日报；渲染金句时须按 `source_id` 复核 `message_logs.is_recalled`，事后撤回的原话不再展示。

## 启用（合并不自动部署）

1. 使用包含 `chat-analyst-dsh/` 的固定 V4 Python/DSH 镜像；备份后让 V4 迁移入口应用 019/020（`idx_group_created` 在大表上是在线 DDL，需预留时间）。
2. 运维创建 `.env.chat-analysis`（gitignore，示例见 `deploy/docker/.env.chat-analysis.example`），生成两个强且不同的 secret。
3. 通过独立 `chat-analyst` Profile 的 operator patch 配置 DSH provider、模型、认证与真实 contextWindow；不复制 QQ 人格/工具/会话。
4. 在既有受控 Compose 链路显式加入 `deploy/docker/docker-compose.chat-analysis.yml`，预创建持久目录 `chat-analyst-runtime` 并授权给 DSH 用户。
5. 站点配置 `CHAT_ANALYSIS_ADMIN_API_URL` 与 admin secret；DSH secret 不进入站点或浏览器。

本地启动：`python -m igngbot_v4.chat_analysis`、`sh scripts/run-chat-analyst-profile.sh`。

## 验证与风险

离线：`tests/test_chat_analysis.py`（适配器去标识、撤回排除、统计、证据/称号/金句校验、调度键、昵称列写入）与 `yunying-dsh/tests/chat-analyst.test.js`（原生多步、工具隔离、预算、提交被拒后修正、错误回传边界）。`YUNYING_TEST_DB=yunying_v4_test` 开启真库用例（快照水位、覆盖扫描、草稿、空日发布、重试/CAS、往日线索、租约与记账恢复）；再加 `YUNYING_RUN_PROFILE_SMOKE=1` 跑 Python API + 官方 CLI 双进程协议测试。全部使用合成记录与模型 wire fixture。

模型能否读懂群聊语境、称号是否得体、真实费用与耗时，都必须用真实群和付费模型影子验收；程序覆盖扫描与引用校验不是结论正确性的证明。秒级时间戳下同秒顺序按行号；入库中断、撤回和 OneBot 未上报的消息会降低覆盖率。没有部署、生产迁移或付费模型验收时不能宣称这些已完成。
