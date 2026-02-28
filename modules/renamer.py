"""File renaming and copying module."""

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Characters illegal in filenames across common filesystems
ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Remove illegal filesystem characters from a filename."""
    return ILLEGAL_CHARS.sub("_", name)


def build_new_filename(original_path: Path, dt: datetime) -> str:
    """Build new filename: YYYY-MM-DD_HHMMSS_originalfilename.ext"""
    prefix = dt.strftime("%Y-%m-%d_%H%M%S")
    stem = sanitize_filename(original_path.stem)
    ext = original_path.suffix.lower()
    return f"{prefix}_{stem}{ext}"


def resolve_collision(dest_path: Path) -> Path:
    """If dest_path exists, append _2, _3, etc. until unique."""
    if not dest_path.exists():
        return dest_path

    stem = dest_path.stem
    ext = dest_path.suffix
    parent = dest_path.parent
    counter = 2

    while True:
        candidate = parent / f"{stem}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def get_relative_subfolder(media_path: Path, temp_dir: str) -> Path:
    """Preserve the subfolder structure from the Takeout extraction."""
    try:
        rel = media_path.relative_to(temp_dir)
        # Return just the directory part (not the filename)
        return rel.parent
    except ValueError:
        return Path("")


def copy_and_rename(
    media_path: Path,
    dt: datetime,
    temp_dir: str,
    output_dir: str,
) -> Tuple[Optional[Path], str]:
    """Copy a media file to output with the new name and preserved subfolder structure.

    Returns (new_path, status_string).
    """
    try:
        new_name = build_new_filename(media_path, dt)
        subfolder = get_relative_subfolder(media_path, temp_dir)

        dest_dir = Path(output_dir) / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = resolve_collision(dest_dir / new_name)

        shutil.copy2(str(media_path), str(dest_path))

        # Verify copy
        if dest_path.exists() and dest_path.stat().st_size > 0:
            logger.debug("Copied: %s → %s", media_path.name, dest_path.name)
            return dest_path, "ok"
        else:
            logger.error("Copy verification failed for %s", media_path.name)
            return None, "copy_failed"

    except OSError as e:
        logger.error("Failed to copy %s: %s", media_path.name, e)
        return None, f"error: {e}"


def rename_all(
    matched_files: list,
    process_results: list,
    temp_dir: str,
    output_dir: str,
) -> list:
    """Rename and copy all files to output directory.

    matched_files: list of (media_path, json_path_or_None)
    process_results: list of dicts from metadata.process_file()

    Returns list of dicts with rename info.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    rename_results = []

    for (media_path, _json_path), proc_result in zip(matched_files, process_results):
        ts_str = proc_result.get("timestamp_used")
        if ts_str:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        else:
            # Fallback to file mtime
            mtime = os.path.getmtime(media_path)
            dt = datetime.fromtimestamp(mtime, tz=timezone.utc)

        new_path, status = copy_and_rename(media_path, dt, temp_dir, output_dir)

        rename_results.append({
            "original_path": str(media_path),
            "new_path": str(new_path) if new_path else None,
            "new_filename": new_path.name if new_path else None,
            "status": status,
        })

    return rename_results
