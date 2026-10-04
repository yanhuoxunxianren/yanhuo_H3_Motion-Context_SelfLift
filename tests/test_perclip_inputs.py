"""v1.12.0 逐段输入集合节点。

主节点上的 ref_pack_N / prompt_N / duration_N / ref_audio_N_k 被搬到
``YanhuoH3PerClipInputs`` 上，主节点只留一个 ``per_clip_inputs`` 聚合端口。
本文件锁住三件事：

1. **编号即寻址** —— 第 N 组的端口只作用于第 N 张条件卡，绝不因为前面空着
   就把后面的往前挤（和上游 Prompt Pack Bridge 的"列表压缩"语义相反）。
2. **bundle 是纯搬运** —— 收/发两端都不换算、不排序、不补默认值。
3. **主节点摊平后语义不变** —— 摊平出来的键和以前直接连线的一模一样，
   所以缓存失效、v1.11.0 编辑态提示词这些既有逻辑照旧生效。

    "D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_perclip_inputs.py
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

from yanhuo_selflift import engine, node, perclip_inputs  # noqa: E402


def _state(clips, prompt_edit=None):
    payload = {"clips": clips}
    if prompt_edit is not None:
        payload["yanhuo_global"] = {"prompt_edit": prompt_edit}
    return json.dumps(payload, ensure_ascii=False)


def _clip(cid, prompt=""):
    return {"id": cid, "prompt": prompt, "seed": 1, "duration": 5}


# ---------------------------------------------------------------------------
# 端口名识别
# ---------------------------------------------------------------------------
class PortNameTests(unittest.TestCase):
    def test_per_clip_names_are_recognized(self):
        for name in (
            "ref_pack_1", "prompt_32", "duration_7",
            "ref_audios_1", "ref_audios_32",           # v1.13.0 音频批次
            "ref_audio_3_0", "ref_audio_3_2",          # v1.12.x 旧形态，仍兼容
        ):
            self.assertTrue(perclip_inputs.is_per_clip_name(name), name)

    def test_audio_batch_names_are_recognized_separately(self):
        self.assertTrue(perclip_inputs.is_per_clip_audio_batch_name("ref_audios_1"))
        self.assertTrue(perclip_inputs.is_per_clip_audio_batch_name("ref_audios_32"))
        for name in ("ref_audio_1", "ref_audio_1_0", "ref_audios", "prompt_1", ""):
            self.assertFalse(perclip_inputs.is_per_clip_audio_batch_name(name), name)

    def test_global_ports_are_not_per_clip(self):
        """ref_audio_1..3（全局独立参考音频）绝不能被当成逐段端口搬走。

        这也是逐段音频批次端口必须叫 ref_audios_N 而不是 ref_audio_N 的原因：
        ref_audio_1 已经被全局端口占用，重名会让 bundle 的键静默串线。
        """
        for name in ("ref_audio", "ref_audio_1", "ref_audio_2", "ref_audio_3",
                     "ref_pack", "prompt_pack", "ref_video_1", "ref_video_fps_1",
                     "clips_json", "model_hires", "", None):
            self.assertFalse(perclip_inputs.is_per_clip_name(name), name)

    def test_declared_ports_cover_every_clip(self):
        names = perclip_inputs.per_clip_port_names()
        # 32 组 ×（ref_pack + prompt + duration + ref_audios）= 128
        self.assertEqual(len(names), 32 * 4)
        for index in range(1, 33):
            self.assertIn(f"prompt_{index}", names)
            self.assertIn(f"ref_audios_{index}", names)
        # 组内顺序稳定：集合节点的端口顺序也是按这个来的。
        self.assertEqual(names[:4], [
            "ref_pack_1", "prompt_1", "duration_1", "ref_audios_1",
        ])

    def test_legacy_audio_slot_names_are_still_listed(self):
        """主节点要继续 pop 掉这批名字，父类才会真的不再声明它们。"""
        legacy = perclip_inputs.legacy_ref_audio_slot_names()
        self.assertEqual(len(legacy), 32 * 3)
        self.assertEqual(legacy[:3], ["ref_audio_1_0", "ref_audio_1_1", "ref_audio_1_2"])
        # 新旧两批不能重叠（否则主节点会重复 pop / 集合节点会重复声明）。
        self.assertFalse(set(legacy) & set(perclip_inputs.per_clip_port_names()))


# ---------------------------------------------------------------------------
# bundle 收 / 发
# ---------------------------------------------------------------------------
class BundleTests(unittest.TestCase):
    def test_pack_only_keeps_connected_per_clip_values(self):
        bundle = perclip_inputs.pack_bundle(
            {
                "prompt_2": "第二段",
                "duration_5": 4.0,
                "ref_audio_1_0": {"waveform": None},
                "clip_count": 5,          # 控件，不是端口
                "model_hires": object(),  # 别的主节点端口
                "prompt_1": None,         # 连了但上游没产出 == 没连
            }
        )
        self.assertEqual(
            sorted(bundle), ["duration_5", "prompt_2", "ref_audio_1_0"]
        )

    def test_round_trip_preserves_names_and_values(self):
        source = {"prompt_7": "第七段", "ref_pack_7": {"slots": [1, 2, 3]}}
        packed = dict(perclip_inputs.pack_bundle(source))
        packed["__type__"] = perclip_inputs.PER_CLIP_BUNDLE_TYPE
        mapping, error = perclip_inputs.unpack_bundle(packed)
        self.assertIsNone(error)
        self.assertEqual(mapping, source)

    def test_unpack_rejects_foreign_values(self):
        for bad in ("nope", 42, [1, 2], object()):
            mapping, error = perclip_inputs.unpack_bundle(bad)
            self.assertEqual(mapping, {})
            self.assertTrue(error)

    def test_unpack_rejects_a_foreign_bundle_type(self):
        mapping, error = perclip_inputs.unpack_bundle(
            {"__type__": "SOME_OTHER_PACK", "prompt_1": "x"}
        )
        self.assertEqual(mapping, {})
        self.assertTrue(error)

    def test_unpack_skips_unknown_keys_but_keeps_the_rest(self):
        mapping, _error = perclip_inputs.unpack_bundle(
            {"prompt_1": "a", "totally_bogus": 1, "ref_audio_9": 2}
        )
        self.assertEqual(mapping, {"prompt_1": "a"})

    def test_unpack_drops_none_values(self):
        mapping, _error = perclip_inputs.unpack_bundle({"prompt_1": None, "duration_2": 3.0})
        self.assertEqual(mapping, {"duration_2": 3.0})

    def test_none_bundle_is_not_an_error(self):
        mapping, error = perclip_inputs.unpack_bundle(None)
        self.assertEqual(mapping, {})
        self.assertIsNone(error)

    def test_describe_lists_the_ports(self):
        self.assertEqual(
            perclip_inputs.describe_bundle({"prompt_2": "x", "duration_1": 1.0}),
            "duration_1,prompt_2",
        )


# ---------------------------------------------------------------------------
# 音频批次拆分（v1.13.0）
# ---------------------------------------------------------------------------
class _FakeWaveform:
    """最小可用的波形替身：只需要 shape[0] 和切片。"""

    def __init__(self, batch):
        self.batch = int(batch)
        self.shape = (self.batch, 1, 8)

    def __getitem__(self, item):
        return _SlicedWaveform(getattr(item, "start", item))


class _SlicedWaveform:
    def __init__(self, start):
        self.start = start
        # 切一刀之后批次维就是 1，和真实 torch 张量的行为一致。
        self.shape = (1, 1, 8)

    def __eq__(self, other):
        return isinstance(other, _SlicedWaveform) and other.start == self.start

    def __repr__(self):  # 让断言失败时可读
        return f"<slice@{self.start}>"


def _audio(batch=1, rate=24000):
    return {"waveform": _FakeWaveform(batch), "sample_rate": rate}


class AudioBatchSplitTests(unittest.TestCase):
    def test_single_audio_passes_through_untouched(self):
        audio = _audio(1)
        self.assertEqual(perclip_inputs.split_audio_refs(audio), [audio])

    def test_batch_of_three_splits_in_order(self):
        parts = perclip_inputs.split_audio_refs(_audio(3))
        self.assertEqual(len(parts), 3)
        for slot, part in enumerate(parts):
            self.assertEqual(part["sample_rate"], 24000)
            # 第 i 刀的波形必须正好是批次里的第 i 段（批次顺序即槽位顺序）。
            self.assertEqual(part["waveform"].start, slot)
            self.assertEqual(part["waveform"].shape, (1, 1, 8))

    def test_batch_of_two_leaves_the_third_slot_empty(self):
        parts = perclip_inputs.split_audio_refs(_audio(2))
        self.assertEqual(len(parts), 2)

    def test_batch_over_three_is_clamped_with_a_warning(self):
        parts = perclip_inputs.split_audio_refs(_audio(5))
        self.assertEqual(len(parts), 3)

    def test_list_form_takes_each_element_as_one_slot(self):
        a, b, c = _audio(1), _audio(1), _audio(1)
        self.assertEqual(perclip_inputs.split_audio_refs([a, b, c]), [a, b, c])
        self.assertEqual(perclip_inputs.split_audio_refs((a, None, b)), [a, b])

    def test_unusable_input_yields_nothing(self):
        for bad in (None, {}, {"sample_rate": 1}, {"waveform": None}, 42, "audio"):
            self.assertEqual(perclip_inputs.split_audio_refs(bad), [], bad)

    def test_expand_replaces_the_batch_key_with_slots(self):
        mapping = {"prompt_1": "a", "ref_audios_2": _audio(3)}
        perclip_inputs.expand_audio_batches(mapping)
        self.assertNotIn("ref_audios_2", mapping)
        self.assertEqual(sorted(mapping), ["prompt_1", "ref_audio_2_0", "ref_audio_2_1", "ref_audio_2_2"])

    def test_expand_keeps_other_ports_untouched(self):
        mapping = {"ref_pack_1": {"slots": []}, "ref_audios_7": _audio(1)}
        perclip_inputs.expand_audio_batches(mapping)
        self.assertEqual(mapping["ref_pack_1"], {"slots": []})
        self.assertEqual(mapping["ref_audio_7_0"]["sample_rate"], 24000)

    def test_expand_ignores_non_batch_audio_names(self):
        mapping = {"ref_audio_1_0": "旧的直连"}
        perclip_inputs.expand_audio_batches(mapping)
        self.assertEqual(mapping, {"ref_audio_1_0": "旧的直连"})

    def test_collect_splits_a_batch_into_legacy_slot_keys(self):
        cls = PKG.NODE_CLASS_MAPPINGS.get("YanhuoH3PerClipInputs")
        if cls is None:  # pragma: no cover
            self.skipTest("Extender unavailable; the collector is not registered")
        bundle, count = cls().collect(clip_count=2, ref_audios_1=_audio(3))
        # 批次端口本身绝不能漏进 bundle——主节点摊平后父类不认识这个键。
        self.assertNotIn("ref_audios_1", bundle)
        self.assertEqual(bundle["ref_audio_1_0"]["waveform"].start, 0)
        self.assertEqual(bundle["ref_audio_1_1"]["waveform"].start, 1)
        self.assertEqual(bundle["ref_audio_1_2"]["waveform"].start, 2)
        self.assertEqual(count, 1)

    def test_unpack_also_expands_a_hand_made_bundle(self):
        packed = {"__type__": perclip_inputs.PER_CLIP_BUNDLE_TYPE, "ref_audios_3": _audio(2)}
        mapping, error = perclip_inputs.unpack_bundle(packed)
        self.assertIsNone(error)
        self.assertNotIn("ref_audios_3", mapping)
        self.assertEqual(len(mapping), 2)
        self.assertIn("ref_audio_3_0", mapping)
        self.assertIn("ref_audio_3_1", mapping)


# ---------------------------------------------------------------------------
# 集合节点
# ---------------------------------------------------------------------------
class CollectorNodeTests(unittest.TestCase):
    def setUp(self):
        self.cls = PKG.NODE_CLASS_MAPPINGS.get("YanhuoH3PerClipInputs")
        if self.cls is None:  # pragma: no cover
            self.skipTest("Extender unavailable; the collector is not registered")

    def test_collect_returns_the_bundle_and_the_group_count(self):
        bundle, count = self.cls().collect(
            clip_count=8, prompt_1="a", prompt_2="b", ref_audio_5_0={"x": 1}
        )
        self.assertEqual(count, 3)  # 第 1 / 2 / 5 组有接线
        self.assertEqual(bundle["__type__"], perclip_inputs.PER_CLIP_BUNDLE_TYPE)
        self.assertEqual(bundle["prompt_1"], "a")
        self.assertEqual(bundle["prompt_2"], "b")

    def test_collect_with_nothing_connected_is_empty(self):
        bundle, count = self.cls().collect(clip_count=4)
        self.assertEqual(count, 0)
        self.assertEqual(bundle, {"__type__": perclip_inputs.PER_CLIP_BUNDLE_TYPE})

    def test_collector_declares_one_audio_batch_port_per_clip(self):
        """v1.13.0：每个片段只有 ref_audios_N 一个音频端口，不再是 3 个级联。"""
        ports = self.cls.INPUT_TYPES().get("optional", {})
        self.assertEqual(list(ports), list(perclip_inputs.per_clip_port_names()))
        self.assertEqual(ports["ref_audios_1"][0], "AUDIO")
        self.assertEqual(ports["ref_audios_9"][0], "AUDIO")
        for index in range(1, 33):
            for k in range(3):
                self.assertNotIn(f"ref_audio_{index}_{k}", ports)

    def test_output_is_the_bundle_type(self):
        self.assertEqual(
            self.cls.RETURN_TYPES[0], perclip_inputs.PER_CLIP_BUNDLE_TYPE
        )

    def test_clip_count_widget_bounds(self):
        widget = self.cls.INPUT_TYPES()["required"]["clip_count"]
        self.assertEqual(widget[0], "INT")
        self.assertEqual(widget[1]["max"], perclip_inputs.MAX_PER_CLIP_CLIPS)


# ---------------------------------------------------------------------------
# 主节点：把 bundle 摊平回 payload
# ---------------------------------------------------------------------------
class MainNodeUnpackTests(unittest.TestCase):
    def _run(self, **kwargs):
        if node._BASE_EXTENDER is None:  # pragma: no cover
            self.skipTest("sibling Extender unavailable")
        instance = node.YanhuoH3MotionContextSelfLift()
        seen = {}
        saved = (node._BASE_EXTENDER.extend, node.invalidate_on_plan_change,
                 engine.installed_sampler, engine._schedule)

        def fake_extend(self, **payload):
            seen.update(payload)
            return "ok"

        def fake_invalidate(settings, *args, **kwargs_):
            return False

        @contextlib.contextmanager
        def fake_install(settings, vae, **rest):
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

    def _bundle(self, **ports):
        bundle = dict(ports)
        bundle["__type__"] = perclip_inputs.PER_CLIP_BUNDLE_TYPE
        return bundle

    def test_bundle_is_unpacked_into_the_legacy_keys(self):
        """父类看到的必须是原来那套键——这是"语义不变"的核心断言。"""
        bundle = self._bundle(
            prompt_3="第三段提示词",
            duration_3=7.5,
            ref_pack_3={"slots": ["img"]},
            ref_audio_3_1={"audio": True},
        )
        _result, seen = self._run(
            **self._base_kwargs(
                clips_json=_state([_clip("clip_1"), _clip("clip_2"), _clip("clip_3")]),
                per_clip_inputs=bundle,
            )
        )
        self.assertEqual(seen.get("prompt_3"), "第三段提示词")
        self.assertEqual(seen.get("duration_3"), 7.5)
        self.assertEqual(seen.get("ref_pack_3"), {"slots": ["img"]})
        self.assertEqual(seen.get("ref_audio_3_1"), {"audio": True})
        # bundle 端口本身不能漏进 payload —— 父类不认识这个键。
        self.assertNotIn(perclip_inputs.PER_CLIP_BUNDLE_INPUT, seen)

    def test_a_bad_bundle_is_ignored_instead_of_crashing(self):
        _result, seen = self._run(
            **self._base_kwargs(
                clips_json=_state([_clip("clip_1")]),
                per_clip_inputs="not a bundle",
            )
        )
        self.assertNotIn("prompt_1", seen)
        self.assertNotIn(perclip_inputs.PER_CLIP_BUNDLE_INPUT, seen)

    def test_direct_cables_win_over_the_bundle(self):
        """老工作流直接连在主节点上的同名端口优先，bundle 只补缺的。"""
        bundle = self._bundle(prompt_1="来自集合节点")
        _result, seen = self._run(
            **self._base_kwargs(
                clips_json=_state([_clip("clip_1")]),
                prompt_1="主节点直连",
                per_clip_inputs=bundle,
            )
        )
        self.assertEqual(seen.get("prompt_1"), "主节点直连")

    def test_edit_mode_still_drops_a_bundle_prompt(self):
        """v1.11.0 的「编辑」态：就算提示词是集合节点送来的，也不能覆盖卡片。"""
        raw = _state([_clip("clip_1", "我改过的")], {"clip_1": True})
        bundle = self._bundle(prompt_1="集合节点的提示词")
        _result, seen = self._run(
            **self._base_kwargs(clips_json=raw, per_clip_inputs=bundle)
        )
        self.assertNotIn("prompt_1", seen)

    def test_readonly_card_still_gets_the_bundle_prompt(self):
        raw = _state([_clip("clip_1", "卡片原文")])
        bundle = self._bundle(prompt_1="集合节点的提示词")
        _result, seen = self._run(
            **self._base_kwargs(clips_json=raw, per_clip_inputs=bundle)
        )
        self.assertEqual(seen.get("prompt_1"), "集合节点的提示词")

    def test_without_the_collector_nothing_changes(self):
        """不接集合节点 == 以前不接 prompt_N：payload 里干净、行为不变。"""
        _result, seen = self._run(
            **self._base_kwargs(clips_json=_state([_clip("clip_1")]))
        )
        self.assertNotIn("prompt_1", seen)
        self.assertNotIn(perclip_inputs.PER_CLIP_BUNDLE_INPUT, seen)

    def test_collector_output_feeds_the_main_node_end_to_end(self):
        """真跑一遍：集合节点 collect() 的输出直接进 extend_with_selflift。"""
        bundle, _count = PKG.NODE_CLASS_MAPPINGS["YanhuoH3PerClipInputs"]().collect(
            clip_count=2, prompt_2="第二段", duration_2=6.0
        )
        _result, seen = self._run(
            **self._base_kwargs(
                clips_json=_state([_clip("clip_1"), _clip("clip_2")]),
                per_clip_inputs=bundle,
            )
        )
        self.assertEqual(seen.get("prompt_2"), "第二段")
        self.assertEqual(seen.get("duration_2"), 6.0)
        self.assertNotIn("prompt_1", seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
