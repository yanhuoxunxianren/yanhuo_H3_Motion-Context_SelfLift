"""Regression coverage for the Motion-Context + high-res-tiling correction.

Reproduces the exact failure this package exists to fix: when a clip carries
Motion Context *keyframes* alongside *references*, upstream's
``h3_tiling._tile_payload`` leaves the keyframe conditioning latents at full
resolution while the tile layout expects narrowed ones, and MiniMax H3 dies
inside ``all_video_rows[~img_update] = cond_video_rows``.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_tiling_fix.py
"""

from __future__ import annotations

import importlib.util
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

PKG = sys.modules["yanhuo_selflift"]

from yanhuo_selflift import tiling_fix  # noqa: E402

try:
    import torch
    from comfy.ldm.minimax.model import PackedLayout
except ImportError:  # pragma: no cover
    torch = None
    PackedLayout = None

try:
    from yanhuo_selflift import vendor

    TILING = vendor.selflift_modules().tiling
except Exception:  # pragma: no cover
    TILING = None

TEXT_LEN = 8
CHANNELS = 24
FRAMES = 2
HEIGHT = 16
WIDTH = 24
AUDIO_T = 4
# The tile grid below needs >= 4 patch columns for h3_tiling to split at all.
AXIS = 4  # width, because (16 + 1) // 2 < (24 + 1) // 2
START, END = 0, 14


def _build(with_keyframes=True, with_refs=True, drop_cond_entry=False):
    video = torch.zeros(1, CHANNELS, FRAMES, HEIGHT, WIDTH)
    audio = torch.zeros(1, 32, 2, AUDIO_T)
    context = torch.zeros(1, TEXT_LEN, 5120)
    keyframes = None
    refs = None
    cond = []
    if with_keyframes:
        latent = torch.arange(HEIGHT * WIDTH, dtype=torch.float32).reshape(1, 1, 1, HEIGHT, WIDTH)
        latent = latent.expand(1, CHANNELS, 1, HEIGHT, WIDTH).contiguous()
        keyframes = [{"latent": latent, "resolved_frame_index": 0}]
        cond.append(latent)
    if with_refs:
        ref_latent = torch.ones(1, CHANNELS, 1, HEIGHT, WIDTH)
        refs = [{"kind": "image", "latent_h": HEIGHT, "latent_w": WIDTH, "latent": ref_latent}]
        cond.append(ref_latent)
    if drop_cond_entry:
        cond = cond[1:]
    layout = PackedLayout(TEXT_LEN, FRAMES, HEIGHT, WIDTH, AUDIO_T, keyframes=keyframes, refs=refs)
    payload = {
        "keyframes": keyframes,
        "refs": refs,
        "cond_video_latents": cond,
        "layout": layout,
    }
    return payload, context, video, audio


def _declared_cond_rows(layout):
    return sum(b - a for a, b, kind in layout.segments if kind in ("cond", "ref_img"))


def _actual_cond_rows(payload):
    return sum(
        z.shape[0] * z.shape[2] * (z.shape[3] // 2) * (z.shape[4] // 2)
        for z in (payload.get("cond_video_latents") or [])
    )


@unittest.skipIf(torch is None or TILING is None, "torch / comfyui-SelfLift unavailable")
class UpstreamBugTests(unittest.TestCase):
    """Document the failure before the correction, so a future fix cannot regress."""

    def test_upstream_leaves_keyframe_conditioning_at_full_resolution(self):
        payload, context, video, audio = _build()
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertNotEqual(_declared_cond_rows(tiled["layout"]), _actual_cond_rows(tiled))

    def test_upstream_is_fine_without_references(self):
        payload, context, video, audio = _build(with_refs=False)
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertEqual(_declared_cond_rows(tiled["layout"]), _actual_cond_rows(tiled))


@unittest.skipIf(torch is None or TILING is None, "torch / comfyui-SelfLift unavailable")
class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.installed = tiling_fix.ensure_installed(TILING)
        self.assertTrue(self.installed)
        self.assertTrue(tiling_fix.is_installed(TILING))

    def tearDown(self):
        # Runs before any addCleanup, so the assertion observes the restore.
        tiling_fix.restore(TILING)
        self.assertFalse(tiling_fix.is_installed(TILING))

    def test_keyframes_and_references_together_are_consistent(self):
        payload, context, video, audio = _build()
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertEqual(_declared_cond_rows(tiled["layout"]), _actual_cond_rows(tiled))

    def test_keyframe_latents_are_narrowed_to_the_tile(self):
        payload, context, video, audio = _build()
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertEqual(tiled["keyframes"][0]["latent"].shape[-2:], (HEIGHT, END - START))
        self.assertEqual(tiled["cond_video_latents"][0].shape[-2:], (HEIGHT, END - START))

    def test_reference_latents_keep_their_own_grid(self):
        payload, context, video, audio = _build()
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertTrue(torch.equal(tiled["cond_video_latents"][1], payload["refs"][0]["latent"]))

    def test_keyframes_only_still_works(self):
        payload, context, video, audio = _build(with_refs=False)
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertEqual(_declared_cond_rows(tiled["layout"]), _actual_cond_rows(tiled))

    def test_references_only_still_works(self):
        payload, context, video, audio = _build(with_keyframes=False)
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        self.assertEqual(_declared_cond_rows(tiled["layout"]), _actual_cond_rows(tiled))

    def test_target_rows_track_the_tile(self):
        payload, context, video, audio = _build()
        tiled = TILING._tile_payload(payload, context, video, audio, AXIS, START, END)
        layout = tiled["layout"]
        self.assertEqual(tuple(layout.position_ids.shape), (layout.seq_len, 3))

        def segment(layout_, kind):
            for start, stop, this in layout_.segments:
                if this == kind:
                    return layout_.position_ids[start:stop]
            raise AssertionError(kind)

        full = payload["layout"]
        # the target video block shares the tile grid: 7 of the 12 patch columns
        self.assertEqual(segment(layout, "video")[:, 2].unique().numel(), (END - START) // 2)
        self.assertEqual(segment(full, "video")[:, 2].unique().numel(), WIDTH // 2)
        # the keyframe cond block shares the target grid, so it narrows too
        self.assertEqual(segment(layout, "cond")[:, 2].unique().numel(), (END - START) // 2)
        # references keep their own grid, so their rows are copied verbatim
        self.assertTrue(torch.equal(segment(layout, "ref_img"), segment(full, "ref_img")))

    def test_inconsistent_payload_raises_tiling_incompatible(self):
        payload, context, video, audio = _build(drop_cond_entry=True)
        with self.assertRaises(tiling_fix.TilingIncompatible):
            TILING._tile_payload(payload, context, video, audio, AXIS, START, END)

    def test_tiled_forward_degrades_to_full_frame(self):
        payload, context, video, audio = _build(drop_cond_entry=True)
        seen = {}

        def executor(streams, timestep, ctx, options, minimax_payload=None, **kwargs):
            seen["streams"] = streams
            return [streams[0] * 2.0, streams[1]]

        streams = [video, audio]
        out = TILING._tiled_forward(executor, streams, 0.5, context, {},
                                    minimax_payload=payload, n_tiles=2)
        self.assertIs(seen["streams"][0], video)
        self.assertEqual(out[0].shape, video.shape)


if __name__ == "__main__":
    unittest.main(verbosity=2)
