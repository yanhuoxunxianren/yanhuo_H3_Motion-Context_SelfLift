"""Coverage for the v1.2.0 companion nodes.

Three nodes were added so the chain can be wired end-to-end without leaving
this package:

* ``YanhuoH3RefPackFromImages``  - IMAGE list  -> H3_REF_PACK (feeds ref_pack_N)
* ``YanhuoH3FinalDecodeOutput``  - cache + VAE -> VIDEO (Chinese Final Decode)
* ``YanhuoH3VideoFileLoader``    - written mp4 -> VIDEO (feed the result onward)

Everything heavy is delegated to the sibling Extender package; these tests only
pin the contract we add on top: pack shape, slot cap, Chinese labels, and the
delegation itself.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_companion_nodes.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
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

from yanhuo_selflift import companion_nodes, node  # noqa: E402

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None

COMPANION = (
    "YanhuoH3RefPackFromImages",
    "YanhuoH3FinalDecodeOutput",
    "YanhuoH3VideoFileLoader",
)


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
class RegistrationTests(unittest.TestCase):
    def test_companion_nodes_are_registered(self):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        for name in COMPANION:
            self.assertIn(name, PKG.NODE_CLASS_MAPPINGS, name)
            self.assertIn(name, PKG.NODE_DISPLAY_NAME_MAPPINGS, name)

    def test_display_names_are_chinese(self):
        for name in COMPANION:
            label = PKG.NODE_DISPLAY_NAME_MAPPINGS.get(name)
            if not label:  # pragma: no cover
                continue
            self.assertTrue(_has_cjk(label), f"{name}: {label!r}")

    def test_descriptions_are_chinese(self):
        for name in COMPANION:
            cls = PKG.NODE_CLASS_MAPPINGS.get(name)
            if cls is None:  # pragma: no cover
                continue
            self.assertTrue(_has_cjk(cls.DESCRIPTION), f"{name}: {cls.DESCRIPTION!r}")

    def test_category_is_shared_with_the_main_node(self):
        for name in COMPANION:
            cls = PKG.NODE_CLASS_MAPPINGS.get(name)
            if cls is None:  # pragma: no cover
                continue
            self.assertEqual(cls.CATEGORY, "MiniMax H3/SelfLift", name)


# ---------------------------------------------------------------------------
# 1) IMAGE list -> ref_pack
# ---------------------------------------------------------------------------
class RefPackTests(unittest.TestCase):
    def setUp(self):
        if torch is None:  # pragma: no cover
            self.skipTest("torch unavailable")
        self.cls = companion_nodes.YanhuoH3RefPackFromImages
        self.instance = self.cls()

    def _batch(self, count, size=4):
        return torch.zeros((count, size, size, 3))

    def test_pack_shape_matches_the_sibling_bridge(self):
        pack, count = self.instance.pack(self._batch(3))
        self.assertEqual(count, 3)
        self.assertEqual(pack["type"], "H3_REF_PACK")
        self.assertEqual(pack["version"], 2)
        self.assertEqual(pack["count"], 3)
        self.assertEqual(len(pack["slots"]), 3)
        # Every slot must be a single-image [1, H, W, C] tensor.
        self.assertEqual([tuple(s.shape) for s in pack["slots"]], [(1, 4, 4, 3)] * 3)

    def test_a_single_hw_c_image_is_accepted(self):
        pack, count = self.instance.pack(torch.zeros((4, 4, 3)))
        self.assertEqual(count, 1)
        self.assertEqual(tuple(pack["slots"][0].shape), (1, 4, 4, 3))

    def test_no_input_gives_an_empty_pack(self):
        for value in (None, torch.zeros((0, 4, 4, 3))):
            pack, count = self.instance.pack(value)
            self.assertEqual(count, 0, value)
            self.assertEqual(pack["slots"], [])

    def test_slots_are_capped_at_nine(self):
        with self.assertLogs("yanhuo_h3_selflift", level="WARNING"):
            pack, count = self.instance.pack(self._batch(12))
        self.assertEqual(count, 9)
        self.assertEqual(len(pack["slots"]), 9)
        self.assertEqual(pack["count"], 9)

    def test_non_tensor_input_is_ignored(self):
        pack, count = self.instance.pack("not a tensor")
        self.assertEqual(count, 0)
        self.assertEqual(pack["slots"], [])

    def test_input_port_is_forced_and_described_in_chinese(self):
        spec = self.cls.INPUT_TYPES()["optional"]["images"]
        self.assertEqual(spec[0], "IMAGE")
        self.assertTrue(spec[1]["forceInput"])
        self.assertTrue(_has_cjk(spec[1]["tooltip"]))

    def test_return_names_are_chinese(self):
        self.assertEqual(self.cls.RETURN_TYPES, ("H3_REF_PACK", "INT"))
        self.assertEqual(self.cls.RETURN_NAMES, ("ref_pack", "参考数"))


# ---------------------------------------------------------------------------
# 2) Final Decode bridge
# ---------------------------------------------------------------------------
class FinalDecodeTests(unittest.TestCase):
    def setUp(self):
        self.cls = PKG.NODE_CLASS_MAPPINGS.get("YanhuoH3FinalDecodeOutput")
        if self.cls is None or self.cls._BASE is None:  # pragma: no cover
            self.skipTest("sibling Final Decode unavailable")

    def test_outputs_video_with_a_chinese_name(self):
        self.assertEqual(self.cls.RETURN_TYPES, ("VIDEO",))
        self.assertEqual(self.cls.RETURN_NAMES, ("成片视频",))
        self.assertTrue(self.cls.OUTPUT_NODE)

    def test_port_names_are_untouched(self):
        """Only tooltips change - renaming a port would break saved workflows."""
        parent = self.cls._BASE.INPUT_TYPES()
        own = self.cls.INPUT_TYPES()
        for section in ("required", "optional"):
            self.assertEqual(
                sorted(own.get(section, {})),
                sorted(parent.get(section, {})),
                section,
            )

    def test_known_tooltips_are_chinese(self):
        own = self.cls.INPUT_TYPES()
        checked = 0
        for section in ("required", "optional"):
            for name, entry in own.get(section, {}).items():
                if not isinstance(entry, tuple) or len(entry) != 2:
                    continue
                options = entry[1]
                if not isinstance(options, dict) or "tooltip" not in options:
                    continue
                if name in companion_nodes._FINAL_DECODE_TOOLTIPS:
                    self.assertTrue(_has_cjk(options["tooltip"]), f"{name}: {options['tooltip']!r}")
                    checked += 1
        self.assertGreater(checked, 3)

    def test_non_widget_options_are_preserved(self):
        """forceInput/lazy/default must survive the translation pass."""
        parent = self.cls._BASE.INPUT_TYPES()
        own = self.cls.INPUT_TYPES()
        for section in ("required", "optional"):
            for name, entry in (parent.get(section) or {}).items():
                if not isinstance(entry, tuple) or len(entry) != 2:
                    continue
                if not isinstance(entry[1], dict):
                    continue
                for key, value in entry[1].items():
                    if key == "tooltip":
                        continue
                    self.assertEqual(
                        own[section][name][1].get(key),
                        value,
                        f"{section}.{name}.{key}",
                    )

    def test_export_delegates_to_the_parent(self):
        calls = {}

        class _Impl:
            def export(self, **kwargs):
                calls.update(kwargs)
                return ("video",)

        instance = self.cls.__new__(self.cls)
        instance._impl = _Impl()
        result = instance.export(cache="C", vae="V")
        self.assertEqual(result, ("video",))
        self.assertEqual(calls, {"cache": "C", "vae": "V"})

    def test_is_changed_is_forwarded(self):
        checker = getattr(self.cls._BASE, "IS_CHANGED", None)
        if checker is None:  # pragma: no cover
            self.assertIsNone(self.cls.IS_CHANGED(a=1))
        else:
            self.assertIsNotNone(self.cls.IS_CHANGED)


# ---------------------------------------------------------------------------
# 3) Video file loader
# ---------------------------------------------------------------------------
class VideoLoaderTests(unittest.TestCase):
    def setUp(self):
        self.cls = companion_nodes.YanhuoH3VideoFileLoader
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / "chain_00001_.mp4"
        self.video.write_bytes(b"\x00")

    def test_validate_rejects_an_empty_path(self):
        result = self.cls.validate_inputs("")
        self.assertIsNot(result, True)
        self.assertTrue(_has_cjk(str(result)))

    def test_validate_rejects_a_missing_file(self):
        result = self.cls.validate_inputs("definitely/not/here.mp4")
        self.assertIsNot(result, True)

    def test_validate_accepts_an_existing_absolute_path(self):
        self.assertIs(self.cls.validate_inputs(str(self.video)), True)

    def test_load_returns_the_video_and_the_resolved_path(self):
        seen = {}

        class _StubModule:
            @staticmethod
            def _video_output_from_path(path):
                seen["path"] = path
                return "VIDEO_OBJECT"

        original = self.cls._video_module
        self.cls._video_module = staticmethod(lambda: _StubModule)
        self.addCleanup(lambda: setattr(self.cls, "_video_module", original))
        try:
            video, path = self.cls().load(str(self.video))
        finally:
            pass
        self.assertEqual(video, "VIDEO_OBJECT")
        self.assertEqual(path, str(self.video.resolve()))
        self.assertEqual(seen["path"], str(self.video.resolve()))

    def test_load_raises_a_chinese_error_when_missing(self):
        with self.assertRaises(ValueError) as ctx:
            self.cls().load("nope.mp4")
        self.assertTrue(_has_cjk(str(ctx.exception)))

    def test_return_names_are_chinese(self):
        self.assertEqual(self.cls.RETURN_TYPES, ("VIDEO", "STRING"))
        self.assertEqual(self.cls.RETURN_NAMES, ("视频", "文件路径"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
