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
    """负面约束，逐条给。**"不要出字"永远在里面**——字由确定性排版层叠，不交给图像模型
    （待拍板① 的倾向：逐条乱码率相乘，且改一句文案就得重生成整图）。"""

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
    """atmosphere-visual 输入：模板库驱动，几何输入=母版固定遮罩。"""

    plan_master_artifact_id: str
    template_id: str
    render_tier: RenderTier = "preview"


class RealismPassRequest(BaseModel):
    """realism-pass 输入：生成式写实化（工厂效果图同用）。"""

    base_render_artifact_id: str
    style_ref: str
    render_tier: RenderTier = "preview"
