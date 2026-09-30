"""配套节点：参考图打包 / 成片导出 / 视频文件加载。

与主节点一样，这里只做"绑定与中文化"，不重复实现任何重逻辑：

* 参考图打包      —— 把一张 IMAGE 列表（batch）按帧顺序折进一个 H3_REF_PACK，
                    直接喂给主节点的 ref_pack_N 端口（对应 Extender 的
                    Reference Pack Bridge 的单输入简化版）。
* 成片导出        —— 继承姊妹包的 Final Decode（磁盘拼接/编码/预览全部复用），
                    只把说明与控件提示翻译为中文。
* 视频文件加载    —— 把链路已写出的成片/分段 mp4 重新作为 VIDEO 提供给下游
                    （放大、插帧、转码等），复用姊妹包的路径→VIDEO 封装。

两个姊妹包保持只读；无法加载父包时本模块整体不注册。
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import torch

from . import vendor

_LOG = logging.getLogger("yanhuo_h3_selflift")

REF_PACK_TYPE = "H3_REF_PACK"
REF_PACK_VERSION = 2
MAX_PACK_SLOTS = 9  # 与 Extender 每卡 MAX_IMAGE_REFS 一致


# ---------------------------------------------------------------------------
# 1) 参考图打包：IMAGE 列表 -> H3_REF_PACK
# ---------------------------------------------------------------------------
def _split_image_list(image):
    """把 [B,H,W,C]（或 [H,W,C]）逐帧拆成单个 [1,H,W,C] 张量。"""
    if not torch.is_tensor(image):
        return []
    tensor = image.detach()
    if tensor.ndim == 3:
        return [tensor.unsqueeze(0)]
    if tensor.ndim != 4:
        return []
    return [tensor[index].unsqueeze(0) for index in range(int(tensor.shape[0]))]


class YanhuoH3RefPackFromImages:
    """把图像列表打包成一个 ref_pack，供主节点的 ref_pack_N 使用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "optional": {
                "images": (
                    "IMAGE",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "参考图列表（batch）：每一帧按顺序成为一个参考槽位"
                            "（最多 9 个，与每卡 Picture 1..9 上限一致），"
                            "直接连接到主节点的 ref_pack_N 端口。"
                        ),
                    },
                ),
            },
        }

    RETURN_TYPES = (REF_PACK_TYPE, "INT")
    RETURN_NAMES = ("ref_pack", "参考数")
    FUNCTION = "pack"
    CATEGORY = "MiniMax H3/SelfLift"
    OUTPUT_NODE = False
    DESCRIPTION = (
        "把一批参考图像（IMAGE 列表）按帧顺序打包成 ref_pack，"
        "接 Yanhuo H3 Motion Context SelfLift 的 ref_pack_N 端口，"
        "即成为 CLIP N 专属的 Picture 1..K 参考集，不影响其他片段与全局参考。"
    )

    def pack(self, images=None):
        slots = []
        for frame in _split_image_list(images):
            if len(slots) >= MAX_PACK_SLOTS:
                _LOG.warning(
                    "[Yanhuo SelfLift] 参考图打包：图像超过 %d 张，多余部分被丢弃"
                    "（每卡参考上限 9 张）。",
                    MAX_PACK_SLOTS,
                )
                break
            slots.append(frame.contiguous())
        pack = {
            "type": REF_PACK_TYPE,
            "version": REF_PACK_VERSION,
            "source": "Yanhuo H3 参考图打包",
            "count": int(len(slots)),
            "slots": slots,
        }
        return (pack, int(len(slots)))


# ---------------------------------------------------------------------------
# 2) 成片导出：复用姊妹包的 Final Decode，仅做中文化
# ---------------------------------------------------------------------------
_FINAL_DECODE_TOOLTIPS = {
    "cache": "主节点输出的链路缓存（cache 端口），包含全部已渲染片段的磁盘数据。",
    "vae": "视频 VAE：用于把缓存的 latent 解码成最终画面。",
    "audio_vae": "音频 VAE：用于解码音轨。",
    "fps": "仅作兼容保留，实际帧率由缓存决定（前端会隐藏此控件）。",
    "filename_prefix": "输出文件名前缀（可包含子目录，如 yanhuo/chain）。",
    "output_directory": "输出目录；留空使用 ComfyUI 的 output 目录。",
    "codec": "编码器：H.264 自动优先用 NVENC 硬编并回退 libx264；也可强制 CPU 编码或无损 FFV1。",
    "crf": "画质档位（0 最高质量，51 最小体积）。",
    "preset": "编码速度/压缩率预设：越慢压缩越好。",
    "audio_bitrate": "音轨码率。",
    "autoplay": "生成结束或载入工作流时自动播放预览。",
    "auto_save_project": "整批模式完成后，在成片旁自动保存可迁移的 .ext 项目（逐段模式忽略；中断的批次不保存）。",
    "save_individual_clips": "仅整批模式：除拼接成片外，把每个最终片段也按最终处理导出（含音频）。关闭则保持原导出路径。",
}


def _final_decode_class():
    package = vendor.extender_package()
    module = importlib.import_module(f"{package.__name__}.motion_context_disk")
    return module.MiniMaxH3MotionContextDiskFinalDecode, module


# 上游在 clip_by_clip 的渐进预览路径里抛的裸 RuntimeError。它只说"期望一个未校验的
# 尾部候选"，却不说现在有几段、哪几段没勾"已校验"，排查全靠猜。这里只做只读诊断、
# 把话说清楚，不改动任何流程语义。
_PREVIEW_TAIL_ERROR = "progressive preview expects one unvalidated tail candidate"


def _describe_chain_state(cache):
    """只读：返回 (段数, 已校验前缀长度, 逐段是否已校验)；读不到就返回 None。"""
    try:
        if not isinstance(cache, dict):
            return None
        manifest_path = Path(cache["manifest_path"])
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # 只读诊断，任何异常都不该影响主流程
        return None
    segments = [dict(x or {}) for x in (manifest.get("segments") or [])]
    prefix = 0
    for desc in segments:
        if not bool(desc.get("validated", False)):
            break
        prefix += 1
    return len(segments), prefix, [bool(d.get("validated", False)) for d in segments]


def _explain_tail_error(cache, original):
    info = _describe_chain_state(cache)
    lines = [
        "成片导出（Final Decode）失败：clip_by_clip 的渐进预览要求「已校验」是一个连续前缀，",
        "并且只允许最后一个已缓存片段处于未校验状态（它就是本次要接上去的新片段）。",
    ]
    if info:
        total, prefix, flags = info
        detail = "、".join(
            f"CLIP{i + 1}={'已校验' if ok else '未校验'}" for i, ok in enumerate(flags)
        )
        lines.append(f"当前缓存：共 {total} 段，已校验前缀只有 {prefix} 段（{detail}）。")
        missing = [i + 1 for i, ok in enumerate(flags[: max(total - 1, 0)]) if not ok]
        if missing:
            lines.append(
                "修复：按顺序勾选 "
                + "、".join(f"CLIP{n}" for n in missing)
                + " 的「已校验」，再 Queue 一次即可出片；勾好后未校验的应只剩 "
                + f"CLIP{total}。"
            )
    lines.extend(
        [
            "常见成因：① 重跑第 N 段会作废 N 之后所有片段的缓存（Motion Context 失效），"
            "缓存段数会从多变少；",
            "② 取消中间某段的「已校验」会连带作废其后所有片段，「已校验」就不再连续；",
            "③ 片段渲染期间在面板上改动了「已校验」。",
            "可用 tools/diagnose_chain.py 随时查看当前链路状态（只读）。",
            f"原始报错：{original}",
        ]
    )
    return "\n".join(lines)


class YanhuoH3FinalDecodeOutput:
    """成片导出（Final Decode 中文桥）：cache + VAE -> VIDEO。"""

    _BASE = None
    _MODULE = None

    @classmethod
    def INPUT_TYPES(cls):
        spec = cls._BASE.INPUT_TYPES()
        translated = {}
        for section in ("required", "optional"):
            source = spec.get(section) or {}
            if not source:
                continue
            merged = dict(source)
            for name, entry in source.items():
                if not isinstance(entry, tuple) or len(entry) != 2:
                    continue
                kind, options = entry
                if not isinstance(options, dict):
                    continue
                chinese = _FINAL_DECODE_TOOLTIPS.get(name)
                if chinese:
                    merged[name] = (kind, {**options, "tooltip": chinese})
            translated[section] = merged
        return translated

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("成片视频",)
    FUNCTION = "export"
    CATEGORY = "MiniMax H3/SelfLift"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "成片导出：接收主节点输出的链路缓存（cache）与 VAE，"
        "在磁盘上把全部已渲染片段拼接/编码成最终视频并给出预览，"
        "输出 VIDEO 供下游（放大、插帧、转码、SaveVideo 等）继续使用。"
        "全部拼接与编码逻辑来自 Extender 的 Final Decode，此处仅做中文化与归类。"
    )

    def __init__(self):
        self._impl = self._BASE()

    def export(self, **kwargs):
        try:
            return self._impl.export(**kwargs)
        except RuntimeError as exc:
            # 只在认得这一条时改写文案；其余异常原样抛出，绝不吞错。
            if _PREVIEW_TAIL_ERROR in str(exc):
                raise RuntimeError(_explain_tail_error(kwargs.get("cache"), str(exc))) from exc
            raise

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        checker = getattr(cls._BASE, "IS_CHANGED", None)
        return checker(**kwargs) if callable(checker) else None

    @classmethod
    def check_lazy_status(cls, **kwargs):
        checker = getattr(cls._BASE, "check_lazy_status", None)
        return checker(**kwargs) if callable(checker) else None


# ---------------------------------------------------------------------------
# 3) 视频文件加载：把已写出的成片/分段重新作为 VIDEO 提供给下游
# ---------------------------------------------------------------------------
class YanhuoH3VideoFileLoader:
    """把链路已导出的视频文件重新装载为 VIDEO。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "视频文件路径：支持绝对路径，或相对 ComfyUI output 目录的路径"
                            "（例如 yanhuo/chain_00001_.mp4）。通常填成片导出写出的 mp4/mkv。"
                        ),
                    },
                ),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("视频", "文件路径")
    FUNCTION = "load"
    CATEGORY = "MiniMax H3/SelfLift"
    OUTPUT_NODE = False
    DESCRIPTION = (
        "视频文件加载：把 Yanhuo H3 Motion Context SelfLift 链路导出的成片或分段"
        "（mp4/mkv）重新作为 VIDEO 提供给下游节点（视频放大、插帧、转码、再剪辑等），"
        "并输出解析后的绝对路径。只读封装，不做二次解码拷贝。"
    )

    def load(self, video_path):
        resolved = _resolve_video_path(str(video_path or "").strip())
        if resolved is None:
            raise ValueError(
                "Yanhuo 视频文件加载：找不到视频文件。"
                "请填入成片导出写出的 mp4/mkv 路径（绝对路径，或相对 output 目录的路径）。"
            )
        module = self._video_module()
        return (module._video_output_from_path(resolved), resolved)

    @staticmethod
    def _video_module():
        return _final_decode_class()[1]

    @classmethod
    def validate_inputs(cls, video_path=""):
        path = str(video_path or "").strip()
        if not path:
            return "请填写视频文件路径（video_path）。"
        if _resolve_video_path(path) is None:
            return f"找不到视频文件：{path}"
        return True


def _resolve_video_path(video_path):
    if not video_path:
        return None
    try:
        import folder_paths

        candidates = [Path(video_path)]
        output_dir = Path(folder_paths.get_output_directory())
        if not video_path.lower().endswith((".mp4", ".mkv", ".webm")):
            candidates += [
                output_dir / f"{video_path}.mp4",
                output_dir / f"{video_path}.mkv",
            ]
        candidates.append(output_dir / video_path)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
    except Exception:
        _LOG.warning("[Yanhuo SelfLift] 视频路径解析失败：%s", video_path, exc_info=True)
    return None


def companion_mappings():
    """Return (NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS) or ({}, {}) on failure."""
    mappings = {
        "YanhuoH3RefPackFromImages": YanhuoH3RefPackFromImages,
        "YanhuoH3VideoFileLoader": YanhuoH3VideoFileLoader,
    }
    displays = {
        "YanhuoH3RefPackFromImages": "Yanhuo 参考图打包 (图像列表→ref_pack)",
        "YanhuoH3VideoFileLoader": "Yanhuo 视频文件加载 (成片→VIDEO)",
    }
    try:
        base, _module = _final_decode_class()
        YanhuoH3FinalDecodeOutput._BASE = base
        YanhuoH3FinalDecodeOutput._MODULE = _module
        mappings["YanhuoH3FinalDecodeOutput"] = YanhuoH3FinalDecodeOutput
        displays["YanhuoH3FinalDecodeOutput"] = "Yanhuo 成片导出 (Final Decode)"
    except Exception as exc:  # pragma: no cover - 姊妹包缺失时降级
        _LOG.warning(
            "[Yanhuo SelfLift] Final Decode 桥未注册（%s）。成片导出请继续使用 "
            "Extender 自带的 Final Decode 节点。",
            exc,
        )
    return mappings, displays


__all__ = [
    "YanhuoH3FinalDecodeOutput",
    "YanhuoH3RefPackFromImages",
    "YanhuoH3VideoFileLoader",
    "companion_mappings",
]
