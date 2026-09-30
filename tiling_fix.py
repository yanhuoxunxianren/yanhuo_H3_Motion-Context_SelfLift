"""Runtime correction for SelfLift's MiniMax H3 high-resolution spatial tiling.

Why this file exists
--------------------
``comfyui-SelfLift/h3_tiling.py`` slices the *target* video latent into spatial
tiles and, for each tile, rebuilds a tile-sized ``PackedLayout``. The packed
sequence contains conditioning rows (``cond`` blocks from ``minimax_keyframes``
and ``ref_img`` blocks from ``minimax_refs``) ahead of the target video rows,
and MiniMax H3 fills them with::

    all_video_rows[~img_update] = cond_video_rows        # ldm/minimax/model.py

so the number of rows patchified out of ``payload["cond_video_latents"]`` must
equal the number of non-target rows the tile layout declares.

Keyframe blocks share the *target* spatial grid, so they must be narrowed along
with the tile. Reference blocks keep their own grid, so they must not. Upstream
only rebuilds the keyframe latents when there are **no refs**::

    if payload.get("keyframes"):
        ...
        tiled["keyframes"] = keyframes
        if not payload.get("refs"):
            tiled["cond_video_latents"] = [...]

Standalone SelfLift graphs never hit that branch (keyframes and refs rarely
coexist there). The Motion Context chain always does - the sibling Extender
deliberately composes ``cond_video_latents`` as
``[keyframe latents] + [ref latents]`` (``patch_motion_payload.py``) so carried
video conditioning survives Ref2VA references. With tiling on, the keyframe
rows stay at full resolution while the tile layout expects the narrowed ones,
and the sampler dies with a bare
``RuntimeError: shape mismatch: value tensor of shape [...]``.

This module installs a corrected ``_tile_payload`` that

* always rebuilds the keyframe entries of ``cond_video_latents`` (references
  are passed through untouched, in their original order),
* verifies the rebuilt list against the tile layout *before* any GPU work,
* degrades to a single full-frame evaluation (with a loud error log) instead of
  crashing the run if a payload shape is still inconsistent.

No file in ``comfyui-SelfLift`` or ``ComfyUI_MiniMax_H3_Extender`` is edited;
the correction is an in-process replacement of two module-level helpers, and
the originals are kept so it can be undone.
"""

from __future__ import annotations

import logging

import comfy.ldm.common_dit

from . import vendor

_LOG = logging.getLogger("yanhuo_h3_selflift")

_MARKER = "_yanhuo_h3_tiling_fix"

_original_tile_payload = None
_original_tiled_forward = None
_fallback_logged = False

# Row kinds inside the packed image stream that are filled from
# ``cond_video_latents`` (everything else is the generated target).
_COND_KINDS = ("cond", "ref_img")


class TilingIncompatible(RuntimeError):
    """A payload cannot be tiled safely; the caller falls back to full-frame."""


def _patch_rows(latent) -> int:
    """Rows ``patchify_video`` produces for one conditioning latent."""
    batch, _channels, frames, height, width = latent.shape
    return batch * frames * (height // 2) * (width // 2)


def _expected_cond_rows(layout) -> int:
    return sum(stop - start for start, stop, kind in layout.segments if kind in _COND_KINDS)


def _make_tile_payload(packed_layout):
    """Build the corrected ``_tile_payload`` bound to upstream's layout helper."""

    def _tile_payload(payload, context, video, audio, axis, start, end):
        height, width = video.shape[-2:]
        padded_height, padded_width = (height + 1) // 2 * 2, (width + 1) // 2 * 2
        full_layout = payload.get("layout")
        signature = (context.shape[1], video.shape[2], padded_height, padded_width, audio.shape[-1])
        if full_layout is None or full_layout.signature != signature:
            full_layout = packed_layout(signature, payload)

        tiled = payload.copy()
        cond_video = list(payload.get("cond_video_latents") or [])
        rebuilt = []

        if payload.get("keyframes"):
            keyframes = []
            for keyframe in payload["keyframes"]:
                latent = keyframe.get("latent")
                if latent is None:
                    keyframes.append(keyframe)
                    continue
                if latent.shape[-2:] != (height, width):
                    raise ValueError(
                        "SelfLift: tiled H3 keyframes must match the target latent height and width"
                    )
                region = comfy.ldm.common_dit.pad_to_patch_size(
                    latent.narrow(axis, start, end - start), (1, 2, 2)
                ).contiguous()
                keyframes.append({**keyframe, "latent": region})
                rebuilt.append(region)
            tiled["keyframes"] = keyframes

        if rebuilt:
            # Packed order is: keyframe cond blocks first, then reference
            # blocks. References keep their own grid, so only the leading
            # keyframe entries are swapped for their narrowed copies.
            tiled["cond_video_latents"] = rebuilt + cond_video[len(rebuilt):]

        tile_height = end - start if axis == 3 else height
        tile_width = end - start if axis == 4 else width
        layout = packed_layout(
            (context.shape[1], video.shape[2], (tile_height + 1) // 2 * 2,
             (tile_width + 1) // 2 * 2, audio.shape[-1]),
            tiled,
        )

        if len(full_layout.segments) != len(layout.segments):
            raise TilingIncompatible(
                "SelfLift H3 tiling: the tile layout declares %d packed segments "
                "but the full layout declares %d." % (len(layout.segments), len(full_layout.segments))
            )
        expected = _expected_cond_rows(layout)
        actual = sum(_patch_rows(latent) for latent in (tiled.get("cond_video_latents") or []))
        if expected != actual:
            raise TilingIncompatible(
                "SelfLift H3 tiling: tiled conditioning carries %d visual rows but the "
                "tile layout expects %d (keyframes=%d, references=%d)."
                % (actual, expected, len(rebuilt), len(cond_video) - len(rebuilt))
            )

        for (source_start, source_end, kind), (target_start, target_end, _) in zip(
            full_layout.segments, layout.segments
        ):
            positions = full_layout.position_ids[source_start:source_end]
            if kind in ("cond", "video"):
                positions = positions.reshape(-1, padded_height // 2, padded_width // 2, 3)
                positions = positions.narrow(axis - 2, start // 2, (end - start + 1) // 2).reshape(-1, 3)
            layout.position_ids[target_start:target_end].copy_(positions)

        tiled["layout"] = layout
        return tiled

    return _tile_payload


def _make_tiled_forward(original):
    """Wrap upstream's tile driver so an incompatible payload degrades gracefully."""

    def _tiled_forward(executor, streams, timestep, context, transformer_options,
                       minimax_payload=None, n_tiles=2, plan=None, **kwargs):
        try:
            return original(executor, streams, timestep, context, transformer_options,
                            minimax_payload=minimax_payload, n_tiles=n_tiles, plan=plan, **kwargs)
        except TilingIncompatible as exc:
            global _fallback_logged
            if not _fallback_logged:
                _fallback_logged = True
                _LOG.error(
                    "[Yanhuo SelfLift] %s Falling back to a single full-frame high-resolution "
                    "evaluation for this run - expect a much larger memory spike. Turn "
                    "selflift_highres_tiling off if it cannot be allocated.",
                    exc,
                )
            return executor(streams, timestep, context, transformer_options,
                            minimax_payload=minimax_payload, **kwargs)

    return _tiled_forward


def is_installed(tiling=None) -> bool:
    module = tiling if tiling is not None else _tiling_module()
    return bool(getattr(getattr(module, "_tile_payload", None), _MARKER, False))


def _tiling_module():
    return vendor.selflift_modules().tiling


def ensure_installed(tiling=None, log=None) -> bool:
    """Install the correction once. Idempotent; never raises into the caller."""
    global _original_tile_payload, _original_tiled_forward
    logger = log or _LOG

    if is_installed(tiling):
        return True

    module = tiling if tiling is not None else _tiling_module()
    original_payload = getattr(module, "_tile_payload", None)
    original_forward = getattr(module, "_tiled_forward", None)
    packed_layout = getattr(module, "_packed_layout", None)
    if original_payload is None or original_forward is None or packed_layout is None:
        logger.warning(
            "[Yanhuo SelfLift] h3_tiling does not expose the expected helpers; "
            "the tiling correction was not installed."
        )
        return False

    fixed_payload = _make_tile_payload(packed_layout)
    setattr(fixed_payload, _MARKER, True)
    fixed_payload._yanhuo_original = original_payload
    fixed_forward = _make_tiled_forward(original_forward)
    setattr(fixed_forward, _MARKER, True)
    fixed_forward._yanhuo_original = original_forward

    _original_tile_payload = original_payload
    _original_tiled_forward = original_forward
    module._tile_payload = fixed_payload
    module._tiled_forward = fixed_forward
    logger.info(
        "[Yanhuo SelfLift] installed the Motion-Context tiling correction "
        "(keyframe + reference conditioning) into comfyui-SelfLift.h3_tiling."
    )
    return True


def restore(tiling=None) -> None:
    """Undo the correction (diagnostics / tests only)."""
    global _original_tile_payload, _original_tiled_forward
    module = tiling if tiling is not None else _tiling_module()
    if _original_tile_payload is not None:
        module._tile_payload = _original_tile_payload
    if _original_tiled_forward is not None:
        module._tiled_forward = _original_tiled_forward
    _original_tile_payload = None
    _original_tiled_forward = None


__all__ = ["TilingIncompatible", "ensure_installed", "is_installed", "restore"]
