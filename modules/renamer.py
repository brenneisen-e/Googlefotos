"""File renaming and copying module."""

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from modules.metadata import get_timestamp_from_json

logger = logging.getLogger(__name__)

# Folder name used when --cluster-by-json-date is active but a file has no
# matching JSON (and therefore no "Google date" to cluster by).
NO_JSON_DATE_FOLDER = "no_json_date"

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
    cluster_folder: Optional[str] = None,
) -> Tuple[Optional[Path], str]:
    """Copy a media file to output with the new name.

    If ``cluster_folder`` is given (e.g. "2023-08-07"), the file is placed
    into ``<output_dir>/<cluster_folder>/`` instead of preserving the
    original Takeout subfolder structure. This is used by the
    ``--cluster-by-json-date`` mode so every file that Google Photos
    currently stamps with the same date ends up in the same folder,
    ready for a delete+re-upload round-trip.

    Returns (new_path, status_string).
    """
    try:
        new_name = build_new_filename(media_path, dt)

        if cluster_folder:
            subfolder = Path(cluster_folder)
        else:
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


def _resolve_cluster_folder(
    proc_result: dict,
    json_path: Optional[Path],
) -> str:
    """Return the YYYY-MM-DD folder name for cluster-by-json-date mode.

    Prefers the ``json_date`` field already stored in ``proc_result`` by
    ``metadata.process_file``. Falls back to re-reading the JSON sidecar
    (useful for resume scenarios where process_file was skipped). If no
    JSON date can be determined, returns the NO_JSON_DATE_FOLDER sentinel.
    """
    json_date = proc_result.get("json_date")
    if json_date and len(json_date) >= 10:
        return json_date[:10]

    if json_path:
        try:
            result = get_timestamp_from_json(json_path)
        except Exception as e:
            logger.debug("Cluster fallback: JSON re-read failed (%s): %s",
                         json_path, e)
            result = None
        if result:
            return result[0].strftime("%Y-%m-%d")

    return NO_JSON_DATE_FOLDER


def rename_all(
    matched_files: list,
    process_results: list,
    temp_dir: str,
    output_dir: str,
    cluster_by_json_date: bool = False,
    min_cluster_mismatches: int = 0,
    skip_no_json_date: bool = False,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> list:
    """Rename and copy all files to output directory.

    matched_files: list of (media_path, json_path_or_None)
    process_results: list of dicts from metadata.process_file()
    cluster_by_json_date: if True, place each file into a folder named
        after the date Google currently shows (the JSON photoTakenTime),
        e.g. ``2023-08-07/``. Files without a JSON go into
        ``no_json_date/`` (unless ``skip_no_json_date`` is set). This lets
        the user delete a whole day in the Google Photos UI and re-upload
        the matching folder in one round, which fixes cluster-misdated
        imports at scale.
    min_cluster_mismatches: in cluster mode, only copy days whose total
        mismatch count (``date_mismatch`` set on process_file results) is
        strictly greater than this number. Smaller clusters are skipped
        with status ``skipped_small_cluster``. Default 0 = copy every day.
    skip_no_json_date: in cluster mode, skip files that have no matched
        JSON sidecar (status ``skipped_no_json_date``) instead of copying
        them into ``no_json_date/``.
    progress_callback: optional callable ``f(filename, status)`` invoked
        once per processed file so the caller can drive a progress bar
        in the console. Large runs (100k+ files) otherwise look stuck
        during the copy phase.

    Returns list of dicts with rename info.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    rename_results = []

    # Pre-compute per-day mismatch counts when cluster filtering is active.
    # We only need this if the caller asked for a size threshold.
    eligible_days: Optional[set] = None
    if cluster_by_json_date and min_cluster_mismatches > 0:
        day_mismatches: dict = {}
        for (_, json_path), proc in zip(matched_files, process_results):
            cluster = _resolve_cluster_folder(proc, json_path)
            if cluster == NO_JSON_DATE_FOLDER:
                continue
            if proc.get("date_mismatch"):
                day_mismatches[cluster] = day_mismatches.get(cluster, 0) + 1
        eligible_days = {
            day for day, count in day_mismatches.items()
            if count > min_cluster_mismatches
        }
        logger.info(
            "Cluster filter: %d day(s) with >%d mismatches will be copied "
            "(out of %d day(s) seen)",
            len(eligible_days), min_cluster_mismatches, len(day_mismatches),
        )

    for (media_path, json_path), proc_result in zip(matched_files, process_results):
        cluster_folder = None
        if cluster_by_json_date:
            cluster_folder = _resolve_cluster_folder(proc_result, json_path)

            # Skip files without a matched JSON sidecar if requested.
            if skip_no_json_date and cluster_folder == NO_JSON_DATE_FOLDER:
                rename_results.append({
                    "original_path": str(media_path),
                    "new_path": None,
                    "new_filename": None,
                    "status": "skipped_no_json_date",
                })
                if progress_callback:
                    progress_callback(media_path.name, "skipped_no_json_date")
                continue

            # Skip files in clusters below the mismatch threshold.
            if (eligible_days is not None
                    and cluster_folder != NO_JSON_DATE_FOLDER
                    and cluster_folder not in eligible_days):
                rename_results.append({
                    "original_path": str(media_path),
                    "new_path": None,
                    "new_filename": None,
                    "status": "skipped_small_cluster",
                })
                if progress_callback:
                    progress_callback(media_path.name, "skipped_small_cluster")
                continue

        ts_str = proc_result.get("timestamp_used")
        if ts_str:
            # timestamp_used is already a local-time string; strptime gives
            # a naive datetime. We don't tag it because all we do with it
            # is strftime, which preserves the values verbatim.
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        else:
            # Fallback to file mtime, in LOCAL time (matches what Windows
            # Explorer / Finder show, and matches our cluster folder naming)
            mtime = os.path.getmtime(media_path)
            dt = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone()

        new_path, status = copy_and_rename(
            media_path, dt, temp_dir, output_dir,
            cluster_folder=cluster_folder,
        )

        rename_results.append({
            "original_path": str(media_path),
            "new_path": str(new_path) if new_path else None,
            "new_filename": new_path.name if new_path else None,
            "status": status,
        })
        if progress_callback:
            progress_callback(media_path.name, status)

    return rename_results
