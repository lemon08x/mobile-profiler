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

完整解压 `mobile-profiler-portable.zip`，然后双击解压目录中的：

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
   `dist\mobile-profiler-portable`。
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
dist\mobile-profiler-portable\
dist\mobile-profiler-portable.zip
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

- **报告标题**：便于阅读，例如“Phone A 一小时 BTR2 续航”。
- **运行目录名**：建议包含设备、版本和轮次，例如
  `phone-a-build-123-round-01`。
- **时长**：功耗测试默认 `3720` 秒，适合一小时 BTR2 流程；先启动功耗采集并等待
  界面出现首个实时样本，再启动 BTR2，额外两分钟用于覆盖启动准备和两边的时间偏差。
  性能测试默认 `1920` 秒，并提供 `1800` 秒（30 分钟）和 `1920` 秒（32 分钟）快捷项；
  32 分钟可覆盖游戏启动、进入场景和约半小时稳定测试窗口。
- **采样间隔**：默认 `1` 秒通常足够。
- **目标游戏 / 应用**：性能测试必填。Android 先选择设备，再点击 **扫描手机应用**；
  列表优先展示带桌面启动入口的第三方应用，可按包名或 Activity 搜索。若旧系统不支持
  启动入口查询，界面会回退到第三方包列表；扫描失败时仍可手工输入。功耗测试中的目标
  包名仍为可选，多应用 BTR2 流程可留空并启用 session mode；性能模式不提供多应用
  会话，始终围绕已选择的具体游戏或应用采集和分析。
- **开始场景**：通常选择“桌面”。
- **开始说明**：例如“功耗先启动，BTR2 预计 2 分钟后开始”。
- **要求断开外部供电**：功耗和性能模式均默认关闭；需要严格拒绝 USB、AC 或无线充电
  状态下启动测试时再手动开启。正式续航结论仍应使用已断开外部供电的设备。
- **性能干扰监控**：建议保持启用；进程和温度只在出现异常时形成主要提示。

采集预设和逐项开关用于控制测试工具自身的干扰，和“功耗 / 性能”分析模式是两个
维度：

| 预设 | 适用场景 | 默认侧重 |
|---|---|---|
| 跟随测试模式 | 日常使用 | 自动选择功耗标准或性能标准 |
| 功耗标准 | 续航与电流原因分析 | 负载、频率、设置、进程/线程、调度和功耗归因 |
| 性能标准 | Android/HarmonyOS 常规帧性能 | 帧率、详细帧、资源分配、进程/线程、温控 |
| 低干扰 | 只保留必要观测 | 基础 CPU/前台；性能模式另保留帧率、目标进程和温度 |
| Harmony SmartPerf 采集 | HarmonyOS 游戏性能 | 使用 `SP_daemon` 的应用 FPS/抖动、CPU/GPU/DDR、目标进程和温度 |

展开 **高级设置 → 选择需要抓取的数据** 后，可以逐项关闭 CPU/GPU/内存频率、
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
| 电流、CPU、CPU/GPU 频率 | 1 秒 |
| 电压 | 5 秒 |
| 前台应用、屏幕、刷新率和平台帧统计 | 5–10 秒 |
| Android SurfaceFlinger 刷新档位累计时长 | 30 秒并在结束时补一次 |
| 整机进程、重点后台活动 | 10 秒 |
| 热点线程、GC/kworker/内核分类 | 30 秒 |
| ThermalService | 10 秒 |
| cpuset、进程调度状态和 ADPF | 30 秒 |

性能模式默认把电流/资源采样调到 0.5 秒、前台窗口与 gfxinfo 上下文调到 2 秒；检测到
原生游戏的前台 SurfaceView/BLAST 图层后，会额外每 0.5 秒读取一次 SurfaceFlinger
呈现时间戳环，避免 120 FPS 下短环形缓冲区覆盖未读取帧。进程、热点线程、
ThermalService 和调度快照分别为 2 / 5 / 5 / 5 秒。较高频率会带来额外 ADB 和
dumpsys 开销，因此性能模式页面会明确展示数据来源和置信度。

### 7.4 开始和结束

点击 **开始采集** 后，UI 会实时展示：

- 电流、电压、实测电池功率和累计能量。
- CPU 总负载、各集群频率和可用的 GPU 信息。
- 前台包名、Activity/窗口、屏幕、当前刷新率，以及平台可提供的帧率/帧耗时指标。
  Android 原生游戏优先显示 SurfaceFlinger BLAST 图层的呈现 FPS、1% Low、P95/P99
  帧间隔和跨帧预算比例，普通 View 应用回退 `gfxinfo` UI 帧提交速率、帧耗时与
  deadline miss；HarmonyOS 显示抽样合成器 FPS、P95 帧间隔和跨刷新槽位比例。
- 会话内刷新档位驻留、触摸交互次数、分辨率/亮度、GPU renderer 和最高温度。
- 当前前 5 个 CPU 热点；后台更新/安装/编译或热状态异常时才显示干扰告警。
- 采集器日志、检查点、ADB/HDC 断线和恢复状态。

性能模式中的 **1% Low** 对原生游戏使用 SurfaceFlinger 呈现帧间隔最慢 1% 的平均
耗时换算；普通 View 应用使用 gfxinfo 会话内帧耗时直方图，直方图不可用时才退化为
采样窗口的 1 分位帧率。**渲染分辨率**只接受前台 BLAST SurfaceView 对应的
SurfaceFlinger GraphicBuffer 尺寸；WindowManager 窗口边界只代表显示窗口，不再冒充
游戏内部渲染尺寸。**插帧 / MEMC** 只有读取到与当前应用或当前游戏进程匹配的厂商
显式状态时才显示；设备能力属性、白名单或“120 Hz 显示 + 60 FPS 应用”不会单独形成
开启结论，因为这些证据无法证明插帧已在当前游戏生效。

性能模式还会保留新观察到的 `gfxinfo framestats` 详细帧时间戳，用于计算渲染阶段
平均/P95/P99 和慢帧明细。性能测试项按导入的持续事件或前台 Activity 区间汇总 FPS、
1% Low、P95/P99、异常帧、主要延迟阶段、CPU/GPU/内存资源、调度与热限制；测试项中的
功率列仅表示同期整机均值。

到达设定时长会自动收尾。也可以点击 **停止并收尾**，程序会保存已经采到的数据并
生成部分报告，不会简单丢弃运行目录。

## 8. 与 BTR2 不同步开始时怎么做

Mobile Profiler 和 BTR2 是两个独立进程：

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
dist\mobile-profiler-portable\
dist\mobile-profiler-portable.zip
```

不会删除 `profiler-runs`、`build` 或源码目录。

### 10.5 验证新包

```powershell
.\dist\mobile-profiler-portable\profiler.cmd --help
.\dist\mobile-profiler-portable\start-ui.bat --demo
```

如果需要包内 ADB，再检查：

```powershell
Test-Path .\dist\mobile-profiler-portable\platform-tools\adb.exe
```

确认后分发：

```text
dist\mobile-profiler-portable.zip
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

- 电池功率来自 `|current| × voltage`，表示整机电池侧净输出，不是单独 CPU、GPU、
  应用或基带电源轨。
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
- 普通量产机可以观察 Android 和 Linux 暴露的进程/线程，但可能看不到受权限保护的
  完整内核线程细节。
- 可以读取 ThermalService 状态、冷却设备、公开阈值、cpuset 和调度结果；通常无法
  读取 OEM 私有热策略算法、完整 governor/uclamp 参数或受限的 `sched_debug`。
- GC、kworker、DEX 等活动与功率的时间关联代表“可能贡献”和干扰证据，不等于
  已经完成物理电源轨级因果拆分。
- ADB/HDC 延迟、fuel gauge 更新周期和 OEM 传感器量化都会限制短时峰值精度。一小时级
  续航分析更应关注持续区间、重复性和条件一致性。

## 17. iOS 无线采集

### 17.1 设计边界

iOS 支持使用独立 sidecar 进程。主程序仍保持标准库运行时，不直接导入
`pymobiledevice3`。这样做有三个目的：

- 不影响现有 Android ADB、HarmonyOS HDC 采集和便携包。
- 将 GPL-3.0-or-later 的可选依赖保持在单独运行时与进程边界。
- 与原生 HarmonyOS HDC 适配共同复用标准化 JSON/JSONL 采样边界。

当前 iOS 支持：

- 电池电量、电压、电流、温度与 `PowerTelemetryData.SystemLoad` 整机物理功率。
- DVT sysmontap 的整机/进程 CPU、内存、磁盘计数器和相对 `powerScore`。
- DVT Graphics 的 Device / Renderer / Tiler 利用率。
- Running / Suspended 应用状态通知和设备 Mach uptime 对齐。
- RemotePairing 无线端点缓存、采集中断后的 sidecar 重启与日志恢复。

### 17.2 安装独立 iOS 运行时

在源码电脑执行：

```powershell
python -m venv .venv-ios
.\.venv-ios\Scripts\python.exe -m pip install "pymobiledevice3==9.34.0"
$iosPython = (Resolve-Path .\.venv-ios\Scripts\python.exe)
```

仓库根目录的 `start-ui.bat` 会自动检测 `.venv-ios\Scripts\python.exe`。如果环境位于
其他目录，也可以先设置：

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

命令成功后会返回 `ios:UDID` 和 Wi-Fi `host:port`。RemotePairing 记录由
`pymobiledevice3` 保存在用户目录中，最近可用端点另存于：

```text
~/.mobile-profiler/ios-devices.json
```

UI 中也可以选择 USB iPhone 后点击顶部 **iOS 无线** 完成同一操作。

### 17.4 拔线 Probe 和采集

RemotePairing 成功后拔掉 USB，再执行 Probe：

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
  --interval 1 `
  --process-interval 10 `
  --session-mode `
  --require-unplugged `
  --output profiler-runs\ios-btr2-round-001 `
  --title "iOS BTR2 one-hour workflow"
```

全局 `--ios-python` 与 `--adb` 一样，必须写在子命令前。设备标识建议始终保留
`ios:` 前缀，避免自动平台判断把裸 UDID 当成 Android serial。

### 17.5 如何解读 iOS 数据

- `SystemLoad` 是电池侧整机物理功率，但常见刷新周期约 20 秒。程序每秒保留当前
  观测值，并用 `power_sample_age_s` 明确它已经保持多久。
- CPU、GPU、进程和应用状态通常以 0.5～1 秒更新，不能因此把物理功率误解成同样的
  一秒级独立测量。
- DVT `powerScore` 是会话内相对诊断分数，不是 mW、J、mWh，也不是单进程电源轨。
- `sysmond`、`DTServiceHub`、`remotepairingdeviced` 会产生可测的观察者开销。报告通过
  `collector_cpu_pct` 和受监控进程行保留这部分证据。
- iOS 当前没有公开的 Android Power Profile、BatteryStats、cpuset、ADPF 或完整热严重度
  等价接口；相应报告区域会标为不可用，而不会伪造估算。

### 17.6 无线发现问题

拔线后 Bonjour 的 RemotePairing 广播可能消失，但已缓存的 IP/端口仍可能正常接受新
连接。程序会先尝试发现，再检查缓存端点并直接重连。若路由器重新分配了 iPhone IP：

1. 重新接入 USB。
2. 保持 iPhone 解锁。
3. 再运行一次 `ios-pair` 或点击 **iOS 无线** 更新端点。
