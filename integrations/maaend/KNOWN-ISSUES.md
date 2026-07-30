# MaaEnd 手机适配已知问题

机器可读真源为 [`issues.json`](issues.json)。

| ID | 状态 | 问题 | 当前处理 |
|---|---|---|---|
| `MAAEND-VIEW-001` | mitigated | 竖屏连接被精确 `1280×720` 门禁提前终止 | 只为已验证的 AndroidOpenGame 无坐标链开放 20 秒过渡；已取得真机终态 `1280×720` |
| `MAAEND-CAP-001` | mitigated | active 截图后端运行时失败后无降级 | 保留候选、自动切换、最长 32 帧退避；待真机长时间验证 |
| `MAAEND-ENV-001` | mitigated | 等待期间手机熄屏并进入 Keyguard | 自动处理 `secure=false` 的唤醒/普通锁屏；安全锁或状态未知仍 fail-closed |
| `MAAEND-TOOL-001` | fixed | Windows GBK 控制台无法打印含 emoji 的 snapshot | 控制台使用 UTF-8 精简摘要，完整内容写 `result.json` |
| `MAAEND-CPP-001` | fixed | viewport C++ 测试调用不存在的 `as_int64()` | 改用 Maa JSON API 的 `as_integer()`，三项定向 C++ 测试通过 |
| `MAAEND-GO-001` | fixed | CaptureUid 错误包装中的动态格式触发 Go vet | 保持 fail-closed 语义并改用 `%v`，全量 Agent `go test ./...` 含 vet 通过 |
| `MAAEND-UPD-001` | mitigated | MXU 自动更新在任务运行中覆盖 v2.20.0 并重启宿主，导致任务状态丢失 | 已隔离 v2.21 树/WebView 状态并恢复 v2.20；清空 `mirrorchyan_rid`，十文件部署；运行中/终态完整性锁检测漂移并强停 |
| `MAAEND-LAUNCH-001` | open | `AndroidOpenGame` 在加载完成前返回 succeeded | 增加 `InWorld` 或等价可操作场景强终点，后续任务不得从加载画面开始 |
| `MAAEND-PULL-001` | open | PullCount 无寻访页面门禁，在无关画面误识别资源数字 | Custom init 前必须识别目标页面；负向测试不得记录任何资源 |
| `MAAEND-PROBE-001` | open | 三个内部 Probe 只有离线终点与回归证据 | 依次取得 Scene、CaptureUid、ViewportInput 真机证据后才能进入普通任务 |
| `MAAEND-E2E-001` | open | 终末地完整真机流程尚未自然结束 | 保持 `end_to_end_verified=false` |
| `MAAEND-GO-002` | mitigated | MapTracker 大地图动作继承了陈旧 viewport | 大地图截图与动作固定 Center；左摇杆/右相机分别固定 Left/Right，仍需真机自然终点回归 |
| `MAAEND-HOST-001` | fixed | stop-file 竞争把中止任务误记为业务成功 | Framework 终态之外强制校验逐任务 Pipeline 自然终点 |
| `MAAEND-USB-001` | open | 手机处于 MIDI USB 用途时不会枚举 ADB 接口 | preflight 区分接口缺失与驱动绑定失败，并校验物理序列号 |
| `MAAEND-PROTOCOL-001` | open | 协议空间寻点仍调用桌面角色控制动作 | 已验证 ONNX 光点识别、Right 相机对齐、Left 摇杆 200 ms 步进和“触碰”终止语义；尚未迁入 Agent wrapper |
| `MAAEND-FIGHT-001` | open | AutoFight 仍绑定桌面键鼠 | 真机触控序列已完成一次 4 敌人战斗并进入奖励选择，但领奖、体力与活跃度终点尚未闭环 |

## 当前非问题

- `1260×2800` 是设备物理分辨率，不需要也不应改成 16:9。
- 竖屏阶段 viewport `configured=true, active=false` 是启动前的预期过渡；只有游戏应该
  已横屏但 20 秒内仍未 active 才是故障。
- alignment 切换只重绘同一 raw frame，不应增加 frame ID。
- Go Agent 现在执行普通 `go test ./...`；不再需要 `-vet=off`。
- MXU 队列停止只是调度终态，不能替代任务自己的自然业务终点。

## 剩余验收项

1. 将协议空间的光点搜索、相机对齐、摇杆步进和“触碰”动作迁入带 alignment/frame ID 的 Agent wrapper。
2. 为 AutoFight 增加 ADB HUD 识别与 Right viewport 触控动作，并以战斗计时/击杀进度驱动 watchdog。
3. 完成 `战斗胜利 → 奖励领取 → 体力变化 → 活跃度变化` 的同一轮自然终点核验；奖励组弹窗不能单独算成功。
4. 为 PullCount 增加寻访页面门禁，先验证大世界负向拒绝，再验证完整自然终点。
5. 按 SceneProbe、CaptureUidProbe、ViewportInputProbe 顺序取得结构化 alignment/frame ID
   和触点释放证据。
6. 长时间统计截图后端切换率与延迟，确认退避没有造成持续卡顿。
7. 获得明确授权后再运行受保护任务；领取、购买、出售、生产、消耗和 BakerEntry 不得
   从低风险 Probe 权限推导。
8. 完整组合自然结束、无未解决高风险 incident 且人工复核通过前，不开放 UI/API，
   不设置 `end_to_end_verified=true`。
