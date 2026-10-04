# qq-bridge reserved2 来源与许可

来源：[Derpyu520/qq-bridge](https://github.com/Derpyu520/qq-bridge/tree/9df6a7e7fc5abcb36793337f778483bd442a3d2d)，固定 commit `9df6a7e7fc5abcb36793337f778483bd442a3d2d`。保留原仓库完整 `LICENSE`；下列 qq-bridge 代码适用其 MIT 许可。没有引入其 SnowLuma 非商业资源包、客户端二进制或第三方素材。

| 文件 | 来源及处理 |
| --- | --- |
| `agent.cordis.yml` | 原 `dsh/agent-presets/qq-chat-v2/agent.cordis.yml`，逐字复制。`config.prefix` 按原 YAML `>-` 语义读取；没有精简、改写、重排或替换 Social Prompt。原工作目录 suffix 不用于 QQ Agent；原 `{{model}}` 模板保留，由官方 DSH 正常插值。 |
| `v2-wait.js` | 原 `src/v2-wait.js`，逐字复制；保留未说完消息的判断。 |
| `qq-model-view.js` | 原 `src/qq-model-view.js`，逐字复制；保留去空值、相同 text/plain 合并、媒体引用语义。 |
| `safe-fetch.js` | 原 `src/safe-fetch.js`，逐字复制；保留 DNS/IP 校验、地址绑定、重定向逐跳校验、体积/时间限制。 |
| `wake-prompts.js` | `src/bridge.js` 的 `buildWakePromptV2`、`buildWakeReminderPromptV2` 函数体逐字提取；外围只增加绑定 adapter。 |
| `web-functions.js` | `src/mcp-web-search-safe.js` 的 `sanitizeQuery`、`decodeHtml`、`bingSearch` 函数体逐字提取；去掉外部 MCP server。 |
| `tool-descriptions.json` | `src/mcp-snowluma-safe.js` 中已移植工具的描述原文。DSH schema 使用其支持的 JSON schema 子集，范围约束仍由执行层验证。 |
| `hashes.json` | 复制/提取结果的 SHA-256。自动化测试比较全部固定文件的字节 hash 及 YAML 实际解释出的原 Prompt。 |

`src/social.js` 和 `src/runtime.js` 适配 reserved2 的 unread/watermark、quiet wait、完整沉睡观察、触发器、有限/无限潜水、主动机会、回复检查、无行动计数、缺少收尾提醒。DSH 已负责模型 loop、工具调度、Session、持久 Inbox 和 compaction；没有复制 donor 的外部 DSH Web API bridge、旧版本兼容、文本输出转发、独立 context summary 或整套控制台。

必要差异：YunYing 名字、Memory Skill 和权限说明放在单独 Profile section；所有群/私聊都有持久映射及 SQL 事件队列；直接 @/回复/私聊跳过普通唤醒限频；连续已查看水位不能跨过未查看消息；未读不以 donor 的易失窗口截断；发送工具按会话及调用 ID 去重；不确定结果不自动重发；跨会话工具/高权限工具由执行层拒绝。高级表情收藏、声线合成和默认形象的 donor 工具第一阶段关闭，已收到的图片/表情/语音、OCR/ASR/转发/历史仍复用 V3。

默认 `bing` 搜索保留 donor 行为。可由部署者设置 `YUNYING_SEARCH_PROVIDER=deepseek`，通过上游 `ctx.web.search` 的认证 provider 搜索；只开放固定查询能力，DSH 通用 web fetch/tool 不开放。该选项用于代理/Fake-IP 环境，不放宽 donor URL 抓取的 SSRF 校验。
