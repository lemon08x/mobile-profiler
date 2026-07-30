# MaaEnd v2.20.0 非 16:9 真机适配

本目录保存 MaaEnd v2.20.0、MaaFramework v5.12.1 和
`maa-framework-go/v4 v4.0.0-beta.17` 的可复现重构补丁、构建部署脚本、风险门禁与
持续改进资料。MaaEnd 补丁按上游 AGPL-3.0 条款提供，其他补丁沿用各自上游许可证；
这里不复制完整源码、资源或二进制发布包。

## 当前结论

`end_to_end_verified` 仍为 `false`。截至 2026-07-28，已完成离线重构、构建、受控恢复、
真机 runtime 部署和一次 USB 启动试跑。该轮取得了 `AndroidOpenGame=succeeded` 与终态
controller `1280×720`，但启动任务在加载画面结束前返回；随后
`PullCountCalculator` 因缺少寻访页面前置门禁而在无关画面误触发 OCR 并失败，因此这些
证据不能提升为完整启动或业务验收。

已完成的能力：

1. Framework 保持 `raw display → 16:9 viewport → 1280×720 logical` 三层坐标，
   不调用 `wm size`，支持 Left、Center、Right adaptive viewport。
2. C ABI option 8、RemoteController、Agent Client/Server、Python、NodeJS 和 Go binding
   均公开 viewport alignment；非法枚举、无 raw frame 和非 adaptive Left/Right 会拒绝。
3. 每个 action 在入队时固定 alignment 与 frame ID；click、long press、scroll、
   swipe、multi-swipe 和 TouchDown/Move/Up 使用同一份快照。触点从 Down 到 Up 固定
   alignment，避免异步执行读取上一节点遗留状态。
4. recognition callback 的 `reco_details` 公开 `viewport_alignment` 和 `frame_id`；
   Controller callback 同时记录 logical param、alignment、display param 和 frame ID。
5. MaaEnd Go/C++ 自定义 Agent 已统一经过 ViewportSession/wrapper；源码审计拒绝 wrapper
   之外新增 `CacheImage`、坐标输入或直接 Android input 命令。
6. 构建链可复现生成六个 Framework/Agent DLL、Go Agent 和 C++ Agent；部署/回滚覆盖
   这八个二进制、MXU 配置和禁用自动更新的 `interface.json`，共十个文件，逐文件校验
   SHA-256。
7. 41 项任务均进入风险清单，其中 27 项声明支持 ADB；未知任务、上游资源哈希漂移和
   未授权持久化操作默认拒绝。
8. 已实现 `SceneProbe`、`CaptureUidProbe`、`ViewportInputProbe` 三个内部 Probe，
   以及自然终点、结构化事件、alignment 轨迹、截图/节点/触点 watchdog、incident
   去重和人工脱敏后的 fixture promotion。
9. `restore-maaend-runtime.ps1` 可先完整哈希并隔离被更新的资源树与 WebView 状态，再以
   同盘准备树原子式恢复固定 v2.20.0；启动基线、周期增量检查和终态全量重哈希会锁定
   MaaEnd.exe、六个 DLL、两个 Agent、interface 和完整资源树，漂移时产生
   `maaend_runtime_drift` 并阻止成功。

当前仍缺少：

- `AndroidOpenGame` 后进入可操作大世界的 `InWorld` 或等价强终点证据；
- 三个 Probe 的真机证据；
- `PullCountCalculator` 的寻访页面 fail-closed 门禁和完整自然终点；
- 面向普通任务组合的逐任务状态、精确 expected-negative 分类和幂等恢复矩阵；
- 获得明确授权后的受保护业务组合自然终点；
- 人工复核通过后才可解除 UI/API 端到端门禁。

## 目录内容

- [`ARCHITECTURE.md`](ARCHITECTURE.md)：截图、识别、alignment、坐标和输入完整链路。
- [`ITERATION-RUNBOOK.md`](ITERATION-RUNBOOK.md)：补丁、构建、部署、Probe、回滚和
  incident/fixture 流程。
- [`REFACTORING-LESSONS.md`](REFACTORING-LESSONS.md)：从 MAA 每日真机闭环提炼的
  页面级 viewport、错误分级、幂等恢复、本地 Qwen 边界和 MaaEnd 实施顺序。
- [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md)：现场问题、已修问题和剩余验收项。
- [`issues.json`](issues.json)：机器可读问题账本。
- [`guard-policy.json`](guard-policy.json)：任务风险、资源哈希、Probe 和 watchdog 契约。
- [`patches/maaframework-v5.12.1-viewport.patch`](patches/maaframework-v5.12.1-viewport.patch)：
  Framework viewport、坐标证据与截图后端故障转移。
- [`patches/maa-framework-go-v4.0.0-beta.17-viewport.patch`](patches/maa-framework-go-v4.0.0-beta.17-viewport.patch)：
  Go binding alignment option 和结构化 callback。
- [`patches/maaend-v2.20.0-viewport-gate.patch`](patches/maaend-v2.20.0-viewport-gate.patch)：
  MaaEnd Go/C++ Agent session、Probe 和竖屏启动门禁。

## 快速离线验证

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

真机试跑必须从 [`ITERATION-RUNBOOK.md`](ITERATION-RUNBOOK.md) 的分层门禁开始。
单一 `AndroidOpenGame` 成功只代表启动过渡闭环，不代表 MaaEnd 完整自动化已验收。
