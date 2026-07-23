# 确定性视觉自动化试验

该试验把 March7thAssistant 中可复用的“截图识别 → 状态图 → 动作 → 再识别”机制实现为
本项目已有自动化契约上的可选组件，不复制上游代码、模板或游戏任务资源。

## 当前范围

- `OpenCvTemplateMatcher`：按归一化区域和多个缩放比例执行模板匹配；
- `TemplateMatchVerifier`：返回匹配分数、阈值、区域和可选标注截图证据；
- `VisualScreenGraph`：仅包含已声明状态和强类型动作；
- `ScreenGraphSkill`：通过 `SkillRuntime` 观察、执行白名单动作并受全局 transition 上限约束；
- JSON 资源包不接受 Python 调用字符串，也不使用 `eval`、`exec` 或任意 shell。

现有 `vision`、`uiautomator2` 和 `hybrid` 模型引擎没有改变。该试验位于确定性 Skill/Verifier
层，后续可由场景执行器直接调用，或由模型选择并监督。

## 安装与合成演示

图像依赖保持可选：

```powershell
python -m pip install -e ".[image]"
python tools\deterministic-visual-demo.py --iterations 50
```

结果写入 `profiler-runs/deterministic-visual-spike/`：

- `frame.png`：模拟 Android 游戏截图；
- `template.png`：待识别模板；
- `match-overlay.png`：匹配位置和分数；
- `result.json`：均值、P95 延迟和精确坐标。

也可以启动本地 Dashboard，进入一级导航“开源自动化”（`#opensource`），展开页面底部
“适配诊断”后运行：

```powershell
mobile-profiler ui
```

页面通过 `POST /api/open-source-automation/demo` 复用包内实现，不从 `tools/` 导入；
`/api/state` 返回依赖、硬盘估算、资源包、运行结果和日志，三张 PNG 证据由受限的
`/api/open-source-automation/evidence/{frame|template|overlay}` 路由读取。OpenCV 不存在时
Dashboard 仍可正常启动，页面会显示安装命令并禁用验证按钮。

2026-07-22 在 Windows x64、Python 3.13 下实测，`numpy 2.5.1` 与
`opencv-python-headless 4.13.0.92` 的清理缓存后安装体积合计为
167,348,599 字节（约 159.6 MiB）：OpenCV 约 108.3 MiB，NumPy 与其动态库约
50.7 MiB。项目代码与合成示例只增加 KB 级空间；真实资源包体积取决于模板数量。

同一环境的 720×1280 合成画面中，模板以 1.0000 分准确命中预期坐标。50 次运行中，
复用同一 observation 的平均匹配时间约 9 ms；每次使用新截图内容时平均约 13 ms、P95
约 17 ms。该数字只代表主机侧合成基准，真机结论仍需同时计入 ADB 截图耗时和对设备的
测量扰动。

示例资源包结构见
[`examples/deterministic-visual-spike.json`](../examples/deterministic-visual-spike.json)。模板路径相对
资源包 JSON 解析，region 使用 `[left, top, right, bottom]` 的 0～1 归一化坐标。

## 与上游流程编排的前端核对

已按上游最新“流程编排”界面逐项核对，并在新页面中展示真实状态：

- 点击/查找图片：已对齐模板、阈值、ROI，并补充多尺度搜索与标注证据；
- 流程编排：当前用强类型状态图和 transition 上限替代通用脚本，属于部分对齐；
- 点击坐标/按键：动作 JSON 已强类型化，真机 ADB Gateway 和审批策略尚未接入；
- 点击/查找文字：当前未引入 OCR 模型；
- 模板采集、流程导入导出：资源包格式已定义，采集器与管理前端尚未实现；
- 运行调试：合成验证、耗时、精确坐标、PNG 证据和日志已经可操作。

因此本轮页面没有放置“看起来可点但没有后端”的 OCR、模板采集或真机执行按钮；待主机侧
效果确认后，再按资源包管理、ADB Gateway、通用控制节点的顺序接入。

## 接入真机前仍需完成

1. 实现统一的 `AdbDeviceGateway`，将 `tap`、`swipe`、按键等动作接入 Policy/Approval；
2. 实现 `ScenarioEngine`，统一节点重试、超时、cleanup 和最终 Verifier；
3. 为目标游戏建立按包名、版本、方向和参考分辨率分层的独立资源包；
4. 横评截图频率对被测设备功耗、温度和帧率的扰动，测量阶段默认使用低频确认或短时 burst；
5. 资源包按需下载，不把所有游戏模板塞进核心便携包。
