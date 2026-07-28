# MAA 真机适配问题账本

机器可读真源是 [`issues.json`](issues.json)。本文只提供便于阅读的分类、现场过程和
优先级；修改状态时必须同步机器账本。

## 结论

当前已经跑通的不是“原版 MAA 直接支持任意手机”，而是下面这条受控路径：

```text
2800×1260 全屏截图
  -> adaptive 16:9 viewport
  -> 1280×720 OpenCV 识别
  -> 同 viewport 反向映射输入
  -> 界园资源状态机
  -> 自然失败/成功结算回调
```

真机长跑已走过招募、编队、自动部署、战斗、战后奖励、通宝教程、通宝拾取、事件、
商店投资/购买和是非境特殊路由，最终在第三战“有教无类”失败并自然结算。它证明了
一轮链路能自然闭环，不代表当前策略能够稳定通关。

同一补丁还跑通了 `StartUp`、`Depot`、`OperBox` 和受限参数下的 `Award`。这组验证
补充覆盖了首页导航、长列表滑动、直接 analyzer、左右物理边缘 UI 和任务领奖页面。

## 状态总览

| 状态 | 问题 | 当前处理 |
|---|---|---|
| fixed | `MAA-DEVICE-001` 原版拒绝非 16:9 | 以最大等比 16:9 viewport 替代整机宽高比检查 |
| mitigated | `MAA-VIEWPORT-001` 中央裁剪丢边缘 UI | 普通页面 adaptive Left/Center/Right；仍需逐个覆盖直接 analyzer |
| known_limit | `MAA-VIEWPORT-002` 整屏缩放压缩模板 | 只保留为诊断模式，不作为生产识别方案 |
| fixed | `MAA-INPUT-001` 多点触控绕过代理 | DOWN/MOVE 统一经过 ControlScaleProxy |
| fixed | `MAA-VIEWPORT-003` 识别与点击 alignment 不一致 | 命中 alignment 贯穿到动作 |
| fixed | `MAA-VIEWPORT-004` JustReturn 没有 alignment | 根据动作 X 位置推断视口 |
| mitigated | `MAA-BATTLE-001` 战斗插件绕过通用 adaptive | 战场 Center、详情 Left、部署/控制 Right，结果做跨视口归一化 |
| mitigated | `MAA-PERF-001` adaptive 重复识别 | 从 `任务数×3` 收敛到每 viewport 一次；待下轮真机量化 |
| fixed | `MAA-STATE-001` Continue 后死路 | Continue 重新接回 Begin |
| fixed | `MAA-RECOG-001` StageTrader 低于默认阈值 | 阈值 0.68 |
| fixed | `MAA-RECOG-002` 是非境误命中 DropsFlag | StrategyChange 前置到普通地图路由之前 |
| mitigated | `MAA-SAFETY-001` ExitThenAbandon 破坏性兜底 | runner 默认资源 overlay 改成 Stop，并先生成 incident |
| fixed | `MAA-RUNNER-001` 把任务完成误当自然结束 | 只以 RoguelikeSettlement 判定一轮终点 |
| fixed | `MAA-RUNNER-002` 结算后自动开下一轮 | 结算回调后主动 stop |
| open | `MAA-OCR-001` 结算统计 OCR 不完整 | 先收集失败/成功页面 fixture，再给 settlement analyzer 加 adaptive |
| open | `MAA-PERF-002` ADB 截图约 0.9–1.8 秒 | runner 已统计 p50/p95/max；待比较 raw ADB、adb-lite、设备端 client |
| known_limit | `MAA-STRATEGY-001` 有教无类漏怪 | 属于阵容/策略，不与坐标修复混合判断 |
| fixed | `MAA-EVIDENCE-001` 临时 runner 缺少证据闭环 | 环境 manifest、watchdog、incident、去重、triage、fixture promotion |
| fixed | `MAA-RESOURCE-001` 源码与 runtime 资源漂移 | resource-check 和运行 manifest hash |
| fixed | `MAA-DEPOT-001` 仓库页签与内容在相反边缘 | Right 点击页签、Left 识别内容，并恢复原 alignment |
| fixed | `MAA-RUNNER-003` OperBox 汇总字段拼写错误 | 按协议读取 `all_opers`，并加入单元测试 |
| fixed | `MAA-UI-001` UI 预检后熄屏且 StartUp 重启 | Awake 门禁；已在前台时不重复启动游戏 |
| fixed | `MAA-PATCH-001` 交付补丁落后于工作树 | patch-sync/patch-check 成为交付门禁 |
| fixed | `MAA-GATE-001` 缺少验收字段时曾默认放行 | 显式 true 才开放预检/启动，服务端同样 fail-closed |
| fixed | `MAA-PACKAGE-001` wheel 缺少仓库 tools runner | runner 与护栏策略进入 Python 包，tools 仅作兼容入口 |

## 真机过程中实际遇到的故障链

### 1. 连接前即失败

原版 `ControlScaleProxy` 读取整机 `2800×1260` 后要求精确 16:9，因此不进入截图和
识别。游戏内自定义边距只能移动游戏 UI，无法改变 MaaCore 在连接阶段看到的 display
宽高比，不能单独解决此问题。

### 2. 中央裁剪正确，但页面边缘控件消失

中央 `[280,0,2240,1260]` 能保持模板比例，却看不到物理左右边缘 UI。hybrid 能拼回
两侧，但会在一张逻辑图中混合三套几何关系，后续动作难以保证与识别来源一致。
最终采用 adaptive：每张识别图本身仍是正常 16:9，只切换物理来源。

### 3. 识别正确但输入不一定正确

早期 click/swipe 已经过缩放代理，但 `inject_input_event` 直接进入 backend；多点触控
仍使用逻辑坐标。另一个问题是 Left/Right 命中后若 Controller 已恢复 Center，点击会
整体横移。现在两个问题都由同一个 active viewport transform 解决。

### 4. 通用页面工作，战斗插件仍失败

`ProcessTask` adaptive 后，战斗、编队和技能插件仍直接拿 `get_image()` 构造 analyzer。
部署栏在右、详情在左、战场在中，不能让某一个固定 crop 同时满足。当前实现显式标注
各自的视口，并把部署识别矩形换算回战场逻辑坐标。

### 5. Continue 恢复后卡住

本地 run-19 中，`Continue` 后只尝试 `MissionFailedFlag2` 与退出兜底，连续重试 20 次
后任务结束。将 Continue 接回 Begin 后，后续运行能恢复地图或楼层页面。

### 6. 商店和是非境识别问题

- `StageTrader` 真机 HSV 分数稳定约 `0.692–0.696`，默认约 `0.70`，因此使用
  `0.68`。这个阈值必须配正负 fixture，不能继续凭单张截图下调。
- 是非境底栏以 `0.979` 命中 `DropsFlag`，导致奖励链重试并走向退出。根因不是阈值
  太低，而是状态优先级错误；因此修复是把 `StrategyChange` 放到普通地图路由前。

### 7. ExitThenAbandon 已真实执行过

run-18 event 117 和 run-20 event 130 都记录了 `ExitThenAbandon` 的
`SubTaskStart`，随后约 0.07–0.09 秒就完成 click。仅依赖 Python callback 后再 stop
存在竞争窗口。正式 runner 在 MaaCore 加载完官方资源后，再加载一个只覆盖 action 的
资源目录，把危险任务变成 `Stop`。callback 只负责留证和分类，不承担第一道拦截。

### 8. “完成”事件含义被误判

多次短 run 在识别失败后也得到 `AllTasksCompleted`；StartUp 的
`TaskChainCompleted` 更只说明启动任务结束。自然一轮终点的可靠证据是：

```json
{
  "message": "SubTaskExtraInfo",
  "what": "RoguelikeSettlement",
  "details": {
    "game_pass": false
  }
}
```

runner 会把成功与失败结算都视为“完整走完一轮”，但单独保留 `game_pass`；不会把
自然失败伪装成通关。

### 9. 结算 OCR 结果不稳定

run-11 曾返回 floor、step、combat、recruit、collection、score、exp、skill 等较完整
字段；run-09、run-10 和 run-22 主要只返回 difficulty、emergency、game_pass。
这表明页面确认与自然结算本身可工作，但固定 ROI 在不同视口布局上不稳定。下一步必须
先保留页面样本，再修改 settlement analyzer，避免继续现场猜 ROI。

### 10. 截图慢和动作慢不是同一个问题

观察到单次截图约 `0.9–1.8s`。此前 adaptive 又按任务数乘三个视口重复识别，使总延迟
进一步放大。当前已把同一帧的 adaptive 分析限制为最多三次，但 ADB 抓取本身仍未优化。
降低 OpenCV 的输出尺寸不会减少设备产生/传输完整物理截图的成本；设备端 client 或
更快 backend 必须用相同页面和相同识别语义做对照实验。

### 11. 仓库不是单一 viewport 页面

仓库普通材料阶段可正常滑动并识别，但进入基础物品阶段后曾报错。材料滑动属于
`JustReturn`，会按动作中心把 active viewport 改回 Center；随后“全部”页签位于物理
右边缘，而切换后的基础物品列表从物理左边缘开始排列。单次固定 alignment 无法同时
覆盖这两个目标。

当前流程显式用 Right 识别并点击“全部”，再切到 Left 识别基础物品，结束时恢复调用前
alignment。另一个独立问题是龙门币样本的颜色平方差约 441，高于原预筛阈值 400；只把
基础物品阈值调到 500，普通材料继续保持 400，避免扩大通用误识别面。

### 12. OperBox 成功不等于 runner 汇总正确

`OperBoxInfo` 最终回调实际包含 `all_opers`、`own_opers` 和 `done`。任务本身已完整结束，
但 runner 曾按 `all_oper` 读取，导致 `all_operators` 为 null。现在汇总逻辑按正式协议字段
读取，并用合成回调做单测；运行目录继续保留原始数据，仓库只保存数量级摘要和不变量。

### 13. UI 接入也必须经过一次真实闭环

“开源自动化”适配器首次真机验证时，adaptive 预检成功，但随后 `StartUp` 超过 90 秒
仍未结束。事件表明连接和截图持续正常，画面却已经熄灭。原因有两层：预检耗时叠加
设备屏幕超时，而适配器即使已经确认游戏在前台，仍传入
`start_game_enabled=true` 再次重启游戏。

现在预检读取 `dumpsys power`，只有 Awake 才把 `game_ready` 置为 true；StartUp 根据
预检的前台包决定是否需要启动进程。修复后的同一 UI 适配路径在 `2800×1260` 真机上
返回 adaptive viewport `[280,0,2240,1260]`，随后 8.2 秒完成 StartUp。这个过程也验证了
“目录里显示已接入”不能替代从页面适配器到自然终点的完整实跑。

### 14. 适配器接入不等于流程验收

崩铁与终末地已经有目录或运行时适配代码，但都没有跑通完整真机流程。此前目录层使用
“`end_to_end_verified` 不是 false”作为放行条件，新适配器一旦漏写字段就可能被误判为
已验收；同时后端 `preflight` 入口没有自己的门禁，理论上可绕过前端按钮。

现在执行策略为 fail-closed：适配器必须显式返回 `end_to_end_verified=true`，才会进入
可预检和可启动集合；字段为 false、缺失或快照异常一律禁止。当前只有明日方舟 MAA
满足这一条件。崩铁和终末地继续可见，是为了对照 MAA 的坐标、识别、护栏和证据闭环
做重构，不代表它们可运行。

### 15. 源码目录能跑不等于安装包能跑

UI 适配器最初直接执行仓库根目录的 `tools/maa-*.py`。这种写法在开发工作树中正常，
但 setuptools wheel 不包含根目录 tools，便携版安装后会把本来已验收的 MAA 显示成
运行时缺失。现在三个 runner 已进入 `mobile_profiler` 包，`tools` 下只保留向后兼容的
命令入口；默认危险动作策略也作为 package data 分发，并与审计源做内容一致性测试。

## 当前开放项的处理顺序

1. `MAA-OCR-001`：从下一次自然结算 incident 提升成功/失败各一张 fixture，给结算
   analyzer 增加 alignment 选择，并验证字段完整率。
2. `MAA-PERF-001/002`：在完全相同资源和页面序列下记录截图 p50/p95、识别耗时和
   callback 序列，确认新 adaptive 算法没有优先级回归，再比较截图 backend。
3. `MAA-BATTLE-001`：把战斗/编队/技能页面加入 fixture manifest，防止后续 UI 更新
   再次把直接 analyzer 留在错误视口。
4. `MAA-SAFETY-001`：长期目标是在 MaaCore 任务层提供正式的 destructive-action
   policy；当前资源 overlay 已能可靠保护诊断运行。

## 证据边界

`.codex-research/roguelike-run-*` 和 `.codex-research/maa-other-features/*` 是本机现场证据，
不是可分发源码的一部分。正式运行应使用对应 runner 写入新的独立 run directory。
任何要进入版本库的截图必须先人工确认无账号、通知或个人信息，再用
`promote-fixture` 提升。
