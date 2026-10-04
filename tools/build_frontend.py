"""Generate this package's frontend from the sibling Extender UI.

The Extender ships a very large custom panel (cards, references, previews,
projects…). Rather than forking it, we regenerate a copy whose only difference
is **which node type it binds to**::

    const TARGET = "MiniMaxH3Extender";   ->  "YanhuoH3MotionContextSelfLift"
    name: "MiniMaxH3.Extender"            ->  "YanhuoH3.SelfLiftUI"

Nothing else changes: features added by newer versions of the Extender UI flow
into this file when the script is re-run. The SelfLift widgets are ordinary
ComfyUI widgets appended by INPUT_TYPES, so the panel needs no changes for them.

After the copy, this script appends ``SIGMAS_UI_TAIL``: a small second extension
that mirrors the ``selflift_sigmas`` external input onto the scheduler/steps/
denoise widgets ((EXT) badge + read-only) without touching the parent's code.

Usage::

    python tools/build_frontend.py
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# v1.9.0：上游 Extender 包可能已经被删除（本包已内置其源码），所以构建源
# 也要能回退到内置副本，否则以后改前端就再也构建不了了。外面的优先——
# 装回来时能直接基于上游最新版重建。
_EXTERNAL_SOURCE = ROOT.parent / "ComfyUI_MiniMax_H3_Extender" / "web" / "extender.js"
_BUNDLED_SOURCE = ROOT / "_vendor" / "ComfyUI_MiniMax_H3_Extender" / "web" / "extender.js"
SOURCE = _EXTERNAL_SOURCE if _EXTERNAL_SOURCE.is_file() else _BUNDLED_SOURCE
TARGET_FILE = ROOT / "web" / "selflift_extender.js"

NODE_NAME = "YanhuoH3MotionContextSelfLift"
EXTENSION_NAME = "YanhuoH3.SelfLiftUI"

# ---------------------------------------------------------------------------
# v1.2.0 后处理：紧凑高度 + 中文界面。
# 每条替换都声明"至少出现 1 次"；Extender 更新导致文案变化时，build 会立刻
# 失败提示，而不是静默漏翻。
# ---------------------------------------------------------------------------
# v1.6.1 底部安全余量：卡片行底部的横向滑轨仍会被节点底边裁掉一截（实测约
# 20px）——REF_SECTION / strip / draft 的估算值与真实 DOM 之间存在零星偏差
# （预览框 margin、参考行实际高度、LoRA 编辑区行高等），累积起来正好吞掉
# 滑轨。统一加 BOTTOM_SAFE_PX，且**下限公式与卡片行扣除侧同时加**：节点处于
# 最小高度时 uiMin 变大 → forceMin 自动长高 → available 同步变大 → 卡片行
# 高度不变，净效果只是节点整体高 20px、滑轨完整可见。
# v1.8.1：用户实测「再调高一倍」→ 40px。此时它只作用于 UI 最小高度（卡片行
# 的实际高度已交给 flex 分配），放宽它不会压缩卡片，只是把面板底边那道留白
# 加厚，代价是每个节点最小高度 +20px。
BOTTOM_SAFE_PX = 40

# v1.8.1 面板底部固定留白（用户要的「底部边界固定高度」）。它同时是 cards
# 内容框的底边基准：root.paddingBottom 一旦固定，cards 由 flex 撑到内容框
# 底部，横向滑轨的下沿就永远停在这条留白的上边，不再被 root 的
# overflow:hidden 吃掉最后几像素。
YANHUO_BOTTOM_GUTTER = 14

# v1.8.1 草稿预览 / 成片预览统一尺寸（用户要求"长宽对齐"）：两边用同一套
# flex 基准 + 同一像素高，object-fit 保证不同长宽比也不再一大一小。
YANHUO_PREVIEW_BASIS = 300
YANHUO_PREVIEW_MIN_W = 200
YANHUO_PREVIEW_H = 200

HEIGHT_REWRITES = (
    # 面板本体再压一层：v1.2.0 首轮 430/470/330/400 还是不够矮 —— 面板下限
    # 实际由"55 + REF_SECTION(160) + 卡片min + 24"决定，所以必须压卡片min才有效。
    ("const UI_MIN_HEIGHT = 650;", "const UI_MIN_HEIGHT = 340;", 1),
    ("const NODES2_MIN_HEIGHT = 700;", "const NODES2_MIN_HEIGHT = 400;", 1),
    ("const CARD_MIN_HEIGHT_REF2VA = 455;", "const CARD_MIN_HEIGHT_REF2VA = 240;", 1),
    ("const CARD_MIN_HEIGHT_FL2VA = 560;", "const CARD_MIN_HEIGHT_FL2VA = 310;", 1),
    ("Math.max(340,", "Math.max(240,", 2),
    # 载入工作流时自动收拢"超大"的已存高度。原版只在 > max(1800, min*3) 时才修，
    # 对本节点来说太宽松（控件多、默认 min 本身就高，1.5~2 倍的旧尺寸永远修不掉）。
    # 收紧到 1.6 倍：超过即视为历史遗留，载入时一次性收拢到最小高度。
    # 代价：用户手动拖到 >1.6 倍最小高度后，重新载入会被收回来。
    (
        "return h > Math.max(1800, Number(minimumHeight || 0) * 3);",
        "return h > Math.max(1200, Number(minimumHeight || 0) * 1.6);",
        1,
    ),
    # v1.3.0 全局条（参考区与卡片之间）的高度计入三处面板下限公式：
    # 基础 34px（开关行），开全局 LoRA 编辑区时再多 118px（yanhuoStripExtra）。
    # v1.4.2 草稿预览框同样计入；v1.8.0 起草稿+成片预览并排为一行，
    # 由 yanhuoPreviewRowExtra() 实测行高计入（估算偏差由 sync 末尾的
    # 实测重排兜底，见 v1.8.0 补偿块）。
    # 同时补上参考区 marginBottom 的 7px —— 此前最小高度下卡片行底部被裁 7px，
    # 位于卡片行底部 24px 内的横向滑轨因此时隐时现（压缩到最小尺寸时看不到滑轨）。
    (
        "55 + REF_SECTION_HEIGHT + cardMinHeightForState(state)",
        "55 + REF_SECTION_HEIGHT + 7 + " + str(BOTTOM_SAFE_PX)
        + " + yanhuoStripExtra(state) + yanhuoPreviewRowExtra()"
        " + cardMinHeightForState(state)",
        1,
    ),
    (
        "available - 55 - REF_SECTION_HEIGHT)}px",
        "available - 55 - REF_SECTION_HEIGHT - 7 - " + str(BOTTOM_SAFE_PX)
        + " - yanhuoStripExtra(runtime.state)"
        " - yanhuoPreviewRowExtra())}px",
        1,
    ),
    (
        "initialUiMinHeight - 55 - REF_SECTION_HEIGHT)}px",
        "initialUiMinHeight - 55 - REF_SECTION_HEIGHT - 7 - " + str(BOTTOM_SAFE_PX)
        + " - yanhuoStripExtra(state)"
        " - yanhuoPreviewRowExtra())}px",
        1,
    ),
    # v1.5.0 窄节点补偿：工具栏按钮在窄节点上会换行堆叠，实际高度远超预算写死的
    # 55px，下方内容被整体顶出 root（overflow:hidden）——位于卡片行底部的横向
    # 滑轨正好被裁掉（现象：只有把节点拉到一定宽度、工具栏回到单行后才出现滑轨）。
    # 在 legacy 同步末尾按实测工具栏高度补足面板与节点高度：只长高、不缩短；
    # 工具栏单行（<=57px）时完全零操作。
    (
        "runtime.domHeight = available;",
        (
            "runtime.domHeight = available;\n"
            "        const __yanhuoTbH = Math.round(\n"
            "            Number(runtime.toolbar?.getBoundingClientRect?.().height) || 55,\n"
            "        );\n"
            "        if (__yanhuoTbH > 57) {\n"
            "            const __yanhuoNeed = __yanhuoTbH + REF_SECTION_HEIGHT + 7\n"
            "                + " + str(BOTTOM_SAFE_PX)
            + " + yanhuoStripExtra(runtime.state) + yanhuoPreviewRowExtra()\n"
            "                + cardMinHeightForState(runtime.state) + CARD_SCROLLBAR_SPACE;\n"
            "            if (available < __yanhuoNeed) {\n"
            "                runtime.root.style.height = `${__yanhuoNeed}px`;\n"
            "                runtime.cards.style.height = `${Math.max(240, __yanhuoNeed\n"
            "                    - __yanhuoTbH - REF_SECTION_HEIGHT - 7 - " + str(BOTTOM_SAFE_PX) + "\n"
            "                    - yanhuoStripExtra(runtime.state) - yanhuoPreviewRowExtra())}px`;\n"
            "                const __yanhuoTargetH = Number(runtime.domWidget?.last_y)\n"
            "                    + __yanhuoNeed + BOTTOM_PAD;\n"
            "                const __yanhuoBeforeH = Number(node.size?.[1] || 0);\n"
            "                if (__yanhuoBeforeH < __yanhuoTargetH) {\n"
            "                    /* v1.12.1：预览行导致的长高要记账（__yanhuoLoanPx），\n"
            "                       预览收起后由 yanhuoFitPanel 还回去；非预览原因\n"
            "                       （工具栏换行、LoRA 行数变多）的长高不算账。 */\n"
            "                    const __yanhuoPrevExtra = yanhuoPreviewRowExtra();\n"
            "                    if (__yanhuoPrevExtra > 0) {\n"
            "                        runtime.__yanhuoLoanPx = Number(runtime.__yanhuoLoanPx || 0)\n"
            "                            + Math.min(__yanhuoTargetH - __yanhuoBeforeH, __yanhuoPrevExtra);\n"
            "                    }\n"
            "                    node.setSize([\n"
            "                        Math.max(NODE_MIN_WIDTH, Number(node.size?.[0] || NODE_MIN_WIDTH)),\n"
            "                        __yanhuoTargetH,\n"
            "                    ]);\n"
            "                }\n"
            "                node.graph?.setDirtyCanvas(true, true);\n"
            "            }\n"
            "        }\n"
            "        /* v1.8.1 滑轨恒定：**彻底放弃像素估算，改由 flexbox 精确分配**。\n"
            "           三条措施各自解决一类失效：\n"
            "           1) root 固定底部余量 yanhuoBottomGutter px —— 用户要的\"底部\n"
            "              边界固定值\"，滑轨下沿不再贴着节点底边被切最后几像素；\n"
            "           2) cards 之外的兄弟一律 flexShrink=0 —— 按自然高度排布，不许\n"
            "              被压扁（v1.8.0 里它们被压缩后又按\"被压扁后的高度\"反推\n"
            "              cards，两者相互打架，是这次全线消失的主因）；\n"
            "              cards 改为 flex:1 1 auto + min-height 兜底 —— 吃掉全部\n"
            "              剩余高度，底边永远落在内容框底边上，root 的 overflow:hidden\n"
            "              因此不可能裁到横向滑轨。toolbar 换行(61~138px)、全局条、\n"
            "              预览行怎么变都不再影响这个结论；\n"
            "           3) 万一连 min-height 都放不下（最小节点 + 预览行展开），由\n"
            "              yanhuoFitPanel() 按子元素实测底边补高面板与节点（有界：\n"
            "              单轮 <=900px、累计 <=1500px、最多 6 轮）。 */\n"
            "        if (runtime.root) {\n"
            "            runtime.root.style.paddingBottom = `${yanhuoBottomGutter()}px`;\n"
            "            for (const __yanhuoEl of runtime.root.children) {\n"
            "                if (!__yanhuoEl || __yanhuoEl === runtime.cards) continue;\n"
            "                if (__yanhuoEl.style.display === \"none\") continue;\n"
            "                __yanhuoEl.style.flexShrink = \"0\";\n"
            "            }\n"
            "            if (runtime.cards) {\n"
            "                /* 与上游 Nodes 2.0 分支（applyNodes2TimelineHeight）完全同一套\n"
            "                   写法：height:auto + flex:1 1 0 —— flex 基准 0，卡片行永远\n"
            "                   等于\"容器剩余空间\"这一个值，不可能多也不可能少，再用\n"
            "                   min-height 兜底下限。 */\n"
            "                runtime.cards.style.height = \"auto\";\n"
            "                runtime.cards.style.flex = \"1 1 0\";\n"
            "                runtime.cards.style.minHeight =\n"
            "                    `${cardMinHeightForState(runtime.state) + CARD_SCROLLBAR_SPACE}px`;\n"
            "            }\n"
            "            requestAnimationFrame(() => yanhuoFitPanel(node, runtime, 0));\n"
            "        }"
        ),
        1,
    ),
)

CHINESE_REWRITES = (
    # 工具栏按钮
    ('add.textContent = "+ Add Clip";', 'add.textContent = "+ 添加片段";', 1),
    ('remove.textContent = "− Remove Last";', 'remove.textContent = "− 删除末尾";', 1),
    ('newProjectButton.textContent = "New Project";', 'newProjectButton.textContent = "新建项目";', 1),
    ('saveProjectButton.textContent = "Save Project";', 'saveProjectButton.textContent = "保存项目";', 1),
    ('loadProjectButton.textContent = "Load Project";', 'loadProjectButton.textContent = "载入项目";', 1),
    ('interruptButton.textContent = "Interrupt";', 'interruptButton.textContent = "中断续跑";', 1),
    (': "Interrupt";', ': "中断续跑";', 1),
    ('"Stopping…"', '"停止中…"', 1),
    ('"MODE: FL2VA"', '"模式: FL2VA"', 1),
    ('"MODE: REF2VA"', '"模式: REF2VA"', 1),
    ('"MOTION: OFF"', '"运动链: 关"', 1),
    ('"MOTION: ON"', '"运动链: 开"', 1),
    # 参考图区
    ('"REFERENCE IMAGES — double-click a thumbnail to edit"', '"参考图像 — 双击缩略图编辑"', 2),
    (
        "REFERENCE IMAGES — CLIP ${preview.clipIndex + 1} running",
        "参考图像 — CLIP ${preview.clipIndex + 1} 渲染中",
        1,
    ),
    ("`Replace Ref ${logicalSlot}`", "`替换参考 ${logicalSlot}`", 1),
    ("`Load Ref ${logicalSlot}`", "`载入参考 ${logicalSlot}`", 1),
    # 卡片
    ('name.placeholder = "name";', 'name.placeholder = "名称";', 1),
    ('"Prompt (EXT)"', '"提示词 (EXT)"', 1),
    (': "Prompt",', ': "提示词",', 1),
    ('isAddRow ? "Add LoRA"', 'isAddRow ? "添加 LoRA"', 1),
    ('makeFieldLabel("Strength")', 'makeFieldLabel("强度")', 1),
    ('makeFieldLabel("Seed")', 'makeFieldLabel("种子")', 1),
    ('dice.title = "Randomize seed";', 'dice.title = "随机种子";', 1),
    ('seedMode.title = "Seed behavior after a generated candidate";', 'seedMode.title = "生成一个候选后的种子行为";', 1),
    ('"Duration s (EXT)"', '"时长 s (EXT)"', 1),
    (': "Duration s")', ': "时长 s")', 1),
    ('document.createTextNode("Validated")', 'document.createTextNode("已校验")', 1),
    ("`Refs ${localCount}`", "`参考 ${localCount}`", 1),
    ('"Manage clip-local Picture / Video / Audio references"', '"管理本片段专属的 Picture / Video / Audio 参考"', 1),
    ("`Local/global slot conflict: ${localConflicts.join(\", \")}`", "`本片段与全局槽位冲突: ${localConflicts.join(\", \")}`", 1),
    # 卡片状态徽标
    ('"● CANDIDATE"', '"● 待渲染"', 1),
    ('"● COMPUTED"', '"● 已缓存"', 1),
    ('"● NEXT"', '"● 下一片段"', 1),
    ('"✓ COMPLETE"', '"✓ 完成"', 1),
    ('"◆ PREPARING"', '"◆ 准备中"', 1),
    ('"▶ RENDERING"', '"▶ 渲染中"', 1),
    ('st === "validated" ? "VALIDATED"', 'st === "validated" ? "已校验"', 1),
    ('st === "cached" ? "CACHE"', 'st === "cached" ? "缓存"', 1),
    # 顶部计数与状态
    ('plan${state.clips.length > 1 ? "s" : ""} • FL2VA', '个计划 • FL2VA', 1),
    (
        'clip${state.clips.length > 1 ? "s" : ""} • ${refCount(runtime)} ref${refCount(runtime) === 1 ? "" : "s"}',
        '个片段 • ${refCount(runtime)} 个参考',
        1,
    ),
    ('" • independent"', '" • 独立模式"', 1),
    ('status.textContent = runtime.statusText || "Ready";', 'status.textContent = runtime.statusText || "就绪";', 1),
    ('|| "Ready")', '|| "就绪")', 2),
    # 项目操作提示
    ('runtime.statusText = "Starting new project…";', 'runtime.statusText = "正在新建项目…";', 1),
    ('runtime.statusText = "Saving project…";', 'runtime.statusText = "正在保存项目…";', 1),
    ('runtime.statusText = "New Project failed";', 'runtime.statusText = "新建项目失败";', 1),
    ('runtime.statusText = "Save Project failed";', 'runtime.statusText = "保存项目失败";', 1),
    ('runtime.statusText = "Load Project failed";', 'runtime.statusText = "载入项目失败";', 1),
    (
        '"Clear all current Extender project data/cache and start with one empty clip; global settings are preserved"',
        '"清空当前项目的数据/缓存，从一个空白片段重新开始；全局设置保留"',
        1,
    ),
    ('"Save settings + disk cache as a portable .ext project"', '"把设置和磁盘缓存打包为可迁移的 .ext 项目"', 1),
    ('"Load a .ext project into this Extender node"', '"把 .ext 项目载入到本节点"', 1),
    (
        '"Finish the current Full Batch clip, save a resumable checkpoint, then decode the partial preview"',
        '"完成当前整批片段、保存可续跑的检查点，然后解码已生成部分"',
        1,
    ),
    (
        '"Wait for the current clip generation to finish before loading a project."',
        '"请等当前片段生成完成后再载入项目。"',
        2,
    ),
)


# ---------------------------------------------------------------------------
# v1.12.1：主节点上的"旧逐段插座清理"钩子。
#
# v1.12.0 把 ref_pack_N / prompt_N / duration_N / ref_audio_N_k 搬到集合节点后，
# INPUT_TYPES 里已经没有它们了；但 LiteGraph 是从**保存的 JSON** 里恢复
# inputs 的，所以老工作流一打开，主节点上那一长排空插座照样在。它们会把
# 透传层挡死（详细理由见 PERCLIP_UI_TAIL 里 findInputEntry 的注释）。
#
# 这里往两个生命周期点插一句清理：
#   * onNodeCreated —— 新建的节点（拖出来的）；
#   * onConfigure   —— 载入工作流的节点（插座正是这时候被恢复出来的）。
# 只删没接线的，接了线的留给用户（那种情况由 findInputEntry 的"有线优先"接管）。
# ---------------------------------------------------------------------------
PERCLIP_REWRITES = (
    (
        "            const runtime = buildUi(this);\n"
        "            removeLegacyImageRefInputs(this);\n",
        "            yanhuoPruneLegacyPerClipInputs(this);\n"
        "            const runtime = buildUi(this);\n"
        "            removeLegacyImageRefInputs(this);\n",
        1,
    ),
    (
        "    const oldConfigure = node.onConfigure;\n"
        "    node.onConfigure = function (info) {\n",
        "    const oldConfigure = node.onConfigure;\n"
        "    node.onConfigure = function (info) {\n"
        "        yanhuoPruneLegacyPerClipInputs(this);\n",
        1,
    ),
)


def _apply_rewrites(text, rewrites, label):
    for old, new, expected in rewrites:
        count = text.count(old)
        if count < 1:
            raise SystemExit(
                f"build_frontend: expected the {label} pattern at least once but found 0:\n"
                f"    {old}\n"
                "The Extender UI probably changed; update this rewrite table."
            )
        if count != expected:
            print(f"note: {label} pattern found {count} time(s), expected {expected}: {old!r}")
        text = text.replace(old, new)
    return text


def _compact_ui(text: str) -> str:
    text = _apply_rewrites(text, HEIGHT_REWRITES, "height")
    return _apply_rewrites(text, PERCLIP_REWRITES, "per-clip cleanup")


def _translate_ui(text: str) -> str:
    return _apply_rewrites(text, CHINESE_REWRITES, "chinese")

# ---------------------------------------------------------------------------
# v1.2.1 外观皮肤：淡紫为主题色、深浅主题自适应、可用工具栏取色键改色。
# 只作用在 [data-h3-extender-root="1"] 子树里，不污染 ComfyUI 全局样式。
# 颜色全部从 --yanhuo-accent 派生（color-mix），深浅主题只切换两个底色变量，
# 因此取色键改一个 accent 值即可整体换色。
# ---------------------------------------------------------------------------
SKIN_CSS = """
[data-h3-extender-root="1"] button,
[data-h3-extender-root="1"] .comfy-button,
[data-h3-extender-root="1"] .p-button {
    border-radius: 6px !important;
    border: 1px solid color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 45%, transparent) !important;
    background: color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 18%, var(--yanhuo-surface, #191a22)) !important;
    color: var(--yanhuo-button-text, #ece6f8) !important;
    font-weight: 600 !important;
    letter-spacing: 0.2px;
    transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
}
[data-h3-extender-root="1"] button:hover:not(:disabled),
[data-h3-extender-root="1"] .comfy-button:hover:not(:disabled) {
    background: color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 32%, var(--yanhuo-surface, #191a22)) !important;
    border-color: color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 70%, transparent) !important;
    box-shadow: 0 1px 5px color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 30%, transparent);
}
[data-h3-extender-root="1"] button:disabled {
    opacity: 0.45;
    color: var(--yanhuo-muted-text, #8b87a0) !important;
}
[data-h3-extender-root="1"] input,
[data-h3-extender-root="1"] select,
[data-h3-extender-root="1"] textarea {
    border-radius: 5px !important;
    border: 1px solid color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 32%, transparent) !important;
    background: var(--yanhuo-field, #121319) !important;
    color: var(--yanhuo-field-text, #e8e6f2) !important;
}
[data-h3-extender-root="1"] input:focus,
[data-h3-extender-root="1"] select:focus,
[data-h3-extender-root="1"] textarea:focus {
    border-color: var(--yanhuo-accent, #b8a1e8) !important;
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 28%, transparent);
    outline: none;
}
[data-h3-extender-root="1"] .h3-extender-card {
    border: 1px solid color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 38%, transparent) !important;
    border-radius: 8px !important;
    background: color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 7%, var(--yanhuo-surface, #191a22)) !important;
}
[data-h3-extender-root="1"] *::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}
[data-h3-extender-root="1"] *::-webkit-scrollbar-thumb {
    background: color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 40%, transparent);
    border-radius: 4px;
}
[data-h3-extender-root="1"] *::-webkit-scrollbar-track {
    background: transparent;
}
"""

SIGMAS_UI_TAIL = """

/* ------------------------------------------------------------------
 * Yanhuo addition (appended by tools/build_frontend.py):
 *   1) 外观皮肤 - 淡紫主题 + 深浅色自适应 + 工具栏取色键；
 *      只作用于本节点面板([data-h3-extender-root="1"])。
 *   2) 高度工具 - 工具栏「⇕ 紧凑」一键把节点收拢到最小高度。
 *   3) 外部 SIGMAS 接管 - mirror the optional selflift_sigmas input onto
 *      the parent's scheduler/steps/denoise widgets. When the socket is
 *      connected, those widgets are bypassed by the backend, so they
 *      render read-only with a "SIGMAS EXT" badge - the same takeover
 *      style as the duration_N/prompt_N ports.
 * 本段与主脚本同处一个 module 作用域，可直接复用 syncDomHeight /
 * uiMinHeightForState / nodes2MinHeightForState / domWidgetRenderMode。
 * ------------------------------------------------------------------ */
const YANHUO_ACCENT_KEY = "yanhuo_selflift_accent";
const YANHUO_DEFAULT_ACCENT = "#b8a1e8"; /* 淡紫 */

function yanhuoApplyAccent(hex) {
    try { localStorage.setItem(YANHUO_ACCENT_KEY, hex); } catch (e) {}
    document.documentElement.style.setProperty("--yanhuo-accent", hex);
}

function yanhuoIsDarkTheme() {
    const body = document.body;
    if (body?.classList?.contains("comfy-theme-dark")) return true;
    if (body?.classList?.contains("comfy-theme-light")) return false;
    const scheme = (getComputedStyle(document.documentElement)
        .getPropertyValue("color-scheme") || "").trim().toLowerCase();
    if (scheme) return scheme.includes("dark");
    return true; /* ComfyUI 桌面版默认深色 */
}

function yanhuoApplyTheme() {
    const dark = yanhuoIsDarkTheme();
    const s = document.documentElement.style;
    s.setProperty("--yanhuo-surface", dark ? "#191a22" : "#ffffff");
    s.setProperty("--yanhuo-field", dark ? "#121319" : "#ffffff");
    s.setProperty("--yanhuo-button-text",
        dark ? "color-mix(in srgb, var(--yanhuo-accent) 40%, #ffffff)" : "#3a2f55");
    s.setProperty("--yanhuo-field-text", dark ? "#e8e6f2" : "#241d38");
    s.setProperty("--yanhuo-muted-text", dark ? "#8b87a0" : "#857d9b");
}

(function initYanhuoSkin() {
    if (document.getElementById("yanhuo-selflift-skin")) return;
    const style = document.createElement("style");
    style.id = "yanhuo-selflift-skin";
    style.textContent = __SKIN_CSS__;
    document.head.appendChild(style);

    let accent = YANHUO_DEFAULT_ACCENT;
    try { accent = localStorage.getItem(YANHUO_ACCENT_KEY) || YANHUO_DEFAULT_ACCENT; } catch (e) {}
    yanhuoApplyAccent(accent);
    yanhuoApplyTheme();

    /* ComfyUI 切换深浅主题时跟随（class 与 data-theme 两条路都盯着）。 */
    const themeObserver = new MutationObserver(() => yanhuoApplyTheme());
    const observe = () => {
        if (document.body) {
            themeObserver.observe(document.body, { attributes: true, attributeFilter: ["class", "data-theme"] });
        }
        themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme"] });
    };
    if (document.body) observe();
    else document.addEventListener("DOMContentLoaded", observe, { once: true });
})();

/* ------------------------------------------------------------------
 * v1.3.5：「连跑全部片段」——只切换上游原生 run_mode 控件
 *
 * 上游 ComfyUI_MiniMax_H3_Extender 自带 run_mode 控件（clip_by_clip /
 * full_batch，见 extender.py:4134）。full_batch 就是「一次 Queue 依次生成
 * 全部片段」：每段之间检查中断、每段采样完立刻落盘缓存、Interrupt 后可从
 * 断点续跑。所以这里不复刻任何排队/校验链路，只做三件事：
 *   1) 把 run_mode 从控件列表提到工具栏，一键开关；
 *   2) 常驻「⏹ 停在当前段」，直接调上游 requestFullBatchInterrupt
 *      （上游自带的 Interrupt 按钮只在运行时才出现，平时找不到）；
 *   3) 给出待生成片段数与状态提示。
 * 不自动勾「已校验」、不自动排队、不改 onExecuted —— 流程语义与手工一致。
 * ------------------------------------------------------------------ */

function yanhuoRunModeWidget(node) {
    return getWidget(node, "run_mode") || null;
}

function yanhuoIsFullBatch(node) {
    return String(yanhuoRunModeWidget(node)?.value || "clip_by_clip") === "full_batch";
}

function yanhuoPendingClips(runtime) {
    return (runtime?.state?.clips || []).filter((c) => !c.validated).length;
}

/* 只改控件值，再走上游自己的提交路径：写回 hidden widget → 抓快照 → 重渲染。
   没有任何排队/校验的副作用。 */
function yanhuoSetRunMode(node, runtime, mode) {
    const widget = yanhuoRunModeWidget(node);
    if (!widget) return false;
    if (String(widget.value) !== mode) {
        widget.value = mode;
        try {
            if (typeof widget.callback === "function") widget.callback(mode, app.canvas, node);
        } catch (e) { /* 上游没挂 callback 时忽略 */ }
        try { updateHidden(node, runtime); } catch (e) {}
        try { captureNativeWorkflowState(node, runtime); } catch (e) {}
    }
    render(node, runtime);
    node.graph?.setDirtyCanvas(true, true);
    yanhuoRefreshChainControls(node, runtime);
    return true;
}

function yanhuoRefreshChainControls(node, runtime) {
    const btn = runtime?.__yanhuoChainButton;
    if (!btn) return;
    const rt = node.__h3Extender || runtime;
    const full = yanhuoIsFullBatch(node);
    const active = ["preparing", "sampling", "complete"].includes(String(rt.activePhase || ""));
    const pending = yanhuoPendingClips(rt);

    btn.textContent = full ? "🔗 连跑全部：开" : "🔗 连跑全部：关";
    btn.title = full
        ? `已开启（run_mode=full_batch）：点一次 Queue，节点依次生成全部未缓存片段，每段采样完立刻落盘。\n`
            + `当前未完成片段：${pending} 个。\n`
            + `中途停下：点旁边的「⏹ 停在当前段」——当前片段跑完、检查点落盘后即停，已完成片段留在缓存里，下次 Queue 从断点续跑。\n`
            + `注意：连跑不会自动勾「已校验」，成片请用 Final Decode 输出。`
        : `已关闭（run_mode=clip_by_clip）：一次 Queue 只生成一个 CLIP，需手动勾「已校验」再排队下一段。\n`
            + `点一下切成「开」，交给上游自带的 full_batch 连跑流程。`;

    const stop = rt.__yanhuoStopButton;
    if (stop) {
        stop.style.display = full ? "inline-block" : "none";
        const busy = Boolean(rt.interruptRequested || rt.interruptRequestBusy);
        stop.disabled = !full || !active || busy;
        stop.textContent = busy ? "⏹ 停止中…" : "⏹ 停在当前段";
        stop.title = busy
            ? "已请求停止：当前片段采样完、检查点落盘后就停，已完成片段不会丢。"
            : "让当前片段跑完并保存可续跑的检查点后停下，不再开始下一段（等价于上游运行时的 Interrupt 按钮）。";
    }
}

/* ------------------------------------------------------------------
 * v1.8.1：面板底部固定留白 + 溢出兜底
 * 高度公式（toolbar=55 / REF=160 / strip / 预览行都是估算）追不上真实 DOM，
 * 所以干脆让浏览器算：syncDomHeight 里已经把 cards 之外的兄弟设成不收缩、
 * cards 设成吃掉剩余空间，剩下的只有一种失效情形 —— 连 cards 的 min-height
 * 都放不下，这时按**实测的子元素底边**补高面板与节点。
 * ------------------------------------------------------------------ */
const YANHUO_BOTTOM_GUTTER = __BOTTOM_GUTTER__;
const YANHUO_FIT_MAX_PX = 1500;
const YANHUO_FIT_MAX_STEP = 900;
const YANHUO_FIT_MAX_PASS = 6;

function yanhuoBottomGutter() {
    return Number.isFinite(YANHUO_BOTTOM_GUTTER) ? YANHUO_BOTTOM_GUTTER : 14;
}

/* 正在拖节点时不介入：否则会和用户的拖拽来回拉扯。松手后 afterResize 会
   再走一次 syncDomHeight，兜底自动补上。 */
function yanhuoFitBusy() {
    const cv = globalThis.app?.canvas;
    if (!cv) return false;
    return Boolean(cv.resizingNode || cv.resizing_node || cv.pointer_is_down);
}

function yanhuoFitPanel(node, runtime, pass) {
    const root = runtime?.root;
    if (!node || !root || !root.isConnected) return;
    if (runtime.syncingDomHeight || yanhuoFitBusy()) return;

    const cs = getComputedStyle(root);
    const padBottom = parseFloat(cs.paddingBottom) || 0;
    const availBottom = root.getBoundingClientRect().bottom - padBottom;
    let contentBottom = 0;
    for (const el of root.children) {
        if (!el || el.style.display === "none" || !(el.offsetHeight > 0)) continue;
        const mb = parseFloat(getComputedStyle(el).marginBottom) || 0;
        const bottom = el.getBoundingClientRect().bottom + mb;
        if (bottom > contentBottom) contentBottom = bottom;
    }
    if (!(contentBottom > 0)) return;

    /* v1.12.1：面板高度只认两条规则 ——
     *   1) 静止（没有预览行）时，高度就是用户选定的尺寸：内容放不下由 cards
     *      行自己的滚动条消化，面板**绝不**自动长高。旧版在这里按 gap 无界
     *      补高，而且只增不减：采样期间每次 render 都把 gap 记进预算，跑完
     *      一个 CLIP 节点就被顶高一大截，直到把提示词全文完整亮出来。
     *   2) 只有预览行（草稿/成片）需要占位时才允许长高，且上限 =
     *      用户选定高度 + 预览行实高 + 余量；预览行收起后把借的高度还回去。
     *      没有这条，预览把节点撑高之后永远缩不回去。 */
    const gap = Math.round(contentBottom - availBottom);
    const previewExtra = yanhuoPreviewRowExtra();
    const curH = Number(node.size?.[1] || 0);

    if (gap <= 2) {
        runtime.__yanhuoFitPx = 0;
        if (previewExtra === 0) {
            const loan = Number(runtime.__yanhuoLoanPx || 0);
            const rest = Number(runtime.__yanhuoRestH || 0);
            if (loan > 0 && curH > loan) {
                /* 预览行收起了：把之前为预览借的高度还回去（用户手动拉大的
                   部分不在 loan 里，不受影响）。 */
                const targetH = Math.round(curH - loan);
                const rootH =
                    Math.round(parseFloat(root.style.height) || root.clientHeight) - loan;
                runtime.__yanhuoLoanPx = 0;
                runtime.__yanhuoRestH = targetH;
                root.style.height = `${Math.max(120, rootH)}px`;
                node.setSize([
                    Math.max(NODE_MIN_WIDTH, Number(node.size?.[0] || NODE_MIN_WIDTH)),
                    targetH,
                ]);
                node.graph?.setDirtyCanvas(true, true);
                if (pass + 1 < YANHUO_FIT_MAX_PASS) {
                    requestAnimationFrame(() => yanhuoFitPanel(node, runtime, pass + 1));
                }
                return;
            }
            runtime.__yanhuoLoanPx = 0;
            runtime.__yanhuoRestH = curH;
        }
        return;
    }

    if (previewExtra === 0) {
        /* 静止状态内容放不下：不动面板，只把"用户选定高度"的记录往下修
           （用户手动缩小节点时 rest 要跟着变小，否则预览收起后会弹回去）。 */
        if (!(Number(runtime.__yanhuoRestH || 0) > curH)) runtime.__yanhuoRestH = curH;
        runtime.__yanhuoLoanPx = 0;
        return;
    }

    const rest = Number(runtime.__yanhuoRestH || 0);
    if (!(rest > 0)) return;  // 还没采到用户的基准尺寸，先不动
    const allowance = rest + previewExtra + 16 - curH;
    if (allowance <= 2) return;
    const grow = Math.min(gap, allowance);
    runtime.__yanhuoFitPx = Number(runtime.__yanhuoFitPx || 0) + grow;
    runtime.__yanhuoLoanPx = Number(runtime.__yanhuoLoanPx || 0) + grow;

    const rootH = Math.round(parseFloat(root.style.height) || root.clientHeight) + grow;
    root.style.height = `${rootH}px`;
    node.setSize([
        Math.max(NODE_MIN_WIDTH, Number(node.size?.[0] || NODE_MIN_WIDTH)),
        Number(node.size?.[1] || 0) + grow,
    ]);
    node.graph?.setDirtyCanvas(true, true);
    if (pass + 1 < YANHUO_FIT_MAX_PASS) {
        requestAnimationFrame(() => yanhuoFitPanel(node, runtime, pass + 1));
    }
}

/* ------------------------------------------------------------------
 * v1.4.0：内置草稿视频预览
 * 后端每一步采样把 x0 解成动图，用自定义 websocket 事件推过来，这里渲染到
 * 节点面板上（不需要外接任何节点，帧数自动等于当前 CLIP 的真实帧数）。
 * ------------------------------------------------------------------ */
const YANHUO_DRAFT_EVENT = "yanhuo_h3_draft_preview";
const YANHUO_DRAFT_DECODER = {
    taeh3: "微型 VAE(taeh3)",
    latent2rgb: "Latent2RGB 近似",
};

/* 草稿预览框会占一行真实高度。v1.4.2 起改走 v1.3.0 全局条的同一条路：
   在 build 期的 HEIGHT_REWRITES 里把 yanhuoDraftExtra() 写进上游三处高度
   公式（uiMinHeightForState / syncDomHeight 的卡片行 / 卡片行初始高度）。
   v1.4.1 曾在运行时包装这两个函数，但包装层重算卡片行高时漏掉了全局条
   高度（yanhuoStripExtra），把上游已正确的行高改错了 —— 卡片行恒比面板
   高出「全局条 + 7px」，表现为卡片下半截被吞、滑轨消失，且拉伸节点也无济
   于事（常数溢出，不随节点尺寸变化）。教训：高度预算一律进 build 期改写
   表，绝不在运行时二次覆盖上游算好的值。
   v1.8.0：纯估算路线到头了 —— headless 实测发现 toolbar(61~138px+mb7)、
   strip(margin 6px) 等漏项随宽度变化，公式永远追不上；预览行高度统一交给
   yanhuoPreviewRowExtra()（UI 最小高度用）与 sync 末尾的实测重排（精确
   对齐滑轨）双重兜底。 */

/* v1.7.0 逐段成片实时预览条：与草稿预览完全同一条路 —— websocket 事件 +
   build 期改写表计入高度。后端每导出完一段就推 yanhuo_h3_final_clip，
   前端在 <video> 里直接播放（/view 端点服务 output 子目录，零自定义路由）。
   v1.8.0 起草稿预览与成片预览并排放进同一行（左草稿/右成片，窄了自动换行），
   高度预算统一由 yanhuoPreviewRowExtra() 实测这一行。 */
const YANHUO_FINAL_EVENT = "yanhuo_h3_final_clip";
const YANHUO_PREVIEW_ROWS = new Set();

function yanhuoPreviewRowExtra() {
    let extra = 0;
    for (const row of YANHUO_PREVIEW_ROWS) {
        if (!row || !row.isConnected || row.style.display === "none") continue;
        if (!(row.offsetHeight > 0)) continue;  // 两个预览都收起时行高为 0，不计入
        extra = Math.max(extra, (row.offsetHeight || 0) + 6);
    }
    return extra;
}

function yanhuoDraftNode(nodeId) {
    if (nodeId === null || nodeId === undefined || String(nodeId) === "") return null;
    const graph = app.graph;
    let node = null;
    try { node = graph?.getNodeById?.(Number(nodeId)) || null; } catch (e) { node = null; }
    if (!node && graph && Array.isArray(graph._nodes)) {
        node = graph._nodes.find((n) => n && String(n.id) === String(nodeId)) || null;
    }
    return node;
}

function yanhuoShowDraft(nodeId, payload) {
    if (!payload || !payload.data) return;
    const box = yanhuoDraftNode(nodeId)?.__h3Extender?.__yanhuoDraftBox;
    if (!box) return;
    const img = box.querySelector("img");
    const label = box.querySelector("[data-yanhuo-draft-label]");
    if (!img) return;
    img.src = "data:image/webp;base64," + payload.data;
    const wasHidden = box.style.display === "none";
    box.style.display = "flex";
    /* 高度预算依赖草稿框的实际像素高度：首次显示、以及图片加载完成
       （不同 CLIP 分辨率不同、高度会变）都要重排一次。与上游调用惯例
       一致传 forceMin=true —— 节点高度不够时自动长高，而不是把内容
       顶出节点边界。 */
    const resync = () => {
        const target = yanhuoDraftNode(nodeId);
        if (target?.__h3Extender) {
            try { syncDomHeight(target, target.__h3Extender, true); } catch (e) { /* 忽略 */ }
        }
    };
    if (!img.dataset.yanhuoDraftLoadHook) {
        img.dataset.yanhuoDraftLoadHook = "1";
        img.addEventListener("load", resync);
    }
    if (wasHidden) requestAnimationFrame(resync);
    if (label) {
        const clip = Number(payload.clip || 0);
        const step = Number(payload.step || 0) + 1;
        const total = Number(payload.total_steps || 0);
        const decoder = YANHUO_DRAFT_DECODER[payload.decoder] || "草稿";
        label.textContent =
            "草稿预览 · "
            + (clip ? "CLIP " + clip + " · " : "")
            + "步 " + step + "/" + total + " · "
            + Number(payload.frames || 0) + " 帧 @ " + Number(payload.fps || 24) + "fps · "
            + decoder;
    }
}

api.addEventListener(YANHUO_DRAFT_EVENT, (event) => {
    const detail = (event && event.detail) || event || {};
    const payload = detail.content || detail;
    try { yanhuoShowDraft(payload.node_id, payload); } catch (e) { /* 预览失败不影响出片 */ }
});

/* v1.7.0 逐段成片实时预览：收到推送就刷新 <video> 播放最新一段成片。
   走 ComfyUI 内置 /view 端点（type=output + subfolder=yanhuo_selflift），
   不需要自定义 HTTP 路由；播放/下载/预览失败都绝不影响采样。 */
function yanhuoFinalResync(nodeId) {
    const target = yanhuoDraftNode(nodeId);
    if (target?.__h3Extender) {
        try { syncDomHeight(target, target.__h3Extender, true); } catch (e) { /* 忽略 */ }
    }
}

function yanhuoShowFinal(nodeId, payload) {
    if (!payload || !payload.filename) return;
    const box = yanhuoDraftNode(nodeId)?.__h3Extender?.__yanhuoFinalBox;
    if (!box) return;
    const video = box.querySelector("video");
    const label = box.querySelector("[data-yanhuo-final-label]");
    const link = box.querySelector("[data-yanhuo-final-link]");
    const params = new URLSearchParams({
        filename: String(payload.filename),
        subfolder: String(payload.subfolder || "yanhuo_selflift"),
        type: String(payload.type || "output"),
    });
    const url = "/view?" + params.toString();
    if (video && video.dataset.yanhuoFinalSrc !== url) {
        video.dataset.yanhuoFinalSrc = url;
        video.src = url;
        try { video.play?.().catch(() => {}); } catch (e) { /* 自动播放被策略拦截就等手点 */ }
    }
    if (link) {
        link.href = url;
        link.setAttribute("download", String(payload.filename));
    }
    if (label) {
        const total = Number(payload.total || 0);
        label.textContent = "成片预览 · 第 " + Number(payload.clip || 0)
            + (total ? "/" + total : "") + " 段 · " + payload.filename;
    }
    const wasHidden = box.style.display === "none";
    box.style.display = "flex";
    if (wasHidden) requestAnimationFrame(() => yanhuoFinalResync(nodeId));
    if (video && !video.dataset.yanhuoFinalMetaHook) {
        video.dataset.yanhuoFinalMetaHook = "1";
        video.addEventListener("loadedmetadata", () => yanhuoFinalResync(nodeId));
    }
}

api.addEventListener(YANHUO_FINAL_EVENT, (event) => {
    const detail = (event && event.detail) || event || {};
    const payload = detail.content || detail;
    try { yanhuoShowFinal(payload.node_id, payload); } catch (e) { /* 预览失败不影响出片 */ }
});

function yanhuoInjectToolbarControls(node) {
    const runtime = node.__h3Extender;
    const root = runtime?.root;
    if (!root || !root.isConnected) {
        setTimeout(() => yanhuoInjectToolbarControls(node), 60);
        return;
    }
    const toolbar = root.querySelector("button")?.parentElement;
    if (!toolbar || toolbar.dataset.yanhuoControls === "1") return;
    toolbar.dataset.yanhuoControls = "1";

    /* v1.4.0 草稿预览框 + v1.7.0 成片预览条：v1.8.0 起并排放进同一行容器
       （左边草稿、右边成片，宽度不够时 flex-wrap 自动换回上下堆叠），
       收到第一条消息才显示对应预览。 */
    if (!runtime.__yanhuoPreviewRow) {
        const previewRow = document.createElement("div");
        previewRow.dataset.yanhuoPreviewRow = "1";
        previewRow.style.display = "flex";
        previewRow.style.flexWrap = "wrap";
        previewRow.style.alignItems = "flex-start";
        previewRow.style.gap = "8px";
        previewRow.style.flex = "0 0 auto";
        previewRow.style.marginTop = "6px";
        toolbar.insertAdjacentElement("afterend", previewRow);
        runtime.__yanhuoPreviewRow = previewRow;
        YANHUO_PREVIEW_ROWS.add(previewRow);
    }
    if (!runtime.__yanhuoDraftBox) {
        const draftBox = document.createElement("div");
        draftBox.dataset.yanhuoDraft = "1";
        draftBox.style.display = "none";
        draftBox.style.flexDirection = "column";
        draftBox.style.gap = "4px";
        /* v1.8.1：与右侧成片预览完全同一套尺寸常数 —— 相同 flex 基准 + 相同
           grow 让两者在并排时等宽，媒体元素固定同一像素高 + object-fit 让两者
           等高（不同长宽比也不再一大一小）。窄到换行时各自占满整行。 */
        draftBox.style.flex = "1 1 __PREVIEW_BASIS__px";
        draftBox.style.minWidth = "__PREVIEW_MIN_W__px";
        const draftImg = document.createElement("img");
        draftImg.style.width = "100%";
        draftImg.style.height = "__PREVIEW_H__px";
        draftImg.style.objectFit = "contain";
        draftImg.style.borderRadius = "6px";
        draftImg.style.border = "1px solid rgba(255,255,255,.18)";
        draftImg.style.background = "#000";
        draftImg.style.display = "block";
        const draftLabel = document.createElement("div");
        draftLabel.dataset.yanhuoDraftLabel = "1";
        draftLabel.style.fontSize = "11px";
        draftLabel.style.opacity = ".75";
        draftBox.appendChild(draftImg);
        draftBox.appendChild(draftLabel);
        runtime.__yanhuoPreviewRow.appendChild(draftBox);
        runtime.__yanhuoDraftBox = draftBox;
    }

    /* v1.7.0 逐段成片实时预览条：与草稿预览并排（右侧）。 */
    if (!runtime.__yanhuoFinalBox) {
        const finalBox = document.createElement("div");
        finalBox.dataset.yanhuoFinal = "1";
        finalBox.style.display = "none";
        finalBox.style.flexDirection = "column";
        finalBox.style.gap = "4px";
        /* v1.8.1：与左侧草稿预览共用同一套尺寸常数。 */
        finalBox.style.flex = "1 1 __PREVIEW_BASIS__px";
        finalBox.style.minWidth = "__PREVIEW_MIN_W__px";
        const finalVideo = document.createElement("video");
        finalVideo.controls = true;
        finalVideo.preload = "metadata";
        finalVideo.style.width = "100%";
        finalVideo.style.height = "__PREVIEW_H__px";
        finalVideo.style.objectFit = "contain";
        finalVideo.style.borderRadius = "6px";
        finalVideo.style.border = "1px solid rgba(255,255,255,.18)";
        finalVideo.style.background = "#000";
        finalVideo.style.display = "block";
        const finalLabelRow = document.createElement("div");
        finalLabelRow.style.display = "flex";
        finalLabelRow.style.alignItems = "center";
        finalLabelRow.style.gap = "10px";
        const finalLabel = document.createElement("div");
        finalLabel.dataset.yanhuoFinalLabel = "1";
        finalLabel.style.fontSize = "11px";
        finalLabel.style.opacity = ".75";
        finalLabel.style.flex = "1 1 auto";
        const finalLink = document.createElement("a");
        finalLink.dataset.yanhuoFinalLink = "1";
        finalLink.textContent = "⬇ 下载本段";
        finalLink.target = "_blank";
        finalLink.rel = "noopener";
        finalLink.style.fontSize = "11px";
        finalLink.style.opacity = ".75";
        finalLabelRow.appendChild(finalLabel);
        finalLabelRow.appendChild(finalLink);
        finalBox.appendChild(finalVideo);
        finalBox.appendChild(finalLabelRow);
        runtime.__yanhuoPreviewRow.appendChild(finalBox);
        runtime.__yanhuoFinalBox = finalBox;
    }

    /* v1.3.5 连跑开关 + 常驻「停在当前段」：插在「载入项目」后面。 */
    const chainBtn = document.createElement("button");
    chainBtn.type = "button";
    chainBtn.textContent = "🔗 连跑全部：关";
    chainBtn.addEventListener("click", (e) => {
        e.preventDefault();
        const rt = node.__h3Extender || runtime;
        const next = yanhuoIsFullBatch(node) ? "clip_by_clip" : "full_batch";
        if (!yanhuoSetRunMode(node, rt, next)) return;
        const pending = yanhuoPendingClips(rt);
        rt.statusText = next === "full_batch"
            ? `连跑已开启（run_mode=full_batch）：点一次 Queue 依次生成 ${pending} 个未完成片段；`
                + `中途可用「⏹ 停在当前段」安全停下，已完成片段留在缓存里可续跑。`
            : "连跑已关闭（run_mode=clip_by_clip）：恢复为一次 Queue 生成一个 CLIP，需手动勾「已校验」再排队。";
        render(node, rt);
    });

    const stopBtn = document.createElement("button");
    stopBtn.type = "button";
    stopBtn.textContent = "⏹ 停在当前段";
    stopBtn.style.display = "none";
    stopBtn.addEventListener("click", (e) => {
        e.preventDefault();
        requestFullBatchInterrupt(node, node.__h3Extender || runtime);
    });

    const loadBtn = Array.from(toolbar.children).find(
        (el) => el.tagName === "BUTTON"
            && /载入项目|Load Project/.test(el.textContent || ""),
    );
    if (loadBtn) {
        loadBtn.insertAdjacentElement("afterend", chainBtn);
        chainBtn.insertAdjacentElement("afterend", stopBtn);
    } else {
        toolbar.insertBefore(chainBtn, toolbar.firstChild);
        chainBtn.insertAdjacentElement("afterend", stopBtn);
    }
    runtime.__yanhuoChainButton = chainBtn;
    runtime.__yanhuoStopButton = stopBtn;

    /* 按钮状态跟着 activePhase / interruptRequested 走，轻量轮询即可；
       节点被移除后自动停掉定时器。 */
    if (!runtime.__yanhuoChainTimer) {
        runtime.__yanhuoChainTimer = setInterval(() => {
            const rt = node.__h3Extender;
            if (!rt?.root?.isConnected) {
                clearInterval(runtime.__yanhuoChainTimer);
                runtime.__yanhuoChainTimer = null;
                return;
            }
            yanhuoRefreshChainControls(node, rt);
        }, 700);
    }
    yanhuoRefreshChainControls(node, runtime);

    const compact = document.createElement("button");
    compact.type = "button";
    compact.textContent = "⇕ 紧凑";
    compact.title = "把节点收拢到最小高度（需要更大时拖节点下边缘即可，重新载入超过最小高度 1.6 倍会被自动收拢）";
    compact.addEventListener("click", (e) => {
        e.preventDefault();
        const rt = node.__h3Extender;
        if (!rt?.state) return;
        const y = Number(rt.domWidget?.last_y) || 0;
        const useNodes2 = domWidgetRenderMode(rt.root) === "nodes2";
        const panelMin = useNodes2
            ? nodes2MinHeightForState(rt.state)
            : uiMinHeightForState(rt.state);
        node.setSize([
            Math.max(NODE_MIN_WIDTH, Number(node.size?.[0] || NODE_MIN_WIDTH)),
            y + panelMin + BOTTOM_PAD,
        ]);
        requestAnimationFrame(() => {
            requestAnimationFrame(() => syncDomHeight(node, rt, true));
        });
        node.graph?.setDirtyCanvas(true, true);
    });
    toolbar.appendChild(compact);

    const picker = document.createElement("input");
    picker.type = "color";
    try { picker.value = localStorage.getItem(YANHUO_ACCENT_KEY) || YANHUO_DEFAULT_ACCENT; }
    catch (e) { picker.value = YANHUO_DEFAULT_ACCENT; }
    picker.title = "面板主题色（双击恢复默认淡紫）";
    picker.style.width = "26px";
    picker.style.height = "24px";
    picker.style.padding = "0";
    picker.style.flex = "0 0 auto";
    picker.style.cursor = "pointer";
    picker.addEventListener("input", () => yanhuoApplyAccent(picker.value));
    picker.addEventListener("dblclick", (e) => {
        e.preventDefault();
        yanhuoApplyAccent(YANHUO_DEFAULT_ACCENT);
        picker.value = YANHUO_DEFAULT_ACCENT;
    });
    toolbar.appendChild(picker);
}

app.registerExtension({
    name: "YanhuoH3.SelfLiftSigmasUI",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "YanhuoH3MotionContextSelfLift") return;

        const SIGMAS_INPUT = "selflift_sigmas";
        const BYPASSED_WIDGETS = ["scheduler", "steps", "denoise"];

        const sigmasConnected = (node) => {
            const input = (node.inputs || []).find((i) => i && i.name === SIGMAS_INPUT);
            return Boolean(input && input.link != null);
        };

        const applyTakeover = (node) => {
            const external = sigmasConnected(node);
            for (const name of BYPASSED_WIDGETS) {
                const widget = (node.widgets || []).find((w) => w && w.name === name);
                if (!widget) continue;
                widget.disabled = external;
                if (widget.inputEl) widget.inputEl.disabled = external;
            }
            node.setDirtyCanvas?.(true, true);
            /* 全局条里的「外部 SIGMAS 生效」徽章跟着连线状态刷新。 */
            const rt = node.__h3Extender;
            if (rt?.yanhuoStrip) yanhuoRefreshGlobalStrip(node, rt);
        };

        /* v1.3.2：新建 / 载入后一次性把节点收拢到紧凑高度。
           上游只在「lastRenderMode == null 的第一次 legacy 同步」时才修高度，
           而且门槛是 max(1200, min*1.6)；一旦首次同步发生在 Nodes 2.0 下（或
           面板晚于那一拍才布局完），之后再切回来就永远不会再修，节点会一直停在
           超长高度。这里自己兜一次：每个节点实例只做一次，超过目标 1.25 倍即收拢。 */
        const YANHUO_CLAMP_RATIO = 1.25;

        function yanhuoCompactTarget(node, runtime) {
            const y = Number(runtime?.domWidget?.last_y) || 0;
            if (!Number.isFinite(y) || y <= 0) return null;
            const useNodes2 = domWidgetRenderMode(runtime.root) === "nodes2";
            const panelMin = useNodes2
                ? nodes2MinHeightForState(runtime.state)
                : uiMinHeightForState(runtime.state);
            return y + panelMin + BOTTOM_PAD;
        }

        function yanhuoClampInitialHeight(node, attempt) {
            const runtime = node?.__h3Extender;
            if (!runtime || runtime.__yanhuoHeightClamped) return;
            const target = yanhuoCompactTarget(node, runtime);
            if (target == null) {
                /* 面板还没布局出 last_y，等下一拍再看（最多约 1.4 秒）。 */
                if ((attempt || 0) < 12) {
                    setTimeout(() => yanhuoClampInitialHeight(node, (attempt || 0) + 1), 120);
                }
                return;
            }
            runtime.__yanhuoHeightClamped = true;
            const current = Number(node.size?.[1]) || 0;
            if (current <= target * YANHUO_CLAMP_RATIO) return; /* 手动调大的，尊重 */
            const width = Math.max(NODE_MIN_WIDTH, Number(node.size?.[0]) || NODE_MIN_WIDTH);
            runtime.syncingDomHeight = true;
            try {
                node.setSize([width, target]);
            } finally {
                runtime.syncingDomHeight = false;
            }
            requestAnimationFrame(() => {
                requestAnimationFrame(() => syncDomHeight(node, runtime, true));
            });
            node.graph?.setDirtyCanvas(true, true);
        }

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const node = this;
            // Inputs are created during onNodeCreated; defer once so the
            // selflift_sigmas socket exists before the first check.
            setTimeout(() => applyTakeover(node), 0);
            // 工具栏（含主扩展注入的按钮）在主 onNodeCreated 里才建好，稍等一拍。
            setTimeout(() => yanhuoInjectToolbarControls(node), 80);
            // 高度：等面板布局稳定后再收拢一次（新建和载入都会走到这里）。
            setTimeout(() => yanhuoClampInitialHeight(node, 0), 260);
            return result;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const result = onConfigure ? onConfigure.apply(this, arguments) : undefined;
            const node = this;
            setTimeout(() => yanhuoClampInitialHeight(node, 0), 260);
            return result;
        };

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const result = onConnectionsChange ? onConnectionsChange.apply(this, arguments) : undefined;
            const node = this;
            setTimeout(() => applyTakeover(node), 0);
            return result;
        };
        /* v1.3.1：外接状态徽标从 canvas 改为全局条里的 DOM 徽章
           （yanhuoRefreshGlobalStrip 里刷新）——canvas 画的字会和输出端口
           名称重叠，而且改字号也躲不开。 */
    },
});
"""

# ---------------------------------------------------------------------------
# v1.3.0 全局 LoRA / 全局种子 / 「跟随上一片段」按钮。
# 原则：设置在前端 state.yanhuo_global 里随 clips_json 持久化，生效在
# 本包 node.py（交给父类前覆盖每个 clip 的 loras/seed）。逐片段原值永远
# 留在工作流里，关掉开关即恢复。这里只维护设置与可见性，不改主脚本结构。
# ---------------------------------------------------------------------------
GLOBALS_UI_TAIL = """

/* ------------------------------------------------------------------
 * Yanhuo addition (appended by tools/build_frontend.py): v1.3.0
 *   1) 全局条（参考图像与 CLIP 卡片之间）：全局 LoRA / 全局种子开关。
 *      开全局 LoRA：所有片段用同一份 LoRA 列表，各卡片的 LoRA 设置被忽略；
 *      开全局种子：所有片段用同一个种子，各卡片的种子被忽略。
 *   2) 每张卡片的骰子旁多一个「⧉ 上一段」：把上一片段的 LoRA 选择与强度
 *      复制到本片段（一次性复制，CLIP 1 没有上一段，按钮禁用）。
 * 与主脚本同处一个 module 作用域：这里直接包装顶层函数 serializeState /
 * parseState / mergeActiveStateJson / serializeProjectState，并复用
 * updateHidden / captureNativeWorkflowState / render / normalizeClipLora /
 * normalizeClipLoras / randomSeed / makeNumberInput / syncDomHeight。
 * 后端（node.py）在调用父类 extend() 之前读取 yanhuo_global 并覆盖 clips，
 * 缓存失效自动按覆盖后的值计算。
 * ------------------------------------------------------------------ */
const YANHUO_GLOBAL_LORA_BLOCK_PX = 118;

function normalizeYanhuoGlobal(value) {
    const v = value && typeof value === "object" ? value : {};
    const lora = v.global_lora && typeof v.global_lora === "object" ? v.global_lora : {};
    const seed = v.global_seed && typeof v.global_seed === "object" ? v.global_seed : {};
    const clampStrength = (n) => Math.max(-100, Math.min(100, Number(n)));
    /* v1.11.0：prompt_edit = { 片段 id: true } —— 该卡片的提示词框处于"编辑"态。
       只读态（默认）由 prompt_N 端口驱动；编辑态允许改，且运行时不被端口覆盖。 */
    const editRaw = v.prompt_edit && typeof v.prompt_edit === "object" ? v.prompt_edit : {};
    const promptEdit = {};
    for (const key of Object.keys(editRaw)) {
        const id = String(key || "").trim();
        if (id && editRaw[key]) promptEdit[id] = true;
    }
    return {
        prompt_edit: promptEdit,
        global_lora: {
            enabled: Boolean(lora.enabled),
            loras: Array.isArray(lora.loras)
                ? lora.loras
                      .map((entry) => ({
                          name: String(entry?.name || "").trim(),
                          strength: Number.isFinite(clampStrength(entry?.strength))
                              ? clampStrength(entry?.strength)
                              : 1.0,
                      }))
                      .filter((entry) => entry.name)
                : [],
        },
        global_seed: {
            enabled: Boolean(seed.enabled),
            /* v1.3.3：本次运行结束后全局种子如何变化。fixed=不变（v1.3.0 行为）；
               inc/dec=增减 1；random=换新随机种子。回溯按钮随时可恢复本次实际用的值。 */
            mode: ["fixed", "inc", "dec", "random"].includes(String(seed.mode))
                ? String(seed.mode)
                : "fixed",
            seed: Math.max(
                0,
                Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(Number(seed.seed ?? 0)) || 0),
            ),
        },
    };
}

function yanhuoClampSeed(value) {
    return Math.max(0, Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(Number(value) || 0)));
}

/* 从 onExecuted 的 h3_extender_state 里取「这次运行实际使用的种子」。
   后端的 clips_json 是全局覆盖之后的生效值（全局种子开时每个 clip 都等于全局种子），
   所以它就是"上次运行生成结果所用的种子"，逐 CLIP 一份，供回溯按钮使用。 */
function yanhuoSeedsFromExecuted(info) {
    if (!info || typeof info.clips_json !== "string" || !info.clips_json) return null;
    let clips = null;
    try {
        const parsed = JSON.parse(info.clips_json);
        clips = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.clips) ? parsed.clips : null;
    } catch (e) {
        return null;
    }
    if (!Array.isArray(clips)) return null;
    const list = [];
    for (const clip of clips) {
        if (!clip || typeof clip !== "object") continue;
        const seed = Number(clip.seed);
        if (!Number.isFinite(seed)) continue;
        list.push(yanhuoClampSeed(seed));
    }
    return list.length ? list : null;
}

/* 第 index 张卡片上次运行实际使用的种子（顺序即卡片顺序）。 */
function yanhuoLastRunSeedFor(runtime, index) {
    const list = runtime?.__yanhuoLastRunSeeds;
    if (!Array.isArray(list) || index < 0 || index >= list.length) return NaN;
    return list[index];
}

function yanhuoGlobalOf(runtime) {
    if (!runtime?.state) return null;
    if (!runtime.state.yanhuo_global) {
        runtime.state.yanhuo_global = normalizeYanhuoGlobal();
    }
    return runtime.state.yanhuo_global;
}

/* 全局条的固定开销：开关行 34px；展开 LoRA 编辑区时再加一块。 */
function yanhuoStripExtra(state) {
    return 34 + (state?.yanhuo_global?.global_lora?.enabled ? YANHUO_GLOBAL_LORA_BLOCK_PX : 0);
}

function yanhuoSigmasConnected(node) {
    return (node?.inputs || []).some(
        (input) => input && input.name === "selflift_sigmas" && input.link != null,
    );
}

/* ---- 持久化：把 yanhuo_global 搭在 clips_json 里，解析时再取回来 ---- */
const __yanhuoOrigSerializeState = serializeState;
serializeState = function (state) {
    const raw = __yanhuoOrigSerializeState(state);
    const g = state?.yanhuo_global;
    if (!g) return raw;
    try {
        const payload = JSON.parse(raw);
        if (payload && typeof payload === "object") {
            payload.yanhuo_global = normalizeYanhuoGlobal(g);
            return JSON.stringify(payload);
        }
    } catch (e) {}
    return raw;
};

const __yanhuoOrigSerializeProjectState = serializeProjectState;
serializeProjectState = function (state) {
    const raw = __yanhuoOrigSerializeProjectState(state);
    const g = state?.yanhuo_global;
    if (!g) return raw;
    try {
        const payload = JSON.parse(raw);
        if (payload && typeof payload === "object") {
            payload.yanhuo_global = normalizeYanhuoGlobal(g);
            return JSON.stringify(payload);
        }
    } catch (e) {}
    return raw;
};

const __yanhuoOrigParseState = parseState;
parseState = function (raw) {
    const state = __yanhuoOrigParseState(raw);
    let saved = null;
    try {
        const p = JSON.parse(raw || "{}");
        if (p && typeof p === "object") saved = p.yanhuo_global;
    } catch (e) {}
    state.yanhuo_global = normalizeYanhuoGlobal(saved);
    return state;
};

const __yanhuoOrigMergeActiveStateJson = mergeActiveStateJson;
mergeActiveStateJson = function (runtime, raw, explicitMode) {
    const result = __yanhuoOrigMergeActiveStateJson(runtime, raw, explicitMode);
    if (result && typeof result === "object" && !result.yanhuo_global) {
        let saved = null;
        try {
            const p = JSON.parse(raw || "{}");
            if (p && typeof p === "object") saved = p.yanhuo_global;
        } catch (e) {}
        result.yanhuo_global = normalizeYanhuoGlobal(saved || runtime?.state?.yanhuo_global);
    }
    return result;
};

/* ---- 每张卡片：骰子后的「⏮ 回溯」+ 提示词下方的「跟随上一段Lora」+ 全局抑制 ---- */
function yanhuoScanCards(node, runtime) {
    const cards = runtime?.cards;
    if (!cards) return;
    /* v1.12.1：connectedExternalPromptValue 只认 node.__h3Extender（上游就是这么
       取的）。万一挂载时机落后于首次扫描，编辑态那段保护会整个失效，这里补挂。 */
    if (node && !node.__h3Extender) node.__h3Extender = runtime;
    const g = yanhuoGlobalOf(runtime);
    if (!g) return;
    Array.from(cards.children).forEach((card) => {
        if (!(card instanceof HTMLElement)) return;
        const dice = card.querySelector(
            'button[title="随机种子"], button[title="Randomize seed"]',
        );
        /* loraGroup 是第一个子标签以 "LoRA N" / "添加 LoRA" 开头的纵向容器，
           「跟随上一段Lora」就插在它前面（即提示词框与 LoRA 1 之间）。 */
        const loraGroup = Array.from(card.children).find(
            (el) =>
                el.tagName === "DIV" &&
                /^(LoRA\\s|添加 LoRA)/.test((el.firstElementChild?.textContent || "").trim()),
        );
        if (card.dataset.yanhuoEnhanced !== "1") {
            card.dataset.yanhuoEnhanced = "1";
            /* v1.3.3：骰子后面的按钮换成「⏮ 回溯」——把上次运行实际使用的
               种子填回本卡片（随机跑完出好结果后一键复现）。 */
            if (dice) {
                const recall = document.createElement("button");
                recall.type = "button";
                recall.textContent = "⏮";
                recall.style.width = "32px";
                recall.style.flex = "0 0 auto";
                recall.addEventListener("click", (e) => {
                    e.preventDefault();
                    const rt = node.__h3Extender || runtime;
                    const index = Array.from(rt.cards.children).indexOf(card);
                    const seed = yanhuoLastRunSeedFor(rt, index);
                    if (!Number.isFinite(seed)) return;
                    const input = recall.parentElement?.querySelector("input");
                    if (!input) return;
                    input.value = String(seed);
                    /* 走主脚本自己的 change 处理：写回 clip.seed + updateHidden + render */
                    input.dispatchEvent(new Event("change", { bubbles: true }));
                });
                dice.insertAdjacentElement("afterend", recall);
                card.__yanhuoRecallButton = recall;
            }
            /* v1.3.3：原「⧉ 上一段」改名「跟随上一段Lora」，从种子行移到提示词下方。 */
            if (loraGroup) {
                const follow = document.createElement("button");
                follow.type = "button";
                follow.textContent = "跟随上一段Lora";
                follow.title = "把上一个 CLIP 的 LoRA 选择与强度复制到本片段（一次性复制，之后各自独立编辑）";
                follow.style.cssText =
                    "align-self:flex-start;padding:3px 10px;margin:4px 0 6px;" +
                    "font-size:10px;white-space:nowrap;";
                follow.addEventListener("click", (e) => {
                    e.preventDefault();
                    const rt = node.__h3Extender || runtime;
                    const list = rt?.state?.clips || [];
                    const index = Array.from(rt.cards.children).indexOf(card);
                    if (index <= 0 || !list[index - 1] || !list[index]) return;
                    list[index].loras = (list[index - 1].loras || []).map((entry) =>
                        normalizeClipLora(entry),
                    );
                    updateHidden(node, rt);
                    captureNativeWorkflowState(node, rt);
                    render(node, rt);
                });
                card.insertBefore(follow, loraGroup);
                card.__yanhuoFollowButton = follow;
            }
        }
        const follow = card.__yanhuoFollowButton;
        if (follow) {
            const index = Array.from(cards.children).indexOf(card);
            follow.disabled = index <= 0 || g.global_lora.enabled;
            follow.title = g.global_lora.enabled
                ? "全局 LoRA 生效中：本按钮暂不可用"
                : "把上一个 CLIP 的 LoRA 选择与强度复制到本片段（一次性复制，之后各自独立编辑）";
        }
        const recall = card.__yanhuoRecallButton;
        if (recall) {
            const index = Array.from(cards.children).indexOf(card);
            const lastSeed = yanhuoLastRunSeedFor(runtime, index);
            const has = Number.isFinite(lastSeed);
            recall.disabled = !has || g.global_seed.enabled;
            recall.title = g.global_seed.enabled
                ? "全局种子生效中：各卡片种子被忽略，请用全局条里的回溯按钮"
                : has
                  ? `回溯上次运行实际使用的种子（${lastSeed}）：替换本卡片当前种子，可复现上次结果`
                  : "还没有运行记录：先跑一次再点，可回溯该片段上次实际使用的种子";
        }
        /* 全局种子生效：压暗本卡片的种子输入 / 骰子 / 种子模式下拉 */
        const seedRow = dice?.parentElement;
        const seedBox = seedRow?.parentElement;
        if (seedBox) {
            const dim = g.global_seed.enabled ? "0.45" : "1";
            seedRow.style.opacity = dim;
            const modeSelect = seedBox.querySelector("select");
            if (modeSelect) modeSelect.style.opacity = dim;
        }
        /* 全局 LoRA 生效：压暗并锁住本卡片的 LoRA 区（loraGroup 在本函数开头已找）。 */
        if (loraGroup) {
            loraGroup.style.opacity = g.global_lora.enabled ? "0.45" : "1";
            loraGroup.style.pointerEvents = g.global_lora.enabled ? "none" : "";
        }
        /* v1.11.0：提示词端口的「刷新」+「只读 / 编辑」两个按钮。 */
        yanhuoEnhancePromptBox(node, runtime, cards, card);
    });
}

/* ------------------------------------------------------------------
 * v1.11.0：条件卡提示词（prompt）的「刷新」与「只读 / 编辑」
 *
 * 上游对 prompt_N 端口是"完全接管"：建卡片时灌值 + readOnly，之后每 500ms
 * 的镜像轮询把框和 clip.prompt 同步成端口文本，排队时后端再整体覆盖一次。
 * 这里加两个开关：
 *   刷新     —— 立刻把端口当前的提示词重新导入本框（不排队、不采样）。
 *   只读/编辑 —— 只读 = 上游默认行为；编辑 = 本框可改，改动写回 clip.prompt，
 *                且运行时**不会**被端口覆盖，只有点刷新 / 切回只读 / 新建
 *                项目才重新整体导入。
 *
 * 实现要点（改的是"取值入口"而不是复制上游逻辑）：
 *   把 connectedExternalPromptValue 包一层 —— 编辑态的卡片返回 null。上游的
 *   镜像循环遇到 null 就 continue（既不改框也不改 clip.prompt），建卡片时
 *   也会回落到 clip.prompt，正好显示已编辑的文本。刷新按钮要拿端口真值，
 *   所以直接调未包装的原函数。第三道（排队覆盖）由本包 node.py 拦：读
 *   yanhuo_global.prompt_edit 后从 payload 里摘掉对应的 prompt_N。
 * ------------------------------------------------------------------ */
const __yanhuoOrigConnectedPromptValue = connectedExternalPromptValue;

function yanhuoPromptEditMap(runtime) {
    const g = runtime?.state?.yanhuo_global;
    return g && g.prompt_edit && typeof g.prompt_edit === "object" ? g.prompt_edit : null;
}

/* 片段的两把钥匙：片段 id 与「clip_<序号>」。
   id 可能为空（后端归一化后靠位置补），所以两个都记，取的时候也两个都认。 */
function yanhuoPromptEditKeys(clip, index) {
    const keys = [];
    const id = String(clip?.id ?? "").trim();
    if (id) keys.push(id);
    if (Number.isInteger(index) && index >= 0) keys.push("clip_" + (index + 1));
    return keys;
}

/* v1.12.1：标记**双写**——
   * runtime.__yanhuoPromptEdit 直接挂在 runtime（= node.__h3Extender）上。
     runtime 本身是稳定的，而 runtime.state 会被 parseState / mergeActiveStateJson
     整个换掉；后端 onExecuted 返回的 clips_json 是 _state_json() 重建的，只有
     {version, clips}，yanhuo_global 不在里面 —— 标记只挂 state 上就会在
     「运行结束」这一刻丢失，卡片随即被 prompt_N 端口的文本冲掉。
   * yanhuo_global.prompt_edit 仍然写，用于随工作流持久化（后端 node.py 只读它）。
   读取时取并集，任何一处为真就是编辑态。 */
function yanhuoPromptEditable(runtime, clip, index) {
    const keys = yanhuoPromptEditKeys(clip, index);
    if (!keys.length) return false;
    const hard = runtime?.__yanhuoPromptEdit;
    if (hard && typeof hard === "object") {
        for (const key of keys) if (hard[key]) return true;
    }
    const map = yanhuoPromptEditMap(runtime);
    if (map) {
        for (const key of keys) if (map[key]) return true;
    }
    return false;
}

function yanhuoSetPromptEditable(runtime, clip, editable, index) {
    if (!runtime) return;
    const keys = yanhuoPromptEditKeys(clip, index);
    if (!keys.length) return;
    if (!runtime.__yanhuoPromptEdit || typeof runtime.__yanhuoPromptEdit !== "object") {
        runtime.__yanhuoPromptEdit = {};
    }
    const g = runtime?.state?.yanhuo_global;
    if (g && (!g.prompt_edit || typeof g.prompt_edit !== "object")) g.prompt_edit = {};
    for (const key of keys) {
        if (editable) {
            runtime.__yanhuoPromptEdit[key] = true;
            if (g) g.prompt_edit[key] = true;
        } else {
            delete runtime.__yanhuoPromptEdit[key];
            if (g) delete g.prompt_edit[key];
        }
    }
}

/* 编辑态返回 null —— 这是绕开上游 500ms 镜像与建卡灌值的唯一开关。
   v1.12.0：取值改走 yanhuoExternalPromptValue —— 逐段端口可能住在集合节点上，
   拿主节点的槽位号去解析连线会解析到别的端口上（见 PERCLIP_UI_TAIL）。 */
connectedExternalPromptValue = function (node, clipIndex) {
    const rt = node?.__h3Extender;
    const idx = Number(clipIndex) - 1;
    const clip = rt?.state?.clips?.[idx];
    if (yanhuoPromptEditable(rt, clip, idx)) return null;
    return yanhuoExternalPromptValue(node, clipIndex);
};

function yanhuoFindPromptBox(card) {
    return card.querySelector("textarea[data-h3-prompt-clip-id]");
}

/* 提示词框上面那行小标签（"提示词" / "提示词 (EXT)"）—— 按钮就挂在它右边，
   不新增一行，卡片高度预算不受影响。 */
function yanhuoFindPromptLabel(card) {
    return (
        Array.from(card.children).find(
            (el) =>
                el.tagName === "DIV" &&
                /^(提示词|Prompt)(\\s|$|\\()/.test((el.textContent || "").trim()),
        ) || null
    );
}

function yanhuoCardIndex(cards, card) {
    return Array.from(cards.children).indexOf(card);
}

function yanhuoEnhancePromptBox(node, runtime, cards, card) {
    const rt = node.__h3Extender || runtime;
    const index = yanhuoCardIndex(cards, card);
    const clip = rt?.state?.clips?.[index];
    const entry = findInputEntry(node, "prompt_" + (index + 1));
    const connected = Boolean(entry && inputConnected(entry.input));
    const box = yanhuoFindPromptBox(card);
    const label = yanhuoFindPromptLabel(card);

    if (label && label.dataset.yanhuoPromptBar !== "1") {
        label.dataset.yanhuoPromptBar = "1";
        label.style.display = "flex";
        label.style.alignItems = "center";
        label.style.gap = "6px";
        const spacer = document.createElement("span");
        spacer.style.flex = "1 1 auto";

        const refresh = document.createElement("button");
        refresh.type = "button";
        refresh.textContent = "刷新";
        refresh.style.cssText = "font-size:10px;padding:1px 7px;line-height:1.3;flex:0 0 auto;";
        refresh.addEventListener("click", (e) => {
            e.preventDefault();
            const now = node.__h3Extender || runtime;
            const idx = yanhuoCardIndex(now.cards, card);
            const c = now?.state?.clips?.[idx];
            const external = yanhuoExternalPromptValue(node, idx + 1);
            if (!c) return;
            if (external === null) {
                /* v1.12.1：别再静默失败。取值失败基本只有两种原因，按钮上直接
                   说清楚，免得以为"刷新坏了"。 */
                const why = __yanhuoFindInputEntry(node, "prompt_" + (idx + 1))
                    ? "上游节点没有可读的文本控件（拼接/处理类节点要运行后才产出文本，前端读不到）"
                    : "集合节点上 prompt_" + (idx + 1) + " 没接线，或 per_clip_inputs 没连";
                refresh.textContent = "取不到值";
                refresh.title = why;
                setTimeout(() => {
                    refresh.textContent = "刷新";
                    refresh.title =
                        "把 prompt_" + (idx + 1) + " 端口当前的提示词重新导入本框（不运行采样）";
                }, 2200);
                return;
            }
            c.prompt = external;
            const b = yanhuoFindPromptBox(card);
            if (b) b.value = external;
            updateHidden(node, now);
            captureNativeWorkflowState(node, now);
            notifyWorkflowChanged(node, now);
        });

        const lock = document.createElement("button");
        lock.type = "button";
        lock.textContent = "只读";
        lock.style.cssText = "font-size:10px;padding:1px 7px;line-height:1.3;flex:0 0 auto;";
        lock.addEventListener("click", (e) => {
            e.preventDefault();
            const now = node.__h3Extender || runtime;
            const idx = yanhuoCardIndex(now.cards, card);
            const c = now?.state?.clips?.[idx];
            if (!c) return;
            const editable = yanhuoPromptEditable(now, c, idx);
            if (editable) {
                /* 切回只读：端口文本整体覆盖回来，与上游默认行为对齐。 */
                const external = yanhuoExternalPromptValue(node, idx + 1);
                if (external !== null) c.prompt = external;
            }
            yanhuoSetPromptEditable(now, c, !editable, idx);
            updateHidden(node, now);
            captureNativeWorkflowState(node, now);
            notifyWorkflowChanged(node, now);
            render(node, now);
        });

        label.append(spacer, refresh, lock);
        card.__yanhuoPromptRefresh = refresh;
        card.__yanhuoPromptLock = lock;
    }

    const refreshBtn = card.__yanhuoPromptRefresh;
    const lockBtn = card.__yanhuoPromptLock;
    if (refreshBtn && lockBtn) {
        /* 端口没连就没有"外部提示词"可刷，两个按钮一并隐藏。 */
        refreshBtn.style.display = connected ? "" : "none";
        lockBtn.style.display = connected ? "" : "none";
        if (connected) {
            const editable = yanhuoPromptEditable(rt, clip, index);
            lockBtn.textContent = editable ? "编辑" : "只读";
            lockBtn.title = editable
                ? "编辑态：本框可改，改动写回本片段，运行时不会被 prompt_" +
                  (index + 1) +
                  " 覆盖。再点一次切回只读（端口提示词整体覆盖回来）。"
                : "只读态：由 prompt_" +
                  (index + 1) +
                  " 端口驱动，改不动。再点一次切到编辑态（可自由修改，运行时不被覆盖）。";
            refreshBtn.title =
                "把 prompt_" + (index + 1) + " 端口当前的提示词重新导入本框（不运行采样）";
        }
    }

    if (box && clip) {
        /* v1.12.1：readOnly 显式给值，不再只在 connected 时才动。
           端口没连（per_clip_inputs 没连、或集合节点上这个 prompt_N 没连）
           就必须能自由编辑 —— 上游只在 promptExternallyDriven 时置
           readOnly，而旧工作流残留的空插座会让那个判定摇摆不定。 */
        const editable = !connected || yanhuoPromptEditable(rt, clip, index);
        box.readOnly = !editable;
        box.style.opacity = connected && !editable ? ".85" : "1";
        if (box.dataset.yanhuoPromptHooked !== "1") {
            box.dataset.yanhuoPromptHooked = "1";
            /* 上游的 input 处理在 promptExternallyDriven 时直接 return，
               编辑态这条写回路得自己补。不 re-render，避免打字时丢焦点。 */
            box.addEventListener("input", () => {
                const now = node.__h3Extender || runtime;
                const cardEl = box.parentElement;
                if (!cardEl || !now?.cards) return;
                const idx = yanhuoCardIndex(now.cards, cardEl);
                const c = now?.state?.clips?.[idx];
                if (!c || !yanhuoPromptEditable(now, c, idx)) return;
                if (box.value === c.prompt) return;
                c.prompt = box.value;
                updateHidden(node, now);
                captureNativeWorkflowState(node, now);
            });
        }
    }
}

/* ---- 全局条 UI ---- */
function yanhuoRefreshGlobalStrip(node, runtime) {
    const strip = runtime?.yanhuoStrip;
    const g = yanhuoGlobalOf(runtime);
    if (!strip || !g) return;
    strip.style.height = `${yanhuoStripExtra(runtime.state)}px`;

    const refs = strip.__yanhuoRefs || {};
    refs.loraToggle.checked = g.global_lora.enabled;
    refs.seedToggle.checked = g.global_seed.enabled;
    refs.seedEditor.style.display = g.global_seed.enabled ? "flex" : "none";
    /* v1.3.1：外接 SIGMAS 状态徽章（替代原来画在 canvas 上的徽标，避免和
       输出端口文字重叠）。连线状态变化时由 applyTakeover 触发到这里。 */
    if (refs.sigmasChip) {
        refs.sigmasChip.style.display = yanhuoSigmasConnected(node) ? "inline-flex" : "none";
    }
    if (document.activeElement !== refs.seedInput) {
        refs.seedInput.value = String(g.global_seed.seed);
    }
    /* v1.3.3：种子行为下拉 + 回溯按钮。 */
    if (refs.seedMode) {
        refs.seedMode.value = g.global_seed.mode || "fixed";
        refs.seedMode.disabled = !g.global_seed.enabled;
        refs.seedMode.style.opacity = g.global_seed.enabled ? "1" : "0.45";
    }
    if (refs.seedRecall) {
        const used = runtime.__yanhuoLastRunSeed;
        const has = Number.isFinite(used);
        refs.seedRecall.disabled = !g.global_seed.enabled || !has;
        refs.seedRecall.title = !g.global_seed.enabled
            ? "开启「全局种子」后可用：回溯上次运行实际使用的种子"
            : has
              ? `回溯上次运行实际使用的种子（${used}）：替换当前全局种子，可复现上次结果`
              : "还没有运行记录：先跑一次再点，可回溯那次实际使用的种子";
    }
    refs.loraBlock.style.display = g.global_lora.enabled ? "flex" : "none";

    if (g.global_lora.enabled) {
        refs.loraRows.replaceChildren();
        const rows = [...g.global_lora.loras, null]; /* null = 添加行 */
        for (let i = 0; i < rows.length; i++) {
            const cfg = rows[i];
            const isAddRow = cfg === null;
            const rowEl = document.createElement("div");
            rowEl.style.display = "grid";
            rowEl.style.gridTemplateColumns = "minmax(0, 1fr) 64px";
            rowEl.style.gap = "6px";
            rowEl.style.alignItems = "center";

            const select = document.createElement("select");
            select.style.width = "100%";
            select.style.minWidth = "0";
            select.style.boxSizing = "border-box";
            select.style.padding = "3px 5px";
            select.title = isAddRow
                ? "选择一个 LoRA 追加到全局列表（对全部片段生效）"
                : "选择 (移除 LoRA) 从全局列表移除这一项";
            const noneOption = document.createElement("option");
            noneOption.value = "";
            noneOption.textContent = isAddRow ? "+ 添加全局 LoRA" : "(移除 LoRA)";
            select.appendChild(noneOption);
            const names = Array.isArray(runtime.loraNames) ? [...runtime.loraNames] : [];
            const current = isAddRow ? "" : String(cfg.name || "");
            if (current && !names.includes(current)) names.unshift(current);
            for (const name of names) {
                if (!isAddRow && name !== current && g.global_lora.loras.some((e) => e.name === name)) {
                    continue;
                }
                const option = document.createElement("option");
                option.value = name;
                option.textContent = name;
                select.appendChild(option);
            }
            select.value = current;
            const rowIndex = i;
            select.addEventListener("change", () => {
                const rt = node.__h3Extender || runtime;
                const gg = yanhuoGlobalOf(rt);
                if (!gg) return;
                const name = String(select.value || "").trim();
                if (isAddRow) {
                    if (name) gg.global_lora.loras.push({ name, strength: 1.0 });
                } else if (!name) {
                    gg.global_lora.loras.splice(rowIndex, 1);
                } else {
                    gg.global_lora.loras[rowIndex].name = name;
                }
                updateHidden(node, rt);
                captureNativeWorkflowState(node, rt);
                yanhuoRefreshGlobalStrip(node, rt);
                render(node, rt);
            });

            const strength = makeNumberInput(
                isAddRow ? 1.0 : cfg.strength,
                -100,
                100,
                0.01,
            );
            strength.title = "全局 LoRA 强度（对全部片段生效）";
            strength.disabled = isAddRow || !current;
            strength.style.opacity = !isAddRow && current ? "1" : "0.45";
            strength.addEventListener("change", () => {
                const rt = node.__h3Extender || runtime;
                const gg = yanhuoGlobalOf(rt);
                if (!gg || isAddRow || !gg.global_lora.loras[rowIndex]) return;
                gg.global_lora.loras[rowIndex].strength = Math.max(
                    -100,
                    Math.min(100, Number(strength.value || 0)),
                );
                updateHidden(node, rt);
                captureNativeWorkflowState(node, rt);
            });

            rowEl.append(select, strength);
            refs.loraRows.appendChild(rowEl);
        }
    }
    yanhuoScanCards(node, runtime);
}

function yanhuoEnsureGlobalStrip(node, runtime) {
    const root = runtime?.root;
    const cards = runtime?.cards;
    if (!root || !cards || !cards.parentElement) return;
    yanhuoGlobalOf(runtime);
    if (runtime.yanhuoStrip) {
        yanhuoRefreshGlobalStrip(node, runtime);
        return;
    }

    const strip = document.createElement("div");
    strip.dataset.yanhuoGlobalStrip = "1";
    strip.style.width = "100%";
    strip.style.minWidth = "0";
    strip.style.boxSizing = "border-box";
    strip.style.display = "flex";
    strip.style.flexDirection = "column";
    strip.style.gap = "4px";
    strip.style.overflow = "hidden";
    strip.style.flex = "0 0 auto";
    strip.style.margin = "2px 0 4px";

    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.gap = "10px";
    row.style.minHeight = "30px";
    row.style.flex = "0 0 auto";

    const makeChip = (label, tooltip) => {
        const chip = document.createElement("label");
        chip.style.cssText =
            "display:inline-flex;align-items:center;gap:5px;cursor:pointer;" +
            "font-size:11px;font-weight:700;white-space:nowrap;letter-spacing:.3px;";
        chip.title = tooltip;
        const box = document.createElement("input");
        box.type = "checkbox";
        chip.append(box, document.createTextNode(label));
        row.appendChild(chip);
        return box;
    };

    const loraToggle = makeChip(
        "全局 LoRA",
        "开启后所有片段使用下面这份全局 LoRA 列表，各卡片自己的 LoRA 设置被忽略；关闭后恢复各卡片自己的选择。",
    );
    const seedToggle = makeChip(
        "全局种子",
        "开启后所有片段使用同一个种子；关闭后恢复各卡片自己的种子。",
    );

    const seedEditor = document.createElement("div");
    seedEditor.style.display = "none";
    seedEditor.style.alignItems = "center";
    seedEditor.style.gap = "5px";
    const seedInput = makeNumberInput(0, 0, Number.MAX_SAFE_INTEGER, 1);
    seedInput.style.width = "118px";
    seedInput.title = "全局种子：所有片段都用这个值生成";
    seedInput.addEventListener("change", () => {
        const rt = node.__h3Extender || runtime;
        const g = yanhuoGlobalOf(rt);
        if (!g) return;
        g.global_seed.seed = Math.max(
            0,
            Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(Number(seedInput.value || 0))),
        );
        updateHidden(node, rt);
        captureNativeWorkflowState(node, rt);
        yanhuoRefreshGlobalStrip(node, rt);
    });
    const seedDice = document.createElement("button");
    seedDice.type = "button";
    seedDice.textContent = "🎲";
    seedDice.title = "随机生成一个全局种子";
    seedDice.style.width = "32px";
    seedDice.addEventListener("click", (e) => {
        e.preventDefault();
        const rt = node.__h3Extender || runtime;
        const g = yanhuoGlobalOf(rt);
        if (!g) return;
        g.global_seed.seed = randomSeed();
        updateHidden(node, rt);
        captureNativeWorkflowState(node, rt);
        yanhuoRefreshGlobalStrip(node, rt);
    });
    seedEditor.append(seedInput, seedDice);

    /* v1.3.3：本次运行结束后全局种子如何变化（固定 / +1 / −1 / 随机）。 */
    const seedMode = document.createElement("select");
    seedMode.style.cssText =
        "width:88px;box-sizing:border-box;background:rgba(0,0,0,.25);" +
        "border:1px solid rgba(255,255,255,.15);color:inherit;border-radius:5px;" +
        "padding:3px 4px;font-size:11px;";
    seedMode.title =
        "本次运行结束后全局种子如何变化：固定=不变；下个+1/−1=增减 1；下个随机=换一个新随机种子。" +
        "选「下个随机」跑出好结果后，点旁边的 ⏮ 就能回溯本次实际使用的种子。";
    for (const [value, label] of [
        ["fixed", "固定"],
        ["inc", "下个 +1"],
        ["dec", "下个 −1"],
        ["random", "下个随机"],
    ]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        seedMode.appendChild(option);
    }
    seedMode.addEventListener("change", () => {
        const rt = node.__h3Extender || runtime;
        const g = yanhuoGlobalOf(rt);
        if (!g) return;
        g.global_seed.mode = seedMode.value;
        updateHidden(node, rt);
        captureNativeWorkflowState(node, rt);
        yanhuoScanCards(node, rt);
    });

    /* v1.3.3：回溯上次运行实际使用的全局种子（随机跑完可一键复现）。 */
    const seedRecall = document.createElement("button");
    seedRecall.type = "button";
    seedRecall.textContent = "⏮ 回溯";
    seedRecall.style.padding = "3px 8px";
    seedRecall.style.whiteSpace = "nowrap";
    seedRecall.addEventListener("click", (e) => {
        e.preventDefault();
        const rt = node.__h3Extender || runtime;
        const g = yanhuoGlobalOf(rt);
        const used = rt?.__yanhuoLastRunSeed;
        if (!g || !Number.isFinite(used)) return;
        g.global_seed.seed = yanhuoClampSeed(used);
        updateHidden(node, rt);
        captureNativeWorkflowState(node, rt);
        yanhuoRefreshGlobalStrip(node, rt);
        render(node, rt);
    });
    seedEditor.append(seedMode, seedRecall);
    row.appendChild(seedEditor);

    const hint = document.createElement("span");
    hint.style.cssText = "font-size:10px;opacity:.55;white-space:nowrap;overflow:hidden;";
    hint.textContent = "全局生效中 — 各卡片的对应设置被忽略";
    hint.style.marginLeft = "auto";
    row.appendChild(hint);

    /* v1.3.1：外接 SIGMAS 生效徽章。放在全局条内而不是节点标题区，
       避免与输出端口文字重叠；由 applyTakeover 在连线变化时刷新。 */
    const sigmasChip = document.createElement("span");
    sigmasChip.style.cssText =
        "display:none;align-items:center;gap:4px;padding:2px 7px;border-radius:9px;" +
        "font-size:10px;font-weight:700;white-space:nowrap;" +
        "border:1px solid color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 55%, transparent);" +
        "background:color-mix(in srgb, var(--yanhuo-accent, #b8a1e8) 22%, transparent);";
    sigmasChip.title = "外接 selflift_sigmas 已生效：steps / scheduler / denoise 三个控件本次被旁路，采样完全使用外部 sigma 表。";
    sigmasChip.textContent = "外部 SIGMAS 生效";
    row.appendChild(sigmasChip);
    strip.appendChild(row);

    const loraBlock = document.createElement("div");
    loraBlock.style.display = "none";
    loraBlock.style.flexDirection = "column";
    loraBlock.style.gap = "4px";
    loraBlock.style.overflowY = "auto";
    loraBlock.style.maxHeight = `${YANHUO_GLOBAL_LORA_BLOCK_PX - 4}px`;
    loraBlock.style.minHeight = "0";
    const loraRows = document.createElement("div");
    loraRows.style.display = "flex";
    loraRows.style.flexDirection = "column";
    loraRows.style.gap = "4px";
    loraBlock.appendChild(loraRows);
    strip.appendChild(loraBlock);

    cards.parentElement.insertBefore(strip, cards);
    runtime.yanhuoStrip = strip;
    strip.__yanhuoRefs = {
        loraToggle,
        seedToggle,
        seedEditor,
        seedInput,
        seedMode,
        seedRecall,
        loraBlock,
        loraRows,
        hint,
        sigmasChip,
    };

    loraToggle.addEventListener("change", () => {
        const rt = node.__h3Extender || runtime;
        const g = yanhuoGlobalOf(rt);
        if (!g) return;
        g.global_lora.enabled = loraToggle.checked;
        updateHidden(node, rt);
        captureNativeWorkflowState(node, rt);
        yanhuoRefreshGlobalStrip(node, rt);
        render(node, rt);
    });
    seedToggle.addEventListener("change", () => {
        const rt = node.__h3Extender || runtime;
        const g = yanhuoGlobalOf(rt);
        if (!g) return;
        g.global_seed.enabled = seedToggle.checked;
        /* 打开瞬间用当前 CLIP 1 的种子做初值，避免每次都从 0 开始 */
        if (seedToggle.checked && !g.global_seed.seed && rt.state.clips?.[0]) {
            g.global_seed.seed = Math.max(0, Math.trunc(Number(rt.state.clips[0].seed || 0)));
        }
        updateHidden(node, rt);
        captureNativeWorkflowState(node, rt);
        yanhuoRefreshGlobalStrip(node, rt);
        render(node, rt);
    });

    /* 卡片每次都会 replaceChildren 重建，重建后重新注入按钮并刷新抑制状态 */
    runtime.yanhuoCardObserver = new MutationObserver(() => {
        requestAnimationFrame(() => yanhuoScanCards(node, runtime));
    });
    runtime.yanhuoCardObserver.observe(cards, { childList: true });

    yanhuoRefreshGlobalStrip(node, runtime);
}

app.registerExtension({
    name: "YanhuoH3.SelfLiftGlobalsUI",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "YanhuoH3MotionContextSelfLift") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const node = this;
            const runtime = node.__h3Extender;
            if (runtime) {
                runtime.__yanhuoNode = node;
                yanhuoEnsureGlobalStrip(node, runtime);
            }
            return result;
        };

        /* v1.3.3：运行结束（onExecuted）做两件事——
           1) 从返回的 clips_json 里记下「这次实际使用的种子」（全局覆盖后的生效值，
              逐 CLIP 一份 + 全局一份），供全局条与各卡片的 ⏮ 回溯按钮使用；
           2) 按「下个种子行为」推进全局种子（固定=不动；+1/−1；随机）。
              回溯用的快照先记后推，所以随机跑完点 ⏮ 拿到的一定是刚才用的值。 */
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const result = onExecuted ? onExecuted.apply(this, arguments) : undefined;
            const node = this;
            const runtime = node.__h3Extender;
            if (!runtime) return result;
            const info = message?.h3_extender_state?.[0];
            const seeds = yanhuoSeedsFromExecuted(info);
            if (seeds) {
                runtime.__yanhuoLastRunSeeds = seeds;
                runtime.__yanhuoLastRunSeed = seeds[0];
                const g = yanhuoGlobalOf(runtime);
                if (g?.global_seed?.enabled) {
                    const mode = g.global_seed.mode || "fixed";
                    if (mode === "inc") g.global_seed.seed = yanhuoClampSeed(Number(g.global_seed.seed) + 1);
                    else if (mode === "dec") g.global_seed.seed = yanhuoClampSeed(Number(g.global_seed.seed) - 1);
                    else if (mode === "random") g.global_seed.seed = randomSeed();
                    if (mode !== "fixed") {
                        updateHidden(node, runtime);
                        captureNativeWorkflowState(node, runtime);
                    }
                }
                yanhuoRefreshGlobalStrip(node, runtime);
                yanhuoScanCards(node, runtime);
            }
            return result;
        };
    },
});
"""

# ---------------------------------------------------------------------------
# v1.12.0：逐段端口搬到「Yanhuo H3 逐段输入集合」节点之后的"透传层"
#
# 后端已经把 ref_pack_N / prompt_N / duration_N / ref_audio_N_k 从主节点的
# INPUT_TYPES 里删掉了，主节点只留一个 per_clip_inputs 聚合端口。但上游那套
# UI 是**按端口名**找插座的（500ms 镜像、建卡灌值、卡片上的 (EXT) 标记、
# v1.11.0 的刷新/只读按钮……一共十几处 findInputEntry(node, "prompt_3")）。
#
# 逐个改那十几处既容易漏又会和上游更新打架，所以这里只改**一个入口**：
# 让 findInputEntry 在"本节点没有这个逐段插座"时，顺着 per_clip_inputs 的
# 连线到集合节点上找。上游代码一行不动，全部自动生效。
#
# 配套两道护栏（缺一个就会出事）：
#   * addDynamicRefInput   —— 上游的动态插座同步会按片段数往上加插座；
#     透传命中集合节点的插座后它会试图在主节点上建一个同名插座，必须拦掉，
#     否则主节点又被撑长了。
#   * removeDynamicRefInput —— 更要拦。它拿的是**集合节点的槽位号**，却对
#     主节点调 removeInput(slot)，槽位号对不上就是删错插座。
# ---------------------------------------------------------------------------
PERCLIP_UI_TAIL = """
/* ---- v1.12.0：逐段端口住在集合节点上，主节点只做透传 ---- */
const YANHUO_PER_CLIP_RE = /^(?:ref_pack|prompt|duration|ref_audios)_\\d+$|^ref_audio_\\d+_\\d+$/;
const YANHUO_BUNDLE_INPUT = "per_clip_inputs";

/* 未包装的原始查找：透传逻辑自己必须先能"只在本节点上找"。 */
const __yanhuoFindInputEntry = findInputEntry;

function yanhuoPerClipSource(node) {
    /* 顺着 per_clip_inputs 的连线找到集合节点。没连、连的是自己、或上游
       没产出都返回 null —— null 时所有逐段端口都等价于"没连"。 */
    if (!node || typeof inputLinkAtSlot !== "function") return null;
    const entry = __yanhuoFindInputEntry(node, YANHUO_BUNDLE_INPUT);
    if (!entry || !inputConnected(entry.input)) return null;
    const link = inputLinkAtSlot(node, entry.slot);
    if (!link) return null;
    let source = null;
    try {
        source = node.graph?.getNodeById?.(link.origin_id) || null;
    } catch (_) {
        source = null;
    }
    return source && source !== node ? source : null;
}

findInputEntry = function (node, name) {
    const own = __yanhuoFindInputEntry(node, name);
    /* v1.12.1：老工作流里主节点上往往还残留着 v1.12.0 之前的同名插座 ——
       LiteGraph 是从保存的 JSON 里恢复 inputs 的，INPUT_TYPES 把它们删了，
       存过的工作流里它们照样在。**只要那个残留插座没接线，就不能让它挡住
       集合节点**，否则 prompt_N / ref_pack_N 一律解析成"未连接"：刷新按钮
       点了没反应、(EXT) 标记不出现、参考图不进卡片。
       残留插座**接了线**则说明这条是用户留着的直连，按老语义走，绝不抢。 */
    if (own && inputConnected(own.input)) return own;
    if (!YANHUO_PER_CLIP_RE.test(String(name || ""))) return own || null;
    const source = yanhuoPerClipSource(node);
    if (!source) return own || null;
    const remote = __yanhuoFindInputEntry(source, name);
    return remote || own || null;
};

const __yanhuoAddDynamicRefInput = addDynamicRefInput;
addDynamicRefInput = function (node, name, type, tooltip) {
    /* 主节点上永远不建逐段插座——它们属于集合节点。 */
    if (YANHUO_PER_CLIP_RE.test(String(name || "")) && !__yanhuoFindInputEntry(node, name)) {
        return false;
    }
    return __yanhuoAddDynamicRefInput(node, name, type, tooltip);
};

const __yanhuoRemoveDynamicRefInput = removeDynamicRefInput;
removeDynamicRefInput = function (node, name, snapshot) {
    /* 同上，而且这一条更关键：透传回来的槽位号是集合节点的，
       拿它去删主节点的插座会删错东西。 */
    if (YANHUO_PER_CLIP_RE.test(String(name || "")) && !__yanhuoFindInputEntry(node, name)) {
        return false;
    }
    return __yanhuoRemoveDynamicRefInput(node, name, snapshot);
};

/* ---- 取值入口也要重定向 ----
 *
 * 透传有个陷阱：findInputEntry 返回的 slot 是**集合节点**的槽位号，而
 * connectedExternalPromptValue/DurationValue 下一步是
 * ``inputLinkAtSlot(node, entry.slot)`` —— node 还是主节点。拿集合节点的
 * 槽位号去主节点上找连线，找到的是完全不相干的另一条线（最常见是解析到
 * model 或 per_clip_inputs 自己），于是端口值永远读不到。
 *
 * 所以"取端口当前值"这两个函数必须自己在集合节点上解析——它们内部用的
 * node 和 slot 才对得上。只查连接状态的地方（建卡、(EXT) 标记、刷新/只读
 * 按钮的显示）用透传就够了，因为它们只碰 entry.input，不碰 entry.slot。
 */
const __yanhuoOrigConnectedDurationValue = connectedExternalDurationValue;

function yanhuoExternalPromptValue(node, clipIndex) {
    /* 端口真值（不受"编辑态"影响 —— 刷新按钮要的就是这个）。
       v1.12.1 两处修正：
       1) 残留但**未接线**的同名插座不抢集合节点（理由见 findInputEntry）。
       2) 底层那条 __yanhuoOrigConnectedPromptValue(node, ...) 内部是
          findInputEntry + inputLinkAtSlot，node 与 slot 必须同源；"本节点这条
          没接"时绝不能拿主节点去解析 —— 槽位号会落到完全不相干的另一条线上。 */
    const own = __yanhuoFindInputEntry(node, "prompt_" + clipIndex);
    if (own && inputConnected(own.input)) {
        const local = __yanhuoOrigConnectedPromptValue(node, clipIndex);
        if (local !== null) return local;
    }
    const source = yanhuoPerClipSource(node);
    if (source) {
        const remote = __yanhuoOrigConnectedPromptValue(source, clipIndex);
        if (remote !== null) return remote;
    }
    return null;
}

function yanhuoExternalDurationValue(node, clipIndex) {
    const own = __yanhuoFindInputEntry(node, "duration_" + clipIndex);
    if (own && inputConnected(own.input)) {
        const local = __yanhuoOrigConnectedDurationValue(node, clipIndex);
        if (local !== null) return local;
    }
    const source = yanhuoPerClipSource(node);
    if (source) {
        const remote = __yanhuoOrigConnectedDurationValue(source, clipIndex);
        if (remote !== null) return remote;
    }
    return null;
}

/* ---- v1.12.1：清掉主节点上残留的旧逐段插座 ----
 *
 * 只删**没接线**的。接了线的说明用户还在直连，删了就丢数据，那种情况交给
 * findInputEntry 的"有线优先"逻辑处理，行为与 v1.12.0 之前一致。
 * 删完节点也短一截（旧工作流上那一长排空插座终于没了）。
 */
function yanhuoPruneLegacyPerClipInputs(node) {
    if (!node || !Array.isArray(node.inputs) || !node.inputs.length) return 0;
    let removed = 0;
    for (let slot = node.inputs.length - 1; slot >= 0; slot--) {
        const input = node.inputs[slot];
        if (!input) continue;
        if (!YANHUO_PER_CLIP_RE.test(String(input.name || ""))) continue;
        if (input.link !== null && input.link !== undefined) continue;
        try {
            node.removeInput(slot);
            removed += 1;
        } catch (_) {
            /* 前端版本差异导致的删不掉就算了，findInputEntry 那边还有兜底。 */
        }
    }
    if (removed > 0) {
        console.info(
            "[Yanhuo H3 SelfLift] v1.12.1：已清理 " + removed + " 个空的旧逐段端口"
            + "（ref_pack_N / prompt_N / duration_N / ref_audio_N_k 已搬到"
            + "「Yanhuo H3 逐段输入集合」节点）。",
        );
    }
    return removed;
}

connectedExternalDurationValue = function (node, clipIndex) {
    return yanhuoExternalDurationValue(node, clipIndex);
};

/* ---- v1.12.1：Get / Set / 文本处理这类"代理节点"要先顺着输入链走 ----
 *
 * 上游 textWidgetValueFromNode 的写法是"先信自己身上的字符串控件，找不到再
 * 顺着输入链走"，注释里写的是反过来的。Get 节点身上恰好有一个 combo 控件，
 * 值是查找用的令牌（不是真正的提示词），于是永远命中第一个分支 —— 这就是
 * 「接了 Get 节点，点刷新什么都刷不出来」的原因。
 *
 * 这里按"是不是代理节点"分流：纯导管（没控件）、combo 控件、或类名里带
 * get/set/reroute/primitive 等关键字的，一律先走输入链，拿不到再退回自己。
 */
const __yanhuoOrigTextWidgetValueFromNode = textWidgetValueFromNode;
const YANHUO_PROXY_CLASS_RE = /(get|set|reroute|route|switch|relay|pass|bridge|primitive|pipe|bus)/i;

function yanhuoLooksLikeProxy(source) {
    const cls = String(source?.comfyClass || source?.type || "");
    if (cls && YANHUO_PROXY_CLASS_RE.test(cls)) return true;
    const widgets = Array.isArray(source?.widgets) ? source.widgets : [];
    /* 纯导管：自己一个控件都没有，值只能从上游来。 */
    if (!widgets.length) return true;
    /* combo 控件的值是"选哪个"的令牌，不是真正的内容（Get 节点就是这种）。 */
    return widgets.some(
        (w) => w && !w.disabled && String(w?.type || "").toLowerCase() === "combo",
    );
}

function yanhuoLinkedInputs(source) {
    if (!Array.isArray(source?.inputs)) return [];
    const out = [];
    for (let slot = 0; slot < source.inputs.length; slot++) {
        const input = source.inputs[slot];
        if (!input || input.link === null || input.link === undefined) continue;
        out.push(slot);
    }
    return out;
}

textWidgetValueFromNode = function (source, depth = 0, seenNodes = null) {
    if (!source) return null;
    const seen = seenNodes instanceof Set ? seenNodes : new Set();
    if (depth > 4 || (source.id != null && seen.has(source.id))) return null;
    const linked = yanhuoLinkedInputs(source);
    /* 只有"看起来像代理"且**唯一**一条输入链时才顺着走。
       多个输入的拼接/处理节点就算顺着走也拿不到合并结果（那是运行期才算得
       出来的），返回一个上游片段反而更误导 —— 直接交给下面的兜底。 */
    if (!yanhuoLooksLikeProxy(source) || linked.length !== 1) {
        return __yanhuoOrigTextWidgetValueFromNode(source, depth, new Set(seen));
    }
    const next = new Set(seen);
    if (source.id != null) next.add(source.id);
    const link = inputLinkAtSlot(source, linked[0]);
    const upstream = link ? source.graph?.getNodeById?.(link.origin_id) : null;
    if (upstream) {
        const nested = textWidgetValueFromNode(upstream, depth + 1, next);
        if (nested !== null && String(nested).trim() !== "") return nested;
    }
    return __yanhuoOrigTextWidgetValueFromNode(source, depth, new Set(seen));
};
"""


def _assert_clean_js(text: str) -> None:
    """v1.8.1：build 期的最后一道闸门。

    背景：注入块是"写死的大段 JS 字符串"，如果在某个字符串里误用了 Python 表达式
    （如 `str(YANHUO_PREVIEW_H)`），它会原样落到生成文件里。**`node --check` 发现
    不了** —— `str(x)` 在语法上是合法的函数调用，只有真跑起来才抛 ReferenceError，
    而那时节点的整个 UI 注入已经挂了。这里在写盘前扫一遍残留。
    """
    leaks = []
    # 只挑无歧义的模式：注释里出现 "True" / "return None" 之类是上游英文行文，别误伤。
    patterns = {
        r"\bstr\(": "疑似 Python 的 str() 调用残留在 JS 里",
        r"__SKIN_CSS__|__BOTTOM_GUTTER__|__PREVIEW_(?:BASIS|MIN_W|H)__": "占位符未被替换",
        r"=\s*(?:None|True|False)\s*[,;)]": "疑似 Python 字面量赋值残留在 JS 里",
    }
    for pattern, reason in patterns.items():
        hits = sorted({m.group(0) for m in re.finditer(pattern, text)})
        if hits:
            leaks.append(f"  - {reason}: {', '.join(hits[:8])}")
    if leaks:
        raise SystemExit(
            "生成文件里发现 Python 残留，拒绝写盘：\n" + "\n".join(leaks)
        )


def build(source: Path = SOURCE, destination: Path = TARGET_FILE) -> Path:
    text = source.read_text(encoding="utf-8")

    target_pattern = re.compile(r'^(const TARGET = ")([^"]+)(";\s*)$', re.M)
    matches = target_pattern.findall(text)
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly one TARGET declaration in {source}, found {len(matches)}"
        )
    original_node = matches[0][1]
    text = target_pattern.sub(rf'\g<1>{NODE_NAME}\g<3>', text)

    extension_pattern = re.compile(r'(name:\s*)"MiniMaxH3\.Extender"')
    extension_matches = extension_pattern.findall(text)
    if len(extension_matches) != 1:
        raise SystemExit(
            f"expected exactly one extension name in {source}, found {len(extension_matches)}"
        )
    text = extension_pattern.sub(rf'\g<1>"{EXTENSION_NAME}"', text)

    # v1.2.0: 高度压缩 + 中文文案。放在 TARGET/扩展名替换之后，
    # 这样上游 Extender 更新时只需维护上面两张替换表。
    text = _compact_ui(text)
    text = _translate_ui(text)

    # The final-decode companion node keeps its own identity.
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    banner = "\n".join([
        "/* ------------------------------------------------------------------",
        f" * GENERATED FILE - do not edit by hand; run tools/build_frontend.py",
        f" * source: {source.name} (sha256:{digest})",
        f" * bound to the {NODE_NAME} node instead of {original_node}.",
        " * Only TARGET and the extension name differ from the source file.",
        " * ---------------------------------------------------------------- */",
    ]) + "\n"

    # 皮肤 CSS 以 JSON 字符串字面量注入，避免 CSS 里的引号/换行破坏 JS。
    import json

    tail = SIGMAS_UI_TAIL.replace("__SKIN_CSS__", json.dumps(SKIN_CSS, ensure_ascii=False))
    tail = tail.replace("__BOTTOM_GUTTER__", str(YANHUO_BOTTOM_GUTTER))
    tail = tail.replace("__PREVIEW_BASIS__", str(YANHUO_PREVIEW_BASIS))
    tail = tail.replace("__PREVIEW_MIN_W__", str(YANHUO_PREVIEW_MIN_W))
    tail = tail.replace("__PREVIEW_H__", str(YANHUO_PREVIEW_H))

    out = banner + text + tail + GLOBALS_UI_TAIL + PERCLIP_UI_TAIL
    _assert_clean_js(out)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(out, encoding="utf-8")
    print(f"wrote {destination} ({destination.stat().st_size} bytes)")
    return destination


if __name__ == "__main__":
    build()
