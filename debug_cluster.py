#!/usr/bin/env python3
"""
Diagnose-Tool: dumpt alle Zeitquellen für Dateien eines bestimmten Tages.

Benutzung:
    python debug_cluster.py --temp ./temp --date 2023-07-30
    python debug_cluster.py --temp ./temp --date 2023-07-30 --out debug.txt

Findet alle Mediendateien im --temp-Ordner, deren Cluster-Tag (JSON-Datum)
oder Dateiname-Datum oder EXIF-Datum am angegebenen Tag liegt, und
schreibt pro Datei EINE Block mit allen Zeitquellen nebeneinander, damit
man die Diskrepanz zwischen Google-Photos-Anzeige und Tool-Cluster sehen
kann.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.metadata import (
    get_timestamp_from_filename,
    get_timestamp_from_exif,
    ALL_IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from modules.matcher import find_json_for_media

MEDIA_EXTS = ALL_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {
    ".gif", ".bmp", ".raw", ".cr2", ".nef", ".arw", ".dng",
}

# Kandidaten-TZs — die Anzeige-Zeitzone von Google Photos ist nicht
# dokumentiert; wir zeigen alle plausiblen Optionen nebeneinander, damit
# man durch Vergleich mit der Google-Photos-Webseite eindeutig erkennt,
# welche TZ Google tatsächlich verwendet.
CANDIDATE_TZS = [
    ("UTC",         ZoneInfo("UTC")),
    ("Berlin",      ZoneInfo("Europe/Berlin")),
    ("Eastern US",  ZoneInfo("America/New_York")),
    ("Pacific US",  ZoneInfo("America/Los_Angeles")),
]


def _json_fields(json_path: Path) -> dict:
    """Read the raw JSON and extract all time-related fields."""
    out = {
        "photoTakenTime.timestamp": None,
        "photoTakenTime.formatted": None,
        "creationTime.timestamp": None,
        "creationTime.formatted": None,
        "geoData": None,
        "geoDataExif": None,
    }
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        out["_error"] = str(e)
        return out
    for field in ("photoTakenTime", "creationTime"):
        sub = data.get(field) or {}
        out[f"{field}.timestamp"] = sub.get("timestamp")
        out[f"{field}.formatted"] = sub.get("formatted")
    for field in ("geoData", "geoDataExif"):
        sub = data.get(field) or {}
        lat = sub.get("latitude")
        lon = sub.get("longitude")
        if lat is not None or lon is not None:
            out[field] = {"lat": lat, "lon": lon}
    return out


def _ts_to_utc(ts_str):
    try:
        return datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
    except Exception:
        return None


def _ts_to_local(ts_str):
    dt = _ts_to_utc(ts_str)
    if dt is None:
        return None
    return dt.astimezone()


def dump_file(mf: Path, temp_root: Path, json_path, out_lines: list):
    """Produce a one-block diagnostic for a single media file."""
    rel = mf.relative_to(temp_root) if temp_root in mf.parents else mf.name
    out_lines.append("=" * 78)
    out_lines.append(f"Datei           : {rel}")
    out_lines.append(f"Absoluter Pfad  : {mf}")

    # Filename-based date
    fn_result = get_timestamp_from_filename(mf)
    if fn_result:
        fn_dt, _ = fn_result
        out_lines.append(f"Datum Dateiname : {fn_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        out_lines.append("Datum Dateiname : (nicht erkannt)")

    # EXIF
    exif_result = get_timestamp_from_exif(mf)
    if exif_result:
        exif_dt, _ = exif_result
        out_lines.append(
            f"EXIF DateTimeOrig: {exif_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        out_lines.append("EXIF DateTimeOrig: (nicht gelesen — ggf. Video/HEIC)")

    # File mtime
    try:
        mtime = os.path.getmtime(mf)
        mdt_local = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone()
        out_lines.append(
            f"File mtime local : {mdt_local.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
    except Exception:
        out_lines.append("File mtime local : (Fehler)")

    # JSON
    if json_path:
        out_lines.append(f"JSON gefunden   : {json_path.name}")
        j = _json_fields(json_path)
        if j.get("_error"):
            out_lines.append(f"  JSON-Fehler    : {j['_error']}")
        for key in ("photoTakenTime.timestamp", "photoTakenTime.formatted",
                    "creationTime.timestamp", "creationTime.formatted"):
            val = j.get(key)
            if val is not None:
                out_lines.append(f"  {key:30s}: {val}")
        # Derived interpretations
        pts = j.get("photoTakenTime.timestamp")
        if pts:
            utc_dt = _ts_to_utc(pts)
            local_dt = _ts_to_local(pts)
            if utc_dt:
                out_lines.append(
                    f"  timestamp AS UTC              : "
                    f"{utc_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}  "
                    f"→ cluster '{utc_dt.strftime('%Y-%m-%d')}'"
                )
            if local_dt:
                out_lines.append(
                    f"  timestamp converted to local  : "
                    f"{local_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}  "
                    f"→ cluster '{local_dt.strftime('%Y-%m-%d')}'"
                )
        for field in ("geoData", "geoDataExif"):
            if j.get(field):
                g = j[field]
                out_lines.append(f"  {field:30s}: lat={g['lat']} lon={g['lon']}")
    else:
        out_lines.append("JSON gefunden   : NEIN")

    out_lines.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--temp", default="./temp",
                    help="Ordner mit entpackten Takeout-Daten")
    ap.add_argument("--date", required=True,
                    help="Datum im Format YYYY-MM-DD (z.B. 2023-07-30)")
    ap.add_argument("--out", default=None,
                    help="Ausgabedatei (Standard: debug_<date>.txt)")
    ap.add_argument("--limit", type=int, default=100,
                    help="Max. Anzahl Dateien im Dump (Standard: 100)")
    args = ap.parse_args()

    target = args.date
    out_path = Path(args.out or f"debug_{target}.txt").resolve()
    temp_root = Path(args.temp).resolve()
    if not temp_root.is_dir():
        print(f"[FEHLER] Temp-Ordner nicht gefunden: {temp_root}")
        sys.exit(1)

    print(f"Scanne {temp_root} …")
    media = [
        f for f in temp_root.rglob("*")
        if f.is_file()
        and f.suffix.lower() in MEDIA_EXTS
        and not f.name.startswith(".trashed-")
    ]
    print(f"  {len(media)} Mediendateien gefunden.")

    matches = []
    for mf in media:
        try:
            json_path = find_json_for_media(mf, str(temp_root))
        except Exception:
            json_path = None

        matches_date = False
        # Match via JSON date (both UTC- and local-interpretation)
        if json_path:
            j = _json_fields(json_path)
            pts = j.get("photoTakenTime.timestamp")
            if pts:
                utc_dt = _ts_to_utc(pts)
                local_dt = _ts_to_local(pts)
                if (utc_dt and utc_dt.strftime("%Y-%m-%d") == target) or \
                   (local_dt and local_dt.strftime("%Y-%m-%d") == target):
                    matches_date = True

        # Match via filename
        if not matches_date:
            fn_res = get_timestamp_from_filename(mf)
            if fn_res and fn_res[0].strftime("%Y-%m-%d") == target:
                matches_date = True

        if matches_date:
            matches.append((mf, json_path))

    print(f"  {len(matches)} Datei(en) passen auf {target}.")
    if not matches:
        print("Keine Treffer – bitte Datum prüfen.")
        return

    if len(matches) > args.limit:
        print(f"  Beschränke Dump auf die ersten {args.limit} Dateien "
              f"(per --limit änderbar).")
        matches = matches[: args.limit]

    lines = [
        f"Debug-Dump für Cluster '{target}'",
        f"Temp-Ordner          : {temp_root}",
        f"System-Zeitzone       : {datetime.now().astimezone().tzinfo}",
        f"Anzahl Dateien im Dump: {len(matches)}",
        "",
        "Interpretation-Hinweise:",
        "  - 'timestamp AS UTC'              = so stellt unser aktueller",
        "     Fix den Cluster-Ordner auf Disk.",
        "  - 'timestamp converted to local'  = frühere (falsche) Logik mit",
        "     .astimezone(); zum Vergleich mit dabei.",
        "  - 'photoTakenTime.formatted'      = der Roh-UTC-Text im JSON.",
        "  - 'EXIF DateTimeOrig'             = was die Kamera im Bild",
        "     gespeichert hat (EXIF hat keine Zeitzonen-Angabe).",
        "",
    ]

    for mf, json_path in matches:
        dump_file(mf, temp_root, json_path, lines)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDump geschrieben nach: {out_path}")
    print("Datei hier einfügen — daraus kann ich eindeutig ableiten,")
    print("welche Zeitquelle vom Tool bzw. Google Photos verwendet wird.")


if __name__ == "__main__":
    main()
