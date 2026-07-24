# 两小时 Agent 自动化：编排设计、问题记录与验收标准

本文记录 Android 两小时 Campaign 的设计约束、真机迭代证据、已知能力边界和正式验收方法。目标不是“进程运行了两小时”，而是让宿主和 Agent 在 7200 秒内持续执行可验证、可重复、无人工动作的真实应用流程，并留下足以复盘的证据。

## 完整跑通的定义

一轮只有同时满足以下条件才记为 `acceptance.passed: true`：

1. 活跃测试时长达到配置的 `cycle_duration_s`；单次短流程模式只要求完整跑完一遍工作流。
2. 性能/功耗采集正常退出，未被宿主提前终止或恢复；最终 checkpoint 必须是 `complete`，`samples.csv`、`analysis.json`、`report.html` 均非空，样本数至少达到按采样间隔计算的理论值的 80%。
3. 本轮开始时已安装的每个 workflow 至少有一次严格成功；只启动应用、`skip`、`max_steps` 和 `completed_with_warnings` 都不算成功。
4. 所有 required workflow 至少严格成功一次。
5. 没有 `take_over`，即整轮没有人工接管。
6. 没有 workflow 因连续失败或安全终止状态被隔离。
7. 设备没有持续离线、锁屏阻塞或录制提前退出。

CLI 顶层的 `max_rounds` 只表示达到了调用者要求的轮数，不等价于业务验收通过。正式结论必须读取 `round-summary.json` 中的 `acceptance` 和 `coverage`。

## 软件契约

每个 workflow 都可以声明 `contract`，宿主会把它注入 Prompt，并在 Agent 返回后独立核验：

- `entry_state`：主验证开始前必须具备的稳定入口。
- `success_evidence`：本次新动作后必须出现的可见证据。
- `forbidden_states`：即使页面诱导也不能进入的状态。
- `login_policy`：`forbidden`、`existing_session_only` 或 `operator_required`。
- `allowed_foreground_packages`：结束时允许处于前台的 Android 包；默认只有目标包。
- `required_actions`：宿主从 Agent 的落盘事件中统计有效动作，例如“两次滚动”“一次长按”“输入后提交”；动作次数不满足时，即使模型调用 `finish` 也会改记为 `incomplete_action_evidence`。

Prompt 约束负责告诉模型“该做什么”，包名检查、任务结果检查和调度器状态机负责保证模型不能仅靠文字宣称成功。两层都需要：只写 Prompt 无法抵御误判，只写宿主规则又无法引导模型处理页面差异。

## 初始化与主验证分离

容易受持久状态影响的应用使用两个独立 Agent 会话：

1. `initialization_tasks` 把应用恢复到契约入口，例如从选英雄页进入地牢、关闭登录弹层、进入 AstroSmash 主玩法、等待星穹铁道主场景。
2. `tasks` 只制造并验证本轮的新状态变化，例如角色移动一格、炮台左移、视角旋转。

初始化成功后，宿主先检查前台包，再创建新的主验证 Agent。这样主任务不会把“选中了英雄”“关闭了弹窗”误算成测试动作，也不会把上轮遗留结果当作本轮证据。

## JSON 基线与轮次编排

Campaign JSON 是预备和正式 workflow 的唯一默认任务基线。Dashboard 会从当前解析后的 `CampaignConfig` 动态生成任务卡；启动 API 默认忽略浏览器传来的 `tasks` 和旧 System Prompt。只有显式发送 `runtime_task_overrides: true` 或 `runtime_system_prompt_override: true` 时才使用本次运行覆盖，因此本地存储的旧模板不会悄悄压过刚修好的 JSON Prompt。

每次启动时宿主解析并固定本轮配置快照。运行中修改源码、JSON 或浏览器草稿都只影响下一次启动，不会让同一个两小时证据窗口前后使用两套规则。复盘时必须明确区分“本轮实际快照”和“根据本轮问题形成、等待下次加载的修复”；不能把后者倒算成本轮已经验证。

正式运行把两个概念分开：

- `repeat_workflows=true`：在当前 7200 秒轮次内，完成一遍后继续调度允许重复或尚未成功的 workflow。
- `max_rounds=1`：只创建一个固定时长轮次，到达硬截止后验收并收尾。

旧 UI 用一个 `loop_enabled` 同时控制两者：关闭时会在单遍完成后提前退出，开启时又会在首个两小时结束后无限创建轮次。当前 Dashboard 固定提交上述两个独立值；只有 CLI 调用者明确省略轮次限制时才保留“持续至设备离线”的运维模式。

## Prompt 策略

当前系统 Prompt 为 `adb-phone-agent-v17`。它在 v16 的成功断言约束上继续强化了动作账本与宿主动作边界：

- `skip` 只用于确定性的外部阻塞或确实不适用，不能代替标题页、选择页、普通教程等安全导航。
- 状态变化任务必须形成“动作前锚点 → 唯一动作 → 动作后锚点”闭环。
- 初始化只建立入口，主验证必须制造本轮新变化。
- 当前语义元素数量为 0 时禁止复用旧 `element_id`。
- 旧页面残留、输入事件已发送和任务描述本身都不能作为成功证据。
- `finish` 是肯定成功断言，不是通用结束报告；消息中出现“无法完成、不一致、未满足、失败、需要人工”等否定结论时，Agent 执行器会拒绝该动作并允许模型修正。
- Campaign 宿主会再次检查完成消息。即使旧 Agent 或自定义实现绕过执行器，矛盾完成声明也会改记为 `contradicted_completion_claim`，不纳入覆盖率。
- 任务声明动作上限后，模型必须先把“最近动作与结果”当作真实计数器；达到上限后不能用重复点击、Undo 或第 4 次操作“再确认一次”。
- 坐标动作必须使用工具协议规定的字段。点击只接受 `element=[x,y]`，滑动必须同时给出 `start` 与 `end`；校验失败时宿主返回一份完整、可复制的正确调用示例，减少模型连续生成同一种残缺参数。

应用 Prompt 采用短任务卡而不是把所有恢复路径塞进一个长任务。步骤上限是异常保护，不是探索预算；正常流程通常应在 2～6 步内结束。

## 动作证据与动作上限

“至少做过”与“最多允许做”是两个独立问题，配置不能只解决其中一个：

- `contract.required_actions` 是成功下限。主 Agent 会话未落盘足够的有效动作时，宿主直接拒绝 `finish`；初始化动作、参数无效动作和上一轮动作都不计入。
- `tasks[].action_limits` 是安全上限。它支持动作组和 `maximum`；例如 `tap` 与 `tap_element` 合并计算，避免模型换一种动作名绕过限制。可选的 `maximum_per_signature` 还会限制同一动作及同一参数组合的次数，适合阻止计算器把同一数字键连点两次，同时不会影响明确需要同格三击但未启用该字段的谜题。
- `maximum: 0` 用于只读会话。质感文件根目录初始化由此只能观察后 `finish/skip/take_over`，不能为了确认路径而误入目录、返回或滚动。
- 上限在动作执行之前检查，超限输入不会触达手机；被拒绝的动作保留在事件日志中供模型修正，但不计入成功下限。

目前最典型的组合是：SGTPuzzles 要求同格点击至少 3 次、同时最多 3 次；AstroSmash 要求长按至少 1 次、同时最多 1 次；Fossify Calculator 总共只允许 4 次普通点击且同一点击参数最多 1 次；内容流要求滚动至少 2 次、同时最多 2 次。下限防止口头完成，上限防止已经成功后继续破坏状态。

## 引擎选择

workflow 可用 `automation_engine` 覆盖全局引擎，显式的 UI 操作者覆盖仍拥有最高优先级。

- Canvas、地图和实时游戏使用 `vision`，因为语义树无法表达棋子、角色或地图像素变化。
- OpenCalculator、Fossify Calculator、质感文件、Edge、夸克等控件明确或需要可靠文本输入的应用使用 `hybrid`，优先精确语义控件，同时保留截图证据。Fossify 当前版本会公开带文字的数字/运算符控件、公式和结果；用 `tap_element` 可避免把 6 点成 9，动作上限则阻止成功后重跑整套输入。
- 引擎不是越复杂越好。WPS 和部分内容流的语义树不包含足够的关键视觉结果，继续使用 `vision`；夸克使用混合引擎完成编辑器往返，但不再依赖会被中文输入法转写的英文 ADB 文本。

## 失败、冷却与隔离

每个 workflow 有独立的连续失败计数：

- 严格成功会清零计数。
- `error`、`task_failed`、`max_steps`、初始化失败和证据不完整会进入 `retry_cooldown_s` 冷却。
- 达到 `quarantine_after_failures` 后，本轮隔离该 workflow，避免两小时内重复消耗模型时间。
- `take_over`、错误前台和无法核验前台属于终止性失败，立即隔离。
- 缺失的可选包在轮次开始时记录为 `missing`，不纳入“已安装 workflow 全覆盖”的分母；缺失 required 包则不允许开始测试。

每个 Agent 都接收轮次硬截止。到达 7200 秒时宿主停止当前 Agent 并记录 `round_deadline`，不允许最后一个长任务把固定时长轮次无限拖长。

## 可观测性

运行期间会持续原子更新：

- Campaign 根目录 `state.json`：当前轮次、当前 workflow、剩余时间、失败计数、冷却和覆盖率。
- `round-XXXX/round-progress.json`：同一份轮次实时快照；结束后替换为完整轮次结果。
- `events.jsonl`：初始化、验证、冷却、隔离、设备离线和硬截止等事件。
- `round-summary.json`：完整 workflow 尝试、采集结果、`coverage` 和 `acceptance`。

`coverage.successful_count` 统计至少严格成功过一次的不同 workflow，不是成功尝试次数。高频简单应用因此不会掩盖某个长期失败的应用。

采集进程运行时可能缓冲 `samples.csv` 与若干 JSONL，因此不能用文件大小暂时不增长来判断停采。`recording/checkpoint.json` 会按固定间隔更新 `host_elapsed_s`、`sample_count`、上下文、温度和重连次数；运行中看 checkpoint，结束后再验收完整文件。只看采集子进程退出码会漏掉“0 样本但退出 0”，宿主现已把最终工件与最低样本覆盖率纳入 `recording_ok`。

## 2026-07-23 真机迭代记录

设备：vivo V2458A，USB ADB `10AF3Q11JP001X1`；模型：OpenAI-compatible `qwen3.6-27b`，thinking disabled。

### 基线全应用烟测

证据目录：`profiler-runs/campaigns/android-general-agent-smoke-test-20260723-120616`

- 配置目标 900 秒，旧调度器实际耗时 921.1 秒，证明 Agent 可以跨越轮次截止。
- 24 个 workflow 均被调度；首次尝试 20 个完成，4 个为 warning。
- 地牢停在“选择英雄”后 `skip`；同一旧流程第二次轮转才进入地图并成功移动，证明入口状态需要独立编排。
- 夸克把 URL 当成查询词，停在搜索结果页。
- 哔哩哔哩和京东停在手机号登录页；旧 Prompt 直接 `skip`。
- 星穹铁道用 92.9 秒完成；把加载/入口与视角动作拆分后，定向复测分别只用 1 步和 2 步。

### 新编排定向烟测

证据目录：`profiler-runs/campaigns/android-agent-targeted-orchestration-smoke-test-20260723-122539`

- 地牢、AstroSmash、京东和星穹铁道严格通过。
- 京东初始化先关闭登录弹层，再由新 Agent 完成两次滚动，不需要账号。
- 哔哩哔哩第一次返回只收起输入法，仍在登录页；后续策略改为收键盘后再点击页面左上角返回。
- 夸克混合引擎在键盘窗口切换时出现 0 个语义元素，模型四次臆造旧元素；即使正确输入 URL，新版入口仍按 AI 查询处理。测试目标因此改为已有公开内容页的两次只读滚动，避免测试已不稳定提供的传统地址栏能力。
- 新输出正确把 warning 记为 `incomplete_evidence`，并给出 4/6 的严格覆盖率，而不是把阶段误报为全通过。

### 阻塞项二次定向复测

证据目录：`profiler-runs/campaigns/android-agent-targeted-retry-smoke-test-20260723-123201`

- 夸克初始化用 2 步退出搜索编辑器，主任务用 3 步完成两次公开结果页滚动。
- 哔哩哔哩初始化用 3 步依次收起键盘、退出手机号登录页并确认公开首页，主任务用 4 步完成两次推荐流滚动。
- 2/2 workflow 严格成功，`acceptance.passed: true`，证明这两个页面不需要人工登录。

### 新编排 24 应用单遍回归

证据目录：`profiler-runs/campaigns/android-agent-full-pass-regression-test-20260723-123526`

- 24/24 均被调度，19 个严格通过，required 流程 4/4 通过。
- Super Snake 的青色玩家短线已转向，但模型继续点击到上限；Prompt 改为只认青色短线，定向复测在 2 步内通过。
- Fossify Calculator、WPS 和 UC 在混合引擎中受语义树缺少关键显示内容影响，视觉截图明明有结果却未结束；按真机证据回退视觉模式。
- 京东一次冷启动黑屏超过原预算；初始化增加受限等待和一次同包重启。
- 回归中腾讯新闻只滚动一次便口头完成，促使宿主增加 `required_actions`。之后同流程必须实际落盘两次有效滚动才可能严格成功。

### 失败项动作证据复测

证据目录：`profiler-runs/campaigns/android-agent-failure-fixes-retry-test-20260723-132153`

- Fossify 改为长按 C 清空整条旧表达式，1 次长按 + 4 次算式按键后得到 48。
- 质感文件和 WPS 的模型都曾提前 `finish`，宿主正确改记 `incomplete_action_evidence`；调整可接受的返回动作组后，进入/返回目录以及新建/退出/不保存均严格通过。
- UC 退出千问覆盖层后用两次公开页面滚动替代不稳定的传统地址栏流程，严格通过。
- 京东成功进入公开首页，但第一次滚动立即触发“京东验证”二维码/快速验证页。此为服务端风控，Agent 正确 `take_over`，需要操作者建立可测试状态后才能启动最终无人值守轮次。

### 京东人工验证后的稳定性复测

证据目录：

- `profiler-runs/campaigns/android-agent-jd-post-verification-stress-test-20260723-142853`
- `profiler-runs/campaigns/android-agent-jd-cold-start-stress-test-20260723-143737`

操作者完成快速验证并回到公开首页后，第一轮两次滚动严格成功；第二次调度时，Android 的京东任务栈恢复到启用防截屏的 `LoginActivity`。ADB 仍报告京东在前台，但 Agent 只能看到系统叠层和纯黑内容。为区分启动栈问题与风控问题，宿主新增 `force_stop_before_launch`：只对明确需要的 workflow 先清理旧任务栈，再从 Launcher Activity 冷启动。冷启动真实到达 `MainFrameActivity` 和公开首页，证明启动策略修复有效。

随后独立冷启动复测中，初始化仍能严格通过，但第一次自动滚动后页面再次切入防截屏黑屏，Agent 按规则 `take_over`。人工验证只能临时恢复入口，不能保证两小时连续 Agent 操作；继续尝试绕过第三方风控既不稳定，也超出安全测试边界。因此正式 7200 秒配置不再调度京东 workflow，保留上述目录作为排除依据。冷启动能力仍保留给其他确有任务栈污染、且不会丢失必要会话状态的应用。

### v16 完整预备与契约复核

证据目录：`profiler-runs/campaigns/android-general-two-hour-soak-prepare-20260723-193723`

- 24 个候选应用全部完成预备调度，19 个严格通过；required 4/4 通过。
- Tetris 旧棋盘曾显示 `GAME OVER`，模型却用 `finish` 报告“无法完成”。v16 否定完成拦截和每次先点一次 Restart 的初始化策略修复后，独立真机复测目录 `profiler-runs/android-agent-tetris-v16-smoke-test-test-20260723-193522` 严格通过。
- OpenCalculator 实际完成 `12+7=19`，旧契约硬要求 5 次点击而误报动作不足；真实应用会实时计算，不需要等号，契约改为四次算式按键。
- Edge 用键盘“前往”语义按钮完成新域名导航，该动作被引擎记为 `tap_element` 而不是 `enter`；最终域名、正文和本轮 `input_text` 已形成充分证据，契约不再绑定具体键盘实现。
- 质感文件与京东当时没有正式 workflow，暴露“预备候选必须有同包主流程”这一配置一致性问题。质感文件补回只读目录往返；京东因后续再次出现真人拼图验证，从无人值守候选集合排除。
- 夸克的视觉 `input_text` 在中文输入法下把 `agent smoke test` 显示成中文候选与 `smoketest`，证明“命令已发送”不能当作文本正确证据。流程改用混合引擎和纯数字草稿 `0723`，必须先看到草稿再清空并取消。

### v16 失败项定向复测

证据目录：

- `profiler-runs/targeted-validation/targeted-five-workflow-v16-prepare-20260723-200822`
- `profiler-runs/targeted-validation/targeted-opencalculator-v16b-prepare-20260723-201840`
- `profiler-runs/targeted-validation/targeted-opencalculator-v16c-prepare-20260723-202230`

质感文件、Edge 和夸克在新契约下分别严格通过。OpenCalculator 第一次定向重跑又发现上一轮已经留下 `12+7=19`，模型在本轮 0 次按键时直接 `finish`，宿主正确拒绝；加入独立“空白输入区初始化”后，第二次重跑暴露视觉上把 `4` 误认成 `7`。最终切换混合引擎、明确数字键盘行列并预留一次完整重试预算后严格通过。这里的优化原则是重建可验证起点、修正输入能力和坐标理解，而不是删除动作证据门槛。

同一轮定向复测中，京东冷启动直接出现拖拽拼图“安全验证”。Agent 立即 `skip`，没有尝试代做真人验证。由于它无法满足连续两小时无人值守的可重复性，最终配置为 23 个预备应用与 23 个正式 workflow，并在文档中保留排除证据。

### v17 完整预备

证据目录：`profiler-runs/campaigns/android-general-two-hour-soak-prepare-20260723-215451`

- USB ADB 设备 `10AF3Q11JP001X1` 上完整执行系统设置、安装集核验、权限、应用初始化和正式 workflow 复核，23/23 全部严格完成。
- required failure 为 0，optional failure 为 0；required workflow 4/4 通过，期间没有人工登录或接管。
- vivo 会在普通 `settings put system accelerometer_rotation 0` 后把值恢复为 `1`。宿主发现读回不一致后改用 `cmd window user-rotation lock 0`，再次读回 `accelerometer_rotation=0` 与 `user_rotation=0`，把厂商兼容动作和最终值一并写入证据。
- SGTPuzzles 曾在三态闭环成功后继续点第 4 次或点 Undo；现在宿主把 `tap/tap_element` 总上限固定为 3，第三次恢复灰格后只能结束。
- AstroSmash 已经处于黑色主玩法时本身就是合法入口，不再为了寻找设置首页而退出游戏；主验证只允许一次左箭头长按。
- 质感文件顶部灰色“下载”面包屑会在真实内部存储根目录残留。判断依据改为目录总数以及 Alarms、Android、DCIM、Documents、Download 等内容锚点；初始化完全只读，主验证只允许一次进入 Documents 和一次返回。
- Edge 地址栏输入与新版浏览器搜索语义不稳定，流程改为已固定的 Example Domain → More information/IANA → back 可逆往返。
- 夸克的中英文输入法会改变 ADB 文本。正式目标改为首页搜索框聚焦、明确出现编辑器与软键盘、再 back 恢复首页，全程不输入或提交内容。

上述调整遵循同一个原则：保留真实功能动作与可见状态变化，但把第三方内容、输入法转写、历史任务栈和模型自由探索从成功条件中剥离。若流程只能靠放宽证据、重用旧结果或绕过风控才能通过，就不应进入正式无人值守集合。

### 正式长跑中形成的后续修复

正式轮目录：`profiler-runs/campaigns/android-general-two-hour-soak-test-20260723-221328`

该轮启动后固定使用当时的 JSON 与 Agent 代码快照。以下修复由运行日志触发，写入当前源码，但不会反向改变这轮证据：

- Fossify Calculator 第 7 次主验证把数字 6 用 `double_tap` 输入成 `8×66`。v17 的矛盾 `finish` 拦截阻止模型把错误结果报成成功；后续第 8 次成功并清零连续失败计数。
- 第 13 次又以两个普通 `tap` 连点数字 6，随后试图再次长按清空并耗尽步骤；第 14 次成功恢复。第 25 次则在公式已经显示 `8×6` 后误点邻近数字 9，得到 `8×69`。由此确认只禁止 `double_tap` 或同坐标重复仍不够。新任务改用混合引擎：C 只允许一次坐标长按，8、×、6、= 必须逐轮选择带准确文字的 `tap_element`；同时设置 `double_tap maximum: 0`、普通按键总计最多 4 次及 `maximum_per_signature: 1`，既防错键又防成功后重跑。
- Super Snake 曾把点击坐标写成 `start/end`；腾讯新闻和搜狐新闻分别连续生成缺少 `start` 的 `swipe_fast`。宿主没有执行残缺动作，模型修正后流程仍成功。新任务卡直接给出完整调用示例，内容流最多执行两次有效滑动；通用参数错误也会返回一份正确字段示例，而不只说“缺少 start”。
- 运行中的 `samples.csv` 和 JSONL 由采集器缓冲，文件大小一度保持不变，但 checkpoint 的样本、上下文与温度计数持续增长。验收逻辑因此改为运行中观察 checkpoint，结束后强制检查 `complete` checkpoint、非空样本/分析/报告，并要求至少 80% 理论采样数；专门的回归测试证明“退出码 0 但没有工件”不能通过。

单次失败不会立即判整轮失败：它进入独立冷却，下一次严格成功会清零连续失败计数；只有连续失败达到阈值或出现 `take_over`、错误前台等终止性状态才隔离。这样既容纳模型的一次可恢复误触，又不让同一个坏流程无限消耗两小时窗口。历史失败仍保留在 `status_counts` 和 Agent 事件中，不会从审计记录消失。

### 两小时正式轮验收结果

证据目录：`profiler-runs/campaigns/android-general-two-hour-soak-test-20260723-221328`

- 活跃 Agent 测试窗口精确达到 `7200.000` 秒，总收尾耗时 `7205.005` 秒；第一个 23/23 严格成功全覆盖在 `952.9` 秒完成，剩余时间继续循环 5 个声明为可重复的稳定 workflow。
- profiler 子进程自然退出，exit code 为 0，`terminated: false`；最终 checkpoint 为 `complete`，stop reason 为 `completed`，0 次 ADB 重连。
- 5 秒间隔理论应有 1440 个样本，实际得到 1408 个，覆盖 97.8%，高于新验收下限 1152；另有 721 个上下文和 721 个温度快照。
- `samples.csv` 181,689 bytes、`analysis.json` 4,038,099 bytes、`report.html` 4,049,931 bytes，严格工件复核无缺失或空文件。
- 23/23 可用 workflow 至少成功一次，required 4/4；共记录 165 次严格完成和 3 次 `incomplete_evidence`。三次均来自 Fossify 旧快照，后续成功均清零连续失败计数。
- 没有 `take_over`、没有隔离 workflow、没有设备离线、锁屏或交互失败；`round-summary.json` 的 `acceptance.passed` 为 `true`。整轮没有人工触碰手机或人工登录。
- 共创建 241 个 Agent 会话、794 次模型决策，其中 781 次有效、13 次由宿主安全拒绝；模型请求累计 3503.4 秒，平均 4.41 秒。输入约 10,125,040 token，输出约 30,360 token，进一步确认输入证据压缩是后续性能优化重点。

正式轮使用的是启动时固定的旧任务快照；本节前述 Fossify hybrid、同参数上限、完整坐标错误反馈和严格录制工件规则属于由该轮形成的后续修复。正式轮证明旧快照已经能无人值守跑满并达到既定验收，新修复还需要独立定向复测，不能冒充已在这 7200 秒内加载。

正式轮结束并重载最新代码后，Fossify hybrid 修复连续真机复测 5 次：

- `profiler-runs/agent-runs/20260724-001653-fossify-hybrid-v17-stress-1-115bcf`
- `profiler-runs/agent-runs/20260724-001739-fossify-hybrid-v17-stress-2-4e6b6f`
- `profiler-runs/agent-runs/20260724-001824-fossify-hybrid-v17-stress-3-b1797c`
- `profiler-runs/agent-runs/20260724-001909-fossify-hybrid-v17-stress-4-30fbbe`
- `profiler-runs/agent-runs/20260724-001953-fossify-hybrid-v17-stress-5-758ad1`

5/5 均以固定 6 步完成，0 个无效动作，动作序列完全一致：`long_press C → tap_element 8 → × → 6 → = → finish`。第 5 次事件明确记录元素 `e018/e020/e023/e032` 分别对应 `8/×/6/=`，最终截图和语义树确认 `8×6=48`。这组证据独立验证了正式轮之后形成的最新修复。

## 当前优化思路与优先级

1. **先收紧可证明性，再提高通过率。** Prompt 负责描述页面差异，宿主负责动作计数、前台包、截止时间和采集工件。遇到误报时优先补可观测起点、动作闭环或宿主断言，不删除失败证据门槛。
2. **把自由探索限制在初始化。** 主验证尽量保持 1 个状态变化和 1 个结束判断；有副作用或容易误触的动作增加总次数、同参数次数或完全只读上限。普通弹层恢复与主功能验证使用独立会话，避免恢复动作污染主证据。
3. **稳定性优先于表面应用数量。** 第一遍覆盖全部 23 个当前可用 workflow；覆盖满足后只循环离线、可逆且已声明 `repeat_after_success` 的核心场景。公开内容、地图、大型在线游戏和第三方风控应用成功一次后停止，避免为了“看起来一直切 App”而引入登录、广告、内容消失和累积状态污染。
4. **下一步主要优化输入成本。** 当前模型输出很短，耗时与 token 几乎都在重复的系统 Prompt、动作历史和动作前后全屏截图。可考虑按任务声明是否需要上一帧、对语义完整页面发送控件差异而不是双图、对 Canvas 保留视觉证据并裁剪稳定 ROI；任何压缩都必须保留同一目标的动作前后对比，不能用省 token 换取不可验收。
5. **逐步增加确定性验证器。** 目前宿主能硬校验“做了几次、是否执行成功、最终前台是谁”，但 `8×6=48`、棋子位置、地图标签整体换位等视觉语义仍主要由模型判断。后续可为高频稳定控件加入 OCR、像素锚点或应用级只读探针；在验证器建立前，仍需保留截图与矛盾完成拦截。

这里的目标不是把每个应用硬编码成坐标脚本。坐标脚本对版本、分辨率和弹层极其脆弱；更合理的分工是让 Agent 处理视觉差异，让宿主把不可协商的安全和证据边界变成确定性代码。

## 能力边界与人工介入

以下情况不能通过增加步骤或放宽成功标准解决：

- 手机号、短信验证码、密码、实名、设备锁、生物识别和真人校验。
- 支付、下单、发消息、删除数据、账号切换等外部或不可逆副作用。
- 物理包装、机身状态、真实关机原因；ADB 只能观测“持续离线”。
- 网络内容消失、服务端强制升级或第三方应用永久取消公开入口。
- 截图和语义树都无法表达的内部状态。

遇到登录阻塞时，Agent 必须停在明确页面并记录应用、阻塞类型和人工下一步。操作者完成登录后应回到稳定入口，再重新运行短测；人工登录动作不能计入最终“仅 Agent”两小时轮次。若人工处理后仍在短时重复操作中再次触发登录、验证或防截屏页面，该 workflow 必须从正式无人值守集合排除，不能靠降低验收标准通过。

## 正式运行与复盘

```powershell
mobile-profiler campaign validate examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1

mobile-profiler campaign test examples\android-two-stage-campaign.json `
  --device 10AF3Q11JP001X1 `
  --max-rounds 1
```

正式结论至少记录：输出目录、`active_duration_s`、录制退出码、不同 workflow 成功覆盖率、隔离列表、是否出现 `take_over`，以及 `acceptance.passed`。任何一项不满足都应先做短测迭代，不能直接重复两小时大跑。
