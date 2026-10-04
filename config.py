"""SelfLift dual-resolution sampling parameters and cache signature.

Every field here changes what the sampler does, so all of them participate in
the chain-cache signature: switching any of them invalidates the Motion Context
cache automatically instead of silently mixing single-stage and dual-stage
segments inside one continuation chain.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
from dataclasses import dataclass, replace

_LOG = logging.getLogger("yanhuo_h3_selflift")

# ``torch`` is optional here: the digest helpers degrade to "?" without it, and
# cache invalidation then falls back to other signals instead of importing a
# heavy dependency at module load.
torch = None

# Widget names exposed by the node. Everything is prefixed so the inherited
# Extender widgets stay untouched.
PREFIX = "selflift_"

WIDGET_NAMES = (
    "selflift_enabled",
    "selflift_transition_step",
    "selflift_lowres_scale",
    "selflift_rho",
    "selflift_w_min",
    "selflift_w_max",
    "selflift_latent_upsample",
    "selflift_upscaler_model",
    "selflift_upscaler_unload",
    "selflift_highres_tiling",
    # v1.6.0 内置语义桥（Semantic Bridge）
    "selflift_bridge_enabled",
    "selflift_bridge_adapter",
    "selflift_bridge_alpha",
    "selflift_bridge_magnitude",
)

# External input sockets (not widgets): declared in INPUT_TYPES so the frontend
# renders a socket, peeled off the payload before the parent's extend() runs.
# v1.10.0：model_hires 不接受 selflift_ 前缀——它要插在父节点的 model 端口正
# 下方，名字必须一目了然，且不能落进 WIDGET_NAMES（那是值类型），也不参与
# split_settings 的剥离（它在 extend_with_selflift 里单独取出）。
INPUT_NAMES = ("selflift_sigmas", "model_hires")

# 二采（高分辨率/低噪阶段）专用模型端口。
MODEL_HIRES_INPUT = "model_hires"

LATENT_UPSAMPLE_MODES = ("nearest", "bilinear")

UPGRADER_KEY = "selflift_plan_signature"


def _clamp_float(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value):
        return float(default)
    return float(min(max(value, low), high))


def _clamp_int(value, low, high, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return int(default)
    return int(min(max(value, low), high))


@dataclass(frozen=True)
class SelfLiftSettings:
    """Resolved, always-valid values for one execution."""

    enabled: bool = True
    transition_step: int = 2
    lowres_scale: float = 0.5
    rho: float = 0.6
    w_min: float = 1.0
    w_max: float = 1.0
    latent_upsample: str = "nearest"
    upscaler_model: str = "none"
    upscaler_unload: bool = True
    highres_tiling: bool = False
    # v1.6.0 内置语义桥：默认关闭，对既有工作流零影响。
    bridge_enabled: bool = False
    bridge_adapter: str = ""
    bridge_alpha: float = 0.10
    bridge_magnitude: str = "per_token"
    # Fingerprint of an externally supplied sigma schedule ("": use widgets).
    sigmas_digest: str = ""
    # v1.10.0 二采专用模型：``hires_model_linked`` 为真时高分辨率阶段跑
    # model_hires；``hires_model_digest`` 是该模型的内容指纹（""=取不到）。
    # 两者都进缓存签名，避免换二采模型后继续吃老 segment。
    hires_model_linked: bool = False
    hires_model_digest: str = ""

    # -- construction -----------------------------------------------------
    @classmethod
    def from_kwargs(cls, kwargs: dict):
        """Read the widget values; every key is optional."""
        source = kwargs or {}
        scale = _clamp_float(source.get("selflift_lowres_scale"), 0.25, 1.0, 0.5)
        return cls(
            enabled=bool(source.get("selflift_enabled", True)),
            transition_step=_clamp_int(source.get("selflift_transition_step"), 1, 10000, 2),
            lowres_scale=_clamp_float(round(scale / 0.05) * 0.05, 0.25, 1.0, 0.5),
            rho=_clamp_float(source.get("selflift_rho"), 0.0, 1.0, 0.6),
            w_min=_clamp_float(source.get("selflift_w_min"), 0.0, 1.0, 1.0),
            w_max=_clamp_float(source.get("selflift_w_max"), 0.0, 1.0, 1.0),
            latent_upsample=(
                str(source.get("selflift_latent_upsample") or "nearest")
                if str(source.get("selflift_latent_upsample") or "nearest") in LATENT_UPSAMPLE_MODES
                else "nearest"
            ),
            upscaler_model=str(source.get("selflift_upscaler_model") or "none"),
            upscaler_unload=bool(source.get("selflift_upscaler_unload", True)),
            highres_tiling=bool(source.get("selflift_highres_tiling", False)),
            bridge_enabled=bool(source.get("selflift_bridge_enabled", False)),
            bridge_adapter=str(source.get("selflift_bridge_adapter") or ""),
            bridge_alpha=_clamp_float(source.get("selflift_bridge_alpha"), 0.0, 1.0, 0.10),
            bridge_magnitude=(
                str(source.get("selflift_bridge_magnitude") or "per_token")
                if str(source.get("selflift_bridge_magnitude") or "per_token")
                in ("per_token", "global", "none")
                else "per_token"
            ),
        )

    def with_effective_transition(self, steps: int):
        """Clamp ``transition_step`` into the legal range for this step count."""
        legal = max(1, int(steps) - 1)
        return replace(self, transition_step=min(max(1, int(self.transition_step)), legal))

    # -- validation -------------------------------------------------------
    def validate(self, steps: int, source: str = "", log=None):
        """Raise ``ValueError`` on combinations SelfLift cannot honour.

        ``source`` names where the step count came from (steps widget vs an
        external sigma table) so the message can say what to change.

        Returns the settings actually used for the run. ``transition_step`` is a
        *cut index* into the schedule, and for too-large values the only sane
        legal value is unambiguous (the last usable index), so since v1.3.1 it
        is clamped with a WARNING instead of aborting a run that has already
        spent a minute loading models. Everything that SelfLift cannot guess
        (steps < 2, bad model/sampler, broken external table) still raises.
        """
        steps = int(steps)
        if not self.enabled:
            return self
        where = source or "the `steps` widget"
        if steps < 2:
            raise ValueError(
                f"SelfLift: steps must be >= 2. {steps} step(s) were resolved from {where}, "
                "but a dual-resolution plan needs at least one low-resolution and one "
                "high-resolution step."
            )
        legal = steps - 1
        resolved = self
        if self.transition_step < 1 or self.transition_step > legal:
            resolved = self.with_effective_transition(steps)
            hint = (
                " Note: with `selflift_sigmas` connected the step count comes from that "
                "table (entries - 1), not from the `steps` widget."
                if self.uses_external_sigmas()
                else " If you meant to drive this from an external sigma chain "
                "(scheduler + interpolation + H3 Sigma Refiner), `selflift_sigmas` is "
                "not in effect for this run: the step count is coming from the `steps` "
                "widget above. Check that the refiner's output is connected to the "
                "`selflift_sigmas` input and that it is not empty."
            )
            (log or _LOG).warning(
                "[Yanhuo SelfLift] transition_step %d is out of range for %d steps "
                "(resolved from %s); clamped to %d. The cut index must leave at least "
                "one step for the high-resolution stage, so the effective plan is "
                "%d low-resolution + %d high-resolution step(s). Set "
                "`selflift_transition_step` to <= %d to silence this message.%s",
                self.transition_step,
                steps,
                where,
                resolved.transition_step,
                resolved.transition_step,
                max(1, steps - resolved.transition_step),
                legal,
                hint,
            )
        if not 0.25 <= resolved.lowres_scale <= 1.0:
            raise ValueError("SelfLift: lowres_scale must be between 0.25 and 1.0.")
        if not 0.0 <= resolved.rho <= 1.0:
            raise ValueError("SelfLift: rho must be between 0.0 and 1.0.")
        if not 0.0 <= resolved.w_min <= resolved.w_max <= 1.0:
            raise ValueError("SelfLift: weights must satisfy 0 <= w_min <= w_max <= 1.")
        if resolved.rho == 0.0 and resolved.upscaler_model in ("", "none"):
            raise ValueError(
                "SelfLift: rho=0 with upscaler_model=none disables both the "
                "SelfLift-zero correction and external latent upscaling. Raise rho "
                "(start near 0.6) or select an external H3 latent upscaler."
            )
        return resolved

    # -- description ------------------------------------------------------
    def uses_pixel_anchor(self) -> bool:
        return self.rho > 0.0 and self.w_max > 0.0

    def uses_learned_lifter(self) -> bool:
        return bool(self.upscaler_model) and self.upscaler_model != "none"

    def uses_external_sigmas(self) -> bool:
        return bool(self.sigmas_digest)

    def uses_second_stage_model(self) -> bool:
        """True when the high-resolution stage runs on ``model_hires``."""
        return bool(self.hires_model_linked)

    def direct_lift_label(self) -> str:
        if not (self.rho >= 1.0 and self.w_min >= 1.0 and self.w_max >= 1.0):
            return "learned-upscaler" if self.uses_learned_lifter() else self.latent_upsample
        return "skipped"

    def plan_text(self, steps: int) -> str:
        effective = self.with_effective_transition(steps)
        return (
            f"selflift={self.enabled} low_steps={effective.transition_step}/"
            f"high_steps={max(0, int(steps) - effective.transition_step)} "
            f"lowres={self.lowres_scale:.2f} rho={self.rho:.2f} "
            f"w=({self.w_min:.2f},{self.w_max:.2f}) lift={self.direct_lift_label()} "
            f"pixel_anchor={self.uses_pixel_anchor()} tiling={self.highres_tiling}"
            + (" sigmas=external" if self.uses_external_sigmas() else "")
            + (" hires=custom" if self.uses_second_stage_model() else "")
        )

    def signature(self) -> str:
        payload = {
            "enabled": bool(self.enabled),
            "transition_step": int(self.transition_step),
            "lowres_scale": round(float(self.lowres_scale), 6),
            "rho": round(float(self.rho), 6),
            "w_min": round(float(self.w_min), 6),
            "w_max": round(float(self.w_max), 6),
            "latent_upsample": self.latent_upsample,
            "upscaler_model": self.upscaler_model,
            "highres_tiling": bool(self.highres_tiling),
            # Fingerprint of the external sigma schedule; empty when the
            # widgets drive the schedule. Without this, rewiring the sigma
            # chain would leave the whole cache looking "unchanged".
            "sigmas": self.sigmas_digest,
        }
        # v1.10.0：只在真的接了 model_hires 时才进签名——没接的用户升级后不会
        # 白白重渲一次；接上/换掉/拔掉都必然重渲（结果确实会变）。
        if self.hires_model_linked:
            payload["hires_model"] = self.hires_model_digest
        # v1.6.0：语义桥只在开启时进签名——没开的用户不会因为升级而白白重渲一次，
        # 开了以后改模型/强度/对齐方式则必定重渲（结果确实会变）。
        bridge = self.bridge_signature()
        if bridge:
            payload["bridge"] = bridge
        # upscaler_unload only affects residency, never the result.
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def bridge_signature(self) -> str:
        """语义桥对出片结果的影响指纹；关闭时为空字符串。"""
        if not self.bridge_enabled:
            return ""
        return (
            f"bridge:{self.bridge_adapter}"
            f":{round(float(self.bridge_alpha), 4)}"
            f":{self.bridge_magnitude}"
        )


def sigmas_digest(sigmas) -> str:
    """Stable short fingerprint of an external sigma schedule ("" when absent).

    The digest feeds the cache signature, so rewiring the sigma chain must
    produce a different value even when the widget-level plan is identical.
    """
    if sigmas is None:
        return ""
    try:
        import torch

        tensor = sigmas.detach().to(device="cpu", dtype=torch.float32).reshape(-1).contiguous()
    except Exception:
        return ""
    if tensor.numel() == 0:
        return ""
    digest = hashlib.sha256(tensor.numpy().tobytes()).hexdigest()[:16]
    return f"{int(tensor.numel())}:{digest}"


def model_digest(model, samples: int = 24) -> str:
    """内容指纹：换了权重的 MODEL 必须给出不同的串（"" = 读不出来）。

    ``ModelPatcher`` 没有任何可序列化的身份：既不像 sigma 那样是个张量，
    也不能指望对象地址稳定（ComfyUI 重启一次就全换）。所以这里直接哈希"决定
    出片的东西"——参数的名/形状/精度、`samples` 个散布在权重里的**采样值**、
    以及每一层 patch（LoRA）：名字、strength、以及 patch 张量本身的采样值。

    采样值是关键：两个同为 H3 架构但权重不同的 checkpoint，形状精度完全一样，
    只有真实数值能区分。取 4 个值用 ``index_select`` 后搬到 CPU，拷贝量是常数，
    对 30GB 的模型也一样便宜（每次执行算一次）。
    """
    if model is None:
        return ""
    digest = hashlib.sha256()
    hits = 0

    try:
        params = list(model.model.named_parameters())
    except Exception:
        params = []
    digest.update(f"params={len(params)}".encode("utf-8"))
    if params:
        step = max(1, len(params) // max(1, samples))
        for name, tensor in params[::step][: max(1, samples)]:
            digest.update(f"|p:{name}:{tuple(tensor.shape)}:{tensor.dtype}".encode("utf-8", "replace"))
            values = _sampled_values(tensor)
            hits += 0 if values == "?" else 1
            digest.update((">" + values).encode("utf-8", "replace"))

    for key, entries in _sorted_items(getattr(model, "patches", None) or {}):
        digest.update(f"|lora:{key}".encode("utf-8", "replace"))
        for entry in list(entries or [])[:8]:
            summary = _patch_summary(entry)
            hits += 0 if summary in ("", "?", "-") else 1
            digest.update((">" + summary).encode("utf-8", "replace"))

    objects = _sorted_items(getattr(model, "object_patches", None) or {})
    if objects:
        digest.update(
            ("|obj:" + ",".join(key for key, _v in objects)).encode("utf-8", "replace")
        )
        hits += 1

    if hits == 0:
        # 一个像样的值都没取到（自定义包装 / 权重在 meta 上 / 非 ComfyUI 模型）。
        # 宁可让调用方走别的兜底，也不要给两个不同模型算出同一串。
        return ""
    return digest.hexdigest()[:16]


def _is_tensor(value) -> bool:
    return hasattr(value, "detach") and hasattr(value, "shape")


def _sorted_items(mapping):
    try:
        return sorted(mapping.items(), key=lambda kv: str(kv[0]))
    except Exception:
        return []


def _sampled_values(tensor, count: int = 4) -> str:
    """Spread ``count`` values across a weight without ever copying it."""
    global torch
    if torch is None:
        try:
            torch = importlib.import_module("torch")
        except Exception:
            return "?"
    try:
        flat = tensor.detach().reshape(-1)
        total = int(flat.numel())
        if total <= 0:
            return "-"
        picks = torch.linspace(0, total - 1, min(count, total)).to(torch.int64).to(flat.device)
        values = flat.index_select(0, picks).to(device="cpu", dtype=torch.float64)
        return ".".join(f"{value:.8e}" for value in values.tolist())
    except Exception:
        return "?"


def _walk(value, depth: int = 0):
    """Yield every tensor / scalar inside a nested patch container."""
    if _is_tensor(value):
        yield value
        return
    if depth > 3:
        return
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        yield value
        return
    for child in children:
        for found in _walk(child, depth + 1):
            yield found


def _patch_summary(entry) -> str:
    parts = []
    for item in _walk(entry):
        if _is_tensor(item):
            parts.append(_sampled_values(item))
        elif isinstance(item, float):
            parts.append(f"{item:.10g}")
        elif isinstance(item, int):
            parts.append(str(item))
        elif item is None:
            parts.append("-")
        else:
            parts.append(str(item)[:48])
    return ";".join(parts)


__all__ = [
    "INPUT_NAMES",
    "LATENT_UPSAMPLE_MODES",
    "MODEL_HIRES_INPUT",
    "PREFIX",
    "UPGRADER_KEY",
    "WIDGET_NAMES",
    "SelfLiftSettings",
    "model_digest",
    "sigmas_digest",
]
