from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mobile_profiler.build_profile import (
    BUILD_PROFILE_FILENAME,
    load_build_profile,
    normalize_build_profile,
)


class BuildProfileTests(unittest.TestCase):
    def test_source_checkout_defaults_to_full_feature_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = load_build_profile(Path(directory))

        self.assertEqual(profile["edition"], "source")
        self.assertFalse(profile["portable"])
        self.assertTrue(profile["features"]["open_source_automation"])

    def test_standard_profile_fails_closed_for_open_source_automation(self) -> None:
        profile = normalize_build_profile(
            {
                "edition": "standard",
                "portable": True,
                "features": {"open_source_automation": True},
                "bundled_extras": ["uiautomator2", "uiautomator2"],
            }
        )

        self.assertEqual(profile["edition"], "standard")
        self.assertTrue(profile["portable"])
        self.assertFalse(profile["features"]["open_source_automation"])
        self.assertEqual(profile["bundled_extras"], ["uiautomator2"])

    def test_generated_full_profile_is_loaded_from_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / BUILD_PROFILE_FILENAME).write_text(
                json.dumps(
                    {
                        "edition": "full",
                        "portable": True,
                        "bundled_extras": ["image", "uiautomator2"],
                    }
                ),
                encoding="utf-8",
            )
            profile = load_build_profile(root)

        self.assertEqual(profile["edition"], "full")
        self.assertTrue(profile["features"]["open_source_automation"])
        self.assertEqual(profile["bundled_extras"], ["image", "uiautomator2"])

    def test_invalid_generated_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / BUILD_PROFILE_FILENAME).write_text(
                '{"edition":"unknown"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Invalid Mobile Profiler"):
                load_build_profile(root)

    def test_unknown_generated_profile_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / BUILD_PROFILE_FILENAME).write_text(
                '{"schema_version":2,"edition":"standard"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "profile schema"):
                load_build_profile(root)


if __name__ == "__main__":
    unittest.main()
