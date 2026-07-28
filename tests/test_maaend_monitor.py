from __future__ import annotations

import json
import unittest

from mobile_profiler.maaend_monitor import MaaEndRunMonitor, parse_maaend_log_line


def watchdogs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "no_progress_seconds": 10,
        "same_node_start_limit": 3,
        "same_node_loop_seconds": 2,
        "static_snapshot_limit": 3,
        "slow_screenshot_seconds": 1,
        "slow_screenshot_consecutive_limit": 2,
        "screenshot_backend_switch_limit": 2,
        "active_touch_grace_seconds": 1,
        "stop_on_slow_screenshot": False,
    }
    values.update(overrides)
    return values


class MaaEndMonitorTests(unittest.TestCase):
    def test_parser_extracts_viewport_and_controller_coordinate_evidence(self) -> None:
        viewport_line = json.dumps(
            {
                "level": "info",
                "task_id": 7,
                "entry": "AndroidOpenGame",
                "raw_width": 2800,
                "raw_height": 1260,
                "viewport_width": 2240,
                "viewport_height": 1260,
                "message": "landscape screenshot viewport activated; continuing AndroidOpenGame",
            }
        )
        details = {
            "action": "touch_down",
            "param": {"contact": 1, "point": [100, 200]},
            "viewport": {
                "alignment": "left",
                "frame_id": 42,
                "logical_param": {"point": [100, 200]},
                "display_param": {"point": [175, 350]},
            },
            "info": {
                "type": "adb",
                "screenshot_viewport": {"active": True, "alignment": "left"},
            },
        }
        controller_line = (
            "[TRC] [message=Controller.Action.Succeeded] "
            f"[details_json={json.dumps(details, separators=(',', ':'))}] "
            "[trans_arg=true]"
        )

        viewport_events = parse_maaend_log_line(viewport_line)
        controller_events = parse_maaend_log_line(controller_line)

        self.assertEqual(viewport_events[0]["kind"], "viewport_activated")
        self.assertEqual(controller_events[0]["kind"], "controller_action")
        self.assertEqual(controller_events[0]["viewport"]["frame_id"], 42)

    def test_parser_keeps_probe_fields_without_persisting_uid_value(self) -> None:
        uid_line = json.dumps(
            {
                "level": "info",
                "component": "captureuid",
                "uid": "salted-redacted-value",
                "alignment": "left",
                "frame_id": 17,
                "use_cache": False,
                "stay_on_current_screen": True,
                "allow_unknown": False,
                "message": "captured uid",
            }
        )
        input_line = json.dumps(
            {
                "level": "info",
                "component": "viewport_input_probe",
                "phase": "camera_reversed",
                "alignment": "right",
                "frame_id": 18,
                "contact": 1,
                "reversed": True,
                "message": "ViewportInputProbe: done",
            }
        )

        uid = parse_maaend_log_line(uid_line)[0]
        input_event = parse_maaend_log_line(input_line)[0]

        self.assertTrue(uid["uid_hash_present"])
        self.assertEqual(len(uid["uid_hash_sha256"]), 64)
        self.assertNotIn("uid", uid)
        self.assertFalse(uid["use_cache"])
        self.assertEqual(input_event["phase"], "camera_reversed")
        self.assertEqual(input_event["contact"], 1)
        self.assertTrue(input_event["reversed"])

    def test_semantic_progress_node_loop_and_no_progress_watchdogs(self) -> None:
        monitor = MaaEndRunMonitor(watchdogs(), now=0)
        state = {
            "task_run_state": {
                "overall_status": "Running",
                "current_task_index": 0,
            }
        }
        tasks = [{"id": "one", "status": "running"}]
        self.assertEqual(monitor.observe_state(state, tasks, now=1), [])
        no_progress = monitor.observe_state(state, tasks, now=12)
        self.assertEqual([row.kind for row in no_progress], ["maaend_no_progress"])

        line = (
            "[TRC] [message=Node.PipelineNode.Starting] "
            '[details_json={"name":"LoopNode"}] [trans_arg=true]'
        )
        monitor = MaaEndRunMonitor(watchdogs(), now=0)
        self.assertEqual(monitor.ingest_log_lines([line], now=1), [])
        self.assertEqual(monitor.ingest_log_lines([line], now=2), [])
        loop = monitor.ingest_log_lines([line], now=3)
        self.assertEqual([row.kind for row in loop], ["maaend_node_loop"])

    def test_node_loop_requires_count_and_elapsed_time_and_resets_window(self) -> None:
        monitor = MaaEndRunMonitor(watchdogs(), now=0)

        def line(name: str) -> str:
            return (
                "[TRC] [message=Node.PipelineNode.Starting] "
                f'[details_json={{"name":"{name}"}}] [trans_arg=true]'
            )

        self.assertEqual(monitor.ingest_log_lines([line("Loading")] * 3, now=1), [])
        self.assertEqual(monitor.ingest_log_lines([line("Loading")], now=2.9), [])
        loop = monitor.ingest_log_lines([line("Loading")], now=3)
        self.assertEqual([row.kind for row in loop], ["maaend_node_loop"])
        self.assertEqual(loop[0].basis["observed_seconds"], 2.0)

        reset = MaaEndRunMonitor(watchdogs(), now=0)
        reset.ingest_log_lines([line("Loading")] * 3, now=1)
        reset.ingest_log_lines([line("Other")], now=2)
        self.assertEqual(reset.ingest_log_lines([line("Loading")] * 3, now=3), [])
        self.assertEqual(reset.state()["same_node_first_started_at"], 3)

    def test_node_loop_ignores_sub_events_and_duplicate_agent_forwarding(self) -> None:
        def framework_line(message: str, details: dict[str, object]) -> str:
            return (
                f"[TRC] [message={message}] "
                f"[details_json={json.dumps(details, separators=(',', ':'))}] "
                "[trans_arg=true]"
            )

        monitor = MaaEndRunMonitor(watchdogs(), now=0)
        first_pipeline = framework_line(
            "Node.PipelineNode.Starting",
            {"task_id": 1, "node_id": 10, "name": "AndroidOpenGame"},
        )
        recognition = framework_line(
            "Node.Recognition.Starting",
            {"task_id": 1, "reco_id": 20, "name": "AndroidOpenGame"},
        )
        action = framework_line(
            "Node.Action.Starting",
            {"task_id": 1, "action_id": 30, "name": "AndroidOpenGame"},
        )
        second_pipeline = framework_line(
            "Node.PipelineNode.Starting",
            {"task_id": 1, "node_id": 11, "name": "AndroidOpenGame"},
        )
        third_pipeline = framework_line(
            "Node.PipelineNode.Starting",
            {"task_id": 1, "node_id": 12, "name": "AndroidOpenGame"},
        )

        self.assertEqual(
            monitor.ingest_log_lines(
                [
                    first_pipeline,
                    first_pipeline,
                    recognition,
                    recognition,
                    action,
                    action,
                    second_pipeline,
                    second_pipeline,
                ],
                now=1,
            ),
            [],
        )
        self.assertEqual(monitor.state()["same_node_starts"], 2)
        loop = monitor.ingest_log_lines([third_pipeline, third_pipeline], now=3)
        self.assertEqual([row.kind for row in loop], ["maaend_node_loop"])
        self.assertEqual(monitor.state()["same_node_starts"], 3)

    def test_static_slow_backend_and_touch_leak_watchdogs(self) -> None:
        monitor = MaaEndRunMonitor(watchdogs(), now=0)
        self.assertEqual(monitor.observe_screenshot(b"same", 1.5, label="watchdog-0001"), [])
        slow = monitor.observe_screenshot(b"same", 1.5, label="watchdog-0002")
        static = monitor.observe_screenshot(b"same", 0.1, label="watchdog-0003")
        self.assertEqual([row.kind for row in slow], ["maaend_slow_screenshot"])
        self.assertEqual([row.kind for row in static], ["maaend_static_screen"])

        switch_line = "Switched active screencap method after runtime failure"
        self.assertEqual(monitor.ingest_log_lines([switch_line], now=4), [])
        switches = monitor.ingest_log_lines([switch_line], now=5)
        self.assertEqual(
            [row.kind for row in switches],
            ["maaend_screencap_failover_loop"],
        )

        down = (
            "[TRC] [message=Controller.Action.Succeeded] "
            '[details_json={"action":"touch_down","param":{"contact":2},'
            '"viewport":{"alignment":"right","frame_id":9,'
            '"logical_param":{"point":[900,300]},'
            '"display_param":{"point":[2100,525]}}}] [trans_arg=true]'
        )
        monitor.ingest_log_lines([down], now=6)
        leak = monitor.terminal_signals(now=8)
        self.assertEqual([row.kind for row in leak], ["maaend_active_touch_leak"])
        self.assertEqual(monitor.state()["active_contacts"], [2])

    def test_touch_up_clears_contact_and_preserves_alignment_trace(self) -> None:
        monitor = MaaEndRunMonitor(watchdogs(), now=0)
        lines = []
        for action in ("touch_down", "touch_move", "touch_up"):
            details = {
                "action": action,
                "param": {"contact": 3},
                "viewport": {
                    "alignment": "left",
                    "frame_id": 11,
                    "logical_param": {"point": [100, 600]},
                    "display_param": {"point": [175, 1050]},
                },
            }
            lines.append(
                "[TRC] [message=Controller.Action.Succeeded] "
                f"[details_json={json.dumps(details, separators=(',', ':'))}] "
                "[trans_arg=true]"
            )
        monitor.ingest_log_lines(lines, now=1)

        self.assertEqual(monitor.terminal_signals(now=2), [])
        self.assertEqual(monitor.state()["active_contacts"], [])
        self.assertEqual(
            [row["alignment"] for row in monitor.state()["alignment_trace"]],
            ["left", "left", "left"],
        )
        self.assertEqual(
            [row["contact"] for row in monitor.state()["alignment_trace"]],
            [3, 3, 3],
        )


if __name__ == "__main__":
    unittest.main()
