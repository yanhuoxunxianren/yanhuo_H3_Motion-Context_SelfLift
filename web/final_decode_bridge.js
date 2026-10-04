// Yanhuo 成片导出（Final Decode 中文桥）的独立预览播放器。
//
// 背景：桥节点 YanhuoH3FinalDecodeOutput 复用上游 Final Decode 的 export()，
// 会发出同样的 h3_video / h3_preview_info UI 消息；但上游 live_preview.js 只认
// 上游类名，播放器永远装不到桥节点上，导致用户只能在下游再接一个「保存视频」
// 节点才能看片（多一次完整编码 + 多一个重复文件）。
//
// 本文件是 SelfLift 自研的最小播放器，不复制上游 Extender 的 live_preview.js
// （该上游文件因许可证原因不随本仓库分发）。当上游播放器已经挂在节点上时，
// 本桥会自动让位，绝不出现两个播放器。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const BRIDGE_TARGET = "YanhuoH3FinalDecodeOutput";
const UPSTREAM_WIDGET_NAME = "h3_live_preview";
const BRIDGE_WIDGET_NAME = "yanhuo_final_preview";
const MIN_HEIGHT = 320;

function hasUpstreamPlayer(node) {
    if (node?.__h3LivePreview) return true;
    return (node?.widgets || []).some((w) => w?.name === UPSTREAM_WIDGET_NAME);
}

function mediaUrl(info) {
    const params = new URLSearchParams();
    if (info?.filename) params.set("filename", String(info.filename));
    if (info?.subfolder) params.set("subfolder", String(info.subfolder));
    params.set("type", String(info?.type || "output"));
    return api.apiURL(`/view?${params.toString()}`);
}

function widgetValue(node, name) {
    const widget = (node?.widgets || []).find((w) => w?.name === name);
    return widget?.value;
}

function installPlayer(node) {
    if (!node) return null;
    if (node.__h3BridgePreview) return node.__h3BridgePreview;
    if (hasUpstreamPlayer(node)) return null;

    const box = document.createElement("div");
    box.style.width = "100%";
    box.style.boxSizing = "border-box";
    box.style.display = "flex";
    box.style.flexDirection = "column";
    box.style.gap = "4px";
    box.style.padding = "4px 0 0 0";
    box.style.overflow = "hidden";

    const label = document.createElement("div");
    label.style.fontSize = "10px";
    label.style.lineHeight = "14px";
    label.style.opacity = "0.7";
    label.style.whiteSpace = "nowrap";
    label.style.overflow = "hidden";
    label.style.textOverflow = "ellipsis";
    label.textContent = "成片预览";

    const video = document.createElement("video");
    video.controls = true;
    video.muted = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.style.width = "100%";
    video.style.minHeight = "180px";
    video.style.display = "block";
    video.style.flex = "1 1 auto";
    video.style.background = "#000";
    video.style.borderRadius = "4px";

    box.append(label, video);

    const state = { box, label, video, lastInfo: null };
    node.__h3BridgePreview = state;

    node.addDOMWidget(BRIDGE_WIDGET_NAME, BRIDGE_WIDGET_NAME, box, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => MIN_HEIGHT,
        getHeight: () => MIN_HEIGHT,
    });
    node.graph?.setDirtyCanvas?.(true, true);
    return state;
}

function showPreview(node, message) {
    const info = message?.h3_video?.[0];
    if (!info?.filename) return;
    const state = installPlayer(node);
    // 上游播放器在场 → 让位，由它负责渲染（功能更完整）。
    if (!state) return;

    const meta = message?.h3_preview_info?.[0] || {};
    const clips = Number(meta.total_clips || meta.clip || 0);
    const frames = Number(meta.preview_frames || 0);
    if (clips > 0 || frames > 0) {
        state.label.textContent =
            `成片预览 — ${clips > 0 ? `${clips} 段` : ""}${frames > 0 ? `${clips > 0 ? " / " : ""}${frames} 帧` : ""}`;
    } else {
        state.label.textContent = "成片预览";
    }

    state.lastInfo = { ...info };
    state.video.src = mediaUrl(info);
    state.video.load();
    if (widgetValue(node, "autoplay") !== false) {
        const played = state.video.play();
        if (played?.catch) played.catch(() => {});
    }
}

app.registerExtension({
    name: "YanhuoH3.FinalDecodeBridgePreview",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== BRIDGE_TARGET) return;

        const oldCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = oldCreated ? oldCreated.apply(this, arguments) : undefined;
            // 延后一帧再挂：若上游 live_preview 已经装了播放器，本桥自动让位。
            requestAnimationFrame(() => {
                try {
                    installPlayer(this);
                } catch (_) {
                    /* 预览失败绝不影响生成 */
                }
            });
            return r;
        };

        const oldExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            if (oldExecuted) oldExecuted.apply(this, arguments);
            try {
                showPreview(this, message);
            } catch (_) {
                /* 预览失败绝不影响生成 */
            }
        };
    },
});
