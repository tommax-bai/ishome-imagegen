"""出站边缘：**私有对象存储**（阿里云 OSS 私有桶，用户裁决 2026-08-30 晚）。

本仓**既读又写**——与渲染层只写册不同：风格图的几何来源是母版，母版由 render2d 的
`plan-2d-render` 写进同一只私有桶，本仓按键把它取下来；出来的图再写回**同一个前缀**下。
母版几 MB，走编排 payload 是不行的（Temporal 单条 payload 有量级限制，且历史会一直背着它），
所以图走桶、键走编排。

**只写不签**。签名是"给谁看、看多久"的事，属业务侧——生成侧不知用户是谁。两边靠**确定性
对象键**接头：键由内容自身（`content_sha256`）推得，见 contracts `registries/object_keys.md`。
因此"这张图出没出来"问存储即知，不必另立台账——台账会与真相漂移，派生不会。

依赖方向（import-linter 锁定）：本模块只依赖运行库（oss2），不感知上层，也不认识 temporalio。
形态与 reportrender 的 `book_store` 相同，两仓各持一份（谁也不能 import 谁）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import oss2

MASTER_KEY_TEMPLATE = "uploads/{content_sha256}/plan-master.png"
"""母版的对象键。**唯一真源在 contracts `registries/object_keys.md`**，本行是逐字副本。

写的一侧是 render2d（activity `plan-2d-render`），读的一侧是本仓——两个仓，谁也不能 import 谁，
只能靠同一条键接头。**对不上就是接不上头**，所以本模块拿到不合形态的母版键当场失败，
不去猜、不去容错：容错的结果是把图写到一个谁也不会去读的地方。
"""

ATMOSPHERE_VISUAL_KEY_TEMPLATE = "uploads/{content_sha256}/atmosphere-{template_id}.{ext}"
"""风格图的对象键：**与源图同一前缀**下的一个派生物。

三条理由照抄注册表开头那三条——键**确定性派生**（同母版同模板同格式重跑覆盖同一个对象）、
**不铸新流水号**（铸一次就多一个对象，重推那次被幂等挡下、多出来的永远没人认领）、
**键里不含用户身份与渠道方言**（生成侧不知用户是谁，而键是生成侧的产物）。
`{template_id}` 进键是因为同一份母版会出好几张风格图，它是这几张之间唯一的区别。

**`{ext}` 由字节首部判定（用户裁决 2026-09-02："标签跟着内容走"）**，与注册表对上传件
`original.{ext}` 的先例同一条口径——键读起来要诚实，写着 `.png` 装着 JPEG 的名不副实作废
（此前"扩展名写死 `.png`"的口径被该裁决推翻，旧说理见 git 历史）。**裁决时认下的代价**：
换一个回不同格式的物理模型，同一张图的键会跟着换后缀，按旧键去取的一侧要认两个名——
拍的时候摆过这一条，取"诚实"舍"稳定"。
"""

_FORMAT_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
)
"""**按字节首部判定格式与扩展名**——同 contracts 对上传件 `{ext}` 的口径（"由字节首部判定，
不认字节就整条响亮失败，不按渠道给的文件名猜"）。扩展名词面照注册表词表（`jpg`/`png`；
`webp`/`gif`/`bmp` 目前没有产物会是，等真字节出现再加行）。签名链接不改这个头，
写的时候写错，业主点开看到的就不是一张图。"""

_CONTENT_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""图片字节的 SHA-256 十六进制**小写** 64 字符（注册表口径）。"""

_TEMPLATE_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""模板 id 要当键的一段用：只收小写字母数字与连字符。别的字符要么把键劈成两段
（`/`）、要么在不同系统上大小写不等价——两种都会让写进去的对象再也取不回来。"""

_ENDPOINT_ENV = "ISHOME_OSS_ENDPOINT"
_BUCKET_ENV = "ISHOME_OSS_BUCKET_PRIVATE"
_ACCESS_KEY_ID_ENV = "ISHOME_OSS_ACCESS_KEY_ID"
_ACCESS_KEY_SECRET_ENV = "ISHOME_OSS_ACCESS_KEY_SECRET"


class ImageStoreError(Exception):
    """取母版或写图失败——响亮失败。图写不进去就是这张图没出来，不许当成功回报。"""

    def __init__(self, details: list[str]) -> None:
        super().__init__("；".join(details))
        self.details = details


def content_sha256_of_master_key(master_object_key: str) -> str:
    """从母版键里取出 `content_sha256`；键不合约定形态即当场失败。

    校验放在**出图之前**：出一张图要花钱花时间，键不对的话出来了也没地方放。
    """
    parts = master_object_key.split("/")
    if len(parts) != 3 or not _CONTENT_SHA256_PATTERN.match(parts[1]):
        raise ImageStoreError(
            [f"母版键不合约定形态 `{MASTER_KEY_TEMPLATE}`：`{master_object_key}`"]
        )
    content_sha256 = parts[1]
    if master_object_key != MASTER_KEY_TEMPLATE.format(content_sha256=content_sha256):
        raise ImageStoreError(
            [f"母版键不合约定形态 `{MASTER_KEY_TEMPLATE}`：`{master_object_key}`"]
        )
    return content_sha256


def _image_format_of(image_bytes: bytes) -> tuple[str, str]:
    """这份字节是什么图，**按首部判**，返回 (Content-Type, 扩展名)。不认得就抛——不给猜的。"""
    for magic, content_type, ext in _FORMAT_BY_MAGIC:
        if image_bytes.startswith(magic):
            return content_type, ext
    raise ImageStoreError(
        [f"回来的字节不是认得的图（首部 {image_bytes[:8]!r}）：认不出格式就写不对头也起不了键"]
    )


def image_content_type_of(image_bytes: bytes) -> str:
    """这份字节的 Content-Type。与扩展名同源同判（裁决 2026-09-02 起键与头说的是同一件事）。"""
    return _image_format_of(image_bytes)[0]


def image_ext_of(image_bytes: bytes) -> str:
    """这份字节该用的扩展名（注册表词表词面：`jpg`/`png`）。"""
    return _image_format_of(image_bytes)[1]


def check_template_id(template_id: str) -> None:
    """模板 id 能不能当对象键的一段用。**装模板库时就判**，不是每次出图判。"""
    if not _TEMPLATE_ID_PATTERN.match(template_id):
        raise ImageStoreError(
            [f"模板 id 当不了对象键的一段（只收小写字母数字与连字符）：`{template_id}`"]
        )


def atmosphere_visual_key_of(master_object_key: str, template_id: str, image_bytes: bytes) -> str:
    """风格图的对象键：与母版同前缀，文件名带模板 id，**扩展名按字节首部判**（裁决 2026-09-02）。"""
    content_sha256 = content_sha256_of_master_key(master_object_key)
    check_template_id(template_id)
    return ATMOSPHERE_VISUAL_KEY_TEMPLATE.format(
        content_sha256=content_sha256, template_id=template_id, ext=image_ext_of(image_bytes)
    )


def check_shares_upload_prefix(master_object_key: str, object_key: str) -> None:
    """另一件产物是不是**与母版同一份上传件**的派生物（同 `uploads/{content_sha256}/` 前缀）。

    房间表与母版必须出自同一次绘制：拿甲的房间表去画乙的户型，出来的图每一间的功能都是错的，
    而它看上去完全正常——这条线上最贵的失败都长这个样子。**前缀相同是这件事唯一验得动的判据**：
    两件产物的键都由源图内容哈希派生，前缀一致就是同一份上传件。

    **不认产物的文件名**：房间表叫什么由写它的一侧（render2d 的 `plan-2d-render`）定，
    键由派发方从那一步的回执里原样带过来。在 contracts 把这条键登记下来之前，
    在本仓抄一个文件名就是把一次巧合当协议——抄的那份不会跟着对面改。
    """
    content_sha256 = content_sha256_of_master_key(master_object_key)
    prefix = f"uploads/{content_sha256}/"
    if not object_key.startswith(prefix) or "/" in object_key[len(prefix) :]:
        raise ImageStoreError(
            [
                f"`{object_key}` 与母版不在同一份上传件底下（母版 `{master_object_key}`）——"
                "那是拿甲的房间表去画乙的户型"
            ]
        )


@dataclass(frozen=True)
class OssSettings:
    """私有桶连接口径。四个值全部来自环境，代码里不留任何默认桶名或端点。"""

    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str

    @staticmethod
    def from_env() -> OssSettings:
        """从环境读取；**缺一即启动就失败**，不等到第一张图出完才发现存不进去。"""
        values = {
            name: os.environ.get(name, "").strip()
            for name in (_ENDPOINT_ENV, _BUCKET_ENV, _ACCESS_KEY_ID_ENV, _ACCESS_KEY_SECRET_ENV)
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ImageStoreError(
                [
                    f"私有对象存储没配全，缺：{'、'.join(missing)}——凭证放"
                    " ~/.ishome/oss-local.env（本机）或 /opt/ishome/env/oss.env（服务器），不入库"
                ]
            )
        return OssSettings(
            endpoint=values[_ENDPOINT_ENV],
            bucket=values[_BUCKET_ENV],
            access_key_id=values[_ACCESS_KEY_ID_ENV],
            access_key_secret=values[_ACCESS_KEY_SECRET_ENV],
        )


class OssImageStore:
    """私有桶的读写口：取母版、写风格图。签名不在这里——本层只读写不签（见模块文档）。"""

    def __init__(self, settings: OssSettings) -> None:
        auth = oss2.Auth(settings.access_key_id, settings.access_key_secret)
        self._bucket = oss2.Bucket(auth, settings.endpoint, settings.bucket)
        self._bucket_name = settings.bucket

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    def get_master(self, master_object_key: str) -> bytes:
        """按键取母版字节。取不到即上抛——**没有母版就没有几何来源**，不拿别的凑。"""
        content_sha256_of_master_key(master_object_key)  # 形态先过一遍，错键不发请求
        return self._get(master_object_key, "母版")

    def get_room_anchors(self, master_object_key: str, room_anchors_object_key: str) -> bytes:
        """按键取房间表字节，**并保证它与母版是同一份上传件的派生物**（见前缀判据）。"""
        check_shares_upload_prefix(master_object_key, room_anchors_object_key)
        return self._get(room_anchors_object_key, "房间表")

    def _get(self, object_key: str, what: str) -> bytes:
        try:
            data: bytes = self._bucket.get_object(object_key).read()
        except oss2.exceptions.NoSuchKey as e:
            raise ImageStoreError(
                [
                    f"{what}不在私有桶 `{self._bucket_name}` 里（键 {object_key}）——"
                    "写它的是 render2d 的 `plan-2d-render`，要么它没写成，要么两侧的键对不上"
                ]
            ) from e
        except oss2.exceptions.OssError as e:
            raise ImageStoreError(
                [f"取{what}失败（桶 `{self._bucket_name}`，键 {object_key}）：{e}"]
            ) from e
        if not data:
            raise ImageStoreError([f"{what}是空对象（键 {object_key}）"])
        return data

    def put_atmosphere_visual(
        self, master_object_key: str, template_id: str, image_bytes: bytes
    ) -> str:
        """写一张风格图，返回对象键。写失败即上抛——不吞、不返回一个指向空气的键。

        参数叫 `image_bytes` 不叫 `image_png`：模型回什么格式是它的事，本层按首部判——
        键的扩展名与 Content-Type 同源同判、说的是同一件事（裁决 2026-09-02"标签跟着内容走"）。
        """
        if not image_bytes:
            raise ImageStoreError(
                [f"图是空的，不往桶里写（母版 {master_object_key}，模板 {template_id}）"]
            )
        key = atmosphere_visual_key_of(master_object_key, template_id, image_bytes)
        content_type = image_content_type_of(image_bytes)
        try:
            self._bucket.put_object(key, image_bytes, headers={"Content-Type": content_type})
        except oss2.exceptions.OssError as e:
            raise ImageStoreError([f"图写不进私有桶 `{self._bucket_name}`（键 {key}）：{e}"]) from e
        return key
