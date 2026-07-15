from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_evidence_attachment(run_dir: Path, source: Path, category: str) -> Path:
    source = source.resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"attachment does not exist or is not a file: {source}")
    safe_category = "".join(character for character in category if character.isalnum() or character in "-_") or "misc"
    destination_dir = run_dir.resolve() / "attachments" / safe_category
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        if destination.stat().st_size == source.stat().st_size and sha256_file(destination) == sha256_file(source):
            return destination
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = destination_dir / f"{source.stem}-{suffix}{source.suffix}"
    shutil.copy2(source, destination)
    return destination


def _iter_attachment_files(paths: Sequence[Path]) -> Iterable[tuple[Path, Path]]:
    for source in paths:
        source = source.resolve()
        if not source.exists():
            raise FileNotFoundError(f"attachment does not exist: {source}")
        if source.is_file():
            yield source, Path(source.name)
            continue
        for file_path in sorted(item for item in source.rglob("*") if item.is_file()):
            yield file_path, Path(source.name) / file_path.relative_to(source)


def create_evidence_archive(
    run_dir: Path,
    output_path: Optional[Path] = None,
    attachments: Sequence[Path] = (),
    force: bool = False,
) -> tuple[Path, Dict[str, object]]:
    run_dir = run_dir.resolve()
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run directory does not exist: {run_dir}")
    if not (run_dir / "metadata.json").exists():
        raise RuntimeError("run directory is missing metadata.json")
    if output_path is None:
        output_path = run_dir.parent / f"{run_dir.name}-evidence.zip"
    output_path = output_path.resolve()
    if output_path.exists() and not force:
        raise FileExistsError(f"archive already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    metadata: Dict[str, object] = {}
    try:
        value = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        if isinstance(value, dict):
            metadata = value
    except (OSError, ValueError, json.JSONDecodeError):
        metadata = {}

    entries: list[Dict[str, object]] = []
    prefix = Path(run_dir.name)
    files = sorted(item for item in run_dir.rglob("*") if item.is_file())
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for file_path in files:
            if file_path.resolve() == output_path or file_path.resolve() == temporary:
                continue
            relative = file_path.relative_to(run_dir)
            archive_name = (prefix / relative).as_posix()
            archive.write(file_path, archive_name)
            entries.append(
                {
                    "path": archive_name,
                    "size": file_path.stat().st_size,
                    "sha256": sha256_file(file_path),
                    "kind": "run_artifact",
                }
            )
        for file_path, relative in _iter_attachment_files(attachments):
            archive_name = (prefix / "attachments" / "external" / relative).as_posix()
            archive.write(file_path, archive_name)
            entries.append(
                {
                    "path": archive_name,
                    "size": file_path.stat().st_size,
                    "sha256": sha256_file(file_path),
                    "kind": "external_attachment",
                    "source": str(file_path),
                }
            )
        manifest: Dict[str, object] = {
            "format": "mobile-profiler-evidence-v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_name": run_dir.name,
            "source_directory": str(run_dir),
            "title": metadata.get("title"),
            "device": metadata.get("device"),
            "adb_serial": metadata.get("adb_serial"),
            "entry_count": len(entries),
            "entries": entries,
            "notes": (
                "SHA-256 hashes cover every archived run artifact and external attachment. "
                "Keep the full ZIP when handing data to an analyst or AI."
            ),
        }
        archive.writestr(
            (prefix / "evidence-manifest.json").as_posix(),
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    temporary.replace(output_path)
    return output_path, manifest
