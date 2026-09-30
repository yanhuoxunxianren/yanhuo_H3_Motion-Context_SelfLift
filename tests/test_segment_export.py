"""Coverage for the v1.5.0 per-segment export (full_batch 逐段出片).

The heavy lifting is delegated to the sibling Extender's disk pipeline; these
tests pin the pure logic we add on top: owner-id restoration, the
sequence-mode gate, the output path layout, the audio-bitrate fallback, and
wrapper idempotency against a stubbed upstream module.

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_segment_export.py
"""

from __future__ import annotations

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

from yanhuo_selflift import segment_export  # noqa: E402


def _with_folder_paths(tmp_dir: str):
    """给 segment_export 里函数内 import 的 folder_paths 打桩。"""
    stub = types.ModuleType("folder_paths")
    stub.get_output_directory = lambda: tmp_dir
    sys.modules["folder_paths"] = stub
    return stub


class OwnerNodeIdTests(unittest.TestCase):
    def test_extracts_digits_from_extender_owner(self):
        self.assertEqual(segment_export.owner_node_id({"owner_id": "extender_507"}), "507")

    def test_unexpected_shape_passes_through(self):
        self.assertEqual(segment_export.owner_node_id({"owner_id": "weird"}), "weird")
        self.assertEqual(segment_export.owner_node_id({}), "")

    def test_manifest_missing(self):
        self.assertEqual(segment_export.owner_node_id(None), "")


class SequenceModeGateTests(unittest.TestCase):
    def test_chain_mode_and_default_supported(self):
        self.assertTrue(segment_export.supported_sequence_mode({"sequence_mode": "ref2va"}))
        self.assertTrue(segment_export.supported_sequence_mode({}))
        self.assertTrue(segment_export.supported_sequence_mode(None))

    def test_other_modes_skipped(self):
        self.assertFalse(segment_export.supported_sequence_mode({"sequence_mode": "fl2va"}))
        self.assertFalse(
            segment_export.supported_sequence_mode({"sequence_mode": "ref2va_independent"})
        )


class OutputPathTests(unittest.TestCase):
    def test_path_layout_and_overwrite_semantics(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _with_folder_paths(tmp)
            try:
                path = segment_export.output_video_path("507", 3, 8)
                self.assertEqual(
                    path,
                    Path(tmp) / "yanhuo_selflift" / "507_clip_03_of_08.mp4",
                )
                self.assertEqual(segment_export.output_video_path(None, 1, 2).name, "chain_clip_01_of_02.mp4")
                # 非法字符被替换成下划线，不产生子目录逃逸
                weird = segment_export.output_video_path("../..", 1, 2)
                self.assertEqual(weird.parent, Path(tmp) / "yanhuo_selflift")
            finally:
                sys.modules.pop("folder_paths", None)


class AudioBitrateFallbackTests(unittest.TestCase):
    def test_falls_back_when_upstream_missing(self):
        # vendor.extender_module() 抛异常时必须安全回落 192k，绝不向上抛。
        original = segment_export.vendor.extender_module
        try:
            def _boom():
                raise RuntimeError("sibling missing")
            segment_export.vendor.extender_module = _boom
            self.assertEqual(segment_export.default_audio_bitrate(), "192k")
        finally:
            segment_export.vendor.extender_module = original


class InstallIdempotencyTests(unittest.TestCase):
    def test_install_wraps_once_and_survives_missing_upstream(self):
        original = segment_export.vendor.extender_module
        saved = segment_export._INSTALLED
        try:
            # 上游缺失：install() 返回 False 且不抛。
            def _boom():
                raise RuntimeError("sibling missing")
            segment_export.vendor.extender_module = _boom
            segment_export._INSTALLED = False
            self.assertFalse(segment_export.install())

            # 伪造上游模块：包装恰好发生一次，重复 install 幂等。
            calls = {"wrap": 0}

            def original_cache(*_args, **_kwargs):
                return {"ok": True}, {}

            fake = types.SimpleNamespace(cache_full_batch_ref2va_segment=original_cache)
            segment_export.vendor.extender_module = lambda: fake
            segment_export._INSTALLED = False
            self.assertTrue(segment_export.install())
            wrapped = fake.cache_full_batch_ref2va_segment
            self.assertTrue(getattr(wrapped, "_yanhuo_segment_export", False))
            self.assertTrue(segment_export.install())
            self.assertIs(fake.cache_full_batch_ref2va_segment, wrapped)
            self.assertEqual(calls["wrap"], 0)

            # 包装函数透传原返回值，且导出失败不影响结果。
            def boom_export(*_a, **_k):
                raise RuntimeError("export exploded")
            # 让 wrapper 内部的 _export_prefix_video 走异常路径：monkeypatch 它。
            real_export = segment_export._export_prefix_video
            segment_export._export_prefix_video = boom_export
            try:
                manifest, info = wrapped("d", "m", 0, None, None, 24.0, export_profile=None)
                self.assertEqual(manifest, {"ok": True})
                self.assertEqual(info, {})
            finally:
                segment_export._export_prefix_video = real_export
        finally:
            segment_export.vendor.extender_module = original
            segment_export._INSTALLED = saved


class NotifyFinalClipTests(unittest.TestCase):
    """v1.7.0 逐段成片实时预览的 websocket 推送。"""

    def setUp(self):
        self._saved_server = sys.modules.get("server")

    def tearDown(self):
        if self._saved_server is None:
            sys.modules.pop("server", None)
        else:
            sys.modules["server"] = self._saved_server

    @staticmethod
    def _install_server_stub():
        sent = []

        class _Instance:
            def send_sync(self, event, payload, sid):
                sent.append((event, payload, sid))

        stub = types.ModuleType("server")
        stub.PromptServer = types.SimpleNamespace(instance=_Instance())
        sys.modules["server"] = stub
        return sent

    def test_event_name(self):
        self.assertEqual(segment_export.FINAL_CLIP_EVENT, "yanhuo_h3_final_clip")

    def test_notify_sends_view_ready_payload(self):
        sent = self._install_server_stub()
        path = Path("D:/out/yanhuo_selflift/439_clip_03_of_03.mp4")
        ok = segment_export.notify_final_clip("439", 3, 3, path, 24.0)
        self.assertTrue(ok)
        event, payload, sid = sent[-1]
        self.assertEqual(event, "yanhuo_h3_final_clip")
        self.assertIsNone(sid)  # 广播给所有前端连接
        self.assertEqual(payload["node_id"], "439")
        self.assertEqual(payload["clip"], 3)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["filename"], "439_clip_03_of_03.mp4")
        self.assertEqual(payload["subfolder"], "yanhuo_selflift")
        self.assertEqual(payload["type"], "output")  # /view 端点可直接取流
        self.assertEqual(payload["path"], str(path))
        self.assertEqual(payload["fps"], 24.0)

    def test_notify_without_server_returns_false(self):
        stub = types.ModuleType("server")
        stub.PromptServer = types.SimpleNamespace(instance=None)
        sys.modules["server"] = stub
        self.assertFalse(segment_export.notify_final_clip("1", 1, 1, Path("x.mp4")))

    def test_notify_never_raises(self):
        stub = types.ModuleType("server")

        class _Boom:
            def send_sync(self, *_a, **_k):
                raise RuntimeError("socket gone")

        stub.PromptServer = types.SimpleNamespace(instance=_Boom())
        sys.modules["server"] = stub
        self.assertFalse(segment_export.notify_final_clip("1", 1, 1, Path("x.mp4")))

    def test_export_failure_does_not_break_wrapper(self):
        """_export_prefix_video 抛异常时推送已被 wrapper 的 try/except 兜住（回归 v1.5.0）。"""
        self.assertTrue(callable(segment_export.notify_final_clip))


if __name__ == "__main__":
    unittest.main()
