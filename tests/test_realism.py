"""写实化纯库：控制稿逐像素、提示词固定句、后端契约（假网关）、门禁（没下限只记不判；v3 只记）。"""

from __future__ import annotations

import base64
import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from imagegen_worker import fidelity_metric_v3, realism, worker
from imagegen_worker.models import RealismStyleTemplate

pytestmark = pytest.mark.usefixtures("memoized_fidelity_v3")

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "真户型-基准-cam-bird-dollhouse"
_LINE_PNG = (_FIXTURES / "line.png").read_bytes()
_GEOMETRY_PNG = (_FIXTURES / "geometry.png").read_bytes()

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "realism"

_TEMPLATE = RealismStyleTemplate(
    template_id="t-realism",
    style="暖白哑光墙面、浅橡木地板、柔和自然光",
    negatives=["鱼眼畸变", "夜景"],
)


def _png(array: Any, mode: str) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 控制稿
# ---------------------------------------------------------------------------


def test_control_sketch_is_the_exact_inverse_in_rgb() -> None:
    """黑底白线单通道 → 白底黑线 RGB：每个像素严格 255-v，三个通道相同，尺寸不变。"""
    line = np.zeros((6, 9), dtype=np.uint8)
    line[2, :] = 255
    line[:, 4] = 255
    line[0, 0] = 37  # 万一将来线稿带灰阶：照样只反色，不阈值化

    sketch = Image.open(io.BytesIO(realism.build_control_sketch(_png(line, "L"))))

    assert sketch.format == "PNG"
    assert sketch.mode == "RGB"
    assert sketch.size == (9, 6)
    arr = np.asarray(sketch)
    expected = 255 - line
    for channel in range(3):
        assert (arr[..., channel] == expected).all()


def test_control_sketch_of_the_real_line_matches_the_surveyed_condition_image() -> None:
    """调研那批真跑送的条件图就是 255-line.png（逐像素核过）——同一份线稿这里做出同一份稿。"""
    sketch = np.asarray(Image.open(io.BytesIO(realism.build_control_sketch(_LINE_PNG))))
    line = np.asarray(Image.open(io.BytesIO(_LINE_PNG)).convert("L"))

    assert sketch.shape == (960, 1280, 3)
    assert set(np.unique(sketch).tolist()) == {0, 255}
    assert (sketch[..., 0] == 255 - line).all()


def test_control_sketch_rejects_empty_and_non_image_bytes() -> None:
    with pytest.raises(realism.RealismError, match="线稿是空的"):
        realism.build_control_sketch(b"")
    with pytest.raises(realism.RealismError, match="解不成图"):
        realism.build_control_sketch(b"definitely not a png")


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def test_prompt_carries_the_fixed_clauses_and_the_template_style() -> None:
    prompt = realism.build_realism_prompt(_TEMPLATE, "bird")

    assert "揭顶式户型鸟瞰效果图" in prompt
    assert "墙体、门窗洞口和房间划分严格按线稿，不增减墙体、不合并或拆分房间" in prompt
    assert _TEMPLATE.style in prompt
    assert "不摆放任何家具和陈设" in prompt
    assert "只渲染墙面、地面、天花和门窗的材质与光" in prompt
    assert "无文字、无水印、无标注" in prompt
    assert "避免：鱼眼畸变；夜景" in prompt


def test_view_clause_follows_view_kind() -> None:
    bird = realism.build_realism_prompt(_TEMPLATE, "bird")
    room = realism.build_realism_prompt(_TEMPLATE, "room")

    assert "鸟瞰" in bird and "室内效果图" not in bird
    assert "室内效果图" in room and "鸟瞰" not in room


def test_no_furnishing_clause_is_there_regardless_of_template() -> None:
    """无陈设不是风格是规矩（用户裁决 2026-09-04）：模板数据说不掉它。"""
    bare = RealismStyleTemplate(template_id="bare", style="s")

    assert "不摆放任何家具和陈设" in realism.build_realism_prompt(bare, "room")


def test_same_input_builds_the_same_prompt_and_hash() -> None:
    a = realism.build_realism_prompt(_TEMPLATE, "bird")
    b = realism.build_realism_prompt(_TEMPLATE, "bird")

    assert a == b
    assert realism.prompt_sha256_of(a) == realism.prompt_sha256_of(b)
    assert len(realism.prompt_sha256_of(a)) == 64


@pytest.mark.parametrize("path", sorted(_TEMPLATES_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_shipped_realism_templates_load(path: Path) -> None:
    """仓里那批写实风格模板逐份装得上；文件名与 templateId 对得上（产物键里带的是 id）。"""
    template = RealismStyleTemplate.model_validate(json.loads(path.read_text(encoding="utf-8")))

    assert template.template_id == path.stem
    assert template.style
    # 风格文字不许写家具（默认无陈设，模板里写了就是两半打架）
    assert not any(word in template.style for word in ("沙发", "床", "餐桌", "布艺"))


def test_worker_loads_every_shipped_realism_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 装配从目录读：`templates/realism/` 里有几份就装几份（北欧转正后是两份），
    风格图那套模板库照旧只读顶层、不被子目录里的写实模板绊倒。"""
    monkeypatch.setenv(worker.TEMPLATES_DIR_ENV, str(_TEMPLATES_DIR.parent))

    realism_templates = worker._load_realism_templates()
    atmosphere_templates = worker._load_templates()

    assert set(realism_templates) == {p.stem for p in _TEMPLATES_DIR.glob("*.json")}
    assert {"modern-minimal", "nordic-light"} <= set(realism_templates)
    assert realism_templates["nordic-light"].style.startswith("北欧风格")
    assert not set(atmosphere_templates) & set(realism_templates)


def test_realism_template_rejects_unknown_fields() -> None:
    """模板是数据；多出来的字段说明模板与代码对不上，当场拒收——尤其是构图/视角这种本该在代码里的。"""
    with pytest.raises(ValueError, match="extra"):
        RealismStyleTemplate.model_validate({"templateId": "t", "style": "s", "composition": "c"})


# ---------------------------------------------------------------------------
# 网关后端（假网关按契约应答）
# ---------------------------------------------------------------------------


class _FakeGateway:
    """按契约应答的假网关：记下每个请求，按预设的剧本回应。"""

    def __init__(self, script: list[tuple[int, dict[str, Any]]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str | None] = []
        self._script = list(script)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                outer.requests.append({"path": self.path, "body": body})
                outer.auth_headers.append(self.headers.get("Authorization"))
                status, payload = (
                    outer._script.pop(0) if outer._script else (500, {"message": "剧本用完"})
                )
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _ok_payload(image_bytes: bytes) -> dict[str, Any]:
    return {
        "created": 1,
        "data": [{"b64_json": base64.b64encode(image_bytes).decode()}],
        "usage": {"image_count": 1},
    }


@pytest.fixture
def gateway_factory() -> Iterator[Any]:
    gateways: list[_FakeGateway] = []

    def _make(script: list[tuple[int, dict[str, Any]]]) -> _FakeGateway:
        gateway = _FakeGateway(script)
        gateways.append(gateway)
        return gateway

    yield _make
    for gateway in gateways:
        gateway.close()


def test_gateway_backend_sends_the_contract_shape(gateway_factory: Any) -> None:
    gateway = gateway_factory([(200, _ok_payload(_GEOMETRY_PNG))])
    backend = realism.GatewaySketchBackend("k-test", gateway.url)
    sketch = realism.build_control_sketch(_LINE_PNG)

    output = backend.generate(sketch, "一段提示词", 12345)

    assert output.image_bytes == _GEOMETRY_PNG
    assert output.backend_name == "gateway-sketch"
    assert output.seed == 12345
    assert output.elapsed_seconds >= 0.0
    assert output.raw_meta["usage"] == {"image_count": 1}
    assert gateway.auth_headers == ["Bearer k-test"]
    [request] = gateway.requests
    assert request["path"] == "/v1/images/generations"
    body = request["body"]
    # 逻辑模型名，不是厂商模型；控制通道的标志位必带；一张白底黑线 PNG；seed 原样带
    assert body["model"] == "realism-pass.default"
    assert body["is_sketch"] is True
    assert body["n"] == 1
    assert body["response_format"] == "b64_json"
    assert body["seed"] == 12345
    assert body["prompt"] == "一段提示词"
    assert body["image"].startswith("data:image/png;base64,")
    sent = base64.b64decode(body["image"].split(",", 1)[1])
    assert sent == sketch
    assert Image.open(io.BytesIO(sent)).mode == "RGB"


def test_gateway_backend_omits_seed_when_none(gateway_factory: Any) -> None:
    gateway = gateway_factory([(200, _ok_payload(_GEOMETRY_PNG))])
    backend = realism.GatewaySketchBackend("k", gateway.url)

    output = backend.generate(realism.build_control_sketch(_LINE_PNG), "p", None)

    assert "seed" not in gateway.requests[0]["body"]
    assert output.seed is None  # 没给就如实写 None，不替派发方铸


def test_gateway_4xx_fails_without_retry(gateway_factory: Any) -> None:
    """契约不对（比如 is_sketch 缺失）网关拒 4xx：再发一次也是同一个结果，只会再花一次钱。"""
    gateway = gateway_factory([(400, {"message": "is_sketch is required"})])
    backend = realism.GatewaySketchBackend("k", gateway.url)

    with pytest.raises(realism.RealismError, match="HTTP 400"):
        backend.generate(realism.build_control_sketch(_LINE_PNG), "p", 1)

    assert len(gateway.requests) == 1


def test_gateway_5xx_is_retried_exactly_once(gateway_factory: Any) -> None:
    gateway = gateway_factory([(503, {"message": "busy"}), (200, _ok_payload(_GEOMETRY_PNG))])
    backend = realism.GatewaySketchBackend("k", gateway.url)

    output = backend.generate(realism.build_control_sketch(_LINE_PNG), "p", 1)

    assert output.image_bytes == _GEOMETRY_PNG
    assert len(gateway.requests) == 2


def test_gateway_5xx_twice_fails_loud(gateway_factory: Any) -> None:
    """最多重试一次：第二次还是 5xx 就响亮失败，不无限重试。"""
    gateway = gateway_factory([(503, {"message": "busy"}), (503, {"message": "busy"})])
    backend = realism.GatewaySketchBackend("k", gateway.url)

    with pytest.raises(realism.RealismError, match="HTTP 503"):
        backend.generate(realism.build_control_sketch(_LINE_PNG), "p", 1)

    assert len(gateway.requests) == 2


def test_receipt_without_an_image_fails_loud(gateway_factory: Any) -> None:
    """因果自检：2xx 但回执里没有图，不拿着空气往下走；也不重试（回执畸形不是网络抖动）。"""
    gateway = gateway_factory([(200, {"data": [{"revised_prompt": "x"}], "usage": {}})])
    backend = realism.GatewaySketchBackend("k", gateway.url)

    with pytest.raises(realism.RealismError, match="没有图片内容"):
        backend.generate(realism.build_control_sketch(_LINE_PNG), "p", 1)

    assert len(gateway.requests) == 1


def test_receipt_whose_bytes_are_not_an_image_fails_loud(gateway_factory: Any) -> None:
    gateway = gateway_factory([(200, _ok_payload(b"<html>not an image</html>"))])
    backend = realism.GatewaySketchBackend("k", gateway.url)

    with pytest.raises(realism.RealismError, match="不是一张图"):
        backend.generate(realism.build_control_sketch(_LINE_PNG), "p", 1)


def test_gateway_backend_capabilities_say_what_the_contract_says() -> None:
    caps = realism.GatewaySketchBackend("k", "http://gateway.test").capabilities()

    assert caps.condition_channels == ("sketch",)
    assert caps.seed_is_effective is True
    assert caps.max_images_per_call == 1
    assert caps.max_output_px is None  # 契约没登记上限：不填猜的值


def test_backend_comes_from_config_by_name_only() -> None:
    backend = realism.backend_from_config("gateway-sketch", api_key="k", gateway_url="http://g")

    assert backend.name == "gateway-sketch"
    with pytest.raises(realism.RealismError, match="没有叫 `wanx` 的写实化后端"):
        realism.backend_from_config("wanx", api_key="k", gateway_url="http://g")
    assert realism.backend_name_from_env({}) == "gateway-sketch"
    assert realism.backend_name_from_env({"ISHOME_REALISM_BACKEND": " gateway-sketch "}) == (
        "gateway-sketch"
    )


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_gate_without_a_floor_records_but_does_not_judge() -> None:
    """下限默认 None＝只记录不判（阈值有数据才定）：分数有、judged=False、passed=None。
    v3 的原位分、配准后分与位移一起记进回执（数同 `test_fidelity_metric_v3.py` 钉的）。"""
    verdict = realism.RealismGate().judge(_LINE_PNG, _GEOMETRY_PNG)

    assert verdict.fidelity_score == pytest.approx(0.0607, abs=1e-4)
    assert verdict.min_fidelity_score is None
    assert verdict.judged is False
    assert verdict.passed is None
    assert verdict.reason is None
    assert verdict.v3 is not None and verdict.v3_error is None
    assert verdict.v3.score_at_origin == pytest.approx(0.0675, abs=1e-4)
    assert verdict.v3.score_registered == pytest.approx(0.0690, abs=1e-4)
    assert verdict.v3.registration() == {"sx": 1.0, "sy": 1.0, "dx": 0, "dy": 1}
    receipt = verdict.as_dict()
    assert receipt["judged"] is False
    assert receipt["metric_versions"] == ["v2", "v3"]
    assert receipt["fidelity_v3_at_origin"] == verdict.v3.score_at_origin
    assert receipt["fidelity_v3_registered"] == verdict.v3.score_registered
    assert receipt["registration"] == {"sx": 1.0, "sy": 1.0, "dx": 0, "dy": 1}
    assert receipt["v3_error"] is None


def test_gate_records_a_v3_failure_and_still_judges_by_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """v3 是候选尺子只记录：它抛什么都不许拖垮门禁——v2 分数照量、判照 v2 下限，
    回执里 v3 三项为 None、`v3_error` 写明是什么。"""

    def boom(line_png: bytes, result_png: bytes, **kwargs: Any) -> Any:
        raise RuntimeError("v3 炸了")

    monkeypatch.setattr(fidelity_metric_v3, "score_fidelity_v3", boom)

    recorded = realism.RealismGate().judge(_LINE_PNG, _GEOMETRY_PNG)
    judged = realism.RealismGate(0.05).judge(_LINE_PNG, _GEOMETRY_PNG)

    assert recorded.fidelity_score == pytest.approx(0.0607, abs=1e-4)
    assert recorded.v3 is None
    assert recorded.v3_error == "RuntimeError: v3 炸了"
    receipt = recorded.as_dict()
    assert receipt["fidelity_v3_at_origin"] is None
    assert receipt["fidelity_v3_registered"] is None
    assert receipt["registration"] is None
    assert receipt["v3_error"] == "RuntimeError: v3 炸了"
    assert receipt["metric_versions"] == ["v2", "v3"]
    assert judged.judged and judged.passed is True and judged.v3_error == "RuntimeError: v3 炸了"


def test_gate_with_a_floor_judges_both_ways() -> None:
    passed = realism.RealismGate(0.05).judge(_LINE_PNG, _GEOMETRY_PNG)
    failed = realism.RealismGate(0.5).judge(_LINE_PNG, _GEOMETRY_PNG)

    assert passed.judged and passed.passed is True and passed.reason is None
    assert failed.judged and failed.passed is False
    assert failed.reason is not None and "低于下限 0.5000" in failed.reason


def test_gate_floor_comes_from_env_and_defaults_to_none() -> None:
    assert realism.RealismGate.from_env({}).min_fidelity_score is None
    assert (
        realism.RealismGate.from_env({"ISHOME_REALISM_MIN_FIDELITY_SCORE": ""}).min_fidelity_score
        is None
    )
    assert (
        realism.RealismGate.from_env(
            {"ISHOME_REALISM_MIN_FIDELITY_SCORE": "0.2"}
        ).min_fidelity_score
        == 0.2
    )
    with pytest.raises(realism.RealismError, match="不是数"):
        realism.RealismGate.from_env({"ISHOME_REALISM_MIN_FIDELITY_SCORE": "high"})
    with pytest.raises(realism.RealismError, match=r"\[0, 1\]"):
        realism.RealismGate(1.5)


def test_gate_cannot_score_garbage_and_says_so() -> None:
    with pytest.raises(realism.RealismError, match="保真度量不出来"):
        realism.RealismGate().judge(_LINE_PNG, b"not an image")


# ---------------------------------------------------------------------------
# CLI 子命令（假网关；风格图那条路的参数一个字不动）
# ---------------------------------------------------------------------------


def test_cli_realism_subcommand_runs_the_whole_path_against_a_fake_gateway(
    gateway_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from imagegen_worker import cli

    gateway = gateway_factory([(200, _ok_payload(_GEOMETRY_PNG))])
    monkeypatch.setenv("LITELLM_API_KEY", "k-cli")
    monkeypatch.delenv("ISHOME_REALISM_MIN_FIDELITY_SCORE", raising=False)
    out = tmp_path / "realism.png"

    code = cli.main(
        [
            "realism",
            "--line",
            str(_FIXTURES / "line.png"),
            "--style",
            "modern-minimal",
            "--view",
            "bird",
            "--seed",
            "7",
            "--gateway",
            gateway.url,
            "--templates-dir",
            str(_TEMPLATES_DIR.parent),
            "-o",
            str(out),
        ]
    )

    assert code == 0
    assert out.read_bytes() == _GEOMETRY_PNG
    assert out.with_suffix(".sketch.png").read_bytes() == realism.build_control_sketch(_LINE_PNG)
    prompt = out.with_suffix(".prompt.txt").read_text(encoding="utf-8")
    assert prompt == gateway.requests[0]["body"]["prompt"]
    assert "现代简约风格" in prompt and "不摆放任何家具和陈设" in prompt
    printed = capsys.readouterr().out
    assert "fidelity_score=0.0607" in printed
    assert "fidelity_v3_at_origin=0.0675 fidelity_v3_registered=0.0690" in printed
    assert "registration=sx=1.000 sy=1.000 dx=+0 dy=+1" in printed
    assert "backend_name=gateway-sketch seed=7" in printed
    assert "prompt_sha256=" + realism.prompt_sha256_of(prompt) in printed
    assert "只记录不判" in printed


def test_cli_realism_gate_failure_still_writes_the_image_and_exits_4(
    gateway_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imagegen_worker import cli

    gateway = gateway_factory([(200, _ok_payload(_GEOMETRY_PNG))])
    monkeypatch.setenv("LITELLM_API_KEY", "k-cli")
    monkeypatch.setenv("ISHOME_REALISM_MIN_FIDELITY_SCORE", "0.9")
    out = tmp_path / "realism.png"

    code = cli.main(
        ["realism", "--line", str(_FIXTURES / "line.png"), "--style", "modern-minimal",
         "--view", "room", "--gateway", gateway.url, "--templates-dir",
         str(_TEMPLATES_DIR.parent), "-o", str(out)]
    )  # fmt: skip

    assert code == cli.EXIT_GATE_FAILED
    assert out.exists()  # 本地迭代要看图：不过线也写出来


def test_cli_without_the_subcommand_is_still_the_atmosphere_path(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """风格图那条路的入口一个字不动：不带子命令仍走它（这里缺 key 就按它的老规矩退 2）。"""
    from imagegen_worker import cli

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)

    code = cli.main(["--master", "m.png", "--rooms", "r.json", "--template", "t.json"])

    assert code == 2
    assert "LITELLM_API_KEY" in capsys.readouterr().err
