"""Coverage for the optional external ``selflift_sigmas`` input.

An externally built sigma schedule (basic scheduler -> interpolation -> sigma
refiner) must replace the scheduler/steps/denoise widgets for the whole run,
participate in the cache signature, and be validated up front - a bad schedule
must be rejected before Queue, not mid-chain.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_external_sigmas.py
"""

from __future__ import annotations

import dataclasses
import importlib.util
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


def _linear(steps):
    if torch is None:  # pragma: no cover
        raise unittest.SkipTest("torch unavailable")
    return torch.linspace(1.0, 0.0, steps + 1)


def _schedule(steps):
    """A shaped-but-legal schedule: start at 1, end at 0, strictly decreasing."""
    if torch is None:  # pragma: no cover
        raise unittest.SkipTest("torch unavailable")
    inner = torch.linspace(1.0, 0.05, steps)
    return torch.cat([inner, torch.zeros(1)])


def _const_model():
    import comfy.model_sampling

    return types.SimpleNamespace(get_model_object=lambda key: comfy.model_sampling.CONST())


class _StubBase:
    """Records whether the widget-driven schedule helper was reached."""

    def __init__(self, sigmas=None):
        self._sigmas_values = sigmas
        self.schedule_calls = []

    def _sigmas(self, model, scheduler, steps, denoise):
        self.schedule_calls.append((scheduler, steps, denoise))
        return self._sigmas_values if self._sigmas_values is not None else _linear(int(steps))

    def _sample_h3(self, model, conditioning, latent, seed, sampler_name, scheduler, steps, denoise):
        return {"samples": "single-stage"}


def _capture_mods():
    captured = []

    def progressive_sample(model, positive, negative, vae, latent_image, sampler, sigmas,
                           seed, cfg, transition_step, lowres_scale, rho, w_min, w_max,
                           latent_upsample, latent_lifter=None, highres_tiling=False,
                           model_hires=None):
        captured.append(
            dict(sigmas=sigmas, transition_step=transition_step, lowres_scale=lowres_scale)
        )
        out = dict(latent_image)
        out["samples"] = torch.zeros(1)
        return out

    return types.SimpleNamespace(captured=captured, progressive_sample=progressive_sample,
                                 upscaler=None), captured


@unittest.skipIf(torch is None, "torch unavailable")
class DigestTests(unittest.TestCase):
    def test_none_gives_empty_digest(self):
        self.assertEqual(config.sigmas_digest(None), "")

    def test_empty_tensor_gives_empty_digest(self):
        self.assertEqual(config.sigmas_digest(torch.zeros(0)), "")

    def test_digest_is_stable_and_content_sensitive(self):
        first = config.sigmas_digest(_schedule(8))
        again = config.sigmas_digest(_schedule(8))
        self.assertEqual(first, again)
        self.assertNotEqual(first, config.sigmas_digest(_schedule(9)))
        self.assertNotEqual(first, config.sigmas_digest(torch.zeros(9)))

    def test_digest_reflects_device_and_dtype_normalisation(self):
        fp64 = _schedule(8).to(torch.float64)
        self.assertEqual(config.sigmas_digest(fp64), config.sigmas_digest(_schedule(8)))

    def test_signature_tracks_the_digest(self):
        base = config.SelfLiftSettings.from_kwargs({})
        with_sigmas = dataclasses.replace(base, sigmas_digest="9:abc")
        self.assertNotEqual(base.signature(), with_sigmas.signature())
        # plan text announces the takeover
        self.assertIn("sigmas=external", with_sigmas.plan_text(8))
        self.assertNotIn("sigmas=external", base.plan_text(8))


@unittest.skipIf(torch is None, "torch unavailable")
class SplitTests(unittest.TestCase):
    def test_sigmas_input_survives_the_split(self):
        """v1.3.2 regression: the socket must reach extend_with_selflift.

        ``split_settings`` used to peel ``selflift_sigmas`` along with the
        widgets, handing the tensor to ``SelfLiftSettings.from_kwargs`` which
        silently drops it. The node then sampled with its own ``steps`` widget
        while the UI still showed the "external sigmas in effect" badge.
        """
        sigmas = _schedule(8)
        settings, payload = node.split_settings(
            {"selflift_enabled": True, "selflift_sigmas": sigmas, "steps": 8}
        )
        self.assertIs(payload.get("selflift_sigmas"), sigmas)
        self.assertEqual(payload["steps"], 8)
        # The settings object only ever carries widget scalars.
        self.assertTrue(settings.enabled)
        for value in vars(settings).values():
            self.assertNotIsInstance(value, torch.Tensor)

    def test_link_diagnostic_names_the_source_node(self):
        prompt = {
            "7": {"class_type": "YanhuoH3MotionContextSelfLift",
                  "inputs": {"selflift_sigmas": ["4", 0]}},
            "4": {"class_type": "H3SigmaRefiner", "inputs": {}},
        }
        self.assertIn("节点 4", node._describe_sigmas_link(
            {"prompt": prompt, "unique_id": "7"}))
        self.assertIn("H3SigmaRefiner", node._describe_sigmas_link(
            {"prompt": prompt, "unique_id": "7"}))

    def test_link_diagnostic_flags_a_missing_upstream(self):
        prompt = {
            "7": {"class_type": "YanhuoH3MotionContextSelfLift",
                  "inputs": {"selflift_sigmas": ["99", 0]}},
        }
        self.assertIn("不在提示中", node._describe_sigmas_link(
            {"prompt": prompt, "unique_id": "7"}))

    def test_link_diagnostic_flags_an_unsubmitted_link(self):
        prompt = {"7": {"class_type": "YanhuoH3MotionContextSelfLift", "inputs": {}}}
        self.assertIn("没有连线", node._describe_sigmas_link(
            {"prompt": prompt, "unique_id": "7"}))

    def test_normalises_device_dtype_and_shape(self):
        tensor = engine.normalize_sigmas(_schedule(8).to(torch.float64))
        self.assertEqual(tensor.dtype, torch.float32)
        self.assertEqual(tensor.device.type, "cpu")
        self.assertEqual(tensor.ndim, 1)

    def test_empty_and_missing_inputs_fall_back_to_none(self):
        self.assertIsNone(engine.normalize_sigmas(None))
        self.assertIsNone(engine.normalize_sigmas(torch.zeros(0)))


@unittest.skipIf(torch is None, "torch unavailable")
class ExternalValidationTests(unittest.TestCase):
    def test_legal_external_schedule_passes(self):
        base = _StubBase()
        engine.validate_plan(
            _const_model(), "euler", "simple", 8, 1.0,
            config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 3}),
            sigmas=_schedule(8),
        )
        # The widget schedule helper must never be reached.
        self.assertEqual(base.schedule_calls, [])

    def test_rejects_an_intermediate_zero(self):
        # still non-increasing, but zero appears before the end
        sigmas = torch.tensor([1.0, 0.5, 0.0, 0.0, 0.0])
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                _const_model(), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}), sigmas=sigmas,
            )
        self.assertIn("zero", str(ctx.exception))

    def test_rejects_a_non_monotonic_schedule(self):
        sigmas = _schedule(8)
        sigmas[3] = sigmas[2] + 0.1
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                _const_model(), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}), sigmas=sigmas,
            )
        self.assertIn("non-increasing", str(ctx.exception))

    def test_rejects_a_schedule_that_does_not_end_at_zero(self):
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                _const_model(), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}),
                sigmas=torch.linspace(1.0, 0.05, 9),
            )
        self.assertIn("end at 0", str(ctx.exception))

    def test_rejects_a_single_interval_schedule(self):
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                _const_model(), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}), sigmas=torch.tensor([1.0]),
            )
        self.assertIn("at least two entries", str(ctx.exception))

    def test_transition_beyond_the_external_schedule_is_clamped(self):
        """v1.3.1: same recovery as the widget path - clamp + WARNING, no abort."""
        with self.assertLogs("yanhuo_h3_selflift", level="WARNING") as captured:
            engine.validate_plan(
                _const_model(), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 9}),
                sigmas=_schedule(8),
            )
        message = captured.output[0]
        self.assertIn("transition_step 9", message)
        self.assertIn("clamped to 7", message)
        # the message must still say where the step count came from
        self.assertIn("selflift_sigmas", message)
        self.assertIn("9 entries -> 8 steps", message)

    def test_widget_path_message_names_the_widgets(self):
        """The widget path must name steps/scheduler/denoise and must NOT talk
        about selflift_sigmas. v1.3.1: it clamps instead of raising, so stub the
        resolved schedule rather than needing a real model to sample from."""
        from unittest import mock

        with mock.patch.object(engine, "_schedule", lambda *a, **k: _linear(8)):
            with self.assertLogs("yanhuo_h3_selflift", level="WARNING") as captured:
                engine.validate_plan(
                    _const_model(), "euler", "simple", 8, 1.0,
                    config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 8}),
                )
        message = captured.output[0]
        self.assertIn("out of range for 8 steps", message)
        self.assertIn("scheduler=simple", message)
        # The *source* line must not claim the step count came from the table.
        self.assertIn("resolved from `steps`=8", message)
        self.assertNotIn("resolved from the external", message)
        # ...and it should actively point at the likely cause (chain not wired).
        self.assertIn("`selflift_sigmas` is not in effect", message)

    def test_widget_path_survives_a_too_large_transition(self):
        """Regression: steps=4 / transition_step=7 used to abort after the model
        was already loaded. It must now finish validating."""
        from unittest import mock

        with mock.patch.object(engine, "_schedule", lambda *a, **k: _linear(4)):
            with self.assertLogs("yanhuo_h3_selflift", level="WARNING"):
                engine.validate_plan(
                    _const_model(), "euler", "simple", 4, 1.0,
                    config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 7}),
                )

    def test_rejects_a_start_sigma_at_or_above_one(self):
        sigmas = torch.tensor([1.0, 1.0, 0.5, 0.0])
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                _const_model(), "euler", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 1}),
                sigmas=sigmas,
            )
        self.assertIn("less than 1", str(ctx.exception))

    def test_still_requires_euler(self):
        with self.assertRaises(ValueError) as ctx:
            engine.validate_plan(
                _const_model(), "dpmpp_2m", "simple", 8, 1.0,
                config.SelfLiftSettings.from_kwargs({}), sigmas=_schedule(8),
            )
        self.assertIn("Euler", str(ctx.exception))

    def test_disabled_settings_skip_everything(self):
        engine.validate_plan(
            _const_model(), "dpmpp_2m", "simple", 8, 1.0,
            config.SelfLiftSettings(enabled=False), sigmas=torch.tensor([0.5, 0.6, 0.0]),
        )


@unittest.skipIf(torch is None, "torch unavailable")
class RoutingTests(unittest.TestCase):
    def test_dual_stage_uses_the_external_schedule(self):
        base = _StubBase()
        mods, captured = _capture_mods()
        latent = {"samples": torch.zeros(1)}
        engine._dual_stage(
            base, mods, config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 3}),
            None, _const_model(), None, latent, 0, "euler", "simple", 8, 1.0,
            sigmas_override=_schedule(8),
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["sigmas"].numel(), 9)
        self.assertEqual(captured[0]["transition_step"], 3)
        # the widget schedule helper stays untouched
        self.assertEqual(base.schedule_calls, [])

    def test_transition_clamps_to_the_external_schedule(self):
        base = _StubBase()
        mods, captured = _capture_mods()
        engine._dual_stage(
            base, mods, config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 50}),
            None, _const_model(), None, {"samples": torch.zeros(1)}, 0,
            "euler", "simple", 8, 1.0, sigmas_override=_schedule(8),
        )
        self.assertEqual(captured[0]["transition_step"], 7)

    def test_replacement_threads_the_sigmas_through(self):
        base = _StubBase()
        mods, captured = _capture_mods()
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 3})
        replacement = engine.make_replacement(base, mods, settings, None, sigmas_override=_schedule(8))
        replacement(_const_model(), None, {"samples": torch.zeros(1)}, 0, "euler", "simple", 8, 1.0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["sigmas"].numel(), 9)

    def test_installed_sampler_passes_sigmas_and_restores(self):
        base = _StubBase()
        mods, captured = _capture_mods()
        settings = config.SelfLiftSettings.from_kwargs({"selflift_transition_step": 3})
        original = base._sample_h3
        with engine.installed_sampler(settings, None, mods=mods, base_module=base,
                                      sigmas=_schedule(8)) as installed:
            self.assertTrue(installed)
            base._sample_h3(_const_model(), None, {"samples": torch.zeros(1)},
                            0, "euler", "simple", 8, 1.0)
        self.assertEqual(len(captured), 1)
        self.assertIs(base._sample_h3.__func__, original.__func__)

    def test_disabled_settings_ignore_the_sigmas_input(self):
        base = _StubBase()
        mods, captured = _capture_mods()
        settings = config.SelfLiftSettings(enabled=False)
        with engine.installed_sampler(settings, None, mods=mods, base_module=base,
                                      sigmas=_schedule(8)) as installed:
            self.assertFalse(installed)
            base._sample_h3(_const_model(), None, {"samples": torch.zeros(1)},
                            0, "euler", "simple", 8, 1.0)
        self.assertEqual(captured, [])
        result = base._sample_h3(_const_model(), None, {"samples": torch.zeros(1)},
                                 0, "euler", "simple", 8, 1.0)
        self.assertEqual(result.get("samples"), "single-stage")


@unittest.skipIf(torch is None, "torch unavailable")
class ScheduleSourceTests(unittest.TestCase):
    """Which schedule actually drives the run (v1.3.1 diagnostics)."""

    def test_external_table_wins_over_the_step_widget(self):
        # 10 entries -> 9 steps, while the widget still says 4.
        self.assertEqual(
            node._effective_steps({"steps": 4}, torch.linspace(1.0, 0.0, 10)), 9
        )

    def test_empty_or_missing_table_falls_back_to_the_widget(self):
        self.assertEqual(node._effective_steps({"steps": 4}, None), 4)
        self.assertEqual(node._effective_steps({"steps": 4}, torch.zeros(0)), 4)

    def test_source_text_names_the_external_table(self):
        text = node._step_source({"steps": 4}, torch.linspace(1.0, 0.0, 10), 9)
        self.assertIn("selflift_sigmas", text)
        self.assertIn("10 entries -> 9 steps", text)

    def test_source_text_names_the_widgets_when_no_table(self):
        text = node._step_source(
            {"steps": 4, "scheduler": "simple", "denoise": 1.0}, None, 4
        )
        self.assertIn("`steps`=4", text)
        self.assertIn("scheduler=simple", text)
        self.assertNotIn("selflift_sigmas", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
