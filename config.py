"""SelfLift dual-resolution sampling parameters and cache signature.

Every field here changes what the sampler does, so all of them participate in
the chain-cache signature: switching any of them invalidates the Motion Context
cache automatically instead of silently mixing single-stage and dual-stage
segments inside one continuation chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, replace

_LOG = logging.getLogger("yanhuo_h3_selflift")

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
INPUT_NAMES = ("selflift_sigmas",)

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


__all__ = [
    "INPUT_NAMES",
    "LATENT_UPSAMPLE_MODES",
    "PREFIX",
    "UPGRADER_KEY",
    "WIDGET_NAMES",
    "SelfLiftSettings",
    "sigmas_digest",
]
