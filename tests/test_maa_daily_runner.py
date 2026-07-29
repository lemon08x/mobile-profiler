from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mobile_profiler.maa_daily_runner import (
    INT_MAX,
    default_daily_plan,
    known_recovery_fingerprint,
    load_gui_daily_plan,
    override_daily_task_options,
    override_infrast_facilities,
    prepare_device,
    qwen_recovery_action,
    select_daily_plan,
    subtask_error_summary,
    task_run_successful,
)
from mobile_profiler.maa_iteration import callback_summary


class FakeAdb:
    def __init__(self, *, awake: bool = False, locked: bool = True) -> None:
        self.awake = awake
        self.locked = locked
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(command))
        if command[-1] == "get-state":
            output = b"device\n"
        elif command[-2:] == ["dumpsys", "power"]:
            output = b"mWakefulness=Awake" if self.awake else b"mWakefulness=Asleep"
        elif command[-2:] == ["dumpsys", "window"]:
            output = f"mDreamingLockscreen={'true' if self.locked else 'false'}".encode()
        elif command[-2:] == ["wm", "size"]:
            output = b"Physical size: 1260x2800"
        elif command[-2:] == ["keyevent", "224"]:
            self.awake = True
            output = b""
        elif "swipe" in command:
            self.locked = False
            output = b""
        else:
            output = b""
        return subprocess.CompletedProcess(command, 0, output)


class MaaDailyRunnerTests(unittest.TestCase):
    def test_known_recruit_recognition_failure_skips_model_triage_only_on_exact_family(self) -> None:
        known = {
            "task": "Recruit",
            "errors": [
                {
                    "taskchain": "Recruit",
                    "subtask": "ProcessTask",
                    "first": ["RecruitConfirm"],
                },
                {
                    "taskchain": "Recruit",
                    "subtask": "AutoRecruitTask",
                    "what": "RecruitError",
                },
                {
                    "taskchain": "Recruit",
                    "subtask": "AutoRecruitTask",
                },
            ],
        }
        self.assertEqual(
            known_recovery_fingerprint(known),
            "recruit:recognition:confirm-and-auto-recruit-error",
        )

        variants = [
            {**known, "task": "Mall"},
            {
                **known,
                "errors": [
                    *known["errors"],
                    {
                        "taskchain": "Recruit",
                        "subtask": "ProcessTask",
                        "first": ["UnexpectedPage"],
                    },
                ],
            },
            {
                **known,
                "errors": [known["errors"][0]],
            },
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertIsNone(known_recovery_fingerprint(variant))

    def test_web_task_options_override_only_allowlisted_core_parameters(self) -> None:
        selected = select_daily_plan(default_daily_plan(), "Fight,Recruit,Mall")
        overridden = override_daily_task_options(
            selected,
            json.dumps(
                {
                    "Fight": {"stage": "1-7", "medicine": 2, "times": 5},
                    "Recruit": {"times": 2, "expedite": True},
                    "Mall": {"buy_first": ["招聘许可", "技巧概要"]},
                },
                ensure_ascii=False,
            ),
        )

        params = {row["task"]: row["params"] for row in overridden}
        self.assertEqual(params["Fight"]["stage"], "1-7")
        self.assertEqual(params["Fight"]["medicine"], 2)
        self.assertEqual(params["Recruit"]["times"], 2)
        self.assertTrue(params["Recruit"]["expedite"])
        self.assertEqual(params["Mall"]["buy_first"], ["招聘许可", "技巧概要"])
        with self.assertRaisesRegex(ValueError, "unsupported Fight option"):
            override_daily_task_options(selected, {"Fight": {"shell": "bad"}})

    def test_task_subset_preserves_requested_order(self) -> None:
        selected = select_daily_plan(default_daily_plan(), "Recruit,Infrast,Award")

        self.assertEqual([row["task"] for row in selected], ["Recruit", "Infrast", "Award"])

    def test_task_subset_rejects_duplicates_and_unknown_tasks(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            select_daily_plan(default_daily_plan(), "Recruit,Recruit")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            select_daily_plan(default_daily_plan(), "Recruit,Roguelike")

    def test_built_in_mall_names_follow_client_language(self) -> None:
        official = select_daily_plan(default_daily_plan(client_type="Official"), "Mall")[0]
        english = select_daily_plan(default_daily_plan(client_type="YoStarEN"), "Mall")[0]

        self.assertEqual(official["params"]["buy_first"], ["招聘许可"])
        self.assertEqual(official["params"]["blacklist"], ["碳", "家具零件", "加急许可"])
        self.assertEqual(english["params"]["buy_first"], ["Recruitment Permit"])

    def test_callback_summary_preserves_process_probe_name(self) -> None:
        summary = callback_summary(
            "SubTaskError",
            {
                "taskchain": "Infrast",
                "subtask": "ProcessTask",
                "first": ["UnlockClues"],
            },
        )

        self.assertEqual(summary["first"], ["UnlockClues"])
        self.assertEqual(summary["probe"], "UnlockClues")

    def test_expected_negative_probe_is_classified_optional(self) -> None:
        optional = subtask_error_summary(
            {
                "taskchain": "Mall",
                "subtask": "ProcessTask",
                "first": ["CreditShop-NoMoney"],
            }
        )
        real = subtask_error_summary(
            {
                "taskchain": "Mall",
                "subtask": "ProcessTask",
                "first": ["UnexpectedPage"],
            }
        )

        self.assertIs(optional["optional"], True)
        self.assertIs(real["optional"], False)

        reception = subtask_error_summary(
            {
                "taskchain": "Infrast",
                "subtask": "ProcessTask",
                "first": ["InfrastReceptionReceiveMessageBoard"],
            }
        )
        self.assertIs(reception["optional"], True)

        extended = subtask_error_summary(
            {
                "taskchain": "Mall",
                "subtask": "ProcessTask",
                "first": ["CreditShop-NoMoney", "UnexpectedPage"],
            }
        )
        wrong_chain = subtask_error_summary(
            {
                "taskchain": "Infrast",
                "subtask": "ProcessTask",
                "first": ["CreditShop-NoMoney"],
            }
        )
        self.assertIs(extended["optional"], False)
        self.assertIs(wrong_chain["optional"], False)

    def test_reception_clue_probe_requires_the_exact_sequence(self) -> None:
        probe_sequence = [
            "InfrastClueSelfNew",
            "InfrastClueSelfMaybeFull",
            "ReceptionFlag",
        ]

        optional = subtask_error_summary(
            {
                "taskchain": "Infrast",
                "subtask": "ProcessTask",
                "first": probe_sequence,
            }
        )
        individual = subtask_error_summary(
            {
                "taskchain": "Infrast",
                "subtask": "ProcessTask",
                "first": ["InfrastClueSelfMaybeFull"],
            }
        )
        reordered = subtask_error_summary(
            {
                "taskchain": "Infrast",
                "subtask": "ProcessTask",
                "first": list(reversed(probe_sequence)),
            }
        )

        self.assertIs(optional["optional"], True)
        self.assertIs(individual["optional"], False)
        self.assertIs(reordered["optional"], False)

    def test_qwen_recovery_decision_is_restricted_to_restart_or_stop(self) -> None:
        self.assertEqual(
            qwen_recovery_action(
                {"available": True, "analysis": {"safe_action": "stop"}}
            ),
            "stop",
        )
        self.assertEqual(
            qwen_recovery_action(
                {"available": True, "analysis": {"safe_action": "click confirm"}}
            ),
            "restart",
        )
        self.assertEqual(qwen_recovery_action({"available": False}), "restart")

    def test_infrast_facility_override_is_ordered_and_non_mutating(self) -> None:
        plan = default_daily_plan()
        narrowed = override_infrast_facilities(
            select_daily_plan(plan, "Infrast"),
            "Power,Reception",
        )

        self.assertEqual(narrowed[0]["params"]["facility"], ["Power", "Reception"])
        self.assertNotEqual(plan[2]["params"]["facility"], ["Power", "Reception"])

    def test_infrast_facility_override_validates_context_and_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            override_infrast_facilities(select_daily_plan(default_daily_plan(), "Infrast"), "Factory")
        with self.assertRaisesRegex(ValueError, "requires Infrast"):
            override_infrast_facilities(select_daily_plan(default_daily_plan(), "Award"), "Power")

    def test_infrast_subtask_error_requires_recovery_even_after_chain_completion(self) -> None:
        degraded = {
            "status": "completed",
            "all_tasks_completed": True,
            "errors": [{"subtask": "InfrastMfgTask"}],
        }
        clean = {"status": "completed", "all_tasks_completed": True, "errors": []}

        self.assertFalse(task_run_successful("Infrast", degraded))
        self.assertTrue(task_run_successful("Infrast", clean))
        self.assertTrue(task_run_successful("Award", degraded))

    def test_optional_infrast_process_probe_does_not_retry_the_whole_base(self) -> None:
        optional_probe = {
            "status": "completed",
            "all_tasks_completed": True,
            "errors": [{"subtask": "ProcessTask"}],
        }

        self.assertTrue(task_run_successful("Infrast", optional_probe))

    def test_gui_queue_is_translated_to_core_params(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "gui.new.json"
            (root / "gui.json").write_text(
                json.dumps(
                    {
                        "Current": "Default",
                        "Configurations": {"Default": {"Start.ClientType": "Official"}},
                    }
                ),
                encoding="utf-8",
            )
            config.write_text(
                json.dumps(
                    {
                        "Current": "Default",
                        "Configurations": {
                            "Default": {
                                "TaskQueue": [
                                    {"$type": "StartUpTask", "IsEnable": True},
                                    {
                                        "$type": "FightTask",
                                        "IsEnable": True,
                                        "StagePlan": [""],
                                        "UseMedicine": False,
                                        "UseStone": False,
                                        "EnableTimesLimit": False,
                                        "Series": 0,
                                    },
                                    {
                                        "$type": "InfrastTask",
                                        "IsEnable": True,
                                        "Mode": "Normal",
                                        "UsesOfDrones": "Money",
                                        "DormThreshold": 30,
                                        "RoomList": [{"Room": "Mfg"}, {"Room": "Trade"}],
                                    },
                                    {
                                        "$type": "RecruitTask",
                                        "IsEnable": True,
                                        "MaxTimes": 4,
                                        "Level3Choose": True,
                                        "Level4Choose": True,
                                        "Level5Choose": False,
                                        "Level6Choose": False,
                                        "Level3Time": 540,
                                        "Level4Time": 540,
                                    },
                                    {
                                        "$type": "MallTask",
                                        "IsEnable": True,
                                        "Shopping": True,
                                        "IsVisitFriendsAvailable": True,
                                        "FirstList": "Recruitment Permit",
                                        "BlackList": "Carbon;Furniture Part",
                                    },
                                    {"$type": "AwardTask", "IsEnable": True, "Award": True},
                                    {"$type": "RoguelikeTask", "IsEnable": True},
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            plan = load_gui_daily_plan(config)

        self.assertEqual([row["task"] for row in plan], ["StartUp", "Fight", "Infrast", "Recruit", "Mall", "Award"])
        self.assertTrue(plan[0]["params"]["start_game_enabled"])
        self.assertEqual(plan[1]["params"]["times"], INT_MAX)
        self.assertEqual(plan[1]["params"]["series"], 0)
        self.assertEqual(plan[2]["params"]["facility"], ["Mfg", "Trade"])
        self.assertEqual(plan[3]["params"]["select"], [4])
        self.assertEqual(plan[3]["params"]["confirm"], [3, 4])
        self.assertEqual(plan[4]["params"]["blacklist"], ["Carbon", "Furniture Part"])
        self.assertTrue(plan[5]["params"]["award"])

    def test_prepare_device_wakes_and_swipes_insecure_keyguard(self) -> None:
        fake = FakeAdb()
        result = prepare_device(Path("adb.exe"), "device", run_func=fake)

        self.assertTrue(result["awake"])
        self.assertTrue(result["unlocked"])
        self.assertEqual(result["actions"], ["wake", "swipe_unlock"])
        self.assertTrue(any("224" in call for call in fake.calls))
        self.assertTrue(any("swipe" in call for call in fake.calls))


if __name__ == "__main__":
    unittest.main()
