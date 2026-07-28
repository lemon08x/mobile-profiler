from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


FRAMEWORK_FILES = (
    "MaaFramework.dll",
    "MaaAdbControlUnit.dll",
    "MaaUtils.dll",
    "MaaToolkit.dll",
    "MaaAgentClient.dll",
    "MaaAgentServer.dll",
)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "PowerShell test")
class MaaEndDeployTests(unittest.TestCase):
    def test_deploy_and_rollback_cover_the_complete_version_contract(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        script = repository / "tools" / "deploy-maaend-viewport.ps1"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            framework = root / "framework"
            artifacts = root / "artifacts"
            for path in (
                runtime / "maafw",
                runtime / "agent",
                runtime / "config",
                framework,
                artifacts,
            ):
                path.mkdir(parents=True, exist_ok=True)

            destinations: dict[Path, bytes] = {}
            for name in FRAMEWORK_FILES:
                old = f"old-{name}".encode()
                new = f"new-{name}".encode()
                destination = runtime / "maafw" / name
                destination.write_bytes(old)
                (framework / name).write_bytes(new)
                destinations[destination] = old
            for relative in ("agent/go-service.exe", "agent/cpp-algo.exe"):
                destination = runtime / relative
                old = f"old-{relative}".encode()
                destination.write_bytes(old)
                source = artifacts / Path(relative).name
                source.write_bytes(f"new-{relative}".encode())
                destinations[destination] = old
            runtime_config = runtime / "config" / "mxu-MaaEnd.json"
            old_config = b'{"version":"old"}'
            runtime_config.write_bytes(old_config)
            destinations[runtime_config] = old_config
            new_config = artifacts / "mxu-MaaEnd.json"
            new_config.write_bytes(b'{"version":"new"}')
            runtime_interface = runtime / "interface.json"
            old_interface = b'{"name":"MaaEnd","version":"v2.20.0","mirrorchyan_rid":"MaaEnd"}'
            runtime_interface.write_bytes(old_interface)
            destinations[runtime_interface] = old_interface
            new_interface = artifacts / "interface.json"
            new_interface.write_bytes(
                b'{"name":"MaaEnd","version":"v2.20.0","mirrorchyan_rid":""}'
            )

            deploy = self._run_script(
                script,
                "Deploy",
                runtime,
                "-FrameworkBin",
                framework,
                "-AgentExecutable",
                artifacts / "go-service.exe",
                "-CppAgentExecutable",
                artifacts / "cpp-algo.exe",
                "-MxuConfig",
                new_config,
                "-InterfaceFile",
                new_interface,
            )
            backup = Path(deploy["backup_root"])

            self.assertEqual(deploy["action"], "Deploy")
            self.assertEqual(len(deploy["files"]), 10)
            self.assertEqual(len(deploy["backup_files"]), 10)
            self.assertTrue(backup.is_relative_to(runtime / "mobile-profiler-backups"))
            for row in deploy["files"]:
                path = Path(row["destination"])
                self.assertTrue(row["verified"])
                self.assertEqual(
                    row["sha256"].casefold(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            for destination, old in destinations.items():
                self.assertNotEqual(destination.read_bytes(), old)

            rollback = self._run_script(
                script,
                "Rollback",
                runtime,
                "-BackupRoot",
                backup,
            )

            self.assertEqual(rollback["action"], "Rollback")
            self.assertEqual(len(rollback["files"]), 10)
            for destination, old in destinations.items():
                self.assertEqual(destination.read_bytes(), old)

    @staticmethod
    def _run_script(
        script: Path,
        action: str,
        runtime: Path,
        *arguments: object,
    ) -> dict[str, object]:
        command = [
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
        ]
        command.extend(os.fspath(value) if isinstance(value, Path) else str(value) for value in arguments)
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise AssertionError(f"unexpected deployment output: {value!r}")
        return value


if __name__ == "__main__":
    unittest.main()
