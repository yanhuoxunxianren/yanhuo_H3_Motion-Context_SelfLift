"""v1.11.0 条件卡提示词的「只读 / 编辑」。

上游对 ``prompt_N`` 端口是"完全接管"，一共三道：建卡灌值 + readOnly、500ms
镜像轮询、排队时 ``_apply_per_clip_prompt_overrides`` 整体覆盖。前两道由前端
``prompt_edit`` 标记绕开，第三道由本包 ``node._edited_prompt_indices`` +
``extend_with_selflift`` 摘键拦下——父类的语义是「None / 空串不覆盖」，
所以摘掉 prompt_N 就等于保住卡片里改过的文本。

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_prompt_edit.py
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

PKG = sys.modules["yanhuo_selflift"]

from yanhuo_selflift import engine, node  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _state(clips, prompt_edit=None):
    payload = {"clips": clips}
    if prompt_edit is not None:
        payload["yanhuo_global"] = {"prompt_edit": prompt_edit}
    return json.dumps(payload, ensure_ascii=False)


def _clip(cid, prompt=""):
    return {"id": cid, "prompt": prompt, "seed": 1, "duration": 5}


# ---------------------------------------------------------------------------
# _edited_prompt_indices
# ---------------------------------------------------------------------------
class EditedIndicesTests(unittest.TestCase):
    def test_no_flags_means_nothing_is_dropped(self):
        raw = _state([_clip("clip_1"), _clip("clip_2")])
        self.assertEqual(node._edited_prompt_indices(raw), [])

    def test_flags_map_to_one_based_indices(self):
        raw = _state(
            [_clip("clip_1"), _clip("clip_2"), _clip("clip_3")],
            {"clip_1": True, "clip_3": True},
        )
        self.assertEqual(node._edited_prompt_indices(raw), [1, 3])

    def test_clips_without_an_id_fall_back_to_position(self):
        raw = _state([{"prompt": "a"}, {"prompt": "b"}], {"clip_2": True})
        self.assertEqual(node._edited_prompt_indices(raw), [2])

    def test_falsy_flags_are_ignored(self):
        raw = _state([_clip("clip_1")], {"clip_1": False})
        self.assertEqual(node._edited_prompt_indices(raw), [])

    def test_unknown_ids_are_ignored(self):
        raw = _state([_clip("clip_1")], {"clip_9": True})
        self.assertEqual(node._edited_prompt_indices(raw), [])

    def test_malformed_or_foreign_input_is_safe(self):
        for raw in (None, "", "not json", "[]", "{}",
                    json.dumps({"clips": "nope", "yanhuo_global": {"prompt_edit": {"a": True}}}),
                    json.dumps({"clips": [], "yanhuo_global": "nope"}),
                    json.dumps({"clips": [], "yanhuo_global": {"prompt_edit": "nope"}})):
            self.assertEqual(node._edited_prompt_indices(raw), [])

    def test_non_dict_clips_entries_are_skipped(self):
        raw = _state(["oops", _clip("clip_2")], {"clip_2": True})
        self.assertEqual(node._edited_prompt_indices(raw), [2])


# ---------------------------------------------------------------------------
# extend_with_selflift 摘键
# ---------------------------------------------------------------------------
class PayloadDropTests(unittest.TestCase):
    """编辑态的片段：prompt_N 必须在进父类之前从 payload 里消失。"""

    def _run(self, **kwargs):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        instance = node.YanhuoH3MotionContextSelfLift()
        seen = {}
        installed = {}
        saved = (node._BASE_EXTENDER.extend, node.invalidate_on_plan_change,
                 engine.installed_sampler, engine._schedule)

        def fake_extend(self, **payload):
            seen.update(payload)
            return "ok"

        def fake_invalidate(settings, *args, **kwargs_):
            return False

        @contextlib.contextmanager
        def fake_install(settings, vae, **rest):
            installed.update(rest)
            yield True

        def fake_schedule(model, scheduler, steps, denoise):
            raise AssertionError("schedule helper must not be reached")

        node._BASE_EXTENDER.extend = fake_extend
        node.invalidate_on_plan_change = fake_invalidate
        engine.installed_sampler = fake_install
        engine._schedule = fake_schedule
        try:
            result = instance.extend_with_selflift(**kwargs)
        finally:
            (node._BASE_EXTENDER.extend, node.invalidate_on_plan_change,
             engine.installed_sampler, engine._schedule) = saved
        return result, seen

    def _base_kwargs(self, **extra):
        kwargs = dict(
            model=None,
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

    def test_edited_card_keeps_its_own_prompt(self):
        raw = _state([_clip("clip_1", "我改过的提示词")], {"clip_1": True})
        _result, seen = self._run(**self._base_kwargs(clips_json=raw, prompt_1="端口提示词"))
        # 父类拿到 None 就不覆盖 —— 所以键必须整个消失。
        self.assertNotIn("prompt_1", seen)
        self.assertEqual(seen["clips_json"], raw)

    def test_readonly_card_still_receives_the_port_value(self):
        raw = _state([_clip("clip_1", "卡片原文")])
        _result, seen = self._run(**self._base_kwargs(clips_json=raw, prompt_1="端口提示词"))
        self.assertEqual(seen.get("prompt_1"), "端口提示词")

    def test_only_the_edited_cards_are_dropped(self):
        raw = _state(
            [_clip("clip_1"), _clip("clip_2"), _clip("clip_3")],
            {"clip_2": True},
        )
        _result, seen = self._run(
            **self._base_kwargs(clips_json=raw, prompt_1="a", prompt_2="b", prompt_3="c")
        )
        self.assertEqual(seen.get("prompt_1"), "a")
        self.assertNotIn("prompt_2", seen)
        self.assertEqual(seen.get("prompt_3"), "c")

    def test_unconnected_port_is_untouched(self):
        raw = _state([_clip("clip_1")], {"clip_1": True})
        _result, seen = self._run(**self._base_kwargs(clips_json=raw))
        self.assertNotIn("prompt_1", seen)

    def test_edited_card_survives_repeated_runs(self):
        """连跑多次都不会被端口值悄悄塞回来。"""
        raw = _state([_clip("clip_1", "改过的")], {"clip_1": True})
        for _ in range(3):
            _result, seen = self._run(
                **self._base_kwargs(clips_json=raw, prompt_1="端口提示词")
            )
            self.assertNotIn("prompt_1", seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
