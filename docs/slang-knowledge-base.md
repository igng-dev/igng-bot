# 梗 / 网络用语知识库

## 交付范围与验收边界

实现顺序为 P0 数据/种子/评测 → P1 自动聊天辅助 → P2 独立维护插件 → P3 ASR/视觉/语义 → P4 scoped 聚合反馈、Owner 页面与盲评工具。

这是能力实现，不是生产验收：**未部署 NAS，未配置真实创作者订阅，未调用付费 ASR/视觉/embedding 服务，未完成真人盲评。** `seed.py` 的60条是明确标记的编辑候选，参考链接仅作为研究起点，没有虚构引用、审核状态或2026年热度。它们不会自动进入已发布索引。200条合成用例只检查工程识别行为，不能证明真实模型会自然玩梗。

## 数据与权限

- MySQL additive migrations `015_slang.sql`、`016_slang_semantic.sql`、`017_slang_auxiliary.sql`；原迁移 checksum 不变。
- `slang_entries` 保存当前条目 JSON（含 aliases/sources），`slang_versions` 保存不可变版本和审核理由。别名索引目前在聊天进程中，不引入向量数据库。
- `candidate/published/disputed/disabled` 是知识管理状态；`unknown/rising/active/fading/nostalgic` 是流行生命周期；`common/occasional/strong/understand_only` 是使用建议。三者独立。
- 条目一个义项；同名多义使用不同稳定 ID。别名可带 `requires_any/excludes_any`。短于3字符的别名没有消歧条件时不自动命中。
- `sources` 包含 URL、原文、origin、时间位置、材料类型与 verified。`verified` 表示已取得或经 Owner 核对的证据片段，**不表示整条解释一定正确**。
- 字幕与视频时间位置由服务端快照固定。ASR/视觉描述是派生解释，默认未验证。发布至少需要一条实际取得或经 Owner 核对的非空证据；释义审核仍由 Owner 负责。
- 聊天 Agent 只有 `slang_search` 读取能力，没有候选发布、订阅修改或全局知识写入工具。旧 donor `qq_slang_*` 工具仍未开放。
- Curator 只有 `slang_read_material/slang_lookup/slang_submit`，无 QQ、Memory、shell、文件系统、插件管理或任意 URL 工具。
- Owner、worker、可选 reader 使用不同的强凭据。reader 不能管理条目，worker 不能发布条目或取得 Owner 权限。

## 聊天链路

`runtime.js` 在允许投递的来信、唤醒快照，以及已授权的未读/历史/详情/等待工具结果中记录当前语境。最多20条、每条600字符、总采样6000字符；忽略自身发言、系统事件、控制消息、撤回和凭据形态。

在官方 `system-prompt/assemble` 阶段更新 `yunying:slang` section，而不是在来信或每个工具步骤追加一条新的历史消息。保留原生 Session/Inbox/Compaction、donor persona、聊天模式、权限和已读水位。OFF 群普通消息不触发增强或新轮；明确呼叫仍只使用当轮允许的上下文。

识别优先别名/变体、上下文条件和完整短语，Latin/hybrid 别名有词边界。已发布、带证据的条目才可进入辅助。多义或语义回退标记不确定；不知道时正常澄清或按需搜索，不进行自动未知词公网研究。

可选推荐要求语境/话题适合、解释可信、无风险、使用强度合适，并满足以下之一：

1. 30天内的公开使用观察且生命周期 active/rising。
2. Owner 明确授权的本会话聚合活动：至少3条独立消息 ID、7天内活动；**不代表全网热度或独立用户人数**。
3. Owner 明确设置本会话 `prefer`；**不代表该梗仍新潮**。

安慰、争执和技术说明不主动推荐。`avoid` 仅禁用主动推荐，不阻止理解。bot 自己使用的表达不计入群活动。

### 预算与降级

| 配置 | 默认 | 含义 |
|---|---:|---|
| `YUNYING_SLANG_ENABLED` | 1 | 识别/语境模块；0关闭 |
| `YUNYING_SLANG_RECOMMEND` | 1 | 主动表达参考；0只理解 |
| `YUNYING_SLANG_BUDGET` | 900 | **UTF-8字节上限**，不是精确 token 数；保守限制输入体积 |
| `YUNYING_SLANG_TIMEOUT_MS` | 150 | DB辅助等待上限，50–1000ms |
| `YUNYING_SLANG_SEMANTIC` | 0 | 可选语义回退；默认无额外模型调用 |
| `YUNYING_SLANG_SEMANTIC_TIMEOUT_MS` | 800 | 可选 reader 请求等待上限 |

最多3条理解、2条推荐；先保留理解，预算不足丢弃整条卡片。不可信边界常驻于非空参考块；没有相关条目不注入。相同语境缓存5秒，同一 DB 获取最多一个在途请求。数据库/索引/reader故障只跳过增强，不阻塞原聊天。发布索引上限5000条，超限明确失败降级，不能默默截断。

未来扩大规模前需做别名索引和检索性能基准；当前同步排序耗时不受 DB timeout 限制。动态 block 在官方请求快照中可能留下历史版本，因此明确注明版本及历史参考不代表当前流行，不自行修改旧 Session 或压缩摘要。

## 独立维护插件

Bundle `slang-curator-dsh/`、入口 `@igng/yunying-dsh/slang-curator`、Python API `python -m igngbot_v4.slang`。

1. Owner 配置来源，维护开关默认关闭。
2. 定时读取最多20个 feed 条目；仅有标题/简介不能生成知识。
3. 优先真实字幕；明确开启的媒体来源才下载/分析视频。
4. 冻结材料、创建幂等任务；worker 领取任务并持有 SQL 租约。
5. 每次尝试使用独立原生 DSH Session，分页读完全部证据后才能提交。
6. 服务端验证所有 evidence IDs，将实际快照片段绑定到候选；模型不能自填 URL/verified/虚构引用。
7. 候选与发布表分离，维护过程不能覆盖已发布解释；Owner 审核用 CAS、事务和版本历史。
8. 原生记录写 `slang_calls` 与既有 `yunying_ai_records` outbox，通过既有 AI job/attempt 适配器记账；未知使用/费用保持未知，不能填0。

### 订阅与平台

支持允许访问的 JSON/RSS feed、Bilibili 创作者 RSS feed + 公共视频信息/字幕。Bilibili 必须核对 owner MID；无公共字幕、登录要求、签名限制时拒绝，不绕过认证、反爬或 DRM。

订阅示例（Owner API `source-save`，MID和feed需自行选定）：

```json
{
  "source": {
    "id": "bilibili:selected-creator",
    "kind": "bilibili",
    "creator_id": "123456",
    "feed_url": "https://example.com/authorized-creator-feed.xml",
    "allowed_hosts": ["example.com", "api.bilibili.com", "aisubtitle.hdslb.com"],
    "enabled": false,
    "media_enabled": false,
    "poll_seconds": 21600
  }
}
```

这是占位配置，不是真实订阅。字幕主机必须按实际允许的提供方配置，不允许通配符。抓取 DNS 逐次解析并固定到公网地址、拒绝私网/reserved/Fake-IP、凭据 URL、重定向、过大响应与 XML entity。开发网络 Fake-IP 会被正确拒绝，不能把 fixture 通过当作真实 Bilibili 可达。

JSON feed 的 `items` 可提供 `url/title/published_at/caption_url/caption_format`（JSON、SRT、VTT）或有明确授权的 `video_url/visual_required`。RSS当前支持 RSS item/link/pubDate；不保证任意 Atom、非标准 RSS 字幕扩展或平台视频流格式。

已创建任务的 source+URL不重复做昂贵预处理；修改过的旧视频需 Owner 导入新的材料快照。source origin 由平台类型与创作者 ID 固定，两个订阅别名不算两个原创来源。每个来源使用持久租约，整轮最多600秒，进程崩溃后租约自动到期。

### 发布与热度

首期 `auto_publish=false`。可显式开启低风险自动发布：至少两个不同原创 origin 的不同已取得原文、多个候选的释义/语气/别名/使用策略精确一致、高置信且风险并集为空；同ID有冲突候选时留待审核。只发布新 ID，且强制 understand_only，绝不自动覆盖/重启已有或停用条目。它仍不能代替专家审核，风险条目应留人工处理。

科普发布日不写成 observed_at。维护提交时服务端强制 lifecycle=unknown、observed_at/verified_at为空；Owner 核对实际使用观察后才修订这些字段。旧梗保留解释，不因退潮删除；公开观察的推荐权重按时间衰减。群统计只改善本会话参考，不提升全局真实性或全网热度。

## ASR、视觉与 embeddings

可选 OpenAI-compatible 服务由 `.env.slang` 配置，绝不由消息/视频文本决定 endpoint。默认不下载任何本地模型。

- 视频最大50MiB/10分钟；更长内容需分段字幕导入。音频按120秒分段；最多8帧，每帧≤1MiB。
- ffprobe/ffmpeg 使用固定命令、无 shell、限定 file/pipe 协议及媒体 demuxer，拒绝任意网页/播放列表访问。
- 临时音视频落设备缓存 `SLANG_MEDIA_CACHE`，不落源码 worktree。无 ASR 或必要视觉服务时转待处理，不编造字幕。
- 视觉描述明确标为模型推断，ASR/视觉证据默认未验证，需 Owner 核对后发布。
- 本地 TF-IDF 回退是词面相关性增强，不声称理解任意隐含引用。
- 真正 embedding 回退额外要求：配置服务/model、独立 `SLANG_READ_SECRET`、社交侧启用 semantic、Owner `semantic-consent` 授权当前 scope。不保存私有查询文本或 query embedding，缓存只存公开 corpus 向量。
- Embedding corpus暂限100条；单批32个输入、daily inputs 默认200；忙时拒绝排队，reader服务端2秒 deadline。超限/失败退回原聊天，扩容必须另做成本与性能评估。
- ASR/视觉/embedding 都在 provider 请求前做持久输入单位预算预留，`SLANG_AUXILIARY_DAILY_INPUTS` 默认200；失败也占预算。`slang_auxiliary_calls` 保留完成状态、供应商报告的 token 数（否则NULL），费用NULL；实际费用需供应商账单核验。

## 任务预算与恢复

默认每日10份新材料、每次24步骤/30000已报告 tokens/600秒、每日150000维护 tokens（按 UTC run-start 日）。只运行一个维护任务；未知 usage 按该 run 的全额 token 预算保守预留，重试不会抹掉旧 Session 或旧账。

Token 限额在供应商报告使用量后停止下一步骤，**不是精确的美元预扣或能撤销已经发生的单次请求**。已发生请求可能超出阈值，成本必须结合实际 provider 用量和价格观察。

租约过期只重排尚未提交的任务，最多3次。已有合法候选不重复提交。原生 JSONL 是用量恢复真源：重启后先 replay/reconcile，再领取新任务。没有原生 Session 时失败关闭，不能把丢失用量当成0。

Owner 可在确认并非 `DSH_HOME` 配置错误、Session 确实不可取得之后调用 `resolve-unavailable-session`，提供 `session/reason/acknowledge_unknown_usage=true`：记录未知用量及一个 run-sized 保守预算，再解除恢复阻塞；不伪造账单。此操作应逐条调查，不批量处理。

## Owner 操作

管理页 `http://127.0.0.1:8792/manage`，只从 localhost 或认证隧道访问。静态页面不含数据/凭据，所有数据动作必须带 Owner Bearer；token只在当前页面内存，CSP禁第三方资源与嵌入，不使用 innerHTML 渲染外部材料。生产访问应使用 HTTPS/可信隧道。

页面支持条目/CAS编辑、候选审核、版本历史与回滚、来源、手动任务、开关、匿名反馈、独立语义授权及导出。默认只显示有限页，条目可继续翻页；完整导出按游标读取。CLI 处理批量种子和 JSON 导入。

```bash
# 凭据通过密钥管理器/忽略的环境文件配置，不作为 CLI 参数或提交内容。
python -m igngbot_v4.slang.seed                    # 输出60条未审核候选，不写数据库
python -m igngbot_v4.slang.admin seed              # Owner 显式导入 candidate，绝不启动时 seed
python -m igngbot_v4.slang.admin export > /approved/private/path/slang-export.json
python -m igngbot_v4.slang.admin source-save --file /approved/private/path/source.json
python -m igngbot_v4.slang.admin policy --file /approved/private/path/policy.json
```

`import --file` 只创建 candidate、expectedVersion=0；遇到已有ID冲突不会覆盖，可能已有部分候选导入，需查看结果后继续，不能反复强写。Owner API能力完整定义见 `service.py`。多条批量导入不是一个跨请求事务。

本群反馈（Owner，而非群友/模型直接授权）：

```json
{"scope":"group:1001","enabled":true}
```

用于 `/admin/consent`（仅匿名计数）或 `/admin/semantic-consent`（允许当前消息片段去配置的 embedding 服务），两种权限独立。默认都关闭。计数只保留 scope、公开梗ID、次数、hash receipt；不保存发言原文、用户身份，不跨群返回，不把 bot 的复读计为流行证据。撤回会从当前辅助上下文移除；历史匿名“曾出现”统计不反向抹除，不作为事实证据。

## 运行、停用与回滚

复制空模板 `.env.slang.example` 到忽略的配置位置，通过安全渠道设置三个分离凭据。`deploy/docker/docker-compose.slang.yml` 是显式 opt-in overlay，使用固定 reviewed bot/DSH 镜像，独立 DSH_HOME，API仅发布loopback。**合并PR不会启用 overlay、导入种子、开启订阅或调用模型。**

无需整个功能才能关闭某一部分：Owner `policy` 的 recognition/recommendation/maintenance/auto_publish 独立开关；聊天进程另有环境总开关。回滚时关闭增强/worker、退回原固定镜像；保留新增表和原生Session供恢复，不能直接删库或清空 DSH_HOME。API启动走既有 checksum migration 管道，不手动推生产 schema。

可选外部语义检索的 `SLANG_READ_SECRET/SLANG_API_URL/YUNYING_SLANG_SEMANTIC` 还须显式加入原 `yunying` 服务的忽略 `.env.v4` 配置；Compose 内的 reader URL 应为 `http://slang-api:8792`。不要把 Owner/worker 凭据提供给社交进程。仅启用新增 overlay 不会自动扩大社交进程的权限。

## 测试与效果评测

```bash
python -m pytest tests -q
npm --prefix yunying-dsh test
npm --prefix yunying-dsh run check
bash -n deploy/deploy-nas.sh deploy/deploy-v4-nas.sh
sh -n scripts/run-yunying-profile.sh scripts/run-slang-curator-profile.sh
bash scripts/privacy-scan.sh
git diff --check
python -m igngbot_v4.slang.seed --evaluation | node scripts/slang-evaluate.mjs
```

真实数据库与双进程必须按 `docs/testing.md` 使用一次性 loopback `yunying_v4_test`，串行跑 Python/Node，不能连接生产。新 process fixture 启动真实官方CLI和独立Python API，校验材料读取→候选→原生用量→重启恢复；模型HTTP和素材仍是脚本替身，不向真实 QQ 发言。

`scripts/slang-blind-review.py` 为已经授权采集、预生成的 baseline/recognition/full 回复创建匿名A/B/C评分包和独立答案表。它不调用模型、不收集真实聊天，评分默认空。人工评估理解、自然度、强行玩梗、过时表达，不能用200个识别 fixture 的准确率冒充真人盲评结果。

真实创作者字幕获取质量、ASR/视觉语义准确度、流行度判断、真实 QQ 风格、长期费用及索引扩容性能仍需后续验收。
