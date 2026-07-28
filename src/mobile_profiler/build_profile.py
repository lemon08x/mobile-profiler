"""Build-edition metadata shared by source and portable distributions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional


BUILD_PROFILE_FILENAME = "_build_profile.json"
BUILD_PROFILE_SCHEMA_VERSION = 1
BUILD_EDITIONS = {"source", "standard", "full"}


def normalize_build_profile(
    value: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    """Return a validated profile; source checkouts default to all features."""

    source = dict(value or {})
    schema_version = source.get("schema_version", BUILD_PROFILE_SCHEMA_VERSION)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != BUILD_PROFILE_SCHEMA_VERSION
    ):
        raise ValueError(f"Unsupported Mobile Profiler build profile schema: {schema_version}")
    edition = str(source.get("edition") or "source").strip().lower()
    if edition not in BUILD_EDITIONS:
        raise ValueError(f"Unsupported Mobile Profiler build edition: {edition}")
    bundled_extras = source.get("bundled_extras", [])
    if not isinstance(bundled_extras, list):
        raise ValueError("build profile bundled_extras must be a list")
    extras = sorted(
        {
            str(item).strip()
            for item in bundled_extras
            if str(item).strip()
        }
    )
    profile: dict[str, object] = {
        "schema_version": BUILD_PROFILE_SCHEMA_VERSION,
        "edition": edition,
        "portable": edition != "source",
        "features": {
            "open_source_automation": edition != "standard",
        },
        "bundled_extras": extras,
    }
    generated_at = str(source.get("generated_at") or "").strip()
    if generated_at:
        profile["generated_at"] = generated_at
    return profile


def load_build_profile(package_root: Optional[Path] = None) -> dict[str, object]:
    """Load the generated portable profile or return the source default."""

    root = package_root or Path(__file__).resolve().parent
    path = root / BUILD_PROFILE_FILENAME
    if not path.is_file():
        return normalize_build_profile()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid Mobile Profiler build profile: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Mobile Profiler build profile must be an object: {path}")
    try:
        return normalize_build_profile(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid Mobile Profiler build profile: {path}: {exc}") from exc


CURRENT_BUILD_PROFILE = load_build_profile()


def current_build_profile() -> dict[str, object]:
    """Return an isolated JSON-compatible copy of the active build profile."""

    return json.loads(json.dumps(CURRENT_BUILD_PROFILE))


def open_source_automation_enabled(
    profile: Optional[Mapping[str, object]] = None,
) -> bool:
    normalized = (
        normalize_build_profile(profile)
        if profile is not None
        else CURRENT_BUILD_PROFILE
    )
    features = normalized.get("features", {})
    return bool(
        isinstance(features, dict)
        and features.get("open_source_automation") is True
    )
