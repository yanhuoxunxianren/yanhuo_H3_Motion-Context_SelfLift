# -*- coding: utf-8 -*-
"""
SVG 自检：越界 / 框重叠 / 文字溢出 / emoji 残留。
用法：  python docs/images/check_svg.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_svg as B  # noqa: E402

# 粗略字宽：中文/全角按 1.0em，ASCII 按 0.55em
def text_width(s, size):
    w = 0.0
    for ch in s:
        w += size if ord(ch) > 0x2E80 else size * 0.55
    return w


def check(name, svg, W, H, boxes, frames=()):
    problems = []

    # 1) emoj
    for m in re.finditer(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", svg):
        problems.append(f"含 emoji/符号 {m.group()!r}（SVG 里可能渲染成豆腐块）")

    # 2) 框越界 + 3) 框重叠
    for b in boxes:
        if b.x < 0 or b.y < 42 or b.right > W or b.bottom > H:
            problems.append(f"框 {b.bid} 越界: ({b.x},{b.y})-({b.right},{b.bottom}) 画布 {W}x{H}")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, c = boxes[i], boxes[j]
            ox = min(a.right, c.right) - max(a.x, c.x)
            oy = min(a.bottom, c.bottom) - max(a.y, c.y)
            if ox > 2 and oy > 2:
                problems.append(f"框重叠: {a.bid} × {c.bid} ({ox:.0f}x{oy:.0f}px)")

    # 4) 文字溢出框宽
    for b in boxes:
        for ln, size, _, _ in B._text_lines(b):
            tw = text_width(ln, size)
            if tw > b.w - 14:
                problems.append(f"文字溢出 {b.bid}: 「{ln}」约 {tw:.0f}px > 框内宽 {b.w - 14}px")

    # 5) 分组框必须包住成员
    for fr in frames:
        for b in boxes:
            if b.x >= fr.x and b.right <= fr.right and b.y >= fr.y and b.bottom <= fr.bottom:
                continue
            if b.x < fr.right and b.right > fr.x and b.y < fr.bottom and b.bottom > fr.y:
                problems.append(f"分组框 {fr.title[:12]}… 与成员 {b.bid} 边界相交")

    status = "OK  " if not problems else "FAIL"
    print(f"{status} {name}")
    for p in problems:
        print(f"       - {p}")
    return not problems


def check_edges(name, layout):
    """连线穿透检测：一条边不应穿过与自己无关的框。"""
    boxes = layout["boxes"]
    bmap = {b.bid: b for b in boxes}
    problems = []
    for e in layout["edges"]:
        ends = {e.a, e.b}
        for (p1, p2) in B.edge_segments(e, bmap):
            steps = 160
            for k in range(steps + 1):
                t = k / steps
                x = p1[0] + (p2[0] - p1[0]) * t
                y = p1[1] + (p2[1] - p1[1]) * t
                for b in boxes:
                    if b.bid in ends:
                        continue
                    # 缩进 4px，忽略擦边
                    if (b.x + 4 < x < b.right - 4) and (b.y + 4 < y < b.bottom - 4):
                        problems.append(
                            f"边 {e.a}→{e.b} 穿过框 {b.bid} @({x:.0f},{y:.0f})"
                        )
                        break
                else:
                    continue
                break
    # 同一条边只报一次
    seen, uniq = set(), []
    for p in problems:
        key = p.split(" @")[0]
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def main():
    ok = True
    for name, fn in B.DIAGRAMS:
        svg = fn()
        lay = dict(B._LAST)
        ok &= _check_by_rects(name, svg, lay["W"], lay["H"])

        # 文字溢出（需要 boxes，这里能拿到）
        over = []
        for b in lay["boxes"]:
            for ln, size, _, _ in B._text_lines(b):
                if text_width(ln, size) > b.w - 14:
                    over.append(f"{b.bid}: 「{ln}」约 {text_width(ln, size):.0f}px > {b.w - 14}px")
        if over:
            ok = False
            print(f"FAIL {name}  文字溢出:")
            for p in over:
                print(f"       - {p}")

        bad = check_edges(name, lay)
        if bad:
            ok = False
            print(f"FAIL {name}  连线穿透:")
            for p in bad:
                print(f"       - {p}")

    print("\nALL OK" if ok else "\n有问题，见上")
    return 0 if ok else 1


def _check_by_rects(name, svg, W, H):
    """不依赖 boxes 变量的兜底检测：解析 SVG 里的 rect 做越界/重叠检查。"""
    rects = []
    for m in re.finditer(
        r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([-\d.]+)" height="([-\d.]+)" '
        r'rx="9"[^/]*?fill="(#[0-9a-fA-F]{6})"',
        svg,
    ):
        x, y, w, h = (float(m.group(i)) for i in (1, 2, 3, 4))
        rects.append((x, y, x + w, y + h))
    problems = []
    for (x1, y1, x2, y2) in rects:
        if x1 < 0 or y1 < 42 or x2 > W or y2 > H:
            problems.append(f"rect 越界 ({x1},{y1})-({x2},{y2})")

    # 背景 rect 高度为整幅，先剔除
    inner = [r for r in rects if not (r[0] == 0 and r[1] == 0)]
    for i in range(len(inner)):
        for j in range(i + 1, len(inner)):
            a, c = inner[i], inner[j]
            ox = min(a[2], c[2]) - max(a[0], c[0])
            oy = min(a[3], c[3]) - max(a[1], c[1])
            if ox > 2 and oy > 2:
                problems.append(f"rect 重叠 ({a[0]},{a[1]})×({c[0]},{c[1]}) {ox:.0f}x{oy:.0f}px")

    for m in re.finditer(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", svg):
        problems.append(f"含 emoji/符号 {m.group()!r}")

    status = "OK  " if not problems else "FAIL"
    print(f"{status} {name}  ({len(rects)} rects)")
    for p in problems:
        print(f"       - {p}")
    return not problems


if __name__ == "__main__":
    sys.exit(main())
