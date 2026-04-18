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


def _load_json(json_path: Path) -> Optional[dict]:
    """Read and parse a JSON sidecar. Returns None on any error."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read JSON %s: %s", json_path, e)
        return None


def get_timestamp_from_json(json_path: Path) -> Optional[Tuple[datetime, str]]:
    """Priority 2: Extract timestamp from Google Takeout JSON sidecar.

    Returns (datetime, source_label) or None.
    Order: photoTakenTime > creationTime.

    The JSON timestamp is a true UTC unix epoch. We immediately convert it
    to the system's LOCAL timezone, because every downstream consumer wants
    the local date/time:

      - Google Photos displays photos under their local date — if we used
        UTC for the cluster folder name, photos taken late evening German
        time (= early next-day UTC) would land in the WRONG cluster folder
        and the user wouldn't find them in Google Photos under that date.
      - EXIF DateTimeOriginal by spec is local time without offset.
      - The "YYYY-MM-DD_HHMMSS" filename should match what the user sees.
    """
    data = _load_json(json_path)
    if data is None:
        return None

    for field, label in [
        ("photoTakenTime", "json_photoTakenTime"),
        ("creationTime", "json_creationTime"),
    ]:
        ts = data.get(field, {}).get("timestamp")
        if ts:
            try:
                dt_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                dt = dt_utc.astimezone()  # convert to system local tz
                if _is_valid_timestamp(dt):
                    return dt, label
            except (ValueError, OSError, OverflowError):
                pass

    return None


def _extract_gps(data: dict) -> Optional[Tuple[float, float, Optional[float]]]:
    """Extract (lat, lon, altitude) from a Google Takeout JSON dict.

    Prefers ``geoData`` (Google's current stored value) over ``geoDataExif``
    (what was in the file's EXIF at upload). Skips entries where lat AND lon
    are both exactly 0 — Google uses (0, 0) as the "no location" sentinel,
    and no real photo is taken in the Gulf of Guinea. Returns None if neither
    source has usable coordinates.
    """
    for field in ("geoData", "geoDataExif"):
        geo = data.get(field)
        if not isinstance(geo, dict):
            continue
        try:
            lat = float(geo.get("latitude", 0) or 0)
            lon = float(geo.get("longitude", 0) or 0)
        except (TypeError, ValueError):
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        alt_raw = geo.get("altitude")
        try:
            alt = float(alt_raw) if alt_raw is not None else None
        except (TypeError, ValueError):
            alt = None
        return lat, lon, alt
    return None


def get_metadata_from_json(json_path: Path) -> dict:
    """Read all re-uploadable metadata from a Google Takeout JSON.

    Returns a dict with:
      - ``description``: user-typed caption (str, possibly empty)
      - ``gps``: (lat, lon, altitude_or_None) tuple, or None if no location

    Fields Google Takeout exports but that CANNOT be re-uploaded via
    drag-and-drop (albums, favorited flag, archived flag, people tags,
    imageViews) are intentionally not surfaced here — they'd be dropped
    by Google Photos on re-ingest regardless.
    """
    result: dict = {"description": "", "gps": None}
    if not json_path:
        return result
    data = _load_json(json_path)
    if data is None:
        return result

    desc = data.get("description", "")
    if isinstance(desc, str):
        result["description"] = desc.strip()

    result["gps"] = _extract_gps(data)
    return result


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

def _deg_to_dms_rational(deg: float) -> tuple:
    """Convert a decimal degree (already absolute value) to DMS rationals.

    Returns ((d, 1), (m, 1), (s_num, 10000)) in the tuple-of-pairs format
    piexif expects for GPSLatitude / GPSLongitude.
    """
    abs_deg = abs(float(deg))
    d = int(abs_deg)
    m_float = (abs_deg - d) * 60
    m = int(m_float)
    s_float = (m_float - m) * 60
    s_num = int(round(s_float * 10000))
    # Carry over if rounding pushed seconds to 60
    if s_num >= 60 * 10000:
        s_num = 0
        m += 1
        if m >= 60:
            m = 0
            d += 1
    return ((d, 1), (m, 1), (s_num, 10000))


def _apply_gps_piexif(exif_dict: dict, gps: Tuple[float, float, Optional[float]]) -> None:
    """Populate the GPS IFD of a piexif dict in-place from (lat, lon, alt)."""
    lat, lon, alt = gps
    gps_ifd = exif_dict.setdefault("GPS", {})
    gps_ifd[piexif.GPSIFD.GPSVersionID] = (2, 3, 0, 0)
    gps_ifd[piexif.GPSIFD.GPSLatitudeRef] = (b"N" if lat >= 0 else b"S")
    gps_ifd[piexif.GPSIFD.GPSLatitude] = _deg_to_dms_rational(lat)
    gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = (b"E" if lon >= 0 else b"W")
    gps_ifd[piexif.GPSIFD.GPSLongitude] = _deg_to_dms_rational(lon)
    if alt is not None:
        gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt >= 0 else 1
        # Altitude as rational meters with mm precision
        alt_num = int(round(abs(alt) * 1000))
        gps_ifd[piexif.GPSIFD.GPSAltitude] = (alt_num, 1000)


def write_exif_piexif(
    filepath: Path,
    dt: datetime,
    description: str = "",
    gps: Optional[Tuple[float, float, Optional[float]]] = None,
) -> bool:
    """Write EXIF dates, optional description, and optional GPS to JPEG/TIFF.

    ``description`` and ``gps`` come from the Google Takeout JSON sidecar.
    Both are skipped silently when empty/None so we never blank out existing
    values.
    """
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

    def _fill(d: dict) -> None:
        d["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str.encode()
        d["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str.encode()
        d["0th"][piexif.ImageIFD.DateTime] = date_str.encode()
        if description:
            # ImageDescription is ASCII in EXIF; encode with replacement so
            # umlauts don't raise. XMP/UserComment would preserve unicode,
            # but piexif can't write XMP and UserComment needs a charset
            # prefix — ExifTool fallback (below, for non-JPEG) handles both.
            d["0th"][piexif.ImageIFD.ImageDescription] = description.encode(
                "ascii", errors="replace"
            )
        if gps:
            _apply_gps_piexif(d, gps)

    try:
        _fill(exif_dict)
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(filepath))
        return True
    except Exception as e:
        # Last resort: try with a completely fresh EXIF dict
        logger.debug("EXIF write failed for %s, retrying with fresh dict: %s", filepath.name, e)
        try:
            fresh = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
            _fill(fresh)
            exif_bytes = piexif.dump(fresh)
            piexif.insert(exif_bytes, str(filepath))
            return True
        except Exception as e2:
            logger.warning("EXIF write ultimately failed for %s: %s", filepath.name, e2)
            return False


def write_exif_exiftool(
    filepath: Path,
    dt: datetime,
    description: str = "",
    gps: Optional[Tuple[float, float, Optional[float]]] = None,
) -> bool:
    """Write EXIF dates, optional description, and optional GPS via ExifTool.

    Used for HEIC/HEIF, videos (MP4/MOV/AVI/M4V), and as a fallback when
    piexif fails on exotic EXIF. Unlike piexif this path supports unicode
    descriptions and quicktime metadata tags.
    """
    exiftool_path = find_exiftool()
    if not exiftool_path:
        return False

    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    ext = filepath.suffix.lower()
    is_video = ext in VIDEO_EXTENSIONS

    args = [
        exiftool_path,
        f"-DateTimeOriginal={date_str}",
        f"-CreateDate={date_str}",
        f"-ModifyDate={date_str}",
    ]
    if is_video:
        # QuickTime containers use their own date atoms in addition to EXIF.
        # -api QuickTimeUTC=1 tells ExifTool to treat the value as UTC.
        args.extend([
            f"-TrackCreateDate={date_str}",
            f"-TrackModifyDate={date_str}",
            f"-MediaCreateDate={date_str}",
            f"-MediaModifyDate={date_str}",
            "-api", "QuickTimeUTC=1",
        ])

    if description:
        args.append(f"-ImageDescription={description}")
        args.append(f"-XMP:Description={description}")
        if is_video:
            # QuickTime description atoms
            args.append(f"-Description={description}")

    if gps:
        lat, lon, alt = gps
        args.extend([
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lon)}",
            f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ])
        if alt is not None:
            args.extend([
                f"-GPSAltitude={abs(alt)}",
                f"-GPSAltitudeRef={'0' if alt >= 0 else '1'}",
            ])

    args.extend(["-overwrite_original", str(filepath)])

    try:
        result = subprocess.run(
            args,
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
        "gps_written": False,
        "description_written": False,
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

        # Pull optional GPS + description from the JSON sidecar so we can
        # write them back into the file's EXIF/XMP. Without this step,
        # deleting and re-uploading to Google Photos would strip location
        # and user captions entirely.
        json_meta = get_metadata_from_json(json_path) if json_path else {
            "description": "", "gps": None,
        }
        description = json_meta.get("description") or ""
        gps = json_meta.get("gps")

        ext = media_path.suffix.lower()

        # Write EXIF based on file type.
        #
        # Priority for image formats:
        #   1. piexif (fast, JPEG/TIFF only — writes date + GPS + ASCII desc)
        #   2. ExifTool fallback (covers PNG/WebP, plus any JPEG/TIFF
        #      where piexif kapitulates on exotic/corrupt EXIF blocks —
        #      ExifTool is far more forgiving than piexif and also writes
        #      XMP:Description so unicode captions round-trip correctly).
        if ext in EXIF_IMAGE_EXTENSIONS:
            written = write_exif_piexif(
                media_path, dt, description=description, gps=gps,
            )
            result["exif_written"] = written
            if written:
                if gps:
                    result["gps_written"] = True
                if description:
                    result["description_written"] = True
            if not written and check_exiftool():
                written_et = write_exif_exiftool(
                    media_path, dt, description=description, gps=gps,
                )
                if written_et:
                    result["exif_written"] = True
                    result["exiftool_used"] = True
                    if gps:
                        result["gps_written"] = True
                    if description:
                        result["description_written"] = True
                    logger.debug(
                        "ExifTool fallback succeeded for %s after piexif failed",
                        media_path.name,
                    )
        elif ext in EXIFTOOL_EXTENSIONS:
            written = write_exif_exiftool(
                media_path, dt, description=description, gps=gps,
            )
            result["exif_written"] = written
            result["exiftool_used"] = written
            if written:
                if gps:
                    result["gps_written"] = True
                if description:
                    result["description_written"] = True
            if not written and not check_exiftool():
                result["status"] = "exiftool_not_found"

        # Always set file timestamps
        set_file_timestamps(media_path, dt)

    except Exception as e:
        logger.error("Error processing %s: %s", media_path.name, e)
        result["status"] = f"error: {e}"

    return result
