"""Yanhuo H3 Motion Context × SelfLift node.

A thin subclass of the sibling ``MiniMaxH3Extender`` node. All existing behaviour
(clips, internal references, Motion Context chaining, Disk Join, preview,
projects, the whole custom UI) comes from the parent; this package only adds

* SelfLift dual-resolution widgets,
* a scoped sampler swap so every clip is rendered by the SelfLift engine,
* automatic cache invalidation when the SelfLift plan changes.

Nothing inside ``ComfyUI_MiniMax_H3_Extender`` or ``comfyui-SelfLift`` is edited.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path

from . import config, engine, semantic_bridge, vendor

_LOG = logging.getLogger("yanhuo_h3_selflift")

NODE_NAME = "YanhuoH3MotionContextSelfLift"
NODE_DISPLAY_NAME = "Yanhuo H3 Motion Context SelfLift"


# ---------------------------------------------------------------------------
# v1.3.0 全局 LoRA / 全局种子
# 前端把设置放在 clips_json 顶层的 yanhuo_global 字段里（随工作流持久化，
# 逐片段的 loras/seed 原值不动）。这里在把 payload 交给父类 extend() 之前
# 按开关覆盖每个 clip 的 loras / seed：
#   * 全局 LoRA 开启 -> 所有 clip.loras = 全局列表（父类随后自行归一化）；
#   * 全局种子开启   -> 所有 clip.seed = 全局种子（seed_mode 的推进只发生在
#     前端显示层，实际执行用的就是这里的值）。
# 缓存失效在覆盖之后计算，所以开关/改值都会触发受影响片段重渲。
# ---------------------------------------------------------------------------
def _apply_yanhuo_globals(clips_json):
    if not isinstance(clips_json, str) or "yanhuo_global" not in clips_json:
        return clips_json
    try:
        payload = json.loads(clips_json)
    except (TypeError, ValueError):
        return clips_json
    if not isinstance(payload, dict):
        return clips_json
    global_cfg = payload.get("yanhuo_global")
    clips = payload.get("clips")
    if not isinstance(global_cfg, dict) or not isinstance(clips, list):
        return clips_json

    changed = False

    lora_cfg = global_cfg.get("global_lora")
    if isinstance(lora_cfg, dict) and lora_cfg.get("enabled"):
        raw_loras = lora_cfg.get("loras")
        if isinstance(raw_loras, list):
            loras = [
                {"name": str(entry.get("name") or "").strip(),
                 "strength": max(-100.0, min(100.0, float(entry.get("strength", 1.0))))}
                for entry in raw_loras
                if isinstance(entry, dict) and str(entry.get("name") or "").strip()
            ]
            for clip in clips:
                if isinstance(clip, dict):
                    clip["loras"] = [dict(entry) for entry in loras]
            changed = True

    seed_cfg = global_cfg.get("global_seed")
    if isinstance(seed_cfg, dict) and seed_cfg.get("enabled"):
        try:
            seed = int(seed_cfg.get("seed", 0))
        except (TypeError, ValueError):
            seed = None
        # 负数/非整数的全局种子视为无效，宁可整段不覆盖也不要悄悄归零。
        if seed is not None and seed >= 0:
            for clip in clips:
                if isinstance(clip, dict):
                    clip["seed"] = seed
            changed = True

    if not changed:
        return clips_json
    return json.dumps(payload, ensure_ascii=False)

# 父节点控件提示词的中文对照（v1.2.0 全量中文化）。
# 只覆盖文案，绝不改动键序/类型，保证旧工作流的控件按位置映射不受影响。
_INHERITED_TOOLTIPS_ZH = {
    "model": "MiniMax H3 Ref2VA 模型。仅在模式为 REF2VA 时加载求值。",
    "clip": "文本编码器（Qwen3-VL）：把提示词编码为 H3 的文本条件。",
    "vae": "视频 VAE：用于 SelfLift 的像素锚点提升与预览解码。",
    "run_mode": "clip_by_clip：逐段渲染，支持运动链因果续接；full_batch：整批一次性渲染（支持中断续跑）。",
    "width": "手动分辨率宽（32 的倍数）；Auto 模式无内部参考图时作为回退。",
    "height": "手动分辨率高（32 的倍数）；Auto 模式无内部参考图时作为回退。",
    "ref_image_size": "match：参考图按目标分辨率匹配；max：按参考图最长边约束。",
    "steps": "采样步数。连接 selflift_sigmas 后步数由该表决定（表条目数-1），此控件被旁路。",
    "sampler_name": "采样器。本节点要求 euler（SelfLift 复用过渡步预测）。",
    "scheduler": "sigma 调度器。连接 selflift_sigmas 后被旁路（denoise 裁剪在上游链完成）。",
    "denoise": "去噪强度。连接 selflift_sigmas 后被旁路。",
    "context_length": "H3 上下文长度档位（每段可用的 latent 帧预算）。",
    "audio_context_length": "音频上下文长度（0 = 不限制）。",
    "resolution_mode": "auto_from_ref：用内部 Ref 1 的宽高比自动定分辨率，无参考图时回退 width/height；manual：手动宽高。",
    "megapixels": "Auto 模式的目标总像素（H3 32 像素网格，向下取整不超预算）。",
    "motion_context": "仅 REF2VA：开启后片段用 Motion Context 因果续接；关闭则为互相独立的随机访问片段。",
    "fl2va_model": "可选 MiniMax H3 FL2VA 模型，仅在模式为 FL2VA 时加载求值。",
    "audio_vae": "音频 VAE：解码音轨。",
    "ref_audio": "ref_audio_1 的旧别名，仅为兼容旧工作流保留；新工作流请用 ref_audio_1..3。",
    "ref_audio_1": "可选的独立参考音频 1（最多支持 3 条独立参考音频）。",
    "ref_audio_2": "可选的独立参考音频 2。",
    "ref_audio_3": "可选的独立参考音频 3。",
    "ref_video_1": "可选参考视频 1（IMAGE 帧序列；H3 按 24fps 处理，源帧率不同请接 ref_video_fps_1）。提示词中用 <Video 1> 引用。",
    "ref_video_fps_1": "参考视频 1 的源帧率（来自 Get Video Components）；未连接时按 24fps 处理。",
    "ref_video_audio_1": "参考视频 1 的音轨。",
    "ref_video_2": "可选参考视频 2（IMAGE 帧序列；H3 按 24fps 处理，源帧率不同请接 ref_video_fps_2）。提示词中用 <Video 2> 引用。",
    "ref_video_fps_2": "参考视频 2 的源帧率（来自 Get Video Components）；未连接时按 24fps 处理。",
    "ref_video_audio_2": "参考视频 2 的音轨。",
    "ref_video_3": "可选参考视频 3（IMAGE 帧序列；H3 按 24fps 处理，源帧率不同请接 ref_video_fps_3）。提示词中用 <Video 3> 引用。",
    "ref_video_fps_3": "参考视频 3 的源帧率（来自 Get Video Components）；未连接时按 24fps 处理。",
    "ref_video_audio_3": "参考视频 3 的音轨。",
    "ref_pack_1": "CLIP 1 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_2": "CLIP 2 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_3": "CLIP 3 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_4": "CLIP 4 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_5": "CLIP 5 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_6": "CLIP 6 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_7": "CLIP 7 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_8": "CLIP 8 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_9": "CLIP 9 专属参考图包（可用「Yanhuo 参考图打包」节点生成）：图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。",
    "ref_pack_10": "CLIP 10 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_11": "CLIP 11 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_12": "CLIP 12 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_13": "CLIP 13 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_14": "CLIP 14 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_15": "CLIP 15 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_16": "CLIP 16 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_17": "CLIP 17 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_18": "CLIP 18 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_19": "CLIP 19 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_20": "CLIP 20 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_21": "CLIP 21 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_22": "CLIP 22 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_23": "CLIP 23 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_24": "CLIP 24 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_25": "CLIP 25 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_26": "CLIP 26 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_27": "CLIP 27 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_28": "CLIP 28 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_29": "CLIP 29 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_30": "CLIP 30 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_31": "CLIP 31 专属参考图包（同 ref_pack_1 说明）。",
    "ref_pack_32": "CLIP 32 专属参考图包（同 ref_pack_1 说明）。",
}

# 逐片段 prompt_N / duration_N / ref_audio_N_k 的提示词用模板生成（共 32 组）。
_PER_CLIP_TEMPLATE_TOOLTIPS = {
    "prompt": "CLIP {n} 的外部提示词覆盖：连接后替换卡片提示词（空串忽略，不清空卡片）。",
    "duration": "CLIP {n} 的外部时长覆盖（秒）：连接后替换卡片 Duration，卡片上显示 (EXT)。",
    "ref_audio": "CLIP {n} 专属参考音频 {k}：仅作用于该片段，不影响其他片段。",
}

# 本节点移除的全局 pack 端口（保留 per-CLIP 的 ref_pack_N / prompt_N / ref_audio_N_x）。
_REMOVED_GLOBAL_INPUTS = ("ref_pack", "prompt_pack")


try:  # Resolve the parent node class once, at import time.
    _BASE_EXTENDER = vendor.extender_module().MiniMaxH3Extender
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment problems only
    _BASE_EXTENDER = None
    _IMPORT_ERROR = exc
    _LOG.error(
        "[Yanhuo SelfLift] could not load the sibling Extender package (%s). %s",
        exc,
        vendor.MISSING_HINT,
    )


# ---------------------------------------------------------------------------
# Parameter plumbing
# ---------------------------------------------------------------------------
def split_settings(kwargs):
    """Peel the ``selflift_*`` widgets off the inherited payload.

    Only WIDGET_NAMES are peeled. ``selflift_sigmas`` (config.INPUT_NAMES) is an
    input *socket* carrying a tensor and must stay in the payload so
    :func:`extend_with_selflift` can read it: peeling it would hand the tensor to
    ``SelfLiftSettings.from_kwargs``, which only knows widget scalars, so the
    external schedule was dropped and the node silently sampled with its own
    ``steps`` widget (v1.3.2 regression).
    """
    rest = dict(kwargs or {})
    payload = {name: rest.pop(name) for name in config.WIDGET_NAMES if name in rest}
    return config.SelfLiftSettings.from_kwargs(payload), rest


def _log_reference_counts(payload):
    """Report how many reference images actually arrived for this run.

    A clip can lose an image in three places (the packing node, the slot merge
    against clip-local Pictures, or the panel only *drawing* part of the row),
    and they are indistinguishable from the canvas. Counting what reached the
    node separates "the input really is short" from "the panel did not draw it".
    """
    packs = []
    for name, pack in (payload or {}).items():
        if not isinstance(name, str) or not name.startswith("ref_pack"):
            continue
        if not isinstance(pack, dict):
            continue
        slots = [slot for slot in (pack.get("slots") or []) if slot is not None]
        if slots:
            packs.append(f"{name}={len(slots)}")
    if packs:
        _LOG.info(
            "[Yanhuo SelfLift] 本次收到的参考图包：%s（张/包）。"
            "若界面显示的张数少于这里，是面板绘制/滚动问题；若这里本身就少，"
            "说明上游图像列表少给了一张。",
            "、".join(packs),
        )
    clips_json = payload.get("clips_json")
    if not isinstance(clips_json, str) or not clips_json.strip():
        return
    try:
        clips = json.loads(clips_json)
    except (TypeError, ValueError):
        return
    if not isinstance(clips, list):
        return
    for index, cfg in enumerate(clips, start=1):
        if not isinstance(cfg, dict):
            continue
        local = cfg.get("local_refs") or {}
        images = (local or {}).get("images") if isinstance(local, dict) else None
        if images:
            _LOG.info(
                "[Yanhuo SelfLift] CLIP %d 自带参考图 %d 张（本地 Picture 槽位）。",
                index,
                len(images),
            )


def _describe_sigmas_link(kwargs):
    """Name the node the ``selflift_sigmas`` link comes from (diagnostics only).

    ComfyUI hands a connected socket ``None`` when the upstream node produced no
    output for it: the node was muted/bypassed, it sits in a branch that did not
    run, or the link never reached the submitted prompt at all. ``prompt`` and
    ``unique_id`` are hidden inputs the parent already declares, so the message
    can point at the exact culprit instead of guessing.
    """
    prompt = kwargs.get("prompt")
    unique_id = kwargs.get("unique_id")
    if not isinstance(prompt, dict) or unique_id is None:
        return ""
    own = prompt.get(str(unique_id)) or prompt.get(unique_id)
    if not isinstance(own, dict):
        return ""
    link = (own.get("inputs") or {}).get("selflift_sigmas")
    if not (isinstance(link, (list, tuple)) and len(link) >= 1):
        return (
            "本次提交的提示里 selflift_sigmas 没有连线——界面上的连线没有进到这次提交的图"
            "（常见于 Get/Set 虚拟节点代理，或连线端点实际粘在别的端口上）。"
        )
    source_id = str(link[0])
    source = prompt.get(source_id)
    if source is None:
        return (
            f"本次提交的提示里 selflift_sigmas 指向节点 {source_id}，但这个节点不在提示中"
            "——它多半被静音/旁路、或位于本次没有执行的分支。"
        )
    return (
        f"本次提交的提示里 selflift_sigmas 指向节点 {source_id}"
        f"（class_type={source.get('class_type')}，mode={source.get('mode')}），"
        "但它这次没有产出任何输出。"
    )


def _widget_spec():
    models = vendor.list_upscaler_models()
    bridge_models = semantic_bridge.list_adapters()
    return {
        "selflift_enabled": (
            "BOOLEAN",
            {
                "default": True,
                "tooltip": (
                    "开启：每段 CLIP 走 SelfLift 双分辨率流程（低分辨率定轮廓 → 提升到高分辨率精修）。"
                    "关闭：完全等同于原 Extender 的单阶段采样。"
                ),
            },
        ),
        "selflift_transition_step": (
            "INT",
            {
                "default": 2,
                "min": 1,
                "max": 10000,
                "step": 1,
                "tooltip": (
                    "低分辨率阶段步数。必须小于 steps（合法范围 1..steps-1）。"
                    "steps=4 时建议 2；steps=8~16 时可提到 4~6，低分辨率吃得越多越省。"
                ),
            },
        ),
        "selflift_lowres_scale": (
            "FLOAT",
            {
                "default": 0.5,
                "min": 0.25,
                "max": 1.0,
                "step": 0.05,
                "tooltip": "低分辨率阶段的空间缩放（论文用 0.5）。越小越省算力，结构越粗。",
            },
        ),
        "selflift_rho": (
            "FLOAT",
            {
                "default": 0.6,
                "min": 0.0,
                "max": 1.0,
                "step": 0.05,
                "tooltip": (
                    "一致性修正覆盖的最高风险位置比例。"
                    "无外部 latent upscaler 时用 SelfLift-zero，建议 0.6；"
                    "配合外部 H3 latent upscaler（rho=0）时走 learned 直提。"
                ),
            },
        ),
        "selflift_w_min": (
            "FLOAT",
            {
                "default": 1.0,
                "min": 0.0,
                "max": 1.0,
                "step": 0.05,
                "tooltip": "修正强度下限。H3 的最近邻提升误差较大，作者建议取 1.0。",
            },
        ),
        "selflift_w_max": (
            "FLOAT",
            {
                "default": 1.0,
                "min": 0.0,
                "max": 1.0,
                "step": 0.05,
                "tooltip": "修正强度上限，保持 1.0。",
            },
        ),
        "selflift_latent_upsample": (
            list(config.LATENT_UPSAMPLE_MODES),
            {
                "default": "nearest",
                "tooltip": "直接 latent 提升的插值方式（论文用 nearest）。选择外部 upscaler 时该项被替换。",
            },
        ),
        "selflift_upscaler_model": (
            models,
            {
                "default": models[0] if models else "none",
                "tooltip": (
                    "外部 H3 latent upscaler（models/latent_upscale_models）。"
                    "选中后低分辨率结果由学习型 3D 提升到目标分辨率，此时请把 rho 设为 0。"
                ),
            },
        ),
        "selflift_upscaler_unload": (
            "BOOLEAN",
            {
                "default": True,
                "tooltip": "提升完成后立刻把外部 upscaler 卸出显存，给高分辨率阶段腾地方。",
            },
        ),
        "selflift_highres_tiling": (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": (
                    "高分辨率阶段空间分块（显存不足时开启；1~8 块自动选择）。"
                    "音频流与参考集保持完整，只保留第一个块的音频预测。"
                ),
            },
        ),
        # External input socket, not a widget: an externally built sigma
        # schedule replaces the scheduler/steps/denoise widgets entirely.
        "selflift_sigmas": (
            "SIGMAS",
            {
                "tooltip": (
                    "外接 SIGMAS（可选）：连接后整条 sigma 调度表由外部接管"
                    "（如 基本调度器 → 插值扩展Sigmas → Sigma Refiner），"
                    "scheduler/steps/denoise 控件被旁路。要求：一维、单调不递增、"
                    "只有末位为 0；transition_step 变为该表中的切割索引。"
                ),
            },
        ),
        # v1.6.0 内置语义桥：模型文件放在 ComfyUI/models/semantic_bridge
        "selflift_bridge_enabled": (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": (
                    "开启内置语义桥（Semantic Bridge）：在每个 CLIP N 的文本条件编码之后、"
                    "采样之前，用一个极小的蒸馏 MLP 把 H3 的语义表示往老师模型拉近一点，"
                    "帮助复杂动作里保持「谁在做什么/道具归谁/攻受关系」。"
                    "默认关闭；开启后改模型或强度会自动重渲受影响的片段。"
                ),
            },
        ),
        "selflift_bridge_adapter": (
            bridge_models,
            {
                "default": bridge_models[0] if bridge_models else "",
                "tooltip": (
                    "语义桥模型文件（放在 ComfyUI/models/semantic_bridge）。"
                    "它不是 LoRA，也不改写提示词；不同版本强度/风格不同，"
                    "建议固定种子对比后再定。"
                ),
            },
        ),
        "selflift_bridge_alpha": (
            "FLOAT",
            {
                "default": 0.10,
                "min": 0.0,
                "max": 1.0,
                "step": 0.01,
                "tooltip": (
                    "语义桥混合强度：0=完全不用，1=完全替换。模型元数据与作者建议从 "
                    "0.10~0.15 起试，强度不是越高越好（过高容易出新的退化）。"
                ),
            },
        ),
        "selflift_bridge_magnitude": (
            ["per_token", "global", "none"],
            {
                "default": "per_token",
                "tooltip": (
                    "投影后的幅值对齐方式：per_token（逐 token 对齐，官方推荐）/"
                    "global（整张张量一个比例）/none（不对齐，变化最猛）。"
                ),
            },
        ),
    }


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------
def _clip_ids(base_module, clips_json, generation_mode, motion_context):
    clips = base_module._parse_clips_json(clips_json, generation_mode, motion_context)
    return [str(cfg.get("id") or f"clip_{i + 1}") for i, cfg in enumerate(clips)]


def _resolve_own_chain(base_module, owner, generation_mode, motion_context, clips_json):
    """Reuse the parent's own path helpers to find the active cache chain."""
    mode = base_module._normalize_generation_mode(generation_mode)
    if mode == "fl2va":
        return base_module.sync_fl2va_manifest(
            owner, base_module.FPS, _clip_ids(base_module, clips_json, mode, motion_context)
        )
    if not motion_context:
        try:
            import importlib

            package = vendor.extender_package()
            independent = importlib.import_module(f"{package.__name__}.ref2va_independent")
        except Exception:
            return None
        return independent.sync_manifest(
            owner,
            base_module.FPS,
            _clip_ids(base_module, clips_json, mode, False),
        )
    return base_module._manifest_for_extender(owner, base_module.FPS)


def _validate_active_model(settings, payload, log=None, sigmas=None):
    """Validate against *the model that will actually sample*, before cost."""
    logger = log or _LOG
    base_module = vendor.extender_module()
    mode = base_module._normalize_generation_mode(payload.get("generation_mode", "ref2va"))
    # FL2VA samples with the dedicated fl2va_model; validating `model` there would
    # only force the Ref2VA checkpoint into memory for nothing.
    active = payload.get("fl2va_model") if mode == "fl2va" else payload.get("model")
    if active is None:
        logger.warning(
            "[Yanhuo SelfLift] no active model in this run; skipping plan validation."
        )
        return
    engine.validate_plan(
        active,
        payload.get("sampler_name"),
        payload.get("scheduler"),
        payload.get("steps"),
        payload.get("denoise"),
        settings,
        sigmas=sigmas,
    )


def _effective_steps(payload, sigmas):
    """How many steps this run will actually sample.

    With ``selflift_sigmas`` connected the sigma table decides; otherwise the
    ``steps`` widget does. Used to resolve the transition cut index *before* the
    cache signature is computed.
    """
    if sigmas is not None:
        try:
            count = int(sigmas.numel()) - 1
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            count = 0
        if count >= 1:
            return count
    try:
        return int(payload.get("steps", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover
        return 0


def _step_source(payload, sigmas, steps):
    if sigmas is not None:
        try:
            entries = int(sigmas.numel())
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            entries = 0
        if entries >= 2:
            return f"the external `selflift_sigmas` table ({entries} entries -> {steps} steps)"
    try:
        denoise = float(payload.get("denoise", 1.0))
    except (TypeError, ValueError):  # pragma: no cover
        denoise = 1.0
    return (
        f"`steps`={steps} with scheduler={payload.get('scheduler')}, denoise={denoise}"
    )


def invalidate_on_plan_change(settings, owner, generation_mode, motion_context, clips_json, log=None):
    """Drop cached segments when the SelfLift plan no longer matches.

    A change inside the sampler is invisible to the Extender's own cache logic,
    so without this a chain would happily re-use single-stage segments next to
    dual-stage ones. The signature lives inside the parent's manifest; the parent
    preserves unknown keys whenever it rewrites it.
    """
    logger = log or _LOG
    if not settings.enabled:
        return False
    try:
        base_module = vendor.extender_module()
    except Exception as exc:
        logger.warning("[Yanhuo SelfLift] Extender unavailable; cache check skipped (%s).", exc)
        return False

    try:
        resolved = _resolve_own_chain(
            base_module, owner, generation_mode, motion_context, clips_json
        )
    except Exception:
        logger.warning(
            "[Yanhuo SelfLift] could not resolve the active cache chain; "
            "skipping automatic invalidation. Reset the node manually if you changed "
            "the SelfLift plan.",
            exc_info=True,
        )
        return False
    if not resolved:
        return False

    data_path, manifest_path, manifest = resolved
    try:
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            return False
        signature = settings.signature()
        manifest = dict(manifest or {})
        if manifest.get(config.UPGRADER_KEY) == signature:
            return False
        segments = list(manifest.get("segments") or [])
        if segments:
            logger.info(
                "[Yanhuo SelfLift] SelfLift plan changed; resetting %d cached clip(s) "
                "so the whole chain re-renders with the new plan.",
                len(segments),
            )
            manifest = dict(
                base_module._truncate_chain(data_path, manifest_path, manifest, 0) or {}
            )
        manifest[config.UPGRADER_KEY] = signature
        manifest["updated_at"] = time.time()
        base_module._write_json_atomic(manifest_path, manifest)
        return bool(segments)
    except Exception:
        logger.warning(
            "[Yanhuo SelfLift] could not update the SelfLift cache signature; "
            "segments from a different plan may be re-used.",
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------
def _localize_inherited_inputs(spec):
    """把父节点 INPUT_TYPES 的英文提示词替换为中文（只改 tooltip，不动键序/类型）。"""
    import re

    merged = dict(spec)
    for section in ("required", "optional"):
        source = spec.get(section) or {}
        if not source:
            continue
        localized = dict(source)
        for name, entry in source.items():
            if not isinstance(entry, tuple) or len(entry) != 2:
                continue
            kind, options = entry
            if not isinstance(options, dict):
                continue
            chinese = _INHERITED_TOOLTIPS_ZH.get(name)
            if chinese is None:
                clip_match = re.fullmatch(r"prompt_(\d+)", name)
                duration_match = re.fullmatch(r"duration_(\d+)", name)
                audio_match = re.fullmatch(r"ref_audio_(\d+)_(\d+)", name)
                if clip_match:
                    chinese = _PER_CLIP_TEMPLATE_TOOLTIPS["prompt"].format(n=clip_match.group(1))
                elif duration_match:
                    chinese = _PER_CLIP_TEMPLATE_TOOLTIPS["duration"].format(n=duration_match.group(1))
                elif audio_match:
                    chinese = _PER_CLIP_TEMPLATE_TOOLTIPS["ref_audio"].format(
                        n=audio_match.group(1), k=audio_match.group(2)
                    )
            if chinese:
                localized[name] = (kind, {**options, "tooltip": chinese})
        merged[section] = localized
    return merged


if _BASE_EXTENDER is not None:

    class YanhuoH3MotionContextSelfLift(_BASE_EXTENDER):
        DESCRIPTION = (
            "Yanhuo H3 运动链 × SelfLift 双分辨率采样：每段 CLIP 先低分辨率起草（定空间动作轮廓），"
            "经一致性提升（SelfLift-zero 或学习型 latent upscaler）后再高分辨率精修细节；"
            "完整保留 Motion Context 跨段续接、参考图/视频/音频、磁盘缓存与项目管理。"
            "连接 selflift_sigmas 可用外部 sigma 调度表（基本调度器→插值→Refiner）接管整条采样调度。"
        )
        CATEGORY = "MiniMax H3/SelfLift"
        FUNCTION = "extend_with_selflift"
        RETURN_NAMES = ("缓存", "片段数", "已校验数", "状态", "缓存大小 MB", "构建")

        @classmethod
        def INPUT_TYPES(cls):
            spec = super().INPUT_TYPES()
            merged = _localize_inherited_inputs(spec)
            required = dict(merged.get("required") or {})
            optional = dict(merged.get("optional") or {})
            # v1.2.0：移除全局 ref_pack / prompt_pack 端口。逐片段的
            # ref_pack_N / prompt_N / duration_N / ref_audio_N_x 全部保留——
            # 两者都是父节点的 optional 输入，移除后 extend() 收到 None，
            # 父逻辑按"未连接"处理，行为安全。
            for name in _REMOVED_GLOBAL_INPUTS:
                optional.pop(name, None)
            merged["required"] = required
            merged["optional"] = optional
            # Appended last on purpose: the parent documents that legacy workflows
            # map their widgets positionally, so new inputs must never be inserted
            # ahead of an existing one.
            required.update(_widget_spec())
            return merged

        def extend_with_selflift(self, **kwargs):
            socket_present = "selflift_sigmas" in (kwargs or {})
            settings, payload = split_settings(kwargs)
            raw_sigmas = payload.pop("selflift_sigmas", None)
            sigmas = raw_sigmas
            if sigmas is not None:
                # The digest rides inside the settings, so rewiring the sigma
                # chain invalidates the cache even when widgets look unchanged.
                settings = dataclasses.replace(settings, sigmas_digest=config.sigmas_digest(sigmas))
                if not settings.uses_external_sigmas():
                    sigmas = None  # empty/garbage input: fall back to the widgets
            # v1.3.0：全局 LoRA / 全局种子在进入父类（含缓存失效计算）之前生效。
            payload["clips_json"] = _apply_yanhuo_globals(payload.get("clips_json"))
            # v1.3.1：先按本次实际的步数把切分点定下来。必须在这里完成，因为
            # 缓存签名用的是 settings.transition_step —— 若只在校验里临时钳制，
            # 之后把 steps 提高到让原值合法时，有效方案变了而签名不变，就会吃
            # 到旧缓存。这里统一一次，下面每一步都用同一个值。
            steps = _effective_steps(payload, sigmas)
            if steps:
                settings = settings.validate(
                    steps, source=_step_source(payload, sigmas, steps), log=_LOG
                )
            # v1.3.1：每次运行都打印一次"这次到底用的哪套调度 + 端口到底送来了什么"。
            # 分四种情况，专治"前端有连线、后端却走控件"的排查：
            #   A 端口在且有表      -> 外接生效（最高优先）
            #   B 端口在但表为空    -> 上游产出空表，回落控件
            #   C 端口在但值为 None -> 前端连线存在、执行时却没拿到值（Get/Set 代理、
            #                          上游未产出等），回落控件
            #   D 端口不在执行输入里 -> 这次执行的图里就没有这条线（旧工作流/未连线）
            if settings.enabled:
                if sigmas is not None:
                    try:
                        entries = int(sigmas.numel())
                    except (AttributeError, TypeError, ValueError):  # pragma: no cover
                        entries = 0
                    _LOG.info(
                        "[Yanhuo SelfLift] 采样调度来源：外接 selflift_sigmas（%d 条 -> %d 步，"
                        "切分 %d 低分辨率 + %d 高分辨率）；节点上的 steps/scheduler/denoise "
                        "控件本次已被旁路。",
                        entries,
                        steps,
                        settings.transition_step,
                        max(0, int(steps) - int(settings.transition_step)),
                    )
                elif raw_sigmas is not None:
                    _LOG.warning(
                        "[Yanhuo SelfLift] 采样调度来源：节点控件 steps=%s scheduler=%s "
                        "denoise=%s。selflift_sigmas 端口有连线、但上游本次产出的是空表/"
                        "无效表，已回落控件调度——检查 Refiner 链的输出。",
                        payload.get("steps"), payload.get("scheduler"), payload.get("denoise"),
                    )
                elif socket_present:
                    _LOG.warning(
                        "[Yanhuo SelfLift] 采样调度来源：节点控件 steps=%s scheduler=%s "
                        "denoise=%s。selflift_sigmas 端口在本次执行的输入里存在、但送到的值是 "
                        "None——上游这次没有产出输出。%s",
                        payload.get("steps"), payload.get("scheduler"), payload.get("denoise"),
                        _describe_sigmas_link(payload),
                    )
                else:
                    _LOG.warning(
                        "[Yanhuo SelfLift] 采样调度来源：节点控件 steps=%s scheduler=%s "
                        "denoise=%s。本次执行的输入里没有 selflift_sigmas 端口——节点显示的连线"
                        "没有进入后端执行图。%s",
                        payload.get("steps"), payload.get("scheduler"), payload.get("denoise"),
                        _describe_sigmas_link(payload),
                    )
            _log_reference_counts(payload)
            if settings.enabled:
                owner = str(
                    payload.get("unique_id")
                    if payload.get("unique_id") is not None
                    else "h3_extender"
                )
                invalidate_on_plan_change(
                    settings,
                    owner,
                    payload.get("generation_mode", "ref2va"),
                    bool(payload.get("motion_context", True)),
                    payload.get("clips_json"),
                    log=_LOG,
                )
                _validate_active_model(settings, payload, log=_LOG, sigmas=sigmas)
            with engine.installed_sampler(
                settings,
                payload.get("vae"),
                sigmas=sigmas,
                node_id=payload.get("unique_id"),
            ):
                # v1.6.0：语义桥包装上游逐 CLIP 的 conditioning 构建函数，
                # 只在本节点本次执行期间生效。
                with semantic_bridge.installed_bridge(settings):
                    return super().extend(**payload)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

if _BASE_EXTENDER is not None:
    NODE_CLASS_MAPPINGS[NODE_NAME] = YanhuoH3MotionContextSelfLift
    NODE_DISPLAY_NAME_MAPPINGS[NODE_NAME] = NODE_DISPLAY_NAME


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "NODE_NAME",
    "invalidate_on_plan_change",
    "split_settings",
]
