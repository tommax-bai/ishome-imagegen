"""风格图编排红线：没有房间表不出图；网关说输入图没送到即整张失败；仓里那批模板装得上。"""

from __future__ import annotations

from pathlib import Path

import pytest

from imagegen_worker import atmosphere, image_gateway
from imagegen_worker.image_store import check_template_id
from imagegen_worker.models import LifeObjectSlot, RoomAnnotation, RoomSlot, StyleTemplate

_TEMPLATE = StyleTemplate(template_id="t", style="s", composition="c")
_ROOMS = [RoomSlot(name="客厅", mask_index=1, anchor_x_px=100, anchor_y_px=100)]

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def test_no_room_table_fails_loud() -> None:
    with pytest.raises(atmosphere.AtmosphereError, match="房间表是空的"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=[],
            template=_TEMPLATE,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
        )


def test_gateway_failure_surfaces_as_atmosphere_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """图生图静默退化成文生图是这条线上最贵的失败：调用成功、图与母版无关。"""

    def _boom(**_: object) -> tuple[bytes, str]:
        raise image_gateway.ImageGatewayError(["输入图没送到模型（回执 input_images=None）"])

    monkeypatch.setattr(image_gateway, "generate_from_image", _boom)

    with pytest.raises(atmosphere.AtmosphereError, match="输入图没送到模型"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=_ROOMS,
            template=_TEMPLATE,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
        )


def test_empty_master_never_reaches_the_model() -> None:
    with pytest.raises(image_gateway.ImageGatewayError, match="源图是空的"):
        image_gateway.generate_from_image(
            model="m", prompt="p", source_png=b"", size="2K", api_key="k"
        )


@pytest.mark.parametrize("path", sorted(_TEMPLATES_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_shipped_templates_load(path: Path) -> None:
    """仓里那批模板逐份装得上。**worker 起进程时判的三件，这里提前判**——
    改坏一个字段要在提交前就红，不是等部署现场起不来。
    """
    template = atmosphere.load_template(path)

    assert template.template_id == path.stem  # 产物的对象键里带的是 id 不是文件名
    check_template_id(template.template_id)  # id 当得了对象键的一段
    assert template.style and template.composition


def test_annotations_on_a_no_text_template_fail_loud() -> None:
    """零字模板收不下注释：「不出字」与「写这些字」不许在同一份提示词里打架。"""
    with pytest.raises(atmosphere.AtmosphereError, match="零字档"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=_ROOMS,
            template=_TEMPLATE,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
            annotations=[RoomAnnotation(room="客厅", text="朋友来了也坐得开")],
        )


def test_annotation_on_an_unknown_room_fails_loud() -> None:
    """注释挂在房间表里没有的房间上＝没有锚点可钉，当场失败不猜。"""
    handwritten = _TEMPLATE.model_copy(update={"room_labels": "handwritten"})

    with pytest.raises(atmosphere.AtmosphereError, match="书房"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=_ROOMS,
            template=handwritten,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
            annotations=[RoomAnnotation(room="书房", text="想看书就躲进来")],
        )


def test_slot_on_an_unknown_room_fails_loud() -> None:
    """槽位挂错房间＝两侧口径对不上，静默丢的代价是"派发方以为交代了、执行方没收到"。"""
    with pytest.raises(atmosphere.AtmosphereError, match="茶室"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=_ROOMS,
            template=_TEMPLATE,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
            life_object_slots=[LifeObjectSlot(room="茶室", objects=["a tea table"])],
        )


def test_experiment_template_asks_for_explicit_pixels() -> None:
    """实验模板的 size 是显式像素尺寸不是 "2K"：出图模型评估实测 "2K" 的长宽比由模型自己挑
    （同参数回来三种比例），显式尺寸才守得住比例与留白带。cream/pencil 不动（降档没拍）。"""
    template = atmosphere.load_template(_TEMPLATES_DIR / "lifestyle-notebook-handwritten.json")

    width, sep, height = template.size.partition("x")
    assert sep == "x" and width.isdigit() and height.isdigit(), template.size


def test_only_the_experiment_template_writes_its_own_text() -> None:
    """**红线"图形层不含文字"没松**：仓里只有那一份写字模板走 `handwritten`。

    这一条不是纪律是门禁——谁给别的模板加上这个字段、或者把默认改掉，这里当场红。
    用户裁决 2026-09-01 上午把这份模板拍成免费第三张（手账·写字版＋注释），
    裁决只到这一份——写字的仍只许有一份。
    """
    writes_text = sorted(
        path.stem
        for path in _TEMPLATES_DIR.glob("*.json")
        if atmosphere.load_template(path).room_labels == "handwritten"
    )

    assert writes_text == ["lifestyle-notebook-handwritten"]
