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

## 当前非问题

- `1260×2800` 是设备物理分辨率，不需要也不应改成 16:9。
- 竖屏阶段 viewport `configured=true, active=false` 是启动前的预期过渡；只有游戏应该
  已横屏但 20 秒内仍未 active 才是故障。
- alignment 切换只重绘同一 raw frame，不应增加 frame ID。
- Go Agent 现在执行普通 `go test ./...`；不再需要 `-vet=off`。
- MXU 队列停止只是调度终态，不能替代任务自己的自然业务终点。

## 剩余验收项

1. 在同一物理设备身份下安全重绑当前 ADB transport，完成只读 preflight。
2. 强化启动终点并取得 `AndroidOpenGame + InWorld + landscape + terminal 1280×720`。
3. 为 PullCount 增加寻访页面门禁，先验证大世界负向拒绝，再验证完整自然终点。
4. 按 SceneProbe、CaptureUidProbe、ViewportInputProbe 顺序取得结构化 alignment/frame ID
   和触点释放证据。
5. 长时间统计截图后端切换率与延迟，确认退避没有造成持续卡顿。
6. 获得明确授权后再运行受保护任务；领取、购买、出售、生产、消耗和 BakerEntry 不得
   从低风险 Probe 权限推导。
7. 完整组合自然结束、无未解决高风险 incident 且人工复核通过前，不开放 UI/API，
   不设置 `end_to_end_verified=true`。
