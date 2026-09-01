"""imagegen_worker activity 出入参模型（pydantic）。

跨 domain 纪律：worker 不 import 其他 domain 的内部模块，activity 入参出参
以本模块与（后续）contracts 生成 SDK 为准。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

RenderTier = Literal["preview", "final"]
"""渲染两档：失效传播默认只重算 preview，final 由用户显式请求或交付节点触发。"""

RoomLabels = Literal["none", "handwritten"]
"""房间名由谁写。**本仓唯一一处"文字层归谁"的开关**，两档的差别只在提示词文字层那几句。

- `none`——图里一个字都不出，房间名由确定性排版层叠上去（**默认口径**，红线"图形层不含文字"）；
- `handwritten`——把房间名（与注释）交给图像模型手写进图里。**用户裁决 2026-09-01 上午**：
  免费第三张风格图＝手账·写字版（`lifestyle-notebook-handwritten`），图上要有注释、
  注释内容我们给。裁决只到这一份模板——写字的仍只许一份（门禁
  `test_only_the_experiment_template_writes_its_own_text` 守着），红线对其余模板未松。

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


class RoomAnnotation(BaseModel):
    """一条要写上图的注释：挂在哪间房、写什么字。**内容我们给，模型只负责把字画上去**
    （用户裁决 2026-09-01：注释要有，且不能由模型临时编——上一轮模型自造的四条注释两处
    箭头指错对象，被推翻的是"先关注释"，没被推翻的是"不许模型自造"）。

    内容由派发方从**户型事实与家庭结构假设**改写而来（同批注那条线的口径：每句引得到事实），
    位置不在这里带——落点房间的锚点在房间表里，提示词层把它换算成模型原生的归一化坐标钉上去。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    room: str
    """落点房间，必须逐字取自房间表——挂在不认识的房间上就没有锚点可钉，当场失败不猜。"""

    text: str
    """写上图的那句话。**一律不含数字**（数字上图走叠印那条线，未建；红线"数字不由 LLM 决定"
    的精神——让模型画数字，画错一个字就是错一个数）。"""

    @field_validator("text")
    @classmethod
    def _text_carries_no_digits(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("注释文本是空的：空注释画上去是一根指着空气的箭头")
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            raise ValueError(
                f"注释文本不许带数字（发现 {digits}）：数字上图走叠印那条线，不让模型画数字"
            )
        return value


class LifeObjectSlot(BaseModel):
    """一间房该画的生活物件槽位：房间 + 该出现的物件清单（1~3 样，任务口径）。

    **模型不许猜生活需求**（用户目标 Demo 复盘的硬约束）：房间的用途和该有的东西由数据说，
    模型只负责画。内容从**户型事实与家庭结构假设**推，由派发方传入——不是模板措辞、也不是
    代码写死（照房间名/注释的先例：数据形状进本模型，派发方以后传）。没给槽位的房间退回
    中性画法（只画功能家具），不许模型自由发挥补脑。

    同一份槽位数据将来要喂给**所有画风模板**——三张图家具一致性的底子，所以它挂在请求上、
    不挂在模板上。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    room: str
    """哪间房，必须逐字取自房间表。"""

    objects: list[str] = Field(min_length=1, max_length=3)
    """该画的生活物件，每样一条（英文短语，进提示词当画画指令，不是要写上图的字）。
    1~3 样是任务口径：0 样的槽位没有意义（那叫没给槽位），多了画面就密成分析表。"""

    @field_validator("objects")
    @classmethod
    def _objects_are_not_blank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("槽位里有空白物件：空字符串画不出东西，是数据没拼好")
        return value


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
    `handwritten` 档 2026-09-01 上午被用户拍为免费第三张（手账·写字版＋注释），
    仍只许一份模板走它；错字率照真跑逐字比对记账，不凭印象。"""

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

    annotations: list[RoomAnnotation] = Field(default_factory=list)
    """要写上图的注释（见 `RoomAnnotation`）。**默认空**＝不写注释，既有派发一个字段不加、
    行为一个字节不变。只有写字那档模板（`roomLabels: handwritten`）收得下它——
    给零字模板递注释是两侧口径对不上，当场失败不静默丢（丢＝派发方以为交代了、执行方没收到）。"""

    life_object_slots: list[LifeObjectSlot] = Field(default_factory=list)
    """逐间生活物件槽位（见 `LifeObjectSlot`）。**默认空**＝整张退回中性画法（每间只画功能
    家具，别的不画），既有派发行为不变。所有画风模板都吃它——家具一致性的底子。"""


class RealismPassRequest(BaseModel):
    """realism-pass 输入：生成式写实化（工厂效果图同用）。"""

    base_render_artifact_id: str
    style_ref: str
    render_tier: RenderTier = "preview"
