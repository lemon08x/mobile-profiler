"""Dashboard controller for the optional deterministic visual automation spike.

The controller keeps OpenCV and NumPy optional.  Dashboard startup only probes
whether the modules are discoverable; the heavyweight runtime is imported when
the user explicitly starts the synthetic verification demo.
"""

from __future__ import annotations

import importlib.util
import json
import math
import threading
import time
from collections import deque
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Dict, List, Optional

from .automation import (
    Artifact,
    NormalizedRegion,
    OpenCvTemplateMatcher,
    ScreenFrame,
    TemplateSpec,
    template_matching_dependency_status,
)
from .maaend_runtime import MaaEndRuntimeController
from .star_rail_runtime import StarRailAsuRuntimeController


IMAGE_EXTRA_ESTIMATED_BYTES = 167_348_599
DEFAULT_DEMO_ITERATIONS = 50
MAX_DEMO_ITERATIONS = 100
DEFAULT_PROJECT_IDS = ("maaend",)
DEFAULT_FEATURE_IDS: tuple[str, ...] = ("maaend-profile",)
STAR_RAIL_UNIVERSE_FEATURE_ID = "m7a-universe"
MAAEND_PROFILE_FEATURE_ID = "maaend-profile"


def _distribution_version(*names: str) -> str:
    for name in names:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return ""


@lru_cache(maxsize=1)
def probe_image_runtime() -> Dict[str, object]:
    """Probe the optional image extra without importing its native modules."""

    try:
        opencv_found = importlib.util.find_spec("cv2") is not None
        numpy_found = importlib.util.find_spec("numpy") is not None
    except (ImportError, OSError, ValueError) as exc:
        return {
            "available": False,
            "detail": f"无法检查 OpenCV / NumPy：{exc}",
            "opencv_version": "",
            "numpy_version": "",
        }
    opencv_version = _distribution_version("opencv-python-headless", "opencv-python")
    numpy_version = _distribution_version("numpy")
    available = opencv_found and numpy_found
    if available:
        detail = (
            f"OpenCV {opencv_version or '已安装'} / "
            f"NumPy {numpy_version or '已安装'}"
        )
    else:
        missing = []
        if not opencv_found:
            missing.append("OpenCV")
        if not numpy_found:
            missing.append("NumPy")
        detail = f"缺少可选图像运行时：{'、'.join(missing)}"
    return {
        "available": available,
        "detail": detail,
        "opencv_version": opencv_version,
        "numpy_version": numpy_version,
    }


def _timing_summary(elapsed_s: List[float]) -> Dict[str, float]:
    elapsed_ms = sorted(value * 1000.0 for value in elapsed_s)
    if not elapsed_ms:
        return {
            "mean_ms": 0.0,
            "p95_ms": 0.0,
            "minimum_ms": 0.0,
            "maximum_ms": 0.0,
        }
    p95_index = max(
        0,
        min(len(elapsed_ms) - 1, math.ceil(len(elapsed_ms) * 0.95) - 1),
    )
    return {
        "mean_ms": sum(elapsed_ms) / len(elapsed_ms),
        "p95_ms": elapsed_ms[p95_index],
        "minimum_ms": elapsed_ms[0],
        "maximum_ms": elapsed_ms[-1],
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def run_synthetic_visual_demo(output_root: Path, iterations: int) -> Dict[str, object]:
    """Run the package-level synthetic matcher demo and persist its evidence."""

    available, detail = template_matching_dependency_status()
    if not available:
        raise RuntimeError(
            f"{detail}. Install with: python -m pip install -e \".[image]\""
        )

    import cv2  # type: ignore[import-not-found]
    import numpy  # type: ignore[import-not-found]

    output_root.mkdir(parents=True, exist_ok=True)
    rng = numpy.random.default_rng(20260722)
    frame = numpy.zeros((1280, 720, 3), dtype=numpy.uint8)
    frame[:] = (24, 28, 36)
    for _ in range(80):
        x = int(rng.integers(0, frame.shape[1]))
        y = int(rng.integers(0, frame.shape[0]))
        radius = int(rng.integers(2, 8))
        color = tuple(int(value) for value in rng.integers(40, 130, size=3))
        cv2.circle(frame, (x, y), radius, color, -1)

    template = rng.integers(0, 255, size=(96, 144, 3), dtype=numpy.uint8)
    cv2.rectangle(template, (4, 4), (139, 91), (255, 255, 255), 3)
    cv2.circle(template, (72, 48), 24, (30, 210, 250), -1)
    cv2.putText(
        template,
        "PLAY",
        (34, 56),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (10, 10, 10),
        2,
        cv2.LINE_AA,
    )
    expected_left, expected_top = 438, 846
    frame[
        expected_top : expected_top + template.shape[0],
        expected_left : expected_left + template.shape[1],
    ] = template

    encoded_frame, frame_payload = cv2.imencode(".png", frame)
    encoded_template, template_payload = cv2.imencode(".png", template)
    if not encoded_frame or not encoded_template:
        raise RuntimeError("OpenCV 无法编码合成验证图片")
    frame_bytes = frame_payload.tobytes()
    template_bytes = template_payload.tobytes()
    _atomic_write(output_root / "frame.png", frame_bytes)
    _atomic_write(output_root / "template.png", template_bytes)

    screen = ScreenFrame(
        Artifact("open-source-demo-frame", "image/png", data=frame_bytes),
        width=frame.shape[1],
        height=frame.shape[0],
    )
    spec = TemplateSpec(
        "play-button",
        template_data=template_bytes,
        threshold=0.98,
        region=NormalizedRegion(0.45, 0.55, 0.95, 0.95),
        scales=(0.9, 1.0, 1.1),
    )
    matcher = OpenCvTemplateMatcher()
    matcher.match(screen, spec)
    cached_elapsed = [matcher.match(screen, spec).elapsed_s for _ in range(iterations)]

    fresh_elapsed: List[float] = []
    match = None
    for index in range(iterations):
        variant = frame.copy()
        variant[0, index % variant.shape[1]] = (
            index % 251,
            (index * 3) % 251,
            (index * 7) % 251,
        )
        encoded_variant, variant_payload = cv2.imencode(".png", variant)
        if not encoded_variant:
            raise RuntimeError("OpenCV 无法编码合成截图变体")
        fresh_screen = ScreenFrame(
            Artifact(
                f"open-source-demo-frame-{index}",
                "image/png",
                data=variant_payload.tobytes(),
            ),
            width=screen.width,
            height=screen.height,
        )
        match = matcher.match(fresh_screen, spec)
        fresh_elapsed.append(match.elapsed_s)
    if match is None:
        raise RuntimeError("合成验证没有产生匹配结果")

    overlay = matcher.annotated_artifact(
        screen,
        match,
        artifact_id="open-source-demo-overlay",
    )
    _atomic_write(output_root / "match-overlay.png", overlay.data)
    cached_timing = _timing_summary(cached_elapsed)
    fresh_timing = _timing_summary(fresh_elapsed)
    bounds = (
        [match.bounds.left, match.bounds.top, match.bounds.right, match.bounds.bottom]
        if match.bounds is not None
        else None
    )
    expected_bounds = [
        expected_left,
        expected_top,
        expected_left + template.shape[1],
        expected_top + template.shape[0],
    ]
    result: Dict[str, object] = {
        "dependency": detail,
        "iterations": iterations,
        "frame": {"width": screen.width, "height": screen.height},
        "matched": match.matched,
        "score": match.score,
        "threshold": match.threshold,
        "scale": match.scale,
        "bounds": bounds,
        "expected_bounds": expected_bounds,
        "coordinate_exact": bounds == expected_bounds,
        "same_observation_timing": cached_timing,
        "fresh_observation_timing": fresh_timing,
        "mean_ms": fresh_timing["mean_ms"],
        "p95_ms": fresh_timing["p95_ms"],
    }
    return result


class OpenSourceAutomationController:
    """Own the open-source project catalog, selection, and diagnostics."""

    def __init__(
        self,
        output_root: Path,
        bundle_path: Optional[Path] = None,
        adb: str = "adb",
        feature_adapters: Optional[Dict[str, object]] = None,
    ) -> None:
        runtime_root = output_root.resolve()
        self.output_root = (runtime_root / "open-source-automation").resolve()
        self.bundle_path = bundle_path.resolve() if bundle_path is not None else None
        self._lock = threading.RLock()
        self._running = False
        self._status = "ready"
        self._last_error = ""
        self._last_result: Optional[Dict[str, object]] = None
        self._last_run_at: Optional[float] = None
        self._revision = ""
        self._selected_project_ids = list(DEFAULT_PROJECT_IDS)
        self._selected_feature_ids = list(DEFAULT_FEATURE_IDS)
        self._selection_saved_at: Optional[float] = None
        self._logs: deque[Dict[str, object]] = deque(maxlen=16)
        self._feature_adapters: Dict[str, object] = (
            {
                STAR_RAIL_UNIVERSE_FEATURE_ID: StarRailAsuRuntimeController(
                    adb,
                    runtime_root,
                ),
                MAAEND_PROFILE_FEATURE_ID: MaaEndRuntimeController(
                    runtime_root,
                    adb=adb,
                ),
            }
            if feature_adapters is None
            else dict(feature_adapters)
        )
        self._load_selection()
        self._log("ready", "开源自动化项目目录已加载，等待选择功能方案")

    @staticmethod
    def _projects() -> List[Dict[str, object]]:
        return [
            {
                "id": "march7th-assistant",
                "name": "March7thAssistant",
                "short_name": "M7A",
                "game": "崩坏：星穹铁道",
                "summary": "覆盖日常、周常与工具箱任务的开源自动化项目。",
                "source_url": "https://github.com/moesnow/March7thAssistant",
                "selectable": True,
                "status": "configurable",
                "status_label": "功能可配置",
                "adapter_status": "pending",
                "adapter_label": "执行适配器待接入",
                "features": [
                    {
                        "id": "m7a-universe",
                        "name": "自动模拟宇宙",
                        "category": "battle",
                        "category_label": "战斗",
                        "description": "编排模拟宇宙 / 差分宇宙的自动化流程。",
                        "featured": True,
                    },
                ],
            },
            {
                "id": "maaend",
                "name": "MaaEnd",
                "short_name": "END",
                "game": "明日方舟：终末地",
                "summary": "使用官方 MaaEnd 发布包运行已配置的 ADB 自动化实例。",
                "source_url": "https://github.com/MaaEnd/MaaEnd",
                "selectable": True,
                "status": "configurable",
                "status_label": "功能可配置",
                "adapter_status": "pending",
                "adapter_label": "等待 MaaEnd 发布目录",
                "features": [
                    {
                        "id": "maaend-profile",
                        "name": "运行已配置实例",
                        "category": "daily",
                        "category_label": "实例任务",
                        "description": (
                            "运行 MaaEnd 中已保存的 ADB 实例；任务与选项继续由 "
                            "MaaEnd 管理。"
                        ),
                        "featured": True,
                    }
                ],
            },
        ]

    @staticmethod
    def _selection_ids(payload: Dict[str, object], key: str) -> List[str]:
        value = payload.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        identifiers: List[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{key} must contain non-empty strings")
            identifier = item.strip()
            if identifier not in identifiers:
                identifiers.append(identifier)
        return identifiers

    @classmethod
    def _normalize_selection(
        cls,
        payload: Dict[str, object],
    ) -> tuple[List[str], List[str]]:
        """Normalize the nested project model and the legacy flat model."""

        nested = payload.get("projects")
        if nested is not None:
            if not isinstance(nested, list):
                raise ValueError("projects must be a list")
            requested_project_ids: List[str] = []
            requested_feature_ids: List[str] = []
            for item in nested:
                if not isinstance(item, dict):
                    raise ValueError("projects must contain objects")
                project_id = str(item.get("project_id") or "").strip()
                if not project_id:
                    raise ValueError("projects must contain project_id")
                if project_id not in requested_project_ids:
                    requested_project_ids.append(project_id)
                for feature_id in cls._selection_ids(item, "feature_ids"):
                    if feature_id not in requested_feature_ids:
                        requested_feature_ids.append(feature_id)
        else:
            requested_project_ids = cls._selection_ids(payload, "project_ids")
            requested_feature_ids = cls._selection_ids(payload, "feature_ids")

        if len(requested_project_ids) > 1:
            raise ValueError("only one open-source automation project may be selected")

        projects = cls._projects()
        project_by_id = {str(project["id"]): project for project in projects}
        feature_by_id: Dict[str, Dict[str, object]] = {}
        for project in projects:
            project_id = str(project["id"])
            for feature in project.get("features", []):
                if isinstance(feature, dict):
                    feature_by_id[str(feature["id"])] = {
                        **feature,
                        "project_id": project_id,
                    }

        for project_id in requested_project_ids:
            project = project_by_id.get(project_id)
            if project is None:
                raise ValueError(
                    f"unknown open-source automation project: {project_id}"
                )
            if project.get("selectable") is not True:
                raise ValueError(
                    "open-source automation project is not selectable yet: "
                    f"{project_id}"
                )
        selected_projects = set(requested_project_ids)
        for feature_id in requested_feature_ids:
            feature = feature_by_id.get(feature_id)
            if feature is None:
                raise ValueError(
                    f"unknown open-source automation feature: {feature_id}"
                )
            if str(feature["project_id"]) not in selected_projects:
                raise ValueError(
                    f"feature {feature_id} does not belong to a selected project"
                )

        normalized_project_ids = [
            str(project["id"])
            for project in projects
            if str(project["id"]) in selected_projects
        ]
        selected_features = set(requested_feature_ids)
        normalized_feature_ids = [
            str(feature["id"])
            for project in projects
            for feature in project.get("features", [])
            if isinstance(feature, dict) and str(feature["id"]) in selected_features
        ]
        return normalized_project_ids, normalized_feature_ids

    @staticmethod
    def _selection_projects(
        project_ids: List[str],
        feature_ids: List[str],
    ) -> List[Dict[str, object]]:
        selected_features = set(feature_ids)
        rows: List[Dict[str, object]] = []
        for project in OpenSourceAutomationController._projects():
            project_id = str(project["id"])
            if project_id not in project_ids:
                continue
            rows.append(
                {
                    "project_id": project_id,
                    "feature_ids": [
                        str(feature["id"])
                        for feature in project.get("features", [])
                        if isinstance(feature, dict)
                        and str(feature["id"]) in selected_features
                    ],
                }
            )
        return rows

    def _load_selection(self) -> None:
        path = self.output_root / "selection.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return
            project_ids, feature_ids = self._normalize_selection(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return
        saved_at = value.get("saved_at")
        with self._lock:
            self._selected_project_ids = project_ids
            self._selected_feature_ids = feature_ids
            self._selection_saved_at = (
                float(saved_at) if isinstance(saved_at, (int, float)) else None
            )

    def _persist_selection(
        self,
        project_ids: List[str],
        feature_ids: List[str],
        saved_at: float,
    ) -> None:
        payload = {
            "schema_version": 2,
            "projects": self._selection_projects(project_ids, feature_ids),
            "project_ids": project_ids,
            "feature_ids": feature_ids,
            "saved_at": saved_at,
        }
        _atomic_write(
            self.output_root / "selection.json",
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def update_selection(self, payload: Dict[str, object]) -> Dict[str, object]:
        """Validate and save a project/feature configuration without executing it."""

        normalized_project_ids, normalized_feature_ids = self._normalize_selection(
            payload
        )
        saved_at = time.time()
        self._persist_selection(
            normalized_project_ids,
            normalized_feature_ids,
            saved_at,
        )
        with self._lock:
            self._selected_project_ids = normalized_project_ids
            self._selected_feature_ids = normalized_feature_ids
            self._selection_saved_at = saved_at
        self._log(
            "configured",
            (
                f"已保存功能方案：{len(normalized_project_ids)} 个项目，"
                f"{len(normalized_feature_ids)} 项功能"
            ),
        )
        return self.snapshot()

    def _log(self, status: str, message: str) -> None:
        with self._lock:
            self._logs.append(
                {
                    "time": time.time(),
                    "status": status,
                    "message": message,
                }
            )

    def _bundle_summary(self) -> Dict[str, object]:
        path = self.bundle_path
        if path is None or not path.is_file():
            return {
                "available": False,
                "graph_id": "",
                "path": "",
                "states": [],
                "transitions": [],
                "templates": [],
                "max_transitions": 0,
            }
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "available": False,
                "graph_id": "",
                "path": str(path),
                "states": [],
                "transitions": [],
                "templates": [],
                "max_transitions": 0,
                "error": str(exc),
            }
        if not isinstance(value, dict):
            value = {}
        states = value.get("states") if isinstance(value.get("states"), list) else []
        transitions = (
            value.get("transitions") if isinstance(value.get("transitions"), list) else []
        )
        templates = (
            value.get("templates") if isinstance(value.get("templates"), list) else []
        )
        return {
            "available": True,
            "graph_id": str(value.get("graph_id") or path.stem),
            "path": str(path),
            "states": states,
            "transitions": transitions,
            "templates": templates,
            "max_transitions": int(value.get("max_transitions") or 0),
        }

    @staticmethod
    def _alignment() -> List[Dict[str, str]]:
        return [
            {
                "feature": "点击图片 / 查找图片",
                "upstream": "模板、阈值、检测区域、重试",
                "status": "ready",
                "current": "OpenCV 多尺度模板匹配、阈值、归一化 ROI 与证据框",
            },
            {
                "feature": "流程编排",
                "upstream": "顺序步骤与 if / for / while",
                "status": "partial",
                "current": "强类型状态图、最短路径与 transition 上限；通用循环编辑器待接入",
            },
            {
                "feature": "点击坐标 / 按键",
                "upstream": "直接执行桌面输入",
                "status": "partial",
                "current": "JSON 动作已强类型化；真机 ADB Gateway 与审批策略待接入",
            },
            {
                "feature": "点击文字 / 查找文字",
                "upstream": "OCR 文字识别",
                "status": "planned",
                "current": "当前未引入 OCR 模型，优先保持安装体积可控",
            },
            {
                "feature": "模板采集 / 导入导出",
                "upstream": "截图框选、流程素材目录与 ZIP",
                "status": "planned",
                "current": "已定义独立资源包格式；采集器与资源包管理前端待实现",
            },
            {
                "feature": "运行与调试",
                "upstream": "完整流程、选中步骤、停止与日志",
                "status": "demo",
                "current": "本页可运行合成验证、查看耗时/坐标/证据与日志",
            },
        ]

    def _adapter_snapshots(self) -> Dict[str, Dict[str, object]]:
        snapshots: Dict[str, Dict[str, object]] = {}
        for feature_id, adapter in self._feature_adapters.items():
            snapshot = getattr(adapter, "snapshot", None)
            if not callable(snapshot):
                snapshots[feature_id] = {
                    "status": "error",
                    "running": False,
                    "available": False,
                    "last_error": "adapter does not expose snapshot()",
                }
                continue
            try:
                value = snapshot()
            except Exception as exc:
                value = {
                    "status": "error",
                    "running": False,
                    "available": False,
                    "last_error": str(exc),
                }
            snapshots[feature_id] = value if isinstance(value, dict) else {
                "status": "error",
                "running": False,
                "available": False,
                "last_error": "adapter snapshot must be an object",
            }
        return snapshots

    @classmethod
    def _projects_with_adapter_state(
        cls,
        adapter_snapshots: Dict[str, Dict[str, object]],
    ) -> List[Dict[str, object]]:
        projects = cls._projects()
        for project in projects:
            available_count = 0
            planned_count = 0
            project_selectable = project.get("selectable") is True
            features = project.get("features", [])
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                feature_id = str(feature.get("id") or "")
                adapter = adapter_snapshots.get(feature_id)
                if adapter is None:
                    feature.update(
                        {
                            "adapter_id": "",
                            "implementation_status": (
                                "planned" if project_selectable else "coming_soon"
                            ),
                            "implementation_label": (
                                "待适配" if project_selectable else "后续接入"
                            ),
                            "can_execute": False,
                        }
                    )
                    planned_count += 1
                    continue
                available = adapter.get("available") is True
                running = adapter.get("running") is True
                feature.update(
                    {
                        "adapter_id": str(
                            adapter.get("adapter_id") or "external-runtime"
                        ),
                        "implementation_status": (
                            "running" if running else "ready" if available else "runtime_missing"
                        ),
                        "implementation_label": (
                            "运行中" if running else "已接入" if available else "运行时未安装"
                        ),
                        "can_execute": available,
                        "runtime_status": str(adapter.get("status") or "unknown"),
                    }
                )
                if available:
                    available_count += 1
                else:
                    planned_count += 1
            project["available_feature_count"] = available_count
            project["planned_feature_count"] = planned_count
            if project_selectable:
                project["adapter_status"] = (
                    "ready" if available_count else "runtime_missing"
                )
                project["adapter_label"] = (
                    f"{available_count} 项功能可运行"
                    if available_count
                    else "功能可配置，运行时待安装"
                )
                project["status_label"] = project["adapter_label"]
        return projects

    @staticmethod
    def _execution_summary(
        selected_project_ids: List[str],
        selected_feature_ids: List[str],
        adapter_snapshots: Dict[str, Dict[str, object]],
    ) -> Dict[str, object]:
        runnable = [
            feature_id
            for feature_id in selected_feature_ids
            if adapter_snapshots.get(feature_id, {}).get("available") is True
        ]
        preflightable = [
            feature_id
            for feature_id in selected_feature_ids
            if feature_id in adapter_snapshots
            and (
                adapter_snapshots[feature_id].get("available") is True
                or (
                    isinstance(
                        adapter_snapshots[feature_id].get("capabilities"),
                        dict,
                    )
                    and adapter_snapshots[feature_id]["capabilities"].get(
                        "configure_when_unavailable"
                    )
                    is True
                )
            )
        ]
        pending = [
            feature_id
            for feature_id in selected_feature_ids
            if feature_id not in runnable
        ]
        running = [
            feature_id
            for feature_id, adapter in adapter_snapshots.items()
            if adapter.get("running") is True
        ]
        ready = [
            feature_id
            for feature_id in runnable
            if isinstance(adapter_snapshots[feature_id].get("preflight"), dict)
            and isinstance(
                adapter_snapshots[feature_id]["preflight"].get("screen"), dict
            )
            and adapter_snapshots[feature_id]["preflight"]["screen"].get(
                "game_ready"
            )
            is True
        ]
        if running:
            status = "running"
            label = "自动化运行中"
            detail = f"正在运行 {len(running)} 项功能，可随时停止。"
        elif not selected_feature_ids:
            status = "not_configured"
            label = "等待选择功能"
            detail = "从下拉框选择一个已接入项目，再配置需要运行的功能。"
        elif ready:
            status = "ready"
            label = "真机预检已通过"
            detail = "已接入功能可以启动。"
        elif preflightable:
            status = "preflight_required"
            label = "需要运行真机预检"
            detail = "填写运行时配置并完成预检后，才允许启动所选功能。"
        else:
            status = "runtime_pending"
            label = "外部运行时尚未就绪"
            detail = "功能适配器已接入，请完成对应外部运行时配置。"
        return {
            "status": status,
            "label": label,
            "detail": detail,
            "can_preflight": bool(preflightable) and not running,
            "can_execute": bool(ready) and not running,
            "can_stop": bool(running),
            "selected_project_count": len(selected_project_ids),
            "selected_feature_count": len(selected_feature_ids),
            "runnable_feature_ids": runnable,
            "preflight_feature_ids": preflightable,
            "pending_feature_ids": pending,
            "ready_feature_ids": ready,
            "running_feature_ids": running,
        }

    def _resolve_feature_adapter(
        self,
        payload: Dict[str, object],
        *,
        allow_running_unselected: bool = False,
    ) -> tuple[str, object]:
        requested = str(payload.get("feature_id") or "").strip()
        with self._lock:
            selected = list(self._selected_feature_ids)
        if requested:
            feature_id = requested
        else:
            candidates = [
                feature_id
                for feature_id in selected
                if feature_id in self._feature_adapters
            ]
            if allow_running_unselected:
                running = [
                    feature_id
                    for feature_id, adapter in self._adapter_snapshots().items()
                    if adapter.get("running") is True
                ]
                candidates = running or candidates
            if len(candidates) != 1:
                raise ValueError("feature_id is required when multiple adapters are selected")
            feature_id = candidates[0]
        adapter = self._feature_adapters.get(feature_id)
        if adapter is None:
            raise RuntimeError(f"feature adapter is not available: {feature_id}")
        if feature_id not in selected and not allow_running_unselected:
            raise RuntimeError(f"feature is not selected: {feature_id}")
        return feature_id, adapter

    def preflight(self, payload: Dict[str, object]) -> Dict[str, object]:
        feature_id, adapter = self._resolve_feature_adapter(payload)
        method = getattr(adapter, "preflight", None)
        if not callable(method):
            raise RuntimeError(f"feature does not support preflight: {feature_id}")
        self._log("preflighting", f"开始预检功能 {feature_id}")
        method(payload)
        self._log("preflighted", f"功能 {feature_id} 预检完成")
        return self.snapshot()

    def configure(self, payload: Dict[str, object]) -> Dict[str, object]:
        feature_id, adapter = self._resolve_feature_adapter(payload)
        method = getattr(adapter, "configure", None)
        if not callable(method):
            raise RuntimeError(f"feature does not support configuration: {feature_id}")
        self._log("configuring", f"开始保存功能 {feature_id} 的任务配置")
        method(payload)
        self._log("configured", f"功能 {feature_id} 的任务配置已保存")
        return self.snapshot()

    def start(self, payload: Dict[str, object]) -> Dict[str, object]:
        feature_id, adapter = self._resolve_feature_adapter(payload)
        method = getattr(adapter, "start", None)
        if not callable(method):
            raise RuntimeError(f"feature does not support start: {feature_id}")
        method(payload)
        self._log("running", f"已启动功能 {feature_id}")
        return self.snapshot()

    def stop(self, payload: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        feature_id, adapter = self._resolve_feature_adapter(
            payload or {},
            allow_running_unselected=True,
        )
        method = getattr(adapter, "stop", None)
        if not callable(method):
            raise RuntimeError(f"feature does not support stop: {feature_id}")
        method()
        self._log("stopping", f"已请求停止功能 {feature_id}")
        return self.snapshot()

    def snapshot(self) -> Dict[str, object]:
        runtime = probe_image_runtime()
        adapters = self._adapter_snapshots()
        projects = self._projects_with_adapter_state(adapters)
        with self._lock:
            demo_running = self._running
            status = "running" if demo_running else self._status
            if not runtime["available"] and not demo_running and status == "ready":
                status = "unavailable"
            result = dict(self._last_result) if self._last_result is not None else None
            logs = list(self._logs)
            error = self._last_error
            last_run_at = self._last_run_at
            revision = self._revision
            selected_project_ids = list(self._selected_project_ids)
            selected_feature_ids = list(self._selected_feature_ids)
            selection_saved_at = self._selection_saved_at
        execution = self._execution_summary(
            selected_project_ids,
            selected_feature_ids,
            adapters,
        )
        adapter_running = bool(execution["running_feature_ids"])
        running = demo_running or adapter_running
        if adapter_running:
            status = "running"
        evidence = {
            kind: f"/api/open-source-automation/evidence/{kind}?revision={revision}"
            for kind in ("frame", "template", "overlay")
        } if result is not None else {}
        if result is not None:
            result["evidence"] = evidence
        return {
            "status": status,
            "running": running,
            "catalog_version": 3,
            "projects": projects,
            "selection": {
                "schema_version": 2,
                "projects": self._selection_projects(
                    selected_project_ids,
                    selected_feature_ids,
                ),
                "project_ids": selected_project_ids,
                "feature_ids": selected_feature_ids,
                "saved_at": selection_saved_at,
            },
            "execution": execution,
            "adapters": adapters,
            "dependency": {
                **runtime,
                "install_command": 'python -m pip install -e ".[image]"',
                "estimated_additional_bytes": IMAGE_EXTRA_ESTIMATED_BYTES,
                "estimated_additional_mib": round(
                    IMAGE_EXTRA_ESTIMATED_BYTES / 1024 / 1024,
                    1,
                ),
            },
            "bundle": self._bundle_summary(),
            "alignment": self._alignment(),
            "demo": {
                "default_iterations": DEFAULT_DEMO_ITERATIONS,
                "max_iterations": MAX_DEMO_ITERATIONS,
                "last_run_at": last_run_at,
                "error": error,
                "result": result,
            },
            "logs": logs,
            "boundary": {
                "typed_actions": True,
                "arbitrary_shell": False,
                "eval": False,
                "scope": "host_controls_only",
                "external_runtime": bool(adapters),
                "trusted_upstream_code": bool(adapters),
                "external_runtime_policy": "upstream_process_permissions",
                "device_gateway": any(
                    adapter.get("available") is True
                    for adapter in adapters.values()
                ),
                "device_gateway_detail": (
                    "已接入受验证的外部运行时；第三方原生代码按其自身权限运行"
                    if any(
                        adapter.get("available") is True
                        for adapter in adapters.values()
                    )
                    else "外部项目运行时尚未安装"
                ),
            },
        }

    def run_demo(self, payload: Dict[str, object]) -> Dict[str, object]:
        raw_iterations = payload.get("iterations", DEFAULT_DEMO_ITERATIONS)
        try:
            iterations = int(raw_iterations)
        except (TypeError, ValueError) as exc:
            raise ValueError("iterations must be an integer") from exc
        if iterations < 1 or iterations > MAX_DEMO_ITERATIONS:
            raise ValueError(f"iterations must be within 1..{MAX_DEMO_ITERATIONS}")
        runtime = probe_image_runtime()
        if not runtime["available"]:
            raise RuntimeError(
                f"{runtime['detail']}。请先运行：python -m pip install -e \".[image]\""
            )
        with self._lock:
            if self._running:
                raise RuntimeError("合成验证正在运行")
            self._running = True
            self._status = "running"
            self._last_error = ""
        self._log("running", f"开始 {iterations} 次缓存与新截图模板匹配")
        started = time.perf_counter()
        try:
            result = run_synthetic_visual_demo(self.output_root, iterations)
            result["duration_s"] = time.perf_counter() - started
            revision = str(time.time_ns())
            result["revision"] = revision
            result["completed_at"] = time.time()
            result_path = self.output_root / "result.json"
            _atomic_write(
                result_path,
                json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except Exception as exc:
            with self._lock:
                self._running = False
                self._status = "error"
                self._last_error = str(exc)
            self._log("error", str(exc))
            raise
        with self._lock:
            self._running = False
            self._status = "completed" if result.get("matched") else "error"
            self._last_result = dict(result)
            self._last_run_at = float(result["completed_at"])
            self._revision = revision
        self._log(
            "completed",
            (
                f"模板分数 {float(result['score']):.4f}，坐标"
                f"{'精确命中' if result.get('coordinate_exact') else '存在偏差'}，"
                f"P95 {float(result['p95_ms']):.2f} ms"
            ),
        )
        return self.snapshot()

    def latest_evidence(self, kind: str) -> Optional[bytes]:
        filenames = {
            "frame": "frame.png",
            "template": "template.png",
            "overlay": "match-overlay.png",
        }
        filename = filenames.get(str(kind or "").strip().lower())
        if filename is None:
            return None
        path = self.output_root / filename
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def close(self) -> None:
        for adapter in self._feature_adapters.values():
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
