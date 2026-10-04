"""Yanhuo H3 Motion Context × SelfLift.

MiniMax H3 Motion Context chaining, rendered with the SelfLift
progressive-resolution sampler instead of a single full-resolution pass.

    low-res Euler prefix -> clean endpoint prediction -> paired lifts
    (latent upsample / learned H3 latent upscaler vs pixel-VAE re-encode)
    -> artifact-aware consistency lift -> re-noise -> high-res Euler suffix

Both source plugins stay untouched and are **not** redistributed here: the
Extender supplies the UI, the clip pipeline and the Motion Context cache;
comfyui-SelfLift supplies the sampling engine. This package only imports them
read-only and binds them together, so both must be installed side by side in
``custom_nodes/``.
"""

__version__ = "1.13.1"

import logging

from .node import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)

WEB_DIRECTORY = "./web"

_log = logging.getLogger("yanhuo_h3_selflift")
if not NODE_CLASS_MAPPINGS:
    _log.error(
        "Yanhuo H3 Motion Context SelfLift: node registration skipped because the "
        "sibling packages could not be loaded. See the errors above."
    )

# 配套节点（参考图打包 / 成片导出 / 视频文件加载）。失败只降级，不拖垮主节点。
try:
    from .companion_nodes import companion_mappings

    extra_mappings, extra_displays = companion_mappings()
    for name, cls in extra_mappings.items():
        NODE_CLASS_MAPPINGS.setdefault(name, cls)
        NODE_DISPLAY_NAME_MAPPINGS.setdefault(name, extra_displays[name])
except Exception:  # pragma: no cover - 姊妹包缺失/无服务器环境
    _log.warning(
        "Yanhuo H3 Motion Context SelfLift: 配套节点注册失败（参考图打包/成片导出/视频加载），"
        "主节点不受影响。",
        exc_info=True,
    )

# v1.5.0 连跑全部逐段出片：包装上游 full_batch 的逐段缓存函数，每段采样完
# 即把当前链路前缀流复制输出为 mp4。失败只降级，不影响主节点。
try:
    from . import segment_export

    segment_export.install()
except Exception:  # pragma: no cover - 姊妹包缺失/无服务器环境
    _log.warning(
        "Yanhuo H3 Motion Context SelfLift: 逐段出片模块安装失败，"
        "连跑全部时不再输出中间视频，主节点不受影响。",
        exc_info=True,
    )

__all__ = [
    "__version__",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
