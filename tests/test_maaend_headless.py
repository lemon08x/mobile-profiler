from __future__ import annotations

import threading
import time

from mobile_profiler.maaend_headless_host import (
    _TaskOutcomeTracker,
    _effective_task_status,
    _validate_task_terminal,
    _wait_job,
)
from mobile_profiler.maaend_headless_trial import (
    _append_adaptive_adb_overrides,
    expand_headless_preset,
)


class _DelayedTerminalJob:
    job_id = 1

    def __init__(self, delay_seconds: float) -> None:
        self._terminal_at = time.monotonic() + delay_seconds

    @property
    def done(self) -> bool:
        return time.monotonic() >= self._terminal_at

    @property
    def status(self) -> object:
        return type("Status", (), {"succeeded": True})()


class _DiscardingEvents:
    def emit(self, *_args: object, **_kwargs: object) -> None:
        return None


def test_wait_job_prefers_terminal_state_during_stop_race() -> None:
    stopped = threading.Event()
    stopped.set()

    status = _wait_job(
        _DelayedTerminalJob(0.1),
        deadline=time.monotonic() + 2,
        stop_requested=stopped,
        label="race",
        events=_DiscardingEvents(),  # type: ignore[arg-type]
    )

    assert status == "succeeded"


def test_expand_headless_preset_converts_upstream_option_values() -> None:
    names, options = expand_headless_preset(
        {
            "task": [
                {
                    "name": "TaskA",
                    "controller": ["ADB"],
                    "option": ["Feature"],
                }
            ],
            "option": {
                "Feature": {
                    "type": "switch",
                    "default_case": "Yes",
                    "cases": [
                        {"name": "Yes"},
                        {"name": "No"},
                    ],
                }
            },
            "preset": [
                {
                    "name": "Daily",
                    "task": [
                        {"name": "TaskA", "option": {"Feature": "No"}}
                    ],
                }
            ],
        },
        "Daily",
    )

    assert names == ["TaskA"]
    assert options == {"TaskA": {"Feature": {"type": "switch", "value": False}}}


def test_pipeline_failure_overrides_succeeded_framework_job() -> None:
    tracker = _TaskOutcomeTracker()
    tracker.observe(
        "Tasker.Task.Succeeded",
        {"task_id": 17, "entry": "AndroidOpenGame"},
    )
    tracker.observe(
        "Node.PipelineNode.Failed",
        {"task_id": 17, "name": "OpenGame", "node_id": 31},
    )

    outcome = tracker.snapshot(17)

    assert _effective_task_status("succeeded", outcome) == "failed"
    assert outcome["pipeline_failures"] == [
        {"name": "OpenGame", "node_id": 31}
    ]


def test_android_open_game_terminal_requires_in_world_landscape_viewport() -> None:
    outcome = {
        "pipeline_successes": [
            {"name": "OpenGame", "resolved_name": "EnterGame", "node_id": 7}
        ]
    }
    screenshot = {
        "available": True,
        "width": 1280,
        "height": 720,
        "raw_resolution": [2800, 1260],
        "black_fraction": 0.01,
        "controller_info": {
            "screenshot_viewport": {
                "active": True,
                "adaptive": True,
                "frame_id": 7,
                "active_contacts": [],
            }
        },
    }

    assert _validate_task_terminal("AndroidOpenGame", outcome, screenshot) == []


def test_android_open_game_terminal_rejects_portrait_false_positive() -> None:
    errors = _validate_task_terminal(
        "AndroidOpenGame",
        {"pipeline_successes": []},
        {
            "available": True,
            "width": 720,
            "height": 1600,
            "raw_resolution": [1260, 2800],
            "black_fraction": 1.0,
            "controller_info": {
                "screenshot_viewport": {
                    "active": False,
                    "adaptive": True,
                    "frame_id": 7,
                    "active_contacts": [],
                }
            },
        },
    )

    assert any("not 1280x720" in error for error in errors)
    assert any("not landscape" in error for error in errors)
    assert any("not active" in error for error in errors)
    assert any("is black" in error for error in errors)
    assert any("EnterGame/InWorld" in error for error in errors)


def test_non_launch_tasks_do_not_inherit_launch_terminal_contract() -> None:
    outcome = {
        "pipeline_successes": [
            {
                "name": "DailyRewardEnd",
                "resolved_name": "DailyRewardEnd",
                "node_id": 12,
            }
        ]
    }

    screenshot = {
        "available": True,
        "width": 1280,
        "height": 720,
        "raw_resolution": [2800, 1260],
        "black_fraction": 0.01,
        "controller_info": {
            "screenshot_viewport": {
                "active": True,
                "adaptive": True,
                "frame_id": 13,
                "active_contacts": [],
            }
        },
    }

    assert _validate_task_terminal("DailyRewards", outcome, screenshot) == []


def test_daily_task_terminal_rejects_framework_only_success() -> None:
    screenshot = {
        "available": True,
        "width": 1280,
        "height": 720,
        "raw_resolution": [2800, 1260],
        "black_fraction": 0.01,
        "controller_info": {
            "screenshot_viewport": {
                "active": True,
                "adaptive": True,
                "frame_id": 17,
                "active_contacts": [],
            }
        },
    }
    errors = _validate_task_terminal(
        "AutoCollect",
        {
            "pipeline_successes": [
                {
                    "name": "AutoCollectFinish",
                    "resolved_name": "AutoCollectFinish",
                    "node_id": 31,
                }
            ]
        },
        screenshot,
    )

    assert len(errors) == 1
    assert "AutoCollectEnd" in errors[0]


def test_credit_shopping_reserve_threshold_is_a_natural_terminal() -> None:
    outcome = {
        "pipeline_successes": [
            {
                "name": "CreditShoppingScanItemAction",
                "resolved_name": "CreditShoppingReserveCredit",
                "node_id": 95,
            }
        ]
    }

    screenshot = {
        "available": True,
        "width": 1280,
        "height": 720,
        "raw_resolution": [2800, 1260],
        "black_fraction": 0.01,
        "controller_info": {
            "screenshot_viewport": {
                "active": True,
                "adaptive": True,
                "frame_id": 21,
                "active_contacts": [],
            }
        },
    }

    assert _validate_task_terminal("CreditShoppingN2", outcome, screenshot) == []


def test_daily_task_terminal_rejects_active_touch_contact() -> None:
    outcome = {
        "pipeline_successes": [
            {
                "name": "AutoCollectEnd",
                "resolved_name": "AutoCollectEnd",
                "node_id": 101,
            }
        ]
    }
    screenshot = {
        "available": True,
        "width": 1280,
        "height": 720,
        "raw_resolution": [2800, 1260],
        "black_fraction": 0.01,
        "controller_info": {
            "screenshot_viewport": {
                "active": True,
                "adaptive": True,
                "frame_id": 34,
                "active_contacts": [0],
            }
        },
    }

    errors = _validate_task_terminal("AutoCollect", outcome, screenshot)

    assert errors == ["terminal has active touch contacts: [0]"]


def test_unknown_task_keeps_generic_framework_terminal_contract() -> None:
    assert _validate_task_terminal("InternalProbe", {}, {}) == []


def test_adaptive_overrides_replace_cross_viewport_region_scene_contract() -> None:
    request: dict[str, object] = {"pipeline_override": "[]"}

    _append_adaptive_adb_overrides(request)

    overrides = __import__("json").loads(str(request["pipeline_override"]))
    by_node = {
        name: definition
        for override in overrides
        for name, definition in override.items()
    }
    assert "AdaptiveGuestTerminalExitToWorld" in by_node["SceneAnyEnterWorld"][
        "next"
    ]
    assert "AdaptiveGuestVisitEndConfirm" in by_node["SceneAnyEnterWorld"][
        "next"
    ]
    assert "AdaptiveGuestVisitEndDialogConfirm" in by_node["SceneAnyEnterWorld"][
        "next"
    ]
    assert "AdaptiveGuestWorldExitToOwnWorld" in by_node["SceneAnyEnterWorld"][
        "next"
    ]
    assert by_node["AdaptiveGuestTerminalExitToWorld"]["custom_action"] == (
        "VisitFriendsMenuTerminalExitAction"
    )
    assert "target" not in by_node["AdaptiveGuestWorldExitToOwnWorld"]
    assert by_node["AdaptiveGuestVisitEndConfirm"]["roi"] == [
        450,
        320,
        830,
        400,
    ]
    assert by_node["AdaptiveGuestVisitEndDialogText"]["roi"] == [
        300,
        300,
        700,
        110,
    ]
    assert by_node["AdaptiveGuestVisitEndDialogConfirm"]["all_of"] == [
        "AdaptiveGuestVisitEndDialogText",
        "YellowConfirmButtonType1",
    ]
    assert by_node["AdaptiveGuestVisitEndDialogConfirm"]["box_index"] == 1
    assert "next" not in by_node["AdaptiveGuestVisitEndDialogConfirm"]
    assert by_node["AdaptiveGuestWorldExitToOwnWorld"]["next"] == [
        "AdaptiveGuestVisitEndConfirm"
    ]
    assert by_node["AdaptiveGuestVisitEndConfirm"]["next"] == [
        "AdaptiveGuestVisitEndDialogConfirm"
    ]
    backpack_gate = by_node[
        "__ScenePrivateWorldDijiangEnterMenuBackpackWithDepot"
    ]["recognition"]
    assert backpack_gate == {
        "type": "TemplateMatch",
        "param": {
            "roi": [-250, 0, 200, 200],
            "template": ["SceneManager/Backpack.png"],
            "green_mask": True,
        },
    }
    assert by_node["InRegionalDevelopment"]["recognition"]["param"][
        "all_of"
    ] == ["CheckRegionalDevelopmentText", "RegionalDevelopmentLevelButton"]
    assert by_node["DepotButton"]["recognition"]["param"]["roi"] == [
        850,
        280,
        350,
        170,
    ]
    assert by_node["CheckLocalDepotNodeText"]["recognition"]["param"][
        "roi"
    ] == [0, 50, 400, 110]
    valley_stock = by_node[
        "__ScenePrivateMenuRegionalDevelopmentValleyIVEnterTargetStockRedistribution"
    ]["recognition"]["param"]
    assert valley_stock["all_of"][0]["recognition"]["param"] == {
        "roi": [700, 300, 350, 150],
        "expected": [
            "物资调度",
            "物資調度",
            "(?i)(?:Stock|Material)\\s*Redistribution",
            "物資再分配",
            "물자 조달",
        ],
    }
    assert valley_stock["all_of"][1] == "InRegionalDevelopmentValleyIV"
    assert valley_stock["box_index"] == 0
    wuling_stock = by_node[
        "__ScenePrivateMenuRegionalDevelopmentWulingEnterTargetStockRedistribution"
    ]["recognition"]["param"]
    assert wuling_stock["all_of"][0]["recognition"]["param"] == valley_stock[
        "all_of"
    ][0]["recognition"]["param"]
    assert wuling_stock["all_of"][1] == "InRegionalDevelopmentWuling"
    assert wuling_stock["box_index"] == 0
    assert by_node[
        "__ScenePrivateMenuRegionalDevelopmentWulingEnterTargetEnvironmentMonitoring"
    ]["recognition"]["param"] == {
        "roi": [650, 350, 400, 180],
        "expected": [
            "环境监测",
            "環境監測",
            "(?i)Environment\\s*Monitoring",
            "環境モニタリング",
            "환경 모니터링",
        ],
    }
    assert by_node["__CloseButtonType1"]["recognition"]["param"][
        "roi"
    ] == [1050, 0, 230, 120]
    assert "帝江号" in by_node["InMapDijiang"]["recognition"]["param"][
        "all_of"
    ][1]["recognition"]["param"]["expected"]
    assert by_node["__ScenePrivateMapTeleportConfirm"]["recognition"]["param"][
        "roi"
    ] == [850, 550, 400, 170]
    assert by_node["InReceptionRoom"]["recognition"]["param"]["roi"] == [
        0,
        0,
        300,
        100,
    ]
    assert by_node["InGrowthChamber"]["recognition"]["param"]["roi"] == [
        0,
        0,
        350,
        100,
    ]
    assert by_node["ReceptionRoomExit"]["recognition"]["param"] == {
        "all_of": ["CloseButtonType1"],
        "box_index": 0,
    }
    assert by_node["GrowthChamberExit"]["recognition"]["param"] == {
        "all_of": ["CloseButtonType1"],
        "box_index": 0,
    }
    assert by_node["VisitFriendsMenuTerminalExitToWorldShip"]["custom_action"] == (
        "VisitFriendsMenuTerminalExitAction"
    )
    assert by_node["SellProductInOutpost"]["recognition"]["param"] == {
        "all_of": [
            "SellProductCheckOutpostText",
            "SellProductOpenOperatorLiaisonButton",
        ]
    }
    assert by_node["__ChangeRegionButton"]["recognition"]["param"]["roi"] == [
        115,
        195,
        90,
        90,
    ]
    assert by_node["__ChangeRegionButtonHover"]["recognition"]["param"][
        "roi"
    ] == [115, 195, 90, 90]
    assert by_node["SellProductCheckRefugeeCampText"]["recognition"]["param"] == {
        "roi": [198, 167, 331, 127],
        "expected": [
            "民暂居",
            "民暫居",
            "难民暂居",
            "難民暫居",
            "(?i)Refugee\\s*Camp",
            "仮設居住地",
        ],
    }
    assert by_node["SellProductChangeGoods"]["recognition"]["param"] == {
        "roi": [900, 320, 380, 200],
        "expected": [
            "更换货品",
            "更換貨品",
            "(?i)Switch\\s*Goods",
            "商品変更",
            "选择货品",
            "選擇貨品",
            "(?i)Select\\s*Goods",
            "商品選択",
            "상품 교체",
            "상품 선택",
            "商品变更",
        ],
    }
    assert by_node["DeliveryJobsInCargoPackGoods"]["recognition"]["param"] == {
        "roi": [20, 1, 391, 54],
        "expected": [
            "物装箱",
            "物裝箱",
            "货物装箱",
            "貨物裝箱",
            "(?i)Pack\\s*Goods",
            "パッキング",
            "화물 포장",
        ],
    }
    assert by_node["DeliveryJobsCargoFillToMax"]["recognition"]["param"] == {
        "roi": [900, 150, 350, 150],
        "expected": ["(?i)MAX"],
    }
    assert by_node["DeliveryJobsCheckCargoFilledToMax"]["recognition"][
        "param"
    ] == {
        "roi": [1080, 180, 170, 320],
        "method": 40,
        "lower": [28, 100, 100],
        "upper": [29, 255, 255],
        "connected": True,
        "count": 700,
    }
    assert by_node["DeliveryJobsInCargoRedistributionBid"]["recognition"][
        "param"
    ] == {
        "roi": [20, 1, 391, 54],
        "expected": [
            "度申请",
            "度申請",
            "调度申请",
            "調度申請",
            "(?i)Redistribution\\s*Bid",
            "再分配入札",
            "재분배 입찰",
        ],
    }
    assert by_node["ProdManualAlreadyIn"]["recognition"]["param"] == {
        "roi": [300, 220, 420, 120],
        "expected": ["获取方式", "獲取方式", "(?i)How\\s*to\\s*obtain"],
    }
    assert by_node["ProdManualBackAtPM"]["recognition"]["param"] == {
        "roi": [1000, 0, 280, 140],
        "template": "ClaimDijiangRewards/back.png",
        "threshold": 0.65,
    }
    assert by_node["ProdManualBackAtList"]["recognition"]["param"] == {
        "all_of": ["CloseButtonType1"],
        "box_index": 0,
    }
    assert by_node["ProdManualBackAtProd"]["recognition"]["param"] == {
        "all_of": ["CloseButtonType1"],
        "box_index": 0,
    }
