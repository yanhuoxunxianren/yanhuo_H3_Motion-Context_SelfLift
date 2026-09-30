"""内置草稿视频预览（Draft Preview）。

原理：SelfLift 的 ``progressive_sample`` 每一步都会调用
``latent_preview.prepare_callback`` 生成的回调（comfyui-SelfLift/nodes.py:329），
回调拿到的 x0 就是这一步的预测结果。核心预览器只取**第一个时间 token** 出一张静图
（latent_preview.py:76 ``x0[0, :, 0]``），所以这里接一根旁路：把**全部时间 token**
解成 RGB 序列，按 H3 的时间布局还原成真实帧数，编码成动图，用自定义 websocket
消息推给节点面板。

两档解码器，自动选择：
  1. taeh3（models/vae_approx/taeh3.safetensors，9.8MB 微型 VAE）—— 接近真解；
  2. Latent2RGB（latent_formats.MiniMaxH3Video.latent_rgb_factors）—— 线性近似，
     零额外模型、零显存压力，永远可用。

预览失败一律静默，绝不影响采样。
"""

from __future__ import annotations

import base64
import io
import logging
import threading

import torch
import torch.nn.functional as F

_LOG = logging.getLogger("yanhuo_h3_selflift")

# 前端监听的自定义 websocket 事件名
EVENT_NAME = "yanhuo_h3_draft_preview"

DEFAULT_FPS = 24
MAX_WIDTH = 512           # 草稿预览显示宽度（只缩放显示，不改变帧数）
WEBP_QUALITY = 80
MAX_ENCODE_FRAMES = 480   # 编码上限，超长片段均匀抽帧，避免单条消息过大
DECODE_CHUNK = 8          # taeh3 分块解码，控制峰值显存

# H3 的时间布局：5 个 latent token 对应 17 帧，首 token 1 帧、其余各 4 帧。
FRAMES_PER_TOKEN = (1, 4, 4, 4, 4)

_PREVIEWER_CACHE = {}
_CACHE_LOCK = threading.Lock()


def frames_for_seconds(seconds, fps: float = DEFAULT_FPS) -> int:
    """H3 时长（秒）→ 总帧数：

    ``max(5, round(a * fps)) + (5 - (max(5, round(a * fps)) % 17)) % 17``

    保证总帧数落在 17k+5 的网格上。仅用于标签显示/自检；预览实际帧数以 latent
    自身的 token 数为准（更可靠，且天然包含 trim 后的真实长度）。
    """
    base = max(5, int(round(float(seconds) * float(fps))))
    return base + (5 - (base % 17)) % 17


def token_frame_count(tokens: int) -> int:
    """latent token 数 → 真实帧数（按 (1,4,4,4,4) 展开）。"""
    return sum(FRAMES_PER_TOKEN[i % len(FRAMES_PER_TOKEN)] for i in range(max(0, int(tokens))))


# ---------------------------------------------------------------------------
# 解码器
# ---------------------------------------------------------------------------
def _video_stream(x0):
    """从 x0 里取出视频流张量 [B, C, T, H, W]。"""
    if x0 is None:
        return None
    try:
        if getattr(x0, "is_nested", False):
            tensors = getattr(x0, "tensors", None)
            if tensors:
                return tensors[0]
            streams = list(x0.unbind())
            return streams[0] if streams else None
    except Exception:
        return None
    return x0


def _load_previewer(latent_format, device):
    """优先 taeh3 微型 VAE，回退 Latent2RGB。结果按 latent_format 类型缓存。"""
    key = type(latent_format).__name__
    with _CACHE_LOCK:
        cached = _PREVIEWER_CACHE.get(key)
        if cached is not None:
            return cached
        entry = ("latent2rgb", None)
        try:
            import latent_preview
            from comfy.cli_args import LatentPreviewMethod
            from comfy import cli_args as _cli

            previous = getattr(_cli.args, "preview_method", None)
            try:
                # 借核心自己的逻辑找 taeh3：VAE_TAESD 分支才会去 vae_approx 目录
                _cli.args.preview_method = LatentPreviewMethod.TAESD
                previewer = latent_preview.get_previewer(device, latent_format)
            finally:
                if previous is not None:
                    _cli.args.preview_method = previous
            kind = type(previewer).__name__
            if kind == "TAEHVPreviewerImpl" and getattr(previewer, "taesd", None) is not None:
                entry = ("taehv", previewer.taesd)
            elif previewer is not None:
                entry = ("latent2rgb_core", previewer)
        except Exception as exc:
            _LOG.debug("[Yanhuo SelfLift] 微型预览 VAE 不可用，回退 Latent2RGB：%s", exc)
        if entry[0] != "taehv" and entry[1] is None:
            try:
                import latent_preview

                factors = getattr(latent_format, "latent_rgb_factors", None)
                if factors is not None:
                    entry = (
                        "latent2rgb",
                        latent_preview.Latent2RGBPreviewer(
                            factors,
                            getattr(latent_format, "latent_rgb_factors_bias", None),
                            getattr(latent_format, "latent_rgb_factors_reshape", None),
                        ),
                    )
            except Exception:
                entry = ("none", None)
        _PREVIEWER_CACHE[key] = entry
        return entry


def expand_tokens(frames: torch.Tensor, tokens: int = 0) -> torch.Tensor:
    """按 H3 的 (1,4,4,4,4) 布局把 token 帧展开成真实帧数。

    一个 latent token 不等于一帧：首个 token 管 1 帧、其后每个管 4 帧，
    5 个 token = 17 帧。展开后预览帧数 == 片段真实帧数。
    """
    count = int(frames.shape[0]) if not tokens else int(tokens)
    if count <= 0 or int(frames.shape[0]) != count:
        return frames
    repeats = []
    for i in range(count):
        repeats.extend([i] * FRAMES_PER_TOKEN[i % len(FRAMES_PER_TOKEN)])
    return frames[torch.tensor(repeats, dtype=torch.long)]


def _to_uint8(frames: torch.Tensor, do_scale: bool) -> torch.Tensor:
    """[F, C, H, W] 或 [F, H, W, C] 浮点 → [F, H, W, 3] uint8（CPU）。"""
    if frames.ndim == 4 and frames.shape[1] == 3:
        frames = frames.permute(0, 2, 3, 1)
    if do_scale:
        frames = (frames + 1.0) / 2.0
    frames = frames.clamp(0.0, 1.0).mul(255.0)
    return frames.to(device="cpu", dtype=torch.uint8)


def _video_decoder(decoder):
    """拿到微型 VAE 的视频解码入口：decode_video 可能在包装器或 first_stage_model 上。"""
    for owner in (decoder, getattr(decoder, "first_stage_model", None)):
        fn = getattr(owner, "decode_video", None)
        if callable(fn):
            return fn
    return None


def _decode_taehv(decoder, video, max_tokens):
    """分块调用 taeh3 的视频解码，控制峰值显存。"""
    decode_video = _video_decoder(decoder)
    tokens = int(video.shape[2])
    indices = list(range(tokens))
    if max_tokens and 0 < max_tokens < tokens:
        picks = torch.linspace(0, tokens - 1, max_tokens).round().long().tolist()
        indices = sorted(set(int(i) for i in picks))
    if decode_video is None:
        # 退化路径：逐 token 走普通 decode（慢一些，但不会崩）
        plain = getattr(decoder, "decode", None)
        if not callable(plain):
            return None
        chunks = []
        for i in indices[:max_tokens] if max_tokens else indices:
            frames = plain(video[:1, :, i:i + 1])
            if frames is None:
                return None
            chunks.append(_to_uint8(frames[0].float(), do_scale=False))
            del frames
        return torch.cat(chunks, dim=0) if chunks else None

    chunks = []
    for start in range(0, len(indices), DECODE_CHUNK):
        part = indices[start:start + DECODE_CHUNK]
        frames = decode_video(video[:1], frame_indices=part)
        if frames is None:
            return None
        chunks.append(_to_uint8(frames[0].float(), do_scale=False))
        del frames
    if not chunks:
        return None
    return torch.cat(chunks, dim=0)


def _decode_latent2rgb(previewer, video):
    """Latent2RGB：一次 24→3 矩阵乘，全时间 token 一起算。"""
    factors = getattr(previewer, "latent_rgb_factors", None)
    if factors is None:
        return None
    v = video[0]                     # [C, T, H, W]
    channels = int(v.shape[0])
    f = factors.to(device=v.device, dtype=torch.float32)
    if f.shape[1] != channels:
        return None
    bias = getattr(previewer, "latent_rgb_factors_bias", None)
    b = bias.to(device=v.device, dtype=torch.float32) if bias is not None else None
    if b is not None and b.shape[0] != f.shape[0]:
        b = None
    rgb = F.linear(v.permute(1, 2, 3, 0).float(), f, bias=b)   # [T, H, W, 3]
    return _to_uint8(rgb, do_scale=True)


def decode_draft(video, latent_format, device, max_tokens=0):
    """[B,C,T,H,W] 视频 latent → uint8 [F,H,W,3]（已按 H3 时间布局展开到真实帧数）。"""
    if video is None or video.ndim != 5:
        return None, ""
    kind, previewer = _load_previewer(latent_format, device)
    tokens = int(video.shape[2])
    try:
        if kind == "taehv":
            frames = _decode_taehv(previewer, video, max_tokens)
            used = "taeh3"
        elif kind in ("latent2rgb", "latent2rgb_core"):
            frames = _decode_latent2rgb(previewer, video)
            used = "latent2rgb"
        else:
            return None, ""
    except Exception as exc:
        _LOG.debug("[Yanhuo SelfLift] 草稿解码失败，本步跳过：%s", exc)
        return None, ""
    if frames is None or frames.shape[0] == 0:
        return None, used
    # 解码器每个 token 出一帧时，按 (1,4,4,4,4) 展开到片段真实帧数
    if int(frames.shape[0]) == tokens:
        frames = expand_tokens(frames, tokens)
    return frames, used


def encode_webp(frames: torch.Tensor, fps: int = DEFAULT_FPS, max_width: int = MAX_WIDTH):
    """uint8 [F,H,W,3] → 动画 WebP 字节流 + 实际帧数。"""
    try:
        from PIL import Image
    except Exception:
        return None, 0
    total = int(frames.shape[0])
    if total <= 0:
        return None, 0
    if total > MAX_ENCODE_FRAMES:
        idx = torch.linspace(0, total - 1, MAX_ENCODE_FRAMES).round().long()
        frames = frames[idx]
        total = int(frames.shape[0])
    width = int(frames.shape[2])
    height = int(frames.shape[1])
    size = None
    if width > max_width > 0:
        size = (int(max_width), max(1, int(round(height * max_width / float(width)))))
    images = []
    for i in range(total):
        image = Image.fromarray(frames[i].numpy(), mode="RGB")
        if size is not None:
            image = image.resize(size, Image.BILINEAR)
        images.append(image)
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=images[1:],
        duration=max(20, int(round(1000.0 / float(fps)))),
        quality=WEBP_QUALITY,
        lossless=False,
        minimize_size=True,
    )
    return buffer.getvalue(), total


def _send(payload) -> bool:
    try:
        from server import PromptServer

        instance = getattr(PromptServer, "instance", None)
        if instance is None:
            return False
        instance.send_sync(EVENT_NAME, payload, None)
        return True
    except Exception as exc:
        _LOG.debug("[Yanhuo SelfLift] 草稿预览推送失败：%s", exc)
        return False


class DraftPreviewSession:
    """一次 Extender 执行里的草稿预览会话。

    ``clip_ordinal`` 由 engine 在每次 ``_sample_h3`` 调用时递增，所以标签里的
    CLIP 序号与正在跑的段一致（clip_by_clip 一次执行只跑一段，full_batch 依次递增）。
    """

    def __init__(self, node_id=None, fps: int = DEFAULT_FPS, max_tokens: int = 0):
        self.node_id = str(node_id) if node_id is not None else ""
        self.fps = int(fps) if fps else DEFAULT_FPS
        self.max_tokens = int(max_tokens or 0)
        self.clip_ordinal = 0
        self._lock = threading.Lock()
        self.emitted = 0

    def note_clip_start(self):
        with self._lock:
            self.clip_ordinal += 1
            return self.clip_ordinal

    def current_clip(self):
        with self._lock:
            return self.clip_ordinal

    def emit(self, step, x0, total_steps, latent_format=None, device=None, stage=""):
        try:
            video = _video_stream(x0)
            frames, used = decode_draft(video, latent_format, device, max_tokens=self.max_tokens)
            if frames is None:
                return False
            blob, count = encode_webp(frames, fps=self.fps)
            if not blob:
                return False
            ok = _send({
                "node_id": self.node_id,
                "clip": int(self.current_clip()),
                "step": int(step),
                "total_steps": int(total_steps),
                "stage": str(stage or ""),
                "frames": int(count),
                "fps": int(self.fps),
                "decoder": used,
                "format": "webp",
                "data": base64.b64encode(blob).decode("ascii"),
            })
            if ok:
                self.emitted += 1
                if self.emitted == 1:
                    _LOG.info(
                        "[Yanhuo SelfLift] 草稿视频预览已启用：解码器=%s，"
                        "帧数随当前 CLIP 的 latent 动态对齐（%d 帧 @ %dfps）。",
                        used, count, self.fps,
                    )
            return ok
        except Exception as exc:
            _LOG.debug("[Yanhuo SelfLift] 草稿预览跳过（step %s）：%s", step, exc)
            return False


def install_draft_callback(session: DraftPreviewSession):
    """把 SelfLift 用的 ``latent_preview.prepare_callback`` 换成带旁路的版本。

    只在 engine 的采样锁内调用；返回还原函数。原回调（进度条 + 核心静图预览）
    照常执行，我们只是追加一条动图消息。
    """
    try:
        import latent_preview as _core
    except Exception:
        return None

    original = getattr(_core, "prepare_callback", None)
    if original is None:
        return None

    def patched(model, steps, x0_output_dict=None):
        inner = original(model, steps, x0_output_dict)
        latent_format = None
        device = None
        try:
            latent_format = model.model.latent_format
            device = model.load_device
        except Exception:
            pass

        def callback(step, x0, x, total_steps):
            result = inner(step, x0, x, total_steps)
            session.emit(step, x0, total_steps, latent_format=latent_format, device=device)
            return result

        return callback

    _core.prepare_callback = patched
    return lambda: setattr(_core, "prepare_callback", original)


__all__ = [
    "EVENT_NAME",
    "DraftPreviewSession",
    "frames_for_seconds",
    "token_frame_count",
    "install_draft_callback",
    "decode_draft",
    "encode_webp",
]
