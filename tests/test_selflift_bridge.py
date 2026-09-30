"""Bridge tests for Yanhuo H3 Motion Context × SelfLift.

Run inside the ComfyUI virtualenv (the engine imports ``comfy.*``)::

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_selflift_bridge.py

Nothing here touches the sibling packages on disk; every Extender interaction is
routed through a stub so the tests stay hermetic.
"""

from __future__ import annotations

import json
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

import importlib.util  # noqa: E402

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

from yanhuo_selflift import config, engine, node, vendor  # noqa: E402

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _StubBase:
    """Minimal stand-in for the sibling ``extender`` module."""

    FPS = 24

    def __init__(self, sigmas):
        self._sigmas_values = sigmas
        self.calls = []

    def _sigmas(self, model, scheduler, steps, denoise):
        # Mirrors the real helper's guards, including the denoise<=0 short circuit.
        steps = max(1, int(steps))
        if float(denoise) <= 0.0:
            return torch.FloatTensor([])
        return self._sigmas_values

    def _sample_h3(self, model, conditioning, latent, seed, sampler_name, scheduler, steps, denoise):
        self.calls.append(
            dict(steps=steps, scheduler=scheduler, sampler=sampler_name, seed=seed)
        )
        return {"samples": "single-stage", "downscale_ratio_temporal": 8}


def _sigmas(steps):
    if torch is None:  # pragma: no cover
        raise unittest.SkipTest("torch unavailable")
    return torch.linspace(1.0, 0.0, steps + 1)


def _capture_mods():
    captured = []

    def progressive_sample(model, positive, negative, vae, latent_image, sampler, sigmas,
                           seed, cfg, transition_step, lowres_scale, rho, w_min, w_max,
                           latent_upsample, latent_lifter=None, highres_tiling=False,
                           model_hires=None):
        captured.append(
            dict(
                model=model,
                positive=positive,
                negative=negative,
                vae=vae,
                latent_image=latent_image,
                sigmas=sigmas,
                seed=seed,
                cfg=cfg,
                transition_step=transition_step,
                lowres_scale=lowres_scale,
                rho=rho,
                w_min=w_min,
                w_max=w_max,
                latent_upsample=latent_upsample,
                lifter=latent_lifter,
                tiling=highres_tiling,
                hires=model_hires,
            )
        )
        out = dict(latent_image)
        out["samples"] = torch.zeros(1)
        return out

    return types.SimpleNamespace(captured=captured, progressive_sample=progressive_sample,
                                 upscaler=None), captured


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults_are_legal_for_four_steps(self):
        settings = config.SelfLiftSettings.from_kwargs({})
        self.assertTrue(settings.enabled)
        self.assertGreaterEqual(settings.transition_step, 1)
        self.assertLessEqual(settings.transition_step, 4 - 1)
        settings.validate(4)

    def test_missing_and_garbage_values_fall_back(self):
        settings = config.SelfLiftSettings.from_kwargs(
            {
                "selflift_transition_step": "x",
                "selflift_rho": None,
                "selflift_lowres_scale": float("nan"),
                "selflift_upscaler_model": None,
            }
        )
        self.assertEqual(settings.transition_step, 2)
        self.assertEqual(settings.rho, 0.6)
        self.assertEqual(settings.lowres_scale, 0.5)
        self.assertEqual(settings.upscaler_model, "none")

    def test_single_step_chains_still_fail(self):
        with self.assertRaises(ValueError) as ctx:
            config.SelfLiftSettings.from_kwargs({}).validate(1)
        self.assertIn("steps must be >= 2", str(ctx.exception))

    def test_transition_beyond_the_schedule_is_clamped_not_fatal(self):
        """v1.3.1: a too-large cut index has exactly one sane legal value, so
        the run continues with a WARNING instead of aborting mid-execution."""
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 7})
        with self.assertLogs("yanhuo_h3_selflift", level="WARNING") as captured:
            resolved = settings.validate(4, source="`steps`=4 with scheduler=simple, denoise=1.0")
        self.assertEqual(resolved.transition_step, 3)
        # The original settings object is frozen and untouched.
        self.assertEqual(settings.transition_step, 7)
        message = captured.output[0]
        self.assertIn("transition_step 7", message)
        self.assertIn("clamped to 3", message)
        self.assertIn("denoise=1.0", message)

    def test_clamped_plan_text_reports_the_effective_split(self):
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 7})
        with self.assertLogs("yanhuo_h3_selflift", level="WARNING"):
            resolved = settings.validate(4)
        self.assertIn("low_steps=3/high_steps=1", resolved.plan_text(4))

    def test_transition_below_one_is_lifted_to_one(self):
        """from_kwargs already floors the widget at 1, but a hand-built settings
        (e.g. from a project file) must survive too."""
        import dataclasses

        settings = dataclasses.replace(
            config.SelfLiftSettings.from_kwargs({}), transition_step=0
        )
        with self.assertLogs("yanhuo_h3_selflift", level="WARNING"):
            resolved = settings.validate(8)
        self.assertEqual(resolved.transition_step, 1)

    def test_rejects_disabled_correction_without_upscaler(self):
        settings = config.SelfLiftSettings.from_kwargs({"selflift_rho": 0.0})
        with self.assertRaises(ValueError) as ctx:
            settings.validate(8)
        self.assertIn("upscaler_model=none", str(ctx.exception))

    def test_signature_tracks_every_result_changing_field(self):
        base = config.SelfLiftSettings.from_kwargs({}).signature()
        changed = config.SelfLiftSettings.from_kwargs({"selflift_rho": 0.3}).signature()
        self.assertNotEqual(base, changed)
        unload = config.SelfLiftSettings.from_kwargs(
            {"selflift_upscaler_unload": False}
        ).signature()
        self.assertEqual(base, unload)
        off = config.SelfLiftSettings.from_kwargs({"selflift_enabled": False}).signature()
        self.assertNotEqual(base, off)
        for field in (
            "selflift_transition_step",
            "selflift_lowres_scale",
            "selflift_w_min",
            "selflift_w_max",
            "selflift_latent_upsample",
            "selflift_upscaler_model",
            "selflift_highres_tiling",
        ):
            kwargs = {field: self._other_value(field)}
            self.assertNotEqual(
                base, config.SelfLiftSettings.from_kwargs(kwargs).signature(), field
            )

    @staticmethod
    def _other_value(field):
        return {
            "selflift_transition_step": 3,
            "selflift_lowres_scale": 0.75,
            "selflift_w_min": 0.5,
            "selflift_w_max": 0.75,
            "selflift_latent_upsample": "bilinear",
            "selflift_upscaler_model": "h3_upscaler.safetensors",
            "selflift_highres_tiling": True,
        }[field]

    def test_plan_text_mentions_both_stages(self):
        text = config.SelfLiftSettings.from_kwargs({}).plan_text(6)
        self.assertIn("low_steps=2", text)
        self.assertIn("high_steps=4", text)


class SplitTests(unittest.TestCase):
    def test_selflift_widgets_are_removed_from_the_parent_payload(self):
        kwargs = {
            "model": "MODEL",
            "vae": "VAE",
            "steps": 8,
            "selflift_enabled": True,
            "selflift_rho": 0.4,
            "selflift_upscaler_model": "none",
        }
        settings, rest = node.split_settings(kwargs)
        self.assertEqual(rest, {"model": "MODEL", "vae": "VAE", "steps": 8})
        self.assertAlmostEqual(settings.rho, 0.4)

    def test_parent_widgets_are_never_consumed(self):
        settings, rest = node.split_settings({"steps": 8, "scheduler": "simple"})
        self.assertEqual(rest["scheduler"], "simple")
        self.assertTrue(settings.enabled)


# ---------------------------------------------------------------------------
# Sampler swap
# ---------------------------------------------------------------------------
class SwapTests(unittest.TestCase):
    def setUp(self):
        self.base = _StubBase(_sigmas(4))

    def test_enabled_run_routes_every_clip_through_selflift(self):
        mods, captured = _capture_mods()
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 2})
        with engine.installed_sampler(settings, "VAE", mods=mods, base_module=self.base):
            out = self.base._sample_h3(
                "model", "cond", {"samples": torch.zeros(1)}, 7, "euler", "simple", 4, 1.0
            )
        self.assertEqual(len(captured), 1)
        call = captured[0]
        self.assertIs(call["positive"], call["negative"])
        self.assertEqual(call["cfg"], 1.0)
        self.assertEqual(call["vae"], "VAE")
        self.assertEqual(call["seed"], 7)
        self.assertEqual(call["transition_step"], 2)
        self.assertEqual(call["tiling"], False)
        self.assertEqual(out["samples"].shape, (1,))
        self.assertNotIn("downscale_ratio_temporal", out)
        self.assertEqual(self.base.calls, [])

    def test_disabled_run_keeps_the_original_sampler(self):
        mods, captured = _capture_mods()
        settings = config.SelfLiftSettings.from_kwargs({"selflift_enabled": False})
        with engine.installed_sampler(settings, "VAE", mods=mods, base_module=self.base):
            out = self.base._sample_h3(
                "model", "cond", {"samples": torch.zeros(1)}, 7, "euler", "simple", 4, 1.0
            )
        self.assertEqual(captured, [])
        self.assertEqual(self.base.calls[0]["seed"], 7)
        self.assertEqual(out["samples"], "single-stage")

    def test_restore_happens_even_when_sampling_raises(self):
        mods, _captured = _capture_mods()
        settings = config.SelfLiftSettings.from_kwargs({})
        original = self.base._sample_h3
        with self.assertRaises(RuntimeError):
            with engine.installed_sampler(settings, "VAE", mods=mods, base_module=self.base):
                self.assertIsNotNone(getattr(self.base._sample_h3, "_yanhuo_selflift", None))
                raise RuntimeError("boom")
        self.assertIs(self.base._sample_h3.__func__, original.__func__)

    def test_transition_step_is_clamped_but_logged(self):
        mods, captured = _capture_mods()
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 9})
        with engine.installed_sampler(settings, "VAE", mods=mods, base_module=self.base):
            self.base._sample_h3(
                "model", "cond", {"samples": torch.zeros(1)}, 1, "euler", "simple", 4, 1.0
            )
        self.assertEqual(captured[0]["transition_step"], 3)

    def test_empty_schedule_short_circuits(self):
        mods, captured = _capture_mods()
        self.base._sigmas_values = torch.FloatTensor([])
        settings = config.SelfLiftSettings.from_kwargs({})
        with engine.installed_sampler(settings, "VAE", mods=mods, base_module=self.base):
            out = self.base._sample_h3(
                "model", "cond", {"samples": torch.zeros(1)}, 1, "euler", "simple", 0, 0.0
            )
        self.assertEqual(captured, [])
        self.assertEqual(out["samples"].shape, (1,))


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self._original = vendor.extender_module

    def tearDown(self):
        vendor.extender_module = self._original

    def _model(self, sampling):
        return types.SimpleNamespace(get_model_object=lambda key: sampling)

    def test_accepts_euler_on_a_const_flow_model(self):
        import comfy.model_sampling

        vendor.extender_module = lambda: _StubBase(_sigmas(8))
        model = self._model(comfy.model_sampling.CONST())
        engine.validate_plan(
            model, "euler", "simple", 8, 1.0,
            config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 4}),
        )

    def test_rejects_a_non_euler_sampler(self):
        import comfy.model_sampling

        vendor.extender_module = lambda: _StubBase(_sigmas(8))
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                self._model(comfy.model_sampling.CONST()), "dpmpp_2m", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}),
            )
        self.assertIn("Euler", str(ctx.exception))

    def test_rejects_a_non_flow_model(self):
        vendor.extender_module = lambda: _StubBase(_sigmas(8))
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                self._model(object()), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}),
            )
        self.assertIn("rectified-flow", str(ctx.exception))

    def test_disabled_needs_no_validation(self):
        vendor.extender_module = lambda: _StubBase(_sigmas(8))
        engine.validate_plan(
            self._model(object()), "anything", "simple", 1, 1.0,
            config.SelfLiftSettings.from_kwargs({"selflift_enabled": False}),
        )


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------
class _FakeChainBase:
    FPS = 24

    def __init__(self, manifest_path, data_path, truncate=True):
        self.manifest_path = Path(manifest_path)
        self.data_path = Path(data_path)
        self.truncate = truncate
        self.truncated = []
        self.writes = 0

    def _normalize_generation_mode(self, mode):
        return str(mode or "ref2va")

    def _parse_clips_json(self, clips_json, generation_mode, motion_context):
        return [{"id": f"clip_{i + 1}"} for i in range(int(clips_json or 1))]

    def _manifest_for_extender(self, owner, fps):
        assert owner
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {"segments": []}
        return self.data_path, self.manifest_path, manifest

    def _truncate_chain(self, data_path, manifest_path, manifest, index):
        self.truncated.append(index)
        manifest = dict(manifest)
        manifest["segments"] = []
        if self.truncate:
            self._write_json_atomic(manifest_path, manifest)
        return manifest

    def _write_json_atomic(self, path, manifest):
        self.writes += 1
        Path(path).write_text(json.dumps(manifest), encoding="utf-8")


class InvalidationTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest_path = self.root / "chain.json"
        self.data_path = self.root / "chain.h3cache"
        self.data_path.write_bytes(b"")
        self._original = vendor.extender_module

    def tearDown(self):
        vendor.extender_module = self._original
        self.tmp.cleanup()

    def _install(self, base):
        vendor.extender_module = lambda: base

    def _write_manifest(self, segments, key=None):
        payload = {"segments": segments}
        if key is not None:
            payload[config.UPGRADER_KEY] = key
        self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_first_run_stamps_without_touching_segments(self):
        self._write_manifest([])
        base = _FakeChainBase(self.manifest_path, self.data_path)
        self._install(base)
        self.assertFalse(
            node.invalidate_on_plan_change(
                config.SelfLiftSettings.from_kwargs({}), "28", "ref2va", True, 1
            )
        )
        self.assertEqual(base.truncated, [])
        written = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertIn(config.UPGRADER_KEY, written)

    def test_plan_change_drops_the_cached_chain(self):
        self._write_manifest([{"clip_id": "clip_1"}, {"clip_id": "clip_2"}], key="stale")
        base = _FakeChainBase(self.manifest_path, self.data_path)
        self._install(base)
        self.assertTrue(
            node.invalidate_on_plan_change(
                config.SelfLiftSettings.from_kwargs({}), "28", "ref2va", True, 1
            )
        )
        self.assertEqual(base.truncated, [0])
        written = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(written["segments"], [])
        self.assertEqual(
            written[config.UPGRADER_KEY],
            config.SelfLiftSettings.from_kwargs({}).signature(),
        )

    def test_matching_plan_keeps_the_cache(self):
        settings = config.SelfLiftSettings.from_kwargs({})
        self._write_manifest([{"clip_id": "clip_1"}], key=settings.signature())
        base = _FakeChainBase(self.manifest_path, self.data_path)
        self._install(base)
        self.assertFalse(
            node.invalidate_on_plan_change(settings, "28", "ref2va", True, 1)
        )
        self.assertEqual(base.truncated, [])
        self.assertEqual(base.writes, 0)

    def test_disabled_never_invalidates(self):
        self._write_manifest([{"clip_id": "clip_1"}], key="stale")
        base = _FakeChainBase(self.manifest_path, self.data_path)
        self._install(base)
        self.assertFalse(
            node.invalidate_on_plan_change(
                config.SelfLiftSettings.from_kwargs({"selflift_enabled": False}),
                "28", "ref2va", True, 1,
            )
        )
        self.assertEqual(base.truncated, [])

    def test_resolution_failure_is_contained(self):
        class Broken(_FakeChainBase):
            def _manifest_for_extender(self, owner, fps):
                raise RuntimeError("cache unavailable")

        self.manifest_path.write_text(json.dumps({"segments": []}), encoding="utf-8")
        self._install(Broken(self.manifest_path, self.data_path))
        with self.assertLogs("yanhuo_h3_selflift", level="WARNING"):
            self.assertFalse(
                node.invalidate_on_plan_change(
                    config.SelfLiftSettings.from_kwargs({}), "28", "ref2va", True, 1
                )
            )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
class RegistrationTests(unittest.TestCase):
    def test_node_is_registered_with_extension(self):
        expected = (
            [
                "YanhuoH3FinalDecodeOutput",
                "YanhuoH3MotionContextSelfLift",
                "YanhuoH3RefPackFromImages",
                "YanhuoH3VideoFileLoader",
            ]
            if node._BASE_EXTENDER is not None
            else []
        )
        self.assertEqual(sorted(PKG.NODE_CLASS_MAPPINGS), expected)

    def test_companion_nodes_have_chinese_display_names(self):
        names = PKG.NODE_DISPLAY_NAME_MAPPINGS
        for key in ("YanhuoH3RefPackFromImages", "YanhuoH3FinalDecodeOutput", "YanhuoH3VideoFileLoader"):
            if key not in names:  # pragma: no cover
                continue
            self.assertTrue(names[key], key)

    def test_input_types_keeps_every_parent_widget(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        cls = PKG.NODE_CLASS_MAPPINGS["YanhuoH3MotionContextSelfLift"]
        parent = node._BASE_EXTENDER.INPUT_TYPES()
        own = cls.INPUT_TYPES()
        for name in parent["required"]:
            self.assertIn(name, own["required"], name)
        for name in parent.get("optional", {}):
            if name in node._REMOVED_GLOBAL_INPUTS:
                # v1.2.0: 全局 ref_pack / prompt_pack 被有意移除，
                # 只保留与 CLIP N 同步的 ref_pack_N / prompt_N / ref_audio_N_x。
                self.assertNotIn(name, own.get("optional", {}), name)
                continue
            self.assertIn(name, own.get("optional", {}), name)
        added = [n for n in own["required"] if n.startswith("selflift_")]
        self.assertEqual(sorted(added), sorted(config.WIDGET_NAMES + config.INPUT_NAMES))

    def test_per_clip_ref_ports_survive_the_global_removal(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        cls = PKG.NODE_CLASS_MAPPINGS["YanhuoH3MotionContextSelfLift"]
        optional = cls.INPUT_TYPES().get("optional", {})
        for name in ("ref_pack_1", "prompt_1", "duration_1", "ref_audio_1_0"):
            self.assertIn(name, optional, name)
        for name in node._REMOVED_GLOBAL_INPUTS:
            self.assertNotIn(name, optional, name)

    def test_inherited_tooltips_are_chinese(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        cls = PKG.NODE_CLASS_MAPPINGS["YanhuoH3MotionContextSelfLift"]
        optional = cls.INPUT_TYPES().get("optional", {})
        # 抽样检查：这些端口的 tooltip 必须已经是中文（含 CJK 字符）。
        for name in ("ref_pack_1", "prompt_1", "duration_1", "ref_audio_1_0", "ref_audio_1"):
            tooltip = optional.get(name, (None, {}))[1].get("tooltip", "")
            self.assertTrue(any("一" <= ch <= "鿿" for ch in tooltip), f"{name}: {tooltip!r}")

    def test_sigmas_input_is_an_input_socket_not_a_widget(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        cls = PKG.NODE_CLASS_MAPPINGS["YanhuoH3MotionContextSelfLift"]
        spec = cls.INPUT_TYPES()["required"]["selflift_sigmas"]
        self.assertEqual(spec[0], "SIGMAS")

    def test_widget_order_appends_after_the_parent(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        cls = PKG.NODE_CLASS_MAPPINGS["YanhuoH3MotionContextSelfLift"]
        parent_order = list(node._BASE_EXTENDER.INPUT_TYPES()["required"])
        own_order = list(cls.INPUT_TYPES()["required"])
        self.assertEqual(own_order[: len(parent_order)], parent_order)


# ---------------------------------------------------------------------------
# Active-model selection
# ---------------------------------------------------------------------------
class ActiveModelTests(unittest.TestCase):
    def setUp(self):
        import comfy.model_sampling

        self._original = vendor.extender_module
        self.sampling = comfy.model_sampling.CONST()
        vendor.extender_module = lambda: types.SimpleNamespace(
            FPS=24,
            _normalize_generation_mode=lambda mode: str(mode or "ref2va"),
            _sigmas=lambda *a, **k: _sigmas(8),
        )

    def tearDown(self):
        vendor.extender_module = self._original

    @staticmethod
    def _recorder(tag, sampling):
        return types.SimpleNamespace(
            tag=tag, sampling=sampling, touched=0, get_model_object=None
        )

    def test_ref2va_validates_the_ref2va_model_only(self):
        ref2va = self._recorder("ref2va", self.sampling)
        ref2va.get_model_object = lambda key: setattr(ref2va, "touched", ref2va.touched + 1) or ref2va.sampling
        fl2va = self._recorder("fl2va", self.sampling)
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 4})
        node._validate_active_model(
            settings,
            {
                "model": ref2va,
                "fl2va_model": fl2va,
                "generation_mode": "ref2va",
                "sampler_name": "euler",
                "scheduler": "simple",
                "steps": 8,
                "denoise": 1.0,
            },
            log=_quiet_logger(),
        )
        self.assertGreater(ref2va.touched, 0)
        self.assertEqual(fl2va.touched, 0)

    def test_fl2va_validates_the_fl2va_model_only(self):
        ref2va = self._recorder("ref2va", self.sampling)
        fl2va = self._recorder("fl2va", self.sampling)
        fl2va.get_model_object = lambda key: setattr(fl2va, "touched", fl2va.touched + 1) or fl2va.sampling
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 4})
        node._validate_active_model(
            settings,
            {
                "model": ref2va,
                "fl2va_model": fl2va,
                "generation_mode": "fl2va",
                "sampler_name": "euler",
                "scheduler": "simple",
                "steps": 8,
                "denoise": 1.0,
            },
            log=_quiet_logger(),
        )
        self.assertGreater(fl2va.touched, 0)
        self.assertEqual(ref2va.touched, 0)


def _quiet_logger():
    """A logger that keeps validation output out of the test report."""
    logger = logging.getLogger("yanhuo_h3_selflift.test")
    logger.propagate = False
    logger.addHandler(logging.NullHandler())
    return logger


if __name__ == "__main__":
    unittest.main(verbosity=2)
