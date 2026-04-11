"""EXIF writing and timestamp repair module.

Timestamp priority (filename-first):
  1. Parse from filename (highest trust – set by device at capture time)
  2. Google JSON photoTakenTime / creationTime
  3. Existing EXIF DateTimeOriginal
  4. File modification time → marked as FLAG

Cross-validation:
  If filename-date AND json-date both exist and delta > 30 days,
  log as "date_mismatch" (informational, filename date still wins).

Files with no valid timestamp after all 4 steps → status "no_timestamp".
"""

import json
import logging
import os
import re
import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

import piexif
from PIL import Image

logger = logging.getLogger(__name__)

EXIF_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}
EXIFTOOL_EXTENSIONS = {".heic", ".heif", ".mp4", ".mov", ".avi", ".m4v"}
ALL_IMAGE_EXTENSIONS = EXIF_IMAGE_EXTENSIONS | {".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".mkv", ".wmv", ".flv", ".3gp"}

# Cross-validation threshold: if filename and JSON dates differ by more
# than this many days, log as "date_mismatch".
MISMATCH_THRESHOLD_DAYS = 30

# ---------------------------------------------------------------------------
# Filename date patterns (ordered most-specific first)
# ---------------------------------------------------------------------------
FILENAME_DATE_PATTERNS = [
    # IMG_YYYYMMDD_HHMMSS (Android camera)
    (re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # IMG-YYYYMMDD-WA0003 (WhatsApp)
    (re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-]WA\d+"), False),
    # VID_YYYYMMDD_HHMMSS
    (re.compile(r"VID[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # PXL_YYYYMMDD_HHMMSS (Pixel camera)
    (re.compile(r"PXL[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # PANO_YYYYMMDD_HHMMSS (Panorama)
    (re.compile(r"PANO[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # MVIMG_YYYYMMDD_HHMMSS (Motion photo)
    (re.compile(r"MVIMG[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # Screenshot_YYYYMMDD-HHMMSS
    (re.compile(r"Screenshot[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # signal-YYYY-MM-DD-HHMMSS (Signal Messenger)
    (re.compile(r"signal[_\-](\d{4})-(\d{2})-(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # signal-YYYY-MM-DD-HH-MM-SS
    (re.compile(r"signal[_\-](\d{4})-(\d{2})-(\d{2})[_\-](\d{2})-(\d{2})-(\d{2})"), True),
    # YYYY-MM-DD_HH-MM-SS
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[_\-](\d{2})-(\d{2})-(\d{2})"), True),
    # YYYY-MM-DD HH:MM:SS
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})\s(\d{2}):(\d{2}):(\d{2})"), True),
    # YYYYMMDD_HHMMSS (generic)
    (re.compile(r"(\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # YYYY-MM-DD only (no time)
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), False),
    # YYYYMMDD only (no time) – last to avoid matching random 8-digit numbers
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"), False),
]

_exiftool_path: Optional[str] = None
_exiftool_checked: bool = False


def find_exiftool() -> Optional[str]:
    """Locate the ExifTool executable.

    Lookup order:
      1. System PATH (``shutil.which("exiftool")``)
      2. Project-local subdirectories matching ``exiftool*/`` — specifically
         looks for ``exiftool.exe`` (Windows, renamed) or ``exiftool`` (Unix)
         inside any such subdirectory, both in the current working directory
         and next to this package.

    Important: ExifTool's Windows archive ships as ``exiftool(-k).exe``.
    That ``(-k)`` activates the "pause before exit" flag and would make
    subprocess calls hang. This discovery intentionally only accepts the
    **renamed** ``exiftool.exe``. If only the ``(-k)`` version exists, a
    warning is logged once so the user knows what to do.

    Returns the absolute path as a string, or None if not found.
    Result is cached after the first call.
    """
    global _exiftool_path, _exiftool_checked
    if _exiftool_checked:
        return _exiftool_path
    _exiftool_checked = True

    # 1. System PATH
    path = shutil.which("exiftool")
    if path:
        _exiftool_path = path
        logger.info("ExifTool found on PATH: %s", path)
        return path

    # 2. Project-local exiftool*/ subdirectories
    search_roots = []
    search_roots.append(Path.cwd())
    try:
        # Package root = parent of modules/
        search_roots.append(Path(__file__).resolve().parent.parent)
    except Exception:
        pass

    seen_dirs = set()
    unrenamed_hits = []
    for root in search_roots:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for sub in sorted(root.glob("exiftool*")):
            if not sub.is_dir():
                continue
            key = str(sub.resolve())
            if key in seen_dirs:
                continue
            seen_dirs.add(key)

            # Preferred: the renamed executable
            for candidate_name in ("exiftool.exe", "exiftool"):
                candidate = sub / candidate_name
                if candidate.is_file():
                    _exiftool_path = str(candidate.resolve())
                    logger.info(
                        "ExifTool found in project subdirectory: %s",
                        _exiftool_path,
                    )
                    return _exiftool_path

            # Fallback detection: unrenamed "(-k)" version
            unrenamed = sub / "exiftool(-k).exe"
            if unrenamed.is_file():
                unrenamed_hits.append(str(unrenamed))

    if unrenamed_hits:
        logger.warning(
            "ExifTool NOT usable: only the unrenamed 'exiftool(-k).exe' was "
            "found at %s. Please rename it to 'exiftool.exe' (the '(-k)' "
            "flag would make subprocess calls hang). ExifTool_files/ must "
            "stay in the same folder.",
            unrenamed_hits[0],
        )

    return None


def check_exiftool() -> bool:
    """Return True if ExifTool is available (PATH or project-local)."""
    return find_exiftool() is not None


# ---------------------------------------------------------------------------
# Timestamp extraction from various sources
# ---------------------------------------------------------------------------

def _is_valid_timestamp(dt: datetime) -> bool:
    """Reject timestamps that are clearly wrong (epoch 0, future)."""
    return dt.year > 1970 and dt.year < 2100


def get_timestamp_from_filename(filepath: Path) -> Optional[Tuple[datetime, str]]:
    """Priority 1: Parse a date/time from the filename.

    The filename is the most trustworthy source because it's written
    by the device at capture time, before any cloud sync.
    """
    name = filepath.stem

    for pattern, has_time in FILENAME_DATE_PATTERNS:
        m = pattern.search(name)
        if m:
            groups = m.groups()
            try:
                if has_time and len(groups) >= 6:
                    dt = datetime(
                        int(groups[0]), int(groups[1]), int(groups[2]),
                        int(groups[3]), int(groups[4]), int(groups[5]),
                        tzinfo=timezone.utc,
                    )
                else:
                    dt = datetime(
                        int(groups[0]), int(groups[1]), int(groups[2]),
                        tzinfo=timezone.utc,
                    )
                if _is_valid_timestamp(dt):
                    return dt, "filename"
            except ValueError:
                continue

    return None


def get_timestamp_from_json(json_path: Path) -> Optional[Tuple[datetime, str]]:
    """Priority 2: Extract timestamp from Google Takeout JSON sidecar.

    Returns (datetime, source_label) or None.
    Order: photoTakenTime > creationTime.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read JSON %s: %s", json_path, e)
        return None

    for field, label in [
        ("photoTakenTime", "json_photoTakenTime"),
        ("creationTime", "json_creationTime"),
    ]:
        ts = data.get(field, {}).get("timestamp")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                if _is_valid_timestamp(dt):
                    return dt, label
            except (ValueError, OSError, OverflowError):
                pass

    return None


def get_timestamp_from_exif(filepath: Path) -> Optional[Tuple[datetime, str]]:
    """Priority 3: Read existing EXIF DateTimeOriginal from the file."""
    ext = filepath.suffix.lower()
    if ext not in (".jpg", ".jpeg", ".tiff", ".tif"):
        return None

    try:
        exif_dict = piexif.load(str(filepath))
        raw = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if raw:
            date_str = raw.decode("utf-8", errors="ignore").strip()
            if date_str and date_str != "0000:00:00 00:00:00":
                dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                if _is_valid_timestamp(dt):
                    return dt, "exif_original"
    except Exception:
        pass

    return None


def get_timestamp_from_mtime(filepath: Path) -> Tuple[datetime, str]:
    """Priority 4 (fallback): use file modification time."""
    mtime = os.path.getmtime(filepath)
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return dt, "file_mtime"


# ---------------------------------------------------------------------------
# Main resolver with cross-validation
# ---------------------------------------------------------------------------

def resolve_timestamp(
    media_path: Path,
    json_path: Optional[Path],
) -> Tuple[datetime, str, Optional[str], Optional[datetime]]:
    """Resolve the best timestamp for a media file.

    NEW PRIORITY:
      1. Filename (highest trust – set by device at capture time)
      2. Google JSON photoTakenTime / creationTime
      3. Existing EXIF DateTimeOriginal
      4. File modification time (flagged)

    Returns (datetime, source_label, mismatch_info_or_None, json_dt_or_None).
    mismatch_info is set when filename and JSON dates differ by >30 days.
    json_dt is the JSON photoTakenTime datetime (even when it did not win),
    so callers can cluster files by the date Google currently shows.
    """
    filename_result = get_timestamp_from_filename(media_path)
    json_result = get_timestamp_from_json(json_path) if json_path else None
    mismatch_info = None
    json_dt = json_result[0] if json_result else None

    # Cross-validation: check if filename and JSON dates diverge
    if filename_result and json_result:
        fn_dt = filename_result[0]
        js_dt = json_result[0]
        delta = abs((fn_dt - js_dt).days)
        if delta > MISMATCH_THRESHOLD_DAYS:
            mismatch_info = (
                f"date_mismatch: filename={fn_dt.strftime('%Y-%m-%d')} "
                f"json={js_dt.strftime('%Y-%m-%d')} delta={delta}d"
            )
            logger.info(
                "Date mismatch for %s: filename=%s json=%s (delta=%dd, using filename)",
                media_path.name,
                fn_dt.strftime("%Y-%m-%d"),
                js_dt.strftime("%Y-%m-%d"),
                delta,
            )

    # Priority 1: Filename
    if filename_result:
        return filename_result[0], filename_result[1], mismatch_info, json_dt

    # Priority 2: JSON
    if json_result:
        return json_result[0], json_result[1], mismatch_info, json_dt

    # Priority 3: Existing EXIF
    exif_result = get_timestamp_from_exif(media_path)
    if exif_result:
        return exif_result[0], exif_result[1], mismatch_info, json_dt

    # Priority 4: File mtime (flagged)
    mtime_dt, mtime_source = get_timestamp_from_mtime(media_path)
    return mtime_dt, mtime_source, mismatch_info, json_dt


# ---------------------------------------------------------------------------
# EXIF writing
# ---------------------------------------------------------------------------

def write_exif_piexif(filepath: Path, dt: datetime) -> bool:
    """Write EXIF dates to JPEG/PNG/WEBP/TIFF using piexif."""
    ext = filepath.suffix.lower()
    if ext not in EXIF_IMAGE_EXTENSIONS:
        return False

    # piexif only supports JPEG and TIFF natively
    if ext not in (".jpg", ".jpeg", ".tiff", ".tif"):
        return False

    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")

    try:
        exif_dict = piexif.load(str(filepath))
    except (piexif.InvalidImageDataError, ValueError, struct.error, Exception):
        # Corrupt EXIF – reset
        logger.debug("Corrupt EXIF in %s, resetting", filepath.name)
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    try:
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str.encode()
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str.encode()
        exif_dict["0th"][piexif.ImageIFD.DateTime] = date_str.encode()

        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(filepath))
        return True
    except Exception as e:
        # Last resort: try with a completely fresh EXIF dict
        logger.debug("EXIF write failed for %s, retrying with fresh dict: %s", filepath.name, e)
        try:
            fresh = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
            fresh["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str.encode()
            fresh["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str.encode()
            fresh["0th"][piexif.ImageIFD.DateTime] = date_str.encode()
            exif_bytes = piexif.dump(fresh)
            piexif.insert(exif_bytes, str(filepath))
            return True
        except Exception as e2:
            logger.warning("EXIF write ultimately failed for %s: %s", filepath.name, e2)
            return False


def write_exif_exiftool(filepath: Path, dt: datetime) -> bool:
    """Write EXIF dates to HEIC/video files using ExifTool subprocess."""
    exiftool_path = find_exiftool()
    if not exiftool_path:
        return False

    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")

    try:
        result = subprocess.run(
            [
                exiftool_path,
                f"-DateTimeOriginal={date_str}",
                f"-CreateDate={date_str}",
                "-overwrite_original",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("ExifTool timeout for %s", filepath.name)
        return False
    except OSError as e:
        logger.warning("ExifTool error for %s: %s", filepath.name, e)
        return False


def set_file_timestamps(filepath: Path, dt: datetime) -> bool:
    """Set file access and modification times."""
    try:
        ts = dt.timestamp()
        os.utime(str(filepath), (ts, ts))
        return True
    except OSError as e:
        logger.warning("Failed to set timestamps on %s: %s", filepath.name, e)
        return False


# ---------------------------------------------------------------------------
# Main per-file processing
# ---------------------------------------------------------------------------

def process_file(media_path: Path, json_path: Optional[Path]) -> dict:
    """Process a single media file: resolve timestamp, write EXIF, set file times.

    Returns a dict with processing results including cross-validation info.
    """
    result = {
        "original_path": str(media_path),
        "timestamp_used": None,
        "timestamp_source": None,
        "exif_written": False,
        "exiftool_used": False,
        "date_mismatch": None,
        "json_date": None,
        "status": "ok",
    }

    try:
        dt, source, mismatch, json_dt = resolve_timestamp(media_path, json_path)
        result["timestamp_used"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        result["timestamp_source"] = source
        result["date_mismatch"] = mismatch
        result["json_date"] = (
            json_dt.strftime("%Y-%m-%d %H:%M:%S") if json_dt else None
        )

        # Flag file_mtime as low-confidence
        if source == "file_mtime":
            result["status"] = "flag_mtime_only"

        ext = media_path.suffix.lower()

        # Write EXIF based on file type.
        #
        # Priority for image formats:
        #   1. piexif (fast, JPEG/TIFF only)
        #   2. ExifTool fallback (covers PNG/WebP, plus any JPEG/TIFF
        #      where piexif kapitulates on exotic/corrupt EXIF blocks —
        #      ExifTool is far more forgiving than piexif).
        if ext in EXIF_IMAGE_EXTENSIONS:
            written = write_exif_piexif(media_path, dt)
            result["exif_written"] = written
            if not written and check_exiftool():
                written_et = write_exif_exiftool(media_path, dt)
                if written_et:
                    result["exif_written"] = True
                    result["exiftool_used"] = True
                    logger.debug(
                        "ExifTool fallback succeeded for %s after piexif failed",
                        media_path.name,
                    )
        elif ext in EXIFTOOL_EXTENSIONS:
            written = write_exif_exiftool(media_path, dt)
            result["exif_written"] = written
            result["exiftool_used"] = written
            if not written and not check_exiftool():
                result["status"] = "exiftool_not_found"

        # Always set file timestamps
        set_file_timestamps(media_path, dt)

    except Exception as e:
        logger.error("Error processing %s: %s", media_path.name, e)
        result["status"] = f"error: {e}"

    return result
