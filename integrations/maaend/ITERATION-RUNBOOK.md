# MaaEnd 分辨率适配持续迭代手册

## 验收层级

| 层级 | 成功条件 | 可解除端到端门禁 |
|---|---|---|
| 离线 | 三组源码审计、三份补丁一致、C++/Go/Python 测试通过 | 否 |
| 只读 smoke | 设备身份、raw 截图和 viewport capability 正确 | 否 |
| 启动过渡 | 单一 `AndroidOpenGame` succeeded，横屏且终态 controller 截图为 `1280×720` | 否 |
| 内部 Probe | Probe 达到各自自然终点，alignment/frame ID/输入证据完整 | 否 |
| 受保护组合 | 已授权任务全部达到业务终点，无未解决高风险 incident | 否 |
| 完整验收 | 组合自然结束且截图、动作轨迹和账号影响经人工复核 | 是，人工复核后 |

`MaaEndRuntimeController.snapshot()` 在最后一级完成前必须保持
`end_to_end_verified=false`。`launch_verified`、`pipeline_verified`、
`custom_agent_verified`、`input_verified` 和 `guarded_flow_verified` 是分层证据，不能替代
最终布尔值。

## 1. 校验账本、源码和补丁

所有命令必须同时指定 Framework、MaaEnd 与 Go binding 三份固定源码：

```powershell
python tools/maaend-iteration.py validate

python tools/maaend-iteration.py source-audit `
  --framework-source .codex-research/MaaFramework-v5.12.1 `
  --maaend-source .codex-research/MaaEnd-v2.20.0-git `
  --go-binding-source .codex-research/maa-framework-go-v4.0.0-beta.17

python tools/maaend-iteration.py patch-check `
  --framework-source .codex-research/MaaFramework-v5.12.1 `
  --maaend-source .codex-research/MaaEnd-v2.20.0-git `
  --go-binding-source .codex-research/maa-framework-go-v4.0.0-beta.17
```

补丁同步覆盖 tracked、staged 和工具中明确登记的 untracked 源码。新文件必须先审查并
加入 `tools/maaend-iteration.py` 白名单，不能被静默塞入补丁：

```powershell
python tools/maaend-iteration.py patch-sync `
  --framework-source .codex-research/MaaFramework-v5.12.1 `
  --maaend-source .codex-research/MaaEnd-v2.20.0-git `
  --go-binding-source .codex-research/maa-framework-go-v4.0.0-beta.17
```

`patch-sync` 校验三个精确上游 HEAD，拒绝未知版本和空 diff。

## 2. 构建

```powershell
./tools/build-maaend-viewport.ps1 `
  -FrameworkSource .codex-research/MaaFramework-v5.12.1 `
  -MaaEndSource .codex-research/MaaEnd-v2.20.0-git `
  -GoBindingSource .codex-research/maa-framework-go-v4.0.0-beta.17 `
  -GoExecutable .codex-research/go1.25.6/go/bin/go.exe `
  -SkipPatch
```

对尚未应用补丁的干净上游工作树删除 `-SkipPatch`。脚本使用临时 modfile 指向本地
Go binding，不修改 MaaEnd 上游 `go.mod`，并执行：

- `viewport::transform`、`viewport::controller`、`screencap::failover`；
- Go binding 和 MaaEnd Agent 普通 `go test`（包含 vet，不使用 `-vet=off`）；
- MaaEnd C++ Agent 编译；
- 生成 `MaaFramework.dll`、`MaaAdbControlUnit.dll`、`MaaUtils.dll`、
  `MaaToolkit.dll`、`MaaAgentClient.dll`、`MaaAgentServer.dll`、`go-service.exe` 和
  `cpp-algo.exe`；
- 输出每个产物的 SHA-256。

## 3. 部署与回滚

部署前必须停止 MaaEnd、MXU、go-service 和 cpp-algo。脚本覆盖六个 DLL、两个 Agent、
一份 MXU 配置和禁用自动更新的 `interface.json`，共十个文件；部署前备份，复制后逐文件
校验 SHA-256。受管目录固定在 v2.20.0，必须清空 `mirrorchyan_rid`，避免启动过程中自动
更新、重启宿主并丢失 MXU 内存任务状态：

如果 runtime 已被自动更新污染，不能在新旧资源树上做文件叠加。先运行只读计划，再把
旧受管树和 `cache/webview_data` 整体移入同盘证据目录，并恢复硬化 staging。脚本不删除
`config`、`debug`、`mobile-profiler-backups` 或 API host；切换失败会把原树移回：

```powershell
./tools/restore-maaend-runtime.ps1 `
  -Action Plan `
  -RuntimeRoot D:/MaaEnd-win-x86_64-v2.20.0 `
  -StagingRoot D:/MaaEnd-restore-20260728-v2.20.0

./tools/restore-maaend-runtime.ps1 `
  -Action Restore `
  -RuntimeRoot D:/MaaEnd-win-x86_64-v2.20.0 `
  -StagingRoot D:/MaaEnd-restore-20260728-v2.20.0
```

```powershell
./tools/deploy-maaend-viewport.ps1 `
  -Action Deploy `
  -RuntimeRoot D:/MaaEnd-win-x86_64-v2.20.0 `
  -FrameworkBin .codex-research/MaaFramework-v5.12.1/build-adb-viewport/bin `
  -AgentExecutable .codex-research/MaaEnd-v2.20.0-git/build-mobile-profiler/go-service.exe `
  -CppAgentExecutable .codex-research/MaaEnd-v2.20.0-git/agent/cpp-algo/build-mobile-profiler/bin/cpp-algo.exe `
  -MxuConfig D:/prepared/mxu-MaaEnd.json `
  -InterfaceFile .codex-research/MaaEnd-v2.20.0-git/assets/interface.json
```

回滚必须显式指向 runtime 内 `mobile-profiler-backups` 的子目录。脚本会解析绝对路径并
拒绝目录逃逸：

```powershell
./tools/deploy-maaend-viewport.ps1 `
  -Action Rollback `
  -RuntimeRoot D:/MaaEnd-win-x86_64-v2.20.0 `
  -BackupRoot D:/MaaEnd-win-x86_64-v2.20.0/mobile-profiler-backups/TIMESTAMP
```

## 4. 真机分层试跑

### 4.1 启动过渡

runner 先读取 wakefulness 与 Keyguard 的 `showing/secure` 状态。对于 `secure=false` 的
普通熄屏或滑动锁屏，可以发送 `KEYCODE_WAKEUP`、`wm dismiss-keyguard`，必要时按当前
display 尺寸上滑；`secure=true` 或状态无法判断时继续 fail-closed，要求用户人工处理。
该过程不调用 `wm size`。随后以 `ro.serialno` 复核物理设备，并只重绑 ID 为
`mobile-profiler-adb` 的受管 MXU Profile：

```powershell
python tools/maaend-viewport-trial.py `
  --runtime-root D:/MaaEnd-win-x86_64-v2.20.0 `
  --adb C:/platform-tools/adb.exe `
  --device DEVICE_TRANSPORT `
  --output-root debug/maaend-open-game-001 `
  --max-seconds 180 `
  --configure-managed-profile
```

必须同时满足：

- 唯一任务为 `AndroidOpenGame` 且 MXU 状态 `succeeded`；
- Go 日志确认经过经校验的无坐标启动链；
- raw 从竖屏过渡为游戏横屏，viewport 从 inactive 变为 active；
- terminal controller screenshot 精确为 logical `1280×720`；
- 后续只读 `SceneProbe` 命中 `InWorld` 或等价可操作场景；仅 viewport active 不再作为
  后续业务任务的启动就绪证据；
- `result.json` 的 `successful=true`。

任一条件失败都停止，不叠加 Agent Probe，并登记 incident。

### 4.2 内部 Probe

Probe 使用专用空任务 ADB Profile；若 Profile 同时启用了普通任务，或普通运行错误使用
空 Profile，Guard 都会拒绝。

| Probe | 固定边界 | 自然终点 |
|---|---|---|
| `SceneProbe` | entry=`InWorld`，override 强制 `next=[]`，禁止坐标输入 | `InWorld` 命中，alignment 合法且 frame ID 非零 |
| `CaptureUidProbe` | entry=`AutoStockpileGetUid`，Left，`use_cache=false`、`stay_on_current_screen=true`、`allow_unknown=false`、`next=[]` | 只保存 UID 哈希证据，alignment/frame ID 有效 |
| `ViewportInputProbe` | 先识别 `InWorld`；请求必须同时带 `authorized_probes:["ViewportInputProbe"]` 和 `allow_viewport_input_probe:true` | Left contact 0 Down/Up；Right contact 1 小幅往返 Move/Up；相机已回程且无活动触点 |

`ViewportInputProbe` 只验证安全大世界中的摇杆中心触点和可逆相机位移，不允许攻击、
交互或角色移动。Probe 未通过时不得进入普通任务。

### 4.3 普通任务

普通任务按 `guard-policy.json` 的 wave 放行。未知任务、资源树哈希不符、未声明 ADB、
RealTimeTask、缺少 `authorized_tasks` 或 BakerEntry 独立授权时均 fail-closed。MXU 队列停止
不等于业务成功；每项任务必须满足自己的 `terminal_contract`。

## 5. Watchdog 与问题闭环

运行时联合监控 MXU 逐任务状态、Framework/Agent 结构化事件和周期截图，包括：

- 长时间无语义进展、同节点循环、静态截图重复；
- 连续慢截图、截图后端反复切换；
- Tasker 停止但逐任务终态不完整；
- TouchDown 后触点超时未释放；
- MXU API 连续错误；
- MaaEnd.exe、interface、六个 DLL、两个 Agent 或资源树在运行中发生漂移。

每轮启动会保存关键文件和资源树的完整哈希基线。周期检查先比较 size/mtime，变化时重算
文件哈希；无论 metadata 是否变化，自然业务终态都会绕过缓存重哈希完整资源树。任何一次
漂移都会锁存，后续即使文件被还原也不能把本轮恢复为成功。

失败 bundle 至少包含请求、environment manifest、设备/二进制/资源哈希、raw 与 logical
截图、alignment 轨迹、MXU 状态、Framework/Agent 日志和去重 fingerprint。账号截图与
日志默认只保留在本机 `debug/`；人工去敏并明确确认后才能晋升 fixture：

```powershell
python tools/maaend-iteration.py promote-fixture `
  --incident debug/RUN/incidents/FINGERPRINT/incident.json `
  --id scene-001 `
  --page world `
  --task SceneProbe `
  --alignment center `
  --confirmed-redacted `
  --redaction-note "UID and account data masked"
```

修复完成后依次重新执行 validate、source-audit、patch-check、离线测试和对应 fixture
回归；没有真机业务证据时不得设置 `end_to_end_verified=true`。
