"""连跑全部（full_batch）逐段出片 —— v1.5.0。

需求（烟火）：开启「连跑全部」后，每采样完一个 CLIP N，就把当前链路（第 1..N 段）
立即拼接输出为一个 mp4 文件，然后继续后面的片段；输出与采样串行进行、互不打断，
直到最后一段输出完为止。

实现完全复用上游的磁盘管线，不重新发明任何一步：

* 上游 full_batch 每段采样后本来就会做逐段 VAE 解码缓存
  （``cache_full_batch_ref2va_segment``：中性 H264 检查点 + 最终规格 sidecar + PCM 音轨），
  这里在其返回后调用上游成片导出用的同一个拼接函数
  ``_export_final_from_exact_segment_caches``（视频 ``-c:v copy`` 流复制 + PCM 音轨 mux），
  把第 1..N 段拼成一个 mp4（FFV1 无损时是 mkv）写到 ``output/yanhuo_selflift/``。
* 额外成本 = 每段一次纯流复制 mux（秒级），没有任何二次 VAE 解码。
* 导出规格（codec/crf/preset）沿用本次运行固定的 full_batch 导出规格——它来自
  工作流里成片导出（Final Decode）节点的控件，逐段文件与最终成片规格天然一致。
* 仅在 REF2VA 运动链模式生效；FL2VA / 独立片段模式自动跳过（记一条日志）。
* 任何异常只记日志，绝不打断采样（与草稿预览同一原则）。

上游包保持只读：这里只包装 ``extender.cache_full_batch_ref2va_segment`` 这个名字绑定
（full_batch 分支专用调用点），不改动上游任何文件与流程语义。
"""

from __future__ import annotations

import functools
import importlib
import logging
import re
import uuid
from pathlib import Path

from . import vendor

_LOG = logging.getLogger("yanhuo_h3_selflift")

_INSTALLED = False
_OUTPUT_SUBDIR = "yanhuo_selflift"
# 上游成片导出里 sequence_mode 的取值：缺省/空 = REF2VA 运动链（唯一支持的模式）。
_SUPPORTED_SEQUENCE_MODES = ("", "ref2va")
# v1.7.0 逐段成片实时预览：每导出完一段就推给前端 <video> 播放。
# 前端用 ComfyUI 内置 /view 端点（type=output + subfolder）取流，无需自定义路由。
FINAL_CLIP_EVENT = "yanhuo_h3_final_clip"


def notify_final_clip(node_id, prefix, total, output_path, fps=None) -> bool:
    """把一段成片的可播放信息推给前端；任何异常都只记 debug，绝不影响采样。"""
    try:
        from server import PromptServer

        instance = getattr(PromptServer, "instance", None)
        if instance is None:
            return False
        instance.send_sync(
            FINAL_CLIP_EVENT,
            {
                "node_id": str(node_id),
                "clip": int(prefix),
                "total": int(total),
                "filename": Path(output_path).name,
                "subfolder": _OUTPUT_SUBDIR,
                "type": "output",
                "path": str(output_path),
                "fps": float(fps) if fps else None,
            },
            None,
        )
        return True
    except Exception as exc:  # pragma: no cover - 推送失败绝不影响主流程
        _LOG.debug("[Yanhuo SelfLift] 成片预览推送失败：%s", exc)
        return False


def _extender():
    return vendor.extender_module()


def _disk_module():
    package = vendor.extender_package()
    return importlib.import_module(f"{package.__name__}.motion_context_disk")


def supported_sequence_mode(manifest) -> bool:
    """只有 REF2VA 运动链（含缺省值）支持逐段出片。"""
    mode = str((manifest or {}).get("sequence_mode") or "ref2va").lower()
    return mode in _SUPPORTED_SEQUENCE_MODES


def owner_node_id(manifest) -> str:
    """从 manifest 的 owner_id（如 ``extender_507``）还原节点 id（``507``）。"""
    owner = str((manifest or {}).get("owner_id") or "")
    match = re.fullmatch(r"extender_(\d+)", owner)
    return match.group(1) if match else owner


def default_audio_bitrate() -> str:
    """音轨码率沿用成片导出节点的控件默认值；读不到就用 192k。"""
    try:
        package = vendor.extender_package()
        module = importlib.import_module(f"{package.__name__}.motion_context_disk")
        spec = module.MiniMaxH3MotionContextDiskFinalDecode.INPUT_TYPES()
        for section in ("required", "optional"):
            entry = (spec.get(section) or {}).get("audio_bitrate")
            if isinstance(entry, tuple) and len(entry) == 2:
                options = entry[1]
                default = options.get("default") if isinstance(options, dict) else None
                if default:
                    return str(default)
    except Exception:  # pragma: no cover - 上游缺失时走兜底
        pass
    return "192k"


def output_extension(export_profile=None) -> str:
    """逐段文件的容器后缀 —— 必须跟随导出规格，否则 mux 一定失败。

    v1.13.1：上游 ``_mux_final`` 不显式给 ``-f``，靠输出扩展名推断封装器。
    FFV1 无损是「视频 ffv1 + 音轨 flac」，只能进 Matroska；写死 .mp4 会被 mp4
    封装器直接拒绝。这里改成与上游 sidecar 用同一个判定，保证逐段文件与
    Final Decode 成片的容器一致。
    """
    try:
        return _disk_module()._full_batch_export_profile_extension(export_profile)
    except Exception:  # pragma: no cover - 上游缺失时按 H.264 语义兜底
        codec = str((export_profile or {}).get("codec") or "") if isinstance(export_profile, dict) else ""
        return "mkv" if codec == "FFV1 lossless" else "mp4"


def output_video_path(node_id, prefix, total, export_profile=None) -> Path:
    """逐段文件：output/yanhuo_selflift/<节点id>_clip_NN_of_TT.<mp4|mkv>（重跑覆盖）。"""
    import folder_paths

    out_dir = Path(folder_paths.get_output_directory()) / _OUTPUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(node_id or "chain")).strip("_") or "chain"
    ext = output_extension(export_profile)
    return out_dir / f"{tag}_clip_{int(prefix):02d}_of_{int(total):02d}.{ext}"


def _export_prefix_video(
    data_path,
    manifest_path,
    clip_index,
    vae,
    audio_vae,
    fps,
    export_profile,
) -> Path:
    """把链路前缀 1..N 拼成一个 mp4 并返回输出路径；所有重活都委托上游。"""
    ext = _extender()
    mcd = _disk_module()

    manifest = mcd._load_manifest_from_paths(data_path, manifest_path)
    if not isinstance(manifest, dict):
        raise RuntimeError("逐段出片：读不到链路 manifest。")
    if not supported_sequence_mode(manifest):
        _LOG.info(
            "[Yanhuo SelfLift] 逐段出片：sequence_mode=%s 暂不支持，跳过"
            "（目前仅 REF2VA 运动链模式生效）。",
            manifest.get("sequence_mode"),
        )
        return None
    if export_profile is None:
        _LOG.info(
            "[Yanhuo SelfLift] 逐段出片：本次运行没有固定导出规格"
            "（工作流里没读到成片导出节点的 codec/crf/preset），跳过。"
        )
        return None

    segments = [dict(x) for x in (manifest.get("segments") or [])]
    prefix = int(clip_index) + 1
    total = len(segments)
    if prefix < 1 or prefix > total:
        return None

    profile = mcd.normalize_full_batch_export_profile(export_profile)
    ffmpeg = mcd._find_ffmpeg()
    node_id = owner_node_id(manifest)

    # 音轨 PCM 缓存补齐到前缀 N（上游逐段缓存通常已写好，这里只补缺口）。
    manifest, segs = mcd._ensure_ref2va_audio_cache(
        data_path, manifest_path, manifest, vae, audio_vae, float(fps), count=prefix,
    )
    segs = [dict(x) for x in (segs or [])][:prefix]

    # 逐段最终规格 sidecar（上游刚缓存过，这里基本全部命中、零解码）。
    sidecars = []
    for index in range(prefix):
        manifest, sidecar, decoded = mcd._ensure_ref2va_final_segment_cache(
            data_path, manifest_path, manifest, index, vae, float(fps), ffmpeg, profile,
        )
        del decoded  # 立刻释放可能存在的整段 RGB 张量
        sidecars.append(sidecar)

    output_path = output_video_path(node_id, prefix, total, profile)
    try:
        output_path.unlink(missing_ok=True)  # 重跑覆盖；防播放器占用导致的覆盖失败
    except OSError:
        pass
    if output_path.suffix.lower() == ".mkv":  # FFV1 无损
        _LOG.info(
            "[Yanhuo SelfLift] 逐段出片：本次为 FFV1 无损，逐段文件用 .mkv 容器"
            "（浏览器内预览条放不了 MKV，文件本身正常，用本地播放器看）。"
        )

    token = f"yanhuo_seg_{prefix:02d}_{uuid.uuid4().hex[:8]}"
    mcd._export_final_from_exact_segment_caches(
        ffmpeg, sidecars, data_path, segs, float(fps), output_path, profile,
        default_audio_bitrate(), token,
    )
    _LOG.info(
        "[Yanhuo SelfLift] 逐段出片：%s（第 %d/%d 段）", output_path, prefix, total,
    )
    # v1.7.0：推给节点面板的成片预览条，采样不停顿、边跑边看。
    notify_final_clip(node_id, prefix, total, output_path, fps)
    try:  # 复用上游的进度事件通道，在工具栏状态行提示输出位置
        ext._send_extender_progress(
            node_id, int(clip_index), total, "sampling",
            f"已输出 {prefix}/{total} 段视频 → {output_path.name}",
        )
    except Exception:  # pragma: no cover - 进度提示绝不影响主流程
        pass
    return output_path


def install() -> bool:
    """包装上游 full_batch 的逐段缓存函数；幂等，失败不拖垮节点注册。"""
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        ext = _extender()
        original = getattr(ext, "cache_full_batch_ref2va_segment", None)
    except Exception:  # pragma: no cover - 姊妹包缺失
        return False
    if original is None:
        return False
    if getattr(original, "_yanhuo_segment_export", False):
        _INSTALLED = True
        return True

    @functools.wraps(original)
    def wrapper(
        data_path, manifest_path, clip_index, vae, audio_vae, fps,
        export_profile=None, color_adjustment=None,
    ):
        manifest, info = original(
            data_path, manifest_path, clip_index, vae, audio_vae, fps,
            export_profile=export_profile, color_adjustment=color_adjustment,
        )
        try:
            _export_prefix_video(
                data_path, manifest_path, clip_index, vae, audio_vae, fps, export_profile,
            )
        except Exception:
            _LOG.exception(
                "[Yanhuo SelfLift] 逐段出片失败（第 %d 段）——不影响采样与后续片段。",
                int(clip_index) + 1,
            )
        return manifest, info

    wrapper._yanhuo_segment_export = True
    ext.cache_full_batch_ref2va_segment = wrapper
    _INSTALLED = True
    _LOG.info("[Yanhuo SelfLift] 逐段出片已启用：连跑全部时每段采样完即输出当前成片。")
    return True


__all__ = [
    "FINAL_CLIP_EVENT",
    "install",
    "notify_final_clip",
    "output_extension",
    "output_video_path",
    "owner_node_id",
    "supported_sequence_mode",
]
