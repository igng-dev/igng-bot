## 运行环境

本程序已从「Ubuntu 虚拟机运行 + NAS 存储」迁移为 **NAS 全栈 Docker Compose 部署**。

### NAS（生产环境）

- 主机：`192.0.2.17`（飞牛 NAS，Debian 12）
- 用户：`serviceuser`
- 部署方式：Docker Compose，工程目录 `/vol2/1000/Docker/igngbot`
- 服务：`bot` / `napcat` / `media` / `frpc`
- 附件数据：`/vol2/1000/Docker/igngbot/data/message_logs`（NAS 本地目录，不再走 CIFS）
- 运维入口：`deploy/deploy-nas.sh`（`up` / `status` / `logs` / `down`）

### Ubuntu 虚拟机（已退役，保留作回滚）

- 主机：`192.0.2.28`
- 连接凭据（含密码）见本地未跟踪文件：`secrets/vm.env`
- 旧 systemd 单元 `igngbot-v3.service` 已 stop + **disable**
- 旧容器 `snowluma` 已 stop
- 用途：镜像构建机（`BUILD_HOST`）+ 48 小时回滚窗口
- **注意**：`/home/deploy/igngbot-v3` 的代码副本落后于本地仓库，不要用它重新部署

## 开发要求

- 在本地完成开发后，通过 `deploy/deploy-nas.sh up` 部署到 NAS。
- 代码改动必须同步更新其单元测试；部署前先跑 `python -m pytest tests -q`。

## Git 与备份要求

- 本项目使用私有 GitHub 仓库 `igng-bot` 作为代码备份。
- 新工作区首次使用时执行 `git config core.hooksPath .githooks`，启用提交和 push 阶段的隐私检查。
- 每次开发完成后，先运行 `powershell -ExecutionPolicy Bypass -File scripts/privacy-scan.ps1`，确认扫描通过后再提交和 push。
- 只提交源代码、提示词、部署脚本、依赖清单和文档；运行日志、数据库/消息数据、缓存、虚拟环境、旧程序归档、上游源码副本和本机 agent 配置不进入仓库。
- 所有密码、API Key、Access Token、WebSocket 密钥和私钥必须通过环境变量或部署机上的未跟踪 `.env` 提供，禁止写入源文件、示例文件或提交信息。
- 推荐流程：`git status` -> 隐私扫描 -> 测试/编译检查 -> `git add` -> 再次隐私扫描 -> `git commit` -> `git push`。
- push 被隐私检查阻止时，先删除或脱敏问题内容，不得使用 `--no-verify` 绕过检查。

## 相关仓库

- IGNG 站点：`~/项目/IGNG站点`
  - `apps/igngchat/lib/bot-media.js` 负责把 `message_logs` 中的附件路径转成签名媒体 URL
  - `services/igng-bot-media/` 是 `media` 服务的源码，部署时由 `deploy-nas.sh sync` 同步到 NAS
