# Android 三引擎真机横评

2026-07-21 使用同一台 vivo V2458A、同一 Qwen `qwen3.6-27b` 服务和同一安全规则，顺序测试：

- `vision`：仅向模型发送前后截图；
- `uiautomator2`：仅向模型发送 revision 绑定的语义树；
- `hybrid`：同时发送截图和语义树。

完整可执行 Prompt、APK SHA-256 和运行目录见
[`examples/android-three-engine-game-benchmark.json`](../examples/android-three-engine-game-benchmark.json)。

## 测试协议

每次运行前强制停止目标包并回到桌面。完整目标拆成两个顺序子任务：

1. 进入主玩法并完成一次有前后证据的操作；该任务禁止按 Home。
2. 按 Home 后重新观察；只有最新截图或前台 package 确认桌面才 `finish`。

这样第一个 `finish` 只能推进到桌面收尾任务，不能跳过完整流程的最后条件。步骤数包含模型的
`finish`，输入 Token 为各步 `prompt_tokens` 之和。

## 结果

### 15 Puzzle（Canvas 棋盘）

| 引擎 | 完整结果 | 步数 | 用时 | 输入 Token | 结论 |
|---|---:|---:|---:|---:|---|
| uiautomator2 | 接管 | 4 | 65.51 s | 42,930 | 只看到整块 `game_view`，看不到数字格；安全接管正确 |
| vision | 通过 | 7 | 33.86 s | 66,297 | 最快；截图可以识别空格和相邻数字 |
| hybrid | 通过 | 7 | 51.82 s | 85,798 | 成功，但语义树对 Canvas 内部没有帮助 |

有效混合运行人工核验到 `MOVES 7 → 8`，数字 4 确实移入空格。不要采用更早的 4 步混合
试跑；该次仅解除暂停便错误声称移动成功，已经由 v10 Prompt 和 Canvas 规则修正。

### Minesweeper Compose（单格语义元素）

| 引擎 | 完整结果 | 步数 | 用时 | 输入 Token | 结论 |
|---|---:|---:|---:|---:|---|
| uiautomator2 | 通过 | 6 | 45.80 s | 66,407 | 精确点击单格，跨分辨率可靠性最好 |
| vision | 通过 | 6 | 36.74 s | 52,950 | 当前设备效率最好 |
| hybrid | 通过 | 6 | 54.28 s | 双证据最强，但输入 Token 接近视觉的 1.91 倍 |

三次运行都完成“New game → 揭开一格 → 后证据确认数字展开 → Home → 桌面确认”。视觉
模式相对 UIA 快 9.06 秒、少 13,457 输入 Token；混合模式相对视觉慢 17.54 秒、多
47,948 输入 Token。

### 新增待检验候选烟测

| 游戏 | 可用引擎 | 真机证据 | 状态 |
|---|---|---|---|
| 2048 | vision | 单次左滑后数字块位置改变，随后回桌面 | 待预备阶段正式复验 |
| Tetris | hybrid | 语义方向键使活动方块横向移动并改变底部堆叠，随后回桌面 | 待正式复验；不采用视觉模式把自然下落误判为右移的首轮结果 |
| Super Snake | vision | 玩家短线从垂直轴向转为水平轴向，`Points/Lives` 保持可见，随后回桌面 | 待正式复验 |
| AstroSmash | vision | 先进入主玩法，再长按左箭头；白色炮台从中心移动到左侧，随后回桌面 | 待正式复验 |
| Asteroid | 无 | 启动流程跳转 Google Play 服务设置页，未进入主玩法 | 阻塞，不加入目录 |

对应 APK、包名、运行目录和人工核验证据记录在
[`examples/android-github-game-candidates.json`](../examples/android-github-game-candidates.json)。烟测中发现模型可能在离开目标应用后错误 `finish`，因此正式 Campaign 现在会在 Agent 完成后读取 resumed package；若不是 workflow 目标包，结果强制改为 `wrong_foreground`，不能标记为支持。

## 当前路由建议

- 标准 Android/Compose 控件、列表、按钮和可访问性良好的格子：优先 `uiautomator2`；
  它的元素点击不依赖机型分辨率。若更看重当前模型成本，可选 `vision`。
- Canvas、OpenGL、SurfaceView 和只暴露整块 `game_view` 的游戏：优先 `vision`。
- 语义元素可用于精确导航、但最终成功只能由像素变化证明的任务：使用 `hybrid`。
- 不把 `hybrid` 作为无条件默认；它在两款游戏上都比视觉更慢、Token 更多。
- 所有要求“操作后回桌面”的候选游戏统一采用两个顺序子任务。

当前样本只有两种典型 UI 结构，结论用于引擎初选而非统计显著性判断。继续验证游戏池时，
按 UIA → hybrid → vision 的顺序寻找可用方案，并保留三模式完整横评的代表样本。
