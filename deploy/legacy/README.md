# 已退役的部署方式

`deploy_vm.sh` 是旧的「Ubuntu 虚拟机 + systemd + venv」部署脚本。
项目已迁移到 NAS 全栈 Docker Compose，正式入口是 `deploy/deploy-nas.sh`。

本目录仅作留档，**不要**用于新部署。回滚流程见 `deploy/rollback/cutover-state.md`。
