"""逐段输入集合节点（v1.12.0）。

把原来堆在 ``Yanhuo H3 Motion Context SelfLift`` 主节点上的 32 组逐段端口
（``ref_pack_N`` / ``prompt_N`` / ``duration_N`` / ``ref_audio_N_k``）搬到独立的
集合节点 ``YanhuoH3PerClipInputs`` 上，主节点只留一个 ``per_clip_inputs``
聚合端口。

v1.13.0：每片段的 3 个音频插座（``ref_audio_N_0/1/2``）合并成一个
``ref_audios_N``——接一段音频批次进去，``collect()`` 内按批次顺序拆成
``ref_audio_N_0..2`` 再装进 bundle，父类看到的键和以前一模一样。
之所以叫 ``ref_audios_N`` 而不是 ``ref_audio_N``，是因为 ``ref_audio_1..3``
已经被主节点的**全局**独立参考音频占用了，重名会静默串线。

搬家的理由很实际：CLIP N 加得越多，主节点的端口列表越长（32 组 × 6 个 =
192 个插座），拖到最后连 model / clip / vae 三个必填口都找不着。集合节点把
这些"每段一份"的输入收在一起，主节点恢复成一张短表。

**语义完全不变**——这是本模块的硬约束：

* 集合节点只是"连线收集器"，它把收到的值按**端口原名**原样装进一个 bundle
  dict，不做任何换算、排序或压缩。CLIP 5 的 ``prompt_5`` 仍然是 CLIP 5 的
  提示词，不会因为 CLIP 3 没连就被挤到第 3 位（这一点和上游 Prompt Pack
  Bridge 的"列表压缩"语义相反，后者是有序列表，前者是**按编号寻址**）。
* 主节点在 ``extend_with_selflift`` 里把 bundle 摊平回 payload 的
  ``prompt_N`` / ``duration_N`` / ``ref_pack_N`` / ``ref_audio_N_k`` 键，
  然后再交给父类。父类（以及本包已有的缓存失效、全局 LoRA、v1.11.0 编辑态
  提示词这些逻辑）看到的还是原来那套键，一行都不用改。

只有端口的"物理位置"变了，数据流和优先级一条都没动。
"""

from __future__ import annotations

import logging
import re

_LOG = logging.getLogger("yanhuo_h3_selflift")

# 集合节点的输出类型。用自定义类型而不是 *，这样 ComfyUI 会在连错线时直接
# 拒绝，而不是把一坨 dict 塞进 AUDIO 口里。
PER_CLIP_BUNDLE_TYPE = "YANHUO_H3_PER_CLIP"

# 主节点上那唯一一个聚合端口的名字。
PER_CLIP_BUNDLE_INPUT = "per_clip_inputs"

# 与上游 Extender 的 MAX_PER_CLIP_SOCKET_CLIPS / MAX_PER_CLIP_AUDIO_REFS 对齐，
# 改这里之前先确认上游没变（web/selflift_extender.js 里的同名常量也要同步）。
MAX_PER_CLIP_CLIPS = 32
MAX_PER_CLIP_AUDIO_REFS = 3

REF_PACK_TYPE = "H3_REF_PACK"

# 一个逐段端口名的完整正则。四种形态：
#   ref_pack_N / prompt_N / duration_N     —— 每片段一个
#   ref_audios_N                           —— 每片段一个（一段音频批次，v1.13.0）
#   ref_audio_N_k                          —— v1.12.x 的旧形态，仅为兼容保留
# 注意：全局的 ref_audio_1..3（不带第二段编号）**不**属于逐段端口，别误伤。
# 这正是逐段音频批次端口必须叫 ref_audios_N 而不是 ref_audio_N 的原因——
# ref_audio_1 已经被全局「独立参考音频 1」占用了，重名会让 bundle 里的键
# 静默串到主节点的全局端口上。
_PER_CLIP_NAME_RE = re.compile(
    r"^(?:ref_pack|prompt|duration|ref_audios)_(\d+)$|^(ref_audio)_(\d+)_(\d+)$"
)

# 逐段音频批次端口名（v1.13.0：每个片段一个，收一段 1~3 段的音频批次）。
_REF_AUDIOS_NAME_RE = re.compile(r"^ref_audios_(\d+)$")


def is_per_clip_audio_batch_name(name):
    """这个名字是不是一个逐段音频批次端口（ref_audios_N）。"""
    return bool(_REF_AUDIOS_NAME_RE.match(str(name or "")))


def is_per_clip_name(name):
    """这个名字是不是一个逐段端口名（用于 bundle 的收/发两端过滤）。"""
    return bool(_PER_CLIP_NAME_RE.match(str(name or "")))


def per_clip_port_names(clips=MAX_PER_CLIP_CLIPS, audios=MAX_PER_CLIP_AUDIO_REFS):
    """返回集合节点要声明的全部逐段端口名（顺序稳定：按片段、按组内次序）。

    v1.13.0：音频从 3 个 ``ref_audio_N_k`` 合并成 1 个 ``ref_audios_N``
    （收一段音频批次，内部按顺序拆开）。
    """
    names = []
    for n in range(1, int(clips) + 1):
        names.append(f"ref_pack_{n}")
        names.append(f"prompt_{n}")
        names.append(f"duration_{n}")
        names.append(f"ref_audios_{n}")
    return names


def legacy_ref_audio_slot_names(clips=MAX_PER_CLIP_CLIPS, audios=MAX_PER_CLIP_AUDIO_REFS):
    """v1.12.x 的 ``ref_audio_N_k`` 端口名。

    v1.13.0 起合并为 ``ref_audios_N``，这批名字只为两件事保留：
    1. 主节点 INPUT_TYPES 里要把它们 pop 掉（父类还声明着，不 pop 会重新长出来）；
    2. 老工作流里已连在集合节点上的 ``ref_audio_N_k`` 线**照常生效**——
       ``pack_bundle`` 按 ``is_per_clip_name`` 收值，这个正则仍认它们。
    """
    names = []
    for n in range(1, int(clips) + 1):
        for k in range(int(audios)):
            names.append(f"ref_audio_{n}_{k}")
    return names


# ---------------------------------------------------------------------------
# bundle 的收 / 发
# ---------------------------------------------------------------------------
def pack_bundle(kwargs):
    """把集合节点收到的 kwargs 里所有逐段端口值收成一个 bundle dict。

    只收``is_per_clip_name``认可的键，且只收非 None 的值——ComfyUI 不会把
    没连线的 optional 端口放进 kwargs，但连了线、上游却没产出时值是 None，
    那种情况等价于"没连"，交给下游按缺省处理。
    """
    bundle = {}
    for name, value in (kwargs or {}).items():
        if value is None or not is_per_clip_name(name):
            continue
        bundle[str(name)] = value
    return bundle


def unpack_bundle(bundle):
    """把 bundle 摊平成 ``{端口名: 值}``，供主节点合并进 payload。

    返回 ``(mapping, error)``。error 非空表示这个 bundle 不是本节点产出的
    （类型串错、不是 dict、端口名不合法……），调用方应当告警并忽略——宁可
    按"没连集合节点"跑，也不要把脏键塞进 payload 让父类炸掉。
    """
    if bundle is None:
        return {}, None
    if not isinstance(bundle, dict):
        return {}, f"逐段输入端口送来的不是 bundle（收到 {type(bundle).__name__}），已忽略"
    if "__type__" in bundle and bundle.get("__type__") != PER_CLIP_BUNDLE_TYPE:
        return {}, (
            f"逐段输入端口送来的 bundle 类型是 {bundle.get('__type__')!r}，"
            f"本节点只认 {PER_CLIP_BUNDLE_TYPE!r}，已忽略"
        )
    mapping = {}
    skipped = []
    for name, value in bundle.items():
        if name == "__type__":
            continue
        if not is_per_clip_name(name):
            skipped.append(str(name))
            continue
        if value is None:
            continue
        mapping[str(name)] = value
    if skipped:
        _LOG.warning(
            "[Yanhuo SelfLift] 逐段输入集合里出现了无法识别的端口名（%s），已跳过。"
            "端口名必须是 ref_pack_N / prompt_N / duration_N / ref_audios_N。",
            "、".join(skipped[:8]),
        )
    # v1.13.0：ref_audios_N 是一段音频批次，这里同样拆开——万一有手工构造的
    # bundle 没经过 collect()，主节点这一侧也能兜住。
    expand_audio_batches(mapping)
    return mapping, None


def describe_bundle(bundle):
    """给日志用的简短描述：'prompt_1,prompt_2,duration_1'。"""
    if not isinstance(bundle, dict):
        return ""
    return ",".join(sorted(k for k in bundle if k != "__type__"))


def split_audio_refs(value, max_refs=MAX_PER_CLIP_AUDIO_REFS):
    """把一段「音频批次」拆成最多 ``max_refs`` 条单段参考音频。

    接受两种形态（都能在真实工作流里出现）：

    * **AUDIO dict**（ComfyUI 标准）：``{"waveform": Tensor[B, C, T], "sample_rate": int}``。
      ``B > 1`` 就按第 0 维切，第 i 刀给 ``ref_audio_N_(i-1)``——**批次顺序即
      槽位顺序**，第 1 段进槽 0、第 2 段进槽 1、第 3 段进槽 2。
    * **list[AUDIO]**：上游节点用了 ``OUTPUT_IS_LIST`` 时收到的是列表，每个
      元素算一段，同样按顺序对号。

    超过 ``max_refs`` 段时**取前三段并告警**（宁可丢弃也不静默错位）。切出来
    的每段都是独立的 AUDIO dict（``sample_rate`` 共享，波形是切片视图）。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [item for item in value if item is not None]
    elif isinstance(value, dict):
        waveform = value.get("waveform")
        if waveform is None or not hasattr(waveform, "shape"):
            return []
        try:
            count = int(waveform.shape[0])
        except Exception:  # 形状读不出来就当单段处理
            return []
        if count <= 1:
            return [value]
        items = []
        for index in range(count):
            try:
                items.append(dict(value, waveform=waveform[index : index + 1]))
            except Exception:
                return []
    else:
        return []

    if len(items) > max_refs:
        _LOG.warning(
            "[Yanhuo SelfLift] 音频批次有 %d 段，超过每片段上限 %d 段，多余的已丢弃。"
            "需要更多参考音频请拆成多个批次或用回 ref_audio_N_k 端口（老工作流仍支持）。",
            len(items),
            max_refs,
        )
        items = items[:max_refs]
    return items


def expand_audio_batches(mapping, max_refs=MAX_PER_CLIP_AUDIO_REFS):
    """就地把 mapping 里的 ``ref_audios_N`` 拆成 ``ref_audio_N_0..2``。

    bundle 里**不能**留着 ``ref_audios_N`` 这个键——主节点把它摊平进 payload
    后父类不认识这个键，会直接 TypeError。所以拆完必须删掉原键。
    """
    if not isinstance(mapping, dict):
        return mapping
    for name in [k for k in mapping if _REF_AUDIOS_NAME_RE.match(str(k))]:
        match = _REF_AUDIOS_NAME_RE.match(str(name))
        clip = int(match.group(1))
        value = mapping.pop(name)
        for slot, audio in enumerate(split_audio_refs(value, max_refs)):
            mapping[f"ref_audio_{clip}_{slot}"] = audio
    return mapping


def _tooltip_ref_pack(n):
    return (
        f"CLIP {n} 专属参考图包（可用「Yanhuo 参考图打包」节点生成）："
        "图像列表成为该片段自己的 Picture 1..K 参考，不影响其他片段。"
        if n <= 9
        else f"CLIP {n} 专属参考图包（同 ref_pack_1 说明）。"
    )


def _tooltip_prompt(n):
    return f"CLIP {n} 的外部提示词覆盖：连接后替换卡片提示词（空串忽略，不清空卡片）。"


def _tooltip_duration(n):
    return f"CLIP {n} 的外部时长覆盖（秒）：连接后替换卡片 Duration，卡片上显示 (EXT)。"


def _tooltip_ref_audio(n):
    return (
        f"CLIP {n} 专属参考音频（一段音频批次）：批次第 1/2/3 段自动对齐该片段的"
        "参考音频 1/2/3 槽位，只作用于该片段，不影响其他片段。"
    )


class YanhuoH3PerClipInputs:
    """逐段输入集合：把 CLIP N 各自的 ref_pack / prompt / duration / ref_audio 收成一束。

    用法：调「片段数」决定显示几组端口 → 只连需要的那几个 → 输出的
    ``逐段输入`` 接到主节点的 ``per_clip_inputs`` 端口。编号即寻址：
    第 N 组的端口只作用于主节点的第 N 张条件卡，与它前面对不对应无关。
    """

    DESCRIPTION = (
        "把各 CLIP N 条件卡的逐段输入（参考图包 / 提示词 / 时长 / 参考音频）"
        "集中在一处接线，再整束送给 Yanhuo H3 Motion Context SelfLift 的 "
        "per_clip_inputs 端口。第 N 组端口只作用于第 N 张条件卡——编号即寻址，"
        "与前面几组有没有接线无关。不接这个节点 = 各条件卡全部用卡片上自己的值。"
    )
    CATEGORY = "MiniMax H3/SelfLift"
    FUNCTION = "collect"
    RETURN_TYPES = (PER_CLIP_BUNDLE_TYPE, "INT")
    RETURN_NAMES = ("逐段输入", "已接线组数")
    OUTPUT_TOOLTIPS = (
        "整束逐段输入，接到 Yanhuo H3 Motion Context SelfLift 的 per_clip_inputs 端口。",
        "本次实际有接线的片段组数（仅用于核对接线，不参与采样）。",
    )

    @classmethod
    def INPUT_TYPES(cls):
        required = {
            "clip_count": (
                "INT",
                {
                    "default": 1,
                    "min": 1,
                    "max": MAX_PER_CLIP_CLIPS,
                    "step": 1,
                    "tooltip": (
                        "要显示几组逐段端口。一般设成主节点上 CLIP 条件卡的数量；"
                        "只用到前面几段就调小，节点就不会拖出一长串空插座。"
                        "接线数超过这个值时节点会自动把它顶上去，不会弄丢已连的线。"
                    ),
                },
            ),
        }
        optional = {}
        for n in range(1, MAX_PER_CLIP_CLIPS + 1):
            optional[f"ref_pack_{n}"] = (
                REF_PACK_TYPE,
                {"forceInput": True, "tooltip": _tooltip_ref_pack(n)},
            )
            optional[f"prompt_{n}"] = (
                "STRING",
                {"forceInput": True, "tooltip": _tooltip_prompt(n)},
            )
            optional[f"duration_{n}"] = (
                "FLOAT",
                {"forceInput": True, "tooltip": _tooltip_duration(n)},
            )
            # v1.13.0：音频合并成一个批次端口。旧的 ref_audio_N_k 不再声明——
            # 老工作流里已连的线由 LiteGraph 从保存的 JSON 恢复插座，值照旧
            # 送到 kwargs，pack_bundle 的正则仍认它们，所以老线不失效。
            optional[f"ref_audios_{n}"] = (
                "AUDIO",
                {"forceInput": True, "tooltip": _tooltip_ref_audio(n)},
            )
        return {"required": required, "optional": optional}

    def collect(self, clip_count=1, **kwargs):
        bundle = pack_bundle(kwargs)
        # v1.13.0：ref_audios_N 是一段音频批次，按批次顺序拆成 ref_audio_N_0..2。
        # bundle 里不能留 ref_audios_N 这个键——主节点摊平后父类不认识它。
        expand_audio_batches(bundle)
        bundle["__type__"] = PER_CLIP_BUNDLE_TYPE
        groups = set()
        for name in bundle:
            match = _PER_CLIP_NAME_RE.match(name)
            if not match:
                continue
            # 两种形态的捕获组位置不同：ref_audio 的片段号在第 3 组。
            groups.add(int(match.group(1) or match.group(3)))
        return (bundle, len(groups))


NODE_CLASS_MAPPINGS = {
    "YanhuoH3PerClipInputs": YanhuoH3PerClipInputs,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "YanhuoH3PerClipInputs": "Yanhuo H3 逐段输入集合",
}

__all__ = [
    "MAX_PER_CLIP_AUDIO_REFS",
    "MAX_PER_CLIP_CLIPS",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "PER_CLIP_BUNDLE_INPUT",
    "PER_CLIP_BUNDLE_TYPE",
    "REF_PACK_TYPE",
    "YanhuoH3PerClipInputs",
    "describe_bundle",
    "expand_audio_batches",
    "is_per_clip_audio_batch_name",
    "is_per_clip_name",
    "legacy_ref_audio_slot_names",
    "pack_bundle",
    "per_clip_port_names",
    "split_audio_refs",
    "unpack_bundle",
]
