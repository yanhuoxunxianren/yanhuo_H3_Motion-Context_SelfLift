# 工作原理图

本插件的 6 张原理图。**同一份内容提供两种形式**：

- **Mermaid**（本文件内，GitHub 原生渲染，可复制进任意支持 Mermaid 的地方）
- **SVG**（[`docs/images/`](images/)，可直接下载、贴进文档/PPT）

| # | 图 | SVG |
|---|---|---|
| ① | 架构与依赖边界 | [`01-architecture.svg`](images/01-architecture.svg) |
| ② | 单段 CLIP 的双分辨率采样管线 | [`02-sampling-pipeline.svg`](images/02-sampling-pipeline.svg) |
| ③ | ComfyUI 工作流接线 | [`03-workflow-wiring.svg`](images/03-workflow-wiring.svg) |
| ④ | 外接 SIGMAS 调度链 | [`04-external-sigmas.svg`](images/04-external-sigmas.svg) |
| ⑤ | 缓存一致性：什么时候会重渲 | [`05-cache-signature.svg`](images/05-cache-signature.svg) |
| ⑥ | 连跑全部与中断续跑循环 | [`06-fullbatch-loop.svg`](images/06-fullbatch-loop.svg) |

SVG 由 [`images/build_svg.py`](images/build_svg.py) 生成，改完重跑即可；
`python images/check_svg.py` 会自检越界、框重叠、连线穿透与文字溢出。

---

## ① 架构与依赖边界

```mermaid
flowchart TD
    CF["ComfyUI 运行时"]
    subgraph PKG["yanhuo_H3_Motion-Context_SelfLift（本包 · MIT）"]
        MAIN["YanhuoH3MotionContextSelfLift<br/>主节点"]
        COMP["配套节点 ×3<br/>参考图打包 / 成片导出 / 视频文件加载"]
        VEN["vendor.py<br/>只读定位，共享同一份 module 实例"]
    end
    EXT["ComfyUI_MiniMax_H3_Extender<br/>CLIP 卡片 UI / 参考图管理<br/>Motion Context 缓存 · Apache-2.0"]
    SL["comfyui-SelfLift<br/>progressive_sample 双采样引擎<br/>上游未提供 LICENSE"]

    CF -->|"注册节点 + 前端扩展"| MAIN
    VEN -->|"import（只读）"| EXT
    VEN -->|"import（只读）"| SL
```

要点：本包**只 import，不修改、不内置、不随包分发**这两个姊妹包。
缺任一个 → 打日志并跳过节点注册，ComfyUI 照常启动。

---

## ② 单段 CLIP 的双分辨率采样管线

```mermaid
flowchart TD
    IN["当前 CLIP 的 latent + 完整 sigma 表"]
    LOW["低分辨率阶段<br/>transition_step 步 Euler<br/>空间缩放到 lowres_scale"]
    X0["预测干净端点 x0"]
    UL["① 直接 latent 提升<br/>nearest / bilinear / 学习型 3D upscaler"]
    UP["② 像素路径<br/>VAE 解码 → 放大 → 重编码"]
    FIX["伪影感知一致性修正<br/>用两路残差定位风险位置<br/>rho / w_min / w_max"]
    NOISE["在过渡 sigma 处重新加噪<br/>沿用表上原有的那一步，NFE 不增加"]
    HIGH["高分辨率阶段<br/>剩余 Euler 步，回到完整 latent 网格"]
    OUT["本段 CLIP 的 latent（回到链路）"]
    TILE["可选：分块 tiling<br/>1~8 块，显存吃紧时开"]

    IN --> LOW --> X0
    X0 -->|"提升（双路并行）"| UL
    X0 --> UP
    UL --> FIX
    UP --> FIX
    FIX -->|"修正量"| NOISE
    NOISE --> HIGH
    HIGH -->|"本段完成"| OUT
    HIGH -.->|"可选"| TILE
```

Motion Context **全程不变**：conditioning 仍带 `minimax_keyframes` 与 `minimax_refs`，
latent 仍是 nested AV latent，音频仍走复用 Euler 边界步 —— 时间连续性完整保留。

关闭 `selflift_enabled` 时整条管线被跳过，等同原 Extender 单阶段采样。

---

## ③ ComfyUI 工作流接线

```mermaid
flowchart LR
    CKPT["Checkpoint 加载器<br/>model / clip / vae"]
    SIG["可选：外接 SIGMAS 链<br/>见 ④"]
    IMGS["图像列表 / Load Image"]
    REFPACK["Yanhuo 参考图打包<br/>images → H3_REF_PACK"]
    MAIN["Yanhuo H3 Motion Context SelfLift<br/>逐 CLIP 采样 + Motion Context 续接"]
    FINAL["Yanhuo 成片导出<br/>(Final Decode)<br/>cache + vae → VIDEO"]
    SAVE["SaveVideo"]
    RELOAD["Yanhuo 视频文件加载<br/>已写出的 mp4 → VIDEO"]
    DOWN["放大 / 插帧"]

    CKPT -->|"model / clip / vae"| MAIN
    SIG -->|"SIGMAS"| MAIN
    IMGS -->|"images"| REFPACK
    REFPACK -->|"ref_pack_N"| MAIN
    MAIN -->|"cache"| FINAL
    FINAL -->|"成片视频"| SAVE
    FINAL -->|"写出的 mp4"| RELOAD
    RELOAD -->|"视频"| DOWN
```

参考图端口只有逐片段的 `ref_pack_N` / `prompt_N` / `ref_audio_N_x`——接哪个片段就只影响哪个片段。
主节点本身不吐视频：`cache` 只是链路缓存，成片必须过 Final Decode。

---

## ④ 外接 SIGMAS 调度链

```mermaid
flowchart LR
    BASE["基本调度器<br/>denoise = 1.0"]
    INTERP["插值扩展 Sigmas"]
    REFINE["H3 Sigma Refiner"]
    PORT["selflift_sigmas<br/>输入端口（非控件）<br/>接管整条 sigma 表"]

    BASE -->|SIGMAS| INTERP
    INTERP -->|SIGMAS| REFINE
    REFINE -->|SIGMAS| PORT

    PORT --> CHECK["进入采样前做全套校验<br/>一维 float、单调不递增<br/>只有末位可为 0、≥2 条<br/>sigmas[transition_step] < 1"]
    PORT --> BYPASS["被旁路：scheduler / steps / denoise<br/>三个控件置灰<br/>显示「外部 SIGMAS 生效」徽章"]
```

`transition_step` 变为**外部表中的切割索引**：低分辨率阶段用表前 `transition_step+1` 个点。

确认是否生效看三处：徽章是否亮 / INFO 日志「采样调度来源」/ 报错里那句 `resolved from ...`。

---

## ⑤ 缓存一致性：什么时候会重渲

```mermaid
flowchart TD
    START["Queue 触发本节点"]
    CALC["计算当前 SelfLift 参数签名<br/>含外接表的内容指纹"]
    CMP["比对 manifest 里的<br/>selflift_plan_signature"]
    HIT["一致 → 百分百复用<br/>正常续跑，不重渲"]
    MISS["不一致 → 清空因果链<br/>_truncate_chain(..., 0)"]
    RERUN["整链重新 Render<br/>日志 SelfLift plan changed<br/>resetting N cached clip(s)"]
    RUN["继续采样当前链路"]

    START --> CALC --> CMP
    CMP -->|一致| HIT
    CMP -->|不一致| MISS
    MISS --> RERUN
    HIT --> RUN
    RERUN --> RUN
```

参数、语义桥、外接表链任一变化都会进签名；关闭语义桥时签名与开启前完全一致，不会白白重渲一次。
三条链路（Ref2VA 运动链 / Ref2VA 独立瑞 / FL2VA）都接了；某条路径解析失败只打 WARNING，不中断生成。

---

## ⑥ 连跑全部与中断续跑循环

```mermaid
flowchart TD
    ON["连跑全部：开<br/>切换上游 run_mode 控件"]
    MODE["run_mode = full_batch"]
    CHK["每段开始：检查中断请求"]
    STOP["写检查点 / 停止<br/>已完成段全保留"]
    SAMPLE["采样当前段"]
    CACHE["立刻落盘缓存<br/>不是全部跑完才写"]
    EXPORT["逐段出片 mp4<br/>output/yanhuo_selflift/<br/>ffmpeg -c:v copy 流复制"]
    PUSH["推送前端预览<br/>websocket → 面板 video"]
    NEXT["下一段 CLIP"]
    DONE["全部完成"]

    ON --> MODE --> CHK
    CHK -->|有中断| STOP
    CHK -->|无中断| SAMPLE
    SAMPLE --> CACHE --> EXPORT
    EXPORT --> PUSH --> NEXT
    NEXT --> CHK
    EXPORT -->|已是最后一段| DONE
```

连跑**不会**自动勾「已校验」——与手工流程一致，不会替你接受未审片的片段。

「停在当前段」= 当前段跑完、检查点落盘后停下，下次 Queue 从断点续跑；
ComfyUI 硬 Interrupt 则是立即中断、当前段作废。
