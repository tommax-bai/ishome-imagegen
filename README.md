# ishome-imagegen

《是我的家》生成式出图服务（`imagegen-svc`）：独立部署的 Temporal worker，承接扩散模型出图（模板驱动风格化交付图、生成式写实化），外部模型 API / GPU 推理伸缩轴。

- **出处**：V1.4 裁决（2026-08-23，绘图能力物理拆分）——中控仓《架构对齐-设计Agent×技术架构.md》§三；绘图逻辑异质 → 独立仓库 + 独立服务，无 RPC、无 schema、无状态。
- **task queue**：`imagegen-activities`（namespace `genpipe`；注册表：ishome-contracts `registries/task_queues.md`）。
- **本仓 activity**（注册名唯一真源：ishome-contracts `activities/registry.md`，只增不改）：

| 注册名 | 函数名 | 职责 |
|---|---|---|
| `atmosphere-visual` | `generate_atmosphere_visual` | 风格化交付图生成（模板库驱动，固定遮罩） |
| `realism-pass` | `apply_realism_pass` | 生成式写实化（工厂效果图同用） |

## 常用命令

```bash
uv sync                 # 安装依赖与 dev 工具
uv run ruff check .     # lint
uv run lint-imports     # import 方向契约（worker → activities → models 单向）
uv run mypy             # strict 类型检查
uv run pytest           # 测试（activity 注册名守门）
uv run imagegen-worker  # 起 worker（TEMPORAL_ADDRESS，默认 localhost:7233）
```

新 clone 后执行一次：`git config core.hooksPath .githooks`（本地 pre-push 质量门）。
