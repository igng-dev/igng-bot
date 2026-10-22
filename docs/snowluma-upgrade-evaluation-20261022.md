# SnowLuma 转接层升级评估（2026-10-22）

状态：**评估完成，未升级生产**。生产继续使用 1.10.2，并叠加本次的启动顺序包装器与 NAS 看门狗。未经下面的维护窗口测试不要直接替换镜像。

## 当前事实

- 镜像：`snowluma-docker-framework:local`，image ID `sha256:a54bd4d0df22cfa080256723a6386bbf0b5cad1f002b0945fb6cfa26ba4b5857`。
- SnowLuma runtime：`1.10.2`（容器内 `/app/snowluma/package.json`）。
- Linux QQ：`3.2.28-48517`（镜像构建参数；更新框架镜像可能同时换成 3.2.32）。
- 端口：OneBot HTTP/WS `3000/3001`（宿主 `3200/3201`），WebUI `5099`，noVNC `6081`。
- 登录卷：`igngbot_napcat-data`（66.93 MB）、`igngbot_napcat-qq-config`（993.8 MB）、`igngbot_napcat-qq-data`（0 B，2026-10-22 实测）。
- 运行时管理：容器内 supervisord 管 QQ 与 SnowLuma；容器 `restart: unless-stopped`；本次新增 NAS 用户 crontab 看门狗。

## 备份记录

- 活备份（运行中，非一致性备份）：`/vol2/1000/Docker/igngbot/v4-backups/napcat-live-20261022T094104Z`，约 617 MB。
- **冷备份（权威）**：`/vol2/1000/Docker/igngbot/v4-backups/napcat-cold-20261022T094234Z`，约 613 MB。QQ 与 SnowLuma 均已停止后打包。
  - `napcat-data.tgz`、`napcat-qq-config.tgz`、`napcat-qq-data.tgz`
  - `.env`、`.env.v4`、`docker-compose.yml`、`docker-compose.v4.yml`
  - `napcat-runtime.txt`（原 image ID / 启动时间）
  - `SHA256SUMS`（三个卷的校验和）
- 还原时先停 `igngbot-napcat`，把对应 tar 解回同名卷，再恢复 compose/env 与旧 image ID；不要只恢复数据库或只恢复配置。

## 故障背景

- `2026-10-19`/`10-20` 日志出现同一 QQ PID 下每约 20 分钟一次的 `session closed → session started`，属于 hook/会话层抖动，不是 QQ 账号反复掉登录。
- `2026-10-20 01:47` QQ 崩溃后被 supervisord 自动拉起，hook 再未上报登录。
- `2026-10-21 17:13` NAS 重启；17:14 容器自动启动，17:15 SnowLuma 连上 QQ 但未收到 login，OneBot 3000/3001 一直未监听。bot 从那时起按 1→300 秒退避重试，累计 130+ 次。
- `2026-10-22` 用“停 hook → 重启 QQ → 等自动登录 → 起 hook”的有序恢复恢复；随后加入启动顺序包装器、OneBot 状态健康字段和 NAS 看门狗。
- 结论：根因在第三方 hook 的登录态同步/启动竞态；QQ 登录卷和 bot 的 OneBot 客户端都不是根因。

## 上游版本

- 当前 `1.10.2` 已落后多个版本。
- `v1.14.22`（2026-10-07）包含与本次直接相关的修复：
  - `fix(onebot): allow a custom session release deadline`
  - `fix(onebot): bring an account back when shutdown stalls`
- `v2.1.1`（2026-10-21）是最新版本，包含安全基线/依赖更新，但属于 major 版本，OneBot/WebUI/配置兼容性需要单独验证。
- 上游 issue #342（1.14.4、Docker）记录了同类“能收不能发 / The QQ connection changed”的半失效状态；该 issue 已关闭，但可见讨论中没有给出可依赖的修复结论。**升级不能替代看门狗**。
- Docker 框架只消费主仓库的 lite tarball；构建方式：
  `SNOWLUMA_TAG=v1.14.22 ./scripts/build-image.sh`（需要 `gh` 与 Docker buildx）。当前框架 README 文档的 QQ 版本是 3.2.32，而本机镜像为 3.2.28；换镜像可能同时换 QQ 版本。

## 选项

### A. 保持 1.10.2 + 本次防护（当前）

不引入版本风险；session 抖动仍可能发生，但启动顺序包装器和看门狗会把“假在线”拉回。这是当前生产状态。

### B. 升级到 v1.14.22（建议先测试）

与当前版本最接近，包含 session 生命周期/停机恢复相关修复。需要重建镜像、冷备份、副本卷测试。QQ 3.2.32 可能影响登录卷兼容性，必须验证自动登录/扫码行为。

### C. 升级到 v2.1.1（后置）

版本最新、安全基线更新，但 major 版本。先让 B 稳定运行一段时间，再评估 OneBot 配置、WebUI、插件/Profile 兼容性。

## 测试计划（维护窗口，不直接换生产）

1. 冷备份三个卷 + `.env`/`.env.v4`/compose；记录旧 image ID（已有）。
2. 在 build host 构建候选镜像：
   `SNOWLUMA_TAG=v1.14.22 ./scripts/build-image.sh`
   导入 NAS 并打测试 tag，例如 `snowluma-docker-framework:1.14.22-test`。
3. 用 helper 容器把三个生产卷复制成 `*-test` 卷；测试容器只挂载 test 卷，绝不把生产卷同时挂给第二个 QQ 客户端。
4. 测试容器使用备用宿主端口（例如 `6099/5109/3300/3301`），保持 `SNOWLUMA_UID/GID`、`shm_size`、`SYS_PTRACE`、`seccomp=unconfined` 与生产一致。
5. 同一 QQ 账号不能在两个客户端同时在线；测试期间停止生产 `igngbot-napcat`，或使用独立测试账号。首选维护窗口停生产、测完恢复。
6. 验收项：
   - QQ 自动登录成功；不要求重新扫码（或明确记录需要扫码）。
   - `login detected` 后 OneBot 3000/3001 监听，`get_login_info` 成功。
   - 在测试群完成收发；不碰生产群。
   - 人为重启 QQ 和 NAS 后，启动顺序包装器/看门狗能恢复，不出现“两个进程都 RUNNING、OneBot 不监听”。
   - 观察至少 24 小时 session churn 频率；与 1.10.2 的 20 分钟级抖动对比。
7. 回滚：停止并删除 test 容器与 test 卷；生产卷未动；旧 image/compose/env 仍在。生产切换失败时按冷备份还原卷并回到旧 image。

## 建议

**暂不升级生产。** 先保留本次的启动顺序包装器 + 看门狗 + `/health` OneBot 状态，解决“进程活着但桥死了”的可观测和自愈问题。然后按上面的计划在维护窗口测试 `v1.14.22`；如果 24 小时验收通过，再安排生产切换。`v2.1.1` 放在 B 之后单独评估。升级前必须再次冷备份，且不要把“升级”当作看门狗的替代品。
