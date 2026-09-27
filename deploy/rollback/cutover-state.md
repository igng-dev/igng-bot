# IGNGbot NAS 迁移 — 回滚预案（2026-09-19）

## 当前状态（迁移已完成）

- NAS `192.0.2.17` 运行完整 compose 栈：`bot` / `napcat` / `media` / `frpc`
- 工程目录：`/vol2/1000/Docker/igngbot`
- 数据目录：`/vol2/1000/Docker/igngbot/data/message_logs`
  （2026-09-19 11:40 从 `/vol1/1000/IGNGbot` 复制搬迁，11343 文件经全量 md5 校验逐字节一致）
- QQ 登录态已迁移为 named volume，无需重新扫码
- VM `192.0.2.28` 的 `igngbot-v3.service` 已 **stop + disable**，`snowluma` 容器已 stop
- VM 上的孤儿容器 `igngbot-searxng` 已删除（代码已不引用、0 请求）
- 旧 VM 的 bot 代码副本 `/home/deploy/igngbot-v3` **落后于本地仓库**（缺少站点 AI 镜像等改动）；
  回滚时若需要旧行为可用它，但不要用它重新部署。

> **数据搬迁后的回滚差异**：原目录 `/vol1/1000/IGNGbot` 已保留为迁移时的完整快照，
> 且 VM 的 CIFS 共享 `IGNGbot` 正映射到它。因此回滚时**不要**反向跑路径改写脚本，
> 直接停 NAS 栈、启动 VM 即可 —— VM 读到的就是它原本熟悉的那份数据。
> 若回滚后又切回 NAS，需重新执行一次搬迁（见下方“数据搬迁”章节）。

## 回滚触发条件

- NAS 上 NapCat 登录失效且无法恢复
- 机器人持续无法收发消息
- 站点聊天记录媒体大面积不可访问

## 回滚步骤

### 1. 停 NAS 栈

```bash
ssh nas 'cd /vol2/1000/Docker/igngbot && docker compose down'
```

### 2. 恢复数据库路径（关键，不可跳过）

迁移时把 `message_logs` 的路径改写为容器内根路径 `/data/message_logs`。
旧 VM 只能识别 `/mnt/media/message_logs`，因此必须先反向改写：

```bash
cd /home/deploy/项目/IGNGbot
set -a && . ./deploy/docker/.env && set +a
MESSAGE_ROOT=/data/message_logs \
REVERT_PREFIX=/mnt/media/message_logs \
LEGACY_PATH_PREFIXES=/data/message_logs \
python deploy/migrations/rewrite_media_paths.py --revert --apply
```

> 反向改写同样覆盖 `file_url` / `audio_file_path` / `attachments_json` / `message_structure`。
> 迁移前遗留的 40 行 `removed image-generation feature` / `typo-variant path` 记录从未被改写，
> 反向操作也不会触碰它们。

> **注意**：路径前缀改写与文件存放位置是两件独立的事。搬迁只改 compose 的
> `BOT_STORAGE_ROOT`，数据库里始终是容器内路径 `/data/message_logs`，因此
> **搬迁本身不需要任何数据库操作**。上面这一步只是为了让旧 VM 能读懂。

### 2b. 若已搬迁数据、且要回滚到 VM

`/vol1/1000/IGNGbot` 仍保留着搬迁前的完整快照，VM 的 CIFS 共享正指向它，
所以**无需把数据搬回去**：停掉 NAS 栈后直接启动 VM 即可。
但搬迁之后新产生的附件只存在于 `/vol2/1000/Docker/igngbot/data`，
如需让 VM 也看到这部分新数据，把它们复制回去：

```bash
ssh nas 'cp -an /vol2/1000/Docker/igngbot/data/message_logs/. /vol1/1000/IGNGbot/message_logs/'
```

（`-n` = 不覆盖已存在文件，保留 VM 侧原有内容。）

### 3. 回滚站点

站点 `bot-media.js` 的改动是**向后兼容**的（同时认旧前缀与新前缀），通常无需回滚。
如需完全回退：

```bash
cd /home/deploy/项目/IGNG站点
git checkout -- apps/igngchat/lib/bot-media.js
node scripts/deploy-app.js igngchat --force-upload
```

站点容器 NapCat 变量若需一并回退，恢复同目录下的 `.env.bak-pre-nas-*` 备份后重启容器。

### 4. 启动 VM 旧服务

```bash
ssh ubuntu-vm 'sudo systemctl enable --now igngbot-v3 && docker start snowluma'
```

### 5. 验证

- VM bot 日志出现 `Connected to SnowLuma OneBot WebSocket` 与 `self_id detected`
- 群内发一条消息，确认 `message_logs` 新行 `file_url` 为 `/mnt/media/message_logs/...`
- 站点聊天记录媒体可正常显示

## 迁移前状态存档

- VM NapCat volume：`snowlumadocker_snowluma-data` / `-qq-config` / `-qq-data`（**未删除**）
- **附件数据快照**：`/vol1/1000/IGNGbot`（搬迁前完整副本，11343 文件 / 955MB，**暂勿删除**）
- NAS 旧独立媒体工程备份：`/fs/1000/ftp/IGNGcloud/.service-bot.pre-igngbot-compose-*`
- 站点 `.env` 备份：`~/项目/IGNG站点/.env.bak-pre-nas-*`
- 站点远端 `.env` 备份：`/opt/1panel/www/sites/IGNG{chat,bbs}/index/.env.bak-pre-nas-*`
- VM 镜像：`snowluma-docker-framework:local` 仍保留（可重新导出）

## 回滚窗口结束后可清理的对象

> 仅在确认新架构稳定运行满 48 小时（即 2026-09-21 02:40 之后）再执行。

| 对象 | 位置 | 说明 |
| --- | --- | --- |
| `/vol1/1000/IGNGbot` | NAS | 搬迁前数据快照，约 955MB；删除前请确认新目录数据完好 |
| `igngbot-v3.service` + `/home/deploy/igngbot-v3` | VM | 旧 bot 代码与单元（代码已落后，勿再用于部署） |
| `snowluma` 容器 + 3 个 volume | VM | 登录态已迁至 NAS named volume |
| CIFS 挂载 `/mnt/media` + fstab 条目 | VM | 迁移后已无任何容器使用 |

**仍然不能删除的**：

- VM 上的另外 19 个容器（home-agent-lab / meteohub / ai-fiction-arena / continuity-hub 等），
  与 IGNGbot 无关，仍在提供服务
- VM 本身 —— 它是 `deploy-nas.sh` 的镜像构建机（`BUILD_HOST=ubuntu-vm`），
  NAS 无法直连 pypi/deb 源，删掉就再也发不了版
