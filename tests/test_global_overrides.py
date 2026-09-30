"""Coverage for the v1.3.0 global LoRA / global seed overrides.

The frontend stores the two toggles (plus the global LoRA list and the global
seed) in the top-level ``yanhuo_global`` field of ``clips_json``; per-clip
``loras`` / ``seed`` stay untouched in the workflow. ``node._apply_yanhuo_globals``
rewrites the clips right before the payload reaches the parent's ``extend()``,
so cache invalidation sees the effective values.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_global_overrides.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
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

from yanhuo_selflift import node  # noqa: E402


def _workflow(clips, global_cfg=None):
    payload = {"version": 2, "clips": clips}
    if global_cfg is not None:
        payload["yanhuo_global"] = global_cfg
    return json.dumps(payload, ensure_ascii=False)


class GlobalOverrideTests(unittest.TestCase):
    def test_no_field_is_a_noop(self):
        raw = _workflow([{"id": "clip_1", "seed": 5, "loras": [{"name": "a.safetensors", "strength": 0.5}]}])
        self.assertEqual(node._apply_yanhuo_globals(raw), raw)

    def test_both_toggles_off_is_a_noop(self):
        raw = _workflow(
            [{"id": "clip_1", "seed": 5, "loras": [{"name": "a", "strength": 0.5}]}],
            {"global_lora": {"enabled": False, "loras": [{"name": "b", "strength": 1}]},
             "global_seed": {"enabled": False, "seed": 42}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        self.assertEqual(out["clips"][0]["seed"], 5)
        self.assertEqual(out["clips"][0]["loras"][0]["name"], "a")

    def test_global_lora_replaces_every_clip(self):
        raw = _workflow(
            [
                {"id": "clip_1", "loras": [{"name": "a", "strength": 0.5}, {"name": "b", "strength": 0.7}]},
                {"id": "clip_2", "loras": []},
                {"id": "clip_3"},
            ],
            {"global_lora": {"enabled": True, "loras": [{"name": "g1", "strength": 0.9}]}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        for clip in out["clips"]:
            self.assertEqual(clip["loras"], [{"name": "g1", "strength": 0.9}])

    def test_global_lora_copies_per_clip_no_shared_mutation(self):
        raw = _workflow(
            [{"id": "clip_1"}],
            {"global_lora": {"enabled": True, "loras": [{"name": "g1", "strength": 0.9}]}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        self.assertIsNot(out["clips"][0]["loras"][0], out["yanhuo_global"]["global_lora"]["loras"][0])

    def test_global_lora_drops_empty_names(self):
        raw = _workflow(
            [{"id": "clip_1"}],
            {"global_lora": {"enabled": True, "loras": [
                {"name": "  ", "strength": 1}, {"name": "keep", "strength": 2},
                "garbage", None,
            ]}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        self.assertEqual(out["clips"][0]["loras"], [{"name": "keep", "strength": 2.0}])

    def test_global_lora_clamps_strength(self):
        raw = _workflow(
            [{"id": "clip_1"}],
            {"global_lora": {"enabled": True, "loras": [{"name": "g", "strength": 999}]}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        self.assertEqual(out["clips"][0]["loras"][0]["strength"], 100.0)

    def test_global_seed_replaces_every_clip(self):
        raw = _workflow(
            [{"id": "clip_1", "seed": 1}, {"id": "clip_2", "seed": 2}],
            {"global_seed": {"enabled": True, "seed": 987654321}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        self.assertEqual([clip["seed"] for clip in out["clips"]], [987654321, 987654321])

    def test_global_seed_negative_falls_back_to_noop(self):
        raw = _workflow([{"id": "clip_1", "seed": 7}], {"global_seed": {"enabled": True, "seed": -5}})
        self.assertEqual(node._apply_yanhuo_globals(raw), raw)

    def test_both_toggles_together(self):
        raw = _workflow(
            [{"id": "clip_1", "seed": 1, "loras": []}, {"id": "clip_2", "seed": 2, "loras": []}],
            {"global_lora": {"enabled": True, "loras": [{"name": "g", "strength": 0.3}]},
             "global_seed": {"enabled": True, "seed": 123}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        for clip in out["clips"]:
            self.assertEqual(clip["seed"], 123)
            self.assertEqual(clip["loras"], [{"name": "g", "strength": 0.3}])

    def test_other_clip_fields_survive(self):
        raw = _workflow(
            [{"id": "clip_1", "seed": 1, "prompt": "p", "duration": 5, "validated": True}],
            {"global_seed": {"enabled": True, "seed": 9}},
        )
        out = json.loads(node._apply_yanhuo_globals(raw))
        clip = out["clips"][0]
        self.assertEqual(clip["prompt"], "p")
        self.assertEqual(clip["duration"], 5)
        self.assertTrue(clip["validated"])
        self.assertEqual(clip["id"], "clip_1")

    def test_yanhuo_global_field_is_kept_for_round_trip(self):
        raw = _workflow([{"id": "clip_1"}], {"global_seed": {"enabled": True, "seed": 9}})
        out = json.loads(node._apply_yanhuo_globals(raw))
        self.assertIn("yanhuo_global", out)

    def test_invalid_json_is_returned_unchanged(self):
        for raw in ("not json", "", None, "[1, 2]", '{"clips": "nope"}'):
            self.assertEqual(node._apply_yanhuo_globals(raw), raw)

    def test_non_dict_global_cfg_is_ignored(self):
        raw = _workflow([{"id": "clip_1", "seed": 3}], "not-a-dict")
        self.assertEqual(node._apply_yanhuo_globals(raw), raw)

    def test_legacy_clip_lora_field_is_left_alone(self):
        """The parent migrates legacy `lora` itself; we must not touch clips
        when only unknown toggles are present."""
        raw = _workflow([{"id": "clip_1", "lora": {"name": "legacy", "strength": 0.5}}])
        self.assertEqual(node._apply_yanhuo_globals(raw), raw)


class RoutingTests(unittest.TestCase):
    """extend_with_selflift must apply the override before anything else reads
    the payload (cache invalidation included)."""

    def test_payload_clips_json_is_overridden_before_super(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        import unittest.mock

        captured = {}

        def fake_extend(_self, **kwargs):
            captured.update(kwargs)
            return ("cache", 1, 1, "ok", 0.0, "build")

        class _Settings:
            enabled = False
            sigmas_digest = None

            def uses_external_sigmas(self):
                return False

        original_split = node.split_settings
        node.split_settings = lambda kwargs: (_Settings(), dict(kwargs))
        self.addCleanup(setattr, node, "split_settings", original_split)

        cls = node.YanhuoH3MotionContextSelfLift
        raw = _workflow(
            [{"id": "clip_1", "seed": 1, "loras": []}],
            {"global_lora": {"enabled": True, "loras": [{"name": "g", "strength": 0.4}]}},
        )
        with unittest.mock.patch.object(node._BASE_EXTENDER, "extend", fake_extend), \
                unittest.mock.patch.object(
                    node.engine, "installed_sampler", lambda *a, **k: contextlib.nullcontext()
                ):
            cls().extend_with_selflift(clips_json=raw, vae=None, unique_id="9")
        out = json.loads(captured["clips_json"])
        self.assertEqual(out["clips"][0]["loras"], [{"name": "g", "strength": 0.4}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
