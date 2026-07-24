## Ubuntu虚拟机
- 主机：`192.0.2.28`
- 用户：`deploy`
- 连接凭据（含密码）见本地未跟踪文件：`secrets/vm.env`
- 该虚拟机用于部署本程序。

## 开发要求
- 在本地完成开发后，在虚拟机完成部署。

## Git 与备份要求
- 本项目使用私有 GitHub 仓库 `igng-bot` 作为代码备份。
- 新工作区首次使用时执行 `git config core.hooksPath .githooks`，启用提交和 push 阶段的隐私检查。
- 每次开发完成后，先运行 `powershell -ExecutionPolicy Bypass -File scripts/privacy-scan.ps1`，确认扫描通过后再提交和 push。
- 只提交源代码、提示词、部署脚本、依赖清单和文档；运行日志、数据库/消息数据、缓存、虚拟环境、旧程序归档、上游源码副本和本机 agent 配置不进入仓库。
- 所有密码、API Key、Access Token、WebSocket 密钥和私钥必须通过环境变量或部署机上的未跟踪 `.env` 提供，禁止写入源文件、示例文件或提交信息。
- 推荐流程：`git status` -> 隐私扫描 -> 测试/编译检查 -> `git add` -> 再次隐私扫描 -> `git commit` -> `git push`。
- push 被隐私检查阻止时，先删除或脱敏问题内容，不得使用 `--no-verify` 绕过检查。
