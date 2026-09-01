"""模板 + 房间槽位 → 提示词。**纯函数，不做 IO**，单测不需要网关。

模板与实例分开（《方案/第一阶段视觉提案与Prompt说明》§12-1）：模板本体只有风格、构图与禁令，
户型专属的那部分——**哪一间在哪儿、画什么生活物件、写什么注释**——由母版与派发数据填进去。

**为什么房间表非填不可**：首跑把母版原样丢给模型、不给房间表，出来的图厨房跑到了次卧的位置。
母版上没有字（它不写字是刻意的），模型只能靠家具猜功能，猜错是必然不是偶然。

**为什么生活物件也由数据说**（2026-09-01）：模型不许猜生活需求——上一轮"draw furniture …
and nothing else"把生活物件全禁了，图寡淡；而放开让模型自由发挥，它画出来的是猫窝与智能音箱
（谁家有猫它不知道）。该画什么从户型事实与家庭结构假设推，作为槽位数据传进来。

**清单语义＝全集**（2026-09-01 晚，真跑定罪后改）：先前清单只装生活物件、功能家具靠模型按
房间名补，实测清单被当成"近似全集"——小孩房给了玩具架与书桌，两跑都没画床，而注释里恰写着
"床和书桌各归各位"。现在每间房的清单列全（功能家具＋生活物件），口径＝每间房画且只画清单里
的；"没列的房间只画功能家具"那半句随之取消——槽位给了就得给全（收货门禁在 `atmosphere`），
混合形态（有的房间有清单、有的靠模型补）正是中性回退下限不稳的来处。
"""

from __future__ import annotations

from collections.abc import Sequence

from imagegen_worker.models import LifeObjectSlot, RoomAnnotation, RoomSlot, StyleTemplate

_GEOMETRY_CLAUSE = (
    "Input image 1 is the ONLY architectural geometry source. Preserve its exact "
    "floor-plan outer silhouette, wall positions and thicknesses, room boundaries, "
    "door openings and window openings. Do not move, add or remove any wall, door, "
    "window or room. Do not change the outer silhouette."
)
"""几何唯一源那一句。**每张图独立回读母版**是架构约束不是生成技巧——用上一张继续生成下一张，
户型会一路漂（《方案》§3 必须避免的最后一条）。"""

_NO_TEXT_CLAUSE = (
    "Render NO text of any kind: no letters, no Chinese characters, no numbers, "
    "no labels, no captions, no watermarks."
)
"""不出字。字由确定性排版层叠——图像模型逐条排版的乱码率相乘，且改一句文案就得重生成整图。"""

_COORDINATE_CONVENTION_CLAUSE = (
    "Coordinates in this prompt are written as <point>x y</point> in the frame of "
    "input image 1: x runs 0-999 left to right, y runs 0-999 top to bottom, the "
    "image width and height each divided into 1000 equal units."
)
"""坐标口径那一句，只在真用到坐标（写字那档）时出现。`<point>`/`<bbox>` 是出图模型的原生
归一化坐标接口（出图模型评估实测，《交接文档-三张图出得来》追记三第三节）：另一跑里"餐厅"
二字字形全对却叠到客厅上——**可靠的不是模型认字，是坐标**，串位靠坐标治。"""

_HANDWRITTEN_LABELS_CLAUSE = (
    "Hand-write each room's Chinese name inside that room, centered on the coordinate "
    "given for it, using EXACTLY the characters given for that room — no substitutes, "
    "no simplification, no extra characters in the name: {names}. Black handwritten "
    "marker, size and angle varying slightly between rooms, no printed typeface, "
    "no label box, no underline, no pinyin, no English translation."
)
"""**实验档**（`roomLabels: handwritten`，2026-09-01；未裁决，不是新口径）：模型自己写房间名。

名字逐字再复述一遍是刻意的——名字是**我们的数据**（母版那份房间表），不是让模型看家具猜。
每个名字带自己的坐标：字形对、位置串（"餐厅"叠到客厅）那次实测之后，位置也不让它猜。"""

_ANNOTATIONS_CLAUSE = (
    "Also hand-write these owner's notes, each inside the room named for it, near the "
    "given coordinate, in a smaller and more casual handwriting than the room names, "
    "each joined by a short quickly-drawn arrow to the object in that room it talks "
    "about. Write EXACTLY the characters given for each note — no substitutes, "
    "no additions, no rewording:"
)
"""注释那一段（用户裁决 2026-09-01：注释要有，内容我们给）。**箭头指它说的那样东西**——
上一轮模型自造注释时两处箭头指错对象（"智能音箱"指宠物垫），这次注释与槽位同源，
它说的东西就在那间房的物件清单里。"""

_ONLY_GIVEN_TEXT_CLAUSE = (
    "Write NO text anywhere in the image except the room names and the notes given "
    "above: no invented notes or labels, no numbers, no English words, no watermarks, "
    "no title."
)
"""写字那档的收口：**写我们给的可以、自由发挥不行**（上一轮真跑量出来的界线——我们给的
20 字零错，模型自造的那批出了重复涂鸦与不成字的淡笔迹）。"""

# 九档方位：锚点落在图幅的哪一格。**算出来的，不是看图说的**。
_VERTICAL_BANDS = (("upper", 1 / 3), ("middle", 2 / 3), ("lower", 1.0))
_HORIZONTAL_BANDS = (("left", 1 / 3), ("center", 2 / 3), ("right", 1.0))


def _band(value: float, bands: tuple[tuple[str, float], ...]) -> str:
    for name, upper in bands:
        if value <= upper:
            return name
    return bands[-1][0]


def where_in_plan(room: RoomSlot, width_px: int, height_px: int) -> str:
    """房间在图上的九档方位（upper-left / middle-center / lower-right …）。"""
    vertical = _band(room.anchor_y_px / max(height_px, 1), _VERTICAL_BANDS)
    horizontal = _band(room.anchor_x_px / max(width_px, 1), _HORIZONTAL_BANDS)
    return f"{vertical}-{horizontal}"


def point_in_plan(room: RoomSlot, width_px: int, height_px: int) -> str:
    """房间锚点的模型原生归一化坐标：`<point>x y</point>`，[0, 999]、图宽高各切 1000 份。

    与九档方位同源（同一个锚点），精度不同：九档在这张母版上实测撞格（客厅与阳台都算成
    lower-center），坐标不撞。写字那档用它钉字的位置；方位词仍保留给模型当冗余的语义校验。
    """
    x = min(999, max(0, round(room.anchor_x_px * 1000 / max(width_px, 1))))
    y = min(999, max(0, round(room.anchor_y_px * 1000 / max(height_px, 1))))
    return f"<point>{x} {y}</point>"


def build_prompt(
    template: StyleTemplate,
    rooms: list[RoomSlot],
    width_px: int,
    height_px: int,
    *,
    annotations: Sequence[RoomAnnotation] = (),
    life_object_slots: Sequence[LifeObjectSlot] = (),
) -> str:
    """拼出发给图像模型的那段话。顺序固定：几何 → 风格 → 构图 → 坐标口径 → 房间表 →
    生活物件槽位 → 文字层 → 禁令。

    顺序固定是为了**同样的输入拼出同样的一段话**——提示词自己得先是确定的，
    才谈得上比较两次生成的差别来自模型还是来自我们。

    **不给注释、不给槽位时，两档模板拼出的话与从前逐字节相同**——既有派发行为不变。
    注释只有写字那档（`roomLabels: handwritten`）拼得进去；给零字模板递注释的口径冲突
    在 `atmosphere` 那层当场拒收，本函数只按写字档拼（纯函数不做门禁）。
    槽位同理按"每间房都有一份全集清单"拼——给了却没给全在 `atmosphere` 拒收，
    本函数不重复判，也不再为"没清单的房间"拼中性回退句。

    **文字层只有两种收口**：写字档＝房间名＋注释＋"给的以外一个字不写"；零字档＝一个字不出。
    没有房间表时两档都退回"不出字"——名字都拿不到，更谈不上让模型照着写。
    """
    handwritten = bool(rooms) and template.room_labels == "handwritten"
    lines = [
        "Use case: style-transfer of an architectural floor plan.",
        _GEOMETRY_CLAUSE,
        f"Style: {template.style}",
        f"Composition: {template.composition}",
    ]
    if handwritten:
        lines.append(_COORDINATE_CONVENTION_CLAUSE)
    if rooms:
        # 房间表按方位念，不按遮罩索引念——索引是内部编号，对模型没有意义。
        # 写字那档每间再带坐标：方位词是语义校验，坐标才是钉位置的（九档实测撞格）。
        listed = "; ".join(
            f"{where_in_plan(room, width_px, height_px)} is the {room.name}"
            + (f" at {point_in_plan(room, width_px, height_px)}" if handwritten else "")
            for room in rooms
        )
        furniture_rule = (
            "each room's complete contents are listed below; draw in each room exactly "
            "its list, nothing more"
            if life_object_slots
            else "draw furniture appropriate to each room's function and nothing else"
        )
        lines.append(f"Room allocation (follow it exactly; {furniture_rule}): {listed}.")
    if rooms and life_object_slots:
        # 清单＝全集（功能家具也在里面），且收货门禁保证每间房都有一份——
        # "没列的房间只画功能家具"那半句因此取消：混合形态（有清单的照单画、没清单的
        # 让模型按功能补）正是上一轮"卫生间近乎空房"与"小孩房没床"两处失守的来处。
        lines.append(
            "Contents of each room (each list is the COMPLETE inventory for that room, "
            "functional furniture included: draw EVERY object listed for the room — a bed, "
            "a table or a toilet in a list must appear in the drawing — and draw NOTHING "
            "in a room that its list does not name; do not invent pets, hobbies, "
            "appliances or gadgets):"
        )
        lines.extend(f"- {slot.room}: {'; '.join(slot.objects)}" for slot in life_object_slots)
    if handwritten:
        by_name = {room.name: room for room in rooms}
        lines.append(
            _HANDWRITTEN_LABELS_CLAUSE.format(
                names="、".join(
                    f"{room.name} at {point_in_plan(room, width_px, height_px)}" for room in rooms
                )
            )
        )
        if annotations:
            lines.append(_ANNOTATIONS_CLAUSE)
            lines.extend(
                f"- in the {note.room} near "
                f"{point_in_plan(by_name[note.room], width_px, height_px)}: {note.text}"
                for note in annotations
            )
        lines.append(_ONLY_GIVEN_TEXT_CLAUSE)
    else:
        lines.append(_NO_TEXT_CLAUSE)
    lines.extend(f"Avoid: {item}" for item in template.negatives)
    return "\n".join(lines)
