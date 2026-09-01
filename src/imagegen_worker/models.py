"""imagegen_worker activity 出入参模型（pydantic）。

跨 domain 纪律：worker 不 import 其他 domain 的内部模块，activity 入参出参
以本模块与（后续）contracts 生成 SDK 为准。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

RenderTier = Literal["preview", "final"]
"""渲染两档：失效传播默认只重算 preview，final 由用户显式请求或交付节点触发。"""

RoomLabels = Literal["none", "handwritten"]
"""房间名由谁写。**本仓唯一一处"文字层归谁"的开关**，两档的差别只在提示词最后那一句。

- `none`——图里一个字都不出，房间名由确定性排版层叠上去（**现行口径**，红线"图形层不含文字"）；
- `handwritten`——把房间名交给图像模型手写进图里（**实验，2026-09-01 起；未裁决，不是新口径**）。

**默认必须是 `none`**：既有两份模板不写这个字段，行为一个字都不变。

它为什么只能是代码不能只是模板数据：`handwritten` 那一档要把**我们自己的房间名**逐字塞进
提示词（名字取自母版那份房间表，不是让模型看图猜），拼这句话的地方在 `style_prompt`。
模板数据说得了"要什么风格"，说不了"把这九个名字写进去"。**"换风格＝换一份模板数据、代码不动"
这条口径因此在这里破了一次**——破的不是风格那三段，是文字层归属这个开关。"""


class RoomSlot(BaseModel):
    """母版上的一个房间：名字 + 它在图上的位置。

    **位置是算出来的不是描述出来的**：由母版给的房间锚点在图幅里的相对坐标推得
    （左上/正中/右下这九档）。让模型自己看图猜哪间是哪间，它会把厨房安到卧室去——
    首跑实测：母版上没有房间名，出来的图里厨房跑到了次卧的位置。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str
    mask_index: int
    """这间房在母版房间遮罩里的索引。生成之后逐房间比对重合度要用它。"""

    anchor_x_px: int
    anchor_y_px: int


class StyleTemplate(BaseModel):
    """一张风格图的模板本体：**只有风格、构图与禁令，没有任何户型专属内容**。

    户型专属的那部分（房间表、家具不变式、标题、批注）是槽位，由系统从母版与画像填。
    模板与实例分开，是为了同一套户型能换风格、同一个风格能换户型——**顺序与组合是配置**。

    模板是数据不是代码（红线：配置只放数据，逻辑归服务）：本体存 `templates/*.json`，
    这里只定它的形状。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    template_id: str
    style: str
    """风格与画法：画面质感、色调、笔触。"""

    composition: str
    """构图规则：视角、版面、留白。"""

    negatives: list[str] = Field(default_factory=list)
    """负面约束，逐条给。"""

    room_labels: RoomLabels = "none"
    """房间名归谁写（见 `RoomLabels`）。**默认 `none`＝图里不出字**，字由确定性排版层叠
    （待拍板① 的倾向：逐条乱码率相乘，且改一句文案就得重生成整图）。
    `handwritten` 是 2026-09-01 起的**实验档**，业主要的手账风明确要求手写中文房间名；
    错字率有没有把"文字层分离"证伪，要看真跑的逐字比对，不凭印象。"""

    size: str = "2K"


class AtmosphereVisual(BaseModel):
    """一次风格图生成的产物。"""

    image_png: bytes
    template_id: str
    prompt: str
    """真正发出去的那段提示词——**留档是为了将来能回答"这张图当时是怎么要出来的"**。"""

    revised_prompt: str = ""
    """模型侧改写后的提示词（有些模型会回这个），如实收下不解释。"""


class AtmosphereVisualRequest(BaseModel):
    """atmosphere-visual 输入：模板库驱动，几何输入＝私有桶里的那张母版。

    **边界上是不透明字典，进来之后才成模型**（同渲染层出册 activity）：派发方不 import 本仓的
    存根签名，两边只靠 contracts 注册名接头。这个模型是本仓自己的收货单，不是跨仓契约。

    **未知字段当场拒收**（`extra="forbid"`）：两侧的字段口径对不上就是接不上头，静默丢掉一个
    字段的代价是"派发方以为交代了、执行方没收到"，而两边的单测都是绿的。

    **本轮不收 `renderTier`**：两档要有差别，得先有一个能分档的东西——模板数据里第二个 `size`，
    或网关里第二个逻辑模型名。在那之前收下它就是收下一个不起作用的开关，真要 `final` 的人
    拿到 preview 的图也看不出来。触发条件写死＝上述两样任一出现时，本模型加字段、
    `atmosphere.render_atmosphere_visual` 按它分支。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    master_object_key: str
    """母版在私有桶里的键（`uploads/{content_sha256}/plan-master.png`），不是本地路径。

    传键不传字节：母版几 MB，进编排 payload 会被 Temporal 历史一直背着；而键是确定性派生的，
    重放时按同一条键取到的还是同一份字节。"""

    room_anchors_object_key: str
    """房间表（房间名 + 遮罩序号 + 锚点）在私有桶里的键。**取键不取内联**，两条判据：

    ①**编排手上没有内联的那一份**：房间表是 `plan-2d-render` 的产物，而那个 activity 的回执
    里只有对象键（图与 JSON 都不内联——那是拿 Temporal 当文件传输通道用）；workflow 自己
    不做 IO，所以内联根本无处可取。
    ②**取键这件事本来就要做**：母版必须走桶，取一次和取两次是同一条通路上的同一件事。

    键由派发方从 `plan-2d-render` 回执里原样带过来（那边叫 `room_anchors_key`），
    本仓**不抄它的文件名**——只验它与母版同前缀（见 `image_store.check_shares_upload_prefix`）。"""

    template_id: str
    """模板数据的 id（`templates/*.json` 里的那批）。进程起来时装好，认不得的 id 当场失败。"""


class RealismPassRequest(BaseModel):
    """realism-pass 输入：生成式写实化（工厂效果图同用）。"""

    base_render_artifact_id: str
    style_ref: str
    render_tier: RenderTier = "preview"
