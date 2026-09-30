"""Integration checks against the *real* sibling packages.

No model weights are loaded and nothing is rendered: this proves the bridge fits
the actual APIs (latent shape, keyframe resizing, sampler swap/restore on the
live Extender module, and SelfLift's positional signature) and that nothing is
left patched behind.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_selflift_integration.py
"""

from __future__ import annotations

import importlib.util
import inspect
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

from yanhuo_selflift import config, engine, vendor  # noqa: E402

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


def _require(name):
    try:
        return importlib.import_module(name)
    except Exception:  # pragma: no cover
        return None


@unittest.skipIf(_require("ComfyUI_MiniMax_H3_Extender") is None, "sibling Extender missing")
class RealPackageTests(unittest.TestCase):
    def test_selflift_package_is_shared_not_duplicated(self):
        # A second import would duplicate h3_upscaler._model_cache and load the
        # learned latent upscaler into memory twice.
        first = vendor.selflift_package()
        second = vendor.selflift_package()
        self.assertIs(first, second)
        self.assertIs(vendor.selflift_modules().upscaler.__name__.startswith(
            first.__name__), True)

    def test_progressive_sample_signature_matches_the_bridge(self):
        # Guards against an upstream reordering of SelfLift's positional args.
        real = vendor.selflift_modules().progressive_sample
        names = list(inspect.signature(real).parameters)
        expected = [
            "model", "positive", "negative", "vae", "latent_image", "sampler",
            "sigmas", "seed", "cfg", "transition_step", "lowres_scale", "rho",
            "w_min", "w_max", "latent_upsample", "latent_lifter", "highres_tiling",
            "model_hires",
        ]
        self.assertEqual(names[: len(expected)], expected)

    def test_viewmodel_exposes_the_sampler_hook(self):
        base = vendor.extender_module()
        self.assertTrue(callable(getattr(base, "_sample_h3", None)))


@unittest.skipIf(
    _require("ComfyUI_MiniMax_H3_Extender") is None or torch is None,
    "sibling packages or torch missing",
)
class RealLatentTests(unittest.TestCase):
    def test_extender_latent_passes_selflift_validation(self):
        base = vendor.extender_module()
        selflift = vendor.selflift_modules()
        latent = base._empty_av_latent(896, 576, 24)
        streams = list(latent["samples"].unbind())
        self.assertTrue(latent["samples"].is_nested)
        self.assertEqual(streams[0].ndim, 5)
        # No exception, no normalized mask for a plain empty latent.
        self.assertIsNone(selflift.nodes._validate_latent_input(latent))

    def test_keyframes_are_resized_for_the_low_resolution_grid(self):
        selflift = vendor.selflift_modules()
        keyframe = {
            "resolved_frame_index": 0.0,
            "latent": torch.arange(1 * 24 * 36 * 56, dtype=torch.float32).reshape(1, 24, 1, 36, 56)
            / (1 * 24 * 36 * 56),
        }
        cond = [(torch.zeros(1, 4), {"minimax_keyframes": [keyframe]})]
        low = selflift.nodes._resize_keyframes(cond, 18, 28)
        resized = low[0][1]["minimax_keyframes"][0]["latent"]
        self.assertEqual(tuple(resized.shape[-2:]), (18, 28))
        source_mean = keyframe["latent"].float().mean(dim=(-2, -1), keepdim=True)
        self.assertTrue(
            torch.allclose(
                resized.mean(dim=(-2, -1), keepdim=True), source_mean, atol=1e-5
            )
        )
        # The original conditioning must not be mutated.
        self.assertEqual(tuple(keyframe["latent"].shape[-2:]), (36, 56))

    def test_audio_stream_keeps_running_through_the_reused_euler_step(self):
        selflift = vendor.selflift_modules()
        state = torch.ones(1, 32, 2, 65)
        denoised = torch.zeros(1, 32, 2, 65)
        sigma = torch.tensor(0.75)
        sigma_next = torch.tensor(0.5)
        out = selflift.nodes._euler_step(state, denoised, sigma, sigma_next)
        expected = 1.0 + (1.0 - 0.0) * ((0.5 - 0.75) / 0.75)
        self.assertAlmostEqual(float(out.flatten()[0]), expected, places=6)


@unittest.skipIf(_require("ComfyUI_MiniMax_H3_Extender") is None, "sibling Extender missing")
class LiveSwapTests(unittest.TestCase):
    def test_live_module_is_restored_exactly(self):
        base = vendor.extender_module()
        original = base._sample_h3
        captured = []

        def fake_progressive(*args, **kwargs):  # pragma: no cover - not called
            captured.append(args)
            raise AssertionError("should not be invoked in this test")

        mods = types.SimpleNamespace(progressive_sample=fake_progressive, upscaler=None)
        settings = config.SelfLiftSettings.from_kwargs({})
        with engine.installed_sampler(settings, None, mods=mods, base_module=base):
            self.assertIsNot(base._sample_h3, original)
            self.assertTrue(getattr(base._sample_h3, "_yanhuo_selflift", False))
        self.assertIs(base._sample_h3, original)
        self.assertEqual(base.__dict__["_sample_h3"], original)

    def test_live_module_restores_after_a_failure(self):
        base = vendor.extender_module()
        original = base._sample_h3
        mods = types.SimpleNamespace(progressive_sample=None, upscaler=None)
        settings = config.SelfLiftSettings.from_kwargs({})
        try:
            with engine.installed_sampler(settings, None, mods=mods, base_module=base):
                raise KeyboardInterrupt("interrupted")
        except KeyboardInterrupt:
            pass
        self.assertIs(base._sample_h3, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
