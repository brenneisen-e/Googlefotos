#!/usr/bin/env python3
"""
Google Photos Metadata Analyzer

Extracts Google Takeout ZIPs in the current directory, scans all photos/videos,
and generates an Excel report of files where metadata dates diverge
(filename vs JSON sidecar vs EXIF vs file modification time).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import struct
import sys
import urllib.parse
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover – Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Google Photos groups photos by the user's LOCAL day, not UTC. A photo
# shot on 2024-01-01 00:30 CET is 2023-12-31 23:30 UTC — if we cluster by
# the UTC day we'd put it on the wrong date and undercount (420 files on
# our side vs. 417 in Google Photos, because a handful of midnight shots
# slid into the neighbouring day).
LOCAL_TZ = ZoneInfo("Europe/Berlin")


def to_local(dt: datetime) -> datetime:
    """Return ``dt`` converted to the user's local timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def _fmt_local(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a datetime in local time, or "" if None."""
    if dt is None:
        return ""
    return to_local(dt).strftime(fmt)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("openpyxl nicht installiert. Bitte installieren mit:  pip install openpyxl")
    sys.exit(1)

try:
    import piexif
except ImportError:
    piexif = None

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
)

console = Console()
logger = logging.getLogger("analyze")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif",
    ".heic", ".heif", ".gif", ".bmp", ".raw", ".cr2",
    ".nef", ".arw", ".dng",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".m4v", ".mkv", ".wmv",
    ".flv", ".3gp",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

MISMATCH_THRESHOLD_DAYS = 30

GOOGLE_TRUNCATE_LEN = 46

# Filename date patterns (most-specific first)
FILENAME_DATE_PATTERNS = [
    (re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-]WA\d+"), False),
    (re.compile(r"VID[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"PXL[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"PANO[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"MVIMG[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"Screenshot[_\-](\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"signal[_\-](\d{4})-(\d{2})-(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"signal[_\-](\d{4})-(\d{2})-(\d{2})[_\-](\d{2})-(\d{2})-(\d{2})"), True),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[_\-](\d{2})-(\d{2})-(\d{2})"), True),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})\s(\d{2}):(\d{2}):(\d{2})"), True),
    (re.compile(r"(\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"), True),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), False),
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"), False),
]


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------

def extract_zips(base_dir: Path, temp_dir: Path) -> int:
    """Extract ZIPs one-by-one into temp_dir, deleting each ZIP after
    extraction to save disk space.

    Returns number of extracted ZIPs.
    """
    zip_files = sorted(base_dir.glob("*.zip"))
    if not zip_files:
        console.print("[yellow]Keine ZIP-Dateien gefunden.[/yellow]")
        return 0

    temp_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("ZIPs entpacken", total=len(zip_files))
        for zf in zip_files:
            dest = temp_dir / zf.stem
            dest.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(zf, "r") as z:
                    z.extractall(dest)
                extracted += 1
                console.print(f"  [green]Entpackt:[/green] {zf.name}")

                # Handle nested ZIPs
                for nested in list(dest.rglob("*.zip")):
                    ndest = nested.parent / nested.stem
                    ndest.mkdir(parents=True, exist_ok=True)
                    try:
                        with zipfile.ZipFile(nested, "r") as nz:
                            nz.extractall(ndest)
                        nested.unlink()
                    except (zipfile.BadZipFile, OSError):
                        pass

                # Delete the original ZIP to free disk space
                zf.unlink()
                console.print(f"  [dim]Gelöscht:[/dim] {zf.name} (Platz freigegeben)")

            except (zipfile.BadZipFile, OSError) as e:
                logger.error("ZIP übersprungen: %s – %s", zf.name, e)
            progress.advance(task)

    return extracted


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _valid(dt: datetime) -> bool:
    return 1970 < dt.year < 2100


def date_from_filename(filepath: Path) -> Optional[datetime]:
    name = filepath.stem
    for pattern, has_time in FILENAME_DATE_PATTERNS:
        m = pattern.search(name)
        if m:
            g = m.groups()
            try:
                if has_time and len(g) >= 6:
                    dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                                  int(g[3]), int(g[4]), int(g[5]),
                                  tzinfo=timezone.utc)
                else:
                    dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                                  tzinfo=timezone.utc)
                if _valid(dt):
                    return dt
            except ValueError:
                continue
    return None


def date_from_json(json_path: Path) -> Optional[datetime]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    for field in ("photoTakenTime", "creationTime"):
        ts = data.get(field, {}).get("timestamp")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                if _valid(dt):
                    return dt
            except (ValueError, OSError, OverflowError):
                pass
    return None


def date_from_exif(filepath: Path) -> Optional[datetime]:
    ext = filepath.suffix.lower()
    if ext not in (".jpg", ".jpeg", ".tiff", ".tif", ".png", ".webp"):
        return None

    def _parse_exif_date(s: str) -> Optional[datetime]:
        """Parse common EXIF date string formats."""
        s = s.strip().rstrip("\x00")
        if not s or s == "0000:00:00 00:00:00":
            return None
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                     "%Y:%m:%d", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                if _valid(dt):
                    return dt
            except ValueError:
                continue
        return None

    # Method 1: piexif (fast, JPEG/TIFF only)
    if piexif is not None and ext in (".jpg", ".jpeg", ".tiff", ".tif"):
        try:
            exif_dict = piexif.load(str(filepath))
            for tag in (piexif.ExifIFD.DateTimeOriginal,
                        piexif.ExifIFD.DateTimeDigitized):
                raw = exif_dict.get("Exif", {}).get(tag)
                if raw:
                    dt = _parse_exif_date(raw.decode("utf-8", errors="ignore"))
                    if dt:
                        return dt
            # Also check 0th IFD DateTime
            raw = exif_dict.get("0th", {}).get(piexif.ImageIFD.DateTime)
            if raw:
                dt = _parse_exif_date(raw.decode("utf-8", errors="ignore"))
                if dt:
                    return dt
        except Exception:
            pass

    # Method 2: Pillow fallback (works for more formats, more robust)
    try:
        from PIL import Image
        from PIL.ExifTags import Base as ExifBase
        with Image.open(filepath) as img:
            exif_data = img.getexif()
            if exif_data:
                # Check DateTimeOriginal (tag 36867), DateTimeDigitized (36868),
                # DateTime (306)
                for tag_id in (36867, 36868, 306):
                    val = exif_data.get(tag_id)
                    if val and isinstance(val, str):
                        dt = _parse_exif_date(val)
                        if dt:
                            return dt
    except Exception:
        pass

    return None


def date_from_video_metadata(filepath: Path) -> Optional[datetime]:
    """Read creation date from video container metadata via ffprobe."""
    ext = filepath.suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        return None
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "format_tags=creation_time",
             str(filepath)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            ct = data.get("format", {}).get("tags", {}).get("creation_time", "")
            if ct:
                # Typical format: "2025-06-10T01:11:31.000000Z"
                for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                            "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(ct, fmt).replace(tzinfo=timezone.utc)
                        if _valid(dt):
                            return dt
                    except ValueError:
                        continue
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return None


def date_from_mtime(filepath: Path) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(filepath), tz=timezone.utc)


# ---------------------------------------------------------------------------
# JSON sidecar matching (reused from modules/matcher.py logic)
# ---------------------------------------------------------------------------

# Cache for JSON maps per directory (avoids re-scanning the same folder)
_json_map_cache: dict = {}
# Per-directory title index: maps normalized title → json Path
_dir_title_cache: dict = {}
# Global title index: maps normalized title → json Path (built lazily)
_title_index: dict = {}
_title_index_built = False


def _get_json_map(directory: Path) -> dict:
    """Build/return a case-insensitive map of all JSON files in directory."""
    key = str(directory)
    if key in _json_map_cache:
        return _json_map_cache[key]
    json_map = {}
    try:
        for f in directory.iterdir():
            if f.is_file() and f.name.lower().endswith(".json"):
                json_map[f.name.lower()] = f
    except OSError:
        pass
    _json_map_cache[key] = json_map
    return json_map


# Characters Google replaces with _ in exported filenames
# (but the JSON title field keeps the originals)
_SPECIAL_CHAR_MAP = str.maketrans("&?;'", "____")

# Equivalent extensions (.jpg ↔ .jpeg)
_EXT_EQUIVALENTS = {
    ".jpg": [".jpeg"],
    ".jpeg": [".jpg"],
}


def _find_json_in_map(json_map: dict, media_name: str, media_stem: str,
                       media_suffix: str) -> Optional[Path]:
    """Core matching logic against a specific json_map.

    Uses PREFIX-BASED matching: for a media file "photo.jpg", any JSON file
    whose name starts with "photo.jpg." and ends with ".json" is a match.
    This catches .json, .supplemental-metadata.json, and ALL truncation variants
    without needing to enumerate them.
    """
    def lookup(candidate: str) -> Optional[Path]:
        return json_map.get(candidate.lower())

    def prefix_match(prefix: str) -> Optional[Path]:
        """Find any JSON file starting with prefix and ending with .json."""
        prefix_lower = prefix.lower() + "."
        for json_name, json_file in json_map.items():
            if json_name.startswith(prefix_lower) and json_name.endswith(".json"):
                return json_file
        return None

    # --- Rule 1: Prefix match (catches .json AND .supplemental-metadata.json AND all truncations) ---
    # photo.jpg → matches photo.jpg.json, photo.jpg.supplemental-metadata.json,
    #             photo.jpg.supplemental-me.json, photo.jpg.s.json, etc.
    r = prefix_match(media_name)
    if r:
        return r

    # --- Rule 2: Stem only (photo.jpg → photo.json) ---
    r = lookup(media_stem + ".json")
    if r:
        return r

    # --- Rule 3: Extension equivalents (.jpg ↔ .jpeg) ---
    alt_exts = _EXT_EQUIVALENTS.get(media_suffix.lower(), [])
    for alt_ext in alt_exts:
        alt_name = media_stem + alt_ext
        r = prefix_match(alt_name)
        if r:
            return r

    # --- Rule 4: Truncated filenames (46/47/51 char limits) ---
    for trunc_len in (46, 47, 51):
        if len(media_name) > trunc_len:
            r = prefix_match(media_name[:trunc_len])
            if r:
                return r

        if len(media_stem) > trunc_len:
            r = prefix_match(media_stem[:trunc_len] + media_suffix)
            if r:
                return r

    # --- Rule 5: Numbered duplicates ---
    # Google places the number differently:
    #   Media:  photo(1).jpg
    #   JSON:   photo.jpg(1).json  OR  photo.JPG(1).supplemental-metadata.json
    m = re.match(r"^(.+)\((\d+)\)$", media_stem)
    if m:
        base, num = m.group(1), m.group(2)
        # Try with various extension cases
        for ext_variant in {media_suffix, media_suffix.upper(), media_suffix.lower()}:
            r = prefix_match(f"{base}{ext_variant}({num})")
            if r:
                return r
        # Also try: base(N) directly (stem-only variant)
        r = prefix_match(f"{base}({num})")
        if r:
            return r
        # And: base.ext(N) with alt extensions
        for alt_ext in alt_exts:
            r = prefix_match(f"{base}{alt_ext}({num})")
            if r:
                return r

    # --- Rule 6: -edited / -cropped / -bearbeitet fallback ---
    # These files often have NO own JSON; fall back to the original's JSON.
    for edit_suffix in ("-edited", "-cropped", "-bearbeitet", "-edit", "-edi", "-ed"):
        if media_stem.lower().endswith(edit_suffix):
            original_stem = media_stem[: -len(edit_suffix)]
            original_name = original_stem + media_suffix
            r = prefix_match(original_name)
            if r:
                return r
            r = lookup(original_stem + ".json")
            if r:
                return r
            # Also try alt extensions
            for alt_ext in alt_exts:
                r = prefix_match(original_stem + alt_ext)
                if r:
                    return r

    # --- Rule 7: URL-decoded fallback (100%25 atzen.jpg → 100% atzen.jpg) ---
    decoded_name = urllib.parse.unquote(media_name)
    if decoded_name != media_name:
        decoded_stem = Path(decoded_name).stem
        decoded_suffix = Path(decoded_name).suffix or media_suffix
        r = prefix_match(decoded_name)
        if r:
            return r
        r = lookup(decoded_stem + ".json")
        if r:
            return r
    # Also try encoding the media name to find encoded JSON filenames
    encoded_name = urllib.parse.quote(media_name, safe=" ")
    if encoded_name != media_name:
        r = prefix_match(encoded_name)
        if r:
            return r

    # --- Rule 8: Title-based match (cached per directory) ---
    # Instead of reading every JSON on each miss, build a per-directory
    # title→path index once and reuse it.
    dir_key = id(json_map)  # same json_map object = same directory
    if dir_key not in _dir_title_cache:
        title_map = {}
        for json_name, json_file in json_map.items():
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                title = data.get("title", "")
                if title and isinstance(title, str):
                    title_lower = title.lower()
                    title_normalized = title_lower.translate(_SPECIAL_CHAR_MAP)
                    title_map[title_lower] = json_file
                    if title_normalized != title_lower:
                        title_map[title_normalized] = json_file
                    # Also index by stem (without extension)
                    title_stem = Path(title).stem.lower()
                    if title_stem not in title_map:
                        title_map[title_stem] = json_file
            except (json.JSONDecodeError, OSError):
                continue
        _dir_title_cache[dir_key] = title_map
    else:
        title_map = _dir_title_cache[dir_key]

    name_lower = media_name.lower()
    name_normalized = media_name.lower().translate(_SPECIAL_CHAR_MAP)
    stem_lower = media_stem.lower()

    r = title_map.get(name_lower) or title_map.get(name_normalized) or title_map.get(stem_lower)
    if r:
        return r

    return None


def _build_title_index(temp_dir: Path) -> None:
    """Build a global index of JSON title → file path for cross-directory matching."""
    global _title_index, _title_index_built
    if _title_index_built:
        return
    for json_file in temp_dir.rglob("*.json"):
        if not json_file.is_file():
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            title = data.get("title", "")
            if title and isinstance(title, str):
                key = title.lower()
                # Keep the first match (year folders are more complete)
                if key not in _title_index:
                    _title_index[key] = json_file
        except (json.JSONDecodeError, OSError):
            continue
    _title_index_built = True


def _find_json(media_path: Path, temp_dir: Optional[Path] = None) -> Optional[Path]:
    """Find the Google Takeout JSON sidecar for a media file.

    Covers ALL known Google Takeout naming schemes:
      - Old format:  photo.jpg.json
      - New format:  photo.jpg.supplemental-metadata.json (+ all truncation variants)
      - Stem only:   photo.json
      - Truncated filenames (46/47/51 char limits)
      - Numbered duplicates with misplaced parenthesis & extension case mismatch
      - Extension equivalents (.jpg ↔ .jpeg)
      - -edited/-cropped/-bearbeitet fallback to original file's JSON
      - Special character substitution (&?;' → _)
      - Prefix-based matching (catches ANY supplemental-metadata truncation)
      - Fuzzy match via JSON title field
      - Cross-directory matching (JSON in album folder, media in year folder)
    """
    name = media_path.name
    stem = media_path.stem
    suffix = media_path.suffix

    # Try same-directory matching first (fastest)
    json_map = _get_json_map(media_path.parent)
    if json_map:
        r = _find_json_in_map(json_map, name, stem, suffix)
        if r:
            return r

    # Cross-directory: try global title index
    if temp_dir:
        _build_title_index(temp_dir)
        name_lower = name.lower()
        name_normalized = name.lower().translate(_SPECIAL_CHAR_MAP)
        # Check by original filename
        r = _title_index.get(name_lower)
        if r:
            return r
        # Check by normalized filename
        r = _title_index.get(name_normalized)
        if r:
            return r

    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_file(media_path: Path, temp_dir: Optional[Path] = None) -> dict:
    """Analyze a single file and return an info dict.

    The returned dict always contains the cluster keys below (so the caller
    can build the "Cluster nach Google-Datum" overview even for files that
    have no mismatch), plus an optional ``row`` entry with the detailed
    row dict that goes into the "Abweichungen" sheet.

    Keys:
      - filename:       str
      - json_date_key:  "YYYY-MM-DD" or None (the date Google currently shows)
      - file_type:      "Foto" or "Video"
      - has_alternative: bool — True if any non-JSON, non-mtime source
                         (filename / EXIF / video metadata) produced a date.
                         These files will be auto-corrected by repair.py.
      - row:            dict or None — populated only on mismatch
    """
    json_path = _find_json(media_path, temp_dir=temp_dir)

    fn_date = date_from_filename(media_path)
    json_date = date_from_json(json_path) if json_path else None
    exif_date = date_from_exif(media_path)
    video_date = date_from_video_metadata(media_path)
    mtime_date = date_from_mtime(media_path)

    ext = media_path.suffix.lower()
    file_type = "Video" if ext in VIDEO_EXTENSIONS else "Foto"

    # "Best guess" local date, used for duplicate grouping. Preference order
    # matches the metadata priority: filename > JSON > EXIF > video-meta > mtime.
    best_date = fn_date or json_date or exif_date or video_date or mtime_date
    best_date_key = to_local(best_date).strftime("%Y-%m-%d") if best_date else None

    info = {
        "filename": media_path.name,
        "path": str(media_path),
        # Cluster by the LOCAL day Google Photos shows, not by UTC day.
        "json_date_key": to_local(json_date).strftime("%Y-%m-%d") if json_date else None,
        "best_date_key": best_date_key,
        "file_type": file_type,
        "has_alternative": bool(fn_date or exif_date or video_date),
        "row": None,
    }

    # Collect all available dates
    sources = {}
    if fn_date:
        sources["Dateiname"] = fn_date
    if json_date:
        sources["JSON"] = json_date
    if exif_date:
        sources["EXIF"] = exif_date
    if video_date:
        sources["Video-Meta"] = video_date
    sources["Änderungsdatum"] = mtime_date

    # Determine mismatches: compare all pairs of trustworthy sources
    # (Dateiname, JSON, EXIF). Änderungsdatum is always shown but
    # only flagged when it's the sole source.
    trustworthy = {k: v for k, v in sources.items() if k != "Änderungsdatum"}

    has_mismatch = False
    mismatch_details = []

    if len(trustworthy) >= 2:
        dates = list(trustworthy.values())
        keys = list(trustworthy.keys())
        for i in range(len(dates)):
            for j in range(i + 1, len(dates)):
                delta = abs((dates[i] - dates[j]).days)
                if delta > MISMATCH_THRESHOLD_DAYS:
                    has_mismatch = True
                    mismatch_details.append(
                        f"{keys[i]} vs {keys[j]}: {delta} Tage"
                    )
    elif len(trustworthy) == 0:
        # Only mtime available – flag as uncertain
        has_mismatch = True
        mismatch_details.append("Nur Änderungsdatum verfügbar (unsicher)")
    # If exactly 1 trustworthy source exists: no mismatch possible, skip

    if has_mismatch:
        info["row"] = {
            "Datei": media_path.name,
            "Pfad": str(media_path.parent),
            "Typ": file_type,
            "Datum Dateiname": _fmt_local(fn_date),
            "Datum JSON": _fmt_local(json_date),
            "Datum EXIF": _fmt_local(exif_date),
            "Datum Video-Meta": _fmt_local(video_date),
            "Änderungsdatum": _fmt_local(mtime_date),
            "Abweichung": "; ".join(mismatch_details),
            "JSON vorhanden": "Ja" if json_path else "Nein",
        }

    return info


# ---------------------------------------------------------------------------
# Excel report
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
MISMATCH_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
THIN_BORDER = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)

COLUMNS = [
    ("Datei", 40),
    ("Pfad", 55),
    ("Typ", 8),
    ("Datum Dateiname", 20),
    ("Datum JSON", 20),
    ("Datum EXIF", 20),
    ("Datum Video-Meta", 20),
    ("Änderungsdatum", 20),
    ("Abweichung", 45),
    ("JSON vorhanden", 14),
]

CLUSTER_COLUMNS = [
    ("JSON-Datum (Google zeigt)", 24),
    ("Betroffen", 12),
    ("Gesamt", 10),
    ("Fotos", 10),
    ("Videos", 10),
    ("Reparierbar", 14),
    ("Problematisch", 15),
]

DUPLICATE_COLUMNS = [
    ("Datum", 14),
    ("Datei", 40),
    ("Pfad", 55),
    ("Größe (Bytes)", 14),
    ("Dublette von", 40),
    ("Pfad Original", 55),
    ("MD5", 34),
    ("Gruppengröße", 14),
]


# ---------------------------------------------------------------------------
# Duplicate detection (exact-byte match, grouped per local day)
# ---------------------------------------------------------------------------

def _md5_of_file(filepath: str) -> Tuple[str, Optional[str], int]:
    """Worker: compute MD5 and file size for one file.

    Returns (filepath, md5_hex or None on error, size_bytes).
    Top-level function so it is picklable for multiprocessing.Pool.
    """
    try:
        size = os.path.getsize(filepath)
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return (filepath, h.hexdigest(), size)
    except OSError:
        return (filepath, None, 0)


def compute_md5_map(filepaths: list) -> dict:
    """Compute MD5 + size for each file in parallel.

    Returns: dict filepath → {"md5": str, "size": int}
    Files that cannot be read are silently omitted.
    """
    if not filepaths:
        return {}

    num_workers = max(1, cpu_count() - 1)
    out: dict = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("MD5 berechnen", total=len(filepaths))
        try:
            with Pool(processes=num_workers) as pool:
                for fp, md5, size in pool.imap_unordered(
                    _md5_of_file, filepaths, chunksize=8
                ):
                    if md5:
                        out[fp] = {"md5": md5, "size": size}
                    progress.advance(task)
        except Exception as e:
            logger.warning("MD5 multiprocessing failed, sequential fallback: %s", e)
            for fp in filepaths:
                _, md5, size = _md5_of_file(fp)
                if md5:
                    out[fp] = {"md5": md5, "size": size}
                progress.advance(task)

    return out


def detect_duplicates(file_infos: list, md5_map: dict) -> list:
    """Find files with identical MD5 on the same local day.

    Groups files by (md5, best_date_key). Any group with 2+ members is a
    duplicate cluster. The lexicographically first filename is reported as
    the "keeper"; every other member becomes a row in the Duplikate sheet.

    Returns a flat list of row dicts (one per extra copy).
    """
    groups: dict = defaultdict(list)
    for info in file_infos:
        fp = info.get("path")
        if not fp:
            continue
        entry = md5_map.get(fp)
        if not entry:
            continue
        date_key = info.get("best_date_key") or "unbekannt"
        groups[(entry["md5"], date_key)].append({
            "filename": info.get("filename", ""),
            "path": fp,
            "size": entry["size"],
        })

    rows = []
    for (md5, date_key), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m["filename"].lower(), m["path"]))
        keeper = members[0]
        for m in members[1:]:
            rows.append({
                "Datum": date_key,
                "Datei": m["filename"],
                "Pfad": str(Path(m["path"]).parent),
                "Größe (Bytes)": m["size"],
                "Dublette von": keeper["filename"],
                "Pfad Original": str(Path(keeper["path"]).parent),
                "MD5": md5,
                "Gruppengröße": len(members),
            })

    rows.sort(key=lambda r: (r["Datum"], r["MD5"], r["Datei"]))
    return rows


def build_cluster_summary(file_infos: list) -> list:
    """Aggregate per-file info into cluster rows keyed by JSON date.

    Each entry counts everything Google Photos currently stamps with that
    date — both files with a real mismatch and files where JSON agrees
    with the filename/EXIF. The Gesamt column matches what you would be
    deleting in Google Photos when you wipe that day. Only JSON dates
    that actually have at least one affected file are returned.

    Sorted by "Betroffen" descending so the worst clusters come first.
    """
    buckets = defaultdict(lambda: {
        "total": 0,
        "fotos": 0,
        "videos": 0,
        "mismatched": 0,
        "reparierbar": 0,
        "problematisch": 0,
    })

    for info in file_infos:
        key = info.get("json_date_key")
        if not key:
            continue
        b = buckets[key]
        b["total"] += 1
        if info.get("file_type") == "Video":
            b["videos"] += 1
        else:
            b["fotos"] += 1
        if info.get("row"):
            b["mismatched"] += 1
            if info.get("has_alternative"):
                b["reparierbar"] += 1
            else:
                b["problematisch"] += 1

    rows = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        if b["mismatched"] == 0:
            continue
        rows.append({
            "JSON-Datum (Google zeigt)": key,
            "Betroffen": b["mismatched"],
            "Gesamt": b["total"],
            "Fotos": b["fotos"],
            "Videos": b["videos"],
            "Reparierbar": b["reparierbar"],
            "Problematisch": b["problematisch"],
        })

    rows.sort(key=lambda r: (-r["Betroffen"], r["JSON-Datum (Google zeigt)"]))
    return rows


def write_excel(rows: list, output_path: Path, total_files: int,
                cluster_rows: Optional[list] = None,
                duplicate_rows: Optional[list] = None):
    """Write the mismatch report as a formatted Excel file."""
    wb = Workbook()
    duplicate_rows = duplicate_rows or []

    # --- Sheet 1: Zusammenfassung ---
    ws_summary = wb.active
    ws_summary.title = "Zusammenfassung"
    ws_summary.sheet_properties.tabColor = "2F5496"

    summary_data = [
        ("Analyse-Datum", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Dateien gesamt", total_files),
        ("Dateien mit Abweichung", len(rows)),
        ("Anteil mit Abweichung", f"{len(rows)/max(total_files,1)*100:.1f}%"),
        ("Schwellenwert", f"{MISMATCH_THRESHOLD_DAYS} Tage"),
        ("Dubletten (gleiches Datum, gleicher Inhalt)", len(duplicate_rows)),
    ]

    ws_summary["A1"] = "Google Photos Metadaten-Analyse"
    ws_summary["A1"].font = Font(name="Calibri", bold=True, size=14, color="2F5496")
    ws_summary.merge_cells("A1:B1")

    for i, (label, value) in enumerate(summary_data, start=3):
        ws_summary.cell(row=i, column=1, value=label).font = Font(bold=True)
        ws_summary.cell(row=i, column=2, value=str(value))

    ws_summary.column_dimensions["A"].width = 25
    ws_summary.column_dimensions["B"].width = 30

    # --- Sheet 2: Cluster nach Google-Datum ---
    ws_cluster = wb.create_sheet("Cluster nach Google-Datum")
    ws_cluster.sheet_properties.tabColor = "C00000"

    ws_cluster["A1"] = (
        "Cluster nach Google-Datum  –  so viele Dateien datiert Google Photos "
        "pro Tag. 'Betroffen' = Abweichung erkannt, 'Gesamt' = alles was du "
        "beim Delete-und-Re-Upload-Workflow mitnimmst."
    )
    ws_cluster["A1"].font = Font(name="Calibri", italic=True, size=9, color="595959")
    ws_cluster.merge_cells(
        start_row=1, start_column=1,
        end_row=1, end_column=len(CLUSTER_COLUMNS),
    )
    ws_cluster.row_dimensions[1].height = 22

    for col_idx, (col_name, col_width) in enumerate(CLUSTER_COLUMNS, start=1):
        cell = ws_cluster.cell(row=2, column=col_idx, value=col_name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws_cluster.column_dimensions[get_column_letter(col_idx)].width = col_width

    ws_cluster.auto_filter.ref = f"A2:{get_column_letter(len(CLUSTER_COLUMNS))}2"
    ws_cluster.freeze_panes = "A3"

    cluster_rows = cluster_rows or []
    for row_idx, cr in enumerate(cluster_rows, start=3):
        for col_idx, (col_name, _) in enumerate(CLUSTER_COLUMNS, start=1):
            cell = ws_cluster.cell(row=row_idx, column=col_idx, value=cr.get(col_name, ""))
            cell.border = THIN_BORDER
            cell.font = Font(name="Calibri", size=10)
            if col_name == "Betroffen" and cr.get("Betroffen", 0) > 0:
                cell.fill = MISMATCH_FILL
            if col_name == "Problematisch" and cr.get("Problematisch", 0) > 0:
                cell.fill = PatternFill(
                    start_color="F4CCCC", end_color="F4CCCC", fill_type="solid"
                )
            if col_name in ("Betroffen", "Gesamt", "Fotos", "Videos",
                            "Reparierbar", "Problematisch"):
                cell.alignment = Alignment(horizontal="right")

    # --- Sheet 3: Abweichungen ---
    ws = wb.create_sheet("Abweichungen")
    ws.sheet_properties.tabColor = "ED7D31"

    # Header
    for col_idx, (col_name, col_width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = col_width

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"
    ws.freeze_panes = "A2"

    # Data rows
    for row_idx, row_data in enumerate(rows, start=2):
        for col_idx, (col_name, _) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=row_data.get(col_name, ""))
            cell.border = THIN_BORDER
            cell.font = Font(name="Calibri", size=10)
            if col_name == "Abweichung":
                cell.fill = MISMATCH_FILL

    # --- Sheet 4: Duplikate ---
    ws_dup = wb.create_sheet("Duplikate")
    ws_dup.sheet_properties.tabColor = "7030A0"

    ws_dup["A1"] = (
        "Duplikate  –  Dateien mit exakt gleichem Inhalt (MD5) am gleichen "
        "Tag (lokale Zeitzone). 'Dublette von' ist der behaltene Kandidat; "
        "alle hier gelisteten Zeilen sind zusätzliche Kopien und können "
        "gelöscht werden."
    )
    ws_dup["A1"].font = Font(name="Calibri", italic=True, size=9, color="595959")
    ws_dup.merge_cells(
        start_row=1, start_column=1,
        end_row=1, end_column=len(DUPLICATE_COLUMNS),
    )
    ws_dup.row_dimensions[1].height = 22

    for col_idx, (col_name, col_width) in enumerate(DUPLICATE_COLUMNS, start=1):
        cell = ws_dup.cell(row=2, column=col_idx, value=col_name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws_dup.column_dimensions[get_column_letter(col_idx)].width = col_width

    ws_dup.auto_filter.ref = f"A2:{get_column_letter(len(DUPLICATE_COLUMNS))}2"
    ws_dup.freeze_panes = "A3"

    DUP_FILL = PatternFill(start_color="E4D5F2", end_color="E4D5F2", fill_type="solid")
    for row_idx, dr in enumerate(duplicate_rows, start=3):
        for col_idx, (col_name, _) in enumerate(DUPLICATE_COLUMNS, start=1):
            cell = ws_dup.cell(row=row_idx, column=col_idx, value=dr.get(col_name, ""))
            cell.border = THIN_BORDER
            cell.font = Font(name="Calibri", size=10)
            if col_name == "Datum":
                cell.fill = DUP_FILL
            if col_name in ("Größe (Bytes)", "Gruppengröße"):
                cell.alignment = Alignment(horizontal="right")

    wb.save(str(output_path))
    console.print(f"\n[bold green]Excel-Report gespeichert:[/bold green] {output_path}")
    console.print(f"  {len(rows)} Dateien mit Metadaten-Abweichung von {total_files} gesamt")
    if cluster_rows:
        console.print(
            f"  {len(cluster_rows)} verschiedene JSON-Daten mit Abweichungen "
            f"(siehe Sheet 'Cluster nach Google-Datum')"
        )
    if duplicate_rows:
        console.print(
            f"  {len(duplicate_rows)} Duplikat-Zeile(n) "
            f"(siehe Sheet 'Duplikate')"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        prog="analyze",
        description="Google Takeout ZIPs analysieren und Excel-Report "
                    "mit Metadaten-Abweichungen erstellen.",
    )
    parser.add_argument(
        "--input", default=".",
        help="Ordner mit den ZIP-Dateien (Standard: aktuelles Verzeichnis)",
    )
    parser.add_argument(
        "--temp", default="./temp_analyze",
        help="Temp-Ordner zum Entpacken (Standard: ./temp_analyze)",
    )
    parser.add_argument(
        "--output", default="metadaten_report.xlsx",
        help="Pfad der Excel-Datei (Standard: metadaten_report.xlsx)",
    )
    parser.add_argument(
        "--skip-extraction", action="store_true",
        help="ZIP-Entpacken überspringen (wenn bereits entpackt)",
    )
    parser.add_argument(
        "--threshold", type=int, default=MISMATCH_THRESHOLD_DAYS,
        help=f"Schwellenwert in Tagen für Abweichung (Standard: {MISMATCH_THRESHOLD_DAYS})",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    global MISMATCH_THRESHOLD_DAYS
    MISMATCH_THRESHOLD_DAYS = args.threshold

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    console.print(Panel(
        "[bold cyan]Google Photos Metadaten-Analyse[/bold cyan]\n"
        "ZIPs entpacken → Metadaten vergleichen → Excel-Report erstellen",
        border_style="cyan",
    ))

    input_dir = Path(args.input).resolve()
    temp_dir = Path(args.temp).resolve()
    output_path = Path(args.output).resolve()

    # Phase 1: Extract ZIPs
    if not args.skip_extraction:
        console.print(Panel("[bold]Phase 1: ZIP-Dateien entpacken[/bold]", border_style="green"))
        count = extract_zips(input_dir, temp_dir)
        console.print(f"  {count} ZIP(s) entpackt nach {temp_dir}\n")
    else:
        console.print("[dim]ZIP-Entpacken übersprungen (--skip-extraction)[/dim]\n")

    if not temp_dir.exists():
        console.print(f"[red]Temp-Ordner nicht gefunden: {temp_dir}[/red]")
        sys.exit(1)

    # Phase 2: Scan media files
    console.print(Panel("[bold]Phase 2: Medien-Dateien scannen[/bold]", border_style="green"))
    media_files = sorted(
        f for f in temp_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in MEDIA_EXTENSIONS
        and not f.name.startswith(".trashed-")
    )
    console.print(f"  {len(media_files)} Medien-Dateien gefunden\n")

    if not media_files:
        console.print("[yellow]Keine Medien-Dateien gefunden. Abbruch.[/yellow]")
        sys.exit(0)

    # Phase 3: Build title index (for cross-directory matching)
    console.print(Panel("[bold]Phase 3: JSON-Titel-Index aufbauen[/bold]", border_style="green"))
    console.print("  Scanne alle JSON-Dateien für Cross-Directory-Matching...")
    _build_title_index(temp_dir)
    console.print(f"  {len(_title_index)} JSON-Titel indexiert\n")

    # Phase 4: Analyze metadata
    console.print(Panel("[bold]Phase 4: Metadaten analysieren[/bold]", border_style="green"))
    file_infos = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Metadaten prüfen", total=len(media_files))
        for mf in media_files:
            try:
                info = analyze_file(mf, temp_dir=temp_dir)
                file_infos.append(info)
            except Exception as e:
                logger.error("Fehler bei %s: %s", mf.name, e)
            progress.advance(task)

    # Deduplicate: same file can appear in album + year folder.
    # Keep only one entry per filename (the one with the most metadata).
    # We dedup ALL infos (not just mismatches) so the cluster summary stays
    # consistent with the Abweichungen sheet.
    def _row_info_score(info: dict) -> int:
        row = info.get("row") or {}
        return sum(
            1 for c in ("Datum Dateiname", "Datum JSON",
                        "Datum EXIF", "Datum Video-Meta")
            if row.get(c)
        )

    seen = {}
    for info in file_infos:
        fname = info.get("filename") or ""
        key = fname.lower()
        if not key:
            continue
        if key not in seen:
            seen[key] = info
        else:
            old = seen[key]
            if _row_info_score(info) > _row_info_score(old):
                seen[key] = info
    file_infos = list(seen.values())

    # Extract the mismatch rows (for the "Abweichungen" sheet) and the
    # cluster aggregation (for the "Cluster nach Google-Datum" sheet).
    mismatch_rows = [i["row"] for i in file_infos if i.get("row")]
    mismatch_rows.sort(key=lambda r: r["Datei"])
    cluster_rows = build_cluster_summary(file_infos)

    # Phase 5: Duplicate detection (MD5 + same local day)
    console.print(Panel(
        "[bold]Phase 5: Duplikate erkennen (MD5, gleicher Tag)[/bold]",
        border_style="green",
    ))
    md5_paths = [i["path"] for i in file_infos if i.get("path")]
    md5_map = compute_md5_map(md5_paths)
    duplicate_rows = detect_duplicates(file_infos, md5_map)
    console.print(
        f"  {len(duplicate_rows)} Dublette(n) gefunden "
        f"(exakt gleiche Bytes, gleiches Datum)\n"
    )

    # Phase 6: Write Excel
    console.print(Panel("[bold]Phase 6: Excel-Report schreiben[/bold]", border_style="green"))
    write_excel(mismatch_rows, output_path, len(media_files),
                cluster_rows=cluster_rows,
                duplicate_rows=duplicate_rows)

    console.print("\n[bold green]Fertig![/bold green]")


if __name__ == "__main__":
    main()
