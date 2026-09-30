# 第三方组件与署名

本仓库**自己的代码**（`*.py`、`tools/`、`tests/` 等）采用 **MIT** 许可，见 `LICENSE`。

本仓库还包含**一个第三方衍生文件**，许可不同，列在下面。

---

## `web/selflift_extender.js`（约 401 KB，Apache-2.0）

| | |
|---|---|
| 来源文件 | `ComfyUI_MiniMax_H3_Extender` 插件包的 `web/extender.js` |
| 来源许可 | **Apache License 2.0**（全文见 [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt)） |
| 生成方式 | 由 [`tools/build_frontend.py`](tools/build_frontend.py) 从来源文件生成，非手工改写 |

### 本仓库对它做了哪些改动

按 Apache-2.0 第 4(b) 条要求，改动明示如下（全部由 `tools/build_frontend.py`
在构建期完成，重跑构建即可复现）：

1. **节点绑定改名**：`const TARGET` 由 `MiniMaxH3Extender` 改为
   `YanhuoH3MotionContextSelfLift`；扩展名由 `MiniMaxH3.Extender` 改为
   `YanhuoH3.SelfLiftUI`。
2. **界面汉化**：工具栏按钮、参考图区、卡片字段、状态徽标、提示文案等改为中文。
3. **面板高度压缩**：重写多处高度预算常量与公式（`UI_MIN_HEIGHT` 等），使节点
   更紧凑；v1.8.1 起卡片行改由 flexbox 分配剩余高度。
4. **新增功能**：草稿视频预览（websocket 推送）、逐段成片预览与播放、
   「连跑全部/停在当前段」按钮、全局 LoRA/种子条、逐段成片导出联动、
   底部横向滑轨的恒定显示修复。
5. **外观皮肤**：额外注入一套 CSS。

生成文件顶部的注释会记录来源文件名与其 sha256，可用于核对具体对应哪个上游版本。

---

## 运行时依赖（**未**随本仓库分发）

下面两个插件包**不在本仓库内**，只在运行时被 `import`。分发本仓库不需要处理它们的许可；
使用本节点则需要自行安装：

- `ComfyUI_MiniMax_H3_Extender` — **Apache-2.0**。
  注意：该包内部含改编自 `NikoDemon80/ComfyUI-H3-Motion-Context`（**GPL-3.0**）的代码，
  见其自带的 `THIRD_PARTY_NOTICE.md`。那部分属于该包自身，与本仓库无关。
- `comfyui-SelfLift` — **上游未提供 LICENSE 文件**，许可不明。

> 本仓库只以只读方式 import 这两个包，不内联、不再分发它们的任何源码，
> 因此本仓库仍可保持 MIT 许可。
