# -*- coding: utf-8 -*-
"""build 期闸门回归测试。

背景：注入块是「写死在 Python 字符串里的一大段 JS」。只要在某个字符串里误写了 Python
表达式（例如 `"1 1 " + str(YANHUO_PREVIEW_BASIS) + "px"`），它会**原样落进生成文件**，
而 `node --check` 与「加载模块」都发现不了 —— `str(x)` 语法上是合法的函数调用，只有
节点真的去渲染预览时才会抛 `ReferenceError: str is not defined`，届时整个 UI 注入已挂。

所以：筛查必须在 build 期做（build_frontend._assert_clean_js），且要有常驻测试守着，
确保这个闸门本身没被悄悄删掉/绕过。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT / "tools"))

try:
    import build_frontend as bf
except Exception as exc:  # pragma: no cover - 只在姊妹包缺失时触发
    bf = None
    _IMPORT_ERROR = exc


def skip_if_unavailable(test):
    def wrapper(self, *args, **kwargs):
        if bf is None:
            self.skipTest(f"build_frontend 不可用: {_IMPORT_ERROR}")
        return test(self, *args, **kwargs)
    return wrapper


class BuildGuardTests(unittest.TestCase):
    """build 本身必须成功 —— 闸门在写盘前 raise SystemExit，能构建成功即代表干净。"""

    @skip_if_unavailable
    def test_build_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.js"
            bf.build(bf.SOURCE, out)
            self.assertTrue(out.stat().st_size > 100000)

    @skip_if_unavailable
    def test_no_python_residue_in_output(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.js"
            bf.build(bf.SOURCE, out)
            text = out.read_text(encoding="utf-8")
        # 这些模式一旦出现就是"Python 混进 JS"，跑起来必然 ReferenceError。
        for pattern, why in (
            (r"\bstr\(", "str() 调用"),
            (r"__SKIN_CSS__|__BOTTOM_GUTTER__|__PREVIEW_(?:BASIS|MIN_W|H)__", "占位符未替换"),
            (r"=\s*(?:None|True|False)\s*[,;)]", "Python 字面量赋值"),
        ):
            self.assertIsNone(re.search(pattern, text), f"生成文件里有 {why}")

    @skip_if_unavailable
    def test_guard_rejects_injected_python(self):
        """闸门本身有效：喂一段含 str() 的文本必须 SystemExit。"""
        with self.assertRaises(SystemExit) as ctx:
            bf._assert_clean_js("const flex = \"1 1 \" + str(YANHUO_PREVIEW_BASIS) + \"px\";")
        self.assertIn("str()", str(ctx.exception))

    @skip_if_unavailable
    def test_guard_ignores_upstream_english_comments(self):
        """上游英文注释里的 'True' / 'None' 不能误伤。"""
        bf._assert_clean_js("// True only after an explicit .ext Load has imposed it")

    @skip_if_unavailable
    def test_build_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.js", Path(td) / "b.js"
            bf.build(bf.SOURCE, a)
            bf.build(bf.SOURCE, b)
            self.assertEqual(a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"))


class V181LayoutTests(unittest.TestCase):
    """v1.8.1 的两处修复必须真的写进产物（防止改动被后续 build 覆盖回去）。"""

    @classmethod
    def setUpClass(cls):
        if bf is None:
            raise unittest.SkipTest(f"build_frontend 不可用: {_IMPORT_ERROR}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "out.js"
        bf.build(bf.SOURCE, cls.out)
        cls.text = cls.out.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cards_uses_flex_fill(self):
        """cards 必须是 flex 填充（不是任何像素估算）。"""
        self.assertRegex(self.text, r'runtime\.cards\.style\.flex = "1 1 0"')
        self.assertRegex(self.text, r'runtime\.cards\.style\.height = "auto"')
        self.assertRegex(self.text, r'runtime\.cards\.style\.minHeight')

    def test_siblings_pinned_nonshrinkable(self):
        self.assertRegex(self.text, r'__yanhuoEl\.style\.flexShrink = "0"')

    def test_bottom_gutter_and_doubled_safe_px(self):
        self.assertRegex(self.text, r'runtime\.root\.style\.paddingBottom = `\$\{yanhuoBottomGutter\(\)\}px`')
        self.assertIn("YANHUO_BOTTOM_GUTTER = 14", self.text)
        self.assertEqual(bf.YANHUO_BOTTOM_GUTTER, 14)
        self.assertEqual(bf.BOTTOM_SAFE_PX, 40)  # 用户要求"再翻倍"
        self.assertRegex(self.text, r"- 7 - 40 - yanhuoStripExtra")  # 预算同步生效

    def test_fit_panel_is_bounded(self):
        for constant in ("YANHUO_FIT_MAX_PX = 1500", "YANHUO_FIT_MAX_STEP = 900",
                         "YANHUO_FIT_MAX_PASS = 6"):
            self.assertIn(constant, self.text)
        # 拖拽中不插手，避免和用户拉扯
        self.assertIn("resizing_node", self.text)
        self.assertIn("pointer_is_down", self.text)

    def test_draft_and_final_preview_share_dimensions(self):
        """草稿/成片必须同一套尺寸常数 —— 用户要求长宽对齐。"""
        self.assertEqual(bf.YANHUO_PREVIEW_BASIS, 300)
        self.assertEqual(bf.YANHUO_PREVIEW_MIN_W, 200)
        self.assertEqual(bf.YANHUO_PREVIEW_H, 200)
        self.assertRegex(self.text, r'draftBox\.style\.flex = "1 1 300px"')
        self.assertRegex(self.text, r'finalBox\.style\.flex = "1 1 300px"')
        self.assertRegex(self.text, r'draftImg\.style\.height = "200px"')
        self.assertRegex(self.text, r'finalVideo\.style\.height = "200px"')
        # 旧的"草稿 320 / 成片 480 + maxHeight 240"必须已消失
        self.assertNotIn('draftBox.style.flex = "0 1 320px"', self.text)
        self.assertNotIn('finalBox.style.flex = "0 1 480px"', self.text)
        self.assertNotIn('finalVideo.style.maxHeight = "240px"', self.text)


class V1120PerClipTests(unittest.TestCase):
    """v1.12.0 逐段端口搬家：透传层必须真的写进产物。

    逐段端口搬去「逐段输入集合」节点之后，主节点的 UI 全靠这一层把
    findInputEntry / 取值函数重定向到集合节点。它一旦被后续 build 覆盖回去，
    症状是"接了集合节点但卡片没反应"，而且不报错——必须有常驻测试守着。
    """

    @classmethod
    def setUpClass(cls):
        if bf is None:
            raise unittest.SkipTest(f"build_frontend 不可用: {_IMPORT_ERROR}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "out.js"
        bf.build(bf.SOURCE, cls.out)
        cls.text = cls.out.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_passthrough_is_injected(self):
        for needle in (
            "YANHUO_PER_CLIP_RE",
            "yanhuoPerClipSource",
            "yanhuoExternalPromptValue",
            "yanhuoExternalDurationValue",
            '"per_clip_inputs"',
        ):
            self.assertIn(needle, self.text, needle)

    def test_find_input_entry_is_redirected(self):
        """透传只认逐段端口名，且不能误伤全局 ref_audio_1..3。"""
        self.assertIn("findInputEntry = function (node, name)", self.text)
        # v1.13.0：ref_audios_N（音频批次）也属于逐段端口。
        self.assertIn(
            r"/^(?:ref_pack|prompt|duration|ref_audios)_\d+$|^ref_audio_\d+_\d+$/",
            self.text,
        )
        self.assertIn("__yanhuoFindInputEntry", self.text)

    def test_dynamic_add_remove_are_guarded(self):
        """少了这两道护栏，主节点会被重新撑长 / 删错插座。"""
        self.assertIn("__yanhuoAddDynamicRefInput", self.text)
        self.assertIn("__yanhuoRemoveDynamicRefInput", self.text)

    def test_collector_frontend_ships_with_the_package(self):
        path = PKG_ROOT / "web" / "perclip_collector.js"
        self.assertTrue(path.is_file(), f"缺少 {path}")
        text = path.read_text(encoding="utf-8")
        self.assertIn("YanhuoH3PerClipInputs", text)
        # 三条硬规则：不断已连的线、编号即寻址、接线数顶高片段数。
        self.assertIn("clip_count", text)
        self.assertIn("MAX_CLIPS = 32", text)

    def test_collector_uses_a_single_audio_batch_port(self):
        """v1.13.0：级联的 ref_audio_N_k 换成一个 ref_audios_N。"""
        path = PKG_ROOT / "web" / "perclip_collector.js"
        text = path.read_text(encoding="utf-8")
        self.assertIn("`ref_audios_${index}`", text)
        self.assertNotIn("addSocket(node, name, \"AUDIO\"", text)
        # 老工作流的空级联插座要清理（连着线的保留）。
        self.assertIn("ref_audio_${i}_${k}", text)


class V1121FixTests(unittest.TestCase):
    """v1.12.1 四个线上问题的修复必须真的写进产物。

    这四条都是「改之前不报错、只是行为不对」的类型，靠肉眼回归一定会漏，
    所以每条都钉一个断言在 build 产物上。
    """

    @classmethod
    def setUpClass(cls):
        if bf is None:
            raise unittest.SkipTest(f"build_frontend 不可用: {_IMPORT_ERROR}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "out.js"
        bf.build(bf.SOURCE, cls.out)
        cls.text = cls.out.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # --- 问题 1&2：旧工作流残留的空插座把集合节点挡死 -------------------
    def test_legacy_sockets_are_pruned_on_load(self):
        """两个生命周期点都要清理：新建 + 载入工作流。"""
        self.assertIn("function yanhuoPruneLegacyPerClipInputs(node)", self.text)
        # onConfigure（载入工作流时插座正是从保存的 JSON 里恢复出来的）
        self.assertRegex(self.text, r"node\.onConfigure = function \(info\) \{\n\s*yanhuoPruneLegacyPerClipInputs\(this\);")
        # onNodeCreated（拖出来的新节点）
        self.assertRegex(self.text, r"\n\s*yanhuoPruneLegacyPerClipInputs\(this\);\n\s*const runtime = buildUi\(this\);")

    def test_prune_keeps_wired_sockets(self):
        """只删没接线的 —— 删掉用户还在用的直连会丢数据。"""
        self.assertIn("if (input.link !== null && input.link !== undefined) continue;", self.text)

    def test_unconnected_own_socket_does_not_shadow_the_collector(self):
        """透传的核心规则：主节点上那条没接线就不能抢集合节点。"""
        self.assertIn("if (own && inputConnected(own.input)) return own;", self.text)
        # 找不到集合节点时才退回主节点自己的（可能是有线的旧直连）插座。
        self.assertIn("return remote || own || null;", self.text)

    def test_external_value_never_resolves_a_foreign_slot(self):
        """取值时 node 与 slot 必须同源，否则会解析到不相干的另一条线上。"""
        for needle in (
            "if (own && inputConnected(own.input)) {\n"
            "        const local = __yanhuoOrigConnectedPromptValue(node, clipIndex);",
            "if (own && inputConnected(own.input)) {\n"
            "        const local = __yanhuoOrigConnectedDurationValue(node, clipIndex);",
        ):
            self.assertIn(needle, self.text, needle[:60])

    def test_proxy_nodes_resolve_through_their_input_chain(self):
        """Get / Set 这类代理节点要先顺着输入链走，不能先信自己那个 combo 令牌。"""
        self.assertIn("function yanhuoLooksLikeProxy(source)", self.text)
        self.assertIn("textWidgetValueFromNode = function (source, depth = 0, seenNodes = null)", self.text)

    def test_refresh_reports_failure_instead_of_silently_doing_nothing(self):
        self.assertIn('refresh.textContent = "取不到值";', self.text)

    # --- 问题 1：未连接时提示词框必须可编辑 -----------------------------
    def test_prompt_box_readonly_is_explicit(self):
        """readOnly 必须每次都显式给值：没连端口 -> false。"""
        self.assertIn("const editable = !connected || yanhuoPromptEditable(rt, clip, index);", self.text)
        self.assertIn("box.readOnly = !editable;", self.text)
        # 旧写法：只在 connected 时才动 readOnly，未连接时残留 true 就永远改不动。
        self.assertNotIn("if (box && connected && clip) {", self.text)

    # --- 问题 4：编辑态标记必须挂在 runtime 上 -------------------------
    def test_edit_flags_survive_a_state_replacement(self):
        """后端 onExecuted 返回的 clips_json 是 _state_json() 重建的，
        里面没有 yanhuo_global —— 标记只挂 runtime.state 上会在那一刻丢掉。"""
        self.assertIn("runtime.__yanhuoPromptEdit", self.text)
        self.assertIn("function yanhuoPromptEditKeys(clip, index)", self.text)
        # 双写：硬标记（抗 state 替换）+ 持久化（后端 node.py 只读它）
        self.assertIn("runtime.__yanhuoPromptEdit[key] = true;", self.text)
        self.assertIn("if (g) g.prompt_edit[key] = true;", self.text)

    def test_scan_cards_pins_the_runtime_on_the_node(self):
        """connectedExternalPromptValue 只认 node.__h3Extender，挂载落后就整体失效。"""
        self.assertIn("if (node && !node.__h3Extender) node.__h3Extender = runtime;", self.text)

    # --- 问题 3：静止状态不许把节点撑长 ---------------------------------
    def test_static_state_never_grows_the_node(self):
        """没有预览行时只更新基准高度，绝不 setSize 长高。"""
        self.assertIn("if (previewExtra === 0) {", self.text)
        self.assertIn("runtime.__yanhuoRestH", self.text)
        self.assertIn("runtime.__yanhuoLoanPx", self.text)
        # 预览收起后把借的高度还回去
        self.assertIn("const targetH = Math.round(curH - loan);", self.text)


if __name__ == "__main__":
    unittest.main()
