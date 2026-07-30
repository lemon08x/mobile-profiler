#!/usr/bin/env python3
"""Validate and synchronize the reproducible MaaEnd viewport adaptation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mobile_profiler.maa_iteration import (  # noqa: E402
    compare_worktree_patch,
    read_json_object,
    sync_worktree_patch,
    triage_run_incidents,
    validate_fixture_manifest,
    validate_issue_ledger,
)
from mobile_profiler.maaend_iteration import (  # noqa: E402
    audit_maaend_agent_source,
    audit_maaframework_source,
    audit_maa_framework_go_source,
    promote_maaend_incident_fixture,
    validate_maaend_policy_copies,
)


INTEGRATION_ROOT = REPOSITORY_ROOT / "integrations" / "maaend"
DEFAULT_LEDGER = INTEGRATION_ROOT / "issues.json"
DEFAULT_FIXTURES = INTEGRATION_ROOT / "fixtures" / "manifest.json"
DEFAULT_GUARD_POLICY = INTEGRATION_ROOT / "guard-policy.json"
DEFAULT_PACKAGED_GUARD_POLICY = (
    REPOSITORY_ROOT / "src" / "mobile_profiler" / "maaend_guard_policy.json"
)
FRAMEWORK_PATCH = INTEGRATION_ROOT / "patches" / "maaframework-v5.12.1-viewport.patch"
AGENT_PATCH = INTEGRATION_ROOT / "patches" / "maaend-v2.20.0-viewport-gate.patch"
GO_BINDING_PATCH = (
    INTEGRATION_ROOT
    / "patches"
    / "maa-framework-go-v4.0.0-beta.17-viewport.patch"
)
EXPECTED_FRAMEWORK_HEAD = "e6aa89259ff6907197becebdeb8efc0074f13dfc"
EXPECTED_AGENT_HEAD = "023e995a37898750d20052f40262c3f632348a76"
EXPECTED_GO_BINDING_HEAD = "2b674ef2aeac62051c201945319802f14d5e5b3e"
FRAMEWORK_UNTRACKED = (
    Path("source/MaaAdbControlUnit/Manager/ScreencapFailoverPolicy.cpp"),
    Path("source/MaaAdbControlUnit/Manager/ScreencapFailoverPolicy.h"),
    Path("source/MaaFramework/Controller/ViewportTransform.cpp"),
    Path("source/MaaFramework/Controller/ViewportTransform.h"),
    Path("test/screencap/CMakeLists.txt"),
    Path("test/screencap/main.cpp"),
    Path("test/viewport/CMakeLists.txt"),
    Path("test/viewport/controller.cpp"),
    Path("test/viewport/main.cpp"),
)
AGENT_UNTRACKED = (
    Path("agent/cpp-algo/source/MapLocator/MinimapExtractor.cpp"),
    Path("agent/cpp-algo/source/MapLocator/MinimapExtractor.h"),
    Path("agent/cpp-algo/source/Viewport/ViewportSession.cpp"),
    Path("agent/cpp-algo/source/Viewport/ViewportSession.h"),
    Path("agent/go-service/pkg/viewport/session.go"),
    Path("agent/go-service/pkg/viewport/session_test.go"),
    Path("agent/go-service/captureuid/capture_test.go"),
    Path("agent/go-service/common/autoalt/click_test.go"),
    Path("agent/go-service/maptracker/default/infer_viewport_test.go"),
    Path("agent/go-service/maptracker/bigmap/pick_viewport_test.go"),
    Path("agent/go-service/pkg/control/adaptor_adb_test.go"),
    Path("agent/go-service/taskersink/aspectratio/checker_viewport_test.go"),
    Path("agent/go-service/viewportprobe/input.go"),
    Path("agent/go-service/viewportprobe/input_test.go"),
    Path("agent/go-service/viewportprobe/register.go"),
    Path("agent/go-service/visitfriends/visitfriends_viewport_test.go"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    validate.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    validate.add_argument("--guard-policy", type=Path, default=DEFAULT_GUARD_POLICY)
    validate.add_argument(
        "--packaged-guard-policy",
        type=Path,
        default=DEFAULT_PACKAGED_GUARD_POLICY,
    )

    triage = commands.add_parser("triage")
    triage.add_argument("--run-dir", type=Path, required=True)
    triage.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)

    promote = commands.add_parser("promote-fixture")
    promote.add_argument("--incident", type=Path, required=True)
    promote.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    promote.add_argument("--id", dest="fixture_id", required=True)
    promote.add_argument("--page", required=True)
    promote.add_argument("--task", required=True)
    promote.add_argument(
        "--alignment",
        choices=("left", "center", "right"),
        required=True,
    )
    promote.add_argument("--forbidden-task", action="append", default=[])
    promote.add_argument("--redaction-note", default="")
    promote.add_argument(
        "--confirmed-redacted",
        action="store_true",
        help="confirm that account data and screenshots were manually redacted",
    )

    audit = commands.add_parser("source-audit")
    audit.add_argument("--framework-source", type=Path, required=True)
    audit.add_argument("--maaend-source", type=Path, required=True)
    audit.add_argument("--go-binding-source", type=Path, required=True)

    check = commands.add_parser("patch-check")
    check.add_argument("--framework-source", type=Path, required=True)
    check.add_argument("--maaend-source", type=Path, required=True)
    check.add_argument("--go-binding-source", type=Path, required=True)

    sync = commands.add_parser("patch-sync")
    sync.add_argument("--framework-source", type=Path, required=True)
    sync.add_argument("--maaend-source", type=Path, required=True)
    sync.add_argument("--go-binding-source", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "validate":
        ledger = args.ledger.expanduser().resolve()
        fixtures = args.fixtures.expanduser().resolve()
        ledger_errors = validate_issue_ledger(read_json_object(ledger))
        fixture_errors = validate_fixture_manifest(read_json_object(fixtures), fixtures)
        guard = validate_maaend_policy_copies(
            args.guard_policy,
            args.packaged_guard_policy,
        )
        result = {
            "valid": not ledger_errors and not fixture_errors and guard["valid"] is True,
            "ledger": {"path": str(ledger), "errors": ledger_errors},
            "fixtures": {"path": str(fixtures), "errors": fixture_errors},
            "guard_policy": guard,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2

    if args.command == "triage":
        run_dir = args.run_dir.expanduser().resolve()
        ledger = args.ledger.expanduser().resolve()
        result = triage_run_incidents(run_dir, ledger)
        print(
            json.dumps(
                {"valid": True, "run_dir": str(run_dir), "ledger": str(ledger), **result},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "promote-fixture":
        try:
            entry = promote_maaend_incident_fixture(
                args.incident.expanduser().resolve(),
                args.fixtures.expanduser().resolve(),
                fixture_id=args.fixture_id,
                page=args.page,
                expected_task=args.task,
                expected_alignment=args.alignment,
                forbidden_tasks=args.forbidden_task,
                confirmed_redacted=args.confirmed_redacted,
                redaction_note=args.redaction_note,
            )
        except (OSError, ValueError) as exc:
            print(
                json.dumps(
                    {"valid": False, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(json.dumps({"valid": True, "fixture": entry}, ensure_ascii=False, indent=2))
        return 0

    framework_source = args.framework_source.expanduser().resolve()
    maaend_source = args.maaend_source.expanduser().resolve()
    go_binding_source = args.go_binding_source.expanduser().resolve()
    if args.command == "source-audit":
        framework = audit_maaframework_source(framework_source)
        agent = audit_maaend_agent_source(maaend_source)
        go_binding = audit_maa_framework_go_source(go_binding_source)
        result = {
            "valid": (
                framework["valid"] is True
                and agent["valid"] is True
                and go_binding["valid"] is True
            ),
            "maaframework": framework,
            "maaend_agent": agent,
            "maa_framework_go": go_binding,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2

    if args.command == "patch-check":
        framework = compare_worktree_patch(
            framework_source,
            FRAMEWORK_PATCH,
            include_untracked=FRAMEWORK_UNTRACKED,
        )
        agent = compare_worktree_patch(
            maaend_source,
            AGENT_PATCH,
            include_untracked=AGENT_UNTRACKED,
        )
        go_binding = compare_worktree_patch(
            go_binding_source,
            GO_BINDING_PATCH,
        )
    elif args.command == "patch-sync":
        framework = sync_worktree_patch(
            framework_source,
            FRAMEWORK_PATCH,
            expected_head=EXPECTED_FRAMEWORK_HEAD,
            include_untracked=FRAMEWORK_UNTRACKED,
        )
        agent = sync_worktree_patch(
            maaend_source,
            AGENT_PATCH,
            expected_head=EXPECTED_AGENT_HEAD,
            include_untracked=AGENT_UNTRACKED,
        )
        go_binding = sync_worktree_patch(
            go_binding_source,
            GO_BINDING_PATCH,
            expected_head=EXPECTED_GO_BINDING_HEAD,
        )
    else:
        raise AssertionError(f"unhandled command: {args.command}")
    result = {
        "valid": (
            framework["matches"] is True
            and agent["matches"] is True
            and go_binding["matches"] is True
        ),
        "maaframework": framework,
        "maaend_agent": agent,
        "maa_framework_go": go_binding,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
