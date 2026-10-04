/**
 * 「Yanhuo H3 逐段输入集合」节点的动态端口（v1.12.0）。
 *
 * 后端一次性声明了 32 组逐段端口（每组 ref_pack / prompt / duration /
 * ref_audios 共 4 个 = 128 个插座），和上游 Extender 的做法一样：先全量
 * 声明让 ComfyUI 承认这些连线，再由前端收拢成"要几组显示几组"。
 *
 * 三条硬规则（碰了就会弄丢用户的线）：
 *   1. **绝不断已连的线** —— 只删没连线的插座。
 *   2. **编号即寻址** —— 第 N 组端口永远对应主节点第 N 张条件卡，绝不因为
 *      前面空着就把后面的往前挤（这一点和上游 Prompt Pack Bridge 的"列表
 *      压缩"语义相反，别照抄那个）。
 *   3. **接线数超过片段数时自动顶高片段数** —— 而不是把多出来的线删掉。
 *
 * 端口顺序按片段分组（ref_pack_N / prompt_N / duration_N / ref_audios_N）。
 *
 * v1.13.0：ref_audio_N_0..2 三个级联插座合并成一个 ref_audios_N（收一段音频
 * 批次，后端按批次顺序拆成该片段的参考音频 1/2/3）。老工作流里连在
 * ref_audio_N_k 上的线照常保留（规则 1），只是不再新增这种插座。
 */

import { app } from "../../scripts/app.js";

const TARGET = "YanhuoH3PerClipInputs";
const MAX_CLIPS = 32;
// v1.12.x 的级联音频插座数，只为清理老工作流的空插座保留。
const LEGACY_MAX_AUDIO = 3;

const COUNT_WIDGET = "clip_count";
const COUNT_LABEL = "片段数";

function groupNames(index) {
    return [`ref_pack_${index}`, `prompt_${index}`, `duration_${index}`, `ref_audios_${index}`];
}

function findSlot(node, name) {
    const inputs = node?.inputs || [];
    for (let slot = 0; slot < inputs.length; slot++) {
        if (String(inputs[slot]?.name || "") === String(name)) return slot;
    }
    return -1;
}

function inputAt(node, name) {
    const slot = findSlot(node, name);
    return slot < 0 ? null : node.inputs[slot];
}

function isConnected(input) {
    return input?.link !== null && input?.link !== undefined;
}

function connectedByName(node, name) {
    return isConnected(inputAt(node, name));
}

function addSocket(node, name, type, tooltip) {
    if (!node || findSlot(node, name) >= 0) return false;
    try {
        node.addInput(name, type, tooltip ? { tooltip } : undefined);
        return true;
    } catch (_) {
        return false;
    }
}

function removeSocket(node, name) {
    const slot = findSlot(node, name);
    if (slot < 0) return false;
    // 规则 1：连着线就绝不删。
    if (isConnected(node.inputs[slot])) return false;
    try {
        node.removeInput(slot);
        return true;
    } catch (_) {
        return false;
    }
}

function countWidget(node) {
    const widgets = Array.isArray(node?.widgets) ? node.widgets : [];
    return widgets.find((widget) => widget && widget.name === COUNT_WIDGET) || null;
}

function readCount(node) {
    const widget = countWidget(node);
    const raw = Number(widget?.value);
    if (!Number.isFinite(raw)) return 1;
    return Math.max(1, Math.min(MAX_CLIPS, Math.round(raw)));
}

function writeCount(node, value) {
    const widget = countWidget(node);
    if (!widget) return false;
    const next = Math.max(1, Math.min(MAX_CLIPS, Math.round(value)));
    if (Number(widget.value) === next) return false;
    widget.value = next;
    // 让 ComfyUI 把新值序列化进工作流（不然刷新页面后片段数会弹回去）。
    if (typeof node.onWidgetChanged === "function") {
        try {
            node.onWidgetChanged(widget.name, next, widget.value, widget);
        } catch (_) {}
    }
    return true;
}

function graphLinkById(graph, id) {
    if (!graph || id === null || id === undefined) return null;
    const links = graph.links;
    if (!links) return null;
    if (typeof links.get === "function") return links.get(id) || null;
    return links[id] || null;
}

/**
 * 把输入插座按"片段分组"重排。
 *
 * addInput() 只能往末尾追加，级连出来的 ref_audio_1_1 会掉到节点最下面，
 * 所以每次增补之后都要重排一次。重排必须同步修正 graph.links 里的
 * target_slot，否则 LiteGraph 画出来的线会连到错误的插座上。
 */
function reorderInputs(node) {
    if (!Array.isArray(node?.inputs) || !node.inputs.length) return false;

    const byName = new Map();
    for (const input of node.inputs) {
        const name = String(input?.name || "");
        if (name && !byName.has(name)) byName.set(name, input);
    }

    const desired = [];
    for (let i = 1; i <= MAX_CLIPS; i++) {
        for (const name of groupNames(i)) {
            const input = byName.get(name);
            if (input) desired.push(input);
        }
    }
    const extras = node.inputs.filter((input) => !desired.includes(input));
    for (const input of extras) desired.push(input);

    const already =
        desired.length === node.inputs.length &&
        desired.every((input, slot) => node.inputs[slot] === input);
    if (already) return false;

    // 先按"当前槽位"记录每条线属于哪个输入对象。
    const linkByInput = new Map();
    for (let slot = 0; slot < node.inputs.length; slot++) {
        const input = node.inputs[slot];
        let link = null;
        if (typeof node.getInputLink === "function") {
            try {
                const resolved = node.getInputLink(slot);
                link =
                    resolved && typeof resolved === "object"
                        ? resolved
                        : graphLinkById(node.graph, resolved);
            } catch (_) {
                link = null;
            }
        }
        if (!link) link = graphLinkById(node.graph, input?.link);
        if (link) linkByInput.set(input, link);
    }

    node.inputs.splice(0, node.inputs.length, ...desired);

    node.inputs.forEach((input, slot) => {
        const link = linkByInput.get(input);
        if (!link) return;
        try {
            link.target_slot = slot;
            if (input) input.link = link.id !== undefined ? link.id : input.link;
        } catch (_) {}
    });

    return true;
}

function syncCollectorInputs(node) {
    if (!node || node.__yanhuoPerClipSyncing) return;
    node.__yanhuoPerClipSyncing = true;
    let changed = false;
    try {
        // 规则 3：接线比片段数多时，把片段数顶上去，而不是删线。
        let highest = 0;
        for (let i = 1; i <= MAX_CLIPS; i++) {
            if (groupNames(i).some((name) => connectedByName(node, name))) highest = i;
        }
        let count = readCount(node);
        if (highest > count) {
            writeCount(node, highest);
            count = highest;
            changed = true;
        }

        for (let i = 1; i <= MAX_CLIPS; i++) {
            const names = groupNames(i);
            if (i > count) {
                // 只删没连线的插座（规则 1）。
                for (const name of names) changed = removeSocket(node, name) || changed;
                continue;
            }
            const specs = [
                ["ref_pack", "H3_REF_PACK", `CLIP ${i} 专属参考图包：图像列表成为该片段自己的 Picture 1..K 参考。`],
                ["prompt", "STRING", `CLIP ${i} 的外部提示词覆盖：连接后替换卡片提示词。`],
                ["duration", "FLOAT", `CLIP ${i} 的外部时长覆盖（秒）：连接后替换卡片 Duration。`],
                [
                    "ref_audios",
                    "AUDIO",
                    `CLIP ${i} 专属参考音频（一段音频批次）：批次第 1/2/3 段自动对齐该片段的参考音频 1/2/3 槽位。`,
                ],
            ];
            for (const [kind, type, tooltip] of specs) {
                changed = addSocket(node, `${kind}_${i}`, type, tooltip) || changed;
            }
            // v1.12.x 的级联音频插座：只清理没连线的（规则 1），连着线的一律保留。
            for (let k = 0; k < LEGACY_MAX_AUDIO; k++) {
                changed = removeSocket(node, `ref_audio_${i}_${k}`) || changed;
            }
        }

        changed = reorderInputs(node) || changed;
        if (changed) {
            try {
                const size = node.computeSize?.();
                const height = Number(size?.[1]);
                const width = Number(node.size?.[0]);
                if (Number.isFinite(height) && height > 0 && Number.isFinite(width) && width > 0) {
                    node.setSize?.([width, height]);
                }
            } catch (_) {}
            node.graph?.setDirtyCanvas(true, true);
        }
    } finally {
        node.__yanhuoPerClipSyncing = false;
    }
}

function deferSync(node) {
    if (!node || node.__yanhuoPerClipQueued) return;
    node.__yanhuoPerClipQueued = true;
    requestAnimationFrame(() => {
        node.__yanhuoPerClipQueued = false;
        syncCollectorInputs(node);
    });
}

app.registerExtension({
    name: "YanhuoH3.PerClipInputs.DynamicInputs",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== TARGET) return;

        const oldCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const node = this;
            const result = oldCreated ? oldCreated.apply(this, arguments) : undefined;
            try {
                const widget = countWidget(node);
                if (widget) {
                    widget.label = COUNT_LABEL;
                    // 片段数是唯一控制显示多少组的开关，改完立刻重排。
                    const previous = widget.callback;
                    widget.callback = function (value) {
                        if (typeof previous === "function") {
                            try {
                                previous.apply(this, arguments);
                            } catch (_) {}
                        }
                        deferSync(node);
                        return undefined;
                    };
                }
            } catch (_) {}
            // ComfyUI 先把 192 个声明出来的插座全建好，这里立刻收拢成"要几组"。
            deferSync(node);
            return result;
        };

        const oldConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = oldConfigure ? oldConfigure.apply(this, arguments) : undefined;
            deferSync(this);
            return result;
        };

        const oldConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const result = oldConnectionsChange
                ? oldConnectionsChange.apply(this, arguments)
                : undefined;
            deferSync(this);
            return result;
        };
    },
});
