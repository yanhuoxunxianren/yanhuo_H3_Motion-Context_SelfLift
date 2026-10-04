"""v1.10.0 ``model_hires``: 二采（高分辨率/低噪阶段）可以另接一个模型。

规则很简单：一采（低分辨率/高噪）永远用父节点的 ``model``；只有连了
``model_hires`` 时，二采才改用那个模型。未连接时两个阶段共用同一个模型，
行为与 v1.9.0 完全一致。

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_model_hires.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
CUSTOM_NODES = PKG_ROOT.parent
COMFY_ROOT = CUSTOM_NODES.parent

for candidate in (str(COMFY_ROOT), str(CUSTOM_NODES), str(PKG_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

if "yanhuo_selflift" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "yanhuo_selflift",
        str(PKG_ROOT / "__init__.py"),
        submodule_search_locations=[str(PKG_ROOT)],
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["yanhuo_selflift"] = _module
    _spec.loader.exec_module(_module)

PKG = sys.modules["yanhuo_selflift"]

from yanhuo_selflift import config, engine, node  # noqa: E402

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeModel:
    """Looks enough like a ModelPatcher for the digest and the flow check."""

    def __init__(self, weights=None, patches=None, objects=None, sampling=None):
        self.weights = dict(weights or {})
        self.patches = dict(patches or {})
        self.object_patches = dict(objects or {})
        self._sampling = sampling
        self.touched = 0
        self.model = types.SimpleNamespace(
            named_parameters=lambda: list(self.weights.items()),
            state_dict=lambda: dict(self.weights),
        )

    def get_model_object(self, key):
        self.touched += 1
        return self._sampling


def _const_sampling():
    import comfy.model_sampling

    return comfy.model_sampling.CONST()


def _flow_model(weights=None, **kwargs):
    return _FakeModel(weights, sampling=_const_sampling(), **kwargs)


def _weights(seed=1.0):
    if torch is None:  # pragma: no cover
        raise unittest.SkipTest("torch unavailable")
    return {
        "blocks.0.weight": torch.arange(64, dtype=torch.float32) * seed,
        "blocks.1.bias": torch.linspace(0.0, seed, 16),
        "out.weight": torch.ones(3, 3) * seed,
    }


class _UntouchableHires(_FakeModel):
    """Stand-in for the user's own ``model_hires`` chain (checkpoint + its LoRA stack).

    ComfyUI applies a LoRA by ``model.clone()`` followed by ``add_patches()`` —
    that is exactly what the Extender's ``_apply_per_clip_loras`` does to the
    per-card model. If either call ever happened to ``model_hires`` inside this
    node, the card/global LoRA would have leaked into the high-resolution stage,
    which v1.10.0 promises must never happen: 二采只走 model_hires 自己的链。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mutations = []

    def clone(self, *args, **kwargs):  # pragma: no cover - must never fire
        self.mutations.append("clone")
        raise AssertionError("model_hires must never be cloned by this node")

    def add_patches(self, *args, **kwargs):  # pragma: no cover - must never fire
        self.mutations.append("add_patches")
        raise AssertionError("model_hires must never receive LoRA patches")


def _stub_base():
    return types.SimpleNamespace(
        FPS=24,
        _sigmas=lambda *a, **k: torch.linspace(1.0, 0.0, 9),
        _sample_h3=lambda *a, **k: {"samples": torch.zeros(1)},
    )


def _capture_mods():
    captured = []

    def progressive_sample(model, positive, negative, vae, latent_image, sampler, sigmas,
                           seed, cfg, transition_step, lowres_scale, rho, w_min, w_max,
                           latent_upsample, latent_lifter=None, highres_tiling=False,
                           model_hires=None):
        captured.append(dict(model=model, hires=model_hires, transition_step=transition_step))
        out = dict(latent_image)
        out["samples"] = torch.zeros(1)
        return out

    return types.SimpleNamespace(captured=captured, progressive_sample=progressive_sample,
                                 upscaler=None), captured


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------
class PortTests(unittest.TestCase):
    def setUp(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        self.cls = PKG.NODE_CLASS_MAPPINGS["YanhuoH3MotionContextSelfLift"]

    def test_port_is_optional_so_it_needs_no_connection(self):
        """不放 required 是有原因的：本机的 ComfyUI 会对"必填插座没连线"硬报错
        （Required input is missing），前端只会把已连线的插座写进 prompt。"""
        spec = self.cls.INPUT_TYPES()
        self.assertIn(config.MODEL_HIRES_INPUT, spec.get("optional") or {})
        self.assertNotIn(config.MODEL_HIRES_INPUT, spec.get("required") or {})

    def test_port_is_a_model_socket(self):
        spec = self.cls.INPUT_TYPES()["optional"][config.MODEL_HIRES_INPUT]
        self.assertEqual(spec[0], "MODEL")
        self.assertNotIn("default", spec[1])

    def test_port_tooltip_is_chinese(self):
        tooltip = self.cls.INPUT_TYPES()["optional"][config.MODEL_HIRES_INPUT][1].get("tooltip", "")
        self.assertTrue(any("一" <= ch <= "鿿" for ch in tooltip), tooltip)
        # 关键语义必须写清楚：一采用 model，二采用 model_hires。
        self.assertIn("一采", tooltip)
        self.assertIn("二采", tooltip)

    def test_port_is_declared_as_an_input_not_a_widget(self):
        self.assertIn(config.MODEL_HIRES_INPUT, config.INPUT_NAMES)
        self.assertNotIn(config.MODEL_HIRES_INPUT, config.WIDGET_NAMES)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
@unittest.skipIf(torch is None, "torch unavailable")
class RoutingTests(unittest.TestCase):
    def _settings(self, **kwargs):
        return config.SelfLiftSettings.from_kwargs(kwargs)

    def test_dual_stage_forwards_the_second_stage_model(self):
        mods, captured = _capture_mods()
        hires = _FakeModel()
        engine._dual_stage(
            _stub_base(), mods, self._settings(selflift_transition_step=3), None,
            "lowres-model", None, {"samples": torch.zeros(1)}, 0,
            "euler", "simple", 8, 1.0, model_hires=hires,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["model"], "lowres-model")
        self.assertIs(captured[0]["hires"], hires)

    def test_without_the_port_both_stages_share_one_model(self):
        """v1.9.0 行为：不接 model_hires 时 upstream 自己回退到同一个 model。"""
        mods, captured = _capture_mods()
        engine._dual_stage(
            _stub_base(), mods, self._settings(), None,
            "only-model", None, {"samples": torch.zeros(1)}, 0,
            "euler", "simple", 8, 1.0,
        )
        self.assertIsNone(captured[0]["hires"])
        self.assertEqual(captured[0]["model"], "only-model")

    def _plan_line(self, **kwargs):
        """跑一次 _dual_stage，返回它打出来的 plan 日志原文。"""
        mods, _captured = _capture_mods()
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        engine._LOG.addHandler(handler)
        engine._LOG.setLevel(logging.DEBUG)
        try:
            engine._dual_stage(
                _stub_base(), mods, self._settings(), None,
                "lowres-model", None, {"samples": torch.zeros(1)}, 0,
                "euler", "simple", 8, 1.0, **kwargs,
            )
        finally:
            engine._LOG.removeHandler(handler)
        plans = [r.getMessage() for r in records if "plan:" in str(r.msg)]
        self.assertEqual(len(plans), 1, [str(r.msg) for r in records])
        return plans[0]

    def test_plan_line_formats_with_the_port_connected(self):
        """v1.10.0 漏写 %s 的回归：占位符比实参少一个时 logging 直接抛
        "not all arguments converted"，整行 plan 日志会静默消失（只剩
        --- Logging error ---）。getMessage() 能格式化成功才算过。"""
        line = self._plan_line(model_hires=_FakeModel())
        self.assertIn("hires=model_hires", line)
        self.assertIn("low 2 step(s)", line)

    def test_plan_line_formats_without_the_port(self):
        line = self._plan_line()
        self.assertNotIn("hires=", line)
        self.assertIn("low 2 step(s)", line)

    def test_replacement_threads_it_every_clip(self):
        base = _stub_base()
        mods, captured = _capture_mods()
        hires = _FakeModel()
        replacement = engine.make_replacement(
            base, mods, self._settings(), None, model_hires=hires
        )
        replacement(_FakeModel(), None, {"samples": torch.zeros(1)}, 0, "euler", "simple", 8, 1.0)
        replacement(_FakeModel(), None, {"samples": torch.zeros(1)}, 1, "euler", "simple", 8, 1.0)
        self.assertEqual([call["hires"] for call in captured], [hires, hires])

    def test_installed_sampler_carries_it_and_restores(self):
        base = _stub_base()
        mods, captured = _capture_mods()
        hires = _FakeModel()
        original = base._sample_h3
        with engine.installed_sampler(self._settings(), None, mods=mods,
                                      base_module=base, model_hires=hires):
            base._sample_h3(_FakeModel(), None, {"samples": torch.zeros(1)},
                            0, "euler", "simple", 8, 1.0)
        self.assertEqual(captured[0]["hires"], hires)
        self.assertIs(base._sample_h3, original)


# ---------------------------------------------------------------------------
# Settings / cache signature
# ---------------------------------------------------------------------------
class SignatureTests(unittest.TestCase):
    def test_unlinked_port_leaves_the_signature_untouched(self):
        base = config.SelfLiftSettings.from_kwargs({})
        self.assertEqual(base.signature(), config.SelfLiftSettings.from_kwargs({}).signature())
        self.assertFalse(base.uses_second_stage_model())
        self.assertNotIn("hires=custom", base.plan_text(8))

    def test_linking_the_port_changes_the_signature(self):
        base = config.SelfLiftSettings.from_kwargs({})
        linked = dataclasses.replace(base, hires_model_linked=True, hires_model_digest="abc123")
        self.assertNotEqual(base.signature(), linked.signature())
        self.assertTrue(linked.uses_second_stage_model())
        self.assertIn("hires=custom", linked.plan_text(8))

    def test_swapping_the_model_changes_the_signature(self):
        first = dataclasses.replace(
            config.SelfLiftSettings.from_kwargs({}),
            hires_model_linked=True, hires_model_digest="AAA",
        )
        second = dataclasses.replace(first, hires_model_digest="BBB")
        self.assertNotEqual(first.signature(), second.signature())


@unittest.skipIf(torch is None, "torch unavailable")
class ModelDigestTests(unittest.TestCase):
    def test_missing_model_has_no_digest(self):
        self.assertEqual(config.model_digest(None), "")

    def test_digest_is_stable_and_content_sensitive(self):
        first = _FakeModel(_weights(1.0))
        again = _FakeModel(_weights(1.0))
        other = _FakeModel(_weights(2.0))
        self.assertEqual(config.model_digest(first), config.model_digest(again))
        self.assertNotEqual(config.model_digest(first), config.model_digest(other))

    def test_same_architecture_different_weights_are_distinguished(self):
        """最容易被漏掉的一种：形状/精度完全一样，只有数值不同。"""
        a = _FakeModel({"w": torch.zeros(32)})
        b = _FakeModel({"w": torch.ones(32)})
        self.assertNotEqual(config.model_digest(a), config.model_digest(b))

    def test_lora_strength_changes_the_digest(self):
        """同一份 checkpoint、只改 LoRA 强度也必须重渲。"""
        shape = ("lora.up", torch.ones(4))
        weak = _FakeModel(_weights(1.0), patches={"out.weight": [(0.8, shape, 1.0, None, None)]})
        strong = _FakeModel(_weights(1.0), patches={"out.weight": [(1.0, shape, 1.0, None, None)]})
        self.assertNotEqual(config.model_digest(weak), config.model_digest(strong))

    def test_a_different_lora_stack_changes_the_digest(self):
        shape = ("lora.up", torch.ones(4))
        one = _FakeModel(_weights(1.0), patches={"out.weight": [(1.0, shape, 1.0, None, None)]})
        both = _FakeModel(_weights(1.0), patches={
            "out.weight": [(1.0, shape, 1.0, None, None)],
            "out.bias": [(1.0, ("lora.bias", torch.ones(2)), 1.0, None, None)],
        })
        self.assertNotEqual(config.model_digest(one), config.model_digest(both))

    def test_object_patches_are_counted(self):
        plain = _FakeModel(_weights(1.0))
        patched = _FakeModel(_weights(1.0), objects={"transformer_options": object()})
        self.assertNotEqual(config.model_digest(plain), config.model_digest(patched))

    def test_unfingerprintable_model_returns_empty(self):
        """没有权重也没有 patch（自定义包装）时必须返回空串，而不是给所有模型同一个值。"""
        self.assertEqual(config.model_digest(_FakeModel()), "")
        self.assertEqual(config.model_digest("not-a-model"), "")

    def test_a_huge_weight_is_never_copied(self):
        """只采样几个值：几百万元素的张量也要秒出，不能整包搬来搬去。"""
        big = torch.zeros(2_000_000)
        big[1_234_567] = 1.0
        digest = config.model_digest(_FakeModel({"huge": big}))
        self.assertTrue(digest)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@unittest.skipIf(torch is None, "torch unavailable")
class ValidationTests(unittest.TestCase):
    def _settings(self, **kwargs):
        return config.SelfLiftSettings.from_kwargs(kwargs)

    def test_accepts_a_second_flow_model(self):
        engine.validate_hires_model(_flow_model(_weights(1.0)), self._settings())

    def test_rejects_a_second_model_that_is_not_flow(self):
        with self.assertRaises(ValueError) as ctx:
            engine.validate_hires_model(_FakeModel(_weights(1.0)), self._settings())
        self.assertIn("rectified-flow", str(ctx.exception))
        self.assertIn("model_hires", str(ctx.exception))

    def test_no_port_and_disabled_settings_skip_validation(self):
        engine.validate_hires_model(None, self._settings())
        engine.validate_hires_model(_FakeModel(_weights(1.0)),
                                    self._settings(selflift_enabled=False))

    def test_first_stage_message_still_names_the_model_input(self):
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(_FakeModel(_weights(1.0)), "euler", "simple", 8, 1.0,
                                 self._settings())
        self.assertIn("rectified-flow", str(ctx.exception))


# ---------------------------------------------------------------------------
# Node plumbing
# ---------------------------------------------------------------------------
@unittest.skipIf(torch is None, "torch unavailable")
class NodePlumbingTests(unittest.TestCase):
    """extend_with_selflift 必须吃掉 model_hires，不能把它原样交给父类。"""

    def _run(self, **kwargs):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        instance = node.YanhuoH3MotionContextSelfLift()
        seen = {}
        invalidated = {}
        installed = {}
        saved = (node._BASE_EXTENDER.extend, node.invalidate_on_plan_change,
                 engine.installed_sampler, engine._schedule)

        # 真局长链路需要一个真 checkpoint 才能算 sigma；这里只关心 model_hires 的
        # 走向，所以把 schedule helper 替掉（同 test_external_sigmas 的做法）。
        def fake_schedule(model, scheduler, steps, denoise):
            return torch.linspace(1.0, 0.0, int(steps) + 1)

        def fake_extend(self, **payload):
            seen.update(payload)
            return "ok"

        def fake_invalidate(settings, *args, **kwargs_):
            invalidated.update(vars(settings))
            return False

        @contextlib.contextmanager
        def fake_install(settings, vae, **rest):
            installed.update(rest)
            yield True

        node._BASE_EXTENDER.extend = fake_extend
        node.invalidate_on_plan_change = fake_invalidate
        engine.installed_sampler = fake_install
        engine._schedule = fake_schedule
        try:
            result = instance.extend_with_selflift(**kwargs)
        finally:
            (node._BASE_EXTENDER.extend, node.invalidate_on_plan_change,
             engine.installed_sampler, engine._schedule) = saved
        return result, seen, invalidated, installed

    def _base_kwargs(self, **extra):
        kwargs = dict(
            model=_flow_model(_weights(1.0)),
            vae=None,
            sampler_name="euler",
            scheduler="simple",
            steps=8,
            denoise=1.0,
            generation_mode="ref2va",
            motion_context=False,
            unique_id="42",
        )
        kwargs.update(extra)
        return kwargs

    def test_port_never_reaches_the_parent(self):
        hires = _flow_model(_weights(2.0))
        result, seen, _invalidated, _installed = self._run(
            **self._base_kwargs(model_hires=hires)
        )
        self.assertEqual(result, "ok")
        # 父类不认识这个键，漏给它只会换成一个 TypeError。
        self.assertNotIn(config.MODEL_HIRES_INPUT, seen)
        self.assertIn("model", seen)

    def test_port_reaches_the_sampler(self):
        hires = _flow_model(_weights(2.0))
        _result, _seen, _invalidated, installed = self._run(
            **self._base_kwargs(model_hires=hires)
        )
        self.assertIs(installed.get("model_hires"), hires)

    def test_linked_port_stamps_the_settings_before_the_plan_check(self):
        hires = _flow_model(_weights(2.0))
        _result, _seen, invalidated, _installed = self._run(
            **self._base_kwargs(model_hires=hires)
        )
        self.assertTrue(invalidated["hires_model_linked"])
        self.assertTrue(invalidated["hires_model_digest"])

    def test_unconnected_port_leaves_the_settings_pristine(self):
        _result, _seen, invalidated, installed = self._run(**self._base_kwargs())
        self.assertFalse(invalidated["hires_model_linked"])
        self.assertEqual(invalidated["hires_model_digest"], "")
        self.assertIsNone(installed.get("model_hires"))

    def test_none_value_from_a_muted_upstream_is_survivable(self):
        _result, _seen, invalidated, installed = self._run(
            **self._base_kwargs(model_hires=None)
        )
        self.assertFalse(invalidated["hires_model_linked"])
        self.assertIsNone(installed.get("model_hires"))

    def test_card_and_global_lora_never_reach_model_hires(self):
        """二采只走 model_hires 自己的 LoRA 链。

        条件卡 LoRA / 全局 LoRA 是父节点在 ``_apply_per_clip_loras`` 里对
        ``model`` 做 ``clone() + add_patches()`` 得到的，产物只作为 ``model``
        进采样。本节点对 ``model_hires`` 只允许两件事：算指纹、校验是不是
        rectified-flow。任何 clone/add_patches 都说明 LoRA 漏进了二采。
        """
        hires = _UntouchableHires(_weights(2.0), sampling=_const_sampling())
        _result, _seen, invalidated, installed = self._run(
            **self._base_kwargs(model_hires=hires)
        )
        self.assertEqual(hires.mutations, [])
        # 原样透传：同一个对象进采样器，中途没有产生任何副本。
        self.assertIs(installed.get("model_hires"), hires)
        self.assertTrue(invalidated["hires_model_linked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
