"""对象键守门：键是跨仓协议，两侧各持逐字副本——对不上就是接不上头，不是风格问题。

唯一真源：ishome-contracts `registries/object_keys.md`（母版这一批产物的前缀由该表
"派生物留了位置"那一段兜住；风格图这条键的形态由中控仓统一入表，本仓只持副本）。
"""

from __future__ import annotations

import pytest

from imagegen_worker.image_store import (
    ATMOSPHERE_VISUAL_KEY_TEMPLATE,
    MASTER_KEY_TEMPLATE,
    ImageStoreError,
    OssSettings,
    atmosphere_visual_key_of,
    check_shares_upload_prefix,
    check_template_id,
    content_sha256_of_master_key,
    image_content_type_of,
)

_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_MASTER_KEY = f"uploads/{_SHA}/plan-master.png"
_ROOM_ANCHORS_KEY = f"uploads/{_SHA}/plan-rooms.json"


def test_key_templates_are_verbatim() -> None:
    """逐字副本：改这两行就是改协议，改了要同步中控仓的注册表与另一侧。"""
    assert MASTER_KEY_TEMPLATE == "uploads/{content_sha256}/plan-master.png"
    assert (
        ATMOSPHERE_VISUAL_KEY_TEMPLATE == "uploads/{content_sha256}/atmosphere-{template_id}.{ext}"
    )


def test_atmosphere_key_lands_next_to_its_master_and_ext_follows_bytes() -> None:
    """产物与源图同前缀、由内容哈希确定性派生；**扩展名跟字节走**（用户裁决 2026-09-02
    "标签跟着内容走"）——网关回 JPEG 键就叫 `.jpg`，回 PNG 就叫 `.png`，与注册表
    `original.{ext}` 先例同口径。"""
    assert (
        atmosphere_visual_key_of(_MASTER_KEY, "cream-journal", b"\xff\xd8\xff\xe0JFIF...")
        == f"uploads/{_SHA}/atmosphere-cream-journal.jpg"
    )
    assert (
        atmosphere_visual_key_of(_MASTER_KEY, "cream-journal", b"\x89PNG\r\n\x1a\n....")
        == f"uploads/{_SHA}/atmosphere-cream-journal.png"
    )


def test_master_key_is_read_not_guessed() -> None:
    assert content_sha256_of_master_key(_MASTER_KEY) == _SHA


def test_content_type_follows_the_bytes() -> None:
    """头按字节写；裁决 2026-09-02 起键的扩展名与它同源同判，说的是同一件事。"""
    assert image_content_type_of(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert image_content_type_of(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"


def test_unknown_bytes_get_no_guessed_content_type() -> None:
    with pytest.raises(ImageStoreError, match="不是认得的图"):
        image_content_type_of(b"who knows what this is")


@pytest.mark.parametrize(
    "bad_key",
    [
        f"uploads/{_SHA}/original.png",  # 是源图不是母版
        f"reports/{_SHA}/plan-master.png",  # 换了桶里的地盘
        f"uploads/{_SHA.upper()}/plan-master.png",  # 大写哈希：同一份字节两个键
        "uploads/deadbeef/plan-master.png",  # 不是 64 位十六进制
        f"uploads/{_SHA}/nested/plan-master.png",
        "plan-master.png",
    ],
)
def test_master_key_shape_fails_loud(bad_key: str) -> None:
    """键不合约定就当场失败——**不猜、不修补、不换个地方写**。"""
    with pytest.raises(ImageStoreError, match="母版键不合约定形态"):
        content_sha256_of_master_key(bad_key)


@pytest.mark.parametrize("bad_id", ["cream/journal", "Cream-Journal", "cream_journal", ""])
def test_template_id_must_be_key_safe(bad_id: str) -> None:
    """模板 id 要当键的一段用：斜杠把键劈成两段，大小写在别的系统上不等价。"""
    with pytest.raises(ImageStoreError, match="模板 id 当不了对象键"):
        check_template_id(bad_id)


def test_room_anchors_must_come_from_the_same_upload() -> None:
    check_shares_upload_prefix(_MASTER_KEY, _ROOM_ANCHORS_KEY)


@pytest.mark.parametrize(
    "bad_key",
    [
        "uploads/" + "a" * 64 + "/plan-rooms.json",  # 别人家的房间表
        f"uploads/{_SHA}/sub/plan-rooms.json",
        "plan-rooms.json",
    ],
)
def test_room_anchors_from_another_upload_fails_loud(bad_key: str) -> None:
    """拿甲的房间表去画乙的户型，出来的图每一间功能都是错的，而它看上去完全正常。"""
    with pytest.raises(ImageStoreError, match="与母版不在同一份上传件底下"):
        check_shares_upload_prefix(_MASTER_KEY, bad_key)


def test_missing_credentials_name_the_missing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """起不来的原因要一眼读得懂：点名缺的是哪一个，不笼统说"配置不全"。"""
    for name in (
        "ISHOME_OSS_ENDPOINT",
        "ISHOME_OSS_BUCKET_PRIVATE",
        "ISHOME_OSS_ACCESS_KEY_ID",
        "ISHOME_OSS_ACCESS_KEY_SECRET",
    ):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("ISHOME_OSS_ACCESS_KEY_SECRET", "")

    with pytest.raises(ImageStoreError, match="ISHOME_OSS_ACCESS_KEY_SECRET"):
        OssSettings.from_env()
