# IGNGbot（igng-bot）· 项目 Agent 规范
> 只写本仓库与设备级规范的差异。Git 纪律、worktree、冲突处理见 `~/.qoder/coder-rules/global-rules.md`。

Collaboration: solo
Default branch: main
Integration: direct-after-validation
Release: manual（**本仓库无 CI workflow**）
Worktree: `~/项目/.wt/IGNGbot/<slug>`

## 这是什么
QQ 群机器人（Python，`requirements.txt`，当前实现在 `igngbot_v3/`）。

## 验证命令
- 安装：`pip install -r requirements.txt`。
- 本仓库**没有登记的自动化测试入口**：改动只能靠人工验收，不得声称"已测试"。需要自动化时先补 pytest 入口或最小 CI workflow，单独一次提交。

## 项目特殊限制
- `老程序v2（仅供了解功能）/` 只读参考；`_upstream_astrbot/` 是上游框架目录（自带 136 行 AGENTS.md 与 142 行 CONTRIBUTING.md）——**两者都不是改动目标**，也不得用它们的约定替代本仓库约定。
- 机器人经手消息与用户数据：真实账号、Token、Cookie、用户数据不得进入提交；数据库口令走环境变量。
- 工作区有 58 个未提交改动，进入本仓库先报告，不得 stash / reset / clean。
- 存在 1 条目录已消失的 worktree 记录（`.codex-mc-migration-20260824/`）：会话开始执行 `git worktree prune`。
