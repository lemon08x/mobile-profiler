# ADB AI 自动化内核边界

## 1. 当前状态

本阶段只建立一个独立的 `mobile_profiler.automation` 公共 API，不接入现有
`adb_agent.py`、任务编排、HTTP API、前端或功耗测试。现有截图 Agent 的行为和依赖均
不改变。

目标不是把第三方项目作为黑盒塞进测试流程，而是把其中可复用的机制重新实现为小型、
可替换的内部模块：上层只依赖契约，ADB、uiautomator2、图像识别、模型和游戏专用算法
以后分别通过适配器注入。

```text
ScenarioEngine / AgentPlanner (Qwen)
             |
             v
        SkillRuntime facade
       /       |          \
observe      act         verify
   |           |            |
Semantic   ActionPolicy   Verifier registry
UI Provider    |              |
               v              v
          ApprovalGate    deterministic evidence
               |
               v
          DeviceGateway

Watcher 只产生干预建议；不能绕过 Policy/Approval 直接操作手机。
EvidenceSink 旁路记录以上所有边界事件和制品。
```

核心包继续保持 Python 标准库零依赖。未来的 `uiautomator2`、OpenCV、Airtest 等实现必须
作为可选 Provider/Skill 存在，卸载后仍可使用纯 ADB 回退实现。

## 2. 开源机制与内部模块映射

下表中的“内化”指依据公开行为和架构重新设计接口及算法，不复制源代码、游戏资源、
识别模板、任务资源包或第三方项目的业务流程。

| 参考项目 | 需要内化的关键机制 | 内部落点 | 本项目的功能要求 |
|---|---|---|---|
| [uiautomator2](https://github.com/openatx/uiautomator2) | 常驻语义服务、selector、元素等待、元素中心点击、UTF-8 输入、服务失效恢复 | `SemanticUiProvider`、`DeviceGateway`、`UiSettler` | 同时返回截图、压缩 UI 元素、前台 Activity 和统一 revision；`tap_element` 必须绑定 revision；Provider 可健康检查和恢复；保留纯 ADB `uiautomator dump` 回退 |
| [MaaFramework](https://github.com/MaaXYZ/MaaFramework) | 设备能力探测、Pipeline、动作/识别分离、回调、录制与回放 | `DeviceCapabilities`、`ScenarioDefinition`、`EvidenceSink` | 场景只能引用注册组件；支持分支、重试、超时、全局步数上限；设备动作是强类型白名单而不是 shell 字符串 |
| [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights) | “识别 → 动作”节点、失败分支、超时和资源包分层 | `ScenarioNode`、`Verifier`、`ComponentRegistry` | 任务数据与执行器分离；静态校验重复节点、悬空引用、未知组件和不可达节点；不复制 AGPL 业务代码及图片资源 |
| [ok-end-field](https://github.com/AliceJump/ok-end-field)、[March7thAssistant](https://github.com/moesnow/March7thAssistant)、[StarRailCopilot](https://github.com/LmeSzinc/StarRailCopilot) 等游戏自动化 | 触发式检测、导航状态机、小地图/图像识别、专用实时控制和任务调度 | `Skill`、`SkillRuntime` | Qwen 只选择、参数化并监督技能；高频战斗/导航由有界技能执行；技能只能使用 Runtime 暴露的观察、动作、验证和证据接口；不复制模板和路线资源 |
| [Open-AutoGLM](https://github.com/zai-org/Open-AutoGLM) | 当前 App 上下文、包名映射、中文输入、敏感动作确认 | `DeviceContext`、`ActionPolicy`、`ApprovalGate` | 模型看到当前 package/activity/IME；输入实现可替换；支付、账号、授权、删除和不可逆操作必须由宿主定级并审批 |
| [AndroidWorld](https://github.com/google-research/android_world) | 任务初始化、随机参数、确定性成功判定、清理 | `ScenarioDefinition.setup/cleanup`、`Verifier` | setup/cleanup 与主体步骤分开；cleanup 在成功、失败、取消后都应尝试；模型的 `finish` 不能代替最终 Verifier |
| [DroidBot](https://github.com/honeynet/droidbot) | UI 状态签名、状态转移图、重复状态和循环发现 | `ui_state_signature`、`UiStateTracker` | 签名不依赖一次性 element ID；支持按任务选择文本/属性；检测原地重复和短周期循环；只保存有界历史 |
| [Airtest](https://github.com/AirtestProject/Airtest) | 图像断言、步骤截图、失败证据和报告结构 | `Verifier`、`Artifact`、`EvidenceSink` | 图像能力作为可选 Verifier；每次断言必须返回状态和证据 ID；原图、裁剪、阈值和匹配区域可复现 |

## 3. 模块职责和要求

### 3.1 `contracts.py`：稳定数据契约

- `TaskCapabilityProfile` 为每个测试项选择最佳 observation、action、verifier、watcher、
  skill 和 settle 策略，而不是向所有任务暴露全部 ADB 能力。
- `Observation` 使用同一个 revision 绑定截图、UI 树和设备上下文。
- `UiElement.element_id` 只在当前 revision 内有效。语义动作必须携带
  `observation_revision`，避免 UI 改变后点击旧位置。
- `ActionRequest` 是具名、带风险等级的强类型动作请求；它不是命令行或 shell 载体。
- `AgentRequest/AgentDecision` 把 Qwen 的一次决策限制为 action、skill、finish 或
  take-over 四类互斥指令；模型协议和 Prompt 细节不会泄漏给场景引擎。
- `VerificationPlan/VerificationReport` 支持 `all` 或 `any` 的确定性验证组合；单项结果
  区分 `passed`、`failed`、`inconclusive` 和 `error`，避免把“无法观察”误判为失败或
  成功。
- `Artifact` 同时支持内存数据和外部 URI，并校验 SHA-256，供截图、XML、匹配裁剪和
  日志统一引用。

### 3.2 `ports.py`：依赖倒置接口

- `DeviceGateway` 只执行已注册动作并负责设备健康/恢复；具体 ADB 命令被封装在实现内。
- `SemanticUiProvider` 负责组合任务所需的观察通道；首批实现计划为
  `AdbUiDumpProvider` 和可选 `Uiautomator2Provider`。
- `ActionPolicy` 在动作执行前检查 Profile、revision、参数、前台 App、风险和速率；
  `ApprovalGate` 是独立的人机确认边界。
- `Watcher` 只能返回 `WatcherIntervention`，不得自行点击“允许”等弹窗。
- `AgentPlanner` 隔离 Qwen/OpenAI-compatible/Anthropic/Gemini 等模型传输；其 `finish`
  只是完成建议，仍需最终 Verifier 通过。
- `VerifierEngine` 组合已注册的确定性 Verifier，并保留每一项证据，不把模型判断作为
  测试通过条件。
- `SkillRuntime` 是专用技能唯一可见的宿主接口，避免游戏算法反向依赖 UI、HTTP、模型
  客户端或具体 ADB 类。
- `EvidenceSink` 对运行目录、数据库或远端存储一无所知；实现负责持久化。

### 3.3 `scenario.py`：场景图

`ScenarioDefinition` 含独立 setup、cleanup、入口节点、最终验证器和全局 transition 上限。
节点可调用三类操作：

- `action`：单个确定性设备动作；
- `skill`：有界的专用状态机或算法；
- `agent`：让 Qwen 基于最新 observation 选择下一步。

`ScenarioGraphValidator` 已实现纯静态校验，不需要连接手机或模型。它检查组件注册、重复
节点、入口、转移目标、终点、可达性，并可检查场景引用是否超出当前测试项 Profile。
实际 `ScenarioEngine` 仍只是接口，后续实现必须保证每节点超时/重试、全局 transition
上限、协作取消，以及 finally 语义的 cleanup。

### 3.4 `state.py`：状态与循环

`ui_state_signature` 默认使用前台 package/activity、控件类型、resource-id、描述、交互
属性和归一化 bounds；忽略临时 element ID 和动态 text。任务可启用 text 或忽略指定
resource-id。无 UI 树时才回退到截图摘要。

`UiStateTracker` 保存有界状态/转移历史，识别连续相同状态和 2～N 状态短循环。它只提供
证据，最终采用重试、回退、重规划还是接管由上层策略决定。

### 3.5 `registry.py`：组件注册

`ComponentRegistry` 只管理 descriptor 到实现的映射，拒绝隐式覆盖并提供显式
`replace=True`。技能、Verifier、Watcher、Provider 可以分别创建 registry，核心无需导入
任何具体实现。

## 4. 安全和许可证边界

- 不提供 `shell`、`exec` 或任意 ADB 参数动作；具体 Gateway 必须逐动作校验参数。
- 模型不能自行降低 `ActionRisk`，Profile 也不能跳过宿主 Policy。
- Watcher 默认不自动接受权限、隐私、账号、支付或删除弹窗。
- 游戏自动化仅用于测试账号和允许的测试环境，不实现注入、内存读取、反检测或绕过风控。
- 本模块没有复制任何上游实现或资源。以后引入第三方运行时前，需单独确认其许可证、
  再分发要求和目标项目的服务条款；强许可证业务资源不得进入本项目核心包。

## 5. 本阶段明确不做

- 不修改 `AdbAgentController`、`phone_action` 或当前 system prompt。
- 不实例化 uiautomator2，不增加第三方依赖，不向手机安装服务。
- 不增加 HTTP API、SSE/MCP/CLI 命令或 UI 配置。
- 不执行场景图，不接入 Qwen，不改变功耗测试生命周期。
- 不导入任何游戏脚本、图片、OCR 模型或资源包。

下一阶段可以在不改公共契约的前提下先实现 `AdbUiDumpProvider`、
`Uiautomator2Provider`、安全的 `AdbDeviceGateway` 和 `ui_tree_stable` Settler，并用独立
测试夹具验证，仍然不必立即接入产品流程。
