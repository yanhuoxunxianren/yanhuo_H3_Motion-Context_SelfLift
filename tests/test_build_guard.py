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


if __name__ == "__main__":
    unittest.main()
