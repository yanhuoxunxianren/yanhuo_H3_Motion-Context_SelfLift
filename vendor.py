"""Locate and *reuse* the two sibling custom-node packages.

Nothing in this project ever writes to the source trees of

* ``ComfyUI_MiniMax_H3_Extender``  – Motion Context chaining, cards, cache, UI
* ``comfyui-SelfLift``             – progressive-resolution sampling engine

Both are imported read-only and, whenever ComfyUI has already imported them,
the **existing module instance is shared** instead of creating a second copy.
Sharing matters: ``comfyui-SelfLift`` keeps a process-wide model cache in
``h3_upscaler._model_cache``; a duplicate import would mean two copies of the
3D latent upscaler resident in VRAM.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

_LOG = logging.getLogger("yanhuo_h3_selflift")

_THIS = Path(__file__).resolve()
# <...>/custom_nodes/yanhuo_H3_Motion-Context_SelfLift/vendor.py
CUSTOM_NODES_DIR = _THIS.parent.parent

EXTENDER_DIR_NAME = "ComfyUI_MiniMax_H3_Extender"
EXTENDER_MODULE_NAME = "ComfyUI_MiniMax_H3_Extender"
SELFLIFT_DIR_NAME = "comfyui-SelfLift"
SELFLIFT_FALLBACK_MODULE_NAME = "yanhuo_selflift_vendor"

MISSING_HINT = (
    "Yanhuo H3 Motion Context × SelfLift requires both sibling packages to be "
    "installed in ComfyUI/custom_nodes/. They must stay intact; this package "
    "only imports them."
)


def _same_path(left, right) -> bool:
    try:
        return Path(left).resolve().as_posix().lower() == Path(right).resolve().as_posix().lower()
    except OSError:
        return False


def _existing_package(root: Path):
    """Return an already-imported package whose ``__path__`` points at *root*."""
    for _name, module in list(sys.modules.items()):
        # Some third-party modules (``torch.classes``) expose a non-iterable
        # ``__path__``; skip anything that cannot be listed.
        try:
            paths = list(getattr(module, "__path__", None) or [])
        except Exception:
            continue
        if not paths:
            continue
        for candidate in paths:
            try:
                if _same_path(candidate, root):
                    return module
            except Exception:
                continue
    return None


def _load_package(module_name: str, root: Path):
    init = root / "__init__.py"
    if not init.is_file():
        raise ImportError(f"{root.name}: not a python package (missing __init__.py). {MISSING_HINT}")
    spec = importlib.util.spec_from_file_location(
        module_name, str(init), submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"{root.name}: could not build an import spec. {MISSING_HINT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def extender_package():
    """Return the loaded ``ComfyUI_MiniMax_H3_Extender`` package object."""
    root = CUSTOM_NODES_DIR / EXTENDER_DIR_NAME
    if not root.is_dir():
        raise ImportError(f"{EXTENDER_DIR_NAME} not found under {CUSTOM_NODES_DIR}. {MISSING_HINT}")
    existing = _existing_package(root)
    if existing is not None:
        return existing
    if EXTENDER_DIR_NAME in sys.modules:
        candidate = sys.modules[EXTENDER_DIR_NAME]
        if _same_path(getattr(candidate, "__file__", None) or "", root / "__init__.py"):
            return candidate
    try:
        return importlib.import_module(EXTENDER_MODULE_NAME)
    except ImportError:
        return _load_package(EXTENDER_MODULE_NAME, root)


def selflift_package():
    """Return the loaded ``comfyui-SelfLift`` package object (dash-named dir)."""
    root = CUSTOM_NODES_DIR / SELFLIFT_DIR_NAME
    if not root.is_dir():
        raise ImportError(f"{SELFLIFT_DIR_NAME} not found under {CUSTOM_NODES_DIR}. {MISSING_HINT}")
    existing = _existing_package(root)
    if existing is not None:
        return existing
    return _load_package(SELFLIFT_FALLBACK_MODULE_NAME, root)


def extender_module():
    """Return the sibling ``extender`` module (home of ``_sample_h3``)."""
    package = extender_package()
    try:
        return importlib.import_module(f"{package.__name__}.extender")
    except ImportError:  # package looked up by file: use its recorded submodule
        module = getattr(package, "extender", None)
        if module is None:
            raise ImportError(
                f"{EXTENDER_DIR_NAME}: could not import its extender module. {MISSING_HINT}"
            )
        return module


def selflift_modules() -> SimpleNamespace:
    """Return the SelfLift modules this bridge needs as a namespace."""
    package = selflift_package()
    base = package.__name__
    nodes = importlib.import_module(f"{base}.nodes")
    upscaler = importlib.import_module(f"{base}.h3_upscaler")
    tiling = importlib.import_module(f"{base}.h3_tiling")
    core = importlib.import_module(f"{base}.selflift")
    return SimpleNamespace(
        package=package,
        nodes=nodes,
        upscaler=upscaler,
        tiling=tiling,
        core=core,
        progressive_sample=nodes.progressive_sample,
    )


def list_upscaler_models():
    """Available latent-upscaler checkpoints, ``"none"`` first. Never raises."""
    try:
        return ["none"] + list(selflift_modules().upscaler.list_upscaler_models())
    except Exception:  # missing package / unreadable model dir: fall back
        return ["none"]


def describe_sources() -> str:
    """One-line provenance string used in logs."""
    try:
        ext_version = getattr(extender_package(), "__version__", "unknown")
    except Exception:
        ext_version = "unavailable"
    return (
        f"extender={EXTENDER_DIR_NAME}@{ext_version}, "
        f"selflift={SELFLIFT_DIR_NAME}"
    )


__all__ = [
    "CUSTOM_NODES_DIR",
    "EXTENDER_DIR_NAME",
    "SELFLIFT_DIR_NAME",
    "MISSING_HINT",
    "describe_sources",
    "extender_module",
    "extender_package",
    "list_upscaler_models",
    "selflift_modules",
    "selflift_package",
]
