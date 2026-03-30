#!/usr/bin/env python3
"""
Google Photos Metadata Analyzer

Extracts Google Takeout ZIPs in the current directory, scans all photos/videos,
and generates an Excel report of files where metadata dates diverge
(filename vs JSON sidecar vs EXIF vs file modification time).
"""

import argparse
import json
import logging
import os
import re
import struct
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

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


# All known supplemental-metadata truncation variants that Google produces.
# The full suffix is ".supplemental-metadata.json" but Google truncates it
# progressively to keep total filename <= 51 chars.
_SUPPL_SUFFIXES = [
    ".supplemental-metadata.json",
    ".supplemental-metadat.json",
    ".supplemental-metada.json",
    ".supplemental-metad.json",
    ".supplemental-meta.json",
    ".supplemental-met.json",
    ".supplemental-me.json",
    ".supplemental-m.json",
    ".supplemental-.json",
    ".supplemental.json",
    ".supplementa.json",
    ".supplement.json",
    ".supplemen.json",
    ".suppleme.json",
    ".supplem.json",
    ".supple.json",
    ".suppl.json",
    ".supp.json",
    ".sup.json",
]

# Characters Google replaces with _ in exported filenames
# (but the JSON title field keeps the originals)
_SPECIAL_CHAR_MAP = str.maketrans("&?;'", "____")


def _find_json(media_path: Path) -> Optional[Path]:
    """Find the Google Takeout JSON sidecar for a media file.

    Covers all known Google Takeout naming schemes:
      - Old format:  photo.jpg.json
      - New format:  photo.jpg.supplemental-metadata.json (and truncated variants)
      - Stem only:   photo.json
      - Truncated filenames (46/47/51 char limits)
      - Numbered duplicates with misplaced parenthesis
      - -edited/-cropped fallback to original file's JSON
      - Special character substitution (&?;' → _)
      - Fuzzy match via JSON title field
    """
    parent = media_path.parent
    name = media_path.name
    stem = media_path.stem
    suffix = media_path.suffix

    json_map = _get_json_map(parent)
    if not json_map:
        return None

    def lookup(candidate: str) -> Optional[Path]:
        return json_map.get(candidate.lower())

    # All JSON suffixes to try (new supplemental-metadata + old .json)
    def _try_all_suffixes(base: str) -> Optional[Path]:
        """Try a base string with all known JSON suffix variants."""
        for s in _SUPPL_SUFFIXES:
            r = lookup(base + s)
            if r:
                return r
        r = lookup(base + ".json")
        if r:
            return r
        return None

    # --- Rule 1: Direct match (photo.jpg → photo.jpg.supplemental-metadata.json or photo.jpg.json) ---
    r = _try_all_suffixes(name)
    if r:
        return r

    # --- Rule 2: Stem only (photo.jpg → photo.json) ---
    r = lookup(stem + ".json")
    if r:
        return r

    # --- Rule 3: Truncated filenames ---
    # Google caps total filename at ~51 chars. The media filename itself can be
    # truncated at ~47 chars (name part) or ~46 chars (before .json suffix).
    for trunc_len in (46, 47, 51):
        if len(name) > trunc_len:
            r = _try_all_suffixes(name[:trunc_len])
            if r:
                return r

        if len(stem) > trunc_len:
            r = _try_all_suffixes(stem[:trunc_len] + suffix)
            if r:
                return r

    # --- Rule 4: Numbered duplicates ---
    # Google places the number differently:
    #   Media:  photo(1).jpg
    #   JSON:   photo.jpg(1).json  OR  photo.JPG(1).json  (case may differ!)
    m = re.match(r"^(.+)\((\d+)\)$", stem)
    if m:
        base, num = m.group(1), m.group(2)
        # Try with various extension cases
        for ext_variant in (suffix, suffix.upper(), suffix.lower()):
            r = _try_all_suffixes(f"{base}{ext_variant}({num})")
            if r:
                return r
        # Also try: base(N).json directly
        r = _try_all_suffixes(f"{base}({num})")
        if r:
            return r

    # --- Rule 5: -edited / -cropped fallback ---
    # These files have NO own JSON; fall back to the original's JSON.
    for edit_suffix in ("-edited", "-cropped", "-edit", "-edi"):
        if stem.lower().endswith(edit_suffix):
            original_stem = stem[: -len(edit_suffix)]
            original_name = original_stem + suffix
            r = _try_all_suffixes(original_name)
            if r:
                return r
            r = lookup(original_stem + ".json")
            if r:
                return r

    # --- Rule 6: Fuzzy match via JSON title field ---
    # Google may substitute &?;' with _ in filenames but keeps originals in
    # the JSON title. Also handles other naming mismatches.
    # Build reverse lookup: read JSON files and check if their "title" matches.
    name_lower = name.lower()
    # Also create a version where special chars are normalized
    name_normalized = name.lower().translate(_SPECIAL_CHAR_MAP)

    for json_name, json_file in json_map.items():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            title = data.get("title", "")
            if not title:
                continue
            title_lower = title.lower()
            title_normalized = title_lower.translate(_SPECIAL_CHAR_MAP)
            # Match: exact, or after special-char normalization
            if title_lower == name_lower or title_normalized == name_normalized:
                return json_file
            # Also match if title equals stem (without extension)
            if title_lower == stem.lower():
                return json_file
        except (json.JSONDecodeError, OSError):
            continue

    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_file(media_path: Path) -> Optional[dict]:
    """Analyze a single file and return a row dict if there is a mismatch,
    or None if everything is consistent.
    """
    json_path = _find_json(media_path)

    fn_date = date_from_filename(media_path)
    json_date = date_from_json(json_path) if json_path else None
    exif_date = date_from_exif(media_path)
    video_date = date_from_video_metadata(media_path)
    mtime_date = date_from_mtime(media_path)

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

    if not has_mismatch:
        return None

    ext = media_path.suffix.lower()
    file_type = "Video" if ext in VIDEO_EXTENSIONS else "Foto"

    return {
        "Datei": media_path.name,
        "Pfad": str(media_path.parent),
        "Typ": file_type,
        "Datum Dateiname": fn_date.strftime("%Y-%m-%d %H:%M:%S") if fn_date else "",
        "Datum JSON": json_date.strftime("%Y-%m-%d %H:%M:%S") if json_date else "",
        "Datum EXIF": exif_date.strftime("%Y-%m-%d %H:%M:%S") if exif_date else "",
        "Datum Video-Meta": video_date.strftime("%Y-%m-%d %H:%M:%S") if video_date else "",
        "Änderungsdatum": mtime_date.strftime("%Y-%m-%d %H:%M:%S"),
        "Abweichung": "; ".join(mismatch_details),
        "JSON vorhanden": "Ja" if json_path else "Nein",
    }


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


def write_excel(rows: list, output_path: Path, total_files: int):
    """Write the mismatch report as a formatted Excel file."""
    wb = Workbook()

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
    ]

    ws_summary["A1"] = "Google Photos Metadaten-Analyse"
    ws_summary["A1"].font = Font(name="Calibri", bold=True, size=14, color="2F5496")
    ws_summary.merge_cells("A1:B1")

    for i, (label, value) in enumerate(summary_data, start=3):
        ws_summary.cell(row=i, column=1, value=label).font = Font(bold=True)
        ws_summary.cell(row=i, column=2, value=str(value))

    ws_summary.column_dimensions["A"].width = 25
    ws_summary.column_dimensions["B"].width = 30

    # --- Sheet 2: Abweichungen ---
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

    wb.save(str(output_path))
    console.print(f"\n[bold green]Excel-Report gespeichert:[/bold green] {output_path}")
    console.print(f"  {len(rows)} Dateien mit Metadaten-Abweichung von {total_files} gesamt")


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

    # Phase 3: Analyze metadata
    console.print(Panel("[bold]Phase 3: Metadaten analysieren[/bold]", border_style="green"))
    mismatch_rows = []

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
                row = analyze_file(mf)
                if row:
                    mismatch_rows.append(row)
            except Exception as e:
                logger.error("Fehler bei %s: %s", mf.name, e)
            progress.advance(task)

    # Deduplicate: same file can appear in album + year folder.
    # Keep only one entry per filename (the one with the most metadata).
    seen = {}
    for row in mismatch_rows:
        key = row["Datei"].lower()
        if key not in seen:
            seen[key] = row
        else:
            # Keep the one with more filled date fields
            old = seen[key]
            old_count = sum(1 for c in ("Datum Dateiname", "Datum JSON", "Datum EXIF", "Datum Video-Meta") if old.get(c))
            new_count = sum(1 for c in ("Datum Dateiname", "Datum JSON", "Datum EXIF", "Datum Video-Meta") if row.get(c))
            if new_count > old_count:
                seen[key] = row
    mismatch_rows = list(seen.values())

    # Sort by filename for readability
    mismatch_rows.sort(key=lambda r: r["Datei"])

    # Phase 4: Write Excel
    console.print(Panel("[bold]Phase 4: Excel-Report schreiben[/bold]", border_style="green"))
    write_excel(mismatch_rows, output_path, len(media_files))

    console.print("\n[bold green]Fertig![/bold green]")


if __name__ == "__main__":
    main()
