# Mobile Profiler 使用指南

本文以 Windows 为主，按实际操作顺序说明如何使用 Mobile Profiler。Android
通过 ADB 接入，原生 HarmonyOS 通过 DevEco Studio 的 HDC 接入；两者都走标准库主
流程。iOS 通过独立的可选 sidecar 运行时接入。三端都可以与 BTR2 配合、保存原始
证据并进入同一套报告和对比流程。

推荐优先使用运行时 UI。命令行保留给规则调试、自动化和故障排查。

## 1. 三分钟快速开始

### 1.1 在源码电脑启动

进入项目目录，双击：

```text
start-ui.bat
```

双击启动时会自动选择一个空闲的本机端口并打开浏览器，避免旧 UI 进程占用 8765
导致启动失败；需要固定端口时可执行 `start-ui.bat --port 8765`。

或者在已安装的环境中执行：

```powershell
mobile-profiler ui
```

### 1.2 在便携包电脑启动

完整解压 `mobile-profiler-v1.0.0-portable.zip`，然后双击解压目录中的：

```text
start-ui.bat
```

目标电脑不需要安装 Python、创建虚拟环境或执行 pip。

### 1.3 UI 中的标准顺序

以下是 Android/HarmonyOS 的标准顺序。Android 无线 ADB 和鸿蒙无线 HDC 细节见第
6 节；iPhone 的首次 RemotePairing 和拔线流程见第 17 节。

1. 在页面顶部人工选择 **Android / iOS / HarmonyOS**。平台选择会过滤设备列表，并
   切换为对应的 ADB、RemoteXPC 或 HDC 工作流。
2. Android/HarmonyOS 尚未连接时，在平台对应的地址框输入 `IP:端口`；iOS 先通过
   已信任的 USB 连接创建 RemotePairing。
3. 选择处于 `device` 状态的同平台手机。
4. 进入 **设备能力**，执行只读 **Probe**。
5. 确认正式测试时手机没有 USB、AC 或无线充电供电。
6. 回到 **实时监控**，填写运行名称、时长和起始说明，点击 **开始采集**。
7. 正常运行 BTR2；两边不必同时开始。
8. 必要时点击 **添加时间标记**，记录“BTR2 开始”“进入视频测试”等事件。
9. 采集结束后，在 **工具与交付** 中导入 BTR2 日志、重建报告、生成证据 ZIP。
10. 两台手机均完成后，在同一页面生成双机对比报告。

> 正式功耗数据必须以电池放电状态为前提。Probe 显示外部供电时，可以检查功能，
> 但不要把该次数据当作正式续航结论。

## 2. 先区分两种电脑角色

| 角色 | 用途 | 可以做什么 | 不建议做什么 |
|---|---|---|---|
| 源码电脑 | 开发和交付 | 修改代码、运行测试、启动 UI、重新构建便携 ZIP | 不要把 `.venv` 当作交付包复制 |
| 便携包电脑 | 实际测试 | ADB 连接、Probe、采集、日志导入、报告恢复、归档、双机比较 | 不直接修改包内代码，不从这里重新打包 |

Windows 虚拟环境通常记录原电脑的 Python 路径，不能可靠地跨电脑或跨目录迁移。
本项目使用官方 Embedded Python 构建真正可迁移的便携包，而不是复制 `.venv`。

## 3. 源码电脑首次安装

要求：

- Windows 10/11 和 PowerShell。
- Python 3.10 或更高版本。
- Android Platform Tools；建议把 `adb.exe` 所在目录加入 `PATH`。
- HarmonyOS 使用 DevEco Studio OpenHarmony SDK 自带的 `hdc.exe`；程序会自动检查
  常见安装路径，也可以使用全局 `--hdc PATH` 或 `HDC` 环境变量。
- Android 或 HarmonyOS 手机已打开开发者选项，并允许对应的 ADB/HDC 调试授权。

如果要采集 iOS，还需要 iPhone 开启 Developer Mode，并另外准备包含
`pymobiledevice3` 的 Python 环境；当前 Windows 便携包不内置该依赖。

先检查：

```powershell
python --version
adb version
adb devices -l
& "C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe" list targets -v
```

在项目目录创建开发环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
mobile-profiler --help
```

项目命令统一为 `mobile-profiler`。

如果 PowerShell 阻止激活脚本：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

也可以不激活环境，直接执行：

```powershell
.\.venv\Scripts\python.exe -m mobile_profiler --help
```

项目运行时只依赖 Python 标准库，没有第三方 Python 运行时依赖。

## 4. 构建并复制 Windows 便携包

### 4.1 从 UI 构建

只有从完整源码工程启动 UI 时，**工具与交付 → 重新构建 Windows 便携包** 才会
启用。

1. 确认当前没有手机采集正在运行。
2. 保持默认输出目录
   `dist\mobile-profiler-v1.0.0-portable`。
3. 根据目标电脑需要，选择是否包含本机 ADB Platform Tools。
4. 点击 **生成新版便携包**。

便携包环境中的该按钮会保持禁用，并提示必须回到源码电脑构建。

### 4.2 从命令行构建

双击：

```text
build-portable.bat
```

或执行：

```powershell
.\build-portable.bat
```

输出为：

```text
dist\mobile-profiler-v1.0.0-portable\
dist\mobile-profiler-v1.0.0-portable.zip
```

ZIP 中包含：

- 官方 Embedded Python。
- 当前源码中的 `mobile_profiler` 包和 Web UI。
- 中文指南、示例规则和启动脚本。
- 构建电脑能找到 ADB 时所需的 `adb.exe` 和 DLL。

把 ZIP 复制到目标电脑后完整解压。不要只复制 `start-ui.bat`，也不要直接在 ZIP
压缩视图中运行。

### 4.3 离线构建

首次默认构建会从 python.org 下载与构建电脑 Python 版本一致的 Windows
embeddable package。离线环境可以预先下载官方 ZIP：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build-portable.ps1 `
  -PythonEmbedZip D:\installers\python-3.13.7-embed-amd64.zip
```

不包含 ADB：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build-portable.ps1 -SkipAdb
```

指定 ADB：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build-portable.ps1 `
  -AdbPath D:\platform-tools\adb.exe
```

## 5. 在目标电脑启动 UI

双击便携目录中的 `start-ui.bat`。默认会：

- 启动本机 `127.0.0.1:8765` 服务。
- 打开默认浏览器。
- 把采集数据保存到便携目录下的 `profiler-runs`。

建议把长期测试数据放在独立磁盘目录，避免软件升级时和程序目录混在一起：

```powershell
.\start-ui.bat --output-root D:\MobileProfilerData\profiler-runs
```

其他常用启动方式：

```powershell
# 不自动打开浏览器
.\start-ui.bat --no-browser

# 修改端口
.\start-ui.bat --port 9000

# 无手机预览完整 UI
.\start-ui.bat --demo
```

UI 默认只监听本机。除非明确需要，不要把 `--host` 改成局域网地址或 `0.0.0.0`。

## 6. ADB / HDC 配对与连接

### 6.1 UI 可以完成的连接

已经知道无线调试的 `IP:调试端口` 时，在 UI 顶部输入后点击 **连接**。UI 执行的
就是：

```powershell
adb connect IP:PORT
```

也可以先在 CMD/PowerShell 中连接，再回到 UI 点击刷新。两种方式等价。

手机已经通过 USB ADB 授权、并且手机与电脑连接同一局域网时，可以直接点击顶部
**无线 ADB**：

1. UI 读取所选手机的 `wlan0` 等 Wi-Fi IPv4 地址。
2. 执行 `adb -s SERIAL tcpip 5555`，重启手机 adbd 到 TCP 模式。
3. 自动把 `手机IP:5555` 填入 ADB IP 输入框。
4. 自动执行 `adb connect 手机IP:5555`。
5. 提示连接成功后即可拔掉 USB，再刷新确认 IP 设备仍为 `device`。

设备下拉框会按 **有线设备（USB）**、**无线设备（Wi-Fi）** 和模拟器分组。
选择无线设备后，可以点击顶部 **断开无线**，UI 会执行：

```powershell
adb disconnect IP:PORT
```

采集运行期间 **无线 ADB** 和 **断开无线** 都会禁用，避免重启或断开 adbd 导致当前
采样中断。如果自动
连接失败，IP 地址仍会保留在输入框中，可以等待一两秒后手工点击 **连接**。部分 OEM
可能禁用传统 TCP 5555，此时继续使用 Android 系统的“无线调试”配对方式。

### 6.2 仍需在终端完成的配对

Android 11+ 首次使用“无线调试”通常需要一次配对：

```powershell
adb pair PHONE_IP:PAIR_PORT
adb connect PHONE_IP:DEBUG_PORT
```

配对端口和调试端口通常不同，以手机“无线调试”页面显示为准。出于交互式配对码
和系统兼容性考虑，`adb pair` 仍建议在终端完成。

### 6.3 传统 TCP 5555 方式

部分设备可先用 USB 执行：

```powershell
adb tcpip 5555
adb connect PHONE_IP:5555
```

连接成功后拔掉 USB。正式功耗测试不能让 USB 继续给手机供电。
顶部 **无线 ADB** 按钮就是这组命令的 UI 自动化版本，并会优先选择 Wi-Fi 网卡，
不会使用移动数据 `rmnet` 地址。

### 6.4 常见设备状态

- `device`：可以使用。
- `unauthorized`：解锁手机并确认调试授权。
- `offline`：执行 `adb disconnect IP:PORT` 后重新连接。
- 同时有多台设备：在 UI 顶部设备下拉框明确选择目标序列号。

### 6.5 HarmonyOS 无线 HDC

鸿蒙设备在下拉框中使用 `harmony:` 前缀，不会被当成 Android ADB serial。先通过
USB 完成 HDC 授权，并确保手机和电脑在同一 Wi-Fi。UI 中选择 USB 鸿蒙设备后点击
顶部 **鸿蒙无线**，程序会：

1. 读取 `wlan0` IPv4 地址。
2. 执行 `hdc -t USB_SERIAL tmode port 8710`。
3. 执行 `hdc tconn 手机IP:8710`。
4. 将 `harmony:手机IP:8710` 选为无线目标。

命令行等价操作：

```powershell
$hdc = "C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"
& $hdc list targets -v
& $hdc -t USB_SERIAL tmode port 8710
& $hdc tconn PHONE_IP:8710

mobile-profiler --hdc $hdc probe `
  --platform harmony `
  --device harmony:PHONE_IP:8710 `
  --json
```

确认 TCP 目标为 `Connected` 后拔掉 USB，并再次 Probe。只有 BatteryService 显示
`pluggedType: 0`、放电电流为负值时，才适合正式续航测试。鸿蒙采集会读取：

- BatteryService 电量、电流、电压和电池温度。
- 持久 HDC shell 中的 `/proc/stat`，以及低频 `hidumper --cpufreq`。
- AbilityManager 前台 Ability、PowerManagerService 屏幕/电源状态。
- RenderService 当前/支持刷新率、`fpsCount` 刷新档位计数、最近合成提交帧间隔和
  Maleoon GPU renderer；WindowManager 前台窗口。
- MultimodalInput 触摸设备、轴能力和已分发的触摸交互事件。
- `top` / `ps` 进程快照和 ThermalService 传感器温度。

HarmonyOS 量产接口没有公开面板控制器的硬件触控采样率。工具会显示“不可用”，只统计
真实分发到系统的触摸交互，不会把点击频率冒充 240/300 Hz。

Android BatteryStats、ActivityManager、ADPF 和 `dumpsys gpu` 在 HarmonyOS 上不适用，
报告会明确标记不可用，不会使用 Android 名称包装鸿蒙数据。

## 7. 单机标准测试流程

### 7.1 固定测试条件

两次测试或两台手机比较前，尽量固定：

- 电池起始电量和电池健康状态。
- 屏幕亮度、刷新率、分辨率和自动亮度开关。
- Wi-Fi、移动网络、蓝牙、定位和 SIM 状态。
- 环境温度、散热条件和手机壳。
- 系统版本、应用版本、账号和缓存预热状态。
- 测试前静置时间。

系统更新后尤其要留出静置时间。`dex2oat`、`artd`、`dexopt`、`odrefresh`、
`update_engine`、`apexd` 等后台活动可能同时增加 CPU、I/O、温度和电池功率。

### 7.2 执行 Probe

选择手机后进入 **设备能力**，点击 **运行 Probe**。重点检查：

- 电池是否在放电，是否检测到 USB/AC/Wireless 供电。
- `current_now` 是否可读取，单位判断是否合理。
- CPU policy 和频率节点是否存在。
- GPU 频率/负载或 UID work duration 是否可用。
- 当前/支持刷新率、前台窗口、GPU renderer 和触摸设备是否可见；Android 检查
  SurfaceFlinger 驻留与 `gfxinfo`，HarmonyOS 检查 RenderService 帧节奏。
- 整机进程和温度传感器是否可见；Android 上再检查 cpuset/ADPF。

Probe 是只读能力检查，不开始采集，也不重置 BatteryStats。

#### 高通 / Adreno 设备

检测到 `QTI`、`qcom` 或 KGSL 后，程序会启用高通适配：

- 读取 SoC、board platform 和可见的 Adreno `gpu_model`。
- 优先检查 KGSL `devfreq/cur_freq`、`gpuclk`、`gpu_busy_percentage`。
- 对 `gpubusy` 的“忙碌时间 / 总时间”两列数据计算真实百分比。
- 排除 `gpubw`、busmon、memlat 等总线 devfreq，避免把总线频率误报成 GPU 核心频率。
- 读取 WALT governor、CPU policy 的动态上下限，以及可见的 `core_ctl` 最小/最大
  在线核心数。
- KGSL 被量产权限限制时，自动保留 `dumpsys gpu` 的 UID 活跃时长、GPU 总内存和
  各 PID 内存快照作为回退证据。

量产高通系统常允许读取 `gpu_model`，但拒绝 shell 读取 `gpuclk`、`gpubusy` 和
devfreq。此时报告会明确写“权限受限”，不会把 `gpubw` 或 CPU/总线数据伪装成 GPU
负载。root/userdebug 设备如果开放这些节点，则会自动启用实时 GPU 频率或负载。

当前适配已在 QTI SM8750、Adreno830v2、Android 16 user/release-keys 设备上完成
只读 Probe 验证：WALT、`core_ctl`、ThermalService、GPU UID 工作和 GPU 内存可用，
KGSL 实时频率/负载由 SELinux 限制并正确降级。

### 7.3 配置采集

在 **实时监控** 中填写：

- **测试平台**：必须先人工选择 Android、iOS 或 HarmonyOS。设备下拉框只显示当前
  平台设备，后端也会校验设备类型，平台与设备不一致时拒绝 Probe 和采集。
- **平台专属界面**：Android 显示 ADB、BatteryStats、gfxinfo、GPU 节点和 Android
  调度项；iOS 显示 RemoteXPC、DVT/PowerTelemetry、Bundle ID、进程资源和采集器开销；
  HarmonyOS 显示 HDC、SmartPerf、RenderService、DDR 和 602 高性能模式。
- **能力边界**：iOS 当前不提供通用应用 FPS、Core Animation 详细帧时间戳、CPU/内存
  频率或 Android 调度接口，因此对应开关会禁用；Android 不显示 Harmony hitch；
  HarmonyOS 不显示 Android settings、BatteryStats 归因和当前不可用的热点线程扫描。

- 页面顶部默认选择 **功耗测试**。切换到 **性能测试** 后，主指标改为 FPS、1% Low、
  P99 帧耗时和异常帧；渲染分辨率与插帧状态仅在存在可靠证据时显示，同时提高前台窗口、
  进程和调度快照频率。
  正在采集时不能改变测试档位。性能测试必须绑定具体游戏或应用；Android 可点击
  **扫描手机应用**，搜索包名或 Activity 后选择，选择结果会自动写入目标包名。

两种模式不仅替换实时卡片和报告表格，也使用不同的分析目标：

- **功耗测试**围绕电流 / 功率为什么变化展开，检查任务负载、cpuset 与 Governor、
  CPU/GPU/内存频率、亮度和刷新率、省电与无线设置、后台活动、BatteryStats、UID、
  Wakelock 和组件模型。它保留刷新率等显示设置上下文，但不展开帧延迟链路。
- **性能测试**围绕慢帧如何产生展开，分析 VSync 起步、输入、动画、布局遍历、UI 绘制、
  RenderThread 提交、GPU 等待、BufferQueue、SurfaceFlinger/HWC 背压，以及调度和热限制。
  功耗只记录整机曲线、平均/P95/峰值和能量，不执行组件、UID、Wakelock、前台应用能量或
  第三方任务功耗归因。

性能模式的 **帧率数据流与渲染链路** 按应用产帧、渲染/队列、系统合成、屏幕扫描顺序
展示所有已检测来源。每个阶段会标记为 **主数据、有效、仅参考、无效或无数据**：

- Android 原生游戏优先把目标 SurfaceView/BLAST 层的 SurfaceFlinger present 时间戳标为
  主 FPS；`gfxinfo` UI 提交速率仍保留，但不会冒充游戏呈现帧率。
- HarmonyOS 优先使用绑定目标进程的 SmartPerf 应用 FPS；RenderService 合成窗口作为
  合成阶段数据或回退主数据。
- 屏幕刷新率和 `fpsCount` 只代表显示扫描/档位驻留，始终标为参考数据；未绑定目标图层的
  全局合成器采样、静止不增长的累计计数会明确标为无效，并显示不能采用的原因。
- `gfxinfo framestats` 或 SmartPerf 抖动可提供渲染阶段延迟，即使没有独立 FPS 也会沿链路
  展示，避免为了填充阶段而推算不存在的帧率。

采集面板默认只显示测试时长和目标应用等必要项。下面的命名、目录、采样周期、开始场景、
监控开关和抓取项目均使用当前模式的默认值，需要调整时再展开 **更多采集设置**。

- **报告标题**：便于阅读，例如“Phone A 一小时 BTR2 续航”。
- **运行目录名**：建议包含设备、版本和轮次，例如
  `phone-a-build-123-round-01`。
- **时长**：功耗测试默认 `3720` 秒，适合一小时 BTR2 流程；先启动功耗采集并等待
  界面出现首个实时样本，再启动 BTR2，额外两分钟用于覆盖启动准备和两边的时间偏差。
  性能测试默认 `1920` 秒，并提供 `1800` 秒（30 分钟）和 `1920` 秒（32 分钟）快捷项；
  32 分钟可覆盖游戏启动、进入场景和约半小时稳定测试窗口。
- **采样间隔**：Android、HarmonyOS 与 iOS 性能模式默认 `1` 秒；iOS 功耗模式默认
  `5` 秒，以降低 DVT/sysmond 的观察者开销。
- **目标游戏 / 应用**：性能测试必填。Android 先选择设备，再点击 **扫描手机应用**；
  列表优先展示带桌面启动入口的第三方应用，可按包名或 Activity 搜索。若旧系统不支持
  启动入口查询，界面会回退到第三方包列表；扫描失败时仍可手工输入。功耗测试中的目标
  包名仍为可选，多应用 BTR2 流程可留空并启用 session mode；性能模式不提供多应用
  会话，始终围绕已选择的具体游戏或应用采集和分析。
- **设备亮度（Android）**：选择在线设备后，界面会读取当前整数亮度，并提示该机的
  最小值、最大值和最小调整间隔。输入整数后可直接应用；程序同时写入系统亮度并调用
  DisplayManager 立即刷新。若设备处于自动亮度，应用时会切换为手动亮度。不同厂商的
  整数范围可能是 `0–255`、`0–1023` 或更高，应以界面实测提示为准；本次验证的 vivo
  V2458A 为 `0–255`、最小间隔 `1`（归一化约 `0.00392`）。采集期间亮度控件会锁定，
  防止中途改变测试条件。
- **屏幕热降亮监控（Android）**：开启“温度 / 热限制”采集时，程序会把系统设定亮度、
  DisplayManager 请求/有效亮度、`BrightnessThermalClamper` 上限以及 Thermal HAL 的
  `lcd-backlight` 冷却档位分开记录。正常状态下显示侧详情约每 30 秒探测一次；发现显示
  热限制或背光冷却活动后，改为跟随热状态周期持续追踪，并额外保留恢复点。实时曲线和
  报告曲线会用橙色标出“疑似”、红色标出“确认”的每个采样点，报告同时逐点列出时间、
  设定值、估算有效档位、热上限、温度、前台应用和判定证据。屏幕明确处于 OFF/DOZE 时
  不会按降亮处理。
- **开始场景**：通常选择“桌面”。
- **开始说明**：例如“功耗先启动，BTR2 预计 2 分钟后开始”。
- **要求断开外部供电**：功耗和性能模式均默认开启，避免充电状态改变功耗与热表现；
  只做连接或功能验证时可以手动关闭。关闭后外供区间只保留原始通道，不生成耗电、
  能量、续航或归因结论。
- **专项诊断**：全系统进程、热点线程、调度和功耗归因默认关闭；只有明确的问题假设需要时再开启。

采集预设和逐项开关用于控制测试工具自身的干扰，和“功耗 / 性能”分析模式是两个
维度：

| 预设 | 适用场景 | 默认侧重 |
|---|---|---|
| 跟随测试模式 | 日常使用 | 自动选择功耗标准或性能标准 |
| 功耗标准 | 续航与电流原因分析 | CPU/GPU、核心频率、前台/显示、温度和测试前后设置 |
| 性能标准 | Android/HarmonyOS 常规帧性能 | 帧率、CPU/GPU、核心频率、前台/显示和温度；详细帧专项默认关闭 |
| 低干扰 | 只保留必要观测 | 基础 CPU/前台；性能模式另保留帧率和温度 |
| Harmony SmartPerf 采集 | HarmonyOS 游戏性能 | 使用 `SP_daemon` 的应用 FPS/抖动、CPU/GPU、可用时的 DDR 和温度 |

展开 **更多采集设置 → 选择需要抓取的数据** 后，可以逐项关闭 CPU/GPU/内存频率、
前台窗口、帧率、详细帧、触控、目标进程、全系统进程、热点线程、温度、调度、设置
快照和功耗归因。关闭项目后，程序会尽量跳过对应命令、快照和分析；基础电流、电压和
设备时间戳始终保留。详细帧依赖帧率、热点线程依赖进程上下文，界面会自动处理这些
依赖。内存频率在所有标准预设中默认关闭；只有手工开启且设备向 ADB/HDC shell 公开
可读的 DMC/DRAM/MIF 节点时，才会出现在实时数据和报告中。性能模式会强制关闭功耗
来源归因；HarmonyOS 不支持的 Android 设置快照、
BatteryStats/UID/Wakelock 归因和当前不可用的全系统热点线程会显示为禁用。最终报告的
**采集项与干扰控制** 表格会记录实际启用状态、关闭原因和预计干扰等级。

#### HarmonyOS SmartPerf 与设备高性能模式

这两个选项相互独立，不能混为同一个“性能模式”：

- **Harmony SmartPerf 采集**是数据来源。它调用设备 `/bin/SP_daemon`，按设备原生约
  1 秒节奏采集目标应用 FPS、帧抖动明细、目标 PID CPU/PSS、逐核 CPU 频率/利用率、
  GPU 频率/负载、DDR 频率、温度、电流和电压。只能在 HarmonyOS 的**性能测试**中
  选择，并需要当前前台游戏或明确填写包名。
- **设备高性能模式**是设备状态变更。启用后，测试开始时执行
  `power-shell setmode 602`，用于拉高机器性能策略、查看能力上限；它不会自动打开
  SmartPerf，也不会增加新的采集指标。默认关闭。

建议按目的选择：

- 测正常用户状态的帧表现：选择 SmartPerf，但不打开设备高性能模式。
- 测机器能力上限：选择性能测试，可同时选择 SmartPerf 并打开设备高性能模式。
- 比较高性能模式收益：正常模式和 602 模式分别独立跑一轮，固定场景、温度、亮度、
  分辨率和刷新率，不要在同一轮中途切换。

602 通常会明显提高功耗和温度，也可能更快进入热限制。程序会先读取原模式（量产机
常见为 600），测试期间切换到 602，并在正常结束、手动停止或异常退出时尝试恢复原
模式；报告会记录原模式、是否成功进入 602、结束后的模式和恢复状态。若报告显示恢复
失败，应立即执行 `power-shell setmode --help`，检查输出中的 `current mode is`，并手动
执行原模式对应的 `power-shell setmode MODE`。

USB 连接可用于验证 SmartPerf、帧率和 602 切换是否工作，但 USB 供电会改变电池侧
电流。此类短测不能作为正式整机功耗或续航结论。需要同时比较整体功耗时，应使用无线
HDC、拔掉 USB，并确认 BatteryService 为放电状态。

默认系统监控频率已经针对一小时测试控制了 ADB 开销：

| 数据 | 默认周期 |
|---|---:|
| 电流、CPU 整体负载 | 1 秒 |
| Android / Harmony SmartPerf CPU/GPU 频率 | 约 1 秒 |
| HarmonyOS 原生 `hidumper --cpufreq` 核心组频率 | 30 秒；中间样本保留最近值 |
| Android 电压 | 5 秒；HarmonyOS 原生与 SmartPerf 随主采样返回 |
| 前台应用、屏幕、刷新率和平台帧统计 | 5–10 秒 |
| Android SurfaceFlinger 刷新档位累计时长 | 30 秒并在结束时补一次 |
| 整机进程、重点后台活动 | 10 秒 |
| 热点线程、GC/kworker/内核分类 | 30 秒 |
| Android / HarmonyOS 原生 ThermalService | 功耗模式 10 秒；性能模式 5 秒 |
| Harmony SmartPerf 温度 | 随 SP_daemon 主流固定约 1 秒 |
| cpuset、进程调度状态和 ADPF | 30 秒 |

性能模式默认以 1 秒采集电流与 CPU/GPU 资源、以 2 秒采集前台窗口与 gfxinfo 上下文；检测到
原生游戏的前台 SurfaceView/BLAST 图层后，会额外每 0.5 秒读取一次 SurfaceFlinger
呈现时间戳环，避免 120 FPS 下短环形缓冲区覆盖未读取帧。确认该时间戳通道可用且详细
framestats 未开启后，后续上下文不再重复执行 gfxinfo 汇总；实时 1% Low 使用最近约
10 秒呈现帧间隔滚动计算，积累不足约 8 秒时先留空，全会话报告仍按全部有效帧重算。
进程、热点线程、
ThermalService 和调度快照分别为 2 / 5 / 5 / 5 秒；HarmonyOS 原生 CPU 核心组频率
单独降为约 30 秒一次，因为完整 `hidumper --cpufreq` 命令本身较重。较高频率会带来额外 ADB/HDC 和
dumpsys 开销，因此性能模式页面会明确展示数据来源和置信度。

### 7.4 开始和结束

点击 **开始采集** 后，UI 会实时展示：

- 电流、电压、实测电池侧功率和累计能量。顶部显示“当前电池放电功率”，测试窗口
  汇总明确标为“平均电池侧功率”；检测到 USB、AC 或无线充电时当前值改为“当前电池
  充入功率”，并展开 `电流 × 电压` 说明。黑屏只会
  降低设备负载，不会让快充输入归零，因此充电状态下的 10～20 W 以上数值不能解释为
  黑屏待机功耗，也不会用于续航压力来源归因。
- CPU 总负载、各集群频率和可用的 GPU 信息。
- 前台包名、Activity/窗口、屏幕、当前刷新率，以及平台可提供的帧率/帧耗时指标。
  Android 原生游戏优先显示 SurfaceFlinger BLAST 图层的呈现 FPS、1% Low、P95/P99
  帧间隔和跨帧预算比例，普通 View 应用回退 `gfxinfo` UI 帧提交速率、帧耗时与
  deadline miss；HarmonyOS 显示抽样合成器 FPS、P95 帧间隔和跨刷新槽位比例。
- 会话内刷新档位驻留、触摸交互次数、分辨率/亮度、GPU renderer 和最高温度。
- Android 实时图中的全部疑似/确认热降亮点，以及当前系统设定亮度到显示侧有效档位的
  变化；历史点在亮度恢复后仍会保留到本轮测试结束，并进入离线报告。
- Android / HarmonyOS 的 CPU 调度历史趋势，包括 top-app/foreground 可调度 CPU 数、
  目标进程调度组和 ADPF/图形管线会话数；关闭“调度 / ADPF”采集项时整个面板隐藏。
- 当前前 5 个 CPU 热点；后台更新/安装/编译或热状态异常时才显示干扰告警。
- 采集器日志、检查点、ADB/HDC 断线和恢复状态。

性能模式中的 **1% Low** 对原生游戏使用 SurfaceFlinger 呈现帧间隔最慢 1% 的平均
耗时换算；普通 View 应用使用 gfxinfo 会话内帧耗时直方图，直方图不可用时才退化为
采样窗口的 1 分位帧率。**渲染分辨率**只接受前台 BLAST SurfaceView 对应的
SurfaceFlinger GraphicBuffer 尺寸；WindowManager 窗口边界只代表显示窗口，不再冒充
游戏内部渲染尺寸。**插帧 / MEMC** 只有读取到与当前应用或当前游戏进程匹配的厂商
显式状态时才显示；设备能力属性、白名单或“120 Hz 显示 + 60 FPS 应用”不会单独形成
开启结论，因为这些证据无法证明插帧已在当前游戏生效。

Android 的详细帧时间戳属于高开销专项采集，性能标准预设默认关闭；手工开启后才会读取
新观察到的 `gfxinfo framestats`，用于计算渲染阶段平均/P95/P99 和慢帧明细。性能测试项按导入的持续事件或前台 Activity 区间汇总 FPS、
1% Low、P95/P99、异常帧、主要延迟阶段、CPU/GPU/内存资源、调度与热限制；测试项中的
功率列仅表示同期整机均值。

报告会把 APP → RENDER → COMPOSITOR → DISPLAY 数据源按有效性可视化，并用帧间隔
直方图展示帧预算、预算边缘和慢帧长尾。没有有效样本的阶段耗时、慢帧、热点线程、
进程快照或 GPU 页面不会显示空表，而是集中记录在 **数据质量 → 分析覆盖与省略项**。
实时页面中的 GPU 曲线入口、资源调度行和性能上下文行同样以实际样本为准：候选节点或
renderer 已识别但整轮没有读到有效 GPU 频率/负载时会自动隐藏，不再长期显示 `--`。
功耗模式的资源压力页使用正负相关条形图展示 CPU/GPU/温度等指标与整机功率的同期
关系；相关性仅用于筛选压力线索，不表示因果或独立电源轨功耗。

到达设定时长会自动收尾。也可以点击 **停止并收尾**，程序会保存已经采到的数据并
生成部分报告，不会简单丢弃运行目录。

### 7.5 使用 AI 自动化完成 ADB 闭环任务

左侧 **AI 自动化** 是当前独立于功耗采集的基础功能块，只支持 Android ADB。它使用
统一的多模态模型适配层，不依赖 BTR2 的 Python 代码、相机或机械臂。当前支持：

- OpenAI-compatible Chat Completions：OpenAI、Azure 完整 deployment URL、局域网
  vLLM/Ollama，以及提供兼容接口的千问、GLM、InternVL、Llama Vision 等模型服务；
- Anthropic Messages API：原生图像块和 `tool_use`；
- Google Gemini `generateContent`：原生内嵌图像和函数调用。

Agent、system prompt、任务编排与 ADB 执行器不感知具体模型供应商。各适配器只负责把
统一的截图、文本和 `phone_action` schema 转换成供应商协议，再把工具调用结果还原成
同一个动作结构。

使用前确认：

- 右上角已经选择一台处于 `device` 状态的 Android 手机。
- 运行 Mobile Profiler 的电脑可以访问所选多模态模型服务器。
- 暂时默认使用局域网 OpenAI-compatible 千问服务：地址
  `http://192.168.31.237:8000`，模型 `qwen3.6-27b`。这只是默认配置，不是功能绑定。
- 可以在 UI 中切换 OpenAI-compatible、Anthropic Claude 或 Google Gemini，并修改
  API 地址、模型名称、API Key 和认证头。
- 也可以在启动 UI 前设置 `MOBILE_PROFILER_MODEL_PROVIDER`、
  `MOBILE_PROFILER_MODEL_ENDPOINT`、`MOBILE_PROFILER_MODEL_NAME` 和
  `MOBILE_PROFILER_MODEL_API_KEY`。旧的 `BTR2_LLM_PROVIDER`、
  `BTR2_LLM_ENDPOINT`、`BTR2_LLM_MODEL`、`BTR2_LLM_TOKEN` 继续兼容。

操作步骤：

1. 进入 **AI 自动化**。
2. 填写流程名称，并按执行顺序添加一个或多个任务卡。可以选择内置模板，也可以添加
   空白任务；任务卡支持上移、下移和删除。
3. 为每个子任务填写清晰、可从屏幕判断是否完成的目标，并按需填写注意事项。例如先
   “打开系统设置”，再“关闭自动亮度并把亮度调整到约 50%”。
4. 分别设置每个任务的最大步骤、任务超时和失败策略：
   - **停止流程**：超时或步骤耗尽后，整个流程以 `task_failed` 结束。
   - **记录并继续**：记录失败结果并开始下一项，最终状态为
     `completed_with_warnings`。
5. 选择模型协议，核对模型 API、模型名称和 API Key。OpenAI-compatible 默认使用
   `Authorization: Bearer`，Azure 可选择 `api-key`；Anthropic 和 Gemini 会分别使用
   `x-api-key` 与 `x-goog-api-key`。本地无鉴权服务可以留空 Key。
6. 核对动作后等待和单次模型请求超时。
7. 如需适配特殊 ROM 或测试制度，展开 **ADB Agent System Prompt** 编辑全局规则；
   随时可以点击 **恢复默认** 回到内置版本。
8. 点击 **启动测试流程**。
9. 观察右侧最新 ADB 截图、当前子任务、子任务内步骤、模型判断、`phone_action`、
   逐项结果和闭环日志。
10. 模型调用 `finish` 时只完成当前子任务，编排器会继续下一项；调用 `take_over` 时
   整个流程停止并提示人工接管。任何时候都可以点击 **停止流程**。

内置任务模板当前包括：

- 回到桌面；
- 打开系统设置；
- 亮度初始化示例；
- 当前应用只读浏览烟测。

模板是可编辑起点，不是硬编码脚本。添加后仍应按设备厂商、系统版本和测试目标检查
任务目标、注意事项与安全边界。

UI 最终调用的编排接口为 `POST /api/ai-agent/start`，核心载荷如下，方便后续初始化
工具或外部流程生成任务卡：

```json
{
  "device": "ADB_SERIAL",
  "workflow_name": "续航测试前初始化",
  "system_prompt": "完整的 ADB Agent system prompt",
  "model_provider": "openai_compatible",
  "api_base_url": "http://192.168.31.237:8000",
  "model": "qwen3.6-27b",
  "api_key_mode": "bearer",
  "tasks": [
    {
      "id": "open-settings",
      "name": "打开系统设置",
      "prompt": "打开 Android 系统设置并确认进入主页面。",
      "attention_prompt": "不要修改任何设置。",
      "max_steps": 6,
      "timeout_s": 60,
      "on_failure": "stop"
    }
  ]
}
```

兼容旧版单任务载荷 `{ "task": "...", "max_steps": 30 }`；服务端会把它归一化成
一个子任务。新流程应优先使用 `tasks` 数组。

每一步的实际数据流是：

```text
adb exec-out screencap -p
  -> 全局 ADB system prompt
  -> 当前子任务 + 注意事项 + 已完成任务摘要
  -> 截图 + 最近动作结果
  -> OpenAI-compatible / Anthropic / Gemini 协议适配器
  -> 供应商原生工具或函数调用
  -> 0～999 归一化坐标的 phone_action
  -> 服务端校验并换算为当前手机像素坐标
  -> adb input / keyevent / monkey
  -> 下一张截图
```

安全边界：

- 模型不能下发任意 `adb shell`。服务端只接受点击、双击、长按、滑动、固定
  keyevent、可打印 ASCII 文本、合法包名启动、等待、完成和人工接管。
- 验证码、账号授权、支付、删除数据、发送消息等敏感或不可逆步骤应进入人工接管。
- 基础闭环的 `input_text` 只保证可打印 ASCII；中文输入法控制后续单独适配。
- API Key 只保留在当前内存请求配置中，不进入 `/api/state`，也不会写入运行目录。
- 不要把 Key 写成 URL 的 `key`、`token` 等查询参数；服务端会拒绝这种地址，避免密钥
  随 `api_base_url` 进入状态或 `config.json`。
- 全局 system prompt 会写入本次运行的 `config.json`，便于复现实际决策协议；不要在
  prompt 或任务文本中填写密钥、验证码或其他敏感数据。

全局 system prompt 默认明确以下协议：

- ADB 截图是包含状态栏和导航栏的完整帧缓冲，工具坐标统一归一化到 `0～999`；
- 每轮只允许一个 `phone_action`，执行后必须通过下一张截图验证结果；
- `finish` 只结束当前子任务，`take_over` 结束整个编排；
- 通知权限默认拒绝；敏感权限、登录凭证、验证码、支付、删除/发送/提交等外部副作用
  操作进入人工接管；
- 同一动作连续无效时必须换方式，禁止无限点击或无限滚动。

每次 Agent 运行会保存到：

```text
profiler-runs\agent-runs\时间-流程-随机后缀\
  config.json
  events.jsonl
  task-01-step-001.png
  task-01-step-002.png
  task-02-step-001.png
  ...
```

`events.jsonl` 同时记录 `task_start`、`action` 和 `task_end`，动作事件包含子任务编号、
子任务内步骤、全流程步骤、截图文件、模型判断、动作结果、请求耗时和 token 用量。

当前版本尚未把 Agent 的开始/结束和动作自动写入功耗测试时间线，也没有外部 API
工具。先通过本功能块验证 system prompt、任务编排、截图、模型决策和 ADB 执行闭环；
后续再把同一控制器接入功耗会话与外部服务。新增手机初始化流程时，优先增加任务模板
或任务卡组合，不要扩大为模型可任意执行的 shell。

## 8. 与 BTR2 不同步开始时怎么做

对于功耗采集与日志对齐，Mobile Profiler 和 BTR2 仍是两个独立进程；上一节的 AI
自动化使用独立多模态模型 API；即使默认连接 BTR2 局域网千问，也不改变两边的采集
生命周期：

```text
Android 手机  <-- Wi-Fi ADB -->+
HarmonyOS 手机 <-- Wi-Fi HDC -->+--> Mobile Profiler --> 原始数据和报告
      ^
      |
机械臂/相机
      |
     BTR2 ---------------------------------------> 带绝对时间戳日志
```

它们不需要同时启动。推荐：

1. 手机回到桌面。
2. 先启动功耗采集，开始场景选择“桌面”。
3. 在开始说明中记录预计的 BTR2 启动方式。
4. BTR2 真正开始时，在 UI 点击 **添加时间标记**，写入“BTR2 开始”。
5. BTR2 继续按正常流程运行。
6. 测试结束后导入带主机绝对时间戳的 BTR2 日志。

程序使用采集期间保存的“主机绝对时间 ↔ 设备 uptime”同步点，将外部日志对齐到
手机采样时间线。手工标记直接记录设备 uptime，可作为额外核对锚点。

## 9. 使用“工具与交付”页面

所有路径都是“运行 UI 的这台电脑”上的本机路径。浏览器为了安全不会向网页暴露
任意文件的完整路径，因此大日志采用路径输入，而不是上传到浏览器内存。

### 9.1 导入 BTR2 日志

填写：

- **运行记录**：要关联的手机采集目录。
- **BTR2 日志绝对路径**：例如 `D:\BTR2\logs\round-001.log`。
- **规则 JSON 路径**：默认使用项目内
  `examples\btr2-log-rules.json`。
- **多手机过滤**：混合日志可填写 `phone_key=phone1`，每行一个条件。
- **Replace**：建议保持启用；替换以前导入的外部事件，但保留 UI 手工标记。

点击 **导入并重建报告** 后，程序会：

1. 用规则匹配日志中的阶段、动作和状态。
2. 转换到设备 uptime。
3. 保存 `events.jsonl`。
4. 把原始日志和规则复制到 `attachments\btr2\`。
5. 重新运行分析并生成 `report.html`。

BTR2 日志必须含可解析的年月日、时分秒绝对时间。只有“从测试开始后的第 N 秒”而
没有绝对起点的日志不能自动可靠对齐，除非先转换或提供明确的绝对时间锚点。

### 9.2 报告重建

分析算法、中文名称或报告界面更新后，选择旧运行并点击 **重建报告**。程序会使用
原始数据重新生成：

- `samples.csv`
- `analysis.json`
- `report.html`
- `report-fragment.html`

不需要重新连接手机。

### 9.3 中断运行恢复

终端关闭、电脑异常退出或采集未正常收尾时，选择运行并点击 **恢复中断运行**。
恢复从 `raw\sampler-stream.txt`、检查点和现有系统快照重新构建结果，并把状态标记为
`recovered`。

正在采集的目录不能恢复。应先停止当前采集，或确认原采集进程已经结束。

### 9.4 证据归档

选择运行，可选填额外文件或目录，每行一个路径，然后点击 **生成证据包**。

默认输出到：

```text
profiler-runs\archives\运行名-evidence-时间戳.zip
```

证据 ZIP 包含完整运行目录、可选附件和 `evidence-manifest.json`。清单记录每个文件
的大小和 SHA-256，适合交付给测试人员或 AI 做后续综合分析。

历史报告页中的 **打包原始数据** 是无额外附件的快捷入口。

### 9.5 双机或两次运行比较

先确保两条运行都已经：

- 完成采集或恢复。
- 导入各自的 BTR2 日志。
- 使用相同或可配对的测试项名称/`comparison_key`。

在双机比较表单中选择 Run A 和 Run B，填写标签，点击 **生成对比报告**。输出到：

```text
profiler-runs\comparisons\对比名称\
|-- comparison.json
`-- comparison.html
```

报告展示 B 相对 A 的功率、单位时间能量、CPU、温度、GC、kworker、DEX/更新活动和
系统干扰差异。结果仍需结合起始电量、环境温度、网络、亮度、版本等条件判断。

### 9.6 重新构建软件

该功能只在源码工程模式启用，详见下一节。为防止打包占用主机资源或交付到一半的
代码，手机采集运行期间不能从 UI 构建便携包。

## 10. 软件修改后的重新打包流程

这是后续每次修改软件后的标准流程。

### 10.1 只在源码工程中修改

需要保留这些目录和文件：

```text
pyproject.toml
src\mobile_profiler\
tests\
tools\build-portable.ps1
build-portable.bat
```

不要直接修改已经交付的便携包，再把它当作新源码。便携包没有完整的开发、测试和
构建结构。

### 10.2 更新版本号（正式交付时）

如果此次修改需要新的版本标识，同时更新：

```text
pyproject.toml                         [project].version
src\mobile_profiler\__init__.py  __version__
```

仅在本地调试小改动时可以暂不改版本，但正式分发建议更新。

### 10.3 运行测试

在源码根目录：

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

如果修改了 Web UI，并且电脑安装了 Node.js，再补充：

```powershell
node --check src\mobile_profiler\web\app.js
```

用 Demo 做一次不连接手机的界面检查：

```powershell
.\start-ui.bat --demo
```

### 10.4 生成新包

两种方式调用同一个构建脚本，结果一致。

UI：

```text
工具与交付 → 重新构建 Windows 便携包 → 生成新版便携包
```

命令行：

```powershell
.\build-portable.bat
```

构建脚本会替换源码工程中默认的：

```text
dist\mobile-profiler-v1.0.0-portable\
dist\mobile-profiler-v1.0.0-portable.zip
```

不会删除 `profiler-runs`、`build` 或源码目录。

### 10.5 验证新包

```powershell
.\dist\mobile-profiler-v1.0.0-portable\profiler.cmd --help
.\dist\mobile-profiler-v1.0.0-portable\start-ui.bat --demo
```

如果需要包内 ADB，再检查：

```powershell
Test-Path .\dist\mobile-profiler-v1.0.0-portable\platform-tools\adb.exe
```

确认后分发：

```text
dist\mobile-profiler-v1.0.0-portable.zip
```

### 10.6 升级目标电脑

推荐把新版本解压到新目录，不要覆盖正在使用的旧目录。测试数据建议始终放在独立
目录，例如：

```text
D:\MobileProfilerData\profiler-runs
```

新旧版本都用同一个外部数据根目录：

```powershell
.\start-ui.bat --output-root D:\MobileProfilerData\profiler-runs
```

旧数据可以在新版本中通过 **重建报告** 升级分析和报告格式。升级软件前仍建议先为
重要运行生成证据 ZIP。

## 11. 如何阅读报告

### 11.1 自动完成的分析

报告可以自动完成：

- 电池侧功率、电流、电压、能量和数据覆盖率统计。
- CPU 总负载、各核心/集群频率和频率驻留影响。
- 可用的整机 GPU 频率/负载，或 `dumpsys gpu` UID work duration 旁证。
- 前台应用、Activity/窗口、屏幕、亮度和刷新率上下文。
- Android SurfaceFlinger 刷新档位累计时长差值、前台 SurfaceView/BLAST 呈现 FPS、
  1% Low 与帧间隔；普通 View 应用的 `gfxinfo` UI 帧提交速率、帧耗时分布和
  deadline miss；以及 Mali/Adreno renderer 和触摸轴/触点能力。
- HarmonyOS 刷新档位驻留、抽样合成器 FPS、平均/P95 帧间隔、跨刷新槽位慢帧、
  触摸交互次数和 GPU renderer。
- 整机前后台进程与线程快照，包括应用、Android 系统服务、原生 Linux 服务和可见
  的内核任务。
- ART/GC、`kworker`、RCU、IRQ/softirq、内存回收、存储、显示合成、热控/电源
  worker 等活动分类。
- `dex2oat`、`dexopt`、`artd`、`installd`、`profman`、`odrefresh`、
  `otapreopt`、`update_engine` 和 `apexd` 的重点监控。
- ThermalService 传感器和平台调度原始快照仍保留；默认报告仅在热严重度、明显温升或
  后台干扰成立时突出结论。
- 按前台应用、BTR2 阶段、测试项和五分钟窗口分配整机实测能量。
- GC、kworker、DEX/更新活动与同时间窗功率、CPU 和温度的关联。

### 11.2 仍适合由 AI 协助的分析

自动报告给出可审计的证据和异常候选，但这些问题通常需要结合 BTR2、系统版本和
测试背景进一步判断：

- 某次功耗升高是测试动作本身，还是后台系统活动造成。
- 两台手机差异是否由条件不一致、热控状态、系统更新后优化或应用行为造成。
- GC、kworker 或 DEX 活动与功耗峰值是相关、可能贡献，还是仅仅同时发生。
- 多份 BTR2、相机、系统日志和两台手机报告之间的综合因果链。
- 哪些异常值得复测，复测时应控制哪些变量。

建议交付给 AI 的材料：两条完整证据 ZIP、BTR2 原始日志、测试版本信息、环境条件
和任何人工观察。不要只交付一个截图或单独的 `report.html`。

### 11.3 报告页面重点

- **概览**：平均/P95/峰值功率、能量、覆盖率和主要告警。
- **时间线 / Flow**：功率、前台应用和测试项与可用的平台干扰证据对齐。
- **测试项**：适合一小时多应用流程，逐项查看功率、CPU/GPU、温度和后台干扰。
- **应用**：按前台时间分配的整机实测能量。
- **CPU / GPU**：系统负载、频率和可用 GPU 证据。
- **性能上下文**：刷新率驻留、抽样 FPS/帧间隔、触控交互、显示/窗口/GPU/温度，
  以及精简后的后台异常和前 5 个 CPU 热点。
- **数据**：原始来源、采样间隔、缺口和可观测边界。

## 12. 原始数据和报告保存在哪里

典型运行目录：

```text
profiler-runs\phone-a-round-01\
|-- metadata.json
|-- checkpoint.json
|-- clock-sync.jsonl
|-- samples.csv
|-- context.csv
|-- system-snapshots.jsonl
|-- thermal-snapshots.jsonl
|-- scheduler-snapshots.jsonl
|-- events.jsonl
|-- analysis.json
|-- report.html
|-- report-fragment.html
|-- attachments\btr2\
|   |-- 原始 BTR2 日志
|   `-- 导入规则 JSON
|-- raw\
|   |-- sampler-stream.txt
|   `-- 其他采集器原始输出
`-- artifacts\
    `-- BatteryStats、CPU/GPU、显示、热控等证据
```

重要原则：

- `raw\sampler-stream.txt` 是中断恢复的主要事实来源。
- `clock-sync.jsonl` 是 BTR2 绝对时间对齐的依据。
- `analysis.json` 是自动分析结果，`report.html` 是展示层。
- 交付或归档时保留整个目录，最好生成证据 ZIP。

## 13. 两台手机对比的建议顺序

1. 为两台手机制定相同的运行名称规则和测试条件。
2. 分别完成 Probe 和一小时采集。
3. 分别导入对应 BTR2 日志。
4. 如果一份日志包含两台手机，分别使用
   `phone_key=phone1` 和 `phone_key=phone2` 过滤。
5. 分别生成证据 ZIP。
6. 在 UI 生成双机对比报告。
7. 将两份证据 ZIP、对比目录和测试条件说明一起交给 AI。

对比结论优先看同一测试项的功率和单位时间能量，不要只比较整小时总能量；测试项
持续时间不同会直接影响总能量。

## 14. 常见问题

### 14.1 UI 找不到设备

```powershell
adb devices -l
adb disconnect PHONE_IP:PORT
adb connect PHONE_IP:PORT
```

确认手机显示 `device`，然后在 UI 点击刷新。

### 14.2 Probe 显示 `powered: ['AC']` 或 `USB`

手机仍在外部供电。拔掉 USB/充电器，等待 BatteryService 状态更新后重新 Probe。

### 14.3 电流为 0 或不支持 `current_now`

不同 OEM 对 fuel gauge 的公开程度不同。报告会保留能力告警。没有可靠电池电流时，
不能把 CPU 模型值当作整机实测功率替代。

### 14.4 输出目录非空

新采集必须使用新的运行目录名。旧目录需要重建时使用 **工具与交付 → 重建报告**，
不要再次启动 record 写入同一目录。

### 14.5 BTR2 导入为 0 个事件

依次检查：

1. 日志是否含绝对日期和时间。
2. 规则中的时间格式和正则是否匹配当前日志。
3. 日志时间是否落在手机采集时间范围内。
4. `FIELD=VALUE` 过滤是否把事件全部排除。
5. 查看规则文件和原始日志编码是否为 UTF-8 或可正常读取的文本编码。

修改规则后保持 Replace，再次导入即可。

### 14.6 报告存在 ADB gap

缺口不会被伪造插值为实测功耗；报告会降低覆盖率，并从能量积分中排除过长间隔。
检查 Wi-Fi、手机休眠、无线调试端口变化和主机网络稳定性。

### 14.7 报告提示 dex2oat / update_engine 活动

这表示测试窗口内存在系统更新、应用优化或运行时编译证据。优先查看与功率峰值的
重叠时间，必要时让手机联网充电并静置到后台优化结束，再按相同条件复测。

### 14.8 便携打包按钮不可用

当前 UI 是从便携包启动，或源码结构不完整。回到包含 `pyproject.toml`、`src` 和
`tools\build-portable.ps1` 的源码工程启动 UI。

### 14.9 便携构建下载失败

检查网络或按第 4.3 节下载官方 Embedded Python ZIP，使用 `-PythonEmbedZip`。

## 15. 命令行高级用法

UI 已覆盖日常工作流。以下命令适合脚本化或排查：

```powershell
# 查看设备能力
mobile-profiler probe --device SERIAL

# 采集一小时
mobile-profiler record --device SERIAL --duration 3720 `
  --session-mode --require-unplugged `
  --start-context desktop --start-note "BTR2 starts later" `
  --output profiler-runs\round-001

# 导入 BTR2 日志
mobile-profiler import-log RUN_DIR LOG_FILE `
  --rules examples\btr2-log-rules.json --replace `
  --match phone_key=phone1

# 重建报告
mobile-profiler report RUN_DIR

# 恢复中断运行
mobile-profiler recover RUN_DIR

# 生成证据包
mobile-profiler archive RUN_DIR --attach EXTRA_LOG --output RUN-evidence.zip

# 双机比较
mobile-profiler compare RUN_A RUN_B `
  --label-a "手机 A" --label-b "手机 B" `
  --output profiler-runs\compare-a-vs-b

# 生成离线 Demo 报告
mobile-profiler demo --output profiler-runs\demo
```

正式命令行采集默认要求外部供电已断开，等同于显式传入 `--require-unplugged`。仅做连接、
功能或充电场景诊断时，可以显式传入 `--allow-external-power`；这类区间只保留原始通道，
不会生成耗电、能量、续航或归因结论。

全局 `--adb PATH`、`--hdc PATH` 和 `--ios-python PATH` 必须放在子命令前：

```powershell
mobile-profiler --adb D:\platform-tools\adb.exe probe --device SERIAL
mobile-profiler --hdc D:\DevEco\hdc.exe probe --platform harmony --device harmony:PHONE_IP:8710
```

HarmonyOS 游戏性能与能力上限示例：

```powershell
$hdc = "C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"

mobile-profiler --hdc $hdc record `
  --platform harmony `
  --device harmony:PHONE_IP:8710 `
  --test-mode performance `
  --capture-preset harmony-smartperf `
  --package com.miHoYo.Yuanshen `
  --harmony-high-performance `
  --duration 120 `
  --output profiler-runs\harmony-performance-limit
```

`--harmony-high-performance` 只负责临时切换 602；删除该参数即可在正常设备模式下使用
相同 SmartPerf 采集。`--enable-feature NAME` 和 `--disable-feature NAME` 可以重复，
例如 `--disable-feature frame_details --disable-feature process_snapshots`。可用项目名以
`mobile-profiler record --help` 为准。

## 16. 准确性和可观测边界

- 电池功率来自 `|current| × voltage`。放电时表示电池侧净输出；充电时表示电池吸收的
  净充入功率，不等于充电器墙端输入，也不等于设备自身负载。两者都不是单独 CPU、
  GPU、应用或基带电源轨。
- 按应用/阶段/测试项的能量，是把同一时间窗的整机实测能量分配到可见上下文。
- CPU 功率模型和 UID 统计是归因证据，不能与电池总功率相加。
- Android GPU 能力依赖 OEM 是否公开频率、busy/load 节点；不可读时只报告可获得的
  `dumpsys gpu` UID 活跃时长和进程内存旁证，不凭空推断整机 GPU 百分比。高通
  `gpubw` 是 GPU 相关总线而不是 GPU 核心频率，程序会主动排除。
- Android 原生游戏在存在前台 SurfaceView/BLAST 图层时，FPS 来自 SurfaceFlinger
  `--latency` 的实际呈现时间戳；该公开接口仍不等同于游戏引擎内部全部阶段或 HWC
  最终扫描输出。普通 View 应用的 `gfxinfo` 速率是前台窗口 UI 帧提交数的会话差值，
  不是最终可见 FPS；同一
  刷新周期内多次提交或多个渲染目标会让它高于屏幕刷新率，这本身可作为潜在冗余渲染
  和额外 CPU/GPU/合成开销的线索。熄屏时会明确标记帧统计不可用。
- Android 与 HarmonyOS 量产接口通常都不公开面板控制器硬件触控扫描频率；工具只
  展示公开的触摸设备/轴/触点能力和可获得的已分发交互，不用点击频率冒充 240/300 Hz。
- HarmonyOS 平台原生采集路径中的 GPU sysfs 通常仍对 HDC shell 受限，也没有 Android
  `dumpsys gpu` 回退；此时 RenderService 只提供 renderer，不推断频率、负载或应用
  能量。显式选择 Harmony SmartPerf 预设且设备开放 `SP_daemon` 时，可以记录其公开的
  GPU 频率和负载，但这些数据仍不是独立 GPU 电源轨功耗。
- HarmonyOS RenderService 的合成器 FPS 是“最近提交记录”的周期抽样，不是应用内部
  渲染线程 FPS；SmartPerf FPS/帧抖动是目标应用的原生采样，二者来源会在报告中区分。
  MultimodalInput 触摸次数也不是面板硬件触控采样率。
- HarmonyOS RenderService 的 `backlight` 只作为背光原始值记录；其量纲不等同于
  DisplayPowerManager 的亮度档位，也不是 nit 或热限亮上限。熄屏时系统还可能保留该值，
  因此不能把它解释为当前物理出光量；精确亮度仍需亮度计。
- 普通量产机可以观察 Android 和 Linux 暴露的进程/线程，但可能看不到受权限保护的
  完整内核线程细节。
- 可以读取 ThermalService 状态、冷却设备、公开阈值、cpuset 和调度结果；通常无法
  读取 OEM 私有热策略算法、完整 governor/uclamp 参数或受限的 `sched_debug`。
- Android ADB 能确认 Framework / Thermal HAL 已触发的显示限亮，并估算归一化有效亮度；
  如果厂商只在面板或显示驱动私有层执行降亮，软件无法保证得到绝对物理亮度（nits），
  需要外部亮度计才能做精确光学测量。
- GC、kworker、DEX 等活动与功率的时间关联代表“可能贡献”和干扰证据，不等于
  已经完成物理电源轨级因果拆分。
- ADB/HDC 延迟、fuel gauge 更新周期和 OEM 传感器量化都会限制短时峰值精度。一小时级
  续航分析更应关注持续区间、重复性和条件一致性。

## 17. iOS RemotePairing 采集

### 17.1 设计边界

iOS 支持使用独立 sidecar 进程。主程序仍保持标准库运行时，不直接导入
`pymobiledevice3`。这样做有三个目的：

- 不影响现有 Android ADB、HarmonyOS HDC 采集和便携包。
- 将 GPL-3.0-or-later 的可选依赖保持在单独运行时与进程边界。
- 与原生 HarmonyOS HDC 适配共同复用标准化 JSON/JSONL 采样边界。

当前 iOS 支持：

- 电池电量、电压、电流、温度与 DiagnosticsService
  `PowerTelemetryData.SystemLoad` 整机原始 PowerTelemetry 通道。
- DVT sysmontap 的整机/进程 CPU、内存、磁盘计数器和相对 `powerScore`。
- DVT Graphics 的 Device / Renderer / Tiler 利用率。
- Running / Suspended 应用状态通知和设备 Mach uptime 对齐。
- RemotePairing 端点缓存、当前 RemoteXPC 可达性、拔线后 LAN 可达性，以及采集中断后的
  sidecar 重启与日志恢复；三者不会混为同一个“无线就绪”状态。

### 17.2 安装独立 iOS 运行时

在源码电脑执行：

```powershell
py -3.13 -m venv .venv-ios
.\.venv-ios\Scripts\python.exe -m pip install `
  "pymobiledevice3==9.34.0" "pmd-pytcp==0.0.6"
$iosPython = (Resolve-Path .\.venv-ios\Scripts\python.exe)
```

必须使用官方 CPython 3.13 或更高版本。iOS 18.2 及以后已经移除旧 QUIC 隧道，
`pymobiledevice3` 需要 Python 3.13 标准库提供的原生 TLS-PSK 回调。
`pymobiledevice3 9.34.0` 的无 root 隧道仍使用同步 PyTCP 接口，因此还要固定
`pmd-pytcp==0.0.6`；自动解析出的 0.1.x 异步接口与它不兼容。

仓库根目录的 `start-ui.bat` 会校验并自动检测 `.venv-ios\Scripts\python.exe`，也会检查
`.venv-ios313` 和 `%LOCALAPPDATA%\mobile-profiler\ios-python313`。旧的 Python 3.12
或 PyTCP 接口不兼容的 sidecar 会被忽略，不再盲目传给 UI。如果环境位于其他目录，
也可以先设置；该路径同样会接受兼容性校验：

```powershell
$env:IOS_PYTHON = "D:\mobile-tools\ios-python\Scripts\python.exe"
.\start-ui.bat
```

如果 pip 提示本机缓存中的 `hexdump` wheel 权限异常，可先绕过该损坏缓存安装源码包，
再重试上一条命令：

```powershell
.\.venv-ios\Scripts\python.exe -m pip install --no-cache-dir `
  https://files.pythonhosted.org/packages/55/b3/279b1d57fa3681725d0db8820405cdcb4e62a9239c205e4ceac4391c78e4/hexdump-3.3.zip
```

### 17.3 首次 RemotePairing

1. 在 iPhone 打开 Developer Mode，并按系统要求重启确认。
2. 解锁 iPhone，用 USB 连接电脑并选择“信任”。
3. 保持手机和电脑位于同一局域网。
4. 执行：

```powershell
mobile-profiler --ios-python $iosPython ios-pair --json
```

命令成功后会返回 `ios:UDID` 和 RemoteXPC `host:port`。RemotePairing 记录由
`pymobiledevice3` 保存在用户目录中，最近可用端点另存于：

```text
~/.mobile-profiler/ios-devices.json
```

UI 中也可以选择 USB iPhone 后点击顶部 **iOS RemotePairing** 完成同一操作。

Windows 还提供顶部 **连接 iPhone 蓝牙** 按钮，用于不启用 Wi-Fi 的蜂窝网络测试：

1. 先保持 USB 连接并完成一次 **iOS RemotePairing**。
2. 在 iPhone 开启蓝牙和“个人热点”。
3. 首次使用时在 Windows 与 iPhone 两端确认蓝牙配对码；若尚未配对，按钮会打开
   Windows 配对窗口，配对完成后再点击一次。
4. 点击 **连接 iPhone 蓝牙**。程序会连接 Windows 的 Bluetooth PAN“接入点”，读取
   iPhone 网关地址，验证已有 RemotePairing 端口，并更新端点缓存。
5. 拔掉 USB、刷新设备，确认 `wireless_ready` 和 `unplug_ready` 均为 `true`。

蓝牙 PAN 常见地址为电脑 `172.20.10.x`、iPhone 网关 `172.20.10.1`，但程序不依赖
固定地址。它适合遥测采集，带宽和时延通常不如 Wi-Fi。

这里必须区分两个状态：

- `remote_xpc_ready`：当前路由可以连接 RemoteXPC，可用于外供性能测试。
- `unplug_ready`：已经拔掉 USB，并且非链路本地 LAN 端点仍然可达，才可用于默认要求
  断电的功耗测试。

`169.254.0.0/16` 或 IPv6 link-local 地址可能来自 USB-NCM。即使 TCP 当前可连，也不能
据此声称“现在可以拔线”。

### 17.4 拔线 Probe 和采集

RemotePairing 成功后拔掉 USB、刷新设备列表，再执行 Probe：

```powershell
mobile-profiler --ios-python $iosPython probe `
  --platform ios `
  --device ios:00008150-EXAMPLE `
  --json
```

正式采集：

```powershell
mobile-profiler --ios-python $iosPython record `
  --platform ios `
  --device ios:00008150-EXAMPLE `
  --duration 3720 `
  --process-interval 10 `
  --session-mode `
  --require-unplugged `
  --output profiler-runs\ios-btr2-round-001 `
  --title "iOS BTR2 one-hour workflow"
```

全局 `--ios-python` 与 `--adb` 一样，必须写在子命令前。设备标识建议始终保留
`ios:` 前缀，避免自动平台判断把裸 UDID 当成 Android serial。

### 17.5 如何解读 iOS 数据

- `SystemLoad` 是整机 PowerTelemetry 通道，常见刷新周期约 20 秒；外部供电时实机上
  可能接近 `SystemPowerIn`，因此不能称为“电池放电功率”。电池电流×电压是另一条
  电池流量通道。程序用 `power_sample_age_s` 明确 SystemLoad 已保持多久。
- CPU、GPU、进程和应用状态通常以 0.5～1 秒更新，不能因此把 SystemLoad 误解成同样的
  一秒级独立测量；它也不是电池 I×V、独立电源轨或亮度计式实测。
- DVT `powerScore` 是会话内相对诊断分数，不是 mW、J、mWh，也不是单进程电源轨。
- `sysmond`、`DTServiceHub`、`remotepairingdeviced` 会产生可测的观察者开销。报告通过
  `collector_cpu_pct` 和受监控进程行保留这部分证据。
- iOS 功耗模式默认使用 5 秒主采样周期以降低 DVT/sysmond 干扰；性能模式默认 1 秒。
  `collector_cpu_pct` 是相关进程同期 CPU 上界，不是工具造成的净增量。
- iOS 当前没有公开的 Android Power Profile、BatteryStats、cpuset、ADPF 或完整热严重度
  等价接口；相应报告区域会标为不可用，而不会伪造估算。

### 17.6 无线发现问题

拔线后 Bonjour 的 RemotePairing 广播可能消失，但已缓存的 IP/端口仍可能正常接受新
连接。程序会先尝试发现，再检查缓存端点并直接重连。链路本地端点会保守标为不可用于
断电测试；只有拔线后仍可达的非链路本地 LAN 端点才会置为 `unplug_ready`。若路由器
重新分配了 iPhone IP：

1. 重新接入 USB。
2. 保持 iPhone 解锁。
3. 再运行一次 `ios-pair` 或点击 **iOS RemotePairing** 更新端点。
