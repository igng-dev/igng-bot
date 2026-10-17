# 作品审核 DSH Profile

站点契约真源：`igng-sites/docs/work-moderation.md`（协议 v2：按范围决定）。本文件只记录 Bot 接入边界，不复制发布业务规则。

## 实现边界

- 独立 opt-in `moderation-dsh` Profile，不加载云萤 QQ 身份、社交记忆、史官或梗库工具。
- 只访问站点内部审核 API，无业务库写凭据；作品读取和结论都绑定 task/run/lease。
- 每作品一个官方持久 Session，任务之间复用；运行任务串行，空闲卸载。
- 固定投递消息 ID，使用原生 Inbox/JSONL/flush 恢复，不自建模型循环，不改写会话日志。
- 已登记 Session 丢失时失败关闭，需恢复持久卷，不静默创建空会话。
- 作品内容、媒体解释和搜索结果均不可信；工具侧校验精确白名单和当前 Agent 身份。
- 搜索按需，不搜索未公开全文/个人资料；不暴露任意 URL fetch、Shell、SQL、文件编辑或子 Agent。
- 作品当前快照/完整覆盖状态是判定依据，不能用历史 Session 结论替代检查。
- 原生模型调用与 compaction 单独记账，未知 usage 不当作零；时间/步骤/token/搜索预算受控。
- 默认关闭，部署与付费模型影子验收必须显式进行，合并不自动运行或发布。

## 跨仓库交付顺序

1. 固化站点契约及错误码。
2. 站点内部 API/隔离数据库可用后实现 Bot Profile。
3. 原生 DSH fixture 验证连续 Session、隔离及恢复。
4. 双进程/临时数据库验证租约与结果幂等。
5. 管理员评审真实模型影子结果后才允许正式消费 active 任务。

## 已知基线风险

2026-10-16 本地 scratch `npm audit`：6 high / 0 critical，涉及锁定 DSH 的 MCP SDK 依赖链及
`http-cache-semantics`。本任务不盲目执行 `npm audit fix` 或升级整个 DSH；独立审核 Profile 必须禁用
MCP/ACP 及通用网络能力。禁用入口降低暴露面，不代表依赖漏洞已修复。

## 阶段 4 实现与验收

- Bundle：`moderation-dsh/`，官方 Profile 名 `work-moderation`，启动入口 `sh scripts/run-moderation-profile.sh`。
- 使用官方 `llm-pi-ai` 的独立 `work-moderation` 路由；`WORK_MODERATION_MODEL_BASE_URL` / `WORK_MODERATION_MODEL_API_KEY` 与站点内部令牌分离，绝不继承 QQ、SQL 或社交 Profile 凭据。
- 参考 `deploy/docker/.env.moderation.example`；worker 默认关闭，容器 overlay 还须显式选择 `work-moderation` profile。这是交付定义，不是部署授权。
- 只提供 `work_manifest/read/search/submit/defer` 与审核 skill。搜索目前只允许三种公开政策主题，实际查询由代码固定，防止把私有正文外传搜索引擎；并非任意 URL 阅读工具。
- 同作品串行、不同作品最多四路并行，空闲卸载 Agent；每次执行一条固定原生输入，任务重试使用确定性的 run 输入，不创建新 Session。
- 新版重新读取；原生 compaction 不提供覆盖信用。已建立 Session 丢失则 `SESSION_MISSING`，不创建空白替身。
- 每次原生 attempt 在工具执行前提交最小用量记录，不传提示词或思维链。站点按 task+native sequence 去重，跨重试累计预算；恢复时从官方 JSONL 补齐可能丢失的记账。未知用量、记账故障或超预算转人工/重试，不自动通过或拒绝。
- `yunying-dsh/tests/work-moderation.test.js` 覆盖原生工具隔离、重复投递、Session 丢失、受控搜索、预算、双 Node 进程恢复及强制 compaction 后的新版本读取。
- `YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests/test_moderation_profile.py -q`：实际官方 CLI/Profile 两次进程启动、固定 Session、HTTP capability 与 Anthropic 线协议夹具。本地通过，未连接真实 QQ/数据库/付费模型。
- `python -m pytest tests -q` 本地 161 passed / 35 skipped；Node 全套重跑通过（首次已有 progress 计时用例失败，未改社交逻辑）。真实 MySQL/社交进程项目在本机跳过，仍以 CI 为准。

## 阶段 6：按范围决定（works-v2）

- `work_submit` 的 `result` 支持 `decisions` 数组：每个必检 scope（`WORK` 或 `NODE:<id>`）一条独立结论；单一必检范围仍可用顶层 `verdict`。站点按范围独立校验覆盖与域内证据，旧格式保持兼容。
- 提示词要求逐范围表态；`required:false` 的未变更章节只是上下文，沿用既有决定，不要求结论。
- `yunying-dsh/tests/work-moderation.test.js` 新增多域透传用例（`scoped decisions pass through unchanged for chapter-level review`）。
- 未连接真实付费模型；真实影子验收仍待显式执行。

## 尚未完成

统一管理中心、影子/灰度/回滚验收仍属后续阶段；当前不要启用 active 消费。
阶段5增加 `work_media`：只接收清单单元ID，由站点独立视觉模型完整检查固定hash原图，返回观察与回执。不接受任意URL、OCR或图片标签冒充视觉覆盖；不可用则立即defer，不能继续提交内容结论。图片证据需本轮receiptId及observation原文。
目前13项审核原生测试通过，包含跨作品并发隔离、记账故障关闭、覆盖校验可修正与预算错误立即终止；Node整轮99 passed / 5 skipped。实际官方CLI双进程冒烟再次通过，Python仍为161 passed / 35 skipped。既有社交progress计时用例两次偶发失败，未改社交逻辑；单文件7项及整轮重跑通过，保留为基线风险。
真实视觉/付费模型未验收。阶段4版本CI：[run 38892288158](https://github.com/igng-dev/igng-bot/actions/runs/38892288158)，`verify`通过（2m17s）；后续视觉/并发版本 [run 38896085018](https://github.com/igng-dev/igng-bot/actions/runs/38896085018) 的 `verify`通过（2m14s），不覆盖最后的错误码处理补丁。
跨仓本地验收由站点 `tests/work-moderation-bot-contract.test.mjs` 提供：显式传入本Bot的scratch根路径后，使用真实HTTP处理器/站点覆盖与预算服务/官方DSH工具，模型与数据库为隔离夹具；不加载.env或生产密钥，不把该结果伪称为真实MySQL或付费模型验收。
