"""imagegen_worker：生成式出图 activity 执行进程（imagegen-svc）。

V1.4 裁决（2026-08-23）：绘图能力物理拆分——本仓承接 atmosphere-visual 与
realism-pass（扩散模型：模板驱动风格化、写实化），独立部署 Temporal worker，
专属 task queue `imagegen-activities`，无对外 RPC 端口、无数据库 schema、
无状态（算完即焚，产物写 OSS + 注册 ArtifactRegistry）。伸缩轴：外部模型
API 配额 / GPU 推理。
"""
