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

## 2026-07-29 开发落点

已生成基于 `0f2aaf8c86772186e93bca830c998c5ddac12758` 的 v2 补丁：
`patches/src-0f2aaf8c-adaptive-geometry.patch`。

本轮已实现：

- 不可变几何对象和 logical/capture/display 正反变换；
- 同一 raw frame 的三视口懒缓存及单调 `frame_id`；
- `device.image` 暂时兼容为 Center，并提供显式 `image_for(alignment)`；
- click/long-click/swipe/drag 的显式 alignment 和可选 frame/contact；
- MaaTouch/minitouch 与 scrcpy 各自的末端坐标语义，去除 1280×720 二次缩放；
- 摇杆 Left/contact 1、镜头 Center、右侧地图按键 Right 的首批关键路径；
- 公共识别 API 已接受 alignment，并迁移小地图、雷达、战斗状态和交互首批消费者；
- scrcpy 4.1 server、1600/1920 长边、缩放后 720 高度预检，以及宿主完整补丁探测门禁；
- 路线 checkpoint 原子持久化、全地图 `plane_floor` 验证和中途实际坐标恢复；
- 无 checkpoint 的中途位置不再接受跨地图候选，无法证明是出生点时直接停止；
- 战斗中断后只重放原路线的出口收尾，宽屏敌人与可破坏物使用 SRC 原有的有界补打；
- 日常队列的主界面快捷栏、战斗波次、委托教学、无名勋礼、支援奖励、背包页签及数据
  OCR 已按实际 UI 锚点切分到 Left/Center/Right 视口。

首批迁移后，按 `self.device.image\b` 精确模式静态统计仍有 42 个文件、84 个访问点；
另有 `ui.device.image`、`main.device.image` 等别名访问。它们继续列入阶段 6，不能因为
公共 API 已支持 alignment 就视为自动完成。

本轮未新增或运行自动化测试，最终验收直接使用 vivo V2458A 真机。2026-07-29 的
2800×1260、Android 16、safe insets=125/0/125/0 样本已无人工干预完成世界 8：命途与
初始祝福、13 个路线域、事件/交易/休整、精英、Boss、祝福/奇物/命途回响、禁用沉浸器
奖励跳过、`ROGUE_REPORT` 自然结算和奖励领取全部通过。宽屏敌人与可破坏物有界补打
多次生效，runner 返回 `status=completed`，route checkpoint 在自然终点后清除。

同日继续完成两轮 daily 验收。第一轮完成助战战斗、委托、无名勋礼、每日实训、
Freebies 与 DataUpdate，最终活跃度为 `500`、无剩余任务且五档奖励全部领取；第二轮
原样复跑返回 `status=completed`、`work_performed=false`、`idempotent=true`。这同时
验证了调度持久化和当日重复启动不会重复消耗账号资源。

完整补丁 checkout 因而开放 `end_to_end_verified=true`，验收范围明确为单参考设备；
旋转、minitouch/uiautomator2 及多机型矩阵仍未完成。

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
| 1. 几何契约 | 新增不可变 `DisplayGeometry`、`CaptureGeometry`、`Viewport`；处理旋转、safe inset、display/capture 比例 | 2800×1260、insets=125/0/125/0 已验证；旋转和其他系统 UI 形态待验收 | 单机已验证 |
| 2. 多视口截图 | 同一 raw frame 懒生成 Left/Center/Right 三个 `1280×720` view，并共享 `frame_id` | 2800×1260 的三视口和 scrcpy 1920 缩放帧已确认 | 单机已验证 |
| 3. 识别来源传播 | 模板、OCR、颜色、局部 ROI 返回 `Hit(..., alignment, frame_id)`；淘汰裸坐标返回 | 公共识别 API 的结果均能追溯 raw frame 与 viewport | 1–1.5 周 |
| 4. 动作语义 | click/swipe/drag/joystick 全部接收 alignment；为连续触摸固定 contact owner | MaaTouch 摇杆、交互和战斗已验证；其余调用点与 contact owner 审计待完成 | 核心单机已验证 |
| 5. 后端转换 | MaaTouch、minitouch、scrcpy control 分别将 logical+viewport 映射到 capture/display | MaaTouch + scrcpy 4.1 已验证；minitouch、uiautomator2 和旋转一致性待回放 | 部分已验证 |
| 6. 直接读取审计 | 审计 48 个直接读取 `self.device.image` 的文件、104 个访问点；按页面声明默认 alignment | CI 清单归零或每个豁免带责任人、原因和回归 fixture | 1.5–2 周 |
| 7. 分层流程回归 | 先主界面/领取等静态任务，再打本、地图导航、Rogue 入口，最后完整 Rogue | 2800×1260 真机已完成世界 8 自然结算，并完成 daily 首轮及幂等复跑 | 单机已完成 |
| 8. 真机矩阵 | 覆盖主流比例、挖孔/刘海、导航模式、正反横屏和各 Android SDK | 每类至少一台设备连续 3 轮无越界、误触、contact 泄漏 | 持续 |

阶段 1–6 应作为一个上游 SRC patch series 维护；Mobile Profiler 只负责选择 checkout、
传递配置、采集证据和执行验收门，不在宿主层再次实现触控桥。

## 从单机闭环开始的后续顺序

1. 完成阶段 6 的直接图像读取清单，为每个剩余消费者声明 Left/Center/Right，优先覆盖
   编队、地图、战斗弹窗和 Rogue 分支页面。
2. 在当前 vivo 上补齐反向横屏、手势/三键导航切换，以及 minitouch、uiautomator2
   后端的一致性；任何坐标语义不一致都在扩大设备范围前修复。
3. 按 16:9 → 19.5:9 → 20:9 → 21:9/超宽的顺序接入真机，每台先保存 raw frame、
   三视口与几何 JSON，再运行世界 8 完整单轮。
4. 每个几何类别连续完成 3 轮，检查越界、误触、contact 泄漏、错误 viewport 和
   checkpoint 恢复；失败样本固化为回归 fixture。
5. 矩阵达标后再声明“多机型适配完成”，并把已验证的分辨率、系统 UI 形态和触控后端
   作为结构化能力返回给目录页。

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
- `end_to_end_verified=true` 只表示完整补丁已在声明的参考设备完成自然终点，不外推到
  其他几何与触控后端。
- 只有至少覆盖 16:9、19.5:9、20:9 和超宽各一台真机，并完成完整 Rogue 自然终点，
  才能将多机型分辨率适配标记为完成。
