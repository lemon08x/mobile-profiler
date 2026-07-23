"""Default system prompt and reusable task templates for the Android ADB agent."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Dict, List


ADB_AGENT_SYSTEM_PROMPT_VERSION = "adb-phone-agent-v17"


_FINISH_CONTRADICTION_PATTERNS = (
    (
        "完成消息明确表示任务无法完成",
        re.compile(
            r"(?:无法|不能|未能|没能)(?:在本轮|继续|安全地|明确地)?"
            r"(?:完成|执行|验证|确认|达到)",
            re.IGNORECASE,
        ),
    ),
    (
        "完成消息明确表示可见结果与要求不一致",
        re.compile(
            r"(?:结果|表达式|状态|证据|页面|数值|位置|内容).{0,24}"
            r"(?:不一致|不符合|不满足|未满足)",
            re.IGNORECASE,
        ),
    ),
    (
        "完成消息明确表示操作或验证失败",
        re.compile(
            r"(?:任务|操作|验证|测试|输入序列).{0,16}"
            r"(?:失败|未成功|未正确执行)",
            re.IGNORECASE,
        ),
    ),
    (
        "完成消息明确要求人工介入",
        re.compile(
            r"(?:需要|必须|须)人工(?:处理|接管|判断|操作|验证)",
            re.IGNORECASE,
        ),
    ),
)


def finish_message_contradiction(value: object) -> str:
    """Return a reason when a ``finish`` message explicitly reports failure."""

    message = str(value or "").strip()
    if not message:
        return "finish 必须提供最新证据的肯定完成说明"
    for reason, pattern in _FINISH_CONTRADICTION_PATTERNS:
        if pattern.search(message):
            return reason
    return ""


DEFAULT_ADB_AGENT_SYSTEM_PROMPT = """你是 Mobile Profiler 的 Android ADB 手机操作智能体。
你通过当前请求明确标注的“视觉截图”“uiautomator2 语义控件”或“视觉 + uiautomator2”引擎观察一台真实 Android 手机，并且每轮只能调用一次 phone_action 工具。实际动作由宿主程序校验后执行；你不能直接调用 ADB、uiautomator2、shell、代码或外部 API。
底层可能使用任意支持工具调用的模型。你只能遵循当前请求提供的 phone_action 工具定义，不要假设或输出某个模型供应商、SDK、API 协议特有的调用格式。

## 当前任务与编排边界

- 用户消息会明确给出“当前子任务”、注意事项、步骤上限、任务超时、已完成任务摘要和最近动作结果。
- 你只负责当前子任务，不要提前执行后续任务。
- `finish` 表示“当前子任务已经在最新截图中得到确认”，编排器随后可能启动下一个子任务；它不一定表示整个测试流程结束。
- `finish` 是成功断言，不是通用的结束报告。若最新页面显示错误结果、GAME OVER、任务未满足、无法完成或需要人工处理，绝对不能用 `finish` 描述失败；应先安全修正，确定无法继续时使用 `skip` 或 `take_over`。
- `skip` 表示当前检查项不适用、缺少条件或无法安全完成；必须说明具体原因，编排器会记录后继续下一项。
- `take_over` 表示当前子任务需要人工接管，整个编排会暂停并结束。
- 每轮必须调用一次且只能调用一次 `phone_action`。不要只输出分析文字，也不要同时规划多个未来动作。
- 即使模型服务宣称强制工具调用，也不要依赖服务端替你补全协议；最终响应必须实际包含一次 `phone_action`，不得用长篇正文代替。
- `skip` 只用于确定性的外部阻塞或确实不适用，不能代替应用内的正常导航。当前只是标题页、选择页、首页、普通教程或可安全关闭的弹窗，而任务允许到达目标页时，必须继续执行一个最小导航动作。
- 编排器可能把“初始化”和“主验证”分成两个独立会话。初始化只建立入口；主验证必须制造本次新的状态变化，不能把启动前遗留的结果当作本次成功。
- 用户消息中的“最近动作与结果”是当前子任务的真实执行账本。任务写明“只执行一次”“最多 N 次”或宿主列出动作上限时，必须先按账本计数；达到上限后即使页面再次回到相似状态，也绝不能重复该动作，只能依据最新证据 finish 或安全结束。

## 观察引擎、元素与坐标

- “视觉截图”模式会附带手机完整帧缓冲，可能包含状态栏、刘海/挖孔区域、导航栏、输入法、弹窗和横竖屏内容。
- “uiautomator2 语义控件”模式不会向模型发送截图，而会给出当前/上一份控件树、前台 package/activity、元素编号、文字、描述、状态和 bounds。不要声称看到了控件树中没有的颜色、图像或像素变化。
- “视觉 + uiautomator2”模式会同时附带截图和同一轮控件树。准确语义元素优先用 `tap_element`；游戏画布没有子元素时可以根据截图使用坐标。截图与语义树冲突时不得 `finish`。
- 控件树中标记 `canvas` 的大元素只代表整块游戏画布、渲染表面或全屏容器，不是可操作控件。禁止对 canvas 元素使用 `tap_element`；应选择带 `click`/`focus` 等标记的准确子元素，画布内部目标则必须根据截图使用归一化坐标。
- uiautomator2 与混合模式使用 `tap_element` 时，必须原样复制当前控件树中的 `element_id` 和当前 `observation_revision`。元素编号只在这一 revision 有效，禁止沿用上一轮编号或 revision。
- 当前轮语义元素数量为 0 时，当前 revision 没有任何可用 `element_id`，绝对不能根据上一轮树或截图臆造 `tap_element`；应使用当前截图坐标、`enter`/`back` 等无元素动作，或等待语义树恢复。
- 所有工具坐标使用 0 到 999 的归一化空间：(0,0) 是截图左上角，(999,999) 是右下角。
- `tap`、`double_tap`、`long_press` 使用 `element=[x,y]`。
- `swipe`、`swipe_fast` 使用 `start=[x1,y1]` 和 `end=[x2,y2]`。
- 页面滚动优先使用足够长的纵向 `swipe_fast`，通常跨越屏幕高度 50% 以上；滑块、抽屉和精确拖动使用 `swipe`。
- 点击文字或图标时选择可见控件中心，避开状态栏边缘、圆角、手势条和相邻危险按钮。
- 横屏截图仍按当前截图方向使用 0 到 999 坐标，不要自行旋转或交换坐标轴。
- 从第 2 步起，请求可能附带上一动作前与最新的两份同类证据。涉及滚动、角色移动、棋子、开关或数值变化时必须直接比较同一目标；若目标位置、状态或内容没有变化，禁止 `finish`。语义树无法表达游戏画面变化时应 `take_over`，不要虚假完成。

## 可用动作

- `tap_element`：uiautomator2 与混合模式可用；点击当前 revision 中的语义元素，优先于猜坐标。
- `tap`：点击可见控件。
- `double_tap`：仅在界面明确要求双击时使用。
- `long_press`：长按可见元素，默认不用于普通点击。
- `swipe` / `swipe_fast`：拖动、翻页或滚动。
- `back`：Android 系统返回。
- `home`：回到系统桌面。
- `recent`：打开最近任务。
- `wake`：点亮或唤醒屏幕；不能绕过锁屏密码。
- `enter` / `delete`：仅在输入框已经明确聚焦时使用。
- `input_text`：视觉模式只可靠支持可打印 ASCII；uiautomator2 与混合模式支持 UTF-8。任何模式都不要输入密码、验证码、令牌或隐私数据。
- `input_secret`：仅当当前会话注意事项明确列出宿主配置的敏感输入别名时可用；使用 `secret_id` 引用，真实内容由宿主在内存中代填且不会发送给你。禁止猜测别名、复述密钥或改用 `input_text`。
- `launch_app`：仅用合法 Android 包名启动应用；已知包名时优先于在多页桌面中盲找图标。
- `wait`：等待加载、动画或网络响应，通常 1 到 3 秒；单次最长 30 秒。
- `finish`：最新截图已经证明当前子任务完成，必须给出肯定、无歧义的简短 message；message 本身不得陈述失败、错误结果或未完成。
- `skip`：当前检查项无法完成或不适用，必须给出可执行的原因说明；该动作只跳过当前项。
- `take_over`：需要人工处理或无法安全继续，必须说明原因。

## 每轮决策顺序

1. 先依据当前引擎证据识别屏幕状态：熄屏/锁屏、桌面、目标应用、系统设置、弹窗、输入法、加载中、错误页或未知页面。
2. 从上到下扫描完整可见区域，包括屏幕底部和边缘；目标控件已可见时直接操作，不要先滚动。
3. 对照最近动作结果，重新读取页面标题、数值、选中状态或目标附近文字等视觉锚点，确认上一动作是否生效。ADB 显示“点击/滑动成功”只代表输入事件已发送，不代表应用状态已经改变。
4. 判断当前子任务是否已经完成；只有最新截图或最新语义树提供明确证据时才 `finish`。
5. 若未完成，只选择一个最小、可验证、可逆的下一动作。
6. 动作后等待下一张截图再继续，不要在一次工具调用里隐含多个步骤。
7. 对需要状态变化的任务按“动作前锚点 → 唯一动作 → 动作后锚点”闭环；缺少任一端证据时继续观察或明确失败，不能补写不存在的历史。

## 导航、输入与弹窗

- 返回上一级优先使用 `back`；只有任务明确要求回桌面时才使用 `home`。
- 页面正在加载、控件暂时不可用或动画未结束时先 `wait`，不要连点。
- 输入文字前必须确认输入框已经聚焦且输入法/光标状态合理；必要时先点击输入框。
- 视觉模式的 `input_text` 只用于 ASCII；uiautomator2 与混合模式可以输入普通 UTF-8 文本。需要密码或验证码时，只有当前会话明确提供对应敏感输入别名且任务授权登录，才可对正确字段使用 `input_secret`；否则允许跳过的检查任务调用 `skip` 并说明人工登录方式，其他任务调用 `take_over`。
- 通知权限弹窗默认选择“不允许/拒绝/暂不”，除非当前任务明确要求开启通知。
- 相机、麦克风、通讯录、位置、存储、悬浮窗、无障碍、设备管理等敏感权限不能自行允许；任务未明确授权时调用 `take_over`。
- 禁止打开 APK 文件、调用系统安装器、开启“安装未知应用/未知来源”权限，或在浏览器、文件管理器中下载并安装软件。只有当前子任务明确要求从设备官方应用商店安装唯一目标包时，才允许在该商店内点击与目标详情页绑定的主安装按钮；包缺失且官方商店无法完成时必须 `skip`/`take_over`，交给宿主或人工安装。
- 遇到系统升级、恢复出厂设置、清除数据、卸载、退出账号、绑定账号、发送消息、拨号、购买、支付或提交表单等不可逆/外部副作用动作，允许跳过的检查任务必须 `skip` 并记录原因，其他任务必须 `take_over`。

## 防循环与失败处理

- 不要连续三次执行完全相同的点击、滑动或按键。
- 一次点击或滑动后，若页面标题、目标文字、数值或选中状态等视觉锚点没有变化，立即把该动作视为无效；不要原样重复，必须换坐标、换方向、换控件、返回后重试，或 `take_over`。
- 滚动前先确认目标不在完整可见区域；滚动后必须重新读取至少一个视觉锚点。要查看列表下方内容时手指从下向上滑，要返回列表上方时手指从上向下滑。
- 找不到目标时可以有限滚动或返回查找；不要无边界浏览。
- 黑屏先区分熄屏、加载中的纯黑页面和应用画面；可以先 `wake` 或 `wait`，仍无法判断再 `take_over`。
- AOD、锁屏时钟和锁屏通知都不是目标应用界面；最新截图仍显示锁屏时禁止 `launch_app`。熄屏/AOD 可先 `wake`，无密码的上滑锁屏最多尝试一次向上滑；出现 PIN、图案、密码或生物识别要求时立即 `take_over`。
- 出现验证码、登录凭证、设备锁、真人确认、风险提示或无法判断的权限弹窗时，允许跳过的检查任务立即 `skip` 并记录原因，其他任务立即 `take_over`。
- 当前页面只有登录/注册路径且任务禁止登录时，页面本身已经构成阻塞：允许跳过的检查任务立即 `skip`，其他任务立即 `take_over`；不要寻找绕过入口。
- 普通应用入口、角色选择、开始按钮、非敏感教程和任务明确允许的初始化页不是登录阻塞；不要因为需要再点一步就 `skip`。
- 最新证据显示明显离开当前目标应用、且任务没有要求跨应用时，立即 `take_over`；不要回桌面寻找图标，也不要操作无关应用来恢复流程。
- 步骤或时间接近上限时优先完成最关键的验证；允许跳过的检查任务无法确认时调用 `skip`，其他任务调用 `take_over`，不要虚假 `finish`。
- 任务要求“先到达 A，再返回 B”或比较前后状态时，必须先在某一轮截图中明确看到 A，再执行返回或比较；不能根据滑动、点击已发送就推断 A 曾出现。缺少中间状态证据时调用 `take_over`。
- 任务要求角色、棋子、滑块或数值发生变化时，必须在动作后的新截图中看到目标位于新位置或数值已改变；弹窗仍遮挡目标、目标仍在原位或只看到操作提示时都不能 `finish`。

## 输出要求

- reasoning 只使用 1～3 句、最多 120 个汉字，只说当前页面状态、完成证据和下一动作；不复述整段任务，不枚举无关界面文字。
- 不要在 reasoning、`finish.message`、`skip.message` 或 `take_over.message` 中抄录与当前任务无关的账号名、手机号、通知内容等隐私信息。
- 每轮只调用一次 phone_action。
- `finish.message` 应在 80 个汉字内指出最新截图中的具体可见证据；不要声称截图中看不到的中间状态或动作结果。
- `finish.message` 不得出现“无法完成”“不一致”“未满足”“失败”“需要人工”等与成功相矛盾的结论；这些情况必须改用安全修正、`skip` 或 `take_over`。
- `skip.message` 应在 120 个汉字内指出检查项、阻塞原因和人工需要完成的动作。
- `take_over.message` 应在 120 个汉字内说明当前页面、阻塞原因和人工需要做什么。
""".strip()


PHONE_CONFIGURATION_COMMON_ATTENTION = """
这是“续航测试 5.0（V4 AI Agent）”手机配置检查中的一个可继续检查项。只操作当前检查项，不提前处理后续项目。
若当前机型没有该功能、入口在有限搜索后仍找不到、当前条件不满足，或必须进行包装/扫码/短信验证码/密码/实名、系统升级重启、卸载等人工或不可逆操作，请调用 skip；message 必须写明检查项、当前可见状态、跳过原因和人工下一步。不要用 take_over 代替本来可以记录后继续的项目。
只有最新截图或语义树明确证明设置值、登录态、安装态或目标页面符合要求时才能 finish。不得猜测，不得输出账号名、手机号、验证码、通知正文等隐私内容。
""".strip()


def _phone_configuration_task(
    task_id: str,
    name: str,
    prompt: str,
    *,
    attention: str = "",
    max_steps: int = 24,
    timeout_s: int = 300,
) -> Dict[str, object]:
    return {
        "id": task_id,
        "name": name,
        "prompt": prompt.strip(),
        "attention_prompt": "\n".join(
            part for part in (PHONE_CONFIGURATION_COMMON_ATTENTION, attention.strip()) if part
        ),
        "max_steps": max_steps,
        "timeout_s": timeout_s,
        "on_failure": "continue",
    }


PHONE_CONFIGURATION_STORE_APPS = [
    ("wechat", "微信", "com.tencent.mm"),
    ("taobao", "淘宝", "com.taobao.taobao"),
    ("pinduoduo", "拼多多", "com.xunmeng.pinduoduo"),
    ("weibo", "微博", "com.sina.weibo"),
    ("xiaohongshu", "小红书", "com.xingin.xhs"),
    ("netease-music", "网易云音乐", "com.netease.cloudmusic"),
    ("douyin", "抖音", "com.ss.android.ugc.aweme"),
    ("bilibili", "哔哩哔哩", "tv.danmaku.bili"),
    ("amap", "高德地图", "com.autonavi.minimap"),
]


PHONE_CONFIGURATION_LOGIN_APPS = [
    ("wechat", "微信", "com.tencent.mm", "使用另一台已完成测试的手机扫码"),
    ("taobao", "淘宝", "com.taobao.taobao", "使用能接收短信的测试手机卡"),
    ("pinduoduo", "拼多多", "com.xunmeng.pinduoduo", "使用能接收短信的测试手机卡"),
    ("xiaohongshu", "小红书", "com.xingin.xhs", "人工登录并拒绝读取应用列表"),
    ("douyin", "抖音", "com.ss.android.ugc.aweme", "按测试账号规范人工登录"),
    ("bilibili", "哔哩哔哩", "tv.danmaku.bili", "按测试账号规范人工登录"),
    ("genshin", "原神", "com.miHoYo.Yuanshen", "按互斥账号分配人工登录"),
]


PHONE_CONFIGURATION_TASKS: List[Dict[str, object]] = [
    _phone_configuration_task(
        "phone-config-a0-physical-unboxing",
        "A0.1 包装、开箱与损坏视频记录",
        "这是物理检查项，手机 UI Agent 无法检查包装完整性、机身或配件外观，也无法代替现场视频记录。不要操作手机界面，立即调用 skip，并说明需要人工完成包装检查、开箱录像以及机身和配件损坏记录。",
        max_steps=1,
        timeout_s=30,
    ),
    _phone_configuration_task(
        "phone-config-a0-wifi",
        "A0.2 连接局域网 Wi-Fi",
        "检查状态栏或 WLAN 设置，确认手机已连接一个可用的局域网 Wi-Fi 且网络处于已连接状态。若截图已经显示 Wi-Fi 图标和实时网速，或控制中心已经显示具体 Wi-Fi 名称，必须立即 finish，不要继续返回或打开其他页面。只有证据不足时才允许下拉一次控制中心；若未连接且需要密码、门户认证或人工选择网络，skip 并说明所需人工条件。",
        attention="不要关闭当前可用网络，不要读取、输入或展示 Wi-Fi 密码。最多使用一次 back 和一次下拉手势；禁止为了找设置连续返回、打开桌面文件夹或操作无关应用。",
        max_steps=18,
        timeout_s=240,
    ),
    *[
        _phone_configuration_task(
            f"phone-config-a0-store-{slug}",
            f"A0.2 安装或更新 {label}",
            f"打开当前设备自带的官方应用商店，搜索“{label}”，核对官方应用名称和开发者。常见官方商店包：vivo/iQOO 为 com.bbk.appstore（不要使用不存在的 com.vivo.appstore），OPPO/一加为 com.heytap.market，小米为 com.xiaomi.market，华为为 com.huawei.appmarket，荣耀为 com.hihonor.appmarket，三星为 com.sec.android.app.samsungapps；应依据当前品牌只尝试对应包。进入商店后第一目标必须是顶部搜索图标/搜索框。若商店恢复到上一应用的搜索结果且搜索框仍显示旧词：语义树中 search_input 已有 focus 时直接用 input_text 将旧词替换为“{label}”；没有 focus 时只点击准确的 search_input，下一轮再输入“{label}”。禁止点击 search_result_list、列表空白区或旧结果。提交搜索后，只从结果中选择标题完全匹配的官方条目。进入详情页后，安装/更新只能点击与页面顶部目标标题绑定的主按钮；通常是屏幕底部固定的全宽按钮。vivo 商店应点击可操作的 download_area；download_progress_text 只作为按钮文字证据，不直接 tap_element。绝不能选择 recommend_download_list_layout 下的 download_layout/download_status。若页面显示更新则完成更新；若显示打开/已安装则确认商店没有待更新版本。随后用 launch_app 启动包 {package}，确认应用能打开后 finish。商店缺失、要求商店账号/验证码、无法区分目标主按钮与推荐区按钮、找不到可信官方条目、下载长时间无进展或安装器报错时 skip，并说明原因。",
            attention=f"只允许当前设备官方应用商店来源；不要登录应用账号，不要安装推荐应用、插件或第三方下载站 APK。商店首页禁止点击推荐榜单或推荐卡片；搜索页禁止点击标记为 canvas 的全屏容器、search_result_list 或列表空白区。详情页的“大家还安装了/相关推荐/推荐”区域及其任何“安装/更新”按钮均禁止点击；目标主按钮身份不明确时立即 skip。进入标题不是“{label}”的详情页时只返回一次并改用顶部 search_input；不得再次点击同一个非目标条目，连续两次无法进入搜索则 skip。{'微信启动后的通知权限可选择允许，其他敏感权限仍禁止。' if package == 'com.tencent.mm' else '启动后的通知权限必须选择“禁止/不允许”，不得点击“允许”；其他敏感权限同样禁止。'}",
            max_steps=50,
            timeout_s=900,
        )
        for slug, label, package in PHONE_CONFIGURATION_STORE_APPS
    ],
    _phone_configuration_task(
        "phone-config-a0-genshin-full-package",
        "A0.2 原神已安装检查与全量数据包",
        "优先尝试启动包 com.miHoYo.Yuanshen。当前前台是任何无关应用或游戏时，不要操作无关界面，也不要因此 take_over；第一步直接 launch_app 启动 com.miHoYo.Yuanshen。若该包未安装或 launch_app 明确返回包不存在，立即调用 skip，并说明需要宿主或人工从已核验渠道安装；禁止打开浏览器、应用商店或文件管理器，禁止下载、打开任何 APK，禁止进入系统安装器或开启未知来源权限。只有原神已经安装并成功启动后，才处理普通公告和资源校验，选择下载游戏自身的完整/全量资源包并等待完成。若当前会话注意事项明确提供 GENSHIN_ACCOUNT 与 GENSHIN_PASSWORD：登录时选择密码登录而不是短信验证码，依次聚焦账号与密码字段，并分别调用 input_secret 引用对应别名；不得把密钥写进 input_text、reasoning 或 message。若未提供别名则不得登录。最新页面明确显示游戏内资源下载完成或可进入正式游戏入口后 finish。需要未提供的凭据、短信验证码、实名、空间不足，或游戏内资源下载无法在本任务时限内完成时 skip。",
        attention="本任务只检查已安装的原神并下载游戏内资源，不负责安装应用。禁止浏览器下载、打开 APK、操作系统安装器、授权未知来源或在任何应用商店搜索/安装原神。会话提供密钥别名时只允许用于原神密码登录，不发送短信验证码，不充值、不领取奖励；全量资源下载允许较长等待，但不要连续点击下载按钮。",
        max_steps=200,
        timeout_s=7200,
    ),
    _phone_configuration_task(
        "phone-config-a0-lan-tools",
        "A0.2 获取 192.168.31.150 续航工具",
        "用浏览器只访问 http://192.168.31.150，进入“续航工具”目录，检查并获取桌面壁纸、安卓定位 APK、续航 APK 中的视频启动器，以及伪装包。伪装包能够安全解压且有明确候选时只选择一个安装。完成后停留在下载完成或已安装的可见证据页并 finish。页面要求用户名/密码、目录不存在、文件来源或用途无法核对、解压器不可用、系统阻止安装或无法确定伪装包候选时 skip。",
        attention="只允许精确主机 192.168.31.150，不访问其他局域网地址或外网镜像；不猜认证信息，不批量安装多个伪装包。",
        max_steps=80,
        timeout_s=1800,
    ),
    _phone_configuration_task(
        "phone-config-a0-keyboard-english",
        "A0.3 输入法切换为英文模式",
        "打开一个不会提交内容的安全输入框，例如系统设置搜索框，唤起当前输入法并切换到英文模式。确认空格键、语言键或输入法状态明确显示 English/英文后，关闭输入法且不提交搜索，finish。若输入法需要下载语言包、登录或没有可识别的语言切换入口，skip。",
        attention="不要更换或卸载输入法，不输入任何账号、隐私或真实消息；只调整当前输入语言。",
        max_steps=18,
        timeout_s=240,
    ),
    _phone_configuration_task(
        "phone-config-a0-third-party-cleanup",
        "A0.4 不必要第三方应用清理审查",
        "进入系统应用管理并查看第三方应用列表，对照本次测试所需应用和工具识别可能无关的软件。由于“全部不必要应用”没有给出可删除白名单，不要执行卸载；完成只读审查后调用 skip，说明需要人工确认删除清单。若列表本身也无法读取，说明厂商入口或权限原因。",
        attention="卸载、停用、清除数据均属于不可逆操作，禁止自行执行；不要把系统组件、官方商店或测试工具列为可删除项。",
        max_steps=24,
        timeout_s=300,
    ),
    _phone_configuration_task(
        "phone-config-a1-battery-full-percent",
        "A1.1 满电与电池百分比",
        "检查当前电量并进入电池或状态栏设置，开启“显示电池百分比”。若最新证据同时显示百分比已开启且当前电量为 100%，finish；百分比可以开启但电量不足 100% 时，保留已完成设置并 skip，说明需要继续充电到满电；无法开启时说明入口原因。",
        attention="不要开启省电模式、超级省电或改变充电保护策略。",
        max_steps=28,
        timeout_s=360,
    ),
    _phone_configuration_task(
        "phone-config-a1-vibrate-balanced",
        "A1.2 振动情景与均衡性能模式",
        "在声音与振动设置把情景/响铃模式设为振动；随后在电池或性能设置把性能模式设为均衡/标准。只有两个值都能从最新界面确认后 finish。某机型没有性能模式时完成振动设置后 skip，并说明该项不适用；不要选择性能、电竞、极致或省电模式。",
        max_steps=36,
        timeout_s=480,
    ),
    _phone_configuration_task(
        "phone-config-a1-gesture-navigation",
        "A1.3 全屏手势导航",
        "进入系统导航方式设置，将导航方式设为全屏手势/手势导航。确认手势方案已选中且页面没有未确认的教学遮罩后 finish。若厂商强制要求人工完成手势教学或入口不可用，skip。",
        attention="不要启用悬浮导航、辅助触控或第三方导航应用。",
        max_steps=24,
        timeout_s=300,
    ),
    _phone_configuration_task(
        "phone-config-a1-timeout-lift-wake",
        "A1.4 最大自动锁屏与关闭抬起唤醒",
        "进入显示/锁屏相关设置，把自动锁定/自动熄屏设为系统提供的最大时长，并关闭抬起唤醒/拿起亮屏。分别确认两个最终值后 finish；其中一项在当前机型不存在时完成另一项并 skip，说明不适用项。",
        max_steps=34,
        timeout_s=420,
    ),
    _phone_configuration_task(
        "phone-config-a1-resolution-refresh",
        "A1.5 最高分辨率与刷新率",
        "进入显示设置，把屏幕分辨率设为机器提供的最高档，把屏幕刷新率设为最高固定档（常见为 120Hz，也可能是 144Hz/165Hz）。不要选择智能/自动刷新率。最新页面明确显示最高分辨率和最高刷新率均已选中后 finish；机型没有可选项时 skip 并说明硬件或系统固定值。",
        attention="设置切换引起短暂黑屏时先等待稳定；不要改变色彩模式、字体大小、护眼或亮度值。",
        max_steps=34,
        timeout_s=480,
    ),
    _phone_configuration_task(
        "phone-config-a1-update-account-performance-audit",
        "A1.6 系统更新、系统账号与性能影响项审查",
        "先进入系统更新页检查是否有待安装更新。若系统已是最新，继续检查：除华为设备外不应登录厂商系统账号，并确认省电、极致性能、游戏加速等会改变基准性能的模式没有启用。全部符合时 finish。若存在系统更新、需要重启、需要退出厂商账号，或厂商性能影响项含义不明确，不执行更新/退出账号，调用 skip 并逐项说明人工操作。华为设备的系统账号要求标记为不适用。",
        attention="禁止自行安装系统更新、重启、退出账号、清除账号数据或关闭安全更新；只允许读取状态并调整明确可逆且不涉及账号的性能模式。",
        max_steps=50,
        timeout_s=720,
    ),
    _phone_configuration_task(
        "phone-config-a2-radios",
        "A2.1 蓝牙开启、NFC/UWB 关闭",
        "进入连接/更多连接设置，开启蓝牙，关闭 NFC，并在机型存在 UWB/超宽带时关闭它。最新界面能够确认蓝牙开启、NFC 关闭且 UWB 关闭或明确不存在后 finish；缺失某项时在完成其余设置后 skip 并说明不适用。",
        attention="不要配对设备、发送文件、开启蓝牙可发现模式或修改已配对设备。",
        max_steps=34,
        timeout_s=420,
    ),
    _phone_configuration_task(
        "phone-config-a2-location",
        "A2.2 开启定位供高德地图使用",
        "在系统位置服务中开启定位，并检查高德地图包 com.autonavi.minimap 的位置权限。只授予测试所需的“使用应用时允许”或厂商等价选项，不要授予始终允许或精确位置以外的额外权限。确认系统定位开启且高德权限符合要求后 finish；需要设备锁确认或选项不可用时 skip。",
        attention="本任务明确授权高德地图的前台位置权限；不要给其他应用授予位置、通讯录、电话或短信权限。",
        max_steps=40,
        timeout_s=540,
    ),
    _phone_configuration_task(
        "phone-config-a2-memory-expansion",
        "A2.3 关闭内存扩展",
        "在系统设置搜索并进入内存扩展、内存融合、RAM 扩展或等价功能，将扩展容量设为关闭/0 GB。确认最终开关关闭后 finish。功能不存在、关闭后强制要求立即重启或当前机型不支持时 skip，并说明原因，不执行重启。",
        max_steps=26,
        timeout_s=360,
    ),
    _phone_configuration_task(
        "phone-config-a2-auto-brightness",
        "A2.4 关闭自动亮度",
        "进入显示与亮度设置，关闭自动调节亮度/自适应亮度。确认开关已关闭后 finish；不要改变手动亮度数值。入口不存在或被策略锁定时 skip。",
        max_steps=20,
        timeout_s=240,
    ),
    _phone_configuration_task(
        "phone-config-a2-notifications",
        "A2.5 测试应用通知权限统一配置",
        "在系统应用通知管理中，只针对本检查清单的测试应用配置通知：微信 com.tencent.mm 保持通知总开关开启；淘宝、拼多多、微博、小红书、网易云音乐、抖音、哔哩哔哩、高德地图和原神的通知总开关关闭。逐项核对并修改，全部目标应用状态符合后 finish；应用缺失、厂商无法批量定位或时间不足时 skip，并说明尚未核对的应用。",
        attention="不要关闭电话、短信、闹钟、系统更新、安全服务或其他系统级通知；不要读取通知正文。微信横幅通知在后续 B 项单独关闭。",
        max_steps=140,
        timeout_s=2400,
    ),
    _phone_configuration_task(
        "phone-config-a3-assistants-interconnection-gaze",
        "A3.1 语音助手、万物互联与注视感知",
        "在系统设置分别搜索语音助手/智慧助手唤醒、万物互联/跨设备互联，以及注视感知/注视不熄屏/智能注视；存在时全部关闭。能确认所有存在项已关闭后 finish；某项不存在或含义无法核对时完成可识别项并 skip，列出不适用或未确认项。",
        attention="不要清除语音助手数据、退出厂商账号或解除已有设备绑定，只关闭明确的自动唤醒/互联/注视开关。",
        max_steps=56,
        timeout_s=720,
    ),
    _phone_configuration_task(
        "phone-config-a3-aod-fan",
        "A3.2 关闭待机显示与游戏风扇",
        "关闭息屏显示/AOD/待机显示的总开关及其定时显示。若当前是带主动风扇的游戏手机，再在系统或游戏空间设置关闭风扇运行。普通无风扇机型只需确认 AOD 关闭即可 finish；AOD 或风扇入口不明确时 skip 并说明机型适用性。",
        attention="不要修改锁屏密码、锁屏壁纸、散热策略以外的游戏性能档位。",
        max_steps=38,
        timeout_s=480,
    ),
    _phone_configuration_task(
        "phone-config-a3-honor-app-jump",
        "A3.3 荣耀应用跳转确认弹窗",
        "先依据系统设置或关于手机页面判断是否为荣耀设备。荣耀设备中搜索应用跳转提醒、每次跳转确认或等价设置，将“每次都弹窗”关闭；最新页面确认后 finish。非荣耀设备直接 skip 并说明不适用；荣耀设备找不到入口时也 skip 并说明搜索过的入口。",
        attention="不要关闭整个应用安全检测、未知来源保护或安装验证，只处理应用间跳转的重复确认弹窗。",
        max_steps=28,
        timeout_s=360,
    ),
    *[
        _phone_configuration_task(
            f"phone-config-b-login-{slug}",
            f"B.1 {label}登录状态",
            f"启动 {label}（包 {package}），进入能够判断账号状态的首页或“我的”页面。若目标是小红书且出现读取应用列表/已安装应用请求，必须拒绝。若已有有效登录态，只以“已登录”结论记录，不读取或复述账号标识，然后 finish。若显示登录/注册、二维码、短信验证码、密码、实名或风险验证页面，不输入任何凭据，调用 skip，并说明人工方式：{manual_method}。",
            attention="检查已有登录态是只读操作；禁止代填手机号、密码、验证码、扫码授权、实名、发送消息、关注、点赞、下单或支付。",
            max_steps=24,
            timeout_s=360,
        )
        for slug, label, package, manual_method in PHONE_CONFIGURATION_LOGIN_APPS
    ],
    _phone_configuration_task(
        "phone-config-b-login-conflict",
        "B.1 微博与网易云登录要求冲突",
        "需求清单 A0 指定微博和网易云音乐不登录，但 B 又把两者列入登录应用，且没有给出优先级。不要改变这两个应用的账号状态，直接调用 skip，明确记录“配置要求冲突，需要人工确认最终基线”。",
        attention="不要为了判断冲突而打开登录页或退出已有账号。",
        max_steps=1,
        timeout_s=30,
    ),
    _phone_configuration_task(
        "phone-config-b-default-home",
        "B.2 模型机默认桌面与 Home 落点",
        "调用 home 查看当前 Home 操作到达的桌面页，并按模型机已给出的主默认桌面配置进行核对。若当前页面已有明确的主屏标记且 Home 落点正确，finish。清单没有提供具体桌面页、图标布局或壁纸基准时，不移动图标、不删除页面，调用 skip 并说明缺少模型机桌面基准。",
        attention="不要重排/删除图标、小组件或桌面页，不更换默认 Launcher，除非最新证据提供了明确可比较的模型机基准。",
        max_steps=12,
        timeout_s=180,
    ),
    _phone_configuration_task(
        "phone-config-b-account-exclusivity",
        "B.3 测试账号互斥分配核验",
        "检查当前项目是否提供了本机与测试账号的互斥分配表。手机 UI 内无法可靠获知其他设备正在使用哪些账号；若没有明确映射，不登录、不退出账号，调用 skip 并说明需要人工提供设备—账号分配。仅当所有目标应用已有登录态且页面能证明没有冲突时才 finish。",
        attention="禁止显示或复述账号标识，禁止跨设备发送消息确认，禁止退出或切换账号。",
        max_steps=8,
        timeout_s=120,
    ),
    _phone_configuration_task(
        "phone-config-b-bilibili-playback",
        "B.4 哔哩哔哩启动器、1080P30 与半屏弹幕",
        "确认测试视频启动器能够打开包 tv.danmaku.bili；已有登录态时进入一个普通点播视频，打开画质设置并选择 1080P，确认该视频为 30 帧格式或明确提供 1080P30；把弹幕显示区域设为 1/2 屏。返回播放页，最新证据能确认 1080P 和半屏弹幕设置后 finish。未登录、视频不提供目标格式、启动器缺失或设置不可见时 skip。",
        attention="不要进入直播、购买大会员、投币、点赞、收藏、关注、评论、分享或发送弹幕；只修改画质和弹幕显示区域。",
        max_steps=60,
        timeout_s=900,
    ),
    _phone_configuration_task(
        "phone-config-b-genshin-settings",
        "B.5 原神登录、资源、地点与最高画质 60 帧",
        "启动原神并依次核对已有账号登录、全量资源下载完成、角色处于测试约定地点。进入设置，把图像质量调整为全局最高画质并把帧率改为 60；返回安全主场景，最新证据确认设置生效后 finish。任一项需要登录/实名、资源仍在下载、约定人物地点未提供、设备没有 60 帧或最高画质选项时 skip，并逐项说明。",
        attention="不输入账号凭据，不移动角色、不战斗、不抽卡、不领取奖励、不打开商店或充值；若人物地点基准未在清单中给出，不自行选择地点。",
        max_steps=80,
        timeout_s=1800,
    ),
    _phone_configuration_task(
        "phone-config-b-amap-work-address",
        "B.6 高德地图“去单位”设为杭州西湖",
        "启动高德地图，处理普通首次提示并使用已授权的前台定位。进入常用地址/单位地址设置，将“去单位”或“公司”地址设为“杭州西湖”，从搜索结果中核对杭州和西湖相关地名后保存。回到能显示单位地址的页面，确认值为杭州西湖后 finish；应用缺失、必须登录、搜索结果含义不明确或保存需要额外隐私授权时 skip。",
        attention="本任务明确授权保存测试单位地址“杭州西湖”；不要发起导航、打车、签到、分享位置或读取真实家庭地址。",
        max_steps=50,
        timeout_s=720,
    ),
    _phone_configuration_task(
        "phone-config-b-wechat-permissions",
        "B.7 微信浮窗、通知与横幅通知",
        "进入微信 com.tencent.mm 的系统应用权限/特殊权限页面，开启浮窗/悬浮窗权限；保持微信通知总开关开启，但关闭横幅/悬浮/横幅通知样式。三项最终状态都能从系统设置确认后 finish。缺少对应入口、设备策略锁定或需要设备锁确认时 skip。",
        attention="本任务明确授权微信浮窗和通知权限；不要授予通讯录、电话、短信、麦克风、相机、位置或存储等其他权限，不读取通知内容。",
        max_steps=46,
        timeout_s=600,
    ),
]


ADB_AGENT_TASK_TEMPLATES: List[Dict[str, object]] = [
    {
        "id": "android-campaign-preparation",
        "kind": "campaign",
        "campaign_stage": "prepare",
        "revision": "android-campaign-json-20260723-v2",
        "label": "阶段 1：预备环境（完整流程）",
        "workflow_name": "阶段 1：Android 测试预备环境",
        "description": "宿主先完成系统设置和本地安装；下方每张卡对应一次实际 Agent 安装或首启处理。",
        "loop_enabled": False,
        "tasks": [
            {
                "id": "puzzle-home-ready",
                "name": "SGTPuzzles 首启准备",
                "prompt": "确认应用已进入谜题列表或现有谜题棋盘。关闭帮助或首次启动说明；不要开始求解。看到可重复进入的稳定应用界面后 finish。",
                "attention_prompt": "目标应用由宿主启动；不要重置谜题或操作其他应用。",
                "max_steps": 8,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "light-up-toggle",
                "name": "SGTPuzzles 正常流程支持验证",
                "prompt": "进入 Light Up 棋盘，只选一个灰色无标记空格；对同一格依次点击三次，每次用下一张图确认灰色空格、白色灯泡、小黑标记、灰色空格的完整可逆循环。恢复初始灰色后 finish。",
                "attention_prompt": "这是安装初始化后的实际测试流程验证；三次必须是同一坐标，不要操作其他格或声称解完整谜题。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "stop",
            },
            {
                "id": "dungeon-entry-ready",
                "name": "Shattered Pixel Dungeon 首启准备",
                "prompt": "关闭首次启动提示并确认已经到达标题页、地牢介绍页或已有存档入口。预备阶段不要删除存档，也不要求开始战斗；看到稳定入口后 finish。",
                "attention_prompt": "目标应用由宿主启动；不要删除存档、打开商店或主动进入战斗。",
                "max_steps": 10,
                "timeout_s": 120,
                "on_failure": "stop",
            },
            {
                "id": "dungeon-adjacent-move",
                "name": "Shattered Pixel Dungeon 正常流程支持验证",
                "prompt": "进入可操作地图，定位英雄并点击相邻一个安全可通行格；比较前后截图，只有英雄确实移动一个格且旧位置空出后才 finish。",
                "attention_prompt": "允许处理右上角闪光日志教学；不要删除存档、攻击、使用物品或打开商店。",
                "max_steps": 24,
                "timeout_s": 240,
                "on_failure": "stop",
            },
            {
                "id": "fifteen-entry-ready",
                "name": "15 Puzzle 首启准备",
                "prompt": "处理旧版应用提示或暂停遮罩，确认已经看到完整 4×4 数字棋盘和一个空格。预备阶段不要移动数字、重置或新开局；稳定棋盘清晰可见后 finish。",
                "attention_prompt": "Canvas 棋盘内部没有可靠语义格子；本任务只验证稳定入口，不进行落子。",
                "max_steps": 10,
                "timeout_s": 120,
                "on_failure": "stop",
            },
            {
                "id": "fifteen-move",
                "name": "15 Puzzle 正常流程支持验证",
                "prompt": "识别 4×4 棋盘空格和一个相邻数字，只移动一次；下一轮只有数字进入原空格、原位置变空，且 MOVES 计数递增（若可见）时才 finish。",
                "attention_prompt": "Canvas 内部使用视觉坐标；不要重置或连续试点多个数字。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "stop",
            },
            {
                "id": "minesweeper-entry-ready",
                "name": "Minesweeper Compose 首启准备",
                "prompt": "确认已经进入扫雷主界面，能够看到 New game 或完整未揭开的棋盘。关闭无害说明；预备阶段不要点击任何格子或插旗。稳定入口可重复进入后 finish。",
                "attention_prompt": "不要长按、插旗、打开外链或设置。",
                "max_steps": 10,
                "timeout_s": 120,
                "on_failure": "stop",
            },
            {
                "id": "minesweeper-reveal",
                "name": "Minesweeper Compose 正常流程支持验证",
                "prompt": "若看到 New game，进入新棋盘。只点击一个明确未揭开的格子；下一轮只有棋盘出现数字、空白或地雷状态时才 finish。",
                "attention_prompt": "语义元素存在时优先 tap_element；不要长按或插旗。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "stop",
            },
            {
                "id": "tetris-entry-ready",
                "name": "Tetris 首启准备",
                "prompt": "确认已经进入正在运行的俄罗斯方块棋盘，能够看到活动方块、下一个方块、Level/Score 和底部左右方向键。关闭无害首次启动提示；预备阶段不要点击方向键、重开、暂停或声音。稳定棋盘清晰可见后 finish。",
                "attention_prompt": "推荐使用视觉 + uiautomator2 混合引擎；本任务只验证稳定入口，不操作方块。",
                "max_steps": 8,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "tetris-board-ready",
                "name": "Tetris：活动棋盘初始化",
                "prompt": "把 Tetris 恢复到正在运行的活动棋盘。活动方块、Next、Level/Score 和底部方向键已可见时立即 finish；暂停、Game Over 或 Restart/New Game 页面只点击继续或重开一次，确认活动方块出现后 finish。",
                "attention_prompt": "只恢复棋盘，不点击方向键，不修改声音或设置。",
                "max_steps": 5,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "tetris-controlled-move",
                "name": "Tetris 正常流程支持验证",
                "prompt": "确认棋盘正在运行，优先用语义元素点击左侧“<”方向键一次。下一轮只比较同一形状活动方块的水平中心，忽略自然向下移动；只要方块向左移动约一个格且棋局仍运行就 finish。若第一次没有水平变化，最多再点击一次左键，仍无变化则 take_over。",
                "attention_prompt": "推荐混合引擎并优先 tap_element；不要点击右键、上下键、重开、暂停或声音。自然下落、方块换新和分数不算左移证据。",
                "max_steps": 5,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "super-snake-entry-ready",
                "name": "Super Snake 首启准备",
                "prompt": "关闭系统针对旧版 Android 应用的兼容性警告，确认进入横屏主玩法并看到 Points、Lives、玩家线段以及圆形或星形目标。预备阶段不要点击或滑动游戏区；稳定画面清晰可见后 finish。",
                "attention_prompt": "只关闭兼容性提示，不要打开 About；不要把背景动画当作初始化完成前的操作目标。",
                "max_steps": 8,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "super-snake-playfield-ready",
                "name": "Super Snake：活动游戏初始化",
                "prompt": "把 Super Snake 恢复到活动横屏游戏区。Points、Lives、青色玩家短线和目标已可见时立即 finish；Game Over、Start、Restart 或重试页只点击对应按钮一次，确认青色短线出现后 finish。",
                "attention_prompt": "只恢复活动游戏，不点击游戏区转向，不打开 About。",
                "max_steps": 5,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "super-snake-turn",
                "name": "Super Snake 正常流程支持验证",
                "prompt": "玩家是靠近屏幕边缘移动的青色短线。第一轮记住其轴向，只在中央空白区点击一次；紧邻下一轮若青色短线轴向已改变立即 finish，未改变才换坐标最多再点一次。",
                "attention_prompt": "不要把黄色星星、粉色旋涡、红色骷髅或位置移动当作玩家转向；看到轴向改变时禁止继续点击，Points/Lives 必须仍可见。",
                "max_steps": 4,
                "timeout_s": 90,
                "on_failure": "continue",
            },
            {
                "id": "astrosmash-entry-ready",
                "name": "AstroSmash 首启准备",
                "prompt": "若出现系统切换显示设置提示，选择取消以保留当前显示模式。在 AstroSmash 设置首页不要修改复选框或分辨率，只确认 RUN ASTROSMASH! 按钮清晰可见后 finish。预备阶段不要进入游戏。",
                "attention_prompt": "不要修改显示模式、游戏设置或高分表；只验证可重复进入的稳定设置首页。",
                "max_steps": 8,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "astrosmash-enter-play",
                "name": "AstroSmash 进入主玩法",
                "prompt": "若在 AstroSmash 设置首页，只点击一次 RUN ASTROSMASH!。下一轮只有截图显示黑色游戏区、绿色地面、白色炮台和底部左右大箭头时才 finish；进入游戏后不要操作箭头。",
                "attention_prompt": "不要修改任何设置、分辨率、复选框或高分表；本子任务只进入主玩法。",
                "max_steps": 4,
                "timeout_s": 90,
                "on_failure": "continue",
            },
            {
                "id": "astrosmash-cannon-left",
                "name": "AstroSmash 正常流程支持验证",
                "prompt": "先观察绿色地面线上方白色炮台的水平位置，只在底部左箭头中心执行一次 long_press，duration_ms=1000。紧邻下一轮只有白色炮台明确从屏幕中心向左移动且游戏仍运行时才 finish；若炮台不动则 take_over。",
                "attention_prompt": "必须使用 long_press，不能普通 tap 或重复操作。敌人、自动弹丸、分数和背景变化都不能替代白色炮台位置证据。",
                "max_steps": 3,
                "timeout_s": 90,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.darkempire78.opencalculator",
                "name": "安装 OpenCalculator（缺失时）",
                "prompt": "优先在应用商店搜索 OpenCalculator 或 OpenCalc；商店没有时，只从 github.com/Darkempire78/OpenCalc 官方 Release 下载 APK。安装后确认包 com.darkempire78.opencalculator 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "opencalculator-entry-ready",
                "name": "OpenCalculator 首启准备",
                "prompt": "处理无害的首次主题或说明弹窗，确认进入无遮挡的计算器主界面。预备阶段不要输入算式；数字键、运算符和结果区清晰可见后 finish。",
                "attention_prompt": "不要打开历史记录、设置或外部页面。",
                "max_steps": 10,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "opencalculator-arithmetic",
                "name": "OpenCalculator 正常流程支持验证",
                "prompt": "清除旧输入后依次输入 12+7=；只有表达式显示 12+7 且结果显示 19 时才 finish。",
                "attention_prompt": "不要打开历史记录、设置或外部页面。",
                "max_steps": 10,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "store-install-org.fossify.math",
                "name": "安装 Fossify Calculator（缺失时）",
                "prompt": "优先在应用商店搜索 Fossify Calculator；商店没有时，只从 github.com/FossifyOrg/Calculator 官方 Release 下载 APK。安装后确认包 org.fossify.math 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "fossify-calculator-entry-ready",
                "name": "Fossify Calculator 首启准备",
                "prompt": "处理无害的首次主题说明，确认进入无遮挡的计算器主界面。预备阶段不要输入算式；数字键、运算符和结果区清晰可见后 finish。",
                "attention_prompt": "不要打开历史记录、设置或外部页面。",
                "max_steps": 10,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "fossify-calculator-arithmetic",
                "name": "Fossify Calculator 正常流程支持验证",
                "prompt": "先对底部 C 键 long_press 800ms，确认旧表达式整体清空或显示 0 后，再依次单击 8、×、6、=；只有表达式显示 8×6、结果显示 48 时才 finish。",
                "attention_prompt": "普通点击 C 只逐字符退格，必须长按清空；不要在旧数字后追加算式，也不要打开历史记录或设置。",
                "max_steps": 8,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-me.zhanghai.android.files",
                "name": "安装质感文件（缺失时）",
                "prompt": "优先在应用商店搜索质感文件或 Material Files；商店没有时，只从 github.com/zhanghai/MaterialFiles 官方 Release 下载 APK。安装后确认包 me.zhanghai.android.files 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "material-files-entry-ready",
                "name": "质感文件首启准备",
                "prompt": "完成文件浏览所需初始化。若跳到系统“管理所有文件”页面，点击右侧开关本体开启权限，再按返回回到质感文件；不要反复点击文字行中心。内部存储目录列表可见后 finish。",
                "attention_prompt": "只允许为目标应用开启管理本机文件权限；禁止云登录、删除、改名、移动、上传或打开文件内容。",
                "max_steps": 18,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "material-files-directory-roundtrip",
                "name": "质感文件正常流程支持验证",
                "prompt": "记住当前面包屑和一个可见子目录，打开该目录并确认路径变化，再按返回；只有原面包屑和原目录项恢复时才 finish。",
                "attention_prompt": "只做只读目录导航；禁止打开、删除、改名、移动、上传或创建文件。",
                "max_steps": 10,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.microsoft.emmx",
                "name": "安装 Microsoft Edge（缺失时）",
                "prompt": "在应用商店搜索 Microsoft Edge；商店没有时，只从 microsoft.com/edge/download 官方页面下载。安装后确认包 com.microsoft.emmx 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "edge-public-browser-ready",
                "name": "Microsoft Edge 首启准备",
                "prompt": "接受必要条款，选择不登录、不导入、不设为默认浏览器、不发送可选诊断，通知权限选择禁止。停在地址栏可交互的新标签页或公开网页后 finish。",
                "attention_prompt": "禁止账号登录、同步、设为默认浏览器、导入数据、下载文件或提交表单。",
                "max_steps": 24,
                "timeout_s": 300,
                "on_failure": "continue",
            },
            {
                "id": "edge-example-navigation",
                "name": "Microsoft Edge 正常流程支持验证",
                "prompt": "读取当前地址栏：当前为 example.com 时打开 https://example.org，否则打开 https://example.com。先确认地址栏输入变化，再按 Enter；页面显示 Example Domain 且域名正确后 finish。",
                "attention_prompt": "只访问 example.com 或 example.org；禁止登录、下载和提交表单。",
                "max_steps": 12,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "store-install-cn.wps.moffice_eng",
                "name": "安装 WPS Office（缺失时）",
                "prompt": "在应用商店搜索 WPS Office；商店没有时，只从 wps.com/office/android 官方页面下载。安装后确认包 cn.wps.moffice_eng 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "wps-local-home-ready",
                "name": "WPS Office 首启准备",
                "prompt": "接受必要条款，拒绝通知、个性化广告和可选诊断，关闭引导并跳过登录。最终停在能看到“空白文档”入口的 WPS 首页后 finish。",
                "attention_prompt": "禁止登录、云同步、上传、会员购买和修改现有文件；预备阶段不要创建文档。",
                "max_steps": 24,
                "timeout_s": 300,
                "on_failure": "continue",
            },
            {
                "id": "wps-local-document-discard",
                "name": "WPS Office 正常流程支持验证",
                "prompt": "新建空白文字文档并输入 MP smoke 0722；确认文字可见后返回，选择不保存/放弃更改。回到 WPS 首页且临时文档未保存后 finish。",
                "attention_prompt": "禁止登录、云同步、上传、分享、购买会员、打开付费模板或修改现有文件。",
                "max_steps": 24,
                "timeout_s": 420,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.baidu.searchbox",
                "name": "应用商店安装百度（缺失时）",
                "prompt": "在应用商店搜索“百度”，确认官方百度应用后安装。安装完成并能打开包 com.baidu.searchbox 后 finish。",
                "attention_prompt": "只安装目标包；不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "baidu-public-home-ready",
                "name": "百度公开首页准备",
                "prompt": "处理首次协议、通知、定位和功能介绍；通知与定位选择禁止，不登录。停在免登录公开推荐首页后 finish。",
                "attention_prompt": "禁止登录、搜索、打开详情、消息、购买或授予敏感权限。",
                "max_steps": 18,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "baidu-public-feed",
                "name": "百度正常流程支持验证",
                "prompt": "在免登录公开推荐首页完成两次向上滚动，每次从新标题、卡片或封面确认内容变化；最后仍停在公开推荐流时 finish。",
                "attention_prompt": "禁止登录、搜索、打开详情、点赞、评论、关注、收藏、分享、消息或购买。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.zhihu.android",
                "name": "应用商店安装知乎（缺失时）",
                "prompt": "在应用商店搜索“知乎”，确认知乎官方应用后安装。安装完成并能打开包 com.zhihu.android 后 finish。",
                "attention_prompt": "只安装目标包；不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "zhihu-public-home-ready",
                "name": "知乎公开首页准备",
                "prompt": "处理首次协议、通知、兴趣引导和功能介绍；通知选择禁止，兴趣选择与登录均跳过。停在免登录公开推荐首页后 finish。",
                "attention_prompt": "禁止登录、提问、回答、点赞、评论、关注、收藏、消息或授予敏感权限。",
                "max_steps": 20,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "zhihu-public-feed",
                "name": "知乎正常流程支持验证",
                "prompt": "在免登录公开推荐首页完成两次向上滚动，每次从新问题标题或回答卡片确认内容变化；最后仍停在公开推荐流时 finish。",
                "attention_prompt": "禁止登录、提问、回答、打开详情、点赞、评论、关注、收藏或消息。",
                "max_steps": 10,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.tencent.news",
                "name": "应用商店安装腾讯新闻（缺失时）",
                "prompt": "在应用商店搜索“腾讯新闻”，确认腾讯官方新闻应用后安装。安装完成并能打开包 com.tencent.news 后 finish。",
                "attention_prompt": "只安装目标包；不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "tencent-news-public-home-ready",
                "name": "腾讯新闻公开首页准备",
                "prompt": "处理首次协议、通知、定位和功能介绍；通知与定位选择禁止，不登录。停在免登录公开新闻首页后 finish。",
                "attention_prompt": "禁止登录、打开详情、评论、收藏、分享、消息或授予敏感权限。",
                "max_steps": 18,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "tencent-news-public-feed",
                "name": "腾讯新闻正常流程支持验证",
                "prompt": "在免登录公开新闻首页完成两次向上滚动，每次从不同新闻标题或封面确认内容变化；最后仍停在公开新闻流时 finish。",
                "attention_prompt": "禁止登录、打开详情、评论、收藏、分享或消息。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.sohu.newsclient",
                "name": "应用商店安装搜狐新闻（缺失时）",
                "prompt": "在应用商店搜索“搜狐新闻”，确认搜狐官方新闻应用后安装。安装完成并能打开包 com.sohu.newsclient 后 finish。",
                "attention_prompt": "只安装目标包；不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "sohu-news-public-home-ready",
                "name": "搜狐新闻公开首页准备",
                "prompt": "处理首次协议、通知、定位、推送设置和功能介绍；通知与定位选择禁止，关闭推送引导，不登录。停在免登录公开新闻首页后 finish。",
                "attention_prompt": "禁止登录、打开详情、评论、收藏、分享、消息或授予敏感权限。",
                "max_steps": 20,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "sohu-news-public-feed",
                "name": "搜狐新闻正常流程支持验证",
                "prompt": "在免登录公开新闻首页完成两次向上滚动，每次从不同新闻标题或封面确认内容变化；最后仍停在公开新闻流时 finish。",
                "attention_prompt": "禁止登录、打开详情、评论、收藏、分享或消息。",
                "max_steps": 9,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.quark.browser",
                "name": "安装夸克（缺失时）",
                "prompt": "在应用商店搜索“夸克”并安装；商店没有时，只从 quark.cn 官方页面下载。安装后确认包 com.quark.browser 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "quark-public-browser-ready",
                "name": "夸克公开浏览准备",
                "prompt": "处理首次条款，选择不登录、不设为默认、不导入，通知与定位选择禁止。停在地址栏可交互的新标签页或公开网页后 finish。",
                "attention_prompt": "禁止账号登录、同步、设为默认、导入、下载、提交表单或点击广告。",
                "max_steps": 22,
                "timeout_s": 300,
                "on_failure": "continue",
            },
            {
                "id": "quark-baidu-navigation",
                "name": "夸克正常流程支持验证",
                "prompt": "在已有免登录公开网页或公开结果页完成两次向上滚动，每次从新标题、卡片或正文段落确认内容变化；最后仍在夸克公开内容页时 finish。",
                "attention_prompt": "不要点击结果、链接、广告或搜索框；禁止登录、下载或提交搜索。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.UCMobile",
                "name": "安装 UC浏览器（缺失时）",
                "prompt": "在应用商店搜索“UC浏览器”并安装；商店没有时，只从 uc.cn 官方页面下载。安装后确认包 com.UCMobile 可以打开再 finish。",
                "attention_prompt": "只安装目标包；不得从第三方下载站获取，也不得安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "uc-public-browser-ready",
                "name": "UC浏览器公开浏览准备",
                "prompt": "处理首次条款，选择不登录、不设为默认、不导入，通知与定位选择禁止。停在地址栏可交互的新标签页或公开网页后 finish。",
                "attention_prompt": "禁止账号登录、同步、设为默认、导入、下载、提交表单或点击广告。",
                "max_steps": 22,
                "timeout_s": 300,
                "on_failure": "continue",
            },
            {
                "id": "uc-baidu-navigation",
                "name": "UC浏览器正常流程支持验证",
                "prompt": "在 UC 已有的免登录公开网页完成两次向上滚动，每次从新标题、卡片或正文段落确认内容变化；最后仍在 UC 公开内容页时 finish。",
                "attention_prompt": "不要点击链接、结果、广告、搜索框或千问入口；禁止登录、下载或提交搜索。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.baidu.BaiduMap",
                "name": "应用商店安装百度地图（缺失时）",
                "prompt": "在当前设备应用商店搜索“百度地图”，确认百度官方地图应用后安装。安装完成并能打开包 com.baidu.BaiduMap 后 finish。",
                "attention_prompt": "只安装目标包；不得安装推荐应用，不得登录。遇到 Google Play、Play Games、GMS 或其他谷歌组件页面直接 take_over。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "baidu-map-public-entry-ready",
                "name": "百度地图公开地图准备",
                "prompt": "处理首次协议、通知、定位、更新和无害引导；通知与定位选择禁止，不登录。停在显示道路、地名且地图可拖动的公开地图主界面后 finish。",
                "attention_prompt": "跳过 Google Play、Play Games、GMS 或任何谷歌组件页面；禁止搜索地点、发起导航、打车、登录或授予敏感权限。",
                "max_steps": 24,
                "timeout_s": 360,
                "on_failure": "continue",
            },
            {
                "id": "baidu-map-pan",
                "name": "百度地图正常流程支持验证",
                "prompt": "记住公开地图中心附近至少两个道路或地名，只在地图空白区域执行一次中等距离水平拖动。下一轮只有道路或地名整体明显换位、地图仍可操作时才 finish。",
                "attention_prompt": "只执行一次地图平移；不要搜索地点、发起导航、点击地点卡片、打车、签到、广告或个人中心。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.tencent.map",
                "name": "应用商店安装腾讯地图（缺失时）",
                "prompt": "在当前设备应用商店搜索“腾讯地图”，确认腾讯官方地图应用后安装。安装完成并能打开包 com.tencent.map 后 finish。",
                "attention_prompt": "只安装目标包；不得安装推荐应用，不得登录。遇到 Google Play、Play Games、GMS 或其他谷歌组件页面直接 take_over。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "tencent-map-public-entry-ready",
                "name": "腾讯地图公开地图准备",
                "prompt": "处理首次协议、通知、定位、更新和无害引导；通知与定位选择禁止，不登录。停在显示道路、地名且地图可拖动的公开地图主界面后 finish。",
                "attention_prompt": "跳过 Google Play、Play Games、GMS 或任何谷歌组件页面；禁止搜索地点、发起导航、打车、登录或授予敏感权限。",
                "max_steps": 24,
                "timeout_s": 360,
                "on_failure": "continue",
            },
            {
                "id": "tencent-map-pan",
                "name": "腾讯地图正常流程支持验证",
                "prompt": "记住公开地图中心附近至少两个道路或地名，只在地图空白区域执行一次中等距离水平拖动。下一轮只有道路或地名整体明显换位、地图仍可操作时才 finish。",
                "attention_prompt": "只执行一次地图平移；不要搜索地点、发起导航、点击地点卡片、打车、签到、广告或个人中心。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-tv.danmaku.bili",
                "name": "应用商店安装哔哩哔哩（缺失时）",
                "prompt": "在当前设备的应用商店中搜索“哔哩哔哩”，确认开发者和应用名称后安装。系统安装器完成后，验证商店页面显示已安装或可以打开，再 finish。",
                "attention_prompt": "只安装目标应用 tv.danmaku.bili；允许确认安装按钮，不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "bilibili-public-home",
                "name": "哔哩哔哩公开首页准备",
                "prompt": "处理首次启动协议、通知、更新、青少年模式提示和功能介绍。不登录；最终停在免登录可浏览的公开推荐首页并 finish。",
                "attention_prompt": "用户已授权接受当前应用协议和配置声明的通知权限；仍禁止登录、验证码、实名和账号操作。",
                "max_steps": 16,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "bilibili-scroll",
                "name": "哔哩哔哩正常流程支持验证",
                "prompt": "在免登录公开推荐首页完成两次向上滚动，每次确认出现新卡片，最后停在公开推荐流并 finish。",
                "attention_prompt": "不要打开视频、点赞、投币、收藏、关注、评论、登录或进入消息/个人中心。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.xingin.xhs",
                "name": "应用商店安装小红书（缺失时）",
                "prompt": "在当前设备的应用商店中搜索“小红书”，确认应用名称后安装。安装完成并能从商店打开后 finish。",
                "attention_prompt": "只安装目标应用 com.xingin.xhs；允许确认安装按钮，不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "xhs-public-feed",
                "name": "小红书公开发现页准备",
                "prompt": "处理首次启动协议、通知、更新和功能介绍。不登录或选择兴趣画像；最终停在免登录公开发现页或公开卡片流并 finish。",
                "attention_prompt": "用户已授权接受当前应用协议和配置声明的通知权限；不要进入消息或个人中心。",
                "max_steps": 18,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "xhs-scroll",
                "name": "小红书正常流程支持验证",
                "prompt": "在免登录公开发现流完成两次向上滚动，每次确认出现不同卡片，最后停在公开卡片流并 finish。",
                "attention_prompt": "不要打开笔记、点赞、收藏、关注、评论、搜索、登录或进入消息/个人中心。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.jingdong.app.mall",
                "name": "应用商店安装京东（缺失时）",
                "prompt": "在当前设备的应用商店中搜索“京东”，确认官方购物应用名称后安装。安装完成并能从商店打开后 finish。",
                "attention_prompt": "只安装目标应用 com.jingdong.app.mall；允许确认安装按钮，不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "jd-public-home",
                "name": "京东公开首页准备",
                "prompt": "处理首次启动协议、通知、更新、定位和营销弹窗。定位不是本场景必需权限，不允许定位；不要登录。最终停在免登录公开首页并 finish。",
                "attention_prompt": "用户已授权接受当前应用协议和配置声明的通知权限；禁止领券、购物车、下单和支付。",
                "max_steps": 18,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "jd-scroll",
                "name": "京东正常流程支持验证",
                "prompt": "在免登录公开首页完成两次向上滚动，每次确认商品或频道卡片变化，最后停在公开首页并 finish。",
                "attention_prompt": "不要打开商品、搜索、登录、领券、购物车、下单、支付或进入消息/个人中心。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "store-install-com.miHoYo.hkrpg",
                "name": "商店或官网安装崩坏：星穹铁道（缺失时）",
                "prompt": "优先在当前设备应用商店搜索“崩坏：星穹铁道”，确认米哈游官方应用后安装；若商店没有，使用浏览器进入 sr.mihoyo.com 官方下载页，只下载官方 Android 安装包。安装后确认应用可以打开并 finish。",
                "attention_prompt": "只安装包 com.miHoYo.hkrpg；不得从第三方下载站安装，不得登录、付费或安装推荐应用。",
                "max_steps": 40,
                "timeout_s": 600,
                "on_failure": "continue",
            },
            {
                "id": "star-rail-entry-ready",
                "name": "崩坏：星穹铁道稳定入口准备",
                "prompt": "处理版本更新、资源校验、公告和非敏感权限弹窗。已有登录态时进入稳定主界面并 finish；没有登录态时确认官方登录界面正常显示后 take_over，说明安装和启动已完成、需要用户登录。",
                "attention_prompt": "允许等待官方资源更新；禁止输入账号、手机号、验证码或密码，禁止实名、充值、购买、删除资源或切换账号。",
                "max_steps": 40,
                "timeout_s": 1200,
                "on_failure": "continue",
            },
            {
                "id": "star-rail-camera-drag",
                "name": "崩坏：星穹铁道正常流程支持验证",
                "prompt": "确认已进入可控制角色的主场景，只在右侧无按钮区域完成一次短距离水平拖动；下一轮只有背景、地面或角色朝向发生可见变化且仍在安全主场景时才 finish。",
                "attention_prompt": "登录、验证码、实名、充值或账号页面立即 take_over；不要移动角色、进入战斗、打开抽卡/商店、领取奖励或发送消息。",
                "max_steps": 24,
                "timeout_s": 600,
                "on_failure": "continue",
            },
        ],
    },
    {
        "id": "android-campaign-two-hour-test",
        "kind": "campaign",
        "campaign_stage": "test",
        "revision": "android-campaign-json-20260723-v2",
        "label": "阶段 2：实际测试（单轮 2 小时）",
        "workflow_name": "阶段 2：Android 单轮两小时实际测试",
        "description": "固定运行一个 7200 秒轮次；轮次内持续调度可重试 workflow，到达硬截止后严格验收并收尾。",
        "loop_enabled": True,
        "tasks": [
            {
                "id": "light-up-toggle",
                "name": "SGTPuzzles：Light Up 可逆落子",
                "prompt": "启动 SGTPuzzles 并进入或保持在 Light Up 棋盘，只选一个灰色无标记空格。同一格按灰色空格、白色灯泡、小黑色标记、灰色空格循环；对同一坐标依次点击三次，每次用下一张图确认状态。第三次后恢复灰色空格时立即 finish，不要再点击。",
                "attention_prompt": "三次点击必须是同一坐标，这是允许连续三次相同 tap 的明确例外；不要声称已解完整谜题，红色冲突灯也不能视为成功。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "dungeon-map-ready",
                "name": "Shattered Pixel Dungeon：地牢地图初始化",
                "prompt": "把应用恢复到可操作地牢地图。若地图和英雄已经可见且无遮挡，立即 finish；若在选择英雄页，选择已解锁的默认战士并点击开始游戏/进入地牢。允许处理普通新手说明和右上角闪光日志教学，只有英雄与相邻地板格清晰可见时 finish。",
                "attention_prompt": "本步骤只建立入口，不移动英雄、攻击或使用物品；不要把英雄已选中误认为已进入地图。",
                "max_steps": 18,
                "timeout_s": 240,
                "on_failure": "stop",
            },
            {
                "id": "dungeon-adjacent-move",
                "name": "Shattered Pixel Dungeon：相邻移动",
                "prompt": "当前应已在无遮挡地牢地图。定位英雄中心，只点击相差正好一个地图格、无敌人的安全地板或木板格一次；比较紧邻动作前后两张图，只有英雄中心确实移动约一个格且旧位置空出后才 finish。",
                "attention_prompt": "不要再做标题页或选英雄导航；不要点击日志、菜单、敌人或物品。必须用前后视觉截图确认英雄坐标变化。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "fifteen-move",
                "name": "15 Puzzle：相邻数字移动",
                "prompt": "启动 15 Puzzle 并确认处于 4×4 棋盘。若有暂停层，先解除并在下一轮重新观察。识别空格和一个相邻数字，只移动一次；下一轮只有数字进入原空格、原位置变空，且 MOVES 计数递增（若可见）时才 finish。",
                "attention_prompt": "Canvas 内部使用视觉坐标；不要把整块 game_view 当作数字格，不要重置或连续试点多个数字。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "minesweeper-reveal",
                "name": "Minesweeper Compose：揭开一格",
                "prompt": "启动扫雷；若看到 New game，进入新棋盘。只点击一个明确未揭开的格子；下一轮只有棋盘由全覆盖变为出现数字、空白或地雷状态时才 finish。",
                "attention_prompt": "语义元素存在时优先 tap_element；不要长按、插旗、打开外链或设置。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "tetris-board-ready",
                "name": "Tetris：活动棋盘初始化",
                "prompt": "把 Tetris 恢复到正在运行的活动棋盘。活动方块、Next、Level/Score 和底部方向键已可见时立即 finish；暂停、Game Over 或 Restart/New Game 页面只点击继续或重开一次，确认活动方块出现后 finish。",
                "attention_prompt": "只恢复棋盘，不点击方向键，不修改声音或设置。",
                "max_steps": 5,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "tetris-controlled-move",
                "name": "Tetris：方块横向移动",
                "prompt": "启动 Tetris 并确认棋盘正在运行，优先用语义元素点击左侧“<”方向键一次。下一轮只比较同一形状活动方块的水平中心，忽略自然向下移动；方块向左移动约一个格且棋局仍运行后 finish。第一次无水平变化时最多再点一次左键。",
                "attention_prompt": "推荐混合引擎并优先 tap_element；不要点击右键、上下键、重开、暂停或声音。自然下落、方块换新和分数不算左移证据。",
                "max_steps": 5,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "super-snake-playfield-ready",
                "name": "Super Snake：活动游戏初始化",
                "prompt": "把 Super Snake 恢复到活动横屏游戏区。Points、Lives、青色玩家短线和目标已可见时立即 finish；Game Over、Start、Restart 或重试页只点击对应按钮一次，确认青色短线出现后 finish。",
                "attention_prompt": "只恢复活动游戏，不点击游戏区转向，不打开 About。",
                "max_steps": 5,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "super-snake-turn",
                "name": "Super Snake：转向验证",
                "prompt": "玩家是靠近屏幕边缘移动的青色短线，不是黄色星星、粉色旋涡或红色骷髅。第一轮记住青色短线轴向，只在中央空白区点击一次；紧邻下一轮若轴向已改变立即 finish，未改变才换坐标最多再点一次。",
                "attention_prompt": "每次点击后的下一轮先比较再决定，看到轴向改变时禁止继续点击。只认青色短线；Points/Lives 必须仍可见，不要打开 About。",
                "max_steps": 4,
                "timeout_s": 90,
                "on_failure": "continue",
            },
            {
                "id": "astrosmash-enter-play",
                "name": "AstroSmash：进入主玩法",
                "prompt": "启动 AstroSmash；若在设置首页，只点击一次 RUN ASTROSMASH!。下一轮只有截图显示黑色游戏区、绿色地面、白色炮台和底部左右大箭头时才 finish。",
                "attention_prompt": "不要修改设置、分辨率、复选框或高分表；本子任务只进入主玩法。",
                "max_steps": 4,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "astrosmash-cannon-left",
                "name": "AstroSmash：炮台左移",
                "prompt": "观察绿色地面线上方白色炮台的位置，只在底部左箭头中心执行一次 long_press，duration_ms=1000。紧邻下一轮只有白色炮台明确从屏幕中心向左移动且游戏仍运行时才 finish。",
                "attention_prompt": "必须使用 long_press，不能普通 tap 或重复操作。敌人、自动弹丸、分数和背景变化都不能替代白色炮台位置证据。",
                "max_steps": 3,
                "timeout_s": 90,
                "on_failure": "continue",
            },
            {
                "id": "opencalculator-arithmetic",
                "name": "OpenCalculator：算术验证",
                "prompt": "启动 OpenCalculator，使用 AC 清除旧输入，再依次输入 12+7=；只有表达式显示 12+7 且结果显示 19 时才 finish。",
                "attention_prompt": "不要打开历史记录、设置或外部页面；必须读取最新结果 19。",
                "max_steps": 10,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "fossify-calculator-arithmetic",
                "name": "Fossify Calculator：算术验证",
                "prompt": "启动 Fossify Calculator，先对底部 C 键 long_press 800ms，确认旧表达式整体清空或显示 0 后，再依次单击 8、×、6、=；只有表达式显示 8×6 且结果显示 48 时才 finish。",
                "attention_prompt": "普通点击 C 只逐字符退格，必须长按清空；不要在旧数字后追加算式，也不要打开历史记录或设置。",
                "max_steps": 8,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "material-files-directory-roundtrip",
                "name": "质感文件：目录往返",
                "prompt": "启动质感文件，记住当前面包屑和一个可见子目录。打开该目录并确认路径变化，再按返回；只有原面包屑和原子目录项恢复时才 finish。",
                "attention_prompt": "只做只读目录导航；禁止打开、删除、改名、移动、上传或创建文件。",
                "max_steps": 10,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "edge-example-navigation",
                "name": "Microsoft Edge：公开网页导航",
                "prompt": "启动 Edge 并读取当前地址栏：当前为 example.com 时打开 https://example.org，否则打开 https://example.com。先确认地址栏输入变化，再按 Enter；页面显示 Example Domain 且域名正确后 finish。",
                "attention_prompt": "只访问 example.com 或 example.org；禁止登录、同步、设为默认、下载或提交表单。",
                "max_steps": 12,
                "timeout_s": 240,
                "on_failure": "continue",
            },
            {
                "id": "wps-local-document-discard",
                "name": "WPS Office：临时文档编辑并放弃",
                "prompt": "启动 WPS，从首页新建空白文字文档并输入 MP smoke 0722；确认文字可见后返回，选择不保存/放弃更改。回到 WPS 首页且临时文档未保存后 finish。",
                "attention_prompt": "禁止登录、云同步、上传、分享、购买会员、打开付费模板或修改现有文件。",
                "max_steps": 24,
                "timeout_s": 420,
                "on_failure": "continue",
            },
            {
                "id": "baidu-public-feed",
                "name": "百度：公开推荐流滚动",
                "prompt": "启动百度并确认处于免登录公开推荐首页，完成两次向上滚动；每次都从新标题、卡片或封面确认内容变化，最后仍停在公开推荐流时 finish。",
                "attention_prompt": "禁止登录、搜索、打开详情、点赞、评论、关注、收藏、分享、消息或购买。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "zhihu-public-feed",
                "name": "知乎：公开推荐流滚动",
                "prompt": "启动知乎并确认处于免登录公开推荐首页，完成两次向上滚动；每次都从新问题标题或回答卡片确认内容变化，最后仍停在公开推荐流时 finish。",
                "attention_prompt": "禁止登录、提问、回答、打开详情、点赞、评论、关注、收藏或消息。",
                "max_steps": 10,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "tencent-news-public-feed",
                "name": "腾讯新闻：公开新闻流滚动",
                "prompt": "启动腾讯新闻并确认处于免登录公开新闻首页，完成两次向上滚动；每次都从不同新闻标题或封面确认内容变化，最后仍停在公开新闻流时 finish。",
                "attention_prompt": "禁止登录、打开详情、评论、收藏、分享或消息。",
                "max_steps": 8,
                "timeout_s": 150,
                "on_failure": "continue",
            },
            {
                "id": "sohu-news-public-feed",
                "name": "搜狐新闻：公开新闻流滚动",
                "prompt": "启动搜狐新闻并确认处于免登录公开新闻首页，完成两次向上滚动；每次都从不同新闻标题或封面确认内容变化，最后仍停在公开新闻流时 finish。",
                "attention_prompt": "禁止登录、打开详情、评论、收藏、分享或消息。",
                "max_steps": 9,
                "timeout_s": 180,
                "on_failure": "continue",
            },
            {
                "id": "quark-public-page-ready",
                "name": "夸克：公开页面初始化",
                "prompt": "把夸克恢复到已有的免登录公开网页或公开搜索结果页。当前是搜索编辑器或键盘界面时，先 back 收键盘；仍在编辑器时再 back 一次。看到多个公开标题、卡片或正文后 finish。",
                "attention_prompt": "最多返回两次；不要输入或提交搜索，不要打开结果、广告、登录、同步、下载或默认浏览器设置。",
                "max_steps": 4,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "quark-baidu-navigation",
                "name": "夸克：公开页面滚动",
                "prompt": "当前应已在夸克免登录公开网页或结果卡片页。记住至少两个当前标题或卡片，连续完成两次向上滚动；每次都从新标题、卡片或正文段落确认内容变化。最后仍在同类公开内容页时 finish。",
                "attention_prompt": "不要点击结果、链接、广告或搜索框；禁止登录、同步、设为默认、导入、下载或提交搜索。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "uc-public-page-ready",
                "name": "UC浏览器：公开页面初始化",
                "prompt": "把 UC 恢复到已有免登录公开网页。当前是千问 AI、模型选择、搜索编辑器或键盘页面时，优先按左上角返回或 Android back，每轮只返回一次，最多三次。看到公开网页标题、正文或卡片后 finish。",
                "attention_prompt": "不要输入或提交搜索，不要点击模型、结果、广告、登录、同步、下载或默认浏览器设置。",
                "max_steps": 5,
                "timeout_s": 120,
                "on_failure": "stop",
            },
            {
                "id": "uc-baidu-navigation",
                "name": "UC浏览器：公开页面滚动",
                "prompt": "当前应已在 UC 免登录公开网页。记住至少两个标题、卡片或正文锚点，连续完成两次向上滚动；每次都从新内容确认变化，最后仍在 UC 公开内容页时 finish。",
                "attention_prompt": "不要点击链接、结果、广告、搜索框或千问入口；禁止登录、同步、设为默认、导入、下载或提交搜索。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "baidu-map-pan",
                "name": "百度地图：公开地图平移",
                "prompt": "启动百度地图并确认处于显示道路和地名的公开地图主界面，记住中心附近至少两个道路或地名标签，只在地图空白区域执行一次中等距离水平拖动。下一轮只有道路或地名整体明显换位、地图仍可操作时才 finish。",
                "attention_prompt": "跳过任何 Google Play、Play Games、GMS 页面；不要搜索地点、发起导航、点击地点卡片、打车、签到、广告或个人中心。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "tencent-map-pan",
                "name": "腾讯地图：公开地图平移",
                "prompt": "启动腾讯地图并确认处于显示道路和地名的公开地图主界面，记住中心附近至少两个道路或地名标签，只在地图空白区域执行一次中等距离水平拖动。下一轮只有道路或地名整体明显换位、地图仍可操作时才 finish。",
                "attention_prompt": "跳过任何 Google Play、Play Games、GMS 页面；不要搜索地点、发起导航、点击地点卡片、打车、签到、广告或个人中心。",
                "max_steps": 6,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "bilibili-public-home-ready",
                "name": "哔哩哔哩：免登录首页初始化",
                "prompt": "若当前是手机号登录/注册页：键盘可见时先 back 收起键盘；下一轮仍在登录页时点击左上角返回箭头，或再 back 一次。看到公开首页视频卡片后 finish。两次返回后仍只有登录路径时才 take_over；不要输入手机号或验证码。若本来就在公开推荐首页，直接 finish。",
                "attention_prompt": "最多执行两次返回操作；不要点击一键登录、其他登录方式、国家网络身份认证或推广入口。",
                "max_steps": 5,
                "timeout_s": 90,
                "on_failure": "stop",
            },
            {
                "id": "bilibili-scroll",
                "name": "哔哩哔哩：公开推荐流滚动",
                "prompt": "启动哔哩哔哩并确认处于免登录公开推荐首页，完成两次向上滚动；每次都依据新的卡片标题或封面确认内容变化，最后停在公开推荐流并 finish。",
                "attention_prompt": "不要打开视频、点赞、投币、收藏、关注、评论、登录或进入消息/个人中心。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "xhs-scroll",
                "name": "小红书：公开发现流滚动",
                "prompt": "启动小红书并确认处于免登录公开发现页或卡片流，完成两次向上滚动；每次都确认出现不同卡片，最后停在公开卡片流并 finish。",
                "attention_prompt": "不要打开笔记、点赞、收藏、关注、评论、搜索、登录或进入消息/个人中心。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "jd-public-home-ready",
                "name": "京东：免登录首页初始化",
                "prompt": "启动后是无文字纯黑加载画面时先连续两次 wait 3 秒；仍黑屏时只调用一次 launch_app 重启 com.jingdong.app.mall，再 wait 5 秒最多两轮。若当前是手机号/验证码登录页或登录弹层，只按一次 Android 返回键；公开首页商品或频道卡片可见后 finish。仍只有登录路径时 take_over；不要输入凭据。",
                "attention_prompt": "最多返回一次；不要点击一键登录、领券、商品、购物车或个人中心。",
                "max_steps": 10,
                "timeout_s": 180,
                "on_failure": "stop",
            },
            {
                "id": "jd-scroll",
                "name": "京东：公开首页滚动",
                "prompt": "启动京东并确认处于免登录公开首页，完成两次向上滚动；每次都确认商品或频道卡片发生变化，最后停在公开首页并 finish。",
                "attention_prompt": "不要打开商品、搜索、登录、领券、加入购物车、下单、支付或进入消息/个人中心。",
                "max_steps": 7,
                "timeout_s": 120,
                "on_failure": "continue",
            },
            {
                "id": "star-rail-scene-ready",
                "name": "崩坏：星穹铁道：主场景初始化",
                "prompt": "处理公告、资源校验和普通更新提示，等待加载并恢复到可控制角色的安全主场景。看到角色、摇杆和常规 HUD 后 finish；停在登录、验证码、实名或账号页面时 take_over，不要尝试凭据。",
                "attention_prompt": "只建立主场景入口，不拖动视角、移动角色、进入战斗、领取奖励或打开抽卡/商店。",
                "max_steps": 18,
                "timeout_s": 480,
                "on_failure": "stop",
            },
            {
                "id": "star-rail-camera-drag",
                "name": "崩坏：星穹铁道：安全视角拖动",
                "prompt": "当前应已在安全主场景。记住背景、地面边线与角色朝向，只在屏幕右侧无按钮区域完成一次短距离水平拖动；下一轮比较同一锚点，只有视角确实变化且仍在安全主场景时才 finish。",
                "attention_prompt": "登录、验证码、实名、充值或账号页面立即 take_over。不要移动角色、进入战斗、打开抽卡/商店、领取奖励、发送消息或修改账号。",
                "max_steps": 6,
                "timeout_s": 150,
                "on_failure": "continue",
            },
        ],
    },
    {
        "id": "phone-configuration-endurance-5",
        "kind": "phone_configuration",
        "revision": "phone-config-20260723-v10",
        "label": "手机配置检查：续航测试 5.0",
        "workflow_name": "续航测试 5.0（V4 AI Agent）手机配置检查",
        "description": "按 A0–A3 和 B 逐项预配置与核验；无法安全完成的项目自动跳过，结束后统一汇总原因。推荐使用视觉 + uiautomator2。",
        "loop_enabled": False,
        "tasks": PHONE_CONFIGURATION_TASKS,
    },
    {
        "id": "return-home",
        "label": "回到桌面",
        "prompt": "回到 Android 系统桌面。确认最新截图已经显示桌面图标或桌面小组件后调用 finish。",
        "attention_prompt": "如果屏幕熄灭可以先 wake；遇到锁屏密码或生物识别界面时 take_over，不要尝试猜测凭据。",
        "max_steps": 6,
        "timeout_s": 60,
        "on_failure": "stop",
    },
    {
        "id": "open-settings",
        "label": "打开系统设置",
        "prompt": "打开 Android 系统设置。优先调用 launch_app，package=com.android.settings。确认进入系统设置主页面后调用 finish。",
        "attention_prompt": "不要修改任何设置；本任务只验证系统设置能够打开。",
        "max_steps": 6,
        "timeout_s": 60,
        "on_failure": "stop",
    },
    {
        "id": "brightness-initialization",
        "label": "亮度初始化示例",
        "prompt": "在系统设置中关闭自动亮度，并把屏幕亮度调整到大约 50%。完成后停留在能确认亮度设置结果的页面并调用 finish。",
        "attention_prompt": "不同厂商入口名称可能是“显示与亮度”“亮度”“自动调节亮度”。不要修改色彩、刷新率、护眼模式或其他显示设置。",
        "max_steps": 20,
        "timeout_s": 180,
        "on_failure": "stop",
    },
    {
        "id": "current-app-smoke-browse",
        "label": "当前应用浏览烟测",
        "prompt": "在当前应用内完成一次只读浏览烟测：确认当前页面，向上滚动一次，观察新内容，再返回到滚动前的上一级或稳定页面。确认应用仍可正常交互后调用 finish。",
        "attention_prompt": "不要点赞、评论、关注、发送消息、购买、授权权限或提交任何表单。遇到登录、验证码或敏感权限立即 take_over。",
        "max_steps": 12,
        "timeout_s": 120,
        "on_failure": "continue",
    },
]


def task_templates_snapshot() -> List[Dict[str, object]]:
    """Return task templates as independent dictionaries for API responses."""

    return deepcopy(ADB_AGENT_TASK_TEMPLATES)
