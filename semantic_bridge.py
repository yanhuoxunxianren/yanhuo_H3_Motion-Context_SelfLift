"""MiniMax H3 语义桥（Semantic Bridge）—— v1.6.0 内置于本节点。

**语义桥是什么**（先说清楚原理，再谈实现）：

MiniMax H3 的文本条件由 Qwen3-VL 编出，形状 ``[B, T, 5120]``。语义桥不是 LoRA，
也不改写提示词，而是一个极小的蒸馏 MLP（``5120 -> 512 -> 512 -> 5120``，SiLU，
约 22MB fp32），它学的不是"怎么动"，而是**把 H3 第 49 层的语义表示往"老师模型"
（SenseNova L32/L34）的语义空间拉一点**，用来减少复杂动作里"谁在做什么"的错乱
（动作串人、武器换主、攻受关系混淆、转身/遮挡后人物关系丢失等）。

单次前向的运算：

1. ``h`` = H3 conditioning 张量（原始语义）；
2. ``x = RMS_norm(h)``（逐 token 归一化）——把幅值信息剥离，只让 MLP 看方向；
3. ``p = MLP(x)``；
4. 幅值对齐：``per_token``（按每个 token 把 p 的 RMS 拉回 h 的 RMS）/
   ``global``（整张张量一个标量）/ ``none``（不对齐）；
5. ``hybrid = h + alpha * (p - h)``（在原始与投影之间线性插值，alpha 推荐 0.10~0.15）；
6. 换回原始 device/dtype 输出，CONDITIONING 其余字段原样保留。

所以它的作用位置**只能是 H3 文本编码之后、采样之前**——本节点把它挂在上游
``_make_ref2va_conditioning`` / ``make_fl2va_conditioning`` 之后，逐 CLIP N 生效。

上游包保持只读：只包装 extender 模块里的这两个名字绑定，不改任何文件与语义。
默认关闭（``selflift_bridge_enabled=false``），对已有工作流零影响。
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

import torch
import torch.nn as nn

from . import vendor

_LOG = logging.getLogger("yanhuo_h3_selflift")

# 模型目录：ComfyUI/models/semantic_bridge（与另两个语义桥插件同一处）。
_MODELS_SUBDIR = "semantic_bridge"
_FOLDER_KEY = "yanhuo_semantic_bridge"
_EXPECTED_IN_OUT = 5120

_LOCK = threading.Lock()
_ADAPTER_CACHE = {}
_WARNED = set()


# ---------------------------------------------------------------------------
# 模型发现
# ---------------------------------------------------------------------------
def model_dir() -> str:
    import folder_paths

    return os.path.join(folder_paths.models_dir, _MODELS_SUBDIR)


def _register_folder():
    import folder_paths

    try:
        folder_paths.add_model_folder_path(_FOLDER_KEY, model_dir())
    except Exception:  # pragma: no cover - 旧版本 API 差异
        pass


def list_adapters():
    """列出 models/semantic_bridge 下的 .safetensors（去重、排序）。"""
    names = []
    try:
        import folder_paths

        _register_folder()
        raw = folder_paths.get_filename_list(_FOLDER_KEY) or []
        names.extend(str(x).replace("\\", "/") for x in raw)
    except Exception:  # pragma: no cover - folder_paths 不可用
        pass
    try:
        root = model_dir()
        if os.path.isdir(root):
            for entry in os.listdir(root):
                if entry.lower().endswith(".safetensors"):
                    names.append(entry)
    except OSError:  # pragma: no cover
        pass

    seen = set()
    result = []
    for name in sorted(names, key=lambda x: x.lower()):
        if name.lower().endswith(".safetensors") and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result or ["NO_BRIDGE_MODEL_FOUND.safetensors"]


def _full_adapter_path(name) -> str:
    import folder_paths

    _register_folder()
    try:
        path = folder_paths.get_full_path(_FOLDER_KEY, name)
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    except Exception:  # pragma: no cover
        pass
    fallback = os.path.join(model_dir(), str(name).replace("/", os.sep))
    if os.path.isfile(fallback):
        return os.path.abspath(fallback)
    raise FileNotFoundError(
        "Yanhuo 语义桥：找不到模型文件。\n"
        f"已选：{name}\n期望目录：{model_dir()}"
    )


# ---------------------------------------------------------------------------
# 适配器加载（结构推断 + 缓存）
# ---------------------------------------------------------------------------
class SemanticBridgeMLP(nn.Module):
    def __init__(self, input_dim=5120, hidden_dim=512, output_dim=5120):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim, bias=True)
        self.fc2 = nn.Linear(hidden_dim, hidden_dim, bias=True)
        self.fc3 = nn.Linear(hidden_dim, output_dim, bias=True)
        self.act = nn.SiLU()

    def forward(self, x):
        x = self.act(self.fc1(x))
        x = self.act(self.fc2(x))
        return self.fc3(x)


def _validate_and_infer(weights):
    required = ("fc1.weight", "fc1.bias", "fc2.weight", "fc2.bias", "fc3.weight", "fc3.bias")
    missing = [k for k in required if k not in weights]
    if missing:
        raise RuntimeError(f"语义桥权重缺少张量：{missing}")

    fc1, fc2, fc3 = weights["fc1.weight"], weights["fc2.weight"], weights["fc3.weight"]
    if fc1.ndim != 2 or fc2.ndim != 2 or fc3.ndim != 2:
        raise RuntimeError("语义桥权重必须是二维 Linear 张量。")
    hidden_dim, input_dim = fc1.shape
    if input_dim != _EXPECTED_IN_OUT or fc3.shape[0] != _EXPECTED_IN_OUT:
        raise RuntimeError(
            f"语义桥维度应为 {_EXPECTED_IN_OUT} -> hidden -> hidden -> {_EXPECTED_IN_OUT}，"
            f"实际 {input_dim} -> {hidden_dim} -> {tuple(fc2.shape)} -> {fc3.shape[0]}。"
        )
    if fc2.shape != (hidden_dim, hidden_dim) or fc3.shape[1] != hidden_dim:
        raise RuntimeError(f"语义桥隐藏层维度不一致：{tuple(fc1.shape)} / {tuple(fc2.shape)} / {tuple(fc3.shape)}")
    return int(input_dim), int(hidden_dim), int(fc3.shape[0])


def _compute_device():
    try:
        import comfy.model_management as model_management

        return model_management.get_torch_device()
    except Exception:  # pragma: no cover - 无 ComfyUI 环境时
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_adapter(name, device=None):
    """加载并缓存适配器；返回 (model, meta)。结构维度自动推断（256/512 都支持）。"""
    from safetensors.torch import load_file

    path = _full_adapter_path(name)
    device = device or _compute_device()
    mtime = os.path.getmtime(path)
    key = (path, float(mtime), str(device))

    cached = _ADAPTER_CACHE.get(key)
    if cached is not None:
        return cached

    with _LOCK:
        cached = _ADAPTER_CACHE.get(key)
        if cached is not None:
            return cached
        weights = load_file(path, device="cpu")
        in_dim, hidden, out_dim = _validate_and_infer(weights)
        model = SemanticBridgeMLP(in_dim, hidden, out_dim)
        with torch.no_grad():
            model.fc1.weight.copy_(weights["fc1.weight"].float())
            model.fc1.bias.copy_(weights["fc1.bias"].float())
            model.fc2.weight.copy_(weights["fc2.weight"].float())
            model.fc2.bias.copy_(weights["fc2.bias"].float())
            model.fc3.weight.copy_(weights["fc3.weight"].float())
            model.fc3.bias.copy_(weights["fc3.bias"].float())
        # 这个 MLP 只有几百万参数，放在主设备上用 fp32 计算最稳（回写时再还原 dtype）。
        model = model.to(device=device, dtype=torch.float32)
        model.eval()
        model.requires_grad_(False)
        payload = (model, {"path": path, "hidden_dim": hidden})
        _ADAPTER_CACHE[key] = payload
        _LOG.info(
            "[Yanhuo SelfLift] 语义桥已加载：%s（hidden=%d，%s）",
            name, hidden, device,
        )
        return payload


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
def _rms_normalize(x):
    return x / torch.sqrt(x.pow(2).mean(dim=-1, keepdim=True) + 1e-6)


def _match_per_token(source, target):
    s_rms = torch.sqrt(source.pow(2).mean(dim=-1, keepdim=True) + 1e-8)
    t_rms = torch.sqrt(target.pow(2).mean(dim=-1, keepdim=True) + 1e-8)
    return source * (t_rms / s_rms)


def _match_global(source, target):
    s_rms = torch.sqrt(source.pow(2).mean() + 1e-8)
    t_rms = torch.sqrt(target.pow(2).mean() + 1e-8)
    return source * (t_rms / s_rms)


def apply_bridge(cond, adapter, alpha, magnitude="per_token"):
    """把语义桥作用在 CONDITIONING 上；任何异常都只记日志、原样返回。"""
    if cond is None:
        return cond
    try:
        alpha = float(alpha)
    except (TypeError, ValueError):
        alpha = 0.0
    if alpha <= 0.0:
        return cond
    if magnitude not in ("per_token", "global", "none"):
        magnitude = "per_token"

    try:
        model, _meta = load_adapter(adapter)
    except Exception as exc:
        _warn_once(f"语义桥加载失败（{adapter}）：{exc}")
        return cond

    device = next(model.parameters()).device
    result = []
    for item in cond:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            result.append(item)
            continue
        native, meta = item
        if not torch.is_tensor(native) or native.ndim != 3 or native.shape[-1] != _EXPECTED_IN_OUT:
            _warn_once(
                f"语义桥跳过：期望 H3 conditioning [B,T,{_EXPECTED_IN_OUT}]，"
                f"实际 {tuple(native.shape) if torch.is_tensor(native) else type(native).__name__}。"
            )
            result.append(item)
            continue

        original_device = native.device
        original_dtype = native.dtype
        h = native.to(device=device, dtype=torch.float32)
        with torch.inference_mode():
            projected = model(_rms_normalize(h))
        if magnitude == "per_token":
            projected = _match_per_token(projected, h)
        elif magnitude == "global":
            projected = _match_global(projected, h)
        hybrid = h + alpha * (projected - h)
        hybrid = hybrid.to(device=original_device, dtype=original_dtype)

        new_meta = dict(meta) if isinstance(meta, dict) else {}
        new_meta["yanhuo_h3_semantic_bridge"] = True
        new_meta["yanhuo_h3_semantic_bridge_alpha"] = alpha
        new_meta["yanhuo_h3_semantic_bridge_mode"] = magnitude
        new_meta["yanhuo_h3_semantic_bridge_adapter"] = str(adapter)
        result.append([hybrid, new_meta])

    return result


def _warn_once(message):
    if message in _WARNED:
        return
    _WARNED.add(message)
    _LOG.warning("[Yanhuo SelfLift] %s", message)


def _looks_like_conditioning(value):
    if not isinstance(value, (list, tuple)) or not value:
        return False
    item = value[0]
    return isinstance(item, (list, tuple)) and len(item) == 2 and torch.is_tensor(item[0])


def bridge_signature(enabled, adapter, alpha, magnitude) -> str:
    """开关/模型/强度/对齐方式任一变化都要让缓存失效（结果确实会变）。"""
    if not enabled:
        return ""
    return f"bridge:{adapter}:{round(float(alpha), 4)}:{magnitude}"


# ---------------------------------------------------------------------------
# 挂点：包装上游逐 CLIP 的 conditioning 构建函数
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def installed_bridge(settings):
    """在本节点执行期间生效的语义桥包装（只影响本节点，不碰其它 H3 节点）。"""
    enabled = bool(getattr(settings, "bridge_enabled", False))
    adapter = str(getattr(settings, "bridge_adapter", "") or "")
    alpha = getattr(settings, "bridge_alpha", 0.10)
    magnitude = getattr(settings, "bridge_magnitude", "per_token")

    if not enabled or not adapter or float(alpha or 0.0) <= 0.0:
        yield
        return

    try:
        ext = vendor.extender_module()
    except Exception:  # pragma: no cover
        _LOG.warning("[Yanhuo SelfLift] 语义桥：无法访问 Extender，本次跳过。")
        yield
        return

    targets = [
        name for name in ("_make_ref2va_conditioning", "make_fl2va_conditioning")
        if callable(getattr(ext, name, None))
    ]
    if not targets:
        _LOG.warning("[Yanhuo SelfLift] 语义桥：找不到上游 conditioning 构建函数，本次跳过。")
        yield
        return

    originals = {name: getattr(ext, name) for name in targets}

    def _wrap(name, original):
        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            try:
                if isinstance(result, tuple) and len(result) == 2 and _looks_like_conditioning(result[0]):
                    return (apply_bridge(result[0], adapter, alpha, magnitude), result[1])
                if _looks_like_conditioning(result):
                    return apply_bridge(result, adapter, alpha, magnitude)
            except Exception:
                _LOG.exception("[Yanhuo SelfLift] 语义桥应用失败（%s）——按原始 conditioning 继续。", name)
            return result
        return wrapper

    try:
        for name in targets:
            setattr(ext, name, _wrap(name, originals[name]))
        _LOG.info(
            "[Yanhuo SelfLift] 语义桥已启用：%s（alpha=%.2f，%s），作用于 %s",
            adapter, float(alpha), magnitude, "、".join(targets),
        )
        yield
    finally:
        for name, original in originals.items():
            try:
                setattr(ext, name, original)
            except Exception:  # pragma: no cover
                pass


__all__ = [
    "apply_bridge",
    "bridge_signature",
    "installed_bridge",
    "list_adapters",
    "load_adapter",
]
