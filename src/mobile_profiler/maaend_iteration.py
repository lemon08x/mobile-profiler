"""Reproducibility and source-invariant checks for the MaaEnd viewport port."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from .maa_iteration import promote_incident_fixture
from .maaend_guard import validate_maaend_guard_policy


def validate_maaend_policy_copies(
    integration_path: Path,
    packaged_path: Path,
) -> dict[str, object]:
    """Validate both installed policy sources and require byte identity."""

    rows: dict[str, dict[str, object]] = {}
    payloads: dict[str, bytes] = {}
    failures: list[str] = []
    for name, raw_path in (
        ("integration", integration_path),
        ("packaged", packaged_path),
    ):
        path = raw_path.expanduser().resolve()
        errors: list[str] = []
        try:
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, Mapping):
                errors.append("policy root must be an object")
            else:
                errors.extend(validate_maaend_guard_policy(value))
            payloads[name] = payload
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
        rows[name] = {
            "path": os.fspath(path),
            "sha256": hashlib.sha256(payloads.get(name, b"")).hexdigest()
            if name in payloads
            else "",
            "errors": errors,
        }
        failures.extend(f"{name}: {error}" for error in errors)

    byte_identical = (
        "integration" in payloads
        and "packaged" in payloads
        and payloads["integration"] == payloads["packaged"]
    )
    if not byte_identical:
        failures.append("integration and packaged guard policies are not byte-identical")
    return {
        "valid": not failures,
        "byte_identical": byte_identical,
        "integration": rows["integration"],
        "packaged": rows["packaged"],
        "failures": failures,
    }


def promote_maaend_incident_fixture(
    incident_path: Path,
    manifest_path: Path,
    *,
    fixture_id: str,
    page: str,
    expected_task: str,
    expected_alignment: str,
    forbidden_tasks: Iterable[str] = (),
    confirmed_redacted: bool,
    redaction_note: str = "",
) -> dict[str, object]:
    """Promote local evidence only after an explicit privacy review."""

    if not confirmed_redacted:
        raise ValueError(
            "MaaEnd fixture promotion requires explicit confirmation that the "
            "screenshot and incident metadata were manually redacted"
        )
    return promote_incident_fixture(
        incident_path,
        manifest_path,
        fixture_id=fixture_id,
        page=page,
        expected_task=expected_task,
        expected_alignment=expected_alignment,
        forbidden_tasks=forbidden_tasks,
        privacy_review={
            "redaction_confirmed": True,
            "confirmed_at": datetime.now().astimezone().isoformat(),
            "note": redaction_note,
        },
    )


def _read_required(root: Path, relative: str, failures: list[str]) -> str:
    path = root / relative
    if not path.is_file():
        failures.append(f"required source file is missing: {relative}")
        return ""
    return path.read_text(encoding="utf-8")


def _require_markers(
    text: str,
    relative: str,
    markers: dict[str, str],
    failures: list[str],
) -> None:
    for marker, message in markers.items():
        if marker not in text:
            failures.append(f"{relative}: {message}")


def audit_maaframework_source(source_root: Path) -> dict[str, object]:
    root = source_root.expanduser().resolve()
    failures: list[str] = []
    controller_name = "source/MaaFramework/Controller/ControllerAgent.cpp"
    pipeline_name = "source/MaaFramework/Task/PipelineTask.cpp"
    result_name = "source/include/Common/TaskResultTypes.h"
    transform_name = "source/MaaFramework/Controller/ViewportTransform.cpp"
    screencap_name = "source/MaaAdbControlUnit/Manager/ScreencapAgent.cpp"
    failover_name = "source/MaaAdbControlUnit/Manager/ScreencapFailoverPolicy.cpp"
    controller = _read_required(root, controller_name, failures)
    pipeline = _read_required(root, pipeline_name, failures)
    result_types = _read_required(root, result_name, failures)
    transform = _read_required(root, transform_name, failures)
    screencap = _read_required(root, screencap_name, failures)
    failover = _read_required(root, failover_name, failures)

    _require_markers(
        controller,
        controller_name,
        {
            '"MAA_SCREENSHOT_VIEWPORT"': "viewport environment contract is missing",
            "viewport_info[\"raw_width\"]": "controller info no longer reports raw width",
            "viewport_info[\"logical_width\"]": "controller info no longer reports logical width",
            "viewport_transform_->logical_to_display(p, alignment)": (
                "coordinate-bearing input no longer maps logical to display"
            ),
            "action.viewport_alignment = viewport_alignment_": (
                "queued actions no longer snapshot viewport alignment"
            ),
            "contact_alignments_[param.contact] = alignment": (
                "touch contacts no longer pin their down alignment"
            ),
            "MaaCtrlId ControllerAgent::post_focused(Action action)": (
                "focused controller actions no longer register callbacks atomically"
            ),
            "std::unique_lock lock { focus_ids_mutex_ };\n    const MaaCtrlId id = post(std::move(action));": (
                "controller action callback registration race is no longer locked"
            ),
            "segment.end_hold": "segmented MultiSwipe no longer applies end_hold",
            "cv::resize(raw(viewport), result, viewport_transform_->logical_size()": (
                "recognition screenshot no longer crops then resizes the active viewport"
            ),
        },
        failures,
    )
    _require_markers(
        pipeline,
        pipeline_name,
        {
            "ctrl->viewport_images()": (
                "adaptive recognition candidates are no longer sourced from the controller"
            ),
            "select_direct_hit_action_alignment": (
                "DirectHit coordinate actions no longer select an adaptive alignment"
            ),
            "case Type::MultiSwipe": "MultiSwipe is missing from DirectHit alignment inference",
            "case Type::TouchDown": "touch input is missing from DirectHit alignment inference",
            "result.viewport_alignment =": (
                "recognition results no longer expose their selected viewport alignment"
            ),
            "result.frame_id = candidate.frame_id": (
                "recognition results no longer expose their raw frame identity"
            ),
        },
        failures,
    )
    _require_markers(
        result_types,
        result_name,
        {
            "std::string viewport_alignment": (
                "recognition callback result no longer carries viewport alignment"
            ),
            "uint64_t frame_id = 0": (
                "recognition callback result no longer carries raw frame identity"
            ),
            "MEO_TOJSON(reco_id, name, algorithm, box, viewport_alignment, frame_id, detail)": (
                "recognition viewport evidence is no longer serialized"
            ),
        },
        failures,
    )
    _require_markers(
        transform,
        transform_name,
        {
            "ViewportMode::Adaptive": "adaptive viewport mode is missing",
            "logical_to_display": "logical-to-display transform is missing",
            "candidate_alignments": "left/center/right viewport candidates are missing",
        },
        failures,
    )
    _require_markers(
        screencap,
        screencap_name,
        {
            "ranked_units_ = speed_test()": "initialized screenshot backends are not retained",
            "failover_policy_.try_candidates": "runtime screenshot failover is missing",
            "ranked.unit->on_app_started(intent)": "app-start events are not broadcast to retained backends",
            "ranked.unit->on_image_resolution_changed(pre, cur)": (
                "resolution changes are not broadcast to retained backends"
            ),
        },
        failures,
    )
    _require_markers(
        failover,
        failover_name,
        {
            "std::min<uint32_t>(state.consecutive_failures - 1, 5)": (
                "screenshot failure backoff is no longer capped at 32 frames"
            ),
            "active_candidate_ = candidate": "successful fallback does not become active",
        },
        failures,
    )
    return {
        "valid": not failures,
        "source_root": os.fspath(root),
        "failures": failures,
    }


def audit_maaend_agent_source(source_root: Path) -> dict[str, object]:
    root = source_root.expanduser().resolve()
    failures: list[str] = []
    checker_name = "agent/go-service/taskersink/aspectratio/checker.go"
    register_name = "agent/go-service/taskersink/aspectratio/register.go"
    go_viewport_name = "agent/go-service/pkg/viewport/session.go"
    capture_uid_name = "agent/go-service/captureuid/capture.go"
    viewport_probe_name = "agent/go-service/viewportprobe/input.go"
    service_register_name = "agent/go-service/register.go"
    cpp_viewport_name = "agent/cpp-algo/source/Viewport/ViewportSession.cpp"
    checker = _read_required(root, checker_name, failures)
    register = _read_required(root, register_name, failures)
    go_viewport = _read_required(root, go_viewport_name, failures)
    capture_uid = _read_required(root, capture_uid_name, failures)
    viewport_probe = _read_required(root, viewport_probe_name, failures)
    service_register = _read_required(root, service_register_name, failures)
    cpp_viewport = _read_required(root, cpp_viewport_name, failures)
    _require_markers(
        checker,
        checker_name,
        {
            'return entry == androidOpenGameEntry': (
                "portrait transition is no longer restricted to exact AndroidOpenGame entry"
            ),
            "!viewport.Configured || viewport.Active": (
                "portrait entry no longer requires configured but inactive viewport"
            ),
            'viewport.Mode != "adaptive"': "portrait entry no longer requires adaptive mode",
            "viewport.RawWidth != int(width) || viewport.RawHeight != int(height)": (
                "controller and viewport raw resolutions are no longer identity-checked"
            ),
            "validateAndroidOpenGameLaunchChain": "coordinate-free launch chain validation is missing",
            "maa.RecognitionTypeDirectHit": "launch-chain DirectHit requirement is missing",
            "maa.ActionTypeStartApp": "launch-chain StartApp requirement is missing",
            "waitForCompatibleScreenshotViewport": "landscape activation gate is missing",
            "portraitTransitionWait  = 20 * time.Second": "landscape gate timeout drifted",
            "controller.PostScreencap().Wait()": "landscape gate no longer refreshes screenshots",
            "transitions  map[uint64]portraitTransition": (
                "portrait transition state is no longer isolated by task ID"
            ),
            "transitionMu sync.Mutex": "portrait transition state is no longer synchronized",
            "portrait Android launch transition failed closed": "fail-closed stop path is missing",
        },
        failures,
    )
    _require_markers(
        go_viewport,
        go_viewport_name,
        {
            "type Frame struct": "Go viewport frame contract is missing",
            "Alignment maa.ViewportAlignment": "Go frame no longer carries alignment",
            "FrameID   uint64": "Go frame no longer carries frame ID",
            "func (s *Session) CaptureCandidates": (
                "Go candidate recognition no longer shares one raw frame"
            ),
            "controllerSessionLock.Lock()": (
                "Go alignment and controller actions are no longer serialized"
            ),
            "actionErr := s.backend.touchUp(contact)": (
                "Go TouchUp no longer releases after alignment selection failure"
            ),
        },
        failures,
    )
    _require_markers(
        cpp_viewport,
        cpp_viewport_name,
        {
            "ViewportSession::Capture": "C++ viewport capture wrapper is missing",
            "ViewportSession::TouchDown": "C++ touch wrapper is missing",
            "ViewportSession::TouchUp": "C++ touch release wrapper is missing",
            "const bool selected =": (
                "C++ TouchUp may skip release when contact alignment is unavailable"
            ),
            "MaaControllerPostTouchUp(controller_, contact)": (
                "C++ wrapper no longer posts contact release"
            ),
        },
        failures,
    )
    _require_markers(
        capture_uid,
        capture_uid_name,
        {
            "session.CaptureCandidates(context.Background(), true)": (
                "CaptureUid no longer evaluates one raw frame across candidate viewports"
            ),
            "maa.Rect{0, 660, 420, 60}": (
                "CaptureUid safe-area OCR strip drifted"
            ),
            "uidLabelRe.FindAllStringSubmatch": (
                "CaptureUid no longer separates the labelled UID from unrelated digits"
            ),
            'Str("alignment", viewportAlignmentName(frame.Alignment))': (
                "CaptureUid no longer reports recognition alignment evidence"
            ),
            'Uint64("frame_id", frame.FrameID)': (
                "CaptureUid no longer reports raw frame evidence"
            ),
        },
        failures,
    )
    if "text=%q" in capture_uid:
        failures.append(
            f"{capture_uid_name}: CaptureUid error paths may leak raw OCR text"
        )
    _require_markers(
        viewport_probe,
        viewport_probe_name,
        {
            "session.PreferredLabel(ctx, maa.ViewportAlignmentLeft)": (
                "input probe no longer fixes the joystick to Left"
            ),
            "session.PreferredLabel(ctx, maa.ViewportAlignmentRight)": (
                "input probe no longer fixes the camera to Right"
            ),
            "cameraX+cameraDeltaX": "input probe no longer performs its bounded camera move",
            "session.TouchMove(ctx, right, cameraContact, cameraX, cameraY, 0)": (
                "input probe no longer reverses the camera move"
            ),
            "session.ReleaseTouches(context.Background())": (
                "input probe cancellation/panic cleanup is missing"
            ),
            'Bool("reversed", true)': "input probe no longer reports reversal evidence",
        },
        failures,
    )
    _require_markers(
        service_register,
        service_register_name,
        {
            '"github.com/MaaXYZ/MaaEnd/agent/go-service/viewportprobe"': (
                "ViewportInputProbe package is not linked into the Go Agent"
            ),
            "viewportprobe.Register()": "ViewportInputProbe action is not registered",
        },
        failures,
    )

    go_allowed = {
        Path("agent/go-service/pkg/viewport/session.go"),
        Path("agent/go-service/pkg/control/adaptor_desktop.go"),
    }
    cpp_allowed = {
        Path("agent/cpp-algo/source/Viewport/ViewportSession.cpp"),
        Path(
            "agent/cpp-algo/source/MapNavigator/Backend/Desktop/"
            "desktop_input_backend.cpp"
        ),
    }
    go_forbidden = re.compile(
        r"\.(?:CacheImage|Post(?:Click|Swipe(?:V2)?|TouchDown|TouchMove|TouchUp|Scroll))\s*\("
    )
    cpp_forbidden = re.compile(
        r"\bMaaController(?:CachedImage|Post(?:Click|Swipe|TouchDown|TouchMove|TouchUp|Scroll))\s*\("
    )
    adb_input = re.compile(r"\binput\s+(?:tap|swipe|motionevent)\b", re.IGNORECASE)
    scan_roots = (
        (root / "agent" / "go-service", {".go"}),
        (
            root / "agent" / "cpp-algo" / "source",
            {".cpp", ".cc", ".cxx", ".h", ".hpp"},
        ),
    )
    for scan_root, suffixes in scan_roots:
        if not scan_root.is_dir():
            continue
        for path in sorted(scan_root.rglob("*")):
            if path.suffix.lower() not in suffixes:
                continue
            relative = path.relative_to(root)
            text = path.read_text(encoding="utf-8", errors="replace")
            matcher = go_forbidden if path.suffix.lower() == ".go" else cpp_forbidden
            allowed = go_allowed if path.suffix.lower() == ".go" else cpp_allowed
            for line_number, line in enumerate(text.splitlines(), start=1):
                if relative not in allowed and matcher.search(line):
                    failures.append(
                        f"{relative.as_posix()}:{line_number}: direct controller screenshot/input bypasses ViewportSession"
                    )
                if adb_input.search(line):
                    failures.append(
                        f"{relative.as_posix()}:{line_number}: direct Android input command is forbidden"
                    )
    _require_markers(
        register,
        register_name,
        {
            "checker := &AspectRatioChecker{}": "a shared checker instance is not constructed",
            "maa.AgentServerAddTaskerSink(checker)": "Tasker sink registration is missing",
            "maa.AgentServerAddContextSink(checker)": "Context sink registration is missing",
        },
        failures,
    )
    return {
        "valid": not failures,
        "source_root": os.fspath(root),
        "failures": failures,
    }


def audit_maa_framework_go_source(source_root: Path) -> dict[str, object]:
    root = source_root.expanduser().resolve()
    failures: list[str] = []
    controller_name = "controller.go"
    native_name = "internal/native/framework.go"
    event_name = "event.go"
    controller = _read_required(root, controller_name, failures)
    native = _read_required(root, native_name, failures)
    event = _read_required(root, event_name, failures)
    _require_markers(
        controller,
        controller_name,
        {
            "type ViewportAlignment int32": "public Go viewport alignment type is missing",
            "ViewportAlignmentCenter ViewportAlignment = 0": "Center enum value drifted",
            "ViewportAlignmentLeft   ViewportAlignment = 1": "Left enum value drifted",
            "ViewportAlignmentRight  ViewportAlignment = 2": "Right enum value drifted",
            "func (c *Controller) SetScreenshotViewportAlignment": (
                "Go controller alignment setter is missing"
            ),
        },
        failures,
    )
    _require_markers(
        native,
        native_name,
        {
            "MaaCtrlOption_ScreenshotViewportAlignment MaaCtrlOption = 8": (
                "Go native option 8 contract drifted"
            )
        },
        failures,
    )
    _require_markers(
        event,
        event_name,
        {
            "Viewport ControllerActionViewportDetail": (
                "controller callbacks no longer expose viewport evidence"
            ),
            'FrameID               uint64            `json:"frame_id"`': (
                "controller callback frame ID is missing"
            ),
            "type NodeRecognitionResultDetail struct": (
                "node recognition callback detail type is missing"
            ),
            'ViewportAlignment string `json:"viewport_alignment"`': (
                "node recognition callbacks no longer expose viewport alignment"
            ),
            'RecognitionDetails *NodeRecognitionResultDetail `json:"reco_details"`': (
                "node callbacks no longer expose typed recognition details"
            ),
        },
        failures,
    )
    recognition_detail_fields = event.count(
        'RecognitionDetails *NodeRecognitionResultDetail `json:"reco_details"`'
    )
    if recognition_detail_fields < 4:
        failures.append(
            f"{event_name}: typed recognition details are missing from one or more node callbacks"
        )
    return {
        "valid": not failures,
        "source_root": os.fspath(root),
        "failures": failures,
    }
