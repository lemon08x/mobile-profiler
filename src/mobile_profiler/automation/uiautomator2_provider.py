"""Optional uiautomator2 semantic observation and action adapter.

The module itself only uses the standard library and imports ``uiautomator2``
when a provider instance first connects.  This keeps the core profiler usable
without the optional dependency while giving the ADB agent a real semantic UI
engine when the extra is installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import threading
import time
import xml.etree.ElementTree as ElementTree
from importlib import metadata
from typing import Any, Callable, Mapping, Optional

from .contracts import (
    Artifact,
    Bounds,
    DeviceContext,
    Observation,
    ObservationRequest,
    UiElement,
    UiHierarchy,
)


_BOUNDS_RE = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
_SURFACE_CLASSES = {
    "android.opengl.GLSurfaceView",
    "android.view.SurfaceView",
    "android.view.TextureView",
    "android.webkit.WebView",
}
_CONTAINER_CLASS_NAMES = {
    "FrameLayout",
    "GridLayout",
    "LinearLayout",
    "ListView",
    "RecyclerView",
    "RelativeLayout",
    "ScrollView",
    "ViewGroup",
}


def uiautomator2_dependency_status() -> tuple[bool, str]:
    """Return availability plus a concise UI-facing version/error string."""

    try:
        if importlib.util.find_spec("uiautomator2") is None:
            return False, "未安装；运行 pip install mobile-profiler[uiautomator2]"
        try:
            version = metadata.version("uiautomator2")
        except metadata.PackageNotFoundError:
            version = "已安装"
        return True, f"uiautomator2 {version}"
    except (ImportError, ValueError) as exc:
        return False, f"依赖检查失败：{exc}"


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _short(value: object, limit: int = 300) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _exception_detail(exc: BaseException, limit: int = 500) -> str:
    message = _short(exc, limit)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _parse_bounds(value: object, width: int, height: int) -> Optional[Bounds]:
    match = _BOUNDS_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    left = max(0, min(width, left))
    top = max(0, min(height, top))
    right = max(0, min(width, right))
    bottom = max(0, min(height, bottom))
    if right <= left or bottom <= top:
        return None
    return Bounds(left, top, right, bottom)


def _node_priority(attributes: Mapping[str, str], class_name: str, leaf: bool) -> int:
    score = 0
    if _bool(attributes.get("clickable")):
        score += 120
    if _bool(attributes.get("checkable")):
        score += 100
    if _bool(attributes.get("scrollable")):
        score += 90
    if _bool(attributes.get("focusable")):
        score += 40
    if attributes.get("text") or attributes.get("content-desc"):
        score += 80
    if attributes.get("resource-id"):
        score += 35
    if class_name in _SURFACE_CLASSES:
        score += 70
    if leaf and class_name:
        score += 15
    return score


def parse_uiautomator2_hierarchy(
    xml_text: str,
    *,
    revision: str,
    width: int,
    height: int,
    max_elements: int = 240,
) -> UiHierarchy:
    """Parse and compact a uiautomator2 XML dump into revision-bound elements."""

    if width <= 0 or height <= 0:
        raise ValueError("uiautomator2 hierarchy requires positive screen dimensions")
    if max_elements <= 0:
        raise ValueError("max_elements must be positive")
    try:
        root = ElementTree.fromstring(str(xml_text or ""))
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"uiautomator2 returned invalid hierarchy XML: {exc}") from exc

    parents = {
        child: parent
        for parent in root.iter()
        for child in parent
    }
    records: list[dict[str, object]] = []
    for order, node in enumerate(root.iter("node")):
        attributes = {str(key): str(value or "") for key, value in node.attrib.items()}
        if not _bool(attributes.get("visible-to-user"), True):
            continue
        bounds = _parse_bounds(attributes.get("bounds"), width, height)
        if bounds is None:
            continue
        class_name = _short(attributes.get("class"), 180)
        priority = _node_priority(attributes, class_name, len(node) == 0)
        if priority <= 0:
            continue
        ancestor_resource_ids: list[str] = []
        parent = parents.get(node)
        while parent is not None:
            resource_id = str(parent.attrib.get("resource-id") or "").strip()
            if resource_id:
                ancestor_resource_ids.append(resource_id)
            parent = parents.get(parent)
        records.append(
            {
                "order": order,
                "priority": priority,
                "bounds": bounds,
                "attributes": attributes,
                "class_name": class_name,
                "ancestor_resource_ids": tuple(ancestor_resource_ids),
            }
        )

    if len(records) > max_elements:
        selected = sorted(
            records,
            key=lambda item: (
                -int(item["priority"]),
                int(item["bounds"].top),  # type: ignore[union-attr]
                int(item["bounds"].left),  # type: ignore[union-attr]
                int(item["order"]),
            ),
        )[:max_elements]
        records = sorted(selected, key=lambda item: int(item["order"]))

    elements: list[UiElement] = []
    for index, record in enumerate(records, 1):
        attributes = record["attributes"]
        assert isinstance(attributes, dict)
        checkable = _bool(attributes.get("checkable"))
        checked_value = _bool(attributes.get("checked")) if checkable else None
        bounds = record["bounds"]
        assert isinstance(bounds, Bounds)
        canvas_like = (
            str(record["class_name"]) in _SURFACE_CLASSES
            or (
                bounds.width * bounds.height >= width * height * 0.45
                and not attributes.get("text")
                and not attributes.get("content-desc")
                and not _bool(attributes.get("clickable"))
            )
        )
        elements.append(
            UiElement(
                element_id=f"e{index:03d}",
                bounds=bounds,
                text=_short(attributes.get("text"), 300),
                resource_id=_short(attributes.get("resource-id"), 240),
                content_description=_short(attributes.get("content-desc"), 300),
                class_name=str(record["class_name"]),
                package=_short(attributes.get("package"), 200),
                clickable=_bool(attributes.get("clickable")),
                enabled=_bool(attributes.get("enabled"), True),
                focusable=_bool(attributes.get("focusable")),
                scrollable=_bool(attributes.get("scrollable")),
                selected=_bool(attributes.get("selected")),
                checked=checked_value,
                visible=True,
                attributes={
                    "checkable": checkable,
                    "focused": _bool(attributes.get("focused")),
                    "long_clickable": _bool(attributes.get("long-clickable")),
                    "password": _bool(attributes.get("password")),
                    "canvas_like": canvas_like,
                    "ancestor_resource_ids": record["ancestor_resource_ids"],
                },
            )
        )

    xml_bytes = str(xml_text or "").encode("utf-8", errors="replace")
    return UiHierarchy(
        revision=revision,
        width=width,
        height=height,
        elements=tuple(elements),
        source="uiautomator2",
        raw_artifact=Artifact(
            artifact_id=f"ui-{revision}",
            media_type="application/xml",
            data=xml_bytes,
            metadata={"raw_element_count": sum(1 for _ in root.iter("node"))},
        ),
    )


def _non_actionable_element_kind(element: UiElement) -> str:
    if element.clickable or element.focusable or element.checked is not None:
        return ""
    if element.attributes.get("canvas_like") is True:
        return "canvas"
    class_name = element.class_name.rsplit(".", 1)[-1]
    if (
        not element.text
        and not element.content_description
        and (
            class_name in _CONTAINER_CLASS_NAMES
            or class_name.endswith("Layout")
            or class_name.endswith("ViewGroup")
        )
    ):
        return "container"
    return "element"


def _is_recommendation_element(element: UiElement) -> bool:
    resources = [element.resource_id]
    ancestors = element.attributes.get("ancestor_resource_ids")
    if isinstance(ancestors, (list, tuple)):
        resources.extend(str(value or "") for value in ancestors)
    recommendation_tokens = (
        "recommend",
        "related",
        "suggest",
        "also_install",
        "guess_you",
    )
    return any(
        token in resource.lower()
        for resource in resources
        for token in recommendation_tokens
    )


def _coordinate_targets_recommendation(
    ui: UiHierarchy,
    x: int,
    y: int,
) -> bool:
    candidates = [
        element
        for element in ui.elements
        if element.visible
        and element.enabled
        and element.bounds.left <= x <= element.bounds.right
        and element.bounds.top <= y <= element.bounds.bottom
        and (
            element.clickable
            or element.focusable
            or bool(element.text)
            or bool(element.content_description)
        )
    ]
    if not candidates:
        return False

    primary_action_tokens = (
        "download_area",
        "download_progress",
        "primary_action",
        "main_action",
        "install_button",
        "update_button",
    )
    if any(
        not _is_recommendation_element(element)
        and any(token in element.resource_id.lower() for token in primary_action_tokens)
        for element in candidates
    ):
        return False

    def z_order(element: UiElement) -> int:
        match = re.search(r"(\d+)$", element.element_id)
        return int(match.group(1)) if match else 0

    return _is_recommendation_element(max(candidates, key=z_order))


def _is_notification_permission_observation(ui: UiHierarchy) -> bool:
    labels = " ".join(
        value
        for element in ui.elements
        for value in (element.text, element.content_description)
        if value
    ).lower()
    return any(
        marker in labels
        for marker in (
            "请求向您发送通知",
            "请求发送通知",
            "send you notifications",
            "send notifications",
            "post notifications",
        )
    )


def _is_allow_permission_control(element: UiElement) -> bool:
    labels = {
        element.text.strip().lower(),
        element.content_description.strip().lower(),
    }
    return bool(
        labels
        & {
            "允许",
            "允许通知",
            "allow",
            "allow notifications",
        }
    )


def format_ui_hierarchy(observation: Observation, max_chars: int = 18000) -> str:
    """Render one compact semantic observation for a text-only model turn."""

    ui = observation.ui
    context = observation.context
    if ui is None:
        return (
            f"revision={observation.revision} package={context.foreground_package or '-'} "
            f"activity={context.foreground_activity or '-'} elements=0\n"
            "当前界面没有可用的语义控件。"
        )
    lines = [
        (
            f"revision={observation.revision} source={ui.source} "
            f"screen={ui.width}x{ui.height} package={context.foreground_package or '-'} "
            f"activity={context.foreground_activity or '-'} elements={len(ui.elements)}"
        )
    ]
    non_actionable_counts = {"canvas": 0, "container": 0, "element": 0}
    for element in ui.elements:
        bounds = element.bounds
        normalized = bounds.normalized(ui.width, ui.height, 999)
        non_actionable_kind = _non_actionable_element_kind(element)
        if non_actionable_kind:
            non_actionable_counts[non_actionable_kind] += 1
        flags = [
            name
            for name, enabled in (
                ("click", element.clickable),
                ("scroll", element.scrollable),
                ("focus", element.focusable),
                ("selected", element.selected),
                ("checked", element.checked is True),
                ("disabled", not element.enabled),
                ("canvas", element.attributes.get("canvas_like") is True),
            )
            if enabled
        ]
        details = []
        if element.text:
            details.append(f'text="{_short(element.text, 180)}"')
        if element.content_description:
            details.append(f'desc="{_short(element.content_description, 180)}"')
        if element.resource_id:
            details.append(f'res="{_short(element.resource_id, 180)}"')
        if element.class_name:
            details.append(f'class="{element.class_name.rsplit(".", 1)[-1]}"')
        suffix = " ".join(details) if details else "no-label"
        display_id = (
            f"non-actionable-{non_actionable_kind}-{non_actionable_counts[non_actionable_kind]}"
            if non_actionable_kind
            else element.element_id
        )
        line = (
            f"[{display_id}] bounds=[{bounds.left},{bounds.top},{bounds.right},{bounds.bottom}] "
            f"normalized=[{normalized[0]},{normalized[1]},{normalized[2]},{normalized[3]}] "
            f"flags={','.join(flags) or '-'}"
            f"{',non-actionable' if non_actionable_kind else ''} {suffix}"
        )
        if sum(len(item) + 1 for item in lines) + len(line) > max_chars:
            lines.append(f"…其余 {len(ui.elements) - len(lines) + 1} 个元素已截断")
            break
        lines.append(line)
    return "\n".join(lines)


class Uiautomator2Provider:
    """Semantic UI provider with lazy connection and one-shot recovery."""

    def __init__(
        self,
        device_id: str,
        *,
        connect_factory: Optional[Callable[[str], object]] = None,
        idle_timeout_ms: int = 500,
    ) -> None:
        self.device_id = str(device_id or "").strip()
        if not self.device_id:
            raise ValueError("uiautomator2 requires a device serial")
        self._connect_factory = connect_factory
        self.idle_timeout_ms = max(100, min(5000, int(idle_timeout_ms)))
        self._device: Optional[object] = None
        self._revision_counter = 0
        self._lock = threading.RLock()
        self.last_error = ""

    def _configure_device(self, device: object) -> object:
        try:
            settings = getattr(device, "settings", None)
            if settings is not None:
                settings["wait_timeout"] = 2.0
        except Exception:
            pass
        try:
            jsonrpc = getattr(device, "jsonrpc", None)
            configure = getattr(jsonrpc, "setConfigurator", None)
            if callable(configure):
                configure(
                    {
                        "waitForIdleTimeout": self.idle_timeout_ms,
                        "waitForSelectorTimeout": 1000,
                    }
                )
        except Exception:
            pass
        return device

    def _connect(self) -> object:
        if self._connect_factory is not None:
            return self._configure_device(self._connect_factory(self.device_id))
        available, detail = uiautomator2_dependency_status()
        if not available:
            raise RuntimeError(detail)
        try:
            import uiautomator2 as u2  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - guarded by status check
            raise RuntimeError("uiautomator2 依赖不可用") from exc
        return self._configure_device(u2.connect(self.device_id))

    def _device_or_connect(self) -> object:
        with self._lock:
            if self._device is None:
                self._device = self._connect()
            return self._device

    def recover(self) -> bool:
        with self._lock:
            self._device = None
            try:
                device = self._connect()
                reset = getattr(device, "reset_uiautomator", None)
                if callable(reset):
                    reset()
                self._configure_device(device)
                self._device = device
                self.last_error = ""
                return True
            except Exception as exc:  # third-party transport exceptions vary
                self.last_error = _short(exc, 500)
                self._device = None
                return False

    def observe(self, request: ObservationRequest) -> Observation:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                device = self._device_or_connect()
                width, height = getattr(device, "window_size")()
                xml_text = getattr(device, "dump_hierarchy")(
                    compressed=True,
                    pretty=False,
                    max_depth=40,
                    root_in_active=True,
                )
                try:
                    info = getattr(device, "info")
                except Exception:
                    info = {}
                if not isinstance(info, Mapping):
                    info = {}
                info_package = str(info.get("currentPackageName") or "").strip()
                foreground_package = info_package
                foreground_activity = ""
                self._revision_counter += 1
                digest = hashlib.sha256(
                    (
                        str(xml_text)
                        + "\n"
                        + foreground_package
                        + "\n"
                        + foreground_activity
                    ).encode("utf-8", errors="replace")
                ).hexdigest()[:10]
                revision = f"u2-{self._revision_counter:06d}-{digest}"
                hierarchy = parse_uiautomator2_hierarchy(
                    str(xml_text),
                    revision=revision,
                    width=int(width),
                    height=int(height),
                    max_elements=request.max_ui_elements,
                )
                observation = Observation(
                    revision=revision,
                    captured_at=time.time(),
                    channels=frozenset(
                        {"ui_hierarchy", "foreground_activity", "device_context"}
                    ),
                    context=DeviceContext(
                        device_id=self.device_id,
                        foreground_package=foreground_package,
                        foreground_activity=foreground_activity,
                        orientation=(
                            "portrait" if int(width) <= int(height) else "landscape"
                        ),
                        screen_on=(
                            _bool(info.get("screenOn"))
                            if info.get("screenOn") is not None
                            else None
                        ),
                        metadata={
                            "display_rotation": info.get("displayRotation"),
                        },
                    ),
                    ui=hierarchy,
                    metadata={"provider": "uiautomator2"},
                )
                self.last_error = ""
                return observation
            except Exception as exc:  # third-party transport exceptions vary
                last_error = exc
                self.last_error = _short(exc, 500)
                if attempt == 0 and self.recover():
                    continue
                break
        raise RuntimeError(
            f"uiautomator2 无法读取界面：{_short(last_error or self.last_error, 800)}"
        ) from last_error

    @staticmethod
    def _normalized_pair(
        action: Mapping[str, object],
        key: str,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        value = action.get(key)
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            raise ValueError(f"{action.get('action')} requires {key}=[x,y]")
        try:
            x = max(0, min(999, int(value[0])))
            y = max(0, min(999, int(value[1])))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{action.get('action')} requires integer {key}") from exc
        return (
            int(round(x / 999 * max(0, width - 1))),
            int(round(y / 999 * max(0, height - 1))),
        )

    def execute_action(
        self,
        action: Mapping[str, object],
        observation: Observation,
        stop_event: threading.Event,
    ) -> str:
        """Execute one non-terminal, allowlisted phone action through uiautomator2."""

        name = str(action.get("action") or "").strip().lower()
        if name in {"finish", "skip", "take_over"}:
            raise ValueError(f"{name} is a terminal action handled by the agent")
        if name == "wait":
            try:
                duration = float(action.get("duration_seconds") or 1.0)
            except (TypeError, ValueError):
                duration = 1.0
            duration = max(0.2, min(30.0, duration))
            stop_event.wait(duration)
            return f"uiautomator2 等待 {duration:.1f} 秒"

        device = self._device_or_connect()
        ui = observation.ui
        width = ui.width if ui is not None else 1
        height = ui.height if ui is not None else 1
        try:
            if name == "tap_element":
                if ui is None:
                    raise ValueError("tap_element requires a UI hierarchy")
                revision = str(action.get("observation_revision") or "").strip()
                if revision != observation.revision:
                    raise ValueError(
                        "tap_element observation_revision is stale; use the latest revision"
                    )
                element_id = str(action.get("element_id") or "").strip()
                if not element_id:
                    raise ValueError("tap_element requires element_id")
                try:
                    element = ui.element(element_id)
                except KeyError as exc:
                    raise ValueError(f"unknown element_id for current revision: {element_id}") from exc
                if not element.visible or not element.enabled:
                    raise ValueError(f"element {element_id} is not enabled and visible")
                non_actionable_kind = _non_actionable_element_kind(element)
                if non_actionable_kind:
                    raise ValueError(
                        f"element {element_id} is a non-actionable {non_actionable_kind}; "
                        "choose a clickable/focusable child or a precise coordinate target"
                    )
                if (
                    action.get("_forbid_recommendation_controls") is True
                    and _is_recommendation_element(element)
                ):
                    raise ValueError(
                        f"element {element_id} belongs to an app-store recommendation area; "
                        "use only the target app's title-bound primary install/update control"
                    )
                if (
                    action.get("_forbid_notification_allow") is True
                    and ui is not None
                    and _is_notification_permission_observation(ui)
                    and _is_allow_permission_control(element)
                ):
                    raise ValueError(
                        "notification permission must be denied for this task; "
                        "select 禁止/Don't allow"
                    )
                x, y = element.bounds.center
                getattr(device, "click")(x, y)
                label = element.text or element.content_description or element.resource_id
                return f"uiautomator2 点击 {element_id} ({x}, {y}){f' · {_short(label, 80)}' if label else ''}"
            if name in {"tap", "double_tap", "long_press"}:
                x, y = self._normalized_pair(action, "element", width, height)
                if (
                    action.get("_forbid_recommendation_controls") is True
                    and ui is not None
                    and _coordinate_targets_recommendation(ui, x, y)
                ):
                    raise ValueError(
                        "coordinate target belongs to an app-store recommendation area; "
                        "use only the target app's primary install/update control"
                    )
                if (
                    action.get("_forbid_notification_allow") is True
                    and ui is not None
                    and _is_notification_permission_observation(ui)
                    and any(
                        _is_allow_permission_control(element)
                        and element.bounds.left <= x <= element.bounds.right
                        and element.bounds.top <= y <= element.bounds.bottom
                        for element in ui.elements
                    )
                ):
                    raise ValueError(
                        "notification permission must be denied for this task; "
                        "select 禁止/Don't allow"
                    )
                if name == "tap":
                    getattr(device, "click")(x, y)
                    return f"uiautomator2 点击 ({x}, {y})"
                if name == "double_tap":
                    getattr(device, "double_click")(x, y, duration=0.12)
                    return f"uiautomator2 双击 ({x}, {y})"
                try:
                    duration_ms = int(action.get("duration_ms") or 800)
                except (TypeError, ValueError):
                    duration_ms = 800
                duration_ms = max(300, min(5000, duration_ms))
                getattr(device, "long_click")(x, y, duration=duration_ms / 1000.0)
                return f"uiautomator2 长按 ({x}, {y}) {duration_ms} ms"
            if name in {"swipe", "swipe_fast"}:
                start_x, start_y = self._normalized_pair(action, "start", width, height)
                end_x, end_y = self._normalized_pair(action, "end", width, height)
                default_duration = 220 if name == "swipe_fast" else 600
                try:
                    duration_ms = int(action.get("duration_ms") or default_duration)
                except (TypeError, ValueError):
                    duration_ms = default_duration
                duration_ms = max(50, min(5000, duration_ms))
                getattr(device, "swipe")(
                    start_x,
                    start_y,
                    end_x,
                    end_y,
                    duration=duration_ms / 1000.0,
                )
                return (
                    f"uiautomator2 滑动 ({start_x}, {start_y}) → "
                    f"({end_x}, {end_y}) {duration_ms} ms"
                )
            key_names = {
                "back": "back",
                "home": "home",
                "recent": "recent",
                "enter": "enter",
                "delete": "delete",
            }
            if name in key_names:
                getattr(device, "press")(key_names[name])
                return f"uiautomator2 按键 {name}"
            if name == "wake":
                getattr(device, "screen_on")()
                return "uiautomator2 点亮屏幕"
            if name == "input_text":
                text = str(action.get("text") or "")
                if not text:
                    raise ValueError("input_text requires text")
                if len(text) > 500 or any(ord(character) < 32 for character in text):
                    raise ValueError("input_text contains unsupported control characters")
                selector = device(focused=True)  # type: ignore[operator]
                if not bool(getattr(selector, "exists", False)):
                    raise ValueError("input_text requires a focused editable element")
                set_text = getattr(selector, "set_text", None)
                if not callable(set_text):
                    raise RuntimeError("focused uiautomator2 element does not support set_text")
                try:
                    set_text(text)
                except Exception as first_exc:
                    first_error = _exception_detail(first_exc)
                    if not self.recover():
                        raise RuntimeError(
                            f"input_text first attempt failed ({first_error}); "
                            f"uiautomator2 recovery failed ({self.last_error or 'unknown error'})"
                        ) from first_exc
                    retry_device = self._device_or_connect()
                    retry_selector = retry_device(focused=True)  # type: ignore[operator]
                    if not bool(getattr(retry_selector, "exists", False)):
                        raise RuntimeError(
                            f"input_text first attempt failed ({first_error}); "
                            "focused editable element disappeared after recovery"
                        ) from first_exc
                    retry_set_text = getattr(retry_selector, "set_text", None)
                    if not callable(retry_set_text):
                        raise RuntimeError(
                            f"input_text first attempt failed ({first_error}); "
                            "focused element does not support set_text after recovery"
                        ) from first_exc
                    try:
                        retry_set_text(text)
                    except Exception as retry_exc:
                        raise RuntimeError(
                            f"input_text failed twice (first: {first_error}; "
                            f"retry: {_exception_detail(retry_exc)})"
                        ) from retry_exc
                return f"uiautomator2 输入文本：{_short(text, 80)}"
            if name == "launch_app":
                package = str(action.get("package") or "").strip()
                if not _PACKAGE_RE.fullmatch(package):
                    raise ValueError("launch_app requires a valid Android package name")
                forbidden_packages = action.get("_forbid_launch_packages")
                if (
                    isinstance(forbidden_packages, (list, tuple, set, frozenset))
                    and package in {str(value or "").strip() for value in forbidden_packages}
                ):
                    raise ValueError(
                        f"launch_app package is forbidden for this task: {package}"
                    )
                app_info = getattr(device, "app_info", None)
                if callable(app_info):
                    try:
                        app_info(package)
                    except Exception as exc:
                        raise ValueError(
                            f"launch_app package is not installed: {package}"
                        ) from exc
                getattr(device, "app_start")(
                    package,
                    wait=True,
                    stop=False,
                    use_monkey=True,
                )
                app_current = getattr(device, "app_current", None)
                if callable(app_current):
                    current = app_current()
                    current_package = (
                        str(current.get("package") or "").strip()
                        if isinstance(current, Mapping)
                        else ""
                    )
                    if current_package and current_package != package:
                        raise RuntimeError(
                            f"launch_app did not reach {package}; foreground is {current_package}"
                        )
                return f"uiautomator2 启动应用 {package}"
        except ValueError:
            raise
        except Exception as exc:  # third-party RPC exception types are not stable
            detail = _exception_detail(exc)
            recovered = self.recover()
            if not recovered and self.last_error:
                detail = f"{detail}; recovery failed: {self.last_error}"
            self.last_error = detail
            raise RuntimeError(f"uiautomator2 动作执行失败：{detail}") from exc
        raise ValueError(f"Unsupported uiautomator2 phone_action: {name or '<empty>'}")
