"""File renaming and copying module."""

import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from modules.metadata import get_timestamp_from_json

logger = logging.getLogger(__name__)

# Folder name used when --cluster-by-json-date is active but a file has no
# matching JSON (and therefore no "Google date" to cluster by).
NO_JSON_DATE_FOLDER = "no_json_date"

# Characters illegal in filenames across common filesystems
ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Detects an already-applied "YYYY-MM-DD_HHMMSS_" prefix so we don't
# stack a second one on top when Google Takeout hands us a file that
# some earlier tool (or an album-folder export) already prefixed.
# Without this, a file named "2012-09-02_105509_SC20120902-105509.jpg"
# becomes "2012-09-02_105509_2012-09-02_105509_SC20120902-105509.jpg".
EXISTING_DATE_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{6}_"
)


def sanitize_filename(name: str) -> str:
    """Remove illegal filesystem characters from a filename."""
    return ILLEGAL_CHARS.sub("_", name)


def build_new_filename(original_path: Path, dt: datetime) -> str:
    """Build new filename: YYYY-MM-DD_HHMMSS_originalfilename.ext

    If the original filename is already prefixed with a YYYY-MM-DD_HHMMSS_
    pattern (happens when Google Takeout's album folders contain
    pre-prefixed copies, or when the tool is re-run over its own output),
    the existing prefix is stripped first so we don't stack two prefixes.
    """
    prefix = dt.strftime("%Y-%m-%d_%H%M%S")
    stem = sanitize_filename(original_path.stem)
    stem = EXISTING_DATE_PREFIX_RE.sub("", stem, count=1)
    ext = original_path.suffix.lower()
    return f"{prefix}_{stem}{ext}"


# ---------------------------------------------------------------------------
# Content-hash dedup (pre-copy, so each unique photo lands in the cluster
# folder exactly once regardless of how many Takeout subfolders it lived in)
# ---------------------------------------------------------------------------

def _md5_file(path_str: str) -> Tuple[str, Optional[str]]:
    """Return (path, md5_hex) for a given file path; md5 is None on error."""
    try:
        h = hashlib.md5()
        with open(path_str, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return path_str, h.hexdigest()
    except OSError:
        return path_str, None


def deduplicate_by_content(
    matched_files: List[Tuple[Path, Optional[Path]]],
    process_results: List[dict],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Tuple[Path, Optional[Path]]], List[dict], int]:
    """Collapse byte-identical input files down to one representative each.

    Google Takeout stores every photo in both its year-folder (``Fotos von
    2012/…``) AND in every album it belongs to (``Hochzeit 2012/…``). A file
    in two albums is therefore present three times. If we copy all of them
    to the cluster output folder, the user gets 2-5x the expected file count
    per day — which is exactly the "70 Dateien statt 35" symptom.

    Running the duplicate-detection phase AFTER copying would usually clean
    this up, but only if the pre-copy processing step writes byte-identical
    EXIF to every copy. In practice the original files can differ slightly
    (different orientation tag, different JFIF header bytes from Google's
    per-album re-encode, etc.), so post-copy MD5 dedup misses them even
    though they are perceptually the same photo.

    This pass hashes every matched input file in parallel and keeps only
    one entry per MD5. The survivor is the first occurrence, so album
    duplicates collapse into their year-folder original. Returned tuple:
    (deduped matched_files, aligned process_results, duplicates_dropped).
    """
    if not matched_files:
        return matched_files, process_results, 0

    paths = [str(mf[0]) for mf in matched_files]
    n_procs = max(1, min(cpu_count(), 8))
    hashes: dict = {}  # path -> md5
    completed = 0
    with Pool(processes=n_procs) as pool:
        for path_str, md5 in pool.imap_unordered(_md5_file, paths, chunksize=32):
            hashes[path_str] = md5
            completed += 1
            if progress_callback:
                progress_callback(completed, len(paths))

    seen: set = set()
    kept_files: List[Tuple[Path, Optional[Path]]] = []
    kept_results: List[dict] = []
    dropped = 0
    for (mp, jp), pr in zip(matched_files, process_results):
        md5 = hashes.get(str(mp))
        if md5 is None:
            # Hash failed (unreadable file); let it through, will fail later
            # in the copy phase with a clean error rather than being
            # silently dropped here.
            kept_files.append((mp, jp))
            kept_results.append(pr)
            continue
        if md5 in seen:
            dropped += 1
            continue
        seen.add(md5)
        kept_files.append((mp, jp))
        kept_results.append(pr)

    return kept_files, kept_results, dropped


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
    skip_if_exists: bool = False,
) -> Tuple[Optional[Path], str]:
    """Copy a media file to output with the new name.

    If ``cluster_folder`` is given (e.g. "2023-08-07"), the file is placed
    into ``<output_dir>/<cluster_folder>/`` instead of preserving the
    original Takeout subfolder structure. This is used by the
    ``--cluster-by-json-date`` mode so every file that Google Photos
    currently stamps with the same date ends up in the same folder,
    ready for a delete+re-upload round-trip.

    ``skip_if_exists``: when True, if a file with the EXACT same output
    filename already exists in the destination, skip this copy and return
    ("skipped_duplicate_name"). Used in cluster mode to collapse Google
    Takeout's year-folder + album-folder duplicates — identical filename
    at the identical capture timestamp is always the same logical photo,
    even if Google re-encoded the album copy so byte-MD5s diverge. When
    False (legacy, non-cluster mode) the old `_2`/`_3` suffix collision
    resolver keeps both files so nothing gets lost.

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

        raw_dest = dest_dir / new_name
        if skip_if_exists and raw_dest.exists():
            logger.debug(
                "Skip Takeout-duplicate (same output name already copied): %s",
                media_path,
            )
            return None, "skipped_duplicate_name"
        dest_path = resolve_collision(raw_dest)

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

    Priority (empirically matched against the Google Photos Grid View):

      1. ``exif_date`` — EXIF DateTimeOriginal's DATE component. This is
         the camera-local wall-clock date with no timezone conversion,
         and matches how Google Photos groups photos under the day header
         in the Grid View (verified with three live screenshots: a 2003
         photo with no EXIF TZ, a 2003 photo with EXIF TZ GMT-05:00, and
         a 2023 WhatsApp photo with EXIF TZ GMT+02:00 all appeared under
         their EXIF local date regardless of their UTC timestamp).

      2. ``json_date`` — formatted JSON photoTakenTime. Used when a file
         has no readable EXIF (e.g. videos or stripped metadata). The
         timezone conversion of this field is best-effort and may be one
         day off for the narrow class of files whose EXIF was stripped
         but whose Grid View date Google computes from the timestamp in
         some opaque TZ. Better than nothing for those.

      3. Re-read JSON as a last-ditch fallback on resume scenarios where
         process_file wasn't invoked.

      4. NO_JSON_DATE_FOLDER sentinel if absolutely nothing is known.
    """
    # Priority 1: EXIF DateTimeOriginal — the camera's local wall-clock
    # which Google reads as its primary Grid-view date source.
    exif_date = proc_result.get("exif_date")
    if exif_date and len(exif_date) >= 10:
        return exif_date[:10]

    # Priority 2: JSON photoTakenTime — already converted via the
    # configured TZ (google_tz.txt, default Pacific) when read by
    # metadata.get_timestamp_from_json. This is the correct fallback
    # for EXIF-less files because Google itself computes Grid dates
    # from the same timestamp in its server TZ. Notable case: WhatsApp
    # photos (IMG-YYYYMMDD-WAxxxx.jpg) have no EXIF, so the filename
    # parses to the local upload date (e.g. 12.03.) while Google
    # actually groups them under the UTC/Pacific date of the upload
    # moment (e.g. 11.03. — the message was sent just after midnight
    # Berlin time). Filename-based clustering would always mismatch
    # by one day for such files — JSON wins.
    json_date = proc_result.get("json_date")
    if json_date and len(json_date) >= 10:
        return json_date[:10]

    # Priority 3: Filename-embedded date — only used when neither EXIF
    # nor JSON gave us a date (rare: JSON-less scan from a very old
    # Takeout export, or a file the matcher couldn't pair). Strictly
    # inferior to JSON for Grid-matching but better than giving up.
    if proc_result.get("timestamp_source") == "filename":
        ts_used = proc_result.get("timestamp_used")
        if ts_used and len(ts_used) >= 10:
            return ts_used[:10]

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
    dedupe_by_content: bool = False,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> list:
    """Rename and copy all files to output directory.

    ``dedupe_by_content``: if True, collapse byte-identical input files
    down to a single entry via MD5 before copying — prevents Google
    Takeout's year-folder + album-folder duplicates from both landing in
    the cluster output. Only the first occurrence survives. Significantly
    cuts output size on libraries where most photos are in at least one
    album.

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

    # Collapse year-folder + album-folder copies of the same photo before
    # anything else. Each collapsed duplicate is emitted as a completed
    # rename_result with status "skipped_duplicate_content" so the caller's
    # progress bar advances correctly and the CSV reports the skip reason.
    if dedupe_by_content and matched_files:
        original_count = len(matched_files)
        matched_files, process_results, dropped = deduplicate_by_content(
            matched_files, process_results,
        )
        if dropped:
            logger.info(
                "Content-dedup: %d duplicate input files collapsed into "
                "%d unique (%.1f%% reduction).",
                dropped, len(matched_files),
                100.0 * dropped / max(original_count, 1),
            )
        # Advance progress bar for the dropped files so the UI's total
        # stays in sync with the caller's expectation (which was sized
        # against the pre-dedup matched_files length).
        if progress_callback and dropped:
            for _ in range(dropped):
                progress_callback("(dedup)", "skipped_duplicate_content")

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
            # In cluster mode, skip Takeout year-folder + album-folder
            # duplicates that would otherwise collide on the same output
            # name. Pre-copy MD5 dedup already handles byte-identical
            # copies; this catches Google's per-album re-encodes too,
            # because a collision on the output filename implies same
            # capture time + same original filename = same logical photo.
            skip_if_exists=bool(cluster_by_json_date),
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
