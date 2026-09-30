"""只读诊断：检查 H3 链路缓存的「已校验」状态是否满足 Final Decode 的约束。

用法（在 ComfyUI 的 venv 里跑，或直接系统 python 也行，不需要 torch）：

    python tools/diagnose_chain.py                 # 自动挑最近修改的 chain_*.json
    python tools/diagnose_chain.py <manifest.json> # 指定某个 manifest

只读取、不修改任何文件。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _validated_prefix(segments):
    n = 0
    for desc in segments:
        if not bool((desc or {}).get("validated", False)):
            break
        n += 1
    return n


def find_latest_manifest():
    here = Path(__file__).resolve().parent
    root = here.parent.parent / "ComfyUI_MiniMax_H3_Extender" / "cache"
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("chain_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def main(argv):
    if len(argv) > 1:
        path = Path(argv[1]).expanduser().resolve()
    else:
        path = find_latest_manifest()
        if path is None:
            print("没有找到 chain_*.json，请手动传入 manifest 路径。")
            return 1
    if not path.exists():
        print(f"manifest 不存在：{path}")
        return 1

    manifest = json.loads(path.read_text(encoding="utf-8"))
    segments = list(manifest.get("segments") or [])
    total = len(segments)
    prefix = _validated_prefix(segments)
    flags = [bool((d or {}).get("validated", False)) for d in segments]

    print(f"manifest : {path}")
    print(f"owner    : {manifest.get('owner_id')}   fps={manifest.get('fps')}")
    print(f"片段总数 : {total}    总帧数={manifest.get('final_frame_count')}")
    print(f"已校验前缀: {prefix}")
    print("逐段状态 :")
    for i, (desc, ok) in enumerate(zip(segments, flags)):
        print(
            f"  CLIP {i + 1}: {'已校验' if ok else '未校验'}"
            f"  帧={desc.get('frames')} trim={desc.get('trim_frames')}"
            f"  id={desc.get('clip_id')}"
        )

    print()
    if total == 0:
        print("链路为空：还没渲染过任何片段。")
        return 0
    if prefix >= total:
        print("全部片段已校验 → Final Decode 会直接输出已提交的完整预览缓存。")
        return 0
    if prefix == total - 1:
        print(f"状态正常：未校验的尾部候选只剩 CLIP {total}，Final Decode 可以出片。")
        return 0

    missing = [i + 1 for i, ok in enumerate(flags[: max(total - 1, 0)]) if not ok]
    print("❌ 状态不满足 Final Decode 的约束（clip_by_clip 渐进预览）。")
    print("   约束：『已校验』必须是连续前缀，且只允许最后一个缓存片段未校验。")
    print(f"   当前：共 {total} 段，已校验前缀只有 {prefix} 段，未校验的有 {total - prefix} 段。")
    if missing:
        print(f"   修复：按顺序勾选这些片段的「已校验」→ CLIP " + "、CLIP ".join(str(n) for n in missing))
    print(f"   勾好之后，未校验的应只剩 CLIP {total}（刚渲染出的候选段）。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
