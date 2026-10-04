# 自动化验证与验收边界

测试入口先于 V4 实现单独登记。Python 使用 `pytest.ini` + `requirements-dev.txt`；DSH 使用 Node 原生 test runner，不替换上游 Agent Loop。

## 默认离线回归

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
cd yunying-dsh
task_node_cache="${XDG_CACHE_HOME:-$HOME/.cache}/igngbot-v4/node-runtime"
mkdir -p "$task_node_cache"
cp package.json package-lock.json "$task_node_cache/"
(cd "$task_node_cache" && npm ci --ignore-scripts)
test -e node_modules || ln -s "$task_node_cache/node_modules" node_modules
npm test
npm run check
```

无显式 opt-in 时跳过真实数据库与双进程测试。测试不读取部署凭据、不向真实 QQ/付费模型发消息。DSH 测试实际使用固定官方 core、Tool Runtime、Skill、JSONL Persistence、attachment admission、compaction；只有 LLM/外部 QQ 服务使用脚本替身。原 Prompt 同时核对固定字节和上游模板插值后的实际模型请求，CLI 的模型请求也验证精确 Social 工具目录边界。

## 可复现的临时数据库集成

只允许 `YUNYING_TEST_DB=yunying_v4_test`，固定 loopback `127.0.0.1`，默认端口33316，可用 `YUNYING_TEST_DB_PORT` 改测试端口。集成测试以该独立服务的本地 root/空密码连接；**只能使用一次性、只绑定 loopback 的开发数据库，不可指向 NAS/生产**。测试保留合成记录，便于核查；重置数据库需要测试服务操作者自行处理，测试不会清空任意数据库。

在该独立服务创建 `yunying_v4_test`（utf8mb4），先初始化复用的消息表及新增迁移：

```python
# 独立服务已存在、仅 loopback，端口按测试设置；没有使用 Config 的生产 DB 参数。
from types import SimpleNamespace
from igngbot_v3.db import DBHandler
from igngbot_v4.migrate import migrate
import pymysql
from pymysql.cursors import DictCursor
cfg = SimpleNamespace(DB_HOST='127.0.0.1', DB_PORT=33316,
                      DB_USER='root', DB_PASSWORD='', DB_NAME='yunying_v4_test',
                      PROMPT_DIR='/tmp/no-v3-prompt-seed')
db = DBHandler(cfg)
db.init_message_tables()
db.conn.close()
with pymysql.connect(host='127.0.0.1', port=33316, user='root',
                     database='yunying_v4_test', charset='utf8mb4',
                     autocommit=True, cursorclass=DictCursor,
                     init_command="SET time_zone = '+00:00'") as conn:
    migrate(conn)
```


```bash
YUNYING_TEST_DB=yunying_v4_test YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q
(cd yunying-dsh && YUNYING_TEST_DB=yunying_v4_test npm test)
```

两条命令串行运行，因为数据库独占租约主动阻止两个 Profile 同时拥有同一 DB。双进程测试需 Node >=24、pnpm 和已安装官方 `dsh`；可选 `YUNYING_TEST_DSH_BIN` 指定其实际 CLI 路径。

## CI

已有 `.github/workflows/ci.yml` 已扩展：Python3.12/Node24、官方 CLI、一次性 loopback MySQL8.4、schema 初始化、两个完整测试套件（含双进程故障恢复）、语法和凭据扫描。CI 不使用生产配置，不发布或部署。实际远端 run 结果另在 PR 记录。

## 2026-10-04 初始实现的本地结果（NAS切换前）

环境：CPython3.12.13，Node24.18.0，官方 DSH0.2.1-alpha.1，独立 loopback MariaDB11.8.6（MySQL 协议/InnoDB，33316），独立 Compose CLI2.40.3。没有使用生产 MySQL、真实 QQ 或模型凭据。

| 实际命令 | 结果 |
| --- | --- |
| `python -m pip install -r requirements-dev.txt` | requirements 含全部运行依赖，安装检查通过，venv 在共享缓存。 |
| `YUNYING_TEST_DB=yunying_v4_test YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q` | **96 passed**，1条幂等建表 warning（call_logs 已存在）；无 skip。 |
| `YUNYING_TEST_DB=yunying_v4_test npm test` | **22 passed**，无 skip；含真实 SQL + 官方 DSH。 |
| `npm run check` | 通过。 |
| `bash -n deploy/deploy-nas.sh deploy/deploy-v4-nas.sh`、`sh -n scripts/run-yunying-profile.sh` | 通过。 |
| Compose `config --no-env-resolution` 合并 base + v4 overlay | 通过；使用临时副本、空配置示例及合成 VNC/固定 image 值，没有启动容器。 |
| `bash scripts/privacy-scan.sh`、`git diff --check` | 通过；暂存后的凭据扫描在 PR 集成前再次执行。 |
| `npm audit --json` | **未通过安全审计：9 high / 0 critical**，来自上游 http-cache-semantics 链；详见架构文档。 |

| 场景 | 自动化证据 |
| --- | --- |
| 群聊沉默与 @/回复 | 原生 loop 的普通文本不触发发送；@ 工具回复；实际官方 CLI 双进程 fixture 的 @/reply-to 事件与 OneBot reply segment。自然风格取决于真实模型，尚未验收。 |
| 连续多人来信 | 原生21条并发事件恰好入 Inbox 一次、max model concurrency=1；双进程 OneBot fixture 连续12条事件落库且无并发调用/重复发送。 |
| 主动读取、wait/wake/潜水 | 未读 source ID 与连续 watermark、分页/新到达不能越水位、10秒静默与300秒观察分离、取消、有限时间唤醒、无限潜水安全。测试缩短计时，不等待生产分钟级时长。 |
| 搜索后自然回复 | 原生工具结果→模型→QQ 的协议链；双进程经官方 DeepSeek search provider 的 Messages wire fixture。真实公网结果和模型风格尚未验收。 |
| Person Memory 跨群 CRUD / 私有隔离 | 真实 SQL 的本人共享授权、来源校验、跨群检索/更新、CAS、撤销授权、忘记、Owner rollback、审计；别群私有搜索和直接 read 均拒绝。 |
| 撤回、图片、语音、旧历史 | V3 媒体/语音/转发/撤回回归；V4 当前 DB 行、撤回 tombstone、路径隔离、OCR/转写 view；原生图片 tool admission、模型 image content 和重启后的持久引用。没有重跑真实 OCR/ASR 模型下载。 |
| Compaction 后继续 | 实际官方 `compactNow` 创建 durable summary，下一 QQ 事件继续；JSONL 恢复后仍保持摘要和同一 Session。生产自动压缩组件保留，没有自建 summary。 |
| 重启恢复 | 真实 SQL journal/FIFO/backoff、identity/Memory、native JSONL Inbox 取消/重入；实际断开 Python+官方CLI 两个测试进程的 DB 租约连接，二者退出再启动，同一 Session UUID、Memory read 与 QQ reply 恢复。 |
| 发送/管理员/权限 | CQ 为纯文本、当前会话引用/@ 校验、同一 call 去重、unknown 不重发、普通成员不能暂停/提升权限、高权限 host tool 隐藏且执行 guard 拒绝、内部错误过滤。 |
| AI 记录/网站镜像 | 真实 SQL 的 call_logs 与站点 schema-shaped ai_jobs/attempts，失败重试无重复，保留 provider 与 cache token 语义。未向真实网站数据库写入。 |

真实 Bing 调用尝试被 donor SSRF 校验正确拒绝：开发网络 DNS 返回198.18.0.100 Fake-IP（系统 DNS 与显式公网 resolver 均如此）。没有放宽私网/metadata 防护。明确选择 `YUNYING_SEARCH_PROVIDER=deepseek` 可避开该 DNS 模式，但真实认证搜索仍需部署验收。

未执行：Docker 镜像实际构建、NAS 切换、生产迁移、真实群/私聊、付费模型、公网搜索成功、真实图片/语音模型、长期在线压测。不能把118项程序/协议测试描述为这些环境已验收。

## NAS部署修订

暂停权限、搜索回退与媒体配置修订后的完整验证为Python102 passed / Node25 passed，无skip；远端CI含MySQL8.4也通过。真实NAS构建、生产迁移、官方Session重启恢复、现役模型工具续接、网络搜索及签名图片证据和未验收项目见 [2026-10-04部署记录](v4-nas-deployment-20261004.md)。该记录更新前一阶段的“未执行”清单；不会把协议fixture升格为真实对话风格或OCR/ASR验收。部署时inclusive npm audit为1 high / 0 critical，仍未通过全量依赖安全审计。


## 机械记录、聊天模式与原生任务记账调整

本轮在独立 loopback MariaDB11.8.6、官方 DSH0.2.1-alpha.1 CLI、合成 OneBot/Messages 服务上验证。未连接生产数据库、真实群或付费模型；未部署 NAS/网站。

| 命令 | 实际结果 |
| --- | --- |
| `python -m pip install -r requirements-dev.txt` | 共享 venv 的依赖检查通过。 |
| `YUNYING_TEST_DB=yunying_v4_test YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q` | **107 passed**，无 skip；幂等建表及旧 DBHandler ping 的7条 warning。 |
| `YUNYING_TEST_DB=yunying_v4_test npm --prefix yunying-dsh test` | **30 passed**，无 skip；实际官方 loop/JSONL/tool/runtime，含模型失败重试。 |
| `npm --prefix yunying-dsh run check` | 全部插件模块语法通过。 |
| `bash -n deploy/deploy-nas.sh deploy/deploy-v4-nas.sh`、`sh -n scripts/run-yunying-profile.sh` | 通过。 |
| `bash scripts/privacy-scan.sh`、`git diff --check` | 通过；提交前再次校验暂存范围。 |

新增回归覆盖：DSH 接收失败时，同会话连续消息/附件元数据仍全部机械提交；断点恢复不重复执行切换命令；OFF群普通消息不生成模型请求，@/reply 携带最近受限上下文且最多一个 Agent；OFF下 wait 能在当前呼叫轮看到普通新消息，active wake config、提醒与定时器不能开启新轮；OFF恢复、先开启再重启均不重放观察积压；实际 CLI 在OFF模式完成 Memory→搜索→回复，并在租约故障后恢复原 Session；硬暂停阻断呼叫，解除硬暂停后等待新呼叫。

真实 SQL 记账测试覆盖：网站连接故障时 native outbox 仍 pending、恢复后重复投递去重；三次模型调用聚合到一个 job；未知 usage 保留 NULL与未知次数，cache/input正确合计；compaction为独立任务；原生三次重试保持一个turn，Session历史回放产生相同task/request ID。既有跨群Person Memory/私有隔离、撤回、图片/语音/转发、官方compaction和重启测试继续通过。

这些结果不等于真实QQ风格、付费提供方计费一致性、NAS长时运行或真实OCR/ASR模型验收。原部署快照仍在部署记录中；本轮迁移003/004只在临时库应用。站点旧摘要/人格控件保留在另一个仓库，未修改或部署；V4侧已停止旧摘要/Prompt bootstrap与业务摘要生成。依赖安全告警未作为本轮功能改动处理。

## 数据退役与归档恢复

清理实现使用独立 loopback MySQL8.0.36（与 NAS 主库相同版本），端口43316经只绑定127.0.0.1的 SSH 转发到独立测试容器；没有指向生产数据库。最终运行：

- `YUNYING_TEST_DB=yunying_v4_test YUNYING_TEST_DB_PORT=43316 YUNYING_RUN_PROFILE_SMOKE=1 python -m pytest tests -q`：**113 passed，无 skip**；73条已知幂等建表/旧 ping warnings。
- `YUNYING_TEST_DB=yunying_v4_test YUNYING_TEST_DB_PORT=43316 npm --prefix yunying-dsh test`：**30 passed，无 skip**。
- `npm --prefix yunying-dsh run check`、两个部署脚本与 Profile shell 的语法检查、`bash scripts/privacy-scan.sh`、`git diff --check`：通过。

新 SQL fixtures 覆盖：完整归档/恢复、原 Prompt 与 NULL/空字符串、tokens/task ID、媒体路径与字段顺序、撤回 tombstone、旧表缺失的历史读取、过期 ingress与命令幂等、陈旧证明/租约/未决发送拒绝、腐坏归档拒绝、DDL 中断重试和恢复后管理员改动保护。fixture 中合成证明只用于测试各 guard，不代表已经做过生产备份恢复；实际全库 SQL 的独立恢复证明与部署结果须另行登记。此前一次复跑因 SSH 测试转发已经退出而连接失败，重新建立只绑定 loopback 的转发后上述完整验证全部通过。

操作与 V3 恢复边界见 [数据退役流程](v4-retirement.md)。Social Prompt 字节、官方 Session/Compaction、Memory 工具权限和模型目录仍由原回归覆盖，不在这次清理中改写。
