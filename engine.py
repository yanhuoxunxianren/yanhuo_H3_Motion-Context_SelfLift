"""Dual-resolution sampling engine: SelfLift inside the Motion Context chain.

The Extender renders one continuation clip at a time through a single module-level
entry point, ``extender._sample_h3``. Instead of forking that file, this module
**temporarily installs** a replacement for it, for the duration of one Extender
execution only, and always restores the original in a ``finally`` block. The
replacement hands the clip to SelfLift's own ``progressive_sample``:

    low-res prefix  (transition_step Euler evaluations at lowres_scale)
        -> predict the clean endpoint (x0)
        -> paired lifts: latent upsample (nearest / bilinear / learned 3D upscaler)
                         vs pixel-VAE re-encode
        -> artifact-aware consistency lift (rho, w_min, w_max)
        -> re-noise at the transition sigma (no extra NFE)
    high-res suffix (remaining Euler evaluations at the full latent grid,
                     optionally spatially tiled)

Nothing about how a clip *gets* to the sampler changes: conditioning still
carries ``minimax_keyframes`` and ``minimax_refs``, the latent is still the
Extender's nested AV latent, and the audio stream keeps running through the
reused Euler boundary step. That is what preserves Motion Context continuity.
"""

from __future__ import annotations

import contextlib
import logging
import threading

import comfy.k_diffusion.sampling
import comfy.model_management
import comfy.model_sampling
import comfy.samplers
import torch

from . import config, preview, tiling_fix, vendor

_LOG = logging.getLogger("yanhuo_h3_selflift")

# One Extender execution may span dozens of sampler calls. Serialize the swap so
# two nodes can never observe a half-installed module, and so any other node that
# reached the Extender's sampler waits for the restore.
_SWAP_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------
def _sampler_object(name):
    return comfy.samplers.sampler_object(str(name))


def normalize_sigmas(sigmas):
    """Coerce an external sigma schedule to a 1-D fp32 CPU tensor (or ``None``)."""
    if sigmas is None:
        return None
    try:
        tensor = sigmas.detach().to(device="cpu", dtype=torch.float32).reshape(-1).contiguous()
    except Exception as exc:
        raise ValueError(f"SelfLift: the external sigma input is not a usable tensor ({exc}).") from exc
    if tensor.numel() == 0:
        return None
    return tensor


def _validate_external_sigmas(sigmas, settings: config.SelfLiftSettings):
    """Mirror SelfLift's own schedule invariants before any GPU work starts.

    Returns the settings actually used (see ``SelfLiftSettings.validate``: a
    too-large cut index is clamped with a WARNING rather than aborting)."""
    steps = int(sigmas.numel()) - 1
    if steps < 1:
        raise ValueError(
            "SelfLift: the external sigma schedule needs at least two entries "
            "(start sigma plus the final zero)."
        )
    if not bool(torch.isfinite(sigmas).all()) or bool((sigmas < 0).any()):
        raise ValueError("SelfLift: the external sigma schedule must be finite and nonnegative.")
    if bool((sigmas[1:] > sigmas[:-1]).any()):
        raise ValueError(
            "SelfLift: the external sigma schedule must be non-increasing. "
            "Check the sigma-shaping nodes upstream (split/interpolate/refine)."
        )
    if bool((sigmas[:-1] <= 0).any()):
        raise ValueError(
            "SelfLift: only the final sigma of the external schedule may be zero; "
            "an intermediate zero would end sampling early."
        )
    if float(sigmas[-1]) != 0.0:
        raise ValueError("SelfLift: the external sigma schedule must end at 0.")
    settings = settings.validate(
        steps,
        source=(
            f"the external `selflift_sigmas` table "
            f"({int(sigmas.numel())} entries -> {steps} steps)"
        ),
    )
    legal = steps - 1
    if settings.transition_step > legal:
        raise ValueError(
            f"SelfLift: transition_step {settings.transition_step} exceeds the external "
            f"sigma schedule ({steps} steps, max {legal})."
        )
    if float(sigmas[settings.transition_step]) >= 1.0:
        raise ValueError(
            "SelfLift: the high-resolution starting sigma must be less than 1. "
            "Raise transition_step or reshape the external schedule."
        )
    return settings


def _euler_check(sampler):
    """SelfLift reuses the transition-step prediction, so it needs plain Euler."""
    if not isinstance(sampler, comfy.samplers.KSAMPLER):
        return "SelfLift requires the sampler to expose the standard KSAMPLER interface."
    function = getattr(sampler, "sampler_function", None)
    if function is not comfy.k_diffusion.sampling.sample_euler:
        return "SelfLift requires the standard Euler sampler (sampler_name=euler)."
    try:
        churn = sampler.extra_options.get("s_churn", 0.0)
    except AttributeError:
        churn = 0.0
    if churn != 0.0:
        return "SelfLift requires Euler with s_churn=0."
    return None


def _require_flow_model(model, stage=""):
    """Every stage runs on the same rectified-flow schedule; check one model.

    v1.10.0：pixel-space sanity check — with ``model_hires`` connected the two
    stages can be *different* checkpoints, and both must understand the shared
    sigma table.
    """
    model_sampling = model.get_model_object("model_sampling")
    if not isinstance(model_sampling, comfy.model_sampling.CONST):
        raise ValueError(
            "SelfLift requires a rectified-flow model (constant-shift sampling). "
            f"MiniMax H3 qualifies; the {stage or 'model'} input does not."
        )


def validate_hires_model(model, settings: config.SelfLiftSettings, log=None):
    """Validate the optional ``model_hires`` used by the high-resolution stage."""
    if not settings.enabled or model is None:
        return
    _require_flow_model(model, stage="`model_hires`（高分辨率阶段）")


def validate_plan(model, sampler_name, scheduler, steps, denoise, settings: config.SelfLiftSettings,
                  sigmas=None):
    """Raise ``ValueError`` with actionable text before any GPU work starts."""
    if not settings.enabled:
        return

    external = normalize_sigmas(sigmas)
    if sigmas is not None and external is None:
        _LOG.warning(
            "[Yanhuo SelfLift] selflift_sigmas was connected but resolved to an empty "
            "schedule; falling back to scheduler/steps/denoise for this run."
        )
    if external is None:
        # validate() returns the settings it will actually run with: a too-large
        # cut index is clamped (with a WARNING) instead of killing a run that
        # already spent a minute loading the model.
        settings = settings.validate(
            int(steps),
            source=f"`steps`={int(steps)} with scheduler={scheduler}, denoise={float(denoise)}",
        )
    else:
        # Widget steps/denoise are bypassed entirely; the schedule IS the input.
        settings = _validate_external_sigmas(external, settings)

    _require_flow_model(model, stage="`model`")

    sampler = _sampler_object(sampler_name)
    problem = _euler_check(sampler)
    if problem:
        raise ValueError(f"{problem} Set sampler_name=euler and scheduler=simple.")

    if external is not None:
        return

    sigmas = _schedule(model, scheduler, steps, denoise)
    if sigmas.numel() < 2:
        raise ValueError(
            "SelfLift: the resolved sigma schedule is empty (denoise too small)."
        )
    legal = sigmas.numel() - 2
    if settings.transition_step > legal:
        raise ValueError(
            f"SelfLift: transition_step {settings.transition_step} exceeds the resolved "
            f"schedule (max {legal})."
        )
    if float(sigmas[settings.transition_step]) >= 1.0:
        raise ValueError(
            "SelfLift: the high-resolution starting sigma must be less than 1. "
            "Raise transition_step or use more steps."
        )


def _schedule(model, scheduler, steps, denoise):
    from .vendor import extender_module

    return extender_module()._sigmas(model, scheduler, int(steps), float(denoise))


def schedule_for(base_module, model, scheduler, steps, denoise):
    """Resolved sigma schedule taken from whichever Extender owns the run."""
    return base_module._sigmas(model, scheduler, int(steps), float(denoise))


# ---------------------------------------------------------------------------
# The replacement sampler
# ---------------------------------------------------------------------------
def _effective_transition(settings: config.SelfLiftSettings, steps: int):
    legal = max(1, int(steps) - 1)
    if settings.transition_step <= legal:
        return settings.transition_step
    _LOG.warning(
        "[Yanhuo SelfLift] transition_step %d clamped to %d for %d steps.",
        settings.transition_step,
        legal,
        steps,
    )
    return legal


def _build_lifter(mods, settings: config.SelfLiftSettings):
    if not settings.uses_learned_lifter():
        return None
    upscaler = mods.upscaler
    name = settings.upscaler_model

    def lifter(z0_low, out_hw):
        return upscaler.learned_latent_lift(
            z0_low, out_hw, name, force_unload=settings.upscaler_unload
        )

    return lifter


def _dual_stage(base_module, mods, settings, vae, model, conditioning, latent, seed, sampler_name,
                scheduler, steps, denoise, sigmas_override=None, model_hires=None):
    external = normalize_sigmas(sigmas_override)
    source = "widgets"
    if external is not None:
        # An external schedule replaces the scheduler/steps/denoise widgets;
        # transition_step now indexes into this table.
        sigmas = external
        steps = int(sigmas.numel()) - 1
        source = f"external({int(sigmas.numel())} entries)"
    else:
        sigmas = schedule_for(base_module, model, scheduler, steps, denoise)
    out_single = dict(latent)
    if sigmas.numel() < 2:
        samples = out_single.get("samples")
        if samples is not None:
            out_single["samples"] = samples.to(comfy.model_management.intermediate_device())
        return out_single

    transition_step = _effective_transition(settings, int(steps))
    sampler = _sampler_object(sampler_name)
    lifter = _build_lifter(mods, settings)

    _LOG.info(
        # 注意：占位符个数必须和下面实参个数一一对应。v1.10.0 加了 hires 那段
        # 却漏了 %s，logging 直接抛 "not all arguments converted"，整行 plan
        # 日志就静默不见了（只有 --- Logging error --- 露出来）。
        "[Yanhuo SelfLift] plan: %s | schedule=%s steps=%d | low %d step(s) -> %s lift "
        "-> high %d step(s)%s%s",
        settings.plan_text(int(steps)),
        source,
        steps,
        transition_step,
        "learned" if lifter is not None else settings.latent_upsample,
        max(0, int(steps) - transition_step),
        " (tiled)" if settings.highres_tiling else "",
        " | hires=model_hires" if model_hires is not None else "",
    )

    # 上游约定：低分辨率/高噪阶段（一采）永远跑 model；高分辨率/低噪阶段（二采）
    # 跑 model_hires，未连接时 None -> upstream 自动回退成同一个 model。
    result = mods.progressive_sample(
        model,
        conditioning,          # positive
        conditioning,          # negative: cfg=1 drops it (ComfyUI's cfg1 fast path),
                               # matching the Extender's own single-cond guider.
        vae,
        dict(latent),
        sampler,
        sigmas,
        int(seed),
        1.0,
        transition_step,
        settings.lowres_scale,
        settings.rho,
        settings.w_min,
        settings.w_max,
        settings.latent_upsample,
        latent_lifter=lifter,
        highres_tiling=settings.highres_tiling,
        model_hires=model_hires,
    )

    out = dict(result)
    out.pop("downscale_ratio_spacial", None)
    out.pop("downscale_ratio_temporal", None)
    samples = out.get("samples")
    if samples is not None:
        out["samples"] = samples.to(comfy.model_management.intermediate_device())
    return out


def make_replacement(base_module, mods, settings, vae, sigmas_override=None, session=None,
                     model_hires=None):
    """Build a drop-in ``_sample_h3`` replacement bound to this execution."""
    original = getattr(base_module, "_sample_h3", None)
    if original is None:
        raise RuntimeError(
            "Yanhuo SelfLift: the sibling Extender no longer exposes _sample_h3; "
            "this bridge must be updated for that version."
        )

    def replacement(model, conditioning, latent, seed, sampler_name, scheduler, steps, denoise):
        if not settings.enabled:
            return original(model, conditioning, latent, seed, sampler_name, scheduler, steps, denoise)
        # 每进一次 _sample_h3 就是在跑下一段 CLIP，草稿预览的序号跟着走。
        if session is not None:
            session.note_clip_start()
        return _dual_stage(
            base_module,
            mods,
            settings,
            vae,
            model,
            conditioning,
            latent,
            seed,
            sampler_name,
            scheduler,
            steps,
            denoise,
            sigmas_override=sigmas_override,
            model_hires=model_hires,
        )

    replacement.__doc__ = "Yanhuo SelfLift dual-resolution stand-in for _sample_h3."
    replacement._yanhuo_selflift = True
    replacement._original = original
    return replacement


@contextlib.contextmanager
def installed_sampler(settings, vae, mods=None, base_module=None, sigmas=None, node_id=None,
                      model_hires=None):
    """Install the dual-stage sampler for the duration of one Extender run.

    ``node_id`` 是本次执行的节点 id，草稿预览用它把动图投递到对应的节点面板上。

    The attribute swap touches the sibling Extender module, but it is always
    restored — including on exceptions and interrupts. ``mods`` / ``base_module``
    exist so the bridge stays unit-testable without the real packages.
    ``sigmas`` is an optional externally supplied schedule (the
    ``selflift_sigmas`` input); when present it replaces the scheduler/steps/
    denoise widgets for every clip in this run.
    ``model_hires`` is the optional v1.10.0 second-stage model: the
    low-resolution prefix always samples with the stage's own model, while the
    high-resolution suffix uses ``model_hires`` (falling back to the same model
    when nothing is connected).
    """
    if not settings.enabled:
        yield False
        return

    mods = mods if mods is not None else vendor.selflift_modules()
    if base_module is None:
        base_module = vendor.extender_module()
    if settings.highres_tiling:
        # Motion Context chains carry keyframes *and* references at once, a
        # combination upstream's tile builder does not narrow correctly.
        try:
            tiling_fix.ensure_installed(getattr(mods, "tiling", None), log=_LOG)
        except Exception:
            _LOG.warning(
                "[Yanhuo SelfLift] could not install the tiling correction; "
                "high-resolution tiling may fail on clips with Motion Context keyframes.",
                exc_info=True,
            )
    session = preview.DraftPreviewSession(node_id=node_id)
    restore_preview = preview.install_draft_callback(session)
    replacement = make_replacement(
        base_module, mods, settings, vae, sigmas_override=sigmas, session=session,
        model_hires=model_hires,
    )
    with _SWAP_LOCK:
        original = getattr(base_module, "_sample_h3", None)
        if getattr(original, "_yanhuo_selflift", False):
            raise RuntimeError(
                "Yanhuo SelfLift: another execution already owns the sampler slot; "
                "run only one chain at a time."
            )
        try:
            base_module._sample_h3 = replacement
            yield True
        finally:
            # Restore even when the sampler raised or the job was interrupted.
            base_module._sample_h3 = replacement._original
            if restore_preview is not None:
                restore_preview()


__all__ = ["installed_sampler", "validate_hires_model", "validate_plan"]
