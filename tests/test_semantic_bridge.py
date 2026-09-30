"""Coverage for the v1.6.0 built-in Semantic Bridge.

The bridge is a tiny distilled MLP applied on the H3 text conditioning
([B, T, 5120]) after encoding and before sampling. These tests pin:

* model discovery under ``models/semantic_bridge``,
* the maths contract (alpha=0 is a true bypass; per_token/global keep magnitude;
  dtype/device/shape are preserved),
* graceful skipping of non-H3 conditioning,
* the upstream hook wraps and restores ``_make_ref2va_conditioning`` /
  ``make_fl2va_conditioning``.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_semantic_bridge.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

import torch

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

from yanhuo_selflift import config, semantic_bridge  # noqa: E402


def _fake_cond(tokens=7, dim=5120, dtype=torch.float32):
    tensor = torch.randn(1, tokens, dim, dtype=dtype)
    return [[tensor, {"pooled_output": None}]]


def _real_adapter():
    names = [x for x in semantic_bridge.list_adapters() if not x.startswith("NO_")]
    return names[0] if names else None


def _settings(**kwargs):
    base = dict(
        bridge_enabled=True,
        bridge_adapter=_real_adapter() or "",
        bridge_alpha=0.10,
        bridge_magnitude="per_token",
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class DiscoveryTests(unittest.TestCase):
    def test_lists_local_bridge_models_when_present(self):
        names = semantic_bridge.list_adapters()
        self.assertTrue(names)
        if not os.path.isdir(semantic_bridge.model_dir()):  # pragma: no cover
            self.skipTest("no semantic_bridge model folder on this machine")
        real = [x for x in names if not x.startswith("NO_")]
        self.assertTrue(real, f"expected real adapters, got {names}")
        self.assertTrue(all(x.endswith(".safetensors") for x in names))


class BypassTests(unittest.TestCase):
    def test_alpha_zero_is_a_true_bypass(self):
        cond = _fake_cond()
        self.assertIs(semantic_bridge.apply_bridge(cond, _real_adapter(), 0.0), cond)

    def test_disabled_context_does_not_touch_upstream(self):
        calls = {"n": 0}

        def make(*_a, **_k):
            calls["n"] += 1
            return _fake_cond(), {}

        fake = types.SimpleNamespace(
            _make_ref2va_conditioning=make,
            make_fl2va_conditioning=make,
        )
        original = semantic_bridge.vendor.extender_module
        try:
            semantic_bridge.vendor.extender_module = lambda: fake
            with semantic_bridge.installed_bridge(_settings(bridge_enabled=False)):
                pass
            self.assertIs(fake._make_ref2va_conditioning, make)
            self.assertIs(fake.make_fl2va_conditioning, make)
            self.assertEqual(calls["n"], 0)
        finally:
            semantic_bridge.vendor.extender_module = original


class MathContractTests(unittest.TestCase):
    def setUp(self):
        adapter = _real_adapter()
        if adapter is None:  # pragma: no cover
            self.skipTest("no bridge model available")
        self.adapter = adapter

    def _hybrid(self, alpha, magnitude):
        cond = _fake_cond()
        out = semantic_bridge.apply_bridge(cond, self.adapter, alpha, magnitude)
        return cond[0][0], out[0][0]

    def test_shape_and_dtype_preserved(self):
        original, hybrid = self._hybrid(0.1, "per_token")
        self.assertEqual(original.shape, hybrid.shape)
        self.assertEqual(original.dtype, hybrid.dtype)
        self.assertFalse(torch.equal(original, hybrid))

    def test_magnitude_helpers_match_target_rms(self):
        # 幅值对齐的契约本身：对齐后 source 的 RMS 等于 target 的 RMS。
        target = torch.randn(1, 7, 5120)
        source = torch.randn(1, 7, 5120) * 3.0

        per_token = semantic_bridge._match_per_token(source, target)
        t_rms = target.pow(2).mean(dim=-1).sqrt()
        p_rms = per_token.pow(2).mean(dim=-1).sqrt()
        self.assertTrue(torch.allclose(p_rms, t_rms, rtol=1e-4, atol=1e-4))

        glob = semantic_bridge._match_global(source, target)
        self.assertTrue(
            torch.allclose(
                glob.pow(2).mean().sqrt(),
                target.pow(2).mean().sqrt(),
                rtol=1e-4,
                atol=1e-4,
            )
        )

    def test_hybrid_stays_in_a_reasonable_magnitude_band(self):
        # hybrid = h + alpha*(p-h) 是两个向量的凸组合，RMS 必然 <= 原 RMS
        # （只有 p 与 h 完全同向时才相等）；这里只要求不失控。
        for mode in ("per_token", "global", "none"):
            original, hybrid = self._hybrid(0.1, mode)
            o_rms = original.float().pow(2).mean().sqrt()
            h_rms = hybrid.float().pow(2).mean().sqrt()
            ratio = float(h_rms / o_rms)
            self.assertGreater(ratio, 0.3)
            self.assertLessEqual(ratio, 1.05)

    def test_modes_produce_different_results(self):
        original, per_token = self._hybrid(0.1, "per_token")
        _, glob = self._hybrid(0.1, "global")
        _, none_mode = self._hybrid(0.1, "none")
        self.assertFalse(torch.equal(per_token, glob))
        self.assertFalse(torch.equal(per_token, none_mode))

    def test_alpha_one_moves_further_than_small_alpha(self):
        original, small = self._hybrid(0.1, "per_token")
        _, large = self._hybrid(1.0, "per_token")
        d_small = (small.float() - original.float()).abs().mean()
        d_large = (large.float() - original.float()).abs().mean()
        self.assertGreater(d_large, d_small)

    def test_metadata_tagged(self):
        cond = _fake_cond()
        out = semantic_bridge.apply_bridge(cond, self.adapter, 0.12, "global")
        meta = out[0][1]
        self.assertTrue(meta["yanhuo_h3_semantic_bridge"])
        self.assertAlmostEqual(meta["yanhuo_h3_semantic_bridge_alpha"], 0.12)
        self.assertEqual(meta["yanhuo_h3_semantic_bridge_mode"], "global")

    def test_non_h3_conditioning_is_skipped(self):
        cond = [[torch.randn(1, 7, 4096), {}]]
        out = semantic_bridge.apply_bridge(cond, self.adapter, 0.2, "per_token")
        self.assertIs(out[0][0], cond[0][0])

    def test_bad_adapter_falls_back_without_raising(self):
        cond = _fake_cond()
        out = semantic_bridge.apply_bridge(cond, "does_not_exist.safetensors", 0.2, "per_token")
        self.assertIs(out[0][0], cond[0][0])


class HookTests(unittest.TestCase):
    def setUp(self):
        self.adapter = _real_adapter()
        if self.adapter is None:  # pragma: no cover
            self.skipTest("no bridge model available")

    def test_wraps_and_restores_both_builders(self):
        latent = {"samples": torch.zeros(1)}

        def make(*_a, **_k):
            return _fake_cond(), latent

        fake = types.SimpleNamespace(
            _make_ref2va_conditioning=make,
            make_fl2va_conditioning=make,
        )
        original = semantic_bridge.vendor.extender_module
        try:
            semantic_bridge.vendor.extender_module = lambda: fake
            settings = _settings(bridge_adapter=self.adapter, bridge_alpha=0.15)
            with semantic_bridge.installed_bridge(settings) as _:
                result = fake._make_ref2va_conditioning()
                self.assertIsInstance(result, tuple)
                self.assertTrue(result[0][0][1].get("yanhuo_h3_semantic_bridge"))
                self.assertAlmostEqual(result[0][0][1]["yanhuo_h3_semantic_bridge_alpha"], 0.15)
                self.assertIs(result[1], latent)
            # 退出后必须还原，绝不影响其它节点
            self.assertIs(fake._make_ref2va_conditioning, make)
            self.assertIs(fake.make_fl2va_conditioning, make)
        finally:
            semantic_bridge.vendor.extender_module = original


class SignatureTests(unittest.TestCase):
    def test_disabled_bridge_leaves_signature_untouched(self):
        off = config.SelfLiftSettings(bridge_enabled=False)
        self.assertEqual(off.bridge_signature(), "")
        self.assertNotIn("bridge", off.signature())

    def test_changing_bridge_settings_changes_signature(self):
        a = config.SelfLiftSettings(
            bridge_enabled=True, bridge_adapter="x.safetensors", bridge_alpha=0.1,
        )
        b = config.SelfLiftSettings(
            bridge_enabled=True, bridge_adapter="x.safetensors", bridge_alpha=0.2,
        )
        c = config.SelfLiftSettings(
            bridge_enabled=True, bridge_adapter="y.safetensors", bridge_alpha=0.1,
        )
        self.assertNotEqual(a.signature(), b.signature())
        self.assertNotEqual(a.signature(), c.signature())


if __name__ == "__main__":
    unittest.main()
