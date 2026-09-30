# -*- coding: utf-8 -*-
"""
生成 yanhuo_H3_Motion-Context_SelfLift 的原理图（SVG）。

声明式：每张图只要给出节点坐标与连线路由，脚本负责画框、连线、箭头和标注。
重新生成：  python docs/images/build_svg.py
输出：      docs/images/*.svg

SVG 用固定浅色配色（白底深字），在 GitHub 浅色/深色主题下都能看清。
"""

import os
import xml.etree.ElementTree as ET

FONT = "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"

C = {
    "text":    "#2b2b38",
    "dim":     "#61617a",
    "line":    "#6b6b80",
    "bg":      "#ffffff",
    # 本包 / 主流程
    "own_fill":   "#f3eefc",
    "own_line":   "#b8a1e8",
    # 外部 / 第三方
    "ext_fill":   "#eef2f7",
    "ext_line":   "#8fa3bf",
    # 中性（ComfyUI / 框架）
    "neu_fill":   "#ececed",
    "neu_line":   "#9a9aa8",
    # 强调（关键步骤 / 修正）
    "hot_fill":   "#fff2e2",
    "hot_line":   "#e0a860",
    # 结果 / 输出
    "out_fill":   "#e8f5ec",
    "out_line":   "#79b58c",
}


class Box:
    """一个矩形节点。label 为字符串列表（逐行居中）。"""

    def __init__(self, bid, x, y, w, h, label, kind="own", size=13.0, bold=False, sub=None):
        self.bid = bid
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = [] if label is None else ([label] if isinstance(label, str) else list(label))
        self.kind = kind
        self.size = size
        self.bold = bold
        self.sub = [sub] if isinstance(sub, str) else (list(sub) if sub else [])

    @property
    def cx(self):
        return self.x + self.w / 2.0

    @property
    def cy(self):
        return self.y + self.h / 2.0

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h


class Frame:
    """分组外框：只画一个带标题的容器，不参与连线。"""

    def __init__(self, x, y, w, h, title, kind="own"):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.title = title
        self.kind = kind


class Note:
    """纯文字注释（无边框）。"""

    def __init__(self, x, y, text, size=11.5, dim=True, anchor="start"):
        self.x, self.y, self.text = x, y, text
        self.size = size
        self.dim = dim
        self.anchor = anchor


class Edge:
    """连线。route: h 水平 / v 垂直 / hv 先横后竖 / vh 先竖后横。"""

    def __init__(self, a, b, label="", route="h", dash=False, color=None, label_pos=None):
        self.a, self.b = a, b
        self.label = label
        self.route = route
        self.dash = dash
        self.color = color or C["line"]
        self.label_pos = label_pos  # (x, y) 手动指定标签位置


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def edge_segments(e, bmap):
    """复算一条边的折线段（与 _render_edge 同一套几何），供自检使用。"""
    a, b = bmap[e.a], bmap[e.b]
    if e.route == "h":
        x1, y1 = (a.right, a.cy) if a.cx < b.cx else (a.x, a.cy)
        x2, y2 = (b.x, b.cy) if a.cx < b.cx else (b.right, b.cy)
        return [((x1, y1), (x2, y2))]
    if e.route == "z":
        x1, y1 = (a.right, a.cy) if a.cx < b.cx else (a.x, a.cy)
        x2, y2 = (b.x, b.cy) if a.cx < b.cx else (b.right, b.cy)
        mx = (x1 + x2) / 2.0
        return [((x1, y1), (mx, y1)), ((mx, y1), (mx, y2)), ((mx, y2), (x2, y2))]
    if e.route == "v":
        y1 = a.bottom if a.cy < b.cy else a.y
        y2 = b.y if a.cy < b.cy else b.bottom
        return [((a.cx, y1), (b.cx, y2))]
    if e.route == "hv":
        x1, y1 = (a.right, a.cy) if a.cx < b.cx else (a.x, a.cy)
        x2, y2 = (b.cx, b.y) if a.cy < b.cy else (b.cx, b.bottom)
        return [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2))]
    if e.route == "vh":
        y1 = a.bottom if a.cy < b.cy else a.y
        x2 = b.x if a.cx < b.cx else b.right
        y2 = b.y if a.cy < b.cy else b.bottom
        return [((a.cx, y1), (a.cx, y2)), ((a.cx, y2), (x2, y2))]
    raise ValueError(e.route)


def _text_lines(box):
    """把 label 与 sub 拼成渲染行（sub 用小一号灰字）。"""
    out = [(ln, box.size, box.bold, C["text"]) for ln in box.label]
    out += [(ln, box.size - 1.5, False, C["dim"]) for ln in box.sub]
    return out


def _render_box(box):
    fill = C.get(box.kind + "_fill", C["own_fill"])
    line = C.get(box.kind + "_line", C["own_line"])
    parts = [
        f'<rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" rx="9" ry="9" '
        f'fill="{fill}" stroke="{line}" stroke-width="1.4"/>'
    ]
    lines = _text_lines(box)
    line_h = box.size + 5.0
    total = sum((box.size - 1.5 if i >= len(box.label) else box.size) + 5.0 for i in range(len(lines)))
    top = box.cy - total / 2.0 + box.size * 0.75
    for i, (txt, size, bold, color) in enumerate(lines):
        weight = " font-weight=\"600\"" if bold else ""
        parts.append(
            f'<text x="{box.cx}" y="{top + i * line_h:.1f}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="{size}" fill="{color}"{weight}>{_esc(txt)}</text>'
        )
    return "\n".join(parts)


def _render_note(n):
    color = C["dim"] if n.dim else C["text"]
    return (
        f'<text x="{n.x}" y="{n.y}" text-anchor="{n.anchor}" font-family="{FONT}" '
        f'font-size="{n.size}" fill="{color}">{_esc(n.text)}</text>'
    )


def _render_edge(e, boxes):
    a, b = boxes[e.a], boxes[e.b]
    dash = ' stroke-dasharray="5 4"' if e.dash else ""

    if e.route == "h":
        x1, y1 = (a.right, a.cy) if a.cx < b.cx else (a.x, a.cy)
        x2, y2 = (b.x, b.cy) if a.cx < b.cx else (b.right, b.cy)
        d = f"M {x1:.1f} {y1:.1f} L {x2:.1f} {y2:.1f}"
    elif e.route == "z":
        # 正交 Z 形：水平 → 垂直 → 水平，避免斜线穿过中间的节点
        x1, y1 = (a.right, a.cy) if a.cx < b.cx else (a.x, a.cy)
        x2, y2 = (b.x, b.cy) if a.cx < b.cx else (b.right, b.cy)
        mx = (x1 + x2) / 2.0
        d = f"M {x1:.1f} {y1:.1f} H {mx:.1f} V {y2:.1f} H {x2:.1f}"
    elif e.route == "v":
        y1 = a.bottom if a.cy < b.cy else a.y
        y2 = b.y if a.cy < b.cy else b.bottom
        d = f"M {a.cx:.1f} {y1:.1f} L {b.cx:.1f} {y2:.1f}"
    elif e.route == "hv":
        x1, y1 = (a.right, a.cy) if a.cx < b.cx else (a.x, a.cy)
        x2, y2 = (b.cx, b.y) if a.cy < b.cy else (b.cx, b.bottom)
        d = f"M {x1:.1f} {y1:.1f} H {x2:.1f} V {y2:.1f}"
    elif e.route == "vh":
        y1 = a.bottom if a.cy < b.cy else a.y
        x2 = b.x if a.cx < b.cx else b.right
        # 水平段落在目标框的上/下边缘，避免横穿框体内部
        y2 = b.y if a.cy < b.cy else b.bottom
        d = f"M {a.cx:.1f} {y1:.1f} V {y2:.1f} H {x2:.1f}"
    else:
        raise ValueError("route must be h/v/hv/vh")

    out = [
        f'<path d="{d}" fill="none" stroke="{e.color}" stroke-width="1.5" '
        f'marker-end="url(#arrow)"{dash}/>'
    ]
    if e.label:
        if e.label_pos:
            lx, ly = e.label_pos
        elif e.route in ("h", "z"):
            lx, ly = (x1 + x2) / 2.0, min(y1, y2) - 8
        elif e.route == "v":
            lx, ly = max(a.cx, b.cx) + 8, (y1 + y2) / 2.0
        else:
            lx, ly = x2 + 8, (y1 + y2) / 2.0
        out.append(
            f'<rect x="{lx - len(e.label) * 3.4:.1f}" y="{ly - 11:.1f}" '
            f'width="{len(e.label) * 6.8:.1f}" height="16" rx="4" fill="{C["bg"]}" fill-opacity="0.92"/>'
        )
        out.append(
            f'<text x="{lx:.1f}" y="{ly + 1:.1f}" text-anchor="middle" font-family="{FONT}" '
            f'font-size="11" fill="{C["dim"]}">{_esc(e.label)}</text>'
        )
    return "\n".join(out)


def _render_frame(fr):
    line = C.get(fr.kind + "_line", C["own_line"])
    return (
        f'<rect x="{fr.x}" y="{fr.y}" width="{fr.w}" height="{fr.h}" rx="12" fill="none" '
        f'stroke="{line}" stroke-width="1.6"/>'
        f'<text x="{fr.x + 16}" y="{fr.y + 26}" font-family="{FONT}" font-size="12.5" '
        f'font-weight="700" fill="{line}">{_esc(fr.title)}</text>'
    )


# 供 check_svg.py 做几何自检：记录最近一次 render 的布局
_LAST = {}


def render(width, height, title, boxes, edges, notes=(), legend=None, frames=()):
    """把一张图渲染成 SVG 字符串。"""
    bmap = {b.bid: b for b in boxes}
    _LAST.clear()
    _LAST.update({"W": width, "H": height, "boxes": list(boxes), "edges": list(edges),
                  "frames": list(frames)})
    body = [
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{C["bg"]}"/>',
        f'<text x="24" y="30" font-family="{FONT}" font-size="16" font-weight="700" '
        f'fill="{C["text"]}">{_esc(title)}</text>',
        f'<line x1="24" y1="40" x2="{width - 24}" y2="40" stroke="{C["own_line"]}" stroke-width="1"/>',
    ]
    if legend:
        lx = width - 24
        for i, (txt, kind) in enumerate(reversed(legend)):
            fill = C.get(kind + "_fill", C["own_fill"])
            line = C.get(kind + "_line", C["own_line"])
            x = lx - (i * 118)
            body.append(
                f'<rect x="{x - 108}" y="16" width="12" height="12" rx="3" fill="{fill}" stroke="{line}"/>'
            )
            body.append(
                f'<text x="{x - 92}" y="26" font-family="{FONT}" font-size="11" '
                f'fill="{C["dim"]}">{_esc(txt)}</text>'
            )
    body += [_render_frame(fr) for fr in frames]
    body += [_render_box(b) for b in boxes]
    body += [_render_edge(e, bmap) for e in edges]
    body += [_render_note(n) for n in notes]

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{C["line"]}"/></marker></defs>\n'
        + "\n".join(body)
        + "\n</svg>\n"
    )


# --------------------------------------------------------------------------
# 图 1：架构与依赖边界
# --------------------------------------------------------------------------
def diagram_architecture():
    W, H = 900, 470
    boxes = [
        Box("comfy", 330, 62, 240, 44, "ComfyUI 运行时", "neu", bold=True),
        Box("main", 90, 200, 370, 56,
            "YanhuoH3MotionContextSelfLift", "own", bold=True,
            sub=["主节点 · 面板显示「Yanhuo H3 Motion Context SelfLift」"]),
        Box("comp", 90, 264, 370, 62,
            "配套节点 ×3", "own",
            sub=["参考图打包 / 成片导出 (Final Decode) / 视频文件加载"]),
        Box("vendor", 90, 342, 370, 56,
            "vendor.py · 只读定位", "own",
            sub=["共享同一份 module 实例，不复制源码、不写回"]),
        Box("ext", 560, 186, 290, 96,
            "ComfyUI_MiniMax_H3_Extender", "ext", bold=True,
            sub=["CLIP 卡片 UI / 参考图管理", "Motion Context 缓存 · Apache-2.0"]),
        Box("sl", 560, 306, 290, 96,
            "comfyui-SelfLift", "ext", bold=True,
            sub=["progressive_sample 双采样引擎", "上游未提供 LICENSE"]),
    ]
    frames = [
        Frame(60, 146, 430, 272, "yanhuo_H3_Motion-Context_SelfLift（本包 · MIT）", "own"),
    ]

    edges = [
        Edge("comfy", "main", "registerExtension + NODE_CLASS_MAPPINGS", "v"),
        Edge("vendor", "ext", "import（只读）", "z"),
        Edge("vendor", "sl", "import（只读）", "z"),
    ]
    notes = [
        Note(60, 434, "两个姊妹包必须都装在 custom_nodes/ 下；缺任一个 → 打日志并跳过节点注册，ComfyUI 照常启动。"),
        Note(60, 452, "本包不修改、不内置、不随包分发这两个包；它们各自的许可由你自己安装时承担。"),
    ]
    return render(W, H, "① 架构与依赖边界", boxes, edges, notes,
                  legend=[("本包", "own"), ("姊妹包（外部）", "ext"), ("框架", "neu")],
                  frames=frames)


# --------------------------------------------------------------------------
# 图 2：单段 CLIP 的采样管线
# --------------------------------------------------------------------------
def diagram_pipeline():
    W, H = 900, 810
    boxes = [
        Box("in", 240, 62, 300, 54, "当前 CLIP 的 latent + 完整 sigma 表", "neu"),
        Box("low", 240, 146, 300, 76, "低分辨率阶段", "own", bold=True,
            sub=["transition_step 步 Euler", "空间缩放到 lowres_scale"]),
        Box("x0", 240, 252, 300, 58, "预测干净端点 x0", "own"),
        Box("up_l", 50, 340, 300, 62, "① 直接 latent 提升", "own",
            sub=["nearest / bilinear / 学习型 3D upscaler"]),
        Box("up_p", 430, 340, 300, 62, "② 像素路径", "own",
            sub=["VAE 解码 → 放大 → 重编码"]),
        Box("fix", 240, 430, 300, 80, "伪影感知一致性修正", "hot", bold=True,
            sub=["用两路残差定位风险位置", "rho / w_min / w_max 控制强度"]),
        Box("noise", 240, 540, 300, 76, "在过渡 sigma 处重新加噪", "hot",
            sub=["沿用表上原有的那一步，NFE 不增加"]),
        Box("high", 240, 646, 300, 66, "高分辨率阶段", "own", bold=True,
            sub=["剩余 Euler 步，回到完整 latent 网格"]),
        Box("out", 240, 742, 300, 54, "本段 CLIP 的 latent（回到链路）", "out", bold=True),
        Box("tiling", 580, 646, 180, 66, "可选：分块 tiling", "neu",
            sub=["1~8 块，显存吃紧"]),
    ]

    edges = [
        Edge("in", "low", "", "v"),
        Edge("low", "x0", "", "v"),
        Edge("x0", "up_l", "提升（双路并行）", "v"),
        Edge("x0", "up_p", "", "v"),
        Edge("up_l", "fix", "", "v"),
        Edge("up_p", "fix", "", "v"),
        Edge("fix", "noise", "修正量", "v"),
        Edge("noise", "high", "", "v"),
        Edge("high", "out", "本段完成", "v"),
        Edge("high", "tiling", "可选", "h"),
    ]
    notes = [
        Note(560, 90, "Motion Context 全程不变", size=12.5, dim=False),
        Note(560, 112, "· conditioning 仍带 minimax_keyframes", size=11.5),
        Note(560, 130, "· 仍带 minimax_refs（参考集）", size=11.5),
        Note(560, 148, "· latent 仍是 nested AV latent", size=11.5),
        Note(560, 166, "· 音频仍走复用 Euler 边界步", size=11.5),
        Note(560, 194, "→ 时间连续性完整保留", size=12),
        Note(560, 226, "关闭 selflift_enabled 时", size=11.5),
        Note(560, 244, "整条管线被跳过，等同原", size=11.5),
        Note(560, 262, "Extender 单阶段采样。", size=11.5),
    ]
    return render(W, H, "② 单段 CLIP 的双分辨率采样管线", boxes, edges, notes,
                  legend=[("采样阶段", "own"), ("修正/加噪", "hot"), ("可选", "neu")])


# --------------------------------------------------------------------------
# 图 3：ComfyUI 工作流接线
# --------------------------------------------------------------------------
def diagram_workflow():
    W, H = 940, 420
    boxes = [
        Box("ckpt", 40, 150, 170, 66, "Checkpoint 加载器", "neu", bold=True,
            sub=["model / clip / vae"]),
        Box("imgs", 40, 300, 170, 56, "图像列表 / Load Image", "neu"),
        Box("refpack", 250, 296, 190, 64, "Yanhuo 参考图打包", "own",
            sub=["images → H3_REF_PACK"]),
        Box("main", 250, 130, 260, 106, "Yanhuo H3 Motion Context SelfLift", "own",
            bold=True, sub=["逐 CLIP 采样 + Motion Context 续接", "out: cache"]),
        Box("sigmas", 250, 52, 260, 54, "可选：外接 SIGMAS 链", "neu",
            sub=["→ selflift_sigmas 端口（见 ④）"]),
        Box("final", 560, 150, 230, 70, "Yanhuo 成片导出", "own",
            sub=["(Final Decode)", "cache + vae → VIDEO"]),
        Box("save", 800, 160, 110, 52, "SaveVideo", "neu"),
        Box("reload", 570, 292, 200, 64, "Yanhuo 视频文件加载", "own",
            sub=["已写出的 mp4 → VIDEO"]),
        Box("down", 800, 300, 110, 52, "放大 / 插帧", "neu"),
    ]
    edges = [
        Edge("ckpt", "main", "model / clip / vae", "h"),
        Edge("imgs", "refpack", "images", "h"),
        Edge("refpack", "main", "ref_pack_N", "hv"),
        Edge("sigmas", "main", "SIGMAS", "v"),
        Edge("main", "final", "cache", "h"),
        Edge("final", "save", "成片视频", "h"),
        Edge("final", "reload", "写出的 mp4", "v"),
        Edge("reload", "down", "视频", "h"),
    ]
    notes = [
        Note(40, 386, "参考图端口只有逐片段的 ref_pack_N / prompt_N / ref_audio_N_x —— 接哪个片段就只影响哪个片段。", size=11.5),
        Note(40, 404, "主节点本身不吐视频：cache 只是链路缓存，成片必须过 Final Decode。", size=11.5),
    ]
    return render(W, H, "③ ComfyUI 工作流接线", boxes, edges, notes,
                  legend=[("本包节点", "own"), ("ComfyUI 原生 / 其他", "neu")])


# --------------------------------------------------------------------------
# 图 4：外接 SIGMAS 链
# --------------------------------------------------------------------------
def diagram_sigmas():
    W, H = 900, 350
    boxes = [
        Box("base", 40, 80, 180, 62, "基本调度器", "neu", bold=True,
            sub=["denoise = 1.0"]),
        Box("interp", 255, 80, 180, 62, "插值扩展 Sigmas", "neu"),
        Box("refine", 470, 80, 180, 62, "H3 Sigma Refiner", "neu"),
        Box("port", 685, 68, 175, 86, "selflift_sigmas", "own", bold=True,
            sub=["输入端口（非控件）", "接管整条 sigma 表"]),
        Box("check", 40, 190, 390, 70, "进入采样前做全套校验", "hot",
            sub=["一维 float、单调不递增", "只有末位可为 0、≥2 条、sigmas[transition_step] < 1"]),
        Box("bypass", 470, 190, 390, 70, "被旁路：scheduler / steps / denoise", "ext",
            sub=["三个控件置灰，显示「外部 SIGMAS 生效」徽章"]),
    ]
    edges = [
        Edge("base", "interp", "SIGMAS", "h"),
        Edge("interp", "refine", "SIGMAS", "h"),
        Edge("refine", "port", "SIGMAS", "h"),
        Edge("port", "check", "", "v"),
        Edge("port", "bypass", "生效后", "v"),
    ]
    notes = [
        Note(40, 306, "transition_step 变为外部表中的切割索引：低分辨率阶段用表前 transition_step+1 个点。", size=11.5),
        Note(40, 324, "确认是否生效看三处：徽章是否亮 / INFO 日志「采样调度来源」/ 报错里那句 resolved from ...", size=11.5),
    ]
    return render(W, H, "④ 外接 SIGMAS 调度链", boxes, edges, notes,
                  legend=[("本包端口", "own"), ("被旁路", "ext"), ("校验", "hot")])


# --------------------------------------------------------------------------
# 图 5：缓存一致性决策
# --------------------------------------------------------------------------
def diagram_cache():
    W, H = 860, 430
    boxes = [
        Box("start", 40, 66, 200, 54, "Queue 触发本节点", "neu", bold=True),
        Box("calc", 280, 60, 220, 66, "计算当前 SelfLift 参数签名", "own",
            sub=["含外接表的内容指纹"]),
        Box("cmp", 540, 56, 220, 74, "比对 manifest 里的", "own", bold=True,
            sub=["selflift_plan_signature"]),
        Box("hit", 600, 180, 220, 62, "一致 → 百分百复用", "out", bold=True,
            sub=["正常续跑，不重渲"]),
        Box("miss", 280, 300, 220, 76, "不一致 → 清空因果链", "hot",
            bold=True, sub=["_truncate_chain(..., 0)"]),
        Box("rerun", 40, 300, 200, 76, "整链重新 Render", "hot",
            sub=["日志 SelfLift plan changed", "resetting N cached clip(s)"]),
        Box("run", 40, 130, 200, 62, "继续采样当前链路", "own"),
    ]
    edges = [
        Edge("start", "calc", "", "h"),
        Edge("calc", "cmp", "", "h"),
        Edge("cmp", "hit", "一致", "v"),
        Edge("cmp", "miss", "不一致", "v"),
        Edge("miss", "rerun", "", "h"),
        Edge("hit", "run", "", "v"),
        Edge("rerun", "run", "", "v"),
    ]
    notes = [
        Note(40, 400, "参数、语义桥、外接表链任一变化都会进签名；关闭语义桥时签名与开启前完全一致，不会白白重渲一次。", size=11.5),
        Note(40, 418, "三条链路（Ref2VA 运动链 / Ref2VA 独立瑞 / FL2VA）都接了；某条路径解析失败只打 WARNING，不中断生成。", size=11.5),
    ]
    return render(W, H, "⑤ 缓存一致性：什么时候会重渲", boxes, edges, notes,
                  legend=[("判定", "own"), ("需要重渲", "hot"), ("复用", "out")])


# --------------------------------------------------------------------------
# 图 6：连跑全部与中断续跑
# --------------------------------------------------------------------------
def diagram_fullbatch():
    W, H = 900, 470
    boxes = [
        Box("on", 40, 62, 200, 54, "连跑全部：开", "own", bold=True,
            sub=["切换上游 run_mode 控件"]),
        Box("mode", 285, 62, 200, 54, "run_mode = full_batch", "neu", bold=True),
        Box("chk", 530, 56, 200, 66, "每段开始：检查中断请求", "hot", bold=True),
        Box("stop", 760, 52, 110, 74, ["写检查点", "停止"], "hot", sub=["已完成段全保留"]),
        Box("sample", 530, 156, 200, 62, "采样当前段", "own"),
        Box("cache", 530, 242, 200, 62, "立刻落盘缓存", "own", sub=["不是全部跑完才写"]),
        Box("export", 530, 328, 200, 76, "逐段出片 mp4", "out",
            sub=["output/yanhuo_selflift/", "ffmpeg -c:v copy 流复制"]),
        Box("push", 285, 328, 200, 76, "推送前端预览", "own",
            sub=["websocket → 面板 <video>", "草稿预览同步刷新"]),
        Box("next", 40, 328, 200, 76, "下一段 CLIP", "neu", sub=["回到中断检查"]),
        Box("done", 760, 328, 110, 76, "全部完成", "out", bold=True),
    ]
    edges = [
        Edge("on", "mode", "", "h"),
        Edge("mode", "chk", "", "h"),
        Edge("chk", "stop", "有中断", "h"),
        Edge("chk", "sample", "无中断", "v"),
        Edge("sample", "cache", "", "v"),
        Edge("cache", "export", "", "v"),
        Edge("export", "push", "", "h"),
        Edge("push", "next", "", "h"),
        Edge("export", "done", "已是最后一段", "h"),
        Edge("next", "chk", "", "vh"),
    ]
    notes = [
        Note(40, 430, "连跑不会自动勾「已校验」——与手工流程一致，不会替你接受未审片的片段。", size=11.5),
        Note(40, 448, "「停在当前段」= 当前段跑完、检查点落盘后停下，下次 Queue 从断点续跑；ComfyUI 硬 Interrupt 则是立即中断、当前段作废。", size=11.5),
    ]
    return render(W, H, "⑥ 连跑全部与中断续跑循环", boxes, edges, notes,
                  legend=[("本包逻辑", "own"), ("产出", "out"), ("中断/分支", "hot")])


DIAGRAMS = [
    ("01-architecture.svg", diagram_architecture),
    ("02-sampling-pipeline.svg", diagram_pipeline),
    ("03-workflow-wiring.svg", diagram_workflow),
    ("04-external-sigmas.svg", diagram_sigmas),
    ("05-cache-signature.svg", diagram_cache),
    ("06-fullbatch-loop.svg", diagram_fullbatch),
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    for name, fn in DIAGRAMS:
        svg = fn()
        # 自校验：必须是合法 XML
        ET.fromstring(svg)
        path = os.path.join(here, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"ok  {name:32s} {len(svg):>7,} bytes")


if __name__ == "__main__":
    main()
