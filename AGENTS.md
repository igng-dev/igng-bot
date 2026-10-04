# IGNGbot（igng-bot）· 项目 Agent 规范
> 只写本仓库与设备级规范的差异。Git 纪律、worktree、冲突处理见 `~/.qoder/coder-rules/global-rules.md`。

Collaboration: solo
Baseline: main
Default branch: main
Release: manual（CI 验证不自动发布或部署）
Worktree: `~/项目/.wt/IGNGbot/<slug>`

## 这是什么
云萤 QQ Social Agent。V4 为官方 DSH 的 out-of-tree Profile/Plugins/Tools/Skill（`yunying-dsh/`）与 Python 基础设施（`igngbot_v4/`）；V3 成熟组件和显式回滚入口保留在 `igngbot_v3/`。

## 验证命令
- 安装：`python -m pip install -r requirements-dev.txt`（含 `requirements.txt`）。依赖使用设备共享缓存，见 `docs/testing.md`。
- Python：`python -m pytest tests -q`。
- Node：在 `yunying-dsh/` 执行 `npm test`、`npm run check`。
- 脚本：`bash -n deploy/deploy-nas.sh deploy/deploy-v4-nas.sh`、`sh -n scripts/run-yunying-profile.sh`。
- 凭据与范围：`bash scripts/privacy-scan.sh`、`git diff --check`。
- 修改会话、数据库、Memory 或 Profile 时必须运行临时数据库/双进程 opt-in 集成；环境与完整命令见 `docs/testing.md`。已有 `.github/workflows/ci.yml` 使用一次性 MySQL8.4，不读生产凭据。fixture 通过不等于真实 QQ/模型/NAS 已验收。

## 项目特殊限制
- `老程序v2（仅供了解功能）/` 只读参考；`_upstream_astrbot/` 是上游框架目录（自带 136 行 AGENTS.md 与 142 行 CONTRIBUTING.md）——**两者都不是改动目标**，也不得用它们的约定替代本仓库约定。
- 机器人经手消息与用户数据：真实账号、Token、Cookie、用户数据不得进入提交；数据库口令走环境变量。
- 历史未提交/失踪 worktree 清单可能过时；每次以实际 `git status` 与 `wt audit` 为准。不得 stash / reset / clean 或自动 prune、删除归属不明的他人工作。
