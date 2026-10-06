# 会话投影：从 MySQL 还原输入输出与工具调用流程

`yunying_session_events` 把官方 DSH Session 日志投影进 MySQL，用于查询与还原"模型看到了什么、回复了什么、调用了哪些工具"。它**不是** durability：官方 JSONL/附件仍是权威与恢复源，投影表可从官方日志完整重建。

## 设计边界

- **权威层不变**：`DSH_HOME` 下的官方 JSONL 与附件继续负责恢复、原生 Inbox 取消/重入、compaction。启动、`flush`、`stat/open/read` 都仍走官方接口，不自定义 durability。
- **投影可丢弃可重建**：`UNIQUE(dsh_session_id,event_seq)` + `INSERT IGNORE`。实时采集失败不影响 Agent；重启时按官方日志回填高于 `MAX(event_seq)` 的部分；`/admin/session/rebuild` 可整会话重放。
- **只留有还原价值的内容**：保留输入输出与工具调用骨架，按设计丢弃可再生的杂碎。

## 事件取舍

| 事件 | 处置 | 说明 |
| --- | --- | --- |
| `turn/start` `turn/end` `step/start` `step/end` | 信封 | turn/step 与结束原因，极小 |
| `system/message` | 全文（上限 64 KiB） | 系统提示词 |
| `developer/message` `user/message` | 全文（上限 64 KiB） | 模型输入；图片/文件只留 attachment 引用 |
| `assistant/message` | 文本 + reasoning + usage；**丢弃原始 `stream`** | 模型输出 |
| `assistant/attempt` | 信封 | 失败尝试，丢弃原始流 |
| `tool/call` | 工具名 + 原始 arguments（上限 16 KiB） | 工具调用流程 |
| `tool/result` | **只留占位**：callId、isError、原始字节数 | 正文按设计丢弃，官方日志保留 |
| `request/header` `request/context` | 只留 provider/model/reason/window | 丢弃工具 schema |
| `compaction/start` `compaction/end` | 信封 | 边界 |
| `compaction/summary` | 全文（上限 64 KiB） | 摘要正文 |
| `session/end-seed` | 信封 | fork 标记 |
| `agent/inbox/spliced` | 不存 | 与 user/message 重复且体积大 |

超长正文会截断并置 `truncated=1`；`content_sha256` 供去重核对。

## 环境开关

`YUNYING_SESSION_TOOL_RESULTS=none|preview|full`，默认 `none`：

- `none`（默认）：`tool/result` 只记占位（callId/isError/字节数）。
- `preview`：另存前 2048 字符预览。
- `full`：另存完整结果（上限 64 KiB）。

三个值都仍丢弃助手原始流、request 工具 schema 与 Inbox splice。

## 写入路径

1. **实时**：`SocialRuntime` 订阅官方 `session/event`，`projectEvent()` 生成行，进入每个会话的内存队列，串行批量 `INSERT IGNORE`。DB 故障时行留在队列，下一次事件或周期 flush（5s）重试。
2. **恢复回填**：`load()` 读取官方日志后，对 `event.seq > MAX(event_seq)` 的事件投影，幂等补齐崩溃/宕机期间的空洞。
3. **重建**：Owner 路由 `/admin/session/rebuild` 调用 `SocialRuntime.rebuildTranscript()`，只读官方日志重放。

## 读取接口（Owner secret）

| 路由 | 作用 |
| --- | --- |
| `POST /admin/session/events` | `{sessionId, after?, limit?}`，返回按 seq 排序的原始投影行 |
| `POST /admin/session/transcript` | 同上，但经 `reconstruct()` 返回有序、带 role 与结构化 `data` 的流程 |
| `POST /admin/session/rebuild` | `{sessionId}`，从官方日志重建该会话投影，返回插入行数 |

`sessionId` 即 `yunying_sessions.dsh_session_id`。Owner secret 与 infrastructure secret 分离，且默认不暴露 host port（与既有 Memory 管理 API 同边界）。

## 验证

- 单元：`yunying-dsh/tests/transcript.test.js` 覆盖逐事件取舍、`toolResultMode`、实时投影顺序、重启回填去重。
- 真实库：`yunying-dsh/tests/mysql-native.test.js`（`YUNYING_TEST_DB=yunying_v4_test`）断言重启后 `user/message`/`assistant/message`/`turn/end` 已投影且 `tool/result.content` 全为 NULL。
- 迁移：`migrations/v4/008_session_transcript.sql`，由 `igngbot_v4.migrate` 按 checksum 记录；只新增表，不改既有结构。
