# ishome-imagegen

《是我的家》生成式出图服务（`imagegen-svc`）：独立部署的 Temporal worker，承接扩散模型出图（模板驱动风格化交付图、生成式写实化），外部模型 API / GPU 推理伸缩轴。

- **出处**：V1.4 裁决（2026-08-23，绘图能力物理拆分）——中控仓《架构对齐-设计Agent×技术架构.md》§三；绘图逻辑异质 → 独立仓库 + 独立服务，无 RPC、无 schema、无状态。
- **task queue**：`imagegen-activities`（namespace `genpipe`；注册表：ishome-contracts `registries/task_queues.md`）。
- **本仓 activity**（注册名唯一真源：ishome-contracts `activities/registry.md`，只增不改）：

| 注册名 | 函数名 | 职责 | 状态 |
|---|---|---|---|
| `atmosphere-visual` | `generate_atmosphere_visual` | 风格化交付图生成（模板库驱动，固定遮罩） | **已实现**（2026-08-31 真跑通过） |
| `realism-pass` | `apply_realism_pass` | 生成式写实化（工厂效果图同用） | 存根——**它的输入还不存在**：吃的是 render3d `base-render` 的底渲产物，那条线一张图都还没出来。落地时点写死＝第一张底渲出得来时 |

## 出图服务（`imagegen-worker`）

**至此成服务**（2026-08-31）。原形态是"纯库 + CLI，接进 activity 的时点写死＝派发链路接通时"——母版进了私有桶、图要送到业主手上，触发条件即此（形态照 reportrender 的先例）。

```bash
set -a; source ~/.ishome/oss-local.env; source ~/.ishome/llm-local.env; set +a   # 凭证不入库
export LITELLM_API_KEY=$LITELLM_MASTER_KEY
export ISHOME_IMAGEGEN_TEMPLATES_DIR=$PWD/templates
uv run imagegen-worker                       # 监听 imagegen-activities（TEMPORAL_ADDRESS 默认 localhost:7233）
```

**`atmosphere-visual` 出入参**（边界上是**不透明字典**：派发方不 import 本仓存根签名，两边只靠 contracts 注册名接头）：

| 入参 | 是什么 |
|---|---|
| `master_object_key` | 母版在私有桶里的键 `uploads/{content_sha256}/plan-master.png`（render2d `plan-2d-render` 写的那份，回执里的 `master_key`） |
| `room_anchors_object_key` | 房间表的键（同一次绘制的产物，回执里的 `room_anchors_key`）。**只验它与母版同前缀，不抄它的文件名**——文件名归写它的那一侧定 |
| `template_id` | `templates/*.json` 里那批模板的 id；起进程时装好，认不得的 id 当场失败 |

出参 `verdict=ok` 时给 `image_object_key` / `bucket` / `content_type` / `image_size_bytes` / `room_count` / `prompt` / `revised_prompt`；失败时给 `violations`（逐条，不空替不静默）。

- **产物键**：`uploads/{content_sha256}/atmosphere-{template_id}.png`——**与源图同前缀**，由母版键确定性派生，同母版同模板重跑覆盖同一个对象（唯一真源＝contracts `registries/object_keys.md`，本仓持逐字副本 + 守门测试）。
- **只写不签**：签名是"给谁看、看多久"的事，属业务侧——生成侧不知用户是谁。因此"这张图出没出来"问存储即知，不另立台账。
- **键的后缀是协议，不是格式断言**：真跑实测网关回来的字节是 **JPEG**，`Content-Type` 因此**按字节首部写**（`image/jpeg`），而键仍是 `.png`——换物理模型是常事（变化轴 3），键跟着模型的输出格式变会让同一张图长出两个对象。
- **写不进桶就不是 ok**：图出得再好、落不了地也按失败回报——回一个指向空气的键，下游会拿它去发给业主。

## CLI（本地迭代入口，不废）

```bash
uv run imagegen --master out/plan-master.png --rooms out/rooms.json \
                --template templates/cream-journal.json -o style.png
```

换模板、看一张图长什么样走它，不必起 Temporal、也不碰私有桶。与 activity 那条路**共用同一份纯库代码**（`atmosphere` / `style_prompt` / `image_gateway`），区别只在母版字节从哪儿来；分界由 import-linter 锁死（`cli` 看不见 `activities`）——从它能看见起，"本地改模板不需要桶凭证"就只是一句承诺而不是结构。

## 红线（违反即返工，来路见中控仓《交接文档-三张图出得来.md》§五）

1. **房间表非填不可**：不给房间表，模型只能靠家具猜功能——首跑实测厨房跑到了次卧的位置。房间方位由母版锚点**算成**九档，不是看图说的。
2. **网关必查回执 `input_images`**：图生图的 `image`/`size`/`watermark` 都不是 OpenAI 标准参数，网关一开 `drop_params` 就丢掉，**调用照样成功、出来的图与母版无关**。不是 1 就整张失败——静默退化成文生图是这条线上最贵的失败。
3. **每张图独立回读母版**：只吃母版，不吃"上一张风格图"——用上一张接着生成下一张，户型会一路漂。
4. **模板是数据**：新风格＝新 JSON + 0 行代码（第三张彩铅即此兑现）；模板本体只有风格、构图、禁令，户型专属那部分是槽位由母版填。
5. **只认逻辑模型名**（`atmosphere-visual.default`）：物理模型映射在网关配置里，换模型不动代码一行。
6. **图形层不含文字**：字由确定性排版层叠上去（在 render2d）。

## 常用命令

```bash
uv sync                 # 安装依赖与 dev 工具
uv run ruff check .     # lint
uv run ruff format .    # 格式
uv run lint-imports     # import 方向契约（worker|cli → activities → atmosphere → style_prompt → models；两个出站边缘不感知上层）
uv run mypy             # strict 类型检查
uv run pytest           # 测试（activity 注册名 + 对象键 + 两道门禁守门）
```

新 clone 后执行一次：`git config core.hooksPath .githooks`（本地 pre-push 质量门）。
