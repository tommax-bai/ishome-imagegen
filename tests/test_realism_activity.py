"""写实化 activity：假后端下全链（取键 → 控制稿 → 提示词 → 出图 → 量分 → 写桶 → 回键与自证数），
以及门禁路径（v2 判、v3 只记）、"写不进桶不许当成功"、入参形态。
桶与后端都用桩件——真桶真网关由真跑留档。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from temporalio import activity as temporal_activity

from imagegen_worker import fidelity_metric_v3, realism
from imagegen_worker.activities import ACTIVITY_REALISM_PASS, RealismPassRenderer
from imagegen_worker.image_store import ImageStoreError, realism_visual_key_of
from imagegen_worker.models import RealismStyleTemplate

pytestmark = pytest.mark.usefixtures("memoized_fidelity_v3")

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "真户型-基准-cam-bird-dollhouse"
_LINE_PNG = (_FIXTURES / "line.png").read_bytes()
_GEOMETRY_PNG = (_FIXTURES / "geometry.png").read_bytes()

_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_CAMERA_ID = "cam-bird-dollhouse"
_LINE_KEY = f"uploads/{_SHA}/{_CAMERA_ID}/line.png"
_STYLE_ID = "modern-minimal"
_TEMPLATE = RealismStyleTemplate(template_id=_STYLE_ID, style="暖白哑光墙面、浅橡木地板")


class FakeRealismBackend:
    """假后端：记下收到的控制稿与提示词，回一张预设的图（默认回几何图——它与线稿几何全对）。"""

    def __init__(self, image_bytes: bytes = _GEOMETRY_PNG, fails_with: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._image_bytes = image_bytes
        self._fails_with = fails_with

    @property
    def name(self) -> str:
        return "fake"

    def capabilities(self) -> realism.RealismCapabilities:
        return realism.RealismCapabilities(("sketch",), True, 1, None)

    def generate(
        self, sketch_png: bytes, prompt: str, seed: int | None, *, run_ref: str | None = None
    ) -> realism.RealismOutput:
        self.calls.append(
            {"sketch_png": sketch_png, "prompt": prompt, "seed": seed, "run_ref": run_ref}
        )
        if self._fails_with is not None:
            raise realism.RealismError([self._fails_with])
        return realism.RealismOutput(
            image_bytes=self._image_bytes,
            backend_name=self.name,
            seed=seed,
            elapsed_seconds=0.25,
            raw_meta={"usage": {"image_count": 1}},
        )


class _StubImageStore:
    def __init__(
        self,
        *,
        line_png: bytes = _LINE_PNG,
        get_fails_with: str | None = None,
        put_fails_with: str | None = None,
    ) -> None:
        self.written: dict[str, bytes] = {}
        self._line_png = line_png
        self._get_fails_with = get_fails_with
        self._put_fails_with = put_fails_with

    @property
    def bucket_name(self) -> str:
        return "ishome-test"

    def get_control_source(self, source_object_key: str) -> bytes:
        if self._get_fails_with is not None:
            raise ImageStoreError([self._get_fails_with])
        return self._line_png

    def put_realism_visual(
        self, source_object_key: str, camera_id: str, style_template_id: str, image_bytes: bytes
    ) -> str:
        if self._put_fails_with is not None:
            raise ImageStoreError([self._put_fails_with])
        key = realism_visual_key_of(source_object_key, camera_id, style_template_id, image_bytes)
        self.written[key] = image_bytes
        return key


def _renderer(
    store: Any,
    backend: FakeRealismBackend | None = None,
    gate: realism.RealismGate | None = None,
) -> RealismPassRenderer:
    return RealismPassRenderer(
        store,
        {_STYLE_ID: _TEMPLATE},
        backend or FakeRealismBackend(),
        gate or realism.RealismGate(),
    )


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "lineKey": _LINE_KEY,
        "styleTemplateId": _STYLE_ID,
        "viewKind": "bird",
        "cameraId": _CAMERA_ID,
        "seed": 12345,
    }
    request.update(overrides)
    return request


class _FakeActivityInfo:
    """Temporal 的 activity 上下文桩件：四个字段——运行编号取 workflow_id，其余三个留痕那层要用。"""

    activity_type = ACTIVITY_REALISM_PASS
    workflow_id = "wf-42"
    workflow_run_id = "run-1"
    attempt = 1


async def test_the_run_ref_comes_from_the_workflow_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行编号用现成的 Temporal workflow id 拼，不为调用记录新造一个标识。

    同一次运行里同一个机位可能换种子重出，编号带上机位与种子才分得开是哪一跑；
    不在 activity 上下文里（单测直接调实现件）就没有编号，如实写 None、不编一个。
    """
    backend = FakeRealismBackend()
    await _renderer(_StubImageStore(), backend).apply_realism_pass(_request())
    assert backend.calls[0]["run_ref"] is None

    monkeypatch.setattr(temporal_activity, "in_activity", lambda: True)
    monkeypatch.setattr(temporal_activity, "info", lambda: _FakeActivityInfo())

    marked = FakeRealismBackend()
    await _renderer(_StubImageStore(), marked).apply_realism_pass(_request())
    assert marked.calls[0]["run_ref"] == f"wf-42:{_CAMERA_ID}:seed12345"

    seedless = FakeRealismBackend()
    await _renderer(_StubImageStore(), seedless).apply_realism_pass(_request(seed=None))
    assert seedless.calls[0]["run_ref"] == f"wf-42:{_CAMERA_ID}"


async def test_full_chain_with_a_fake_backend() -> None:
    store = _StubImageStore()
    backend = FakeRealismBackend()

    result = await _renderer(store, backend).apply_realism_pass(_request())

    assert result["verdict"] == "ok"
    # 键与源线稿同前缀，文件名只带风格，扩展名跟字节走（几何图是 PNG）
    assert result["image_object_key"] == f"uploads/{_SHA}/{_CAMERA_ID}/realism-{_STYLE_ID}.png"
    # 机位在键里只出现一次（前缀那段）——文件名里那份 2026-09-06 删掉，别再加回来
    assert result["image_object_key"].count(_CAMERA_ID) == 1
    assert store.written[result["image_object_key"]] == _GEOMETRY_PNG
    assert result["bucket"] == "ishome-test"
    assert result["source_object_key"] == _LINE_KEY
    assert result["camera_id"] == _CAMERA_ID
    assert result["style_template_id"] == _STYLE_ID
    assert result["view_kind"] == "bird"
    assert result["content_type"] == "image/png"
    assert result["image_size_bytes"] == len(_GEOMETRY_PNG)
    # 自证数
    assert result["backend_name"] == "fake"
    assert result["seed"] == 12345
    assert result["elapsed_seconds"] == 0.25
    assert result["fidelity_score"] == pytest.approx(0.0607, abs=1e-4)
    assert result["prompt_sha256"] == realism.prompt_sha256_of(result["prompt"])
    # 回执 gate：v2 判（没下限＝只记录）、v3 两个分数与配准一起记，给定阈值攒分布
    assert result["gate"] == {
        "fidelity_score": result["fidelity_score"],
        "min_fidelity_score": None,
        "judged": False,
        "passed": None,
        "reason": None,
        "fidelity_v3_at_origin": pytest.approx(0.0675, abs=1e-4),
        "fidelity_v3_registered": pytest.approx(0.0690, abs=1e-4),
        "registration": {"sx": 1.0, "sy": 1.0, "dx": 0, "dy": 1},
        "v3_error": None,
        "metric_versions": ["v2", "v3"],
    }
    # 后端收到的是反色控制稿、提示词是我们拼的那段、seed 原样
    [call] = backend.calls
    assert call["sketch_png"] == realism.build_control_sketch(_LINE_PNG)
    assert call["prompt"] == result["prompt"]
    assert "不摆放任何家具和陈设" in call["prompt"]
    assert "揭顶式户型鸟瞰效果图" in call["prompt"]
    assert call["seed"] == 12345


async def test_sketch_key_is_accepted_as_the_geometry_source() -> None:
    sketch_key = f"uploads/{_SHA}/{_CAMERA_ID}/sketch.png"
    store = _StubImageStore()

    result = await _renderer(store).apply_realism_pass(_request(lineKey=None, sketchKey=sketch_key))

    assert result["verdict"] == "ok"
    assert result["source_object_key"] == sketch_key
    assert result["image_object_key"].startswith(f"uploads/{_SHA}/{_CAMERA_ID}/realism-")


async def test_v3_failure_is_recorded_and_the_image_still_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v3 只记录：它抛错不许拖垮出图——verdict 仍 ok、图进桶、v2 分数照带，`v3_error` 写明原因。"""

    def boom(line_png: bytes, result_png: bytes, **kwargs: Any) -> Any:
        raise RuntimeError("v3 炸了")

    monkeypatch.setattr(fidelity_metric_v3, "score_fidelity_v3", boom)
    store = _StubImageStore()

    result = await _renderer(store).apply_realism_pass(_request())

    assert result["verdict"] == "ok"
    assert len(store.written) == 1
    assert result["fidelity_score"] == pytest.approx(0.0607, abs=1e-4)
    assert result["gate"]["v3_error"] == "RuntimeError: v3 炸了"
    assert result["gate"]["fidelity_v3_at_origin"] is None
    assert result["gate"]["fidelity_v3_registered"] is None
    assert result["gate"]["registration"] is None
    assert result["gate"]["metric_versions"] == ["v2", "v3"]


async def test_gate_with_a_floor_blocks_and_writes_nothing() -> None:
    """给了下限才判：不过线按 failed 回报、带分数与原因，图**不进桶**（同键会盖掉上一张过线的）。"""
    store = _StubImageStore()

    result = await _renderer(store, gate=realism.RealismGate(0.5)).apply_realism_pass(_request())

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["fidelity-gate-failed"]
    assert "低于下限 0.5000" in result["violations"][0]["detail"]
    assert result["fidelity_score"] == pytest.approx(0.0607, abs=1e-4)
    assert result["gate"]["judged"] is True and result["gate"]["passed"] is False
    assert result["backend_name"] == "fake" and result["seed"] == 12345
    assert store.written == {}


async def test_gate_with_a_floor_passes_when_above_it() -> None:
    store = _StubImageStore()

    result = await _renderer(store, gate=realism.RealismGate(0.05)).apply_realism_pass(_request())

    assert result["verdict"] == "ok"
    assert result["gate"]["judged"] is True and result["gate"]["passed"] is True
    assert len(store.written) == 1


async def test_backend_failure_is_reported_not_swallowed() -> None:
    store = _StubImageStore()

    result = await _renderer(
        store, FakeRealismBackend(fails_with="网关拒绝（HTTP 400）")
    ).apply_realism_pass(_request())

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["realism-failed"]
    assert "HTTP 400" in result["violations"][0]["detail"]
    assert store.written == {}


async def test_store_failure_is_not_reported_as_success() -> None:
    result = await _renderer(_StubImageStore(put_fails_with="桶不存在")).apply_realism_pass(
        _request()
    )

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["image-store-failed"]
    assert "桶不存在" in result["violations"][0]["detail"]


async def test_missing_line_never_spends_a_generation() -> None:
    backend = FakeRealismBackend()

    result = await _renderer(
        _StubImageStore(get_fails_with="线稿不在私有桶里"), backend
    ).apply_realism_pass(_request())

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["image-store-failed"]
    assert backend.calls == []


async def test_line_that_is_not_an_image_fails_before_the_model() -> None:
    backend = FakeRealismBackend()

    result = await _renderer(_StubImageStore(line_png=b"not a png"), backend).apply_realism_pass(
        _request()
    )

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["realism-failed"]
    assert backend.calls == []


async def test_unknown_style_template_says_what_is_installed() -> None:
    backend = FakeRealismBackend()

    result = await _renderer(_StubImageStore(), backend).apply_realism_pass(
        _request(styleTemplateId="no-such-style")
    )

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["gate-unknown-style-template"]
    assert _STYLE_ID in result["violations"][0]["detail"]
    assert backend.calls == []


@pytest.mark.parametrize(
    "request_payload",
    [
        _request(lineKey=None),  # 一个几何源都没给
        _request(sketchKey=f"uploads/{_SHA}/{_CAMERA_ID}/sketch.png"),  # 两个都给
        {k: v for k, v in _request().items() if k != "cameraId"},
        {k: v for k, v in _request().items() if k != "viewKind"},
        _request(viewKind="top"),
        _request(seed="twelve"),
        # 档位参数：渲染只有一档（用户裁决 2026-09-04），收下它就是收下一个不起作用的开关
        _request(renderTier="final"),
        _request(outputKey="somewhere/else.png"),  # 键由本仓派生，派发方不给
    ],
)
async def test_input_that_does_not_match_the_agreed_shape_fails_loud(
    request_payload: dict[str, Any],
) -> None:
    backend = FakeRealismBackend()

    result = await _renderer(_StubImageStore(), backend).apply_realism_pass(request_payload)

    assert result["verdict"] == "failed"
    assert [v["check"] for v in result["violations"]] == ["gate-bad-input"]
    assert backend.calls == []


async def test_seed_none_is_passed_through_and_reported_as_none() -> None:
    backend = FakeRealismBackend()

    result = await _renderer(_StubImageStore(), backend).apply_realism_pass(_request(seed=None))

    assert result["verdict"] == "ok"
    assert backend.calls[0]["seed"] is None
    assert result["seed"] is None


def test_activity_name_is_the_contracts_one() -> None:
    assert ACTIVITY_REALISM_PASS == "realism-pass"
