"""风格图 activity：正路径 + 门禁路径，以及"写不进桶不许当成功"。

**桶与网关都用桩件**：这一层要验的是"图出来之后往哪走、走不通怎么办"；真桶与真网关验的是
凭证、网络与模型对不对——那件事由真跑留档，不是单测的题目。

两道**真跑逼出来的门禁**在这里各有一条（它们的规则本体在 `atmosphere` / `image_gateway`，
本文件验的是"接进 activity 之后仍然有效"）：房间表非填不可、网关回执 `input_images` 不是 1
就整张失败。
"""

from __future__ import annotations

import json
import struct
from typing import Any

import pytest

from imagegen_worker import image_gateway
from imagegen_worker.activities import ACTIVITY_ATMOSPHERE_VISUAL, AtmosphereVisualGenerator
from imagegen_worker.image_store import ImageStoreError, atmosphere_visual_key_of
from imagegen_worker.models import StyleTemplate

_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_MASTER_KEY = f"uploads/{_SHA}/plan-master.png"
_ROOM_ANCHORS_KEY = f"uploads/{_SHA}/plan-rooms.json"
_TEMPLATE_ID = "cream-journal"
# 真跑实测：网关回来的是 JPEG（而键的后缀是 .png——键是协议不是格式断言）
_IMAGE_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64

_TEMPLATE = StyleTemplate(
    template_id=_TEMPLATE_ID,
    style="warm cream-pink hand-drawn journal",
    composition="strict orthographic top-down 2D",
    negatives=["perspective"],
)
_MASTER_WIDTH_PX = 900
_MASTER_HEIGHT_PX = 1600
_ROOMS = [
    # 方位是按图幅算出来的：900×1600 的母版上，(450, 800) 落在正中、(100, 100) 落在左上
    {"name": "客厅", "mask_index": 1, "anchor_x_px": 450, "anchor_y_px": 800},
    {"name": "厨房", "mask_index": 2, "anchor_x_px": 100, "anchor_y_px": 100},
]


def _png(width_px: int, height_px: int) -> bytes:
    """一份够读出图幅尺寸的 PNG 头（本层只从头里读宽高，不解码像素）。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width_px, height_px)
        + b"\x08\x06\x00\x00\x00"
    )


class _StubImageStore:
    """桩件私有桶：给出母版与房间表，记下写了什么，或按需当场失败。"""

    def __init__(
        self,
        *,
        master_png: bytes | None = None,
        room_anchors: list[dict[str, Any]] | None = None,
        get_fails_with: str | None = None,
        put_fails_with: str | None = None,
    ) -> None:
        self.written: dict[str, bytes] = {}
        self._master_png = (
            _png(_MASTER_WIDTH_PX, _MASTER_HEIGHT_PX) if master_png is None else master_png
        )
        self._room_anchors = _ROOMS if room_anchors is None else room_anchors
        self._get_fails_with = get_fails_with
        self._put_fails_with = put_fails_with

    @property
    def bucket_name(self) -> str:
        return "ishome-test"

    def get_master(self, master_object_key: str) -> bytes:
        if self._get_fails_with is not None:
            raise ImageStoreError([self._get_fails_with])
        return self._master_png

    def get_room_anchors(self, master_object_key: str, room_anchors_object_key: str) -> bytes:
        if self._get_fails_with is not None:
            raise ImageStoreError([self._get_fails_with])
        return json.dumps(self._room_anchors, ensure_ascii=False).encode("utf-8")

    def put_atmosphere_visual(
        self, master_object_key: str, template_id: str, image_png: bytes
    ) -> str:
        if self._put_fails_with is not None:
            raise ImageStoreError([self._put_fails_with])
        key = atmosphere_visual_key_of(master_object_key, template_id)
        self.written[key] = image_png
        return key


def _generator(store: Any) -> AtmosphereVisualGenerator:
    return AtmosphereVisualGenerator(store, {_TEMPLATE_ID: _TEMPLATE}, "k", "http://gateway.test")


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "master_object_key": _MASTER_KEY,
        "room_anchors_object_key": _ROOM_ANCHORS_KEY,
        "template_id": _TEMPLATE_ID,
    }
    request.update(overrides)
    return request


@pytest.fixture
def gateway_returns_an_image(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """网关桩件：记下真正发出去的那次调用，回一张图。"""
    calls: list[dict[str, Any]] = []

    def _generate(**kwargs: Any) -> tuple[bytes, str]:
        calls.append(kwargs)
        return _IMAGE_BYTES, "revised"

    monkeypatch.setattr(image_gateway, "generate_from_image", _generate)
    return calls


async def test_generates_and_writes_the_visual(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    store = _StubImageStore()
    result = await _generator(store).generate_atmosphere_visual(_request())

    assert result["verdict"] == "ok"
    assert result["image_object_key"] == f"uploads/{_SHA}/atmosphere-{_TEMPLATE_ID}.png"
    assert result["room_count"] == 2
    assert result["image_size_bytes"] == len(_IMAGE_BYTES)
    # 头按字节写：键说 .png，字节是 JPEG，业主点开要看到图不是一屏乱码
    assert result["content_type"] == "image/jpeg"
    # 图确实落进了存储，落的就是回报的那个键——不是回一个指向空气的键。
    assert store.written[result["image_object_key"]] == _IMAGE_BYTES
    # 母版是几何唯一源：发出去的源图就是从桶里取的那一份。
    assert gateway_returns_an_image[0]["source_png"] == _png(_MASTER_WIDTH_PX, _MASTER_HEIGHT_PX)
    # 逻辑模型名，不是厂商模型：换模型是改网关配置，不是改这里。
    assert gateway_returns_an_image[0]["model"] == "atmosphere-visual.default"


async def test_room_table_reaches_the_model(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    """房间表非填不可，且方位是**从桶里那份锚点算出来的**，不是看图说的。"""
    result = await _generator(_StubImageStore()).generate_atmosphere_visual(_request())

    assert result["verdict"] == "ok"
    prompt = gateway_returns_an_image[0]["prompt"]
    assert "middle-center is the 客厅" in prompt
    assert "upper-left is the 厨房" in prompt
    assert prompt == result["prompt"]  # 提示词随回执留档，与发出去的那段逐字相同


async def test_annotations_and_slots_flow_from_request_into_the_prompt(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    """注释与槽位是**派发数据**：从请求进来、经模型校验、拼进提示词——不是模板措辞。"""
    handwritten = _TEMPLATE.model_copy(
        update={"template_id": "note-exp", "room_labels": "handwritten"}
    )
    store: Any = _StubImageStore()
    generator = AtmosphereVisualGenerator(
        store, {"note-exp": handwritten}, "k", "http://gateway.test"
    )
    request = _request(
        template_id="note-exp",
        annotations=[{"room": "客厅", "text": "朋友来了也坐得开"}],
        # 全集口径：槽位给了就得给全——房间表里两间，清单也得两间（缺一间当场 failed）
        life_object_slots=[
            {"room": "客厅", "objects": ["a large sofa"]},
            {"room": "厨房", "objects": ["jars along the counter"]},
        ],
    )

    result = await generator.generate_atmosphere_visual(request)

    assert result["verdict"] == "ok"
    prompt = gateway_returns_an_image[0]["prompt"]
    assert "朋友来了也坐得开" in prompt
    assert "- 客厅: a large sofa" in prompt
    assert "- 厨房: jars along the counter" in prompt


async def test_annotations_with_a_no_text_template_come_back_as_failed(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    """给零字模板递注释＝两侧口径冲突：按 failed 逐条回报，一次钱都不花。"""
    request = _request(annotations=[{"room": "客厅", "text": "朋友来了也坐得开"}])

    result = await _generator(_StubImageStore()).generate_atmosphere_visual(request)

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["atmosphere-failed"]
    assert "零字档" in result["violations"][0]["detail"]
    assert gateway_returns_an_image == []


async def test_empty_room_table_never_reaches_the_model(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    """不给房间表，模型只能靠家具猜功能——首跑实测厨房跑到了次卧的位置。"""
    result = await _generator(_StubImageStore(room_anchors=[])).generate_atmosphere_visual(
        _request()
    )

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["atmosphere-failed"]
    assert "房间表是空的" in result["violations"][0]["detail"]
    assert gateway_returns_an_image == []  # 一次钱都没花


async def test_input_images_not_one_fails_the_whole_visual(monkeypatch: pytest.MonkeyPatch) -> None:
    """图生图静默退化成文生图是这条线上最贵的失败：调用成功、图与母版无关。"""

    def _degraded(**_: Any) -> tuple[bytes, str]:
        raise image_gateway.ImageGatewayError(["输入图没送到模型（回执 input_images=None）"])

    monkeypatch.setattr(image_gateway, "generate_from_image", _degraded)
    store = _StubImageStore()
    result = await _generator(store).generate_atmosphere_visual(_request())

    assert result["verdict"] == "failed"
    assert "输入图没送到模型" in result["violations"][0]["detail"]
    assert store.written == {}  # 退化出来的那张图一个字节也没进桶


async def test_store_failure_is_not_reported_as_success(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    """图出得再好、落不了地也不是 ok——下游会拿着那个键去发给业主。"""
    store = _StubImageStore(put_fails_with="桶不存在")
    result = await _generator(store).generate_atmosphere_visual(_request())

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["image-store-failed"]
    assert "桶不存在" in result["violations"][0]["detail"]


async def test_missing_master_never_spends_a_generation(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    store = _StubImageStore(get_fails_with="母版不在私有桶里")
    result = await _generator(store).generate_atmosphere_visual(_request())

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["image-store-failed"]
    assert gateway_returns_an_image == []


async def test_master_that_is_not_a_png_fails_before_the_model(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    """算不出图幅尺寸就换算不出房间方位——那时候的房间表是错的，不如不出。"""
    result = await _generator(_StubImageStore(master_png=b"not a png")).generate_atmosphere_visual(
        _request()
    )

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["gate-bad-master"]
    assert gateway_returns_an_image == []


async def test_unknown_template_says_what_is_installed() -> None:
    result = await _generator(_StubImageStore()).generate_atmosphere_visual(
        _request(template_id="no-such-template")
    )

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["gate-unknown-template"]
    assert _TEMPLATE_ID in result["violations"][0]["detail"]


@pytest.mark.parametrize(
    "request_payload",
    [
        {"room_anchors_object_key": _ROOM_ANCHORS_KEY, "template_id": _TEMPLATE_ID},
        {"master_object_key": _MASTER_KEY, "template_id": _TEMPLATE_ID},
        # 多出来的字段说明两侧口径对不上：静默丢掉它，等于"派发方以为交代了、执行方没收到"
        {**_request(), "render_tier": "final"},
    ],
)
async def test_input_that_does_not_match_the_agreed_shape_fails_loud(
    request_payload: dict[str, Any],
) -> None:
    result = await _generator(_StubImageStore()).generate_atmosphere_visual(request_payload)

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["gate-bad-input"]


async def test_room_anchors_json_that_is_not_a_room_table_fails_loud(
    gateway_returns_an_image: list[dict[str, Any]],
) -> None:
    store = _StubImageStore(room_anchors=[{"name": "客厅"}])
    result = await _generator(store).generate_atmosphere_visual(_request())

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["gate-bad-room-anchors"]
    assert gateway_returns_an_image == []


def test_activity_name_is_the_contracts_one() -> None:
    assert ACTIVITY_ATMOSPHERE_VISUAL == "atmosphere-visual"
