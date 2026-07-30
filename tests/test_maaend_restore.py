from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MANAGED_DIRECTORIES = (
    "agent",
    "data",
    "locales",
    "maafw",
    "resource",
    "resource_adb",
    "resource_playcover",
    "resource_wlroots",
    "tasks",
)
MANAGED_FILES = ("MaaEnd.exe", "LICENSE", "README.md", "interface.json")


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "PowerShell test")
class MaaEndRestoreTests(unittest.TestCase):
    def test_restore_replaces_managed_tree_and_quarantines_webview_cache(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        script = repository / "tools" / "restore-maaend-runtime.ps1"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            staging = root / "staging"
            for target in (runtime, staging):
                for name in MANAGED_DIRECTORIES:
                    path = target / name
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "payload.bin").write_bytes(
                        f"{target.name}-{name}".encode()
                    )
                (target / "MaaEnd.exe").write_bytes(target.name.encode())
                (target / "LICENSE").write_text(
                    "GNU AFFERO GENERAL PUBLIC LICENSE VERSION 3",
                    encoding="utf-8",
                )
                (target / "README.md").write_text(target.name, encoding="utf-8")
            (runtime / "interface.json").write_text(
                json.dumps(
                    {
                        "name": "MaaEnd",
                        "version": "v2.21.0",
                        "mirrorchyan_rid": "MaaEnd",
                    }
                ),
                encoding="utf-8",
            )
            (staging / "interface.json").write_text(
                json.dumps(
                    {
                        "name": "MaaEnd",
                        "version": "v2.20.0",
                        "mirrorchyan_rid": "",
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "resource_adb" / "v221-only.json").write_text(
                "drift", encoding="utf-8"
            )
            (staging / "resource" / "v220-only.json").write_text(
                "restored", encoding="utf-8"
            )
            for name in ("config", "debug", "mobile-profiler-backups"):
                path = runtime / name
                path.mkdir(parents=True, exist_ok=True)
                (path / "preserved.txt").write_text(name, encoding="utf-8")
            webview = runtime / "cache" / "webview_data"
            webview.mkdir(parents=True)
            (webview / "update-state").write_text("v2.21.0", encoding="utf-8")
            (runtime / "cache" / "etag-index.json").write_text("{}", encoding="utf-8")
            (runtime / "MaaEnd.mobile-profiler-api-v2.20.exe").write_bytes(b"host")

            plan = self._run(script, "Plan", runtime, staging)
            self.assertEqual(plan["action"], "Plan")
            self.assertGreater(plan["differing_inventory_rows"], 0)
            self.assertTrue(plan["webview_cache_present"])

            result = self._run(script, "Restore", runtime, staging)

            self.assertTrue(result["verified"])
            self.assertTrue(result["webview_cache_quarantined"])
            self.assertFalse((runtime / "resource_adb" / "v221-only.json").exists())
            self.assertEqual(
                (runtime / "resource" / "v220-only.json").read_text(encoding="utf-8"),
                "restored",
            )
            self.assertFalse(webview.exists())
            self.assertTrue((runtime / "cache" / "etag-index.json").is_file())
            self.assertTrue((runtime / "MaaEnd.mobile-profiler-api-v2.20.exe").is_file())
            for name in ("config", "debug"):
                self.assertEqual(
                    (runtime / name / "preserved.txt").read_text(encoding="utf-8"),
                    name,
                )
            evidence = Path(result["evidence_root"])
            self.assertEqual(
                (evidence / "runtime" / "interface.json").read_text(encoding="utf-8"),
                (json.dumps(
                    {
                        "name": "MaaEnd",
                        "version": "v2.21.0",
                        "mirrorchyan_rid": "MaaEnd",
                    }
                )),
            )
            self.assertTrue((evidence / "cache" / "webview_data" / "update-state").is_file())
            self.assertTrue((evidence / "preflight.json").is_file())
            self.assertTrue((evidence / "result.json").is_file())

    @staticmethod
    def _run(
        script: Path,
        action: str,
        runtime: Path,
        staging: Path,
    ) -> dict[str, object]:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.fspath(script),
                "-Action",
                action,
                "-RuntimeRoot",
                os.fspath(runtime),
                "-StagingRoot",
                os.fspath(staging),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise AssertionError(f"unexpected restore output: {value!r}")
        return value


if __name__ == "__main__":
    unittest.main()
