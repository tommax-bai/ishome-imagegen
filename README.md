# ishome-imagegen

《是我的家》生成式出图服务（`imagegen-svc`）：独立部署的 Temporal worker，承接扩散模型出图（模板驱动风格化交付图、生成式写实化），外部模型 API / GPU 推理伸缩轴。

- **出处**：V1.4 裁决（2026-08-23，绘图能力物理拆分）——中控仓《架构对齐-设计Agent×技术架构.md》§三；绘图逻辑异质 → 独立仓库 + 独立服务，无 RPC、无 schema、无状态。
- **task queue**：`imagegen-activities`（namespace `genpipe`；注册表：ishome-contracts `registries/task_queues.md`）。
- **本仓 activity**（注册名唯一真源：ishome-contracts `activities/registry.md`，只增不改）：

| 注册名 | 函数名 | 职责 | 状态 |
|---|---|---|---|
| `atmosphere-visual` | `generate_atmosphere_visual` | 风格化交付图生成（模板库驱动，固定遮罩） | **已实现**（2026-08-31 真跑通过） |
| `realism-pass` | `apply_realism_pass` | 生成式写实化（工厂效果图同用）：线稿控制通道，几何由我们的线稿定 | **已实装**（2026-09-05，假后端全链单测；真跑等网关那头的线稿生图 handler） |

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
| `annotations`（可选） | 要写上图的注释 `[{room, text}]`——**内容我们给，模型只画字**（用户裁决 2026-09-01：注释要有、不能由模型临时编）。文本一律不含数字（数字上图走叠印那条线，未建）；落点房间必须在房间表里，位置由那间房的锚点换算成模型原生归一化坐标（`<point>`，[0,999]）钉上去；**只有写字档模板收得下**，给零字模板递它当场 failed（「不出字」与「写这些字」不许在同一份提示词里打架） |
| `lifeObjectSlots`（可选） | 逐间物件槽位 `[{room, objects}]`（每间 1~6 样，**清单语义＝全集**：功能家具＋生活物件都在里面，每间房画且只画清单里的——2026-09-01 真跑定罪：清单只装生活物件时被模型当"近似全集"，小孩房的床被挤掉）。**模型不许猜生活需求**：该画什么从户型事实与家庭结构假设推、由派发方传入。**给了就得给全**：缺一间、一间给两份、注释提到的实体（床/书桌/洗衣机这类，字面匹配）不在那间清单里，都当场 failed；一间都不给＝整张中性画法（旧派发口径）。**所有画风模板都吃它**（三张图家具一致性的底子）。两个字段都不给＝行为与从前逐字节相同 |

出参 `verdict=ok` 时给 `image_object_key` / `bucket` / `content_type` / `image_size_bytes` / `room_count` / `prompt` / `revised_prompt`；失败时给 `violations`（逐条，不空替不静默）。

- **产物键**：`uploads/{content_sha256}/atmosphere-{template_id}.{ext}`——**与源图同前缀**，由母版键确定性派生，同母版同模板同格式重跑覆盖同一个对象（唯一真源＝contracts `registries/object_keys.md`，本仓持逐字副本 + 守门测试）。
- **只写不签**：签名是"给谁看、看多久"的事，属业务侧——生成侧不知用户是谁。因此"这张图出没出来"问存储即知，不另立台账。
- **扩展名与 `Content-Type` 同源同判，都按字节首部定**（用户裁决 2026-09-02"标签跟着内容走"）：真跑实测网关回来的字节是 **JPEG**，键就是 `.jpg`、头就是 `image/jpeg`；认不出的首部整条响亮失败，不按文件名猜。此前"键写死 `.png`、只有头跟字节走"的口径被该裁决推翻；**认下的代价**：换一个回不同格式的物理模型（变化轴 3），同一张图的键会换后缀，按旧键取的一侧要认两个名——拍板时摆过这一条，取"诚实"舍"稳定"。
- **写不进桶就不是 ok**：图出得再好、落不了地也按失败回报——回一个指向空气的键，下游会拿它去发给业主。

## 写实化（`realism-pass`）

**几何由我们的线稿定，不由模型定**（《评审/控制图通路调研-2026-09-02.md》）：从参考图接口（Seedream，三跑三套房）换到**控制通道**（万相线稿生图 `is_sketch=true`：线稿转 90° 出图跟着转、同 seed 逐像素相同、三 seed 极差 0.004）。网关那头是 infra 的 LiteLLM custom handler，本仓只认逻辑模型名 `realism-pass.default`，请求形态＝`POST /v1/images/generations` + `is_sketch: true` + 一张白底黑线 RGB PNG + 可选 `seed`（`is_sketch` 缺失网关拒 4xx，不静默退化）。

**入参**（不透明字典，`extra=forbid`）：

| 入参 | 是什么 |
|---|---|
| `lineKey` **或** `sketchKey`（二选一、必给其一） | render3d `base-render` 那路 `line.png`（黑底白线单通道 0/255）在私有桶里的键；`sketchKey` 留给第五路控制稿（那一路今天还没有）。本仓对两者处理逐字节一样：反色成白底黑线 RGB，不缩放不抗锯齿 |
| `styleTemplateId` | `templates/realism/*.json` 的 id。模板只有风格文字与禁令（材质与光），视角句/几何约束句/无陈设句/无文字句固定在代码里——它们是规矩不是风格 |
| `viewKind` | `bird`（揭顶鸟瞰）/ `room`（室内机位）。**`room` 那句视角提示词没有真跑原话可抄，是执行者写的，未经真跑** |
| `cameraId` | 哪台机位渲的（进产物键，同一套房好几台机位不互相覆盖） |
| `seed`（可选） | 原样送后端；不给＝那一跑不可复现，本仓不替派发方铸 |

**没有档位参数**（用户裁决 2026-09-04：渲染只有一档）。**默认不摆任何家具和陈设**（同日裁决：陈设有无/位置/尺寸算设计，设计上游不存在就不许模型摆），只渲墙面地面天花门窗的材质和光。

**出参** `verdict=ok`：`image_object_key` / `bucket` / `content_type` / `image_size_bytes` + 自证数 `backend_name` / `seed` / `fidelity_score` / `elapsed_seconds` / `prompt_sha256` / `prompt` / `gate`。失败给 `violations` 逐条；门禁不过线（`fidelity-gate-failed`）时自证数照带、**图不进桶**。

- **产物键**：`{线稿键的前缀}/realism-{camera_id}-{style_template_id}.{ext}`——与源线稿同前缀、由源键确定性派生（照风格图那条键的先例），派发方不给 `output_key`、本仓不铸。**键形态待 contracts `registries/object_keys.md` 登记**（render3d 自己的四路键也还没登记）。
- **机位 id 的字符集与 render3d 不一致（待拍，不是裁决）**：本仓 `image_store.check_camera_id` 只收 `[a-z0-9-]`（同模板 id 的口径），而 render3d `object_store.check_key_segment` **不限定字符集**，拟真包里的室内机位 id 带房间名（`cam-room-客厅`）。这样的机位派到 `realism-pass`，会在写桶那一步（`image-store-failed`）才被拦下——图已经出了、钱已经花了。两种统一方向：①机位 id 一律 ASCII slug，房间名另存字段（render3d `CameraSpec.room` 已有）；②本仓放宽到 render3d 那条口径（只拦空、`/`、`..`、首尾空白、控制字符）。拍板前**本仓行为不动**。
- **出口门禁**（`RealismGate`）只做两件：算分、与配置里的下限比。分数＝`fidelity_metric`（从 render3d `保真度量.py` 原样搬入：Sobel+NMS+位置/方向双重命中，三常量不动）。**下限 `ISHOME_REALISM_MIN_FIDELITY_SCORE` 不配＝只记录不判**（阈值有数据才定；尺子两条已知限制：分数只能同输出形态横向比——几何全对的底渲自量只有 0.0607；透视视角未自证）。不做重出循环，那是编排的事。
- **后端只从配置来**：`ISHOME_REALISM_BACKEND`（缺省 `gateway-sketch`＝走网关的线稿控制路），代码里不出现厂商名；自部署 ControlNet 是第二形态，加一个 `RealismBackend` 实现、配置里点名。

## CLI（本地迭代入口，不废）

```bash
uv run imagegen --master out/plan-master.png --rooms out/rooms.json \
                --template templates/cream-journal.json -o style.png
# 注释与槽位是数据文件（与 activity 的两个可选入参同形）：
#   --annotations annotations.json --life-objects life-objects.json

# 写实化：render3d 的 line.png + 风格 id + 视角 → 一张图，同时打印分数与自证数
uv run imagegen realism --line out/cam-bird-dollhouse/line.png --style modern-minimal --view bird \
                --seed 12345 --gateway http://127.0.0.1:4000 -o realism.png
# 旁边落 realism.sketch.png（真正送出去的控制稿）与 realism.prompt.txt（真正发出去的话）
```

换模板、看一张图长什么样走它，不必起 Temporal、也不碰私有桶。与 activity 那条路**共用同一份纯库代码**（`atmosphere` / `style_prompt` / `image_gateway`），区别只在母版字节从哪儿来；分界由 import-linter 锁死（`cli` 看不见 `activities`）——从它能看见起，"本地改模板不需要桶凭证"就只是一句承诺而不是结构。

## 红线（违反即返工，来路见中控仓《交接文档-三张图出得来.md》§五）

1. **房间表非填不可**：不给房间表，模型只能靠家具猜功能——首跑实测厨房跑到了次卧的位置。房间方位由母版锚点**算成**九档，不是看图说的。
2. **网关必查回执 `input_images`**：图生图的 `image`/`size`/`watermark` 都不是 OpenAI 标准参数，网关一开 `drop_params` 就丢掉，**调用照样成功、出来的图与母版无关**。不是 1 就整张失败——静默退化成文生图是这条线上最贵的失败。
3. **每张图独立回读母版**：只吃母版，不吃"上一张风格图"——用上一张接着生成下一张，户型会一路漂。
4. **模板是数据**：新风格＝新 JSON + 0 行代码（第三张彩铅即此兑现）；模板本体只有风格、构图、禁令，户型专属那部分是槽位由母版填。**这条 2026-09-01 被检验时破了一次**，见下方《模板库》。
5. **只认逻辑模型名**（`atmosphere-visual.default`）：物理模型映射在网关配置里，换模型不动代码一行。
6. **图形层不含文字**：字由确定性排版层叠上去（在 render2d）。**口径未变**——`roomLabels` 默认 `none`，仓里只有那份挂着实验名的模板走 `handwritten`（门禁 `test_only_the_experiment_template_writes_its_own_text` 守着）。

## 模板库（`templates/*.json`）

| 模板 id | 画风 | 文字层 | 顶/底留白（真跑实测） |
|---|---|---|---|
| `cream-journal` | 奶油粉水彩手账 | 零文字 | 235px(8.3%) / 387px(13.7%) |
| `pencil-sketch` | 彩铅生活草图 | 零文字 | 71px(3.1%) / 79px(3.5%) |
| `lifestyle-notebook` | 手账风（业主给的那份描述） | 零文字 | **28px(1.2%) / 27px(1.2%)** |
| `lifestyle-notebook-handwritten` | 同上 | **模型手写房间名＋注释（免费第三张，用户裁决 2026-09-01：注释内容我们给、模型只画字）**；`size` 显式 `1584x2816`（"2K" 的长宽比由模型自己挑，显式尺寸才守得住比例） | 不留白（字画在图里，本就不留） |

**这一轮检验出来的两件，都如实记着，别当它们已经解决**：

1. **"换风格＝换模板数据、代码不动"对三段成立，对文字层不成立。** 风格/构图/禁令确实一行代码没动；
   但"房间名由谁写"这个开关是代码（`models.RoomLabels` + `style_prompt` 那一句），因为
   `handwritten` 要把**我们自己的房间名**逐字塞进提示词，而模板数据说不了"把这九个名字写进去"。
2. **留白带不是写进模板就有的。** `lifestyle-notebook` 抄了 `cream-journal` 一模一样的
   "顶 24%/底 28% 留空"那句话，真跑只给到 1.2%/1.2%——**同样的话在不同画风下不成立**。
   本仓不判留白（叠字门禁在 render2d 的 `style_caption`），所以这种图在本仓一路绿灯、
   到叠字那一步才整张失败。**要挡在这儿，得给本仓也加一道量版面的门禁**（未做，未拍板）。

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
