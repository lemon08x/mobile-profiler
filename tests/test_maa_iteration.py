from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mobile_profiler.maa_iteration import (
    CallbackMonitor,
    IncidentSignal,
    classify_exit,
    compare_resource_file,
    compare_worktree_patch,
    prepare_maa_feature_probe,
    promote_incident_fixture,
    read_json_object,
    record_incident_bundle,
    summarize_feature_extra,
    triage_run_incidents,
    validate_fixture_manifest,
    validate_issue_ledger,
    write_guard_overlay,
    write_json_atomic,
    sync_worktree_patch,
)


def policy(**watchdog_overrides: object) -> dict[str, object]:
    watchdogs: dict[str, object] = {
        "no_progress_seconds": 240,
        "same_task_start_limit": 12,
        "static_snapshot_limit": 5,
        "slow_screenshot_seconds": 2.5,
        "stop_on_slow_screenshot": False,
    }
    watchdogs.update(watchdog_overrides)
    return {
        "schema_version": 1,
        "stop_after_settlement": True,
        "destructive_tasks": [
            {
                "resource_task": "Roguelike@ExitThenAbandon",
                "callback_suffix": "ExitThenAbandon",
                "reason": "guard it",
            }
        ],
        "watchdogs": watchdogs,
    }


class MaaIterationTests(unittest.TestCase):
    def test_feature_probe_allowlist_and_account_mutation_guard(self) -> None:
        depot = prepare_maa_feature_probe("Depot")
        self.assertEqual(depot["risk"], "read_only")
        self.assertEqual(depot["params"], {"enable": True})

        with self.assertRaisesRegex(ValueError, "allow-account-mutation"):
            prepare_maa_feature_probe("Award")
        award = prepare_maa_feature_probe("Award", allow_account_mutation=True)
        self.assertTrue(award["params"]["award"])
        self.assertFalse(award["params"]["mail"])

        with self.assertRaisesRegex(ValueError, "unsupported feature probe"):
            prepare_maa_feature_probe("Fight")

    def test_feature_extra_summary_uses_documented_operbox_fields(self) -> None:
        summary = summarize_feature_extra(
            {
                "what": "OperBoxInfo",
                "details": {
                    "done": True,
                    "all_opers": [{"id": "one"}, {"id": "two"}],
                    "own_opers": [{"id": "one"}],
                },
            }
        )
        self.assertEqual(
            summary,
            {
                "what": "OperBoxInfo",
                "done": True,
                "all_operators": 2,
                "owned_operators": 1,
            },
        )

    def test_settlement_not_task_chain_completion_is_one_round_terminal(self) -> None:
        monitor = CallbackMonitor(policy(), now=100.0)
        monitor.observe(
            10002,
            "TaskChainCompleted",
            {"taskchain": "StartUp", "taskid": 1},
            now=101.0,
        )
        self.assertIsNone(monitor.settlement)
        self.assertFalse(monitor.stop_requested)

        monitor.observe(
            20003,
            "SubTaskExtraInfo",
            {
                "what": "RoguelikeSettlement",
                "taskchain": "Roguelike",
                "details": {"game_pass": False, "floor": 3},
            },
            now=102.0,
        )
        outcome = classify_exit(monitor)
        self.assertTrue(monitor.stop_requested)
        self.assertEqual(monitor.stop_reason, "natural_settlement_fail")
        self.assertTrue(outcome["one_round_completed"])
        self.assertFalse(outcome["game_pass"])

    def test_destructive_task_is_guarded_by_resource_overlay_and_callback(self) -> None:
        monitor = CallbackMonitor(policy(), now=100.0)
        signals = monitor.observe(
            20001,
            "SubTaskStart",
            {
                "taskchain": "Roguelike",
                "pre_task": "JieGarden@Roguelike@DropsFlag",
                "details": {
                    "task": "JieGarden@Roguelike@ExitThenAbandon",
                    "action": "Stop",
                },
            },
            now=101.0,
        )
        self.assertEqual([signal.kind for signal in signals], ["destructive_task_guarded"])
        self.assertTrue(signals[0].stop)
        self.assertTrue(monitor.stop_requested)

        with tempfile.TemporaryDirectory() as directory:
            _, task_path = write_guard_overlay(Path(directory), policy())
            overlay = read_json_object(task_path)
        self.assertEqual(overlay["Roguelike@ExitThenAbandon"]["action"], "Stop")

    def test_callback_loop_and_static_screen_watchdogs_emit_once(self) -> None:
        monitor = CallbackMonitor(
            policy(same_task_start_limit=3, static_snapshot_limit=3, slow_screenshot_seconds=0),
            now=100.0,
        )
        callback = {
            "taskchain": "Roguelike",
            "subtask": "ProcessTask",
            "details": {"task": "StageTrader", "action": "ClickSelf"},
        }
        self.assertEqual(monitor.observe(20001, "SubTaskStart", callback, now=101.0), [])
        self.assertEqual(monitor.observe(20001, "SubTaskStart", callback, now=102.0), [])
        loop = monitor.observe(20001, "SubTaskStart", callback, now=103.0)
        self.assertEqual([signal.kind for signal in loop], ["callback_loop"])
        self.assertEqual(monitor.observe(20001, "SubTaskStart", callback, now=104.0), [])

        static_monitor = CallbackMonitor(
            policy(static_snapshot_limit=3, slow_screenshot_seconds=0),
            now=100.0,
        )
        self.assertEqual(static_monitor.observe_snapshot(b"same", 0.1), [])
        self.assertEqual(static_monitor.observe_snapshot(b"same", 0.1), [])
        static = static_monitor.observe_snapshot(b"same", 0.1)
        self.assertEqual([signal.kind for signal in static], ["static_screen"])
        self.assertEqual(static_monitor.observe_snapshot(b"same", 0.1), [])

    def test_no_progress_watchdog_ignores_task_chain_completed_as_terminal(self) -> None:
        monitor = CallbackMonitor(policy(no_progress_seconds=10), now=100.0)
        monitor.observe(10002, "TaskChainCompleted", {"taskchain": "StartUp"}, now=101.0)
        signals = monitor.poll(now=111.0)
        self.assertEqual([signal.kind for signal in signals], ["no_progress"])

    def test_incident_bundle_deduplicates_by_fingerprint(self) -> None:
        signal = IncidentSignal.create(
            "static_screen",
            "same frame",
            {"image_sha256": "abc", "limit": 3},
            stop=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            first = record_incident_bundle(
                run_dir,
                signal,
                events_tail=[{"message": "one"}],
                screenshot=b"png-one",
            )
            second = record_incident_bundle(
                run_dir,
                signal,
                events_tail=[{"message": "two"}],
                screenshot=b"png-two",
            )
            index = read_json_object(run_dir / "incidents" / "index.json")
            incident_path = next((run_dir / "incidents").glob("*/incident.json"))
            persisted = read_json_object(incident_path)

        self.assertEqual(first["occurrence_count"], 1)
        self.assertEqual(second["occurrence_count"], 2)
        self.assertEqual(persisted["occurrence_count"], 2)
        self.assertEqual(len(index["incidents"]), 1)

    def test_incident_bundle_copies_named_files_json_logs_and_per_occurrence_environment(self) -> None:
        signal = IncidentSignal.create(
            "maaend_no_progress",
            "stuck",
            {"task": "PullCountCalculator"},
            stop=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            raw = root / "raw.png"
            log = root / "agent.log"
            raw.write_bytes(b"raw-frame")
            log.write_text("agent evidence\n", encoding="utf-8")
            record_incident_bundle(
                run_dir,
                signal,
                environment={"state": 1},
                artifact_files={"raw-preflight": raw},
                artifact_json={"mxu-state": {"status": "running"}},
                log_files={"agent": log},
            )
            second = record_incident_bundle(
                run_dir,
                signal,
                environment={"state": 2},
                artifact_json={"mxu-state": {"status": "failed"}},
            )
            incident_path = next((run_dir / "incidents").glob("*/incident.json"))
            incident = read_json_object(incident_path)
            first_occurrence = incident["occurrences"][0]
            second_occurrence = incident["occurrences"][1]
            first_artifacts = first_occurrence["artifacts"]
            second_artifacts = second_occurrence["artifacts"]
            latest_environment = read_json_object(incident_path.parent / "environment.json")

            self.assertEqual((incident_path.parent / first_artifacts["raw-preflight"]).read_bytes(), b"raw-frame")
            self.assertEqual(
                read_json_object(incident_path.parent / first_artifacts["mxu-state"])["status"],
                "running",
            )
            self.assertIn("agent evidence", (incident_path.parent / first_artifacts["agent"]).read_text(encoding="utf-8"))
            self.assertNotEqual(
                first_artifacts["environment"],
                second_artifacts["environment"],
            )
            self.assertEqual(latest_environment["state"], 2)
            self.assertEqual(
                first_occurrence["artifact_details"]["raw-preflight"]["size"],
                len(b"raw-frame"),
            )

        self.assertEqual(second["occurrence_count"], 2)

    def test_triage_is_idempotent_for_the_same_incident_bundle(self) -> None:
        signal = IncidentSignal.create(
            "no_progress",
            "stuck",
            {"last_task": "Continue", "threshold_seconds": 20},
            stop=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-001"
            ledger_path = root / "issues.json"
            write_json_atomic(
                ledger_path,
                {"schema_version": 1, "updated_at": "", "issues": []},
            )
            record_incident_bundle(run_dir, signal)
            record_incident_bundle(run_dir, signal)
            first = triage_run_incidents(run_dir, ledger_path)
            second = triage_run_incidents(run_dir, ledger_path)
            ledger = read_json_object(ledger_path)

        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(ledger["issues"]), 1)
        self.assertEqual(ledger["issues"][0]["occurrences"], 2)
        self.assertEqual(validate_issue_ledger(ledger), [])

    def test_promote_incident_fixture_copies_and_hashes_latest_screen(self) -> None:
        signal = IncidentSignal.create(
            "maa_error",
            "recognition failed",
            {"task": "StageTrader"},
            stop=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            record_incident_bundle(run_dir, signal, screenshot=b"fixture-png")
            incident_path = next((run_dir / "incidents").glob("*/incident.json"))
            manifest_path = root / "fixtures" / "manifest.json"
            write_json_atomic(
                manifest_path,
                {"schema_version": 1, "logical_image_size": [1280, 720], "fixtures": []},
            )
            entry = promote_incident_fixture(
                incident_path,
                manifest_path,
                fixture_id="trader-001",
                page="trader",
                expected_task="StageTrader",
                expected_alignment="right",
                forbidden_tasks=["DropsFlag"],
            )
            manifest = read_json_object(manifest_path)
            errors = validate_fixture_manifest(manifest, manifest_path)

        self.assertEqual(entry["expected"]["must_not_match"], ["DropsFlag"])
        self.assertEqual(errors, [])

    def test_resource_check_reports_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            runtime = root / "runtime"
            relative = Path("resource/tasks/Roguelike/JieGarden.json")
            (source / relative).parent.mkdir(parents=True)
            (runtime / relative).parent.mkdir(parents=True)
            (source / relative).write_text("source", encoding="utf-8")
            (runtime / relative).write_text("runtime", encoding="utf-8")
            result = compare_resource_file(source, runtime, relative)
        self.assertFalse(result["matches"])

    def test_patch_sync_includes_staged_and_explicit_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test"],
                check=True,
            )
            (root / "tracked.txt").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            (root / "tracked.txt").write_text("after\n", encoding="utf-8")
            (root / "staged.txt").write_text("staged\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "staged.txt"], check=True)
            (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            patch_path = root / "result.patch"
            synced = sync_worktree_patch(
                root,
                patch_path,
                expected_head=head,
                include_untracked=[Path("untracked.txt")],
            )
            payload = patch_path.read_text(encoding="utf-8")
            checked = compare_worktree_patch(
                root,
                patch_path,
                include_untracked=[Path("untracked.txt")],
            )

        self.assertTrue(synced["matches"])
        self.assertTrue(checked["matches"])
        self.assertIn("tracked.txt", payload)
        self.assertIn("staged.txt", payload)
        self.assertIn("untracked.txt", payload)


if __name__ == "__main__":
    unittest.main()
