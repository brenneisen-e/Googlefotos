"""EXIF writing and timestamp repair module."""

import json
import logging
import os
import re
import shutil
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import piexif
from PIL import Image

logger = logging.getLogger(__name__)

EXIF_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}
EXIFTOOL_EXTENSIONS = {".heic", ".heif", ".mp4", ".mov", ".avi", ".m4v"}
ALL_IMAGE_EXTENSIONS = EXIF_IMAGE_EXTENSIONS | {".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".mkv", ".wmv", ".flv", ".3gp"}

# Filename date patterns
FILENAME_DATE_PATTERNS = [
    # YYYYMMDD_HHMMSS
    (re.compile(r"(\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # YYYY-MM-DD_HH-MM-SS
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[_\-](\d{2})-(\d{2})-(\d{2})"), True),
    # YYYY-MM-DD HH:MM:SS
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})\s(\d{2}):(\d{2}):(\d{2})"), True),
    # IMG_YYYYMMDD_HHMMSS
    (re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # VID_YYYYMMDD_HHMMSS
    (re.compile(r"VID[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # Screenshot_YYYYMMDD-HHMMSS
    (re.compile(r"Screenshot[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    # YYYY-MM-DD only (no time)
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), False),
    # YYYYMMDD only (no time)
    (re.compile(r"(\d{4})(\d{2})(\d{2})"), False),
]

_exiftool_available: Optional[bool] = None


def check_exiftool() -> bool:
    """Check if ExifTool is available on the system."""
    global _exiftool_available
    if _exiftool_available is None:
        _exiftool_available = shutil.which("exiftool") is not None
    return _exiftool_available


def get_timestamp_from_json(json_path: Path) -> Optional[Tuple[datetime, str]]:
    """Extract timestamp from Google Takeout JSON sidecar.

    Returns (datetime, source_label) or None.
    Priority: photoTakenTime > creationTime.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read JSON %s: %s", json_path, e)
        return None

    # Priority 1: photoTakenTime
    pt = data.get("photoTakenTime", {})
    ts = pt.get("timestamp")
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if dt.year > 1970:  # Sanity check – epoch 0 means missing
                return dt, "json_photoTakenTime"
        except (ValueError, OSError):
            pass

    # Priority 2: creationTime
    ct = data.get("creationTime", {})
    ts = ct.get("timestamp")
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if dt.year > 1970:
                return dt, "json_creationTime"
        except (ValueError, OSError):
            pass

    return None


def get_timestamp_from_filename(filepath: Path) -> Optional[Tuple[datetime, str]]:
    """Try to parse a date/time from the filename."""
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
                if 1900 < dt.year < 2100:
                    return dt, "filename"
            except ValueError:
                continue

    return None


def get_timestamp_from_mtime(filepath: Path) -> Tuple[datetime, str]:
    """Fallback: use file modification time."""
    mtime = os.path.getmtime(filepath)
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return dt, "file_mtime"


def resolve_timestamp(media_path: Path, json_path: Optional[Path]) -> Tuple[datetime, str]:
    """Resolve the best timestamp for a media file.

    Priority: JSON photoTakenTime > JSON creationTime > filename parse > file mtime.
    """
    if json_path:
        result = get_timestamp_from_json(json_path)
        if result:
            return result

    result = get_timestamp_from_filename(media_path)
    if result:
        return result

    return get_timestamp_from_mtime(media_path)


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
    if not check_exiftool():
        return False

    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")

    try:
        result = subprocess.run(
            [
                "exiftool",
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


def process_file(media_path: Path, json_path: Optional[Path]) -> dict:
    """Process a single media file: resolve timestamp, write EXIF, set file times.

    Returns a dict with processing results.
    """
    result = {
        "original_path": str(media_path),
        "timestamp_used": None,
        "timestamp_source": None,
        "exif_written": False,
        "exiftool_used": False,
        "status": "ok",
    }

    try:
        dt, source = resolve_timestamp(media_path, json_path)
        result["timestamp_used"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        result["timestamp_source"] = source

        ext = media_path.suffix.lower()

        # Write EXIF based on file type
        if ext in EXIF_IMAGE_EXTENSIONS:
            result["exif_written"] = write_exif_piexif(media_path, dt)
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
