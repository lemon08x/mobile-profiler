#!/usr/bin/env python3
"""Resolve MaaEnd tasks and run them through the UI-free MaaFramework host."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Collection, Mapping, Sequence

from .maaend_runtime import (
    _convert_preset_option_value,
    _initialize_option_values,
    _load_interface_bundle,
    _mxu_agent_configs,
    _mxu_pi_envs,
    _mxu_task_requests,
    _normalize_task_option_values,
    _runtime_resource_paths,
    _stable_task_id,
    validate_maaend_runtime,
)


# Compound scene recognitions authored for a single 16:9 image can span both
# horizontal edges.  On an ultrawide phone, no Left/Center/Right 16:9 viewport
# contains both edges at once.  Keep each adaptive scene contract within one
# viewport while retaining at least two independent visual signals.
_ADAPTIVE_ADB_PIPELINE_OVERRIDES: list[dict[str, object]] = [
    {
        "SceneAnyEnterWorld": {
            # On Android, Back while the VisitFriends guest terminal is open
            # raises the quit-game dialog instead of leaving the terminal.
            # Give that recoverable scene an explicit branch before the
            # catch-all __ScenePrivateAnyExit loop.
            "next": [
                "__ScenePrivateCloseADBExit",
                "AdaptiveGuestVisitEndDialogConfirm",
                "AdaptiveGuestVisitEndConfirm",
                "AdaptiveGuestTerminalExitToWorld",
                "AdaptiveGuestWorldExitToOwnWorld",
                "__ScenePrivateMenuListLoadingEnterWorld",
                "__ScenePrivateMenuListEnterWorld",
                "__ScenePrivateTrialOfSwordmancyEnterWorld",
                "__ScenePrivateAnyEnterWorldSuccess",
                "[JumpBack]__ScenePrivateLogin",
                "[JumpBack]__ScenePrivateNoticeRewardsUpgrade",
                "[JumpBack]__ScenePrivateWorldSpaceExit",
                "[JumpBack]__ScenePrivateWorldStorySkip",
                "[JumpBack]__ScenePrivateLoggedOutConfirm",
                "[JumpBack]SceneWaitLoadingExit",
                "[JumpBack]__ScenePrivateAnyExit",
            ]
        },
        "__ScenePrivateWorldEnterMapAny": {
            # The upstream 16:9 target [90,90,50,50] lands on the left edge
            # of the phone minimap after Left-viewport mapping.  It only
            # opened the map when the action's random point happened to fall
            # inside the circular hit target.  The observed raw minimap
            # center is (280,170) on a 2800x1260 display, which maps to
            # logical (160,97) in the Left 2240x1260 viewport.  Keep the
            # semantic InWorld recognition but use a deterministic interior
            # target; AutoAltClickAction labels it Left before injection.
            "action": {
                "type": "Custom",
                "param": {
                    "custom_action": "AutoAltClickAction",
                    "target": [145, 82, 30, 30],
                    "custom_action_param": {
                        "viewport_alignment": "left",
                    },
                },
            }
        },
        "AdaptiveGuestTerminalExitToWorld": {
            "recognition": "OCR",
            "roi": [0, 0, 220, 80],
            "expected": [
                "访客终端",
                "訪客終端",
                "(?i)Guest\\s*Terminal",
                "訪問端末",
                "방문자 단말기",
            ],
            "pre_delay": 0,
            "action": "Custom",
            "custom_action": "VisitFriendsMenuTerminalExitAction",
            "post_wait_freezes": 400,
            "post_delay": 0,
            "next": ["AdaptiveGuestWorldExitToOwnWorld"],
        },
        "AdaptiveGuestVisitEndDialogText": {
            # The final confirmation dialog is centered in every adaptive
            # viewport, while its button moves horizontally with the chosen
            # crop.  Keep the semantic guard independent from the click box.
            "recognition": "OCR",
            "roi": [300, 300, 700, 110],
            "expected": [
                "是否要结束本次拜访",
                "是否要結束本次拜訪",
                "(?i)End.*visit",
            ],
        },
        "AdaptiveGuestVisitEndDialogConfirm": {
            # A yellow confirmation button occurs in many unrelated dialogs.
            # Require the visit-specific title, then click only the matched
            # YellowConfirmButtonType1 box from the same viewport.
            "recognition": "And",
            "all_of": [
                "AdaptiveGuestVisitEndDialogText",
                "YellowConfirmButtonType1",
            ],
            "box_index": 1,
            "pre_delay": 0,
            "action": "Click",
            "post_wait_freezes": 400,
            "post_delay": 0,
        },
        "AdaptiveGuestVisitEndConfirm": {
            # Leaving a friend's ship opens a second-layer selector.  Android
            # Back merely closes it and returns to the ship, so confirm the
            # explicit "End Visit" action instead.
            "recognition": "OCR",
            # The first in-world prompt lands near logical y=411.  Clicking
            # it opens the friend selector, whose final End Visit button is
            # near the lower-right edge (y≈640) in the Right viewport.  One
            # semantic node deliberately covers both consecutive states.
            "roi": [450, 320, 830, 400],
            "expected": [
                "结束拜访",
                "結束拜訪",
                "(?i)End\\s*Visit",
                "訪問を終了",
                "방문 종료",
            ],
            "pre_delay": 0,
            "action": "Click",
            "post_wait_freezes": 400,
            "post_delay": 0,
            "next": ["AdaptiveGuestVisitEndDialogConfirm"],
        },
        "AdaptiveGuestWorldExitToOwnWorld": {
            "recognition": "TemplateMatch",
            "roi": [0, 0, 220, 100],
            "template": "VisitFriends/ShipEscButton.png",
            "threshold": 0.75,
            "pre_wait_freezes": 400,
            "pre_delay": 0,
            # Android Back opens the quit-game dialog in a friend's ship.
            # Click the matched top-left Leave icon in the Left viewport; its
            # recognition box is tighter than a broad fixed target.
            "action": "Click",
            "post_delay": 0,
            "next": ["AdaptiveGuestVisitEndConfirm"],
        },
    },
    {
        "__ScenePrivateWorldDijiangEnterMenuBackpackWithDepot": {
            # The upstream 16:9 contract combines the left-side Dijiang title
            # with two right-side menu icons.  No adaptive 16:9 viewport on an
            # ultrawide device can contain all three.  Its caller has already
            # established SceneEnterWorldDijiang, so keep the action guarded
            # by the Backpack signal that carries the AutoAltClick target.
            # The SceneEnterWorldDijiang caller is the independent world guard;
            # the upstream WorldMenu template does not match the current
            # Android HUD skin and must not force another teleport loop.
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [-250, 0, 200, 200],
                    "template": ["SceneManager/Backpack.png"],
                    "green_mask": True,
                },
            }
        }
    },
    {
        "InRegionalDevelopment": {
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        "CheckRegionalDevelopmentText",
                        "RegionalDevelopmentLevelButton",
                    ]
                },
            }
        }
    },
    {
        "DepotButton": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [850, 280, 350, 170],
                    "template": "Common/Button/DepotButton.png",
                    "threshold": 0.8,
                },
            }
        }
    },
    {
        "ProtocolSpaceOperationalManualFindProtocolSpaceNormalEnable": {
            # The desktop OperatorEXP icon is rendered with different card
            # artwork on Android (observed best score ~0.42), while both the
            # card title and its row-local Go button remain stable.  Restrict
            # both OCR signals to the first visible operator-progression row
            # so the action cannot drift to another protocol-space entry.
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [300, 130, 650, 180],
                                    "expected": [
                                        "协议空间.*干员进阶",
                                        "協議空間.*幹員進階",
                                        "(?i)Protocol\\s*Space.*Operator",
                                    ],
                                },
                            }
                        },
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [850, 150, 430, 180],
                                    "expected": [
                                        "前往",
                                        "(?i)^Go$",
                                        "進入",
                                        "移動",
                                        "이동",
                                    ],
                                },
                            }
                        },
                    ],
                    "box_index": 1,
                },
            },
            "pre_delay": 0,
            "action": "Click",
            "post_wait_freezes": 400,
            "post_delay": 0,
            "next": ["ProtocolSpaceNormalPrepareEnter"],
        },
        "ProtocolSpaceNormalPrepareEnter": {
            # Android keeps the semantic title but uses a wider header and a
            # different glyph scale than the desktop 200x60 fixture.
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [0, 0, 360, 90],
                    "expected": [
                        "协议空间",
                        "協議空間",
                        "(?i)Protocol\\s*Space",
                        "協約空間",
                        "프로토콜\\s*스페이스",
                    ],
                },
            },
            "pre_delay": 0,
            "post_wait_freezes": 200,
            "post_delay": 0,
            "next": ["ProtocolSpaceLevelChoose"],
        },
    },
    {
        "CheckLocalDepotNodeText": {
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [0, 50, 400, 110],
                    "expected": [
                        "本地仓储节点",
                        "本地區",
                        "本地區倉儲節點",
                        "(?i)Local\\s*Depot\\s*Node",
                        "地域保管ボックス",
                        "현재 지역 저장고 노드",
                        "地域保管",
                    ],
                },
            }
        }
    },
    {
        "__ScenePrivateMenuRegionalDevelopmentValleyIVEnterTargetStockRedistribution": {
            # The current Android build changed the icon artwork, while the
            # visible "物资调度" label remains stable. Match and click that
            # label in the same viewport as the Valley IV scene guard.
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [700, 300, 350, 150],
                                    "expected": [
                                        "物资调度",
                                        "物資調度",
                                        "(?i)(?:Stock|Material)\\s*Redistribution",
                                        "物資再分配",
                                        "물자 조달",
                                    ],
                                },
                            }
                        },
                        "InRegionalDevelopmentValleyIV",
                    ],
                    "box_index": 0,
                },
            }
        },
        "__ScenePrivateMenuRegionalDevelopmentWulingEnterTargetStockRedistribution": {
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [700, 300, 350, 150],
                                    "expected": [
                                        "物资调度",
                                        "物資調度",
                                        "(?i)(?:Stock|Material)\\s*Redistribution",
                                        "物資再分配",
                                        "물자 조달",
                                    ],
                                },
                            }
                        },
                        "InRegionalDevelopmentWuling",
                    ],
                    "box_index": 0,
                },
            }
        },
        "__ScenePrivateMenuRegionalDevelopmentWulingEnterTargetEnvironmentMonitoring": {
            # The Wuling title and the Environment Monitoring tile live in
            # opposite phone safe areas, so no adaptive 16:9 viewport can
            # satisfy the upstream And. This node is reached only after the
            # Wuling scene transition; match and click the tile's own label.
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [650, 350, 400, 180],
                    "expected": [
                        "环境监测",
                        "環境監測",
                        "(?i)Environment\\s*Monitoring",
                        "環境モニタリング",
                        "환경 모니터링",
                    ],
                },
            }
        },
    },
    {
        "DeliveryJobsCheckLocalDepotNodeOriginiumScienceParkText": {
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [0, 180, 1200, 180],
                    "expected": [
                        "源石研究园",
                        "源石研究園",
                        "(?i)Originium\\s*Science\\s*Park",
                        "源石研究パーク",
                        "오리지늄 연구 구역",
                    ],
                },
            }
        }
    },
    {
        "__CloseButtonType1": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [1050, 0, 230, 120],
                    "template": ["Common/Button/CloseButtonType1.png"],
                    "threshold": 0.7,
                    "green_mask": True,
                },
            }
        }
    },
    {
        "InMapAny": {
            # Both upstream templates have low-confidence lookalikes in the
            # Android world HUD (0.72/0.75 on the current phone), so their
            # conjunction can report that the map opened while it is still
            # in the world. The map's top-left Mission Reminder label is a
            # stable semantic signal and is absent from the world HUD. Keep
            # MapMind as the independent right-side signal, but replace the
            # collision-prone MapMissionReminder template with OCR.
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        {
                            "recognition": {
                                "type": "TemplateMatch",
                                "param": {
                                    "roi": [-350, 0, 350, 60],
                                    "template": ["SceneManager/MapMind.png"],
                                    "method": 10001,
                                },
                            }
                        },
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [0, 0, 250, 100],
                                    "expected": [
                                        "事务提醒",
                                        "事務提醒",
                                        "Mission Reminder",
                                        "Reminder",
                                        "業務通知",
                                        "업무 알림",
                                    ],
                                },
                            }
                        },
                    ]
                },
            }
        }
    },
    {
        "InMapDijiang": {
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        "InMapAny",
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [0, 0, 350, 220],
                                    "expected": [
                                        "帝江号",
                                        "帝江號",
                                        "(?i)(?:O.?M.?V|M.?V)",
                                    ],
                                },
                            }
                        },
                    ]
                },
            }
        }
    },
    {
        "__ScenePrivateMapTeleportConfirm": {
            # On the ultrawide Android map the selected protocol point opens
            # a wide, text-bearing button.  The upstream 94x102 icon template
            # is not stable across the phone's UI scale: the button can be
            # plainly visible while both template scores stay below 0.5.
            # Keep the fast template path, then fall back to OCR within the
            # same physical-right ROI.  The recognition result still carries
            # the Right viewport alignment into the inherited Click action.
            "recognition": {
                "type": "Or",
                "param": {
                    "any_of": [
                        {
                            "recognition": {
                                "type": "TemplateMatch",
                                "param": {
                                    "roi": [850, 550, 430, 170],
                                    "template": [
                                        "Common/Button/TeleportButton.png",
                                        "Common/Button/TeleportButtonHover.png",
                                    ],
                                },
                            }
                        },
                        {
                            "recognition": {
                                "type": "OCR",
                                "param": {
                                    "roi": [850, 550, 430, 170],
                                    "expected": [
                                        "传送",
                                        "傳送",
                                        "(?i)Teleport",
                                        "転送",
                                        "전송",
                                    ],
                                },
                            }
                        },
                    ],
                },
            }
        }
    },
    {
        "__ScenePrivateMapTeleportSuccess": {
            # WorldMenu.png and ProtosyncMenuButton.png are byte-identical.
            # In the ultrawide Android map, the physical-right energy icon
            # matches that template at >0.9 and used to end teleportation
            # while the map was still open.  The regional-development button
            # is a separate in-world HUD signal (0.95 on this phone) and is
            # absent from all three map viewports, so use it as the adaptive
            # teleport terminal gate.  Keep the upstream loading/freezes
            # contract; this override replaces recognition only.
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": ["RegionalDevelopmentButton"],
                    "box_index": 0,
                },
            }
        }
    },
    {
        "AutoCollectRoute1AssertLocation": {
            # The Wuling City 5 teleport is not a pixel-exact spawn.  On the
            # adaptive Android viewport, two independent high-confidence
            # minimap matches reported (660.6, 735.0), while the upstream
            # half-open rectangle starts at x=663.  Preserve the map-name
            # guard and expand the spawn rectangle by five map units on each
            # side.  This is deliberately node-local: navigation checkpoints
            # and assertions for other routes retain their authored bounds.
            "recognition": {
                "type": "Custom",
                "param": {
                    "custom_recognition": "MapTrackerAssertLocation",
                    "custom_recognition_param": {
                        "expected": [
                            {
                                "map_name": "map02_lv002",
                                "target": [658, 728, 30, 30],
                            }
                        ]
                    },
                },
            }
        }
    },
    {
        "InReceptionRoom": {
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [0, 0, 300, 100],
                    "expected": [
                        "会客室",
                        "會客室",
                        "(?i)Reception\\s*Room",
                        "応接室",
                    ],
                },
            }
        }
    },
    {
        "InGrowthChamber": {
            "recognition": {
                "type": "OCR",
                "param": {
                    # The phone safe-area places the title across the right
                    # edge of the upstream 43..210 ROI in the Left viewport.
                    "roi": [0, 0, 350, 100],
                    "expected": [
                        "培养舱",
                        "培養艙",
                        "(?i)Growth\\s*Chamber",
                        "培養室",
                    ],
                },
            }
        }
    },
    {
        "ReceptionRoomExit": {
            "recognition": {
                "type": "And",
                # The upstream two-item And selects the close button through
                # box_index=1. Replacing all_of with one right-edge-only item
                # must also reset box_index or MaaFramework rejects the task.
                "param": {
                    "all_of": ["CloseButtonType1"],
                    "box_index": 0,
                },
            }
        }
    },
    {
        "GrowthChamberExit": {
            "recognition": {
                "type": "And",
                # GrowthChamberViewIn already established the left-side title
                # and facility details. The exit node must be right-edge-only
                # because no 16:9 viewport contains both phone safe areas.
                "param": {
                    "all_of": ["CloseButtonType1"],
                    "box_index": 0,
                },
            }
        }
    },
    {
        "VisitFriendsMenuTerminalExitToWorldShip": {
            # The close button is outside the Center crop on 2800x1260.
            # Its Right-viewport logical bounds are x=1115..1152,
            # y=16..53.  The previous broad target extended to x=1200 and
            # MaaFramework legitimately picked a random point that missed.
            # The title recognition belongs to Center, so a normal Click
            # would inherit Center even though these coordinates describe
            # the Right crop. The custom action labels Right explicitly.
            "action": "Custom",
            "custom_action": "VisitFriendsMenuTerminalExitAction",
        }
    },
    {
        "SellProductInOutpost": {
            # The upstream scene combines the far-left outpost breadcrumb
            # with the far-right "QTY in current trade" label. No adaptive
            # 16:9 crop can contain both phone safe areas. Use two stable
            # signals that coexist in the Left viewport instead.
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": [
                        "SellProductCheckOutpostText",
                        "SellProductOpenOperatorLiaisonButton",
                    ]
                },
            }
        },
        "__ChangeRegionButton": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    # The phone places the icon at x≈139 in Left; the
                    # upstream x=24..112 ROI contains only textured artwork
                    # and produced a false-positive click at x=25.
                    "roi": [115, 195, 90, 90],
                    "template": ["Common/Button/ChangeRegionButton.png"],
                    "method": 10001,
                    "threshold": 0.9,
                },
            }
        },
        "__ChangeRegionButtonHover": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [115, 195, 90, 90],
                    "template": ["Common/Button/ChangeRegionButtonHover.png"],
                    "method": 10001,
                    "threshold": 0.9,
                },
            }
        },
        "SellProductCheckRefugeeCampText": {
            # The large Chinese outpost title is rendered close to the right
            # edge of this ROI on the phone. PaddleOCR consistently returns
            # the visible semantic stem ("难民暂居") without the final suffix;
            # accept that stable stem while keeping the original tight ROI.
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [198, 167, 331, 127],
                    "expected": [
                        "民暂居",
                        "民暫居",
                        "难民暂居",
                        "難民暫居",
                        "(?i)Refugee\\s*Camp",
                        "仮設居住地",
                    ],
                },
            }
        },
        "SellProductChangeGoods": {
            # The phone pins this control to the physical right safe area.
            # In Center it is truncated at x=1280; in Right it moves left of
            # the upstream x=1115 ROI. A wider right-side ROI lets adaptive
            # recognition select Right and keeps the click in that viewport.
            "recognition": {
                "type": "OCR",
                "param": {
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
                },
            }
        },
        "DeliveryJobsInCargoPackGoods": {
            # The Android ultrawide layout pins the yellow Next button to the
            # physical right edge. In the Right viewport that button is
            # complete, but the first Chinese title glyph is cropped away and
            # PaddleOCR consistently sees the stable suffix "物装箱". Accept
            # the visible suffix so the page guard and button can be matched
            # from the same frame/alignment.
            "recognition": {
                "type": "OCR",
                "param": {
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
                },
            }
        },
        "DeliveryJobsCargoFillToMax": {
            # The current Android build renders the small MAX glyph with a
            # different font/weight than the desktop template. OCR the label
            # on the guarded cargo-fill page and click its own matched box.
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [900, 150, 350, 150],
                    "expected": ["(?i)MAX"],
                },
            }
        },
        "DeliveryJobsCheckCargoFilledToMax": {
            # The filled-state template no longer matches the current game's
            # taller yellow capacity bar. Detect that bar by the same narrow
            # yellow hue contract used by MaaEnd's guarded buttons, scoped to
            # the capacity gauge so the Next button cannot satisfy it.
            "recognition": {
                "type": "ColorMatch",
                "param": {
                    "roi": [1080, 180, 170, 320],
                    "method": 40,
                    "lower": [28, 100, 100],
                    "upper": [29, 255, 255],
                    "connected": True,
                    "count": 700,
                },
            }
        },
        "DeliveryJobsInCargoRedistributionBid": {
            # The Start Delivery button is right-pinned. In the Right
            # viewport the breadcrumb is clipped to its stable final segment
            # ("度申请"), so accept that suffix and keep the semantic guard in
            # the same frame as the confirm button.
            "recognition": {
                "type": "OCR",
                "param": {
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
                },
            }
        },
    },
    {
        # The Android crafting-manual detail uses an icon-only Return button;
        # the desktop-authored OCR contract never sees the word "Return".
        # The existing Dijiang asset is the same circular arrow and becomes
        # fully visible in the Right viewport on an ultrawide phone.
        "ProdManualAlreadyIn": {
            "recognition": {
                "type": "OCR",
                "param": {
                    "roi": [300, 220, 420, 120],
                    "expected": [
                        "获取方式",
                        "獲取方式",
                        "(?i)How\\s*to\\s*obtain",
                    ],
                },
            }
        },
        "ProdManualBackAtPM": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [1000, 0, 280, 140],
                    "template": "ClaimDijiangRewards/back.png",
                    "threshold": 0.65,
                },
            }
        },
        "ProdManualBackAtList": {
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": ["CloseButtonType1"],
                    "box_index": 0,
                },
            }
        },
        "ProdManualBackAtProd": {
            "recognition": {
                "type": "And",
                "param": {
                    "all_of": ["CloseButtonType1"],
                    "box_index": 0,
                },
            }
        },
    },
]


# A cold Android launch may spend more than a minute compiling shaders.  The
# upstream OpenGame loop recognizes its loading icon immediately and jumps
# back without delay, which exhausts MaaFramework's Pipeline loop budget
# before the phone reaches the title screen.  Keep the delay launch-scoped so
# ordinary loading transitions in daily tasks remain fast.
_ANDROID_OPEN_GAME_PIPELINE_OVERRIDES: list[dict[str, object]] = [
    {
        "WaitBlackScreen": {"post_delay": 5_000},
        "WaitLoadingIcon": {"post_delay": 20_000},
        "WaitLoadingText": {"post_delay": 10_000},
    }
]


def _append_adaptive_adb_overrides(task_request: dict[str, object]) -> None:
    raw_override = task_request.get("pipeline_override", [])
    if isinstance(raw_override, str):
        parsed = json.loads(raw_override)
    else:
        parsed = raw_override
    if isinstance(parsed, dict):
        overrides: list[object] = [parsed]
    elif isinstance(parsed, list):
        overrides = list(parsed)
    else:
        raise ValueError("MaaEnd task Pipeline override must be an object or list")
    protocol_level_index = 4
    if str(task_request.get("name") or "") == "ProtocolSpace":
        for override in overrides:
            if not isinstance(override, dict):
                continue
            node = override.get("ProtocolSpaceLevelChoose")
            if not isinstance(node, dict):
                continue
            recognition = node.get("recognition")
            if not isinstance(recognition, dict):
                continue
            param = recognition.get("param")
            if not isinstance(param, dict):
                continue
            index = param.get("index")
            if isinstance(index, int) and 0 <= index <= 4:
                protocol_level_index = index

    overrides.extend(
        json.loads(json.dumps(row)) for row in _ADAPTIVE_ADB_PIPELINE_OVERRIDES
    )
    if str(task_request.get("name") or "") == "ProtocolSpace":
        level_names = ["一级", "二级", "三级", "四级", "五级"]
        traditional_level_names = ["一級", "二級", "三級", "四級", "五級"]
        level_number = protocol_level_index + 1
        overrides.append(
            {
                "ProtocolSpaceLevelChoose": {
                    # Android renders a different 22px completion glyph, so
                    # the desktop templates peak around 0.60.  Preserve the
                    # configured upstream level index and select its visible
                    # semantic label instead.
                    "recognition": {
                        "type": "OCR",
                        "param": {
                            "roi": [0, 80, 380, 440],
                            "expected": [
                                f"^{level_names[protocol_level_index]}$",
                                f"^{traditional_level_names[protocol_level_index]}$",
                                f"(?i)^Level\\s*{level_number}$",
                            ],
                        },
                    },
                    "pre_delay": 0,
                    "action": "Click",
                    "post_wait_freezes": 200,
                    "post_delay": 0,
                    # The reward set is persisted by the game.  Skipping the
                    # desktop-only picker removes an unrelated modal from the
                    # stamina sink while retaining the selected level.
                    "next": [
                        "ProtocolSpacePrepareLock",
                        "ProtocolSpaceEnterSpace",
                    ],
                }
            }
        )
    if str(task_request.get("name") or "") == "AndroidOpenGame":
        overrides.extend(
            json.loads(json.dumps(row))
            for row in _ANDROID_OPEN_GAME_PIPELINE_OVERRIDES
        )
    task_request["pipeline_override"] = json.dumps(
        overrides,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _default_binding_root() -> Path:
    explicit = os.environ.get("MAAFW_PYTHON_BINDING_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    return (
        repository
        / ".codex-research"
        / "MaaFramework-v5.12.1"
        / "source"
        / "binding"
        / "Python"
    ).resolve()


def _load_options_file(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("--options-file 必须是以任务名为 key 的 JSON 对象")
    return value


def expand_headless_preset(
    interface: Mapping[str, object],
    preset_name: str,
) -> tuple[list[str], dict[str, object]]:
    """Expand an upstream MaaEnd preset without creating or opening its UI."""

    presets = interface.get("preset")
    preset = next(
        (
            row
            for row in presets
            if isinstance(row, dict)
            and str(row.get("name") or "").strip() == preset_name
        ),
        None,
    ) if isinstance(presets, list) else None
    if preset is None:
        raise ValueError(f"MaaEnd 预设不存在: {preset_name}")

    task_definitions = {
        str(row.get("name") or "").strip(): row
        for row in interface.get("task", [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    }
    option_definitions = {
        str(name): definition
        for name, definition in dict(interface.get("option") or {}).items()
        if isinstance(definition, dict)
    }
    names: list[str] = []
    options: dict[str, object] = {}
    raw_tasks = preset.get("task")
    for row in raw_tasks if isinstance(raw_tasks, list) else []:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        name = str(row.get("name") or "").strip()
        definition = task_definitions.get(name)
        if definition is None:
            raise ValueError(f"MaaEnd 预设 {preset_name} 引用了未知任务: {name}")
        if name in options:
            raise ValueError(f"MaaEnd 预设 {preset_name} 包含重复任务: {name}")
        values = _initialize_option_values(
            [str(value) for value in definition.get("option", [])],
            option_definitions,
        )
        raw_options = row.get("option")
        if isinstance(raw_options, dict):
            for option_id, raw_value in raw_options.items():
                option_definition = option_definitions.get(str(option_id))
                if option_definition is None:
                    raise ValueError(
                        f"MaaEnd 预设 {preset_name} 的任务 {name} 引用了未知选项: {option_id}"
                    )
                converted = _convert_preset_option_value(
                    str(option_id),
                    raw_value,
                    option_definition,
                )
                if converted is not None:
                    values[str(option_id)] = converted
        names.append(name)
        options[name] = values
    if not names:
        raise ValueError(f"MaaEnd 预设没有可执行任务: {preset_name}")
    return names, options


def resolve_headless_tasks(
    runtime_root: Path,
    task_names: Sequence[str],
    options_by_task: Mapping[str, object],
    experimental_adb_tasks: Collection[str] = (),
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    if not task_names:
        raise ValueError("至少需要一个 MaaEnd 任务")
    if len(set(task_names)) != len(task_names):
        raise ValueError("MaaEnd headless 任务不能重复")
    interface = _load_interface_bundle(runtime_root)
    definitions = {
        str(row.get("name") or "").strip(): row
        for row in interface.get("task", [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    }
    option_definitions = {
        str(name): definition
        for name, definition in dict(interface.get("option") or {}).items()
        if isinstance(definition, dict)
    }
    unknown_options = sorted(set(options_by_task) - set(task_names))
    if unknown_options:
        raise ValueError("为未选择任务提供了选项: " + ", ".join(unknown_options))
    experimental = {
        str(name).strip() for name in experimental_adb_tasks if str(name).strip()
    }
    unselected_experimental = sorted(experimental - set(task_names))
    if unselected_experimental:
        raise ValueError(
            "实验 ADB 放行只能指向本次选中的任务: "
            + ", ".join(unselected_experimental)
        )
    configurations: list[dict[str, object]] = []
    for name in task_names:
        definition = definitions.get(name)
        if definition is None:
            raise ValueError(f"MaaEnd 任务不存在: {name}")
        controllers = definition.get("controller")
        if (
            not isinstance(controllers, list) or "ADB" not in controllers
        ) and name not in experimental:
            raise ValueError(f"MaaEnd 任务未声明支持 ADB: {name}")
        raw_values = options_by_task.get(name, {})
        if not isinstance(raw_values, dict):
            raise ValueError(f"任务 {name} 的选项必须是 JSON 对象")
        normalized = _normalize_task_option_values(
            definition,
            raw_values,
            option_definitions,
        )
        configurations.append(
            {
                "id": _stable_task_id(name),
                "name": name,
                "enabled": True,
                "option_values": normalized,
            }
        )
    resources = interface.get("resource")
    resource_name = next(
        (
            str(row.get("name") or "")
            for row in resources
            if isinstance(row, dict) and str(row.get("name") or "")
        ),
        "",
    ) if isinstance(resources, list) else ""
    profile = {
        "resource": resource_name,
        "task_configurations": configurations,
    }
    requests, metadata = _mxu_task_requests(
        interface,
        {"globalOptionValues": {}},
        profile,
    )
    for request, row in zip(requests, metadata):
        request["name"] = row["name"]
    return interface, requests, metadata


def build_headless_request(
    *,
    runtime_root: Path,
    binding_root: Path,
    adb: Path,
    device: str,
    output_root: Path,
    task_names: Sequence[str],
    options_by_task: Mapping[str, object],
    max_seconds: float,
    continue_on_failure: bool,
    experimental_adb_tasks: Collection[str] = (),
) -> dict[str, object]:
    validate_maaend_runtime(runtime_root)
    interface, requests, metadata = resolve_headless_tasks(
        runtime_root,
        task_names,
        options_by_task,
        experimental_adb_tasks,
    )
    resource_name = next(
        (
            str(row.get("name") or "")
            for row in interface.get("resource", [])
            if isinstance(row, dict) and str(row.get("name") or "")
        ),
        "",
    )
    raw_agents = _mxu_agent_configs(interface)
    agents: list[dict[str, object]] = []
    for row in raw_agents:
        executable = runtime_root / str(row["child_exec"])
        if os.name == "nt" and not executable.suffix:
            executable = executable.with_suffix(".exe")
        agents.append(
            {
                "path": str(executable.resolve()),
                "args": list(row.get("child_args", [])),
            }
        )
    pi_env = _mxu_pi_envs(
        interface,
        controller_name="ADB",
        resource_name=resource_name,
        maafw_version="DEBUG_VERSION",
    )
    pi_env["PI_CLIENT_NAME"] = "MobileProfilerHeadless"
    pi_env["PI_CLIENT_VERSION"] = "1"
    return {
        "schema_version": 1,
        "created_at": time.time(),
        "runtime_root": str(runtime_root),
        "binary_root": str(runtime_root / "maafw"),
        "binding_root": str(binding_root),
        "adb": str(adb),
        "device": device,
        "output_root": str(output_root),
        "max_seconds": max_seconds,
        "continue_on_failure": continue_on_failure,
        # C++ custom recognitions issue nested Controller RPC calls while the
        # parent waits for their response. Loopback TCP uses the same MaaAgent
        # protocol while avoiding the unreliable Windows AF_UNIX nested path.
        "agent_transport": "tcp" if os.name == "nt" else "ipc",
        "keep_device_awake": True,
        "resource_paths": _runtime_resource_paths(
            runtime_root,
            interface,
            controller_name="ADB",
            resource_name=resource_name,
        ),
        "agents": agents,
        "pi_env": pi_env,
        "tasks": requests,
        "task_metadata": metadata,
        "starts_upstream_ui": False,
        "changes_display_resolution": False,
        "installs_device_client": False,
        "experimental_adb_tasks": sorted(
            str(name).strip()
            for name in experimental_adb_tasks
            if str(name).strip()
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--binding-root", type=Path, default=_default_binding_root())
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--task", action="append")
    selection.add_argument("--preset")
    parser.add_argument(
        "--allow-undeclared-adb-task",
        action="append",
        default=[],
        help=(
            "Explicitly allow one selected task whose upstream Project Interface "
            "does not yet declare ADB support; the task name is recorded in request.json"
        ),
    )
    parser.add_argument("--options-file", type=Path)
    parser.add_argument("--max-seconds", type=float, default=1800.0)
    parser.add_argument("--stop-on-failure", action="store_true")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _parser().parse_args()
    runtime_root = args.runtime_root.expanduser().resolve()
    binding_root = args.binding_root.expanduser().resolve()
    adb = args.adb.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if args.max_seconds <= 0:
        raise ValueError("--max-seconds 必须为正数")
    output_root.mkdir(parents=True, exist_ok=True)
    file_options = _load_options_file(
        args.options_file.expanduser().resolve() if args.options_file else None
    )
    task_names = list(args.task or [])
    options_by_task = file_options
    if args.preset:
        interface = _load_interface_bundle(runtime_root)
        task_names, preset_options = expand_headless_preset(interface, args.preset)
        for task_name, raw_values in file_options.items():
            base = preset_options.get(task_name)
            if isinstance(base, dict) and isinstance(raw_values, dict):
                base.update(raw_values)
            else:
                preset_options[task_name] = raw_values
        options_by_task = preset_options
    request = build_headless_request(
        runtime_root=runtime_root,
        binding_root=binding_root,
        adb=adb,
        device=args.device,
        output_root=output_root,
        task_names=task_names,
        options_by_task=options_by_task,
        max_seconds=args.max_seconds,
        continue_on_failure=not args.stop_on_failure,
        experimental_adb_tasks=args.allow_undeclared_adb_task,
    )
    if args.preset:
        request["preset_name"] = args.preset
    request_path = output_root / "request.json"
    _write_json(request_path, request)
    stdout_path = output_root / "host.stdout.log"
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (source_root, str(binding_root), existing_pythonpath) if item
    )
    env["MAAFW_BINARY_PATH"] = str(runtime_root / "maafw")
    env["MAA_SCREENSHOT_VIEWPORT"] = "1280x720:adaptive"
    stop_path = output_root / "stop.requested"
    if stop_path.exists():
        stop_path.unlink()
    with stdout_path.open("wb") as stdout:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mobile_profiler.maaend_headless_host",
                "--request",
                str(request_path),
            ],
            cwd=str(runtime_root),
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            returncode = process.wait(timeout=args.max_seconds + 90.0)
        except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
            stop_path.write_text(
                json.dumps(
                    {"requested_at": time.time(), "reason": type(exc).__name__},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                returncode = process.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    returncode = process.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait(timeout=10.0)
    result_path = output_root / "host-result.json"
    if not result_path.is_file():
        raise RuntimeError(
            f"headless host 未生成结果，exit={returncode}; log={stdout_path}"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "successful": result.get("successful"),
                "status": result.get("status"),
                "error": result.get("error", ""),
                "tasks": result.get("tasks", []),
                "result": str(result_path),
                "events": str(output_root / "events.jsonl"),
                "stdout": str(stdout_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if returncode == 0 and result.get("successful") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
