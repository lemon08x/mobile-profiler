#!/usr/bin/env python3
"""Validate and triage the reproducible MAA adaptation workflow."""

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
    audit_maa_source,
    compare_resource_file,
    compare_worktree_patch,
    load_guard_policy,
    promote_incident_fixture,
    read_json_object,
    sync_worktree_patch,
    triage_run_incidents,
    validate_fixture_manifest,
    validate_issue_ledger,
)


DEFAULT_LEDGER = REPOSITORY_ROOT / "integrations" / "maa" / "issues.json"
DEFAULT_FIXTURES = REPOSITORY_ROOT / "integrations" / "maa" / "fixtures" / "manifest.json"
DEFAULT_POLICY = REPOSITORY_ROOT / "integrations" / "maa" / "guard-policy.json"
DEFAULT_PATCH = REPOSITORY_ROOT / "integrations" / "maa" / "patches" / "v6.14.2-viewport-transform.patch"
EXPECTED_MAA_HEAD = "2b44185c615d81bc39454933cd4649536c23f4f3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate the issue ledger, fixtures and guard policy")
    validate.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    validate.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    validate.add_argument("--policy", type=Path, default=DEFAULT_POLICY)

    triage = commands.add_parser("triage", help="deduplicate a run's incidents into the issue ledger")
    triage.add_argument("--run-dir", type=Path, required=True)
    triage.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)

    promote = commands.add_parser("promote-fixture", help="promote an incident screenshot to a regression fixture")
    promote.add_argument("--incident", type=Path, required=True)
    promote.add_argument("--manifest", type=Path, default=DEFAULT_FIXTURES)
    promote.add_argument("--id", required=True)
    promote.add_argument("--page", required=True)
    promote.add_argument("--expected-task", required=True)
    promote.add_argument("--alignment", choices=("left", "center", "right"), required=True)
    promote.add_argument("--must-not-match", action="append", default=[])

    resource = commands.add_parser("resource-check", help="compare a source resource with the downloaded runtime")
    resource.add_argument("--source-root", type=Path, required=True)
    resource.add_argument("--runtime-root", type=Path, required=True)
    resource.add_argument(
        "--relative",
        type=Path,
        default=Path("resource/tasks/Roguelike/JieGarden.json"),
    )

    patch = commands.add_parser("patch-check", help="verify that the checked-in patch equals the MAA worktree diff")
    patch.add_argument("--source-root", type=Path, required=True)
    patch.add_argument("--patch", type=Path, default=DEFAULT_PATCH)

    patch_sync = commands.add_parser("patch-sync", help="replace the patch with a verified baseline worktree diff")
    patch_sync.add_argument("--source-root", type=Path, required=True)
    patch_sync.add_argument("--patch", type=Path, default=DEFAULT_PATCH)
    patch_sync.add_argument("--expected-head", default=EXPECTED_MAA_HEAD)

    audit = commands.add_parser("source-audit", help="check coordinate, adaptive and JieGarden invariants")
    audit.add_argument("--source-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "validate":
        ledger_path = args.ledger.expanduser().resolve()
        fixtures_path = args.fixtures.expanduser().resolve()
        policy_path = args.policy.expanduser().resolve()
        ledger_errors = validate_issue_ledger(read_json_object(ledger_path))
        fixture_errors = validate_fixture_manifest(read_json_object(fixtures_path), fixtures_path)
        policy_error = ""
        try:
            load_guard_policy(policy_path)
        except ValueError as exc:
            policy_error = str(exc)
        result = {
            "valid": not ledger_errors and not fixture_errors and not policy_error,
            "ledger": {"path": str(ledger_path), "errors": ledger_errors},
            "fixtures": {"path": str(fixtures_path), "errors": fixture_errors},
            "policy": {"path": str(policy_path), "errors": [policy_error] if policy_error else []},
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2

    if args.command == "triage":
        result = triage_run_incidents(
            args.run_dir.expanduser().resolve(),
            args.ledger.expanduser().resolve(),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "promote-fixture":
        result = promote_incident_fixture(
            args.incident.expanduser().resolve(),
            args.manifest.expanduser().resolve(),
            fixture_id=args.id,
            page=args.page,
            expected_task=args.expected_task,
            expected_alignment=args.alignment,
            forbidden_tasks=args.must_not_match,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "resource-check":
        result = compare_resource_file(
            args.source_root.expanduser().resolve(),
            args.runtime_root.expanduser().resolve(),
            args.relative,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["matches"] else 2

    if args.command == "patch-check":
        result = compare_worktree_patch(
            args.source_root.expanduser().resolve(),
            args.patch.expanduser().resolve(),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["matches"] else 2

    if args.command == "patch-sync":
        result = sync_worktree_patch(
            args.source_root.expanduser().resolve(),
            args.patch.expanduser().resolve(),
            expected_head=args.expected_head,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["matches"] else 2

    if args.command == "source-audit":
        result = audit_maa_source(args.source_root.expanduser().resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
