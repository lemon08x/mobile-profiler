# StarRailCopilot mobile runtime

Mobile Profiler 的崩铁入口已从 M7A / `Auto_Simulated_Universe` 兼容层切换到
[LmeSzinc/StarRailCopilot](https://github.com/LmeSzinc/StarRailCopilot)（SRC）。
当前状态是“底层替换完成、2800×1260 真机已完成世界 8 与两轮日常验收、多机型矩阵
尚未验收”。

## 当前边界

| 项目 | 当前实现 |
| --- | --- |
| 上游 | 用户单独安装 SRC checkout；仓库只维护固定提交的差异补丁，不提交完整源码、路线或图片资源 |
| 许可证 | 上游及其差异补丁遵循 GPL-3.0；Mobile Profiler 宿主通过独立子进程配置和启动 |
| 截图 | SRC 原生 `scrcpy`、`ADB` 或 `uiautomator2` |
| 输入 | SRC 原生 `MaaTouch` 或 `minitouch` |
| 宿主预检 | 标准 ADB CLI，只读执行 `get-state`、`pm path`、`dumpsys activity`、`screencap -p` |
| 当前分辨率门禁 | 原版 SRC 仍只放行 `1280×720`；检测到完整补丁后放行高度不低于 720、比例为 16:9～3:1 的横屏 |
| 运行许可 | 完整补丁 checkout 返回 `end_to_end_verified=true`；缺少任一组件哨兵时继续 fail-closed |

旧实现动态伪造 pyautogui、win32 窗口与键盘接口，再把 PC 操作翻译成
uiautomator2 触摸。SRC 已经拥有完整 Android Device 层，继续保留该桥会产生两套
截图、坐标和触控状态，因此新运行链不加载旧桥：

```text
Mobile Profiler
  -> star_rail_copilot_runner（独立子进程）
     -> 只读 ADB 预检
     -> config/mobile-profiler.json（专用 SRC 实例）
     -> StarRailCopilot + 当前任务的 AzurLaneConfig
     -> SRC Device
        -> ADB / scrcpy 截图
        -> MaaTouch / minitouch 输入
     -> Rogue 或有界 daily 队列
```

## Checkout 与运行参数

默认目录为：

```text
<output-root>/open-source-runtimes/StarRailCopilot
```

也可以设置：

```powershell
$env:MOBILE_PROFILER_STAR_RAIL_COPILOT_ROOT = "D:\src\StarRailCopilot"
```

若 checkout 带有 `toolkit/python.exe`，子进程优先使用该解释器；否则使用 Mobile
Profiler 当前 Python。运行器验证关键源码、Rogue 路线索引、GPLv3 LICENSE、提交号、
路线数量以及内置 scrcpy server 版本。支持 SRC 已声明的国服官服、B 服、国际服和
越南服包名。

专用配置包含：运行目标（模拟宇宙/日常）、服务器、OCR 语言、截图/输入后端、模拟宇宙
世界、命途、区域策略，以及沉浸器、双倍事件、每周刷取和开拓力开关。预检失败不会
导入 SRC 的模型或创建 Device。

## 自适应几何补丁

当前 v2 补丁固定基于 SRC 提交 `0f2aaf8c86772186e93bca830c998c5ddac12758`：

```text
patches/src-0f2aaf8c-adaptive-geometry.patch
```

补丁不会复制完整上游源码，内容包括：

- 不可变的 `DisplayGeometry`、`CaptureGeometry`、`Viewport`、`LogicalFrame`、
  `Hit` 和 `Action`；
- 同一 raw frame 懒生成 Left/Center/Right 三个 `1280×720` 视图；
- `Control` 在进入后端前只做一次 logical → display/capture 映射；
- 公共模板、颜色、裁剪和 `appear_then_click` API 可显式携带 alignment 与 frame；
- MaaTouch/minitouch 使用 display 坐标映射到触控轴，scrcpy 使用视频坐标；
- 摇杆固定为 Left/contact 1，镜头为 Center，右侧交互显式使用 Right；
- 内置 scrcpy 4.1 server，长边可选 1600 或 1920，默认 1920；预检会计算缩放后的
  截图高度，低于 720 时返回 `unsupported_capture_resolution`；
- 模拟宇宙进入每条路线前原子写入 route checkpoint，成功跨域后才清除；进程重启时
  先用全地图定位验证 `plane_floor`，再把实际中途坐标注入原路线；
- 无有效 checkpoint 且角色不在真实出生点时 fail-closed，不再用跨地图相似候选猜测
  路线方向；
- 宽屏战斗域对敌人与可破坏物启用 SRC 原有的有界补打，避免桌面命中标记在手机 HUD
  下漏识别后把未清理实体当成已完成路线；
- 日常队列对主界面快捷栏、战斗波次、委托教学、无名勋礼 OCR/滚动、支援奖励弹窗、
  背包页签与货币/遗器 OCR 分别声明 Left/Center/Right 视口，并在最终阶段复核 500
  活跃度和五档奖励。

在对应 checkout 中应用前先检查：

```powershell
git -C D:\src\StarRailCopilot rev-parse HEAD
git -C D:\src\StarRailCopilot apply --check D:\mobile-profiler\integrations\starrailcopilot\patches\src-0f2aaf8c-adaptive-geometry.patch
git -C D:\src\StarRailCopilot apply D:\mobile-profiler\integrations\starrailcopilot\patches\src-0f2aaf8c-adaptive-geometry.patch
```

Mobile Profiler 会按组件哨兵检测补丁。仅复制一个 `geometry.py` 不会打开非 720p
门禁，route checkpoint 与宽屏可破坏物补打也是完整性门禁的一部分。

2026-07-29，补丁在 vivo V2458A、Android 16、2800×1260、safe insets
`125/0/125/0` 真机上，以 `scrcpy 4.1 + MaaTouch` 无人工干预完成世界 8 一整轮：入口、
命途与初始祝福、13 个路线域、战斗/事件/交易/休整、两次精英、Boss、祝福/奇物/命途
回响、禁用沉浸器时跳过奖励、`ROGUE_REPORT` 自然结算及奖励领取均成功。宽屏敌人与
可破坏物有界补打在同一轮多次触发并继续路线，最终 runner 返回 `status=completed`，
route checkpoint 文件已清除。

同一设备随后完成有界日常队列：助战战斗将约 300 开拓力消耗至 1，委托、无名勋礼、
每日实训、支援奖励、邮件与 DataUpdate 均自然结束；最终校验返回活跃度 `500`、无剩余
任务、五档奖励全部领取。紧接着原样执行第二轮，所有任务均按调度时间跳过，最终返回
`status=completed`、`work_performed=false`、`idempotent=true`。

这构成完整补丁的单参考设备端到端验收，不代表其他分辨率已经稳定；剩余图像消费者
审计、旋转/其他触控后端和多机型回归仍是后续阶段。

## 为什么完整适配仍未结束

分析基线为 SRC 提交 `0f2aaf8c86772186e93bca830c998c5ddac12758`。该版本在
`module/device/screenshot.py` 和 uiautomator2 初始化阶段强制 `1280×720`，scrcpy
也把长边固定为 1280。模板、ROI、点击、滑动、路线定位和摇杆控制都以同一逻辑尺寸
编写。

更关键的是，当前有 48 个 Python 文件、104 处访问直接读取
`self.device.image`。只在公共模板匹配入口裁剪截图会遗漏战斗、编队、地图、Rogue
路线、OCR 和小地图分析。实时移动还要求持续摇杆、转镜头和攻击使用不同 contact，
不能通过一个全局“当前 viewport”安全实现。

因此，底层替换本身比原来的 PC API 模拟更简单，但完整多分辨率支持比 MAA 的
viewport 改造更难：SRC 的坐标消费者更多，高频多点触控也是主流程的一部分。

详细实施阶段、手机矩阵和验收条件见
[RESOLUTION-PLAN.md](RESOLUTION-PLAN.md)。
