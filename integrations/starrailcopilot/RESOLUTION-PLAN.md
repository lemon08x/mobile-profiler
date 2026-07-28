# StarRailCopilot 手机分辨率适配计划

## 目标与难度

目标是在不改写 SRC 路线和模板坐标的前提下，让同一套 `1280×720` 逻辑资源安全
运行在不同横屏手机上，并保证识别命中的 viewport 与后续输入使用同一坐标语义。

综合难度评估为 **高**。单人完成工程改造与基础回归约需 8–12 工程周；完整 Rogue
路线和多品牌真机稳定性还取决于设备矩阵与游戏版本。相比 MAA，主要增量来自 48 个
直接图像消费者、104 处直接访问，以及摇杆/镜头/攻击的并发多点触控。

不在本阶段处理游戏内容更新、重新制作模板、账号策略或自动解锁队伍。任何可能消耗
开拓力或沉浸器的选项仍默认关闭或要求显式配置。

## 当前事实基线

- SRC 逻辑坐标：`1280×720`。
- `Screenshot.check_screen_size()` 与 uiautomator2 初始化均拒绝其他尺寸。
- scrcpy server 当前 `max_size=1280`，超宽手机会得到类似 `1280×576` 的画面。
- MaaTouch/minitouch 内部也以 `1280×720` 为输入语义。
- SRC 原生支持 Android 截图与触控，不需要 PC 键鼠兼容层。
- Mobile Profiler runner 对原版 SRC 仍将非 `1280×720` 设备标为
  `unsupported_resolution`；只有完整组件哨兵通过后才允许自适应 viewport。

## 2026-07-28 开发落点

已生成基于 `0f2aaf8c86772186e93bca830c998c5ddac12758` 的首版补丁：
`patches/src-0f2aaf8c-adaptive-geometry.patch`。

本轮已实现：

- 不可变几何对象和 logical/capture/display 正反变换；
- 同一 raw frame 的三视口懒缓存及单调 `frame_id`；
- `device.image` 暂时兼容为 Center，并提供显式 `image_for(alignment)`；
- click/long-click/swipe/drag 的显式 alignment 和可选 frame/contact；
- MaaTouch/minitouch 与 scrcpy 各自的末端坐标语义，去除 1280×720 二次缩放；
- 摇杆 Left/contact 1、镜头 Center、右侧地图按键 Right 的首批关键路径；
- 公共识别 API 已接受 alignment，并迁移小地图、雷达、战斗状态和交互首批消费者；
- scrcpy 1600/1920 长边、缩放后 720 高度预检，以及宿主完整补丁探测门禁。

首批迁移后，按 `self.device.image\b` 精确模式静态统计仍有 42 个文件、84 个访问点；
另有 `ui.device.image`、`main.device.image` 等别名访问。它们继续列入阶段 6，不能因为
公共 API 已支持 alignment 就视为自动完成。

本轮按要求不新增或运行自动化测试。只做补丁格式、语法和静态差异检查；坐标回放、
并发 contact、旋转、安全区与完整 Rogue 回归统一留到真机联调阶段。当前仍保持
`end_to_end_verified=false`。

## 目标坐标模型

实现不可变、随帧携带的几何对象，明确区分三层：

```text
DisplayGeometry（物理显示、旋转、安全区、导航栏）
  -> CaptureGeometry（截图后端实际返回的尺寸与裁剪）
     -> Viewport（Left / Center / Right 中的一个 16:9 区域）
        -> LogicalFrame（1280×720，供现有识别代码使用）
```

识别结果必须携带来源：

```text
Hit(rect, alignment, frame_id)
```

动作必须显式携带目标语义：

```text
Action(point, alignment, contact_id)
```

禁止使用进程级或 Device 级的全局 `active_viewport`。同一原始帧内可能同时需要左侧
摇杆、中央导航和右侧战斗按钮，全局状态会使异步识别或连续触控串坐标。

## 分阶段计划

| 阶段 | 工作内容 | 完成门槛 | 预计 |
| --- | --- | --- | --- |
| 0. SRC 底层替换 | 独立 runner、原生 SRC Device、只读 ADB 预检、旧 ID 迁移 | 非 720p 手机稳定返回 `unsupported_resolution`；无 pyautogui/win32 路径 | 已完成 |
| 1. 几何契约 | 新增不可变 `DisplayGeometry`、`CaptureGeometry`、`Viewport`；处理旋转、safe inset、display/capture 比例 | 首版代码完成；旋转、inset 与误差门槛待真机验证 | 已开发，待验收 |
| 2. 多视口截图 | 同一 raw frame 懒生成 Left/Center/Right 三个 `1280×720` view，并共享 `frame_id` | 首版代码完成；2800×1260 应为 x=0/280/560，待真机截图确认 | 已开发，待验收 |
| 3. 识别来源传播 | 模板、OCR、颜色、局部 ROI 返回 `Hit(..., alignment, frame_id)`；淘汰裸坐标返回 | 公共识别 API 的结果均能追溯 raw frame 与 viewport | 1–1.5 周 |
| 4. 动作语义 | click/swipe/drag/joystick 全部接收 alignment；为连续触摸固定 contact owner | 公共入口和摇杆已接入；其余调用点与 contact owner 审计待完成 | 核心已开发 |
| 5. 后端转换 | MaaTouch、minitouch、scrcpy control 分别将 logical+viewport 映射到 capture/display | 三后端代码已接入；tap/swipe/多 contact 和旋转一致性待真机回放 | 已开发，待验收 |
| 6. 直接读取审计 | 审计 48 个直接读取 `self.device.image` 的文件、104 个访问点；按页面声明默认 alignment | CI 清单归零或每个豁免带责任人、原因和回归 fixture | 1.5–2 周 |
| 7. 分层流程回归 | 先主界面/领取等静态任务，再打本、地图导航、Rogue 入口，最后完整 Rogue | 每层达到自然终点；失败保留 raw/view 截图、几何、命中和输入轨迹 | 1–2 周 |
| 8. 真机矩阵 | 覆盖主流比例、挖孔/刘海、导航模式、正反横屏和各 Android SDK | 每类至少一台设备连续 3 轮无越界、误触、contact 泄漏 | 持续 |

阶段 1–6 应作为一个上游 SRC patch series 维护；Mobile Profiler 只负责选择 checkout、
传递配置、采集证据和执行验收门，不在宿主层再次实现触控桥。

## 手机分辨率矩阵

优先以“比例 + 系统 UI 形态”建模，不以具体品牌硬编码：

| 类别 | 示例横屏尺寸 | 主要风险 |
| --- | --- | --- |
| 16:9 | 1280×720、1920×1080、2560×1440 | 纯缩放、后端取整 |
| 18:9 | 2160×1080 | 左右留白与中心偏移 |
| 19.5:9 | 2340×1080、2532×1170 | 挖孔、安全区、奇数偏移 |
| 20:9 | 2400×1080、2800×1260 | 三视口选择、导航栏 |
| 21:9/超宽 | 2520×1080 及以上 | 路线与战斗 UI 分居不同 viewport |
| 反向横屏 | 上述各尺寸 | inset 与旋转方向翻转 |
| 手势/三键导航 | 同一设备两种模式 | display、capture 和可交互区域不一致 |

每个样本记录：Android/SDK、分辨率与 density、rotation、cutout/insets、截图后端、
触控后端、游戏服务器、原始 PNG、三视口 PNG、坐标轨迹和最终状态。

## 2800×1260 参考计算

对当前 20:9 真机，按高度适配逻辑画面：

```text
scale = 1260 / 720 = 1.75
viewport_width = 1280 * 1.75 = 2240
Left.offset_x   = 0
Center.offset_x = (2800 - 2240) / 2 = 280
Right.offset_x  = 2800 - 2240 = 560
```

逻辑点 `(x, y)` 到物理点的基础映射为：

```text
physical_x = viewport.offset_x + x * 1.75
physical_y = viewport.offset_y + y * 1.75
```

实际实现必须先叠加 CaptureGeometry、rotation 与 safe inset，而不能直接使用上述简式。
若继续使用 scrcpy，`max_size` 至少要能保留 720 像素高度；对 2800×1260，长边下限为
1600，建议做成 1600/1920 可配置项，再由 viewport 层裁成逻辑帧。不能简单把整张
`1280×576` 拉伸到 `1280×720`，那会破坏模板比例和触控对应关系。

## 首个关键验收

第一项必须通过的并发触控用例：

1. contact 1 在 Left viewport 持续保持并移动摇杆。
2. contact 0 在 Center viewport 连续拖动镜头。
3. 独立 tap 在 Right viewport 点击攻击或技能。
4. 三类动作使用同一 Display/CaptureGeometry，但各自保留 alignment。
5. 镜头和攻击不得释放、移动或复用摇杆 contact；停止后所有 contact 必须归零。

该用例未通过前，不进入完整 Rogue 路线测试。

## 验收与回退规则

- 不支持的几何必须 fail-closed，返回可诊断状态，不猜测 viewport。
- 一次动作的 `frame_id` 已过期时，重新识别；不得在新帧沿用旧命中。
- 任何坐标越出对应 viewport 或物理 display 都在 backend 前拒绝。
- 每次失败保存 raw frame、三个 logical view、几何 JSON、命中列表与 contact timeline。
- 只有至少覆盖 16:9、19.5:9、20:9 和超宽各一台真机，并完成完整 Rogue 自然终点，
  才能将适配器改为 `end_to_end_verified=true`。
