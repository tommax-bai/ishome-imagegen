"""出站边缘：图像模型网关（LiteLLM）。**只认逻辑模型名，不认物理模型**。

依赖方向：本模块**不感知上层**——不 import models/style_prompt/activities，收字符串与字节、
回字节。物理模型映射在 infra 的网关配置里，换模型不动这里一行（`atmosphere-visual.default`
现指向火山方舟 Seedream）。

**图生图容易静默退化成文生图**：`image` / `size` / `watermark` 都不是 OpenAI 标准参数，
网关一旦开 `drop_params` 就把它们丢掉，出来的图与输入无关而调用照样成功——2026-08-23 接入时
踩过，配置里对这两个模型单独关掉了 drop_params。因此本模块**必查回执里的 `input_images`**：
它不是 1，就说明输入图没送到，响亮失败而不是拿着一张凭空生成的图往下走。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_GATEWAY_URL = "http://127.0.0.1:4000"
IMAGE_ENDPOINT = "/v1/images/generations"
DEFAULT_TIMEOUT_SECONDS = 300


class ImageGatewayError(Exception):
    """出图失败。响亮失败，不返回空图让下游拿着它跑。"""

    def __init__(self, details: list[str]) -> None:
        super().__init__("；".join(details))
        self.details = details


def _post(url: str, api_key: str, body: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload: dict[str, Any] = json.load(response)
    except urllib.error.HTTPError as e:
        raise ImageGatewayError([f"网关拒绝（HTTP {e.code}）：{e.read().decode()[:400]}"]) from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ImageGatewayError([f"网关连不上或回了不认识的东西：{e}"]) from e
    return payload


def generate_from_image(
    *,
    model: str,
    prompt: str,
    source_png: bytes,
    size: str,
    api_key: str,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, str]:
    """图生图：一张源图 + 一段提示词 → 一张图。返回（图字节, 模型改写后的提示词）。"""
    if not source_png:
        raise ImageGatewayError(["源图是空的：没有母版就没有几何来源，不往下走"])
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "image": "data:image/png;base64," + base64.b64encode(source_png).decode(),
        "size": size,
        "watermark": False,
        "n": 1,
        "response_format": "b64_json",
    }
    payload = _post(gateway_url + IMAGE_ENDPOINT, api_key, body, timeout_seconds)

    input_images = payload.get("usage", {}).get("input_images")
    if input_images != 1:
        raise ImageGatewayError(
            [
                f"输入图没送到模型（回执 input_images={input_images!r}）——"
                "图生图静默退化成了文生图，出来的图与母版无关。检查网关是否对本模型丢了 image 参数"
            ]
        )

    items = payload.get("data") or []
    if not items:
        raise ImageGatewayError(["网关回了空的图片列表"])
    encoded = items[0].get("b64_json")
    if not encoded:
        raise ImageGatewayError(["回执里没有图片内容（b64_json 为空）"])
    revised = items[0].get("revised_prompt") or ""
    return base64.b64decode(encoded), str(revised)
