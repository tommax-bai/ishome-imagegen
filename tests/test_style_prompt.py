"""提示词红线：房间表必须进去、文字层那句默认是"不出字"、同样输入拼出同样一段话；
注释与槽位是数据——不给时既有两档模板拼出的话逐字节不变。"""

from __future__ import annotations

import pytest

from imagegen_worker.models import LifeObjectSlot, RoomAnnotation, RoomSlot, StyleTemplate
from imagegen_worker.style_prompt import build_prompt, point_in_plan, where_in_plan

_TEMPLATE = StyleTemplate(
    template_id="t-test",
    style="warm cream-pink hand-drawn journal",
    composition="strict orthographic top-down 2D",
    negatives=["perspective", "photographic rendering"],
)


def _room(name: str, x: int, y: int) -> RoomSlot:
    return RoomSlot(name=name, mask_index=1, anchor_x_px=x, anchor_y_px=y)


def test_anchor_becomes_a_position_word() -> None:
    # 方位是算出来的不是看图说的：锚点在图幅里的相对坐标 → 九档
    assert where_in_plan(_room("厨房", 50, 50), 900, 900) == "upper-left"
    assert where_in_plan(_room("客厅", 450, 450), 900, 900) == "middle-center"
    assert where_in_plan(_room("次卧", 850, 850), 900, 900) == "lower-right"


def test_room_table_goes_into_the_prompt() -> None:
    """不给房间表，模型只能靠家具猜功能——首跑实测厨房被安到了次卧的位置。"""
    prompt = build_prompt(_TEMPLATE, [_room("厨房", 450, 100), _room("主卧", 100, 800)], 900, 900)

    assert "upper-center is the 厨房" in prompt
    assert "lower-left is the 主卧" in prompt


def test_no_text_is_the_default() -> None:
    # 字由确定性排版层叠：图像模型逐条排版乱码率相乘，且改一句文案就得重生成整图。
    # **不写 roomLabels 就是不出字**——既有模板一份都不改，行为一个字都不变
    assert _TEMPLATE.room_labels == "none"
    prompt = build_prompt(_TEMPLATE, [_room("客厅", 450, 450)], 900, 900)

    assert "no Chinese characters" in prompt
    assert "Render NO text" in prompt


def test_handwritten_labels_replace_the_no_text_clause() -> None:
    """实验档（未裁决）：房间名交给模型手写。**名字是我们给的，不是它猜的**。"""
    template = _TEMPLATE.model_copy(update={"room_labels": "handwritten"})

    prompt = build_prompt(template, [_room("客厅", 450, 450), _room("卫生间", 100, 100)], 900, 900)

    assert "Render NO text" not in prompt
    assert "Hand-write each room's Chinese name" in prompt
    # 逐字复述那一份名单，顺序与房间表一致，且每个名字钉着自己的坐标
    assert "客厅 at <point>500 500</point>、卫生间 at <point>111 111</point>" in prompt


def test_handwritten_falls_back_to_no_text_without_a_room_table() -> None:
    """名字都拿不到就谈不上让它照着写——退回不出字，绝不让它自己编几个字写上去。"""
    template = _TEMPLATE.model_copy(update={"room_labels": "handwritten"})

    prompt = build_prompt(template, [], 900, 900)

    assert "Render NO text" in prompt
    assert "Hand-write" not in prompt


def test_geometry_source_clause_always_present() -> None:
    # 每张图独立回读母版是架构约束：用上一张继续生成下一张，户型会一路漂
    prompt = build_prompt(_TEMPLATE, [_room("客厅", 450, 450)], 900, 900)

    assert "ONLY architectural geometry source" in prompt
    assert "Do not change the outer silhouette" in prompt


def test_same_input_builds_the_same_prompt() -> None:
    """提示词自己得先是确定的，才谈得上比较两次生成的差别来自模型还是来自我们。"""
    rooms = [_room("客厅", 450, 450), _room("厨房", 450, 100)]

    assert build_prompt(_TEMPLATE, rooms, 900, 900) == build_prompt(_TEMPLATE, rooms, 900, 900)


def test_negatives_are_listed_one_by_one() -> None:
    prompt = build_prompt(_TEMPLATE, [_room("客厅", 450, 450)], 900, 900)

    assert "Avoid: perspective" in prompt
    assert "Avoid: photographic rendering" in prompt


def test_template_rejects_unknown_fields() -> None:
    # 模板是数据；多出来的字段说明模板与代码对不上，当场拒收
    with pytest.raises(ValueError, match="extra"):
        StyleTemplate.model_validate(
            {"templateId": "t", "style": "s", "composition": "c", "roomTable": ["客厅"]}
        )


def test_without_new_data_the_prompt_is_byte_identical_to_before() -> None:
    """**既有派发行为一个字节不变**：不给注释不给槽位，零字档拼出的话与加字段前逐字相同。

    这里把旧句子钉成字面——"and nothing else" 那半句与不带坐标的房间表是加槽位前的原话，
    谁改动了无槽位那条路，这里当场红。
    """
    prompt = build_prompt(_TEMPLATE, [_room("厨房", 450, 100), _room("主卧", 100, 800)], 900, 900)

    assert (
        "Room allocation (follow it exactly; draw furniture appropriate to each room's "
        "function and nothing else): upper-center is the 厨房; lower-left is the 主卧." in prompt
    )
    assert "<point>" not in prompt  # 坐标只属写字那档：零字档的提示词里一处都不出现
    assert "Everyday-life objects" not in prompt


def test_anchor_becomes_a_native_point() -> None:
    """归一化坐标：[0, 999]，图宽高各切 1000 份（出图模型评估实测的原生接口）。"""
    assert point_in_plan(_room("客厅", 689, 1103), 1320, 1600) == "<point>522 689</point>"
    # 贴边取值收在 [0, 999] 里，不越界
    assert point_in_plan(_room("阳台", 1320, 1600), 1320, 1600) == "<point>999 999</point>"
    assert point_in_plan(_room("玄关", 0, 0), 1320, 1600) == "<point>0 0</point>"


def test_handwritten_pins_each_room_name_with_a_point() -> None:
    """房间名的位置用坐标钉：评估里"餐厅"字形全对却叠到客厅上——字靠坐标不靠模型认位置。"""
    template = _TEMPLATE.model_copy(update={"room_labels": "handwritten"})

    prompt = build_prompt(template, [_room("客厅", 450, 450), _room("餐厅", 100, 100)], 900, 900)

    assert "客厅 at <point>500 500</point>" in prompt
    assert "餐厅 at <point>111 111</point>" in prompt
    assert "middle-center is the 客厅 at <point>500 500</point>" in prompt  # 房间表也带坐标
    # 坐标口径那一句只在用到坐标时出现
    assert "x runs 0-999" in prompt


def test_life_object_slots_are_data_not_template_wording() -> None:
    """槽位进提示词：列了的房间画清单上的物件，没列的退回中性画法——模型不许猜生活需求。"""
    slots = [LifeObjectSlot(room="小孩房", objects=["low toy shelves", "a small study desk"])]

    prompt = build_prompt(
        _TEMPLATE,
        [_room("小孩房", 100, 100), _room("主卧", 800, 800)],
        900,
        900,
        life_object_slots=slots,
    )

    assert "- 小孩房: low toy shelves; a small study desk" in prompt
    assert "plus ONLY the everyday-life objects listed below" in prompt
    assert "a room that is not listed gets only furniture appropriate to its function" in prompt
    # 换掉的是那半句禁令，不是整条房间表
    assert "function and nothing else): " not in prompt


def test_annotations_go_in_with_exact_text_and_a_point() -> None:
    """注释内容我们给、位置钉在落点房间的锚点上，模型只负责把字画上去。"""
    template = _TEMPLATE.model_copy(update={"room_labels": "handwritten"})
    notes = [RoomAnnotation(room="阳台", text="洗完抬手就晾上")]

    prompt = build_prompt(
        template, [_room("阳台", 450, 810), _room("客厅", 450, 450)], 900, 900, annotations=notes
    )

    assert "- in the 阳台 near <point>500 900</point>: 洗完抬手就晾上" in prompt
    assert "Write EXACTLY the characters given for each note" in prompt


def test_handwritten_forbids_any_text_beyond_the_given() -> None:
    """写我们给的可以、自由发挥不行——上一轮模型自造注释箭头指错对象，这一句是收口。"""
    template = _TEMPLATE.model_copy(update={"room_labels": "handwritten"})

    prompt = build_prompt(template, [_room("客厅", 450, 450)], 900, 900)

    assert "except the room names and the notes given above" in prompt
    assert "Render NO text" not in prompt  # 两句禁令不同场：收口句与零字句不共存


def test_annotation_text_rejects_digits() -> None:
    """注释文本一律不含数字：数字上图走叠印那条线（未建），不让模型画数字。"""
    with pytest.raises(ValueError, match="数字"):
        RoomAnnotation(room="客厅", text="占了 19% 的面积")


def test_annotation_text_rejects_blank() -> None:
    with pytest.raises(ValueError, match="空"):
        RoomAnnotation(room="客厅", text="   ")


def test_life_object_slot_rejects_empty_or_bloated_lists() -> None:
    """槽位 1~3 样：0 样叫没给槽位，多了画面密成分析表。"""
    with pytest.raises(ValueError):
        LifeObjectSlot(room="客厅", objects=[])
    with pytest.raises(ValueError):
        LifeObjectSlot(room="客厅", objects=["a", "b", "c", "d"])
    with pytest.raises(ValueError, match="空白物件"):
        LifeObjectSlot(room="客厅", objects=["a sofa", " "])
