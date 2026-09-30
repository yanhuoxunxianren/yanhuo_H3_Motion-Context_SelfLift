# yanhuo_H3_Motion-Context_SelfLift

把 **MiniMax H3 Extender 的 Motion Context 续接能力** 和 **SelfLift 双分辨率采样** 合到同一个节点里。

> 本包**不修改** `ComfyUI_MiniMax_H3_Extender`，也**不修改** `comfyui-SelfLift`。
> 它以只读方式 import 这两个姊妹包，只在"怎么采样"这一层做桥接。

节点名：`YanhuoH3MotionContextSelfLift`
面板里显示：**Yanhuo H3 Motion Context SelfLift**（分类 `MiniMax H3/SelfLift`）

---

## 安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/<你的用户名>/yanhuo_H3_Motion-Context_SelfLift.git
```

重启 ComfyUI。本包不引入额外的 pip 依赖，但**必须**先装好下面两个姊妹包（见下一节）。

---

## 它做了什么

每一段 CLIP（续接卡）不再是"一次性全分辨率采样"，而是：

```
低分辨率阶段  (transition_step 步 Euler，空间缩小到 lowres_scale)
      ↓  预测干净端点 x0
一致提升      双路：直接 latent 提升 (nearest / bilinear / 学习型 3D upscaler)
              vs 像素 VAE 解码→放大→重编码
      ↓  用两路残差做「伪影感知一致性修正」(rho, w_min, w_max)，只修最高风险位置
      ↓  在过渡 sigma 处重新加噪（不额外增加 NFE）
高分辨率阶段  (剩余 Euler 步，回到完整 latent 网格；可选空间分块 tiling)
```

**没有变的**：conditioning 里依然带 `minimax_keyframes`（前置尾帧锚点）和 `minimax_refs`，
latent 依然是 Extender 的 nested AV latent，音频流依然走复用 Euler 边界步。
—— 也就是说 **Motion Context 的时间连续性完整保留**。

---

## 依赖（必须都在 `custom_nodes/` 下）

| 包 | 提供什么 |
|---|---|
| `ComfyUI_MiniMax_H3_Extender` | CLIP 卡片 UI、参考图管理、Motion Context 因果缓存、Disk Join、项目存档 |
| `comfyui-SelfLift` | `progressive_sample` 双采样引擎、学习型 H3 latent upscaler、高分辨率分块 |

缺任一个，本包启动时会打日志并**跳过节点注册**（不会让 ComfyUI 起不来）。

---

## 控件说明

在继承下来的 Extender 控件之外，追加了这些（位置在节点原生控件之后、自定义卡片面板之前）：

| 控件 | 默认 | 说明 |
|---|---|---|
| `selflift_enabled` | `true` | 关闭即完全等同原 Extender 单阶段采样 |
| `selflift_transition_step` | `2` | 低分辨率阶段步数，必须 `< steps`（合法 `1..steps-1`） |
| `selflift_lowres_scale` | `0.5` | 低分辨率空间缩放（论文 0.5），范围 0.25–1.0 |
| `selflift_rho` | `0.6` | 修正覆盖的最高风险位置比例；`0` 表示完全不用像素 VAE 锚点 |
| `selflift_w_min` / `selflift_w_max` | `1.0 / 1.0` | 修正强度下限/上限 |
| `selflift_latent_upsample` | `nearest` | 直接 latent 提升的插值方式 |
| `selflift_upscaler_model` | `none` | 外部学习型 H3 latent upscaler（`models/latent_upscale_models`）；选中后请把它当主提升路径，`rho` 设为 `0` |
| `selflift_upscaler_unload` | `true` | 提升完立刻把 upscaler 卸出显存（不影响结果） |
| `selflift_highres_tiling` | `false` | 高分辨率阶段空间自动分块（1–8 块），显存吃紧时开 |

### 外接 SIGMAS 调度（`selflift_sigmas` 输入端口，v1.1.0）

`selflift_sigmas` 是一个可选的 SIGMAS 输入端口（不是控件）。**未连接时一切照旧**；连接后整条
sigma 调度表由外部接管，典型接法：

```
[基本调度器] SIGMAS → [插值扩展Sigmas] → [H3 Sigma Refiner] → selflift_sigmas
```

接管后的行为与约束：

- `scheduler` / `steps` / `denoise` 控件被完全旁路（全局条右侧显示紫色徽章「外部 SIGMAS 生效」，
  三个控件同时变灰）。denoise 裁剪必须在上游链完成（基本调度器 denoise=1.0 即可）。
- `transition_step` 语义变为**外部表中的切割索引**：低分辨率阶段用表前 `transition_step+1` 个点，
  其余走高分辨率。插入插值/精修步后要按新表重设。
- 进入采样前会做全套校验，不合法直接在 Queue 前报错：一维 float、单调不递增、**只有末位可为 0**、
  至少 2 个条目、`sigmas[transition_step] < 1`。
- σ[0] 请保持 1.0；σ[0]<1 等于部分去噪，续接段首帧会欠噪。
- 低分辨率阶段吃的是表前缀，**别把前段排得太密**（前段过密 = 低分辨率阶段烧掉过多步）。
- 外部表的内容指纹（长度 + SHA-256）计入 `selflift_plan_signature`：改链不改参数也会正确触发整链重渲。
- 兼容性：H3 的 PDD 头部按 `sample_sigmas` 最近邻选头，音频 Euler 步也是查表走，外接表天然兼容；
  采样器仍必须是 Euler + `s_churn=0`。

### `transition_step` 越界：现在自动收拢，不再中断（v1.3.1）

```
ValueError: SelfLift: transition_step 7 is out of range for 4 steps
(resolved from `steps`=4 with scheduler=simple, denoise=1.0); valid range is 1..3.
```

**不再中断运行**：`transition_step` 是一个"切分下标"，超出时唯一合理的取值是最后一个可用下标，
所以现在直接**钳制 + 打 WARNING**（例如 4 步时 7 → 3，即 3 步低分辨率 + 1 步高分辨率），
模型已经加载完却被拦下的情况没有了。仍然会硬报错的是那些 SelfLift 猜不出来的配置：
`steps < 2`、模型不是 rectified-flow、采样器不是 Euler、外接 sigma 表本身非法。

钳制发生在**计算缓存签名之前**，所以之后把 `steps` 提高到让原值合法时，签名会跟着变、缓存会失效
（否则会出现"有效方案变了但签名不变、吃到旧缓存"的坑）。

**怎么算**：`有效步数 = 表条目数 - 1`（外接时）或 `steps` 控件值；`高分辨率步数 = 有效步数 - transition_step`。
想让高分辨率阶段拿到 3 步、外接表 10 条（9 步），就把 `transition_step` 设成 6。

**⚠️ 报错里那句 `resolved from ...` 是判断依据**——它说明这次步数是哪来的：

- `resolved from \`steps\`=N with scheduler=..., denoise=...` → **走的是控件路径，外接表没生效**。
  检查 H3 Sigma Refiner 的输出是否真连到了 `selflift_sigmas` 输入、且非空（空表会在日志里留
  "resolved to an empty schedule" 的 WARNING）。
- `resolved from the external \`selflift_sigmas\` table (N entries -> M steps)` → 外接生效，
  步数由表决定（条目数 - 1），与 `steps` 控件无关。

### v1.3.5：「🔗 连跑全部」——把上游原生的 full_batch 提到工具栏

上游 `ComfyUI_MiniMax_H3_Extender` 自己就带 `run_mode` 控件（`clip_by_clip` / `full_batch`，
见 `extender.py:4134`），而 `full_batch` **正是**「一次 Queue 依次生成全部片段」：

- 每段开始/结束都检查中断请求；
- 每段采样完立刻落盘缓存（不是全部跑完才写），随时停下都不丢；
- Interrupt 会写可续跑的检查点，下次 Queue 从断点继续。

所以本版**不再另起一套排队链路**——v1.3.4 那个「自动勾已校验 + 自动续排」的实现已回滚，
它会替你接受未审片的片段、还会劫持 Queue，等于改了整个流程语义。现在只做三件事：

1. 工具栏「载入项目」后面加 **「🔗 连跑全部：开/关」**，一键切换 `run_mode` 控件的值；
2. 旁边常驻 **「⏹ 停在当前段」**，直接调上游 `requestFullBatchInterrupt()`
   （上游自带的 `Interrupt` 按钮只在运行期间才出现，平时看不见，所以补一个常驻的）；
3. 按钮提示里给出当前**未完成片段数**，并写明"连跑不会自动勾已校验"。

切换只改控件值 + 走上游自己的 `updateHidden` / `captureNativeWorkflowState` / `render`
提交路径，**不碰排队、不碰校验、不改 `onExecuted`**，流程语义与手工操作完全一致。

**打断**：点「⏹ 停在当前段」→ 当前片段跑完、检查点落盘后停下，已完成片段全部保留，
下次 Queue 从断点续跑。ComfyUI 的硬 Interrupt 也能用，但那是立即中断，当前段作废。

**注意**：连跑**不会**自动勾「已校验」——与手工流程一致，不会替你接受未审片的片段。
全部跑完后用 Final Decode 成片检查，不满意的单卡取消「已校验」重跑。

### v1.4.0：内置「草稿视频预览」——节点面板直接看当前 CLIP 的采样过程

不用再外挂 Model Preview Override，也不用手动改 `preview_frames`。开启 SelfLift 后，
**每采样一步**都会把这一步的 x0 解成一张动画草稿，显示在节点面板工具栏下方，并标注
`CLIP N · 步 x/y · 帧数 @ fps · 解码器`。

**帧数自动对齐当前 CLIP**：预览帧数不是固定值，而是由当前 latent 的 token 数按 H3 的
时间布局 `(1,4,4,4,4)`（5 个 token = 17 帧）展开得到 —— 10s 片段 72 token → **243 帧**，
7s 片段 52 token → **175 帧**，与 H3 的时长公式
`max(5, round(a*24)) + (5 - max(5, round(a*24)) % 17) % 17` 完全一致。
所以 clip_by_clip 逐段跑、每段时长不同时，预览长度自动跟着变。

**两档解码器，自动选择**：

| 解码器 | 来源 | 说明 |
|---|---|---|
| `taeh3` | `models/vae_approx/taeh3.safetensors`（9.8MB 微型 VAE） | 接近真解，分块解码控制显存 |
| `Latent2RGB` | `latent_formats.MiniMaxH3Video.latent_rgb_factors` | 线性近似，零模型零显存，永远可用 |

**实现要点**（为什么不外挂）：核心预览器只取第一个时间 token 出一张静图
（`latent_preview.py:76` `x0[0, :, 0]`）；我们在 SelfLift 的
`latent_preview.prepare_callback` 上接一根旁路，把**全部时间 token**解出来，
其余链路（进度条、核心静图、采样本身）完全不动。原回调照常执行，
预览任何异常都静默 —— 绝不影响出片。

无新增控件（FPS 24、显示宽 512、编码上限 480 帧均为默认）。

### v1.8.1：滑轨恒定改为 flexbox 分配（v1.8.0 全线遮挡的回归修复）

**回归现象**：v1.8.0 上线后，滑轨反而**在任何宽度下都不显示**（比 v1.7.0 更糟），
同时草稿预览明显比成片预览小一圈。

**根因**：v1.8.0 的「精确分配」有个致命前提错误 —— 它先让浏览器把 cards 之外的兄弟
按 flex 默认 `flex-shrink:1` **压扁**，再拿「压扁后的高度」去反推 cards 该多高。
两者互相打架：总和明明不变，却每帧都在用错误的分母重算，于是缩到一定程度后 cards
被推到远在 root 底边之外（测试台复现的最坏情形是 **溢出 138px**，整行连预览一起被
`overflow:hidden` 吃掉，正是用户截图里"滑轨彻底不见"的样子）。根本原因仍是老毛病：
**像素估算永远追不上真实 DOM**。

**修法（彻底改用浏览器自己算）**：

1. **不再估算**。参照上游自己在 Nodes 2.0 分支 `applyNodes2TimelineHeight()` 里的成熟
   写法，让 flexbox 分配剩余空间：`cards` → `height:auto + flex:1 1 0 + min-height`，
   **cards 之外所有兄弟 → `flexShrink:0`**（按自然高度排布，不许被压扁）。这样 cards
   恒等于「容器剩余空间」这一个值，不多不少，底边一定落在内容框底边上 —— 工具栏换行、
   全局条、预览行高度怎么变都不影响结论。
2. **面板底部固定留白** `YANHUO_BOTTOM_GUTTER = 14px`（用户要的"底部边界固定值"）：
   滑轨下沿不再贴着节点底边被切最后几像素。同时按用户建议把 `BOTTOM_SAFE_PX`
   从 20 **翻倍到 40**（此时它只影响 UI 最小高度，不会压缩卡片）。
3. **溢出兜底** `yanhuoFitPanel()`：万一连 cards 的 min-height 都放不下（最小节点 +
   预览行展开，比如窄到两个预览换行堆叠），按**子元素实测底边**同步长高面板与节点。
   有界防御：单轮 ≤900px、累计 ≤1500px、最多 6 轮；用户正在拖节点时不动手，松手后再补。

**顺带修掉一个致命的 build 期 bug**：注入块是写在 Python 字符串里的大段 JS，其中误用了
`str(YANHUO_PREVIEW_H)` —— 它**原样落进了生成文件**，而 `node --check` 和「加载模块」都
发现不了（`str(x)` 语法上是合法调用），只有节点真去渲染预览时才 `ReferenceError: str is
not defined`，届时整个工具栏注入已经挂了。现在 `tools/build_frontend.py` 在写盘前会扫一遍
Python 残留（`str(` / 未替换的占位符 / `= None|True|False`）并拒绝输出，新增
`tests/test_build_guard.py`（10 项）守住这道闸门不得被绕过。

**草稿/成片预览对齐**：两者改用同一套尺寸常数（`flex` 基准 300px、`min-width` 200px、
媒体元素固定 200px 高 + `object-fit:contain`）。相同基准 + 相同 grow ⇒ 并排时**等宽**，
固定像素高 ⇒ 不同长宽比也**等高**。

**验证**：headless 测试台复刻真实面板结构 —— toolbar `display:flex` 不换行、按钮里的中文
被挤压换行后靠 `min-height:auto` **拒绝收缩**（这是 v1.8.0 估算失效的关键行为）。判据不再是
只看 cards，而是**每一个**可见子元素都不能越过 root 内容框底边。12 个场景
（宽 340~1400 × 3~6 张卡片 × 草稿/成片组合 × 最小尺寸 × 拉伸 +600）**12/12 PASS**，
旧逻辑同批 **0/12 FAIL**（溢出 5~138px）—— bench 具备鉴别力。

### v1.8.0：预览并排 + 滑轨实测重排（估算路线终结，已被 v1.8.1 取代）

**预览并排**：草稿预览与成片预览放进同一个横向行容器（`data-yanhuo-preview-row`），
左边草稿、右边成片；节点太窄放不下时 `flex-wrap` 自动换回上下堆叠。两个预览各自
独立显示/隐藏，行高取两者较大值，高度计入 `yanhuoPreviewRowExtra()`。

**滑轨实测重排**（v1.7.0 补偿失效的真正修复）：用 headless 浏览器连真实工作流测量发现，
v1.7.0 的纵向裁差恒为 0（`cards.scrollHeight == clientHeight`）——滑轨是**横向滚动条**，
被裁的机制是 **cards 容器底边超出 root 底边**（root overflow hidden 裁掉滑轨）。超出量
实测 2~42px，来自高度公式与真实 DOM 的偏差：

| 来源 | 公式预算 | 真实 DOM |
|---|---|---|
| 工具栏 | 常数 55 | **61~138px**（随宽度换行）+ marginBottom 7 |
| 全局条 | 152（offsetHeight） | + marginTop 2 + marginBottom 4 |
| REF 区 | 160 + 7 | 一致 |

节点越高（available 充足）这些漏项越显形 —— 这解释了"压缩时滑轨被裁、拉宽才出现"。

修法：放弃估算，在 legacy sync 末尾**无条件精确分配** ——
`cards 高度 = root 可视高 − 兄弟节点实测占用（含 margin）`；若卡片内容仍被纵向裁切，
再把 root/cards/节点三者同步长高（收敛：两轮内稳定，只长高不缩短，单次上限 800px）。
此机制同时覆盖 toolbar 换行、margin 漏项、预览行高度等一切估算误差，滑轨永远贴着
root 底边内侧 —— 即"底部边界恒定保留滑轨高度"。

验证：headless 测试台复刻面板 DOM（box-sizing 与上游一致），7 个场景
（宽 480~900 × 工具栏 61~138 × 卡片 580~700 × 预览显示组合 × 压缩到最小）
全部满足 `cards 底边 ≤ root 底边 + 1px` 且无纵向裁切。

### v1.7.0：逐段成片实时预览 + 底部滑轨恒定

**逐段成片实时预览**（连跑全部时边跑边看）：每采样完一段 CLIP、逐段成片导出完成的瞬间，
节点面板里直接出现一个 `<video>` 播放条（草稿预览框下方），自动加载并播放最新一段
成片，随后继续下一段采样 —— 不再需要等整个节点跑完、经过「缓存→成片导出→保存视频」
才看到画面。

实现与取舍：

- **为什么不加输出端口 / 不走成片导出节点**：ComfyUI 的执行模型是「节点 return 之后
  输出端口才有数据，下游才开始执行」。本节点要等所有 CLIP 采样完才 return，所以任何
  端口方案（哪怕是新加的"及时视频端口"）都只能在**全部结束后**把最后一段交给下游 ——
  与"每段完成立刻播放"在模型上不相容。中途播放只有 websocket 推送一条路。
- 走的是和草稿预览完全相同的基础设施：后端 `segment_export.notify_final_clip()` 在
  每段 mux 完成后 `send_sync("yanhuo_h3_final_clip", …)`；前端收事件后把
  `<video src="/view?filename=…&subfolder=yanhuo_selflift&type=output">` 换源播放。
  `/view` 是 ComfyUI 内置端点，**零自定义 HTTP 路由**；推送/播放任何异常都不影响采样。
- 预览条带控件（可暂停/拖进度）+「⬇ 下载本段」直链；高度走 `yanhuoFinalExtra()`
  进同一张 HEIGHT_REWRITES 改写表（v1.4.2 确立的规则），显示时长高、收起时归零。

**底部滑轨恒定（裁差自动补偿）**：v1.6.1 的 20px 余量在「压到最矮 + 很窄」时仍可能
不够（卡片因文本换行变高，内容被底边裁掉，滑轨只剩一条线；拉宽后工具栏回到单行、
卡片变矮，滑轨才露出来）。v1.7.0 在 legacy 同步末尾加了**裁差测量**：
`cards.scrollHeight - cards.clientHeight > 2` 即认为滑轨/内容被裁，把 root、cards、
节点三者同步长高裁差值 —— 等效于「底部边界恒定保留滑轨高度」，不再随压缩/拉伸变化。
只长高不缩短，单次上限 800px 防御异常循环。

### v1.6.1：底部滑轨安全余量（BOTTOM_SAFE_PX = 20）

用户实测：加载草稿预览后，卡片行底部的横向滑轨仍会被节点底边裁掉一小截
（LoRA 区最后一行 + 滑轨只露出一半）。

原因：三处高度公式里 `REF_SECTION_HEIGHT`(160) / `yanhuoStripExtra` / `yanhuoDraftExtra`
都是**估算值**，与真实 DOM 之间存在零星偏差（预览框 margin、参考行实际渲染高度、
LoRA 编辑区行高等）。单项都不大，但方向一致，累积起来正好吞掉滑轨需要的
`CARD_SCROLLBAR_SPACE`(24) 里的一部分。

修法：新增 `BOTTOM_SAFE_PX = 20`，**同时**写进四处公式的两侧 ——
下限侧（`uiMinHeightForState` / 窄节点补偿 `__yanhuoNeed`）加上它，节点最小
高度变大，`forceMin` 自动把节点长高 20px；扣除侧（`syncDomHeight` 卡片行 /
初始卡片行）同样扣除它，节点长高后 `available` 同步变大，**卡片行高度不变**。
净效果：节点整体高 20px，滑轨完整可见；拉伸行为与之前完全一致。

### v1.6.0：内置语义桥（Semantic Bridge）

**语义桥是什么**：不是 LoRA，也不改写提示词。它是一个极小的蒸馏 MLP
（`5120 → 512 → 512 → 5120`，SiLU，约 22MB fp32），作用是**把 H3 第 49 层的语义表示往
"老师模型"（SenseNova L32/L34）的语义空间拉一点**，用于减少复杂动作里 H3 的语义错乱：
动作串人、武器/道具换主人、攻受关系混淆、转身/换位/遮挡后人物关系丢失、后续动作从错误的
前序状态继续。

单次前向（本节点的实现，与两个语义桥插件的算法一致）：

```text
h = H3 conditioning [B, T, 5120]        # Qwen3-VL 编码后的文本条件
x = RMS_norm(h)                         # 逐 token 归一化，只留方向
p = MLP(x)
p = 幅值对齐(p, h)                       # per_token（官方推荐）/ global / none
hybrid = h + alpha * (p - h)            # alpha 推荐 0.10~0.15
```

**它必须作用在 H3 文本编码之后、采样之前**。本节点把它挂在上游逐 CLIP 的 conditioning
构建函数（`_make_ref2va_conditioning` / `make_fl2va_conditioning`）之后，因此**每个 CLIP N
都会自动生效**，不需要在工作流里再插一个外挂节点，也不必重连 CONDITIONING 线。

使用（4 个新控件，位于节点 SelfLift 参数末尾）：

| 控件 | 说明 |
|---|---|
| `selflift_bridge_enabled` | 语义桥开关，**默认关闭**（对既有工作流零影响） |
| `selflift_bridge_adapter` | 模型文件，读取 `ComfyUI/models/semantic_bridge`，本地已识别 4 个 |
| `selflift_bridge_alpha` | 混合强度，默认 **0.10**（模型元数据建议 0.10~0.15） |
| `selflift_bridge_magnitude` | `per_token`（默认）/ `global` / `none` |

* 模型目录就是 `D:\comfyui012\ComfyUI\ComfyUI\models\semantic_bridge`，与 BUNNY /
  MiniMax_H3_Semantic_Bridge 两个插件共用，无需重复放置；
* 维度自动推断（本地 4 个权重都是 5120-512-512-5120），fp32 计算后回写原始 dtype/device，
  模型带缓存、只占几十 MB；
* 开启后**改模型/强度/对齐方式会自动让受影响片段重渲**（进了缓存签名）；关闭时签名与升级前
  完全一致，不会因为升级白白重渲一次；
* 只在 REF2VA / FL2VA 的 conditioning 构建处生效；任何加载或应用异常只记日志，按原始
  conditioning 继续，绝不打断出片。

### v1.5.0：连跑全部逐段出片 + 窄节点滑轨补偿

**逐段出片**（新增 `segment_export.py`）：开启「🔗 连跑全部」后，每采样完一个 CLIP N，
立即把当前链路（第 1..N 段）拼接输出为一个 mp4（`output/yanhuo_selflift/<节点id>_clip_NN_of_TT.mp4`，
重跑覆盖），然后继续后面的片段——输出与采样串行，直到最后一段输出完为止。

* 零重复解码：上游 full_batch 每段采样后本就做逐段 VAE 解码缓存（最终规格 sidecar + PCM 音轨），
  逐段出片只做 ffmpeg 流复制拼接（`-c:v copy`），每段额外开销秒级；
* 导出规格自动沿用工作流里成片导出（Final Decode）节点的 codec/crf/preset，与最终成片一致；
  读不到成片导出参数时跳过并记日志；
* 仅 REF2VA 运动链模式生效（FL2VA / 独立片段模式自动跳过）；工具栏状态行会提示
  「已输出 N/总 段视频 → 文件名」；任何导出异常只记日志、绝不打断采样；
* 上游包保持只读：仅包装 `extender.cache_full_batch_ref2va_segment`（full_batch 分支专用
  调用点），中断续跑的检查点重放路径同样会补出当前前缀视频。

**窄节点滑轨补偿**（遗留问题修复）：工具栏按钮在窄节点上会换行堆叠，实际高度远超高度
预算写死的 55px，下方内容被整体顶出 `overflow:hidden` 的面板——底部横向滑轨正好被裁掉
（所以此前只有把节点拉宽、工具栏回到单行后滑轨才出现）。现在 legacy 高度同步末尾按
**实测工具栏高度**补足面板与节点高度（只长高、不缩短），最小尺寸下卡片与滑轨完整可见。

### v1.4.2：v1.4.1 修复无效且引入新溢出——高度预算改回 build 期改写表

实测反馈：草稿预览运行时卡片仍被吞掉下半截、滑轨消失，且**拉伸节点大小都救不回来**。

根因（复盘 v1.4.1 的错误修法）：v1.4.1 在**运行时**包装 `uiMinHeightForState` /
`syncDomHeight` 并二次覆盖卡片行高，但覆盖公式漏掉了全局条高度（`yanhuoStripExtra`，
34~152px），把上游已正确的值改错了——卡片行恒比面板高出「全局条 + 7px」。这是**常数
溢出**，不随节点尺寸变化，所以无论怎么拉伸都无效；开全局 LoRA 编辑区时（+118px）最严重。

修复（回到 v1.3.0 全局条验证过的机制）：

1. 废弃两个运行时包装，改为在 build 期的 `HEIGHT_REWRITES` 里把 `yanhuoDraftExtra()`
   写进上游三处高度公式（`uiMinHeightForState` / `syncDomHeight` 卡片行 / 卡片行初始高度）；
2. 三处公式同时补上参考区 `marginBottom` 的 7px——此前最小高度下卡片行底部被裁 7px，
   位于卡片行底部 24px 内的横向滑轨因此「压缩到最小尺寸就看不见」；
3. 草稿图首次显示 / 加载完成触发 `syncDomHeight(…, forceMin=true)`，节点高度不够时
   自动长高（与上游调用惯例一致），而不是把内容顶出节点边界。

### v1.4.1：修复草稿预览把 CLIP 卡片挤出可视区（截断 + 滑轨消失）

v1.4.0 的草稿预览框插在工具栏和参考图区之间，占了一行真实高度，但上游的最小高度
公式只算了 `工具栏(55) + 参考图区(REF_SECTION_HEIGHT) + 卡片高`。后果是整条卡片行
被顶出可视区，表现为两个症状（同一个根因）：

1. CLIP 卡片下半段被截断，只显示到 Lora 2 一带，下面的参数够不着；
2. 卡片行的**横向滑轨位于卡片行底部**，跟着一起被挤出屏外——看起来就是"滑轨没了"。

修复：把草稿框的实际像素高度并入高度预算（`uiMinHeightForState` 包一层，legacy 与
Nodes 2.0 两条路径同时生效），并在 legacy 路径的卡片行高度里减掉同一份高度；草稿框
首次显示、以及每张草稿图加载完成（不同 CLIP 分辨率不同、高度会变）都触发一次重排。
草稿框隐藏时高度预算自动回到原值。

### v1.3.6：Final Decode 报「progressive preview expects one unvalidated tail candidate」的可读化

这是**链路状态问题，不是采样问题**。上游 clip_by_clip 的渐进预览有一条硬约束
（`motion_context_disk.py:4003`）：

```
_validated_prefix_count(segments) == len(segments) - 1
```

也就是**「已校验」必须是连续前缀，且只允许最后一个已缓存片段未校验**（它就是本次要接上去的新片段）。
不满足就直接 `RuntimeError`，原文案完全不告诉你现在有几段、哪几段没勾。

现在成片导出节点只认这一条异常，把它换成中文可读的诊断：段数、已校验前缀、
逐段状态、需要补勾哪几段。其余异常原样抛出，不吞错、不改流程语义。

**随时自查（只读，不写任何文件）**：

```bash
python tools/diagnose_chain.py                  # 自动挑最近改动的 chain_*.json
python tools/diagnose_chain.py <manifest.json>  # 或指定 manifest
```

**典型触发方式**：

1. 重跑第 N 段 → N 之后所有片段的缓存被作废（Motion Context 失效），缓存段数从多变少；
2. 取消中间某段的「已校验」→ 其后所有片段连带作废，「已校验」不再连续；
3. 片段渲染期间在面板上改动了「已校验」。

**修复**：按顺序把前缀里缺的那些片段重新勾上「已校验」，使未校验的只剩最后一段，再 Queue。

### v1.3.3：种子行为选项 + 「⏮ 回溯上次种子」+「跟随上一段Lora」换位

**全局条**（开启「全局种子」后）：

- 种子输入框后新增**下个种子行为**下拉：**固定 / 下个 +1 / 下个 −1 / 下个随机**——本次运行结束后
  全局种子按选项变化（固定=保持 v1.3.0 行为，默认）。每次运行结束（onExecuted）先记快照再推进。
- 再往右是 **「⏮ 回溯」**：把全局种子替换为**上次运行实际使用的种子**。随机跑出好结果后点一下即可复现。
  数据来源：后端返回的 `clips_json` 是全局覆盖后的**生效值**，所以快照就是真实用于生成的种子。

**每张 CLIP 卡片**：

- 种子骰子后面的按钮换成 **「⏮」**：把该片段上次运行实际使用的种子填回本卡片
  （走主脚本自己的 change 链路，写回 clip.seed 并自动重渲染）。全局种子开启时禁用（卡片种子被忽略）。
- 原「⧉ 上一段」改名 **「跟随上一段Lora」**，并从种子行移到**提示词框与 LoRA 1 之间**，
  功能不变（复制上一片段的 LoRA 选择与强度，CLIP 1 禁用；全局 LoRA 开启时禁用）。

注意：回溯快照存在节点运行时（不写盘），重载工作流后要再跑一次才会有"上次记录"。

### v1.3.2 修复：外接表曾被引擎丢弃（"徽标亮着却走控件"的真因）

v1.3.0/1.3.1 里 `split_settings()` 把 `selflift_sigmas` 和控件一起剥走，交给了只认控件标量的
`SelfLiftSettings.from_kwargs`——**张量在那里被静默丢弃**。结果：面板徽标亮着（前端只看连线），
后端拿到的永远是 `None`，于是回落到节点自己的 `steps` 控件，报错里就会出现那句
`resolved from \`steps\`=4 ...`。

现在只有控件被剥离，`selflift_sigmas` 留在 payload 里由 `extend_with_selflift` 自己读取；
回归测试 `test_sigmas_input_survives_the_split` 锁住这个行为。

顺带把"端口在但值是 None"的日志升级成**指名上游节点**：读 hidden 的 `prompt`/`unique_id`，
直接说出连线的来源节点号、`class_type` 和它为什么没产出（不在提示里 = 被静音/旁路/未执行分支；
在提示里 = 这次没产出输出）。

### 外接 SIGMAS 的优先级（就是"外接最高"）

父类只有两处真正采样（`_sample_h3`，FL2VA 与 Ref2VA 各一处），都被本包置换接管，
`_dual_stage` 里 `sigmas_override` 优先——所以**外接生效时 `steps`/`scheduler`/`denoise` 三个控件
被完全旁路**（UI 上也会被置灰）。缓存签名里带 `sigmas_digest`，改外接链会自动整链重渲。

想确认这次有没有走外接，看三处就够了：

1. **节点右上角徽标**：连上并显示 **「外部 SIGMAS 生效」**（橙色）即已生效；没看到就是没连上。
2. **每次运行一行 INFO 日志**：
   - `采样调度来源：外接 selflift_sigmas（N 条 -> M 步，切分 X 低分辨率 + Y 高分辨率）`
   - 或 `采样调度来源：节点控件 steps=… scheduler=… denoise=…（selflift_sigmas 未连接或为空）`
3. **报错里的 `resolved from ...`** 那一句（同上）。

### 常见报错：`transition_step N is out of range for N steps`（旧版行为，供回溯）

`transition_step` 是**切在真实 sigma 调度表上的索引**，必须给高分辨率阶段留至少 1 步，
所以合法范围是 `1..总步数-1`。报错里会写明步数从哪来：

- 走控件时：`(resolved from steps=8 with scheduler=simple, denoise=1.0)`
  → 总步数来自 `steps` 控件，把它降到 `steps-1` 以内，或把 `steps` 调大。
- 走外接表时：`(resolved from the external selflift_sigmas table (9 entries -> 8 steps))`
  → 总步数 = 表条目数 - 1，**与 `steps` 控件无关**；插值/精修节点加步后要重设切割点。

每次开跑的日志会打印 `schedule=widgets|external(N entries) steps=N | low X step(s) -> ... -> high Y step(s)`，
照着这行就能确认低/高分辨率分配是不是你想要的。

### 两套推荐起点

**A. SelfLift-zero（无外部权重，今天就能跑）**
```
steps=6~8   transition_step=3~4   lowres_scale=0.5
rho=0.6     w_min=1.0   w_max=1.0   upscaler_model=none
```
代价：每段多一次 VAE 解码→放大→重编码。`steps=4` 时基本没有加速收益，主要是画质/稳定性收益。

**B. 学习型 latent upscaler（需先把权重放进 `models/latent_upscale_models`）**
```
steps=8~16  transition_step=4~6   lowres_scale=0.5
rho=0.0     upscaler_model=<你的 h3 upscaler>   upscaler_unload=true
```
这条路跳过了完整的 VAE 往返，加速更接近理论值。显存不足再开 `selflift_highres_tiling`。

> 想要更强的段间稳定性，可以在 `model` 输入前串一个 SelfLift 自带的 `H3 Temporal State Transport (TST)` 节点（tau=0.2），它是 MODEL→MODEL 补丁，与采样器无关。

---

## 缓存一致性

采样器的变化对 Extender 的缓存逻辑是**不可见**的。因此本包把 SelfLift 的完整参数序列化进
manifest 的 `selflift_plan_signature` 字段（Extender 重写 manifest 时会保留未知键）：

- 签名一致 → 正常续跑，缓存百分百复用；
- 签名变了 → 自动 `_truncate_chain(..., 0)` 清空因果链重 Render，日志会打
  `SelfLift plan changed; resetting N cached clip(s)`。

Ref2VA（Motion Context）、Ref2VA（独立瑞）、FL2VA 三条链路都接了；万一某条链路的路径解析失败，
只会打 WARNING 跳过，**不会中断生成**。

---

## 目录结构

```
yanhuo_H3_Motion-Context_SelfLift/
├─ __init__.py            节点注册 + WEB_DIRECTORY
├─ vendor.py              只读定位/复用两个姊妹包（共享同一份 module 实例）
├─ config.py              SelfLiftSettings：解析、校验、缓存签名
├─ engine.py              采样接管（作用域置换 + finally 还原）与前置校验
├─ tiling_fix.py          高分辨率分块的 Motion-Context 修正（见下）
├─ node.py                节点：参数注入、缓存失效、调用父类 extend()
├─ companion_nodes.py     配套节点：参考图打包 / 成片导出 / 视频文件加载（v1.2.0）
├─ tools/build_frontend.py 生成前端 UI 变体（高度压缩 + 中文 + 皮肤）
├─ web/selflift_extender.js  （生成物）原 Extender UI + 三种后处理
└─ tests/                 test_selflift_bridge.py / test_selflift_integration.py /
                          test_tiling_fix.py / test_external_sigmas.py /
                          test_companion_nodes.py
```

---

## 配套节点（v1.2.0）

主节点采样完成后只吐 `cache`（链路缓存），成片要靠 Final Decode 导出；参考图也要先打包。
这三个节点把这条链路补齐，全部复用姊妹包的重逻辑，本包只做**绑定与中文化**。
都在 `MiniMax H3/SelfLift` 分类下。

| 节点 | 显示名 | 输入 → 输出 | 作用 |
| --- | --- | --- | --- |
| `YanhuoH3RefPackFromImages` | Yanhuo 参考图打包 (图像列表→ref_pack) | `images (IMAGE)` → `ref_pack`、`参考数` | 把一批参考图按帧顺序折成一个 `H3_REF_PACK`，直接接主节点的 `ref_pack_N`，即成为 **CLIP N 专属**的 Picture 1..K 参考集。上限 9 张（与每卡上限一致），超出部分丢弃并打 WARNING。 |
| `YanhuoH3FinalDecodeOutput` | Yanhuo 成片导出 (Final Decode) | `cache` + `vae` (+ 编码参数) → `成片视频 (VIDEO)` | 主节点跑完后的**视频输出端**：在磁盘上把全部已渲染片段拼接、编码成成片并给预览，输出 VIDEO 供下游（放大、插帧、转码、SaveVideo）继续用。拼接与编码逻辑全部来自 Extender 的 Final Decode，端口名一字不改（只翻译提示），因此老工作流不会断。 |
| `YanhuoH3VideoFileLoader` | Yanhuo 视频文件加载 (成片→VIDEO) | `video_path (STRING)` → `视频 (VIDEO)`、`文件路径` | 把链路已写出的成片/分段（mp4/mkv）重新装载为 VIDEO 喂给下游。支持绝对路径，或相对 ComfyUI `output` 目录的路径（如 `yanhuo/chain_00001_.mp4`）。只读封装，不做二次解码拷贝。找不到文件时在 Queue 前就报错。 |

典型接法：

```
[图像列表] ──> Yanhuo 参考图打包 ──ref_pack──> Yanhuo H3 Motion Context SelfLift (ref_pack_1)
                                                        │ cache
                                                        v
                                          Yanhuo 成片导出 (Final Decode) ──成片视频──> SaveVideo / 视频放大
                                                        │ （已写出的 mp4）
                                                        v
                                          Yanhuo 视频文件加载 ──视频──> 下游二次处理
```

### 参考图端口：只保留逐片段的

v1.2.0 移除了全局的 `ref_pack` / `prompt_pack` 两个输入端口——它们和逐片段的
`ref_pack_N` / `prompt_N` / `ref_audio_N_x` 语义重叠，容易接错。
现在节点上只剩与 CLIP N 同步的那些端口，接哪个片段就只影响哪个片段。
**已保存的老工作流如果连过这两个全局端口，重新打开时那两条线会显示为空**，把线改接到
`ref_pack_N` 即可（行为等价，只是作用域变成片段级）。

---

## 全局 LoRA / 全局种子 / 跟随上一片段（v1.3.0）

三个新交互，全部为了"多片段批量出片时不用逐卡重复配置"：

### 全局条（参考图像与 CLIP 卡片之间）

一条固定 34px 的工具条，带两个开关：

| 开关 | 开启后 | 关闭后 |
| --- | --- | --- |
| **全局 LoRA** | 条内展开一份全局 LoRA 列表（下拉 + 强度，选满一行自动长出下一行，同卡片规则），**所有片段都用这份列表**；各卡片自己的 LoRA 区压暗并锁定 | 立即恢复各卡片自己的 LoRA 选择（原值一直保留在工作流里，不会丢） |
| **全局种子** | 条内出现种子输入框 + 🎲，**所有片段都用同一个种子**；各卡片的种子/骰子/种子模式压暗 | 恢复各卡片自己的种子 |

实现上设置随 `clips_json` 的 `yanhuo_global` 字段持久化；真正生效在**后端**——本包
`node.py` 在把 payload 交给父类 `extend()` 之前覆盖每个 clip 的 `loras`/`seed`。
所以：缓存失效自动按覆盖后的值计算（改全局 LoRA 会触发受影响片段重渲）；
`.ext` 项目导出/载入也带上这两个开关。

### 「⧉ 上一段」按钮（每张卡片，骰子右侧）

把**上一个 CLIP** 的 LoRA 选择与强度（含个数）一次性复制到本片段，复制后两边各自独立编辑。
CLIP 1 没有上一段，按钮禁用。全局 LoRA 生效时它也暂时禁用（没有意义）。

### 高度代价

全局条常驻占 **34px**，开全局 LoRA 展开编辑区再多 **118px**——面板下限公式已把这部分计入，
打开开关时节点会自动长高、关闭时收回，不用手动拖。

---

## 界面：最小约束 + 中文 + 主题色（v1.2.0 / v1.2.1）

1. **默认高度压缩**：`UI_MIN_HEIGHT` 650→340、`NODES2_MIN_HEIGHT` 700→400、
   卡片最小高度 455/560→**240/310**，节点默认展开时不再占半屏。
   面板下限真正由 `55 + REF_SECTION(160) + 卡片min + 24` 决定，所以卡片高度才是有效的那个阀。
2. **载入时自动收拢历史大尺寸**（v1.2.1）：工作流里保存的节点高度会被 ComfyUI 原样恢复，
   面板的下限只会"抬"不会"压"，所以旧工作流里那个巨大的节点不会因为改了常量就变小。
   本包把 "runaway height" 的判定从 `> max(1800, min*3)` 收紧到 **`> max(1200, min*1.6)`**——
   载入时超过即视为历史遗留，一次性收拢到最小高度。**代价**：手动拖到超过最小 1.6 倍后，
   重新载入会被收回来（想留更大空间别超过这个比值）。
3. **工具栏「⇕ 紧凑」按钮**（v1.2.1）：一键把节点收拢到当前状态的最小高度，不用手动拖。
   它走的是和主脚本同一套 `syncDomHeight`，Legacy / Nodes 2.0 两种模式各算各的下限。
4. **参数说明中文化**：全部 widget 与输入端口的 tooltip（含 `ref_pack_1..32`、
   `prompt_N` / `duration_N` / `ref_audio_N_x`、模式/调度器/去噪等）都改成中文，
   并注明"连接 `selflift_sigmas` 后被旁路"。
5. **按钮与状态中文化**：`+ 添加片段` / `− 删除末尾` / `新建项目` / `保存项目` /
   `载入项目` / `中断续跑`、`模式: FL2VA`、`运动链: 开/关`、`已校验` / `就绪` /
   `● 已缓存` / `▶ 渲染中` 等。
6. **外观 · 淡紫主题 + 取色键**（v1.2.1）：默认 **淡紫 `#b8a1e8`**，深浅主题自适应
   （深色不发白、浅色不发刺），按钮/输入框/下拉/卡片/滚动条全部从这一个色值派生。
   工具栏右侧有一个**颜色选择键**：点一下换色即可整体换肤（存进 localStorage
   `yanhuo_selflift_accent`，整台浏览器共用），**双击恢复默认淡紫**。
   只作用于本节点面板（`[data-h3-extender-root="1"]` 子树），用独立
   `<style id="yanhuo-selflift-skin">` 注入，不污染 ComfyUI 全局样式，也不影响 Extender 原节点。

> v1.2.0 的第一版皮肤是写死的亮色（白底输入框），在深色主题下刺眼——已弃用。

这些都不是手改生成物，而是在 `tools/build_frontend.py` 里维护的：
`HEIGHT_REWRITES`（高度）、`CHINESE_REWRITES`（文案）两张表，每条都声明"至少出现 1 次"。
**Extender 更新后重跑脚本，如果上游文案或常量变了，build 会直接失败并指出是哪一条**，
不会静默漏翻。主题与工具栏控件的实现都在文件尾部的 `SIGMAS_UI_TAIL`：它与主脚本同处一个
ES module 作用域，可以直接调用 `syncDomHeight` / `uiMinHeightForState` / `domWidgetRenderMode`，
所以「⇕ 紧凑」按钮走的是主脚本自己的那套上限/下限计算，不会另立一套把节点拉歪。

### `tiling_fix.py`：Motion Context 关键帧 + 参考图共存时的分块修正

开启 `selflift_highres_tiling` 时，SelfLift 会把高分辨率阶段按空间切成 1~8 块，每块重建一份
`PackedLayout`。原版 `_tile_payload` 只在**没有 refs** 的情况下才把关键帧 latent 跟着块一起裁剪：

```python
if not payload.get("refs"):
    tiled["cond_video_latents"] = [...]
```

而 Motion Context 链条里关键帧和 Ref2VA 参考集**必然同时存在**（Extender 的
`patch_motion_payload.py` 专门让二者共存）。结果是关键帧条件行仍是全分辨率，块的 layout
却按块的网格算行号，H3 在 `all_video_rows[~img_update] = cond_video_rows` 处抛
`RuntimeError: shape mismatch: value tensor of shape [12040, 96] cannot be broadcast to ...`。

本包在**运行时**（不落盘、不改两个原插件的任何文件）替换 `h3_tiling` 里的两个模块级函数：

- `_tile_payload`：始终重建关键帧那部分条件行（参考行保持原网格、原顺序不动），
  并在动 GPU 之前校验"条件行数 == layout 声明的行数"；
- `_tiled_forward`：万一还有对不上的 payload，记录一条 ERROR 并**退化成整帧单次推理**，
  而不是让整条链崩掉（此时显存会明显上涨，日志会提示关掉分块）。

只在 `selflift_highres_tiling=True` 时安装；关掉分块时 SelfLift 行为与原生完全一致。

### 关于生成的前端文件

`web/selflift_extender.js` 由 `tools/build_frontend.py` 从 Extender 的 `web/extender.js`
生成，除替换两个字符串（`const TARGET` 绑定到本节点、扩展名）外，还做三件事：
高度压缩、`CHINESE_REWRITES` 文案替换、文件尾部**追加** `SKIN_CSS` + `SIGMAS_UI_TAIL`
（外观皮肤；`selflift_sigmas` 外接时给 scheduler/steps/denoise 加 `SIGMAS EXT` 徽标并置只读）。
Extender 更新后重跑脚本即可同步：

```bash
python tools/build_frontend.py
```

---

## 已知限制

1. **必须用 Euler + `s_churn=0` + rectified-flow 模型**（MiniMax H3 满足）。其它组合在 Queue 前就会被
   `validate_plan` 拦下并给出明确报错，不会跑到一半才炸。
2. `steps >= 2`，`transition_step <= steps-1`；`rho=0` 且 `upscaler_model=none` 会被直接拒绝
   （等于什么都没启）。
3. **不要并发跑两条 Extender 家族链路**：采样接管是模块级的（有 `finally` 保证还原，
   也有锁串行化），ComfyUI 本身单链执行，正常用法不受影响。
4. 项目存档（`set_project` / 自动导出）里记录的 class 名仍是 `MiniMaxH3Extender`——那是
   父节点自己写的字段，本包无法改，尽量避免用这个项目导入直接还原本节点。
5. `comfyui-SelfLift` 对 MiniMax H3 的适配作者自己标注为**实验性**（论文未在视频模型上验证），
   段间抖动这类问题只能在真实素材上验。
6. 三个配套节点里，**成片导出**依赖 Extender 的 `motion_context_disk` 模块。若该模块缺失，
   只有这一个节点不注册（日志会 WARNING 提示），改用 Extender 自带的 Final Decode 即可；
   另外两个节点不依赖它，照常可用。

---

## 测试

在 ComfyUI 的 venv 里跑：

```bash
"D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_selflift_bridge.py
"D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_selflift_integration.py
"D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_tiling_fix.py
"D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_external_sigmas.py
"D:/comfyui012/ComfyUI/ComfyUI/.venv/Scripts/python.exe" tests/test_companion_nodes.py
```

- `test_selflift_bridge.py`（32 项）：参数解析/校验、缓存签名、参数是否与父控件混用、
  采样接管的路由与还原（含异常路径）、Signature 失效/复用/失败兜底、控件顺序必须追加在父控件之后、
  `selflift_sigmas` 必须是输入端口而非控件、全局 `ref_pack`/`prompt_pack` 已移除而逐片段端口保留、
  继承来的 tooltip 必须是中文。
- `test_selflift_integration.py`（8 项，用真实姊妹包）：SelfLift 的 `progressive_sample`
  位置参数签名是否与本桥一致、真实 Extender latent 能否通过 SelfLift 校验、
  关键帧缩放到低分辨率网格后的均值保持、音频流 Euler 步进、**真实模块上的置换/还原**。
- `test_tiling_fix.py`（10 项）：先复现"关键帧 + 参考集共存时原版行数对不上"的原始 bug，
  再验证修正后关键帧随块裁剪、参考集保持原网格、块内 position_ids 正确、
  不一致 payload 会触发整帧兜底。
- `test_external_sigmas.py`（23 项）：调度表指纹的稳定性/内容敏感性、签名与 plan 文本随外接变化、
  输入剥离、归一化、外部表全套前置校验（单调/零值/末位/transition 范围/仍要求 Euler）、
  外部表绕过 `_sigmas` 直达采样、置换还原、disabled 时忽略外接。
- `test_global_overrides.py`（15 项）：`_apply_yanhuo_globals` 的全部分支——无字段/开关全关是 no-op、
  全局 LoRA 覆盖所有片段且逐 clip 独立拷贝（不共享引用）、空名/非 dict 项被剔除、强度钳制 ±100、
  全局种子覆盖所有片段、负数种子视为无效不覆盖、其余 clip 字段原样保留、
  `yanhuo_global` 字段保留以便往返、坏 JSON/数组 payload/非 dict 配置原样返回、
  以及 `extend_with_selflift` 在 super() 之前完成覆盖（真实父类 extend 打桩验证）。
- `test_companion_nodes.py`（23 项）：三个配套节点的注册/中文显示名与说明、
  参考图打包的槽位形状与 9 张上限（含 `[H,W,C]` 单图、空输入、非张量）、
  打包结构与姊妹包 `reference_bridge` 一致（type/version/count/slots）、
  Final Decode 桥**只改 tooltip 不动端口名**、非 tooltip 选项（forceInput/lazy/default）原样保留、
  `export` 确实委派给父实现、视频加载器的路径解析/校验/中文报错/委派。
