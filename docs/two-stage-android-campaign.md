# Android 两阶段长稳与续航 Campaign

`mobile-profiler campaign` 把设备准备和长时间实际操作拆成两个可以独立执行的阶段。两个阶段共用一份 JSON，但互不隐式启动：可以反复执行预备阶段，也可以在确认设备条件后单独启动实际测试。

示例配置：[`examples/android-two-stage-campaign.json`](../examples/android-two-stage-campaign.json)

两小时 Agent 的严格验收、Prompt 设计、真机问题记录和能力边界见 [`two-hour-agent-automation.md`](two-hour-agent-automation.md)。

## 从 UI 单独启动两个阶段

打开“AI 自动化 → 任务启动”，在“任务模板”中可以直接选择：

- `阶段 1：预备环境（完整流程）`
- `阶段 2：实际测试（单轮 2 小时）`

“AI 自动化 → 阶段配置”直接读取当前 Campaign JSON，并分别展示两个阶段的目的、配置摘要、六步执行链路、系统设置/安装包/软件或 workflow 分组、执行边界和严格验收条件。该页面是只读基线，不读取浏览器模板草稿；预备应用没有对应 workflow、安装提示允许 APK 下载、首启要求高风险文件权限等不一致会在“配置审计”中显式列出。

“任务启动”页保留任务模板选择、新建任务、单轮 workflow 循环开关、当前任务/步骤进度和开始/停止按钮；下方“手动临时任务”可以直接输入一次性目标并运行，不写入模板，同时沿用已保存的 System Prompt 和当前模型配置。Campaign 任务默认直接使用当前 JSON 和内置 System Prompt：浏览器即使残留旧草稿也不会隐式覆盖。只有用户明确点击“运行时覆盖”并保存后，本次启动才发送任务和 System Prompt 覆盖；覆盖不会回写 Campaign JSON，重新选择阶段会恢复 JSON 基线。“阶段配置”始终展示 JSON 基线。实际测试固定提交 `repeat_workflows=true` 与 `max_rounds=1`：前者只控制 7200 秒轮次内是否继续调度 workflow，后者保证到达本轮截止后收尾，不会误跑无限后续轮次。预备阶段固定执行一次，系统设置、本地 APK/APKS 安装和声明权限仍由宿主安全执行。

该通用横评 Campaign 默认允许设备连接充电器或 USB 供电运行；即使配置省略 `recording.require_unplugged`，录制命令也会使用 `--allow-external-power`。外部供电状态会继续写入采样和报告，用于区分充电条件下的诊断数据，但不会阻止两小时轮次启动，也不会被 Campaign 判为录制失败。只有明确配置 `recording.require_unplugged: true` 时，才启用严格断电录制。

## 1. 校验配置

```powershell
mobile-profiler campaign validate examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1
```

校验只解析 JSON、路径和白名单字段，不连接手机。配置中的 `device` 可以留空，并在每次执行时通过 `--device` 覆盖。

## 2. 预备阶段

```powershell
mobile-profiler campaign prepare examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1
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
  --device 10AF3Q11JP001X1 `
  --dry-run
```

## 3. 实际测试阶段

优先使用稳定的 USB ADB 设备号；设备可以继续连接充电器或 USB 外部供电：

```powershell
mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1 `
  --max-rounds 1
```

默认示例将每个轮次固定为 `7200` 秒：

- 每轮启动一个独立的 session-mode 功耗采集目录；默认使用 5 秒采样、低干扰预设，并补充温度和系统设置快照。
- Agent 按配置中的应用场景顺序执行；到达列表末尾后从第一项继续，直到两小时轮次结束。
- workflow 可以先运行独立的 `initialization_tasks` 恢复确定入口，再用新的 Agent 会话执行主 `tasks`；两者都必须严格完成且前台包符合软件契约。
- `completed_with_warnings`、`skip` 和 `max_steps` 不算成功。失败会按 workflow 冷却，连续失败达到阈值后在本轮隔离，避免重复浪费时间。
- 到达轮次截止时宿主会停止仍在运行的 Agent，并记录 `round_deadline`，保证活跃测试窗口不会被单个长任务突破。
- 当前 Agent 结束后由宿主回桌面，再等待 `idle_after_s`，降低跨应用状态污染。
- 两小时结束后等待采集器完成报告，并对该唯一轮次做严格验收。
- 采集验收不仅检查退出码：最终 checkpoint 必须为 `complete`，样本、分析和 HTML 报告均非空，且有效样本不少于理论采样数的 80%。运行期间文件可能缓冲，应读取 checkpoint 的实时计数。
- UI 固定只跑一轮；CLI 只有在调用者省略 `--max-rounds` 时才会继续创建后续轮次。

用于短流程验证时可以限制轮数：

```powershell
mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1 `
  --max-rounds 1
```

只查看实际命令和工作流：

```powershell
mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1 `
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
    round-progress.json
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
- `test.workflows[].contract`：入口状态、严格成功证据、禁止状态、登录策略和允许的结束前台包。
- `test.workflows[].contract.required_actions`：从 Agent 事件中硬校验的有效动作组与最少次数，防止模型未完成两次滚动等要求却提前 `finish`。
- `test.workflows[].tasks[].action_limits`：宿主执行前校验的动作组最大次数；`maximum: 0` 可建立完全只读的初始化会话，`maximum_per_signature` 可阻止同一动作参数重复执行。
- `test.workflows[].initialization_tasks`：与主功能验证分离的可重复入口恢复任务。
- `test.workflows[].automation_engine`：按应用覆盖全局视觉、uiautomator2 或混合引擎。
- `test.workflows[].force_stop_before_launch`：仅对会把不可用或防截屏页面留在任务栈顶的应用启用；每次调度先 `am force-stop`，再从 Launcher 入口冷启动。默认关闭，以保留游戏进度和已有登录会话。
- `test.workflows[].quarantine_after_failures` / `retry_cooldown_s`：本轮失败隔离阈值和重试冷却。

轮次结束后不要只看顶层 `max_rounds`。`round-summary.json` 的 `coverage` 统计不同 workflow 的严格成功覆盖，`acceptance.passed` 同时检查时长、采集、全覆盖、required 流程和无人工接管。

2026-07-23/24 的正式真机轮 `android-general-two-hour-soak-test-20260723-221328` 已完成：活跃窗口 7200 秒、23/23 workflow、required 4/4、无人工接管、无隔离；采集自然退出并生成 1408/1440 个理论样本，`acceptance.passed: true`。详细问题与后续修复见 [`two-hour-agent-automation.md`](two-hour-agent-automation.md)。
