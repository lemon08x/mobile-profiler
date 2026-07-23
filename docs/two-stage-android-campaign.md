# Android 两阶段长稳与续航 Campaign

`mobile-profiler campaign` 把设备准备和长时间实际操作拆成两个可以独立执行的阶段。两个阶段共用一份 JSON，但互不隐式启动：可以反复执行预备阶段，也可以在确认设备条件后单独启动实际测试。

示例配置：[`examples/android-two-stage-campaign.json`](../examples/android-two-stage-campaign.json)

## 从 UI 单独启动两个阶段

打开“AI 自动化 → 任务启动”，在“任务模板”中可以直接选择：

- `阶段 1：预备环境（完整流程）`
- `阶段 2：实际测试（2 小时/轮，至关机）`

“任务启动”页保留任务模板选择、新建任务、循环开关、当前任务/步骤进度和开始/停止按钮；下方“手动临时任务”可以直接输入一次性目标并运行，不写入模板，同时沿用已保存的 System Prompt 和当前模型配置。点击“编辑模板”或切换到“Prompt 编辑 → 任务配置与 Prompt”，可以修改流程名称、任务顺序、任务名称、最大步骤、任务超时、失败策略、每轮目标 Prompt 与注意事项；修改必须点击“保存”后才生效并写入当前浏览器的模板草稿，刷新页面后继续保留。ADB 动作协议和全局安全规则位于“系统级 Prompt”二级菜单，也使用同一个“保存”按钮生效。启动时这些值会覆盖 JSON 中对应任务的默认值，实际测试模板的任务顺序也会作为应用场景执行顺序。实际测试阶段的循环开关开启时持续创建后续轮次，直到设备持续离线 120 秒或用户停止；关闭时整套应用场景只执行一遍并提前收尾。预备阶段固定执行一次，系统设置、本地 APK/APKS 安装和声明权限仍由宿主安全执行。

该通用横评 Campaign 默认允许设备连接充电器或 USB 供电运行；即使配置省略 `recording.require_unplugged`，录制命令也会使用 `--allow-external-power`。外部供电状态会继续写入采样和报告，用于区分充电条件下的诊断数据，但不会阻止两小时轮次启动，也不会被 Campaign 判为录制失败。只有明确配置 `recording.require_unplugged: true` 时，才启用严格断电录制。

## 1. 校验配置

```powershell
mobile-profiler campaign validate examples\android-two-stage-campaign.json `
  --device 192.168.21.169:5555
```

校验只解析 JSON、路径和白名单字段，不连接手机。配置中的 `device` 可以留空，并在每次执行时通过 `--device` 覆盖。

## 2. 预备阶段

```powershell
mobile-profiler campaign prepare examples\android-two-stage-campaign.json `
  --device 192.168.21.169:5555
```

预备阶段按以下顺序执行：

1. 写入并回读校验允许的 Android `settings`；任意 setting key 不能绕过代码白名单。
2. 从单个 APK、包含完整 split 集合的目录或 `.apks` 压缩包执行 `adb install` / `install-multiple`。
3. 对配置逐项声明的运行时权限执行 `pm grant`；没有“授予所有权限”的隐式开关。
4. 如果目标包仍不存在且配置了 `install_prompt`，让 ADB Agent 在指定应用商店中安装，再由宿主用 `pm path` 验证包名。
5. 宿主启动每个应用，ADB Agent 处理首次启动弹窗、协议和权限页，并在稳定主界面结束；每个应用结束后宿主显式回桌面。
6. 初始化完成后，宿主按同包名查找实际测试 workflow，让 Qwen 再完整执行一次主功能操作。只有 workflow 完成、前台仍是目标包且操作后证据成立，才写入 `normal_flow_supported: true`；跑到设置页或其他应用后错误 `finish` 会被宿主改记为 `wrong_foreground`。

`allow_terms_acceptance` 必须逐应用设置。即使允许接受当前应用协议，默认 Prompt 仍禁止账号登录、手机号/验证码、实名认证、支付、下单、发送消息和清除数据。

“AI 自动化 → 应用与游戏”中的 `catalog_status: pending_validation` 表示已完成真机烟测、仍等待上述正式预备复验。复验通过后目录快照会把该项动态提升为 `supported` 并移出待检验分组；配置文件仍保留原始候选状态，方便在新机型上重新横评。当前项目 APK 候选包含 2048、Tetris、Super Snake 和 AstroSmash；它们分别使用视觉、混合、视觉和视觉引擎完成了“进入主玩法、可验证操作、回桌面”的烟测。

先检查计划而不改变设备：

```powershell
mobile-profiler campaign prepare examples\android-two-stage-campaign.json `
  --device 192.168.21.169:5555 `
  --dry-run
```

## 3. 实际测试阶段

先保持 Wi-Fi ADB 稳定；设备可以继续连接充电器或 USB 外部供电：

```powershell
mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 192.168.21.169:5555
```

默认示例将每个轮次固定为 `7200` 秒：

- 每轮启动一个独立的 session-mode 功耗采集目录；默认使用 5 秒采样、低干扰预设，并补充温度和系统设置快照。
- Agent 按配置中的应用场景顺序执行；到达列表末尾后从第一项继续，直到两小时轮次结束。
- 当前 Agent 结束后由宿主回桌面，再等待 `idle_after_s`，降低跨应用状态污染。
- 两小时结束后等待采集器完成报告，再创建下一个轮次。
- 默认不限制轮次数，只有设备持续不可用超过 `offline_grace_s` 才结束整个 Campaign。

用于短流程验证时可以限制轮数：

```powershell
mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 192.168.21.169:5555 `
  --max-rounds 1
```

只查看实际命令和工作流：

```powershell
mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 192.168.21.169:5555 `
  --dry-run
```

## 4. 关机判定边界

ADB 无法在手机已经关机后读取一个“已关机”状态，因此宿主可验证的终止条件是：目标序列号连续不可用达到 `offline_grace_s`。这也可能由 Wi-Fi 漫游、路由器重启或无线调试被系统关闭触发。正式续航测试应使用稳定的专用 Wi-Fi、关闭 AP 省电，并把 grace period 设置得足以覆盖短暂抖动。

设备在轮次中掉线时，采集器先按 `shutdown_finalize_timeout_s` 等待自行收尾；仍未退出才终止子进程，并尝试从落盘 journal 执行 `recover`。因此关机前的完整采样仍会保留。

## 5. 输出

默认输出到 `profiler-runs/campaigns/`：

```text
<campaign>-prepare-<timestamp>/
  events.jsonl
  state.json
  agent-runs/

<campaign>-test-<timestamp>/
  events.jsonl
  state.json
  round-0001/
    record.log
    recording/
    agent-runs/
    round-summary.json
  round-0002/
  ...
```

配置不会保存 API key。`api_key_env`、`api_base_url_env` 和 `model_env` 在执行时从环境变量读取，适合在多台测试主机上复用同一份 JSON。

## 6. 配置要点

- `preparation.settings`：只能使用代码中允许的 namespace/key；写入后必须回读一致。
- `preparation.install_sets`：本地 APK、`.apks` 或 APK 目录；`.apks` 只接受根目录 APK 条目并拒绝路径穿越。
- `preparation.apps[].permissions`：逐项权限和 `required` 标记。
- `preparation.apps[].catalog_status`：`supported` 或 `pending_validation`；待检验项正式复验通过后由目录快照动态提升。
- `preparation.apps[].supported_engines`：该应用已验证可用的 `vision`、`uiautomator2` 或 `hybrid` 引擎。
- `preparation.apps[].install_prompt`：仅在包缺失时启用应用商店安装。
- `preparation.apps[].setup_tasks`：应用首启任务和 Prompt。
- `test.cycle_duration_s`：每轮时长；正式配置为 7200 秒。
- `test.recording`：传给现有 `record` 命令的受限采集参数。
- `test.workflows`：循环应用、任务卡、步骤/超时、回桌面和间隔配置。
