#!/usr/bin/env python3
"""
TZ-Kalibrierung für Google Photos.

IN DEN MEISTEN FÄLLEN NICHT MEHR NÖTIG.
----------------------------------------
Seit Version 6185765 clustert das Repair-Tool primär nach EXIF
DateTimeOriginal, nicht nach der JSON-Timestamp-Umrechnung. EXIF
enthält bereits das lokale Aufnahmedatum, das Google Photos in der
Grid-View anzeigt — eine TZ-Kalibrierung ändert daran nichts.

Dieses Script ist nur noch relevant wenn viele deiner Dateien
**überhaupt kein EXIF** haben (ältere Scans, bestimmte Videos, sehr
alte Formate). Für solche Dateien fällt repair.py auf den
JSON-photoTakenTime in der hier kalibrierten TZ zurück. Falls deine
EXIF-reichen Fotos schon korrekt geclustert werden, brauchst du diese
Kalibrierung nicht.

Workflow:
  1. python analyze_tz.py --temp ./temp_analyze
        Pickt 10 strategisch ausgewählte Dateien und schreibt
        tz_analysis.txt mit einer Lücke pro Datei zum Ausfüllen.

  2. Jede der 10 Dateien in Google Photos suchen (Dateiname ins
     Google-Photos-Suchfeld einfügen), geöffnetes Foto ansehen,
     das Datum das Google Photos zeigt in die Lücke eintragen.

  3. python analyze_tz.py --verify tz_analysis.txt
        Wertet die 10 Antworten aus und nennt die EINDEUTIGE TZ.
        Diese dann in repair.py via --google-tz weitergeben.

Die Dateien werden so ausgewählt, dass jede Kandidaten-TZ
(UTC, Europe/Berlin, America/New_York, America/Los_Angeles)
für mindestens einige Dateien ein anderes Datum liefert — dadurch
legen die 10 Antworten zusammen die TZ eindeutig fest.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.metadata import ALL_IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from modules.matcher import find_json_for_media

MEDIA_EXTS = ALL_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {
    ".gif", ".bmp", ".raw", ".cr2", ".nef", ".arw", ".dng",
}

# Kandidaten — wenn die richtige TZ hier NICHT dabei ist, können wir sie
# nicht finden. Das sind die vier sinnvollen Möglichkeiten für ein
# deutsches Google-Konto mit Server-seitigem Rendering:
CANDIDATES = [
    ("UTC",                "UTC"),
    ("Europe/Berlin",      "Europe/Berlin"),
    ("America/New_York",   "America/New_York"),
    ("America/Los_Angeles","America/Los_Angeles"),
]


def load_json_fields(json_path: Path) -> dict:
    """Liest photoTakenTime + geoData aus einem Sidecar."""
    out = {"timestamp": None, "formatted": None, "has_gps": False}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    pt = data.get("photoTakenTime") or {}
    out["timestamp"] = pt.get("timestamp")
    out["formatted"] = pt.get("formatted")
    for field in ("geoData", "geoDataExif"):
        geo = data.get(field) or {}
        try:
            lat = float(geo.get("latitude", 0) or 0)
            lon = float(geo.get("longitude", 0) or 0)
        except (TypeError, ValueError):
            continue
        if lat != 0.0 or lon != 0.0:
            out["has_gps"] = True
            break
    return out


def candidate_dates(ts: int) -> dict:
    """Gibt pro Kandidaten-TZ den Anzeigedatum zurück."""
    utc_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    result = {}
    for label, tz_name in CANDIDATES:
        local = utc_dt.astimezone(ZoneInfo(tz_name))
        result[label] = local.strftime("%Y-%m-%d")
    return result


def count_distinct(date_map: dict) -> int:
    return len(set(date_map.values()))


# ---------------------------------------------------------------------------
# Teil 1: Auswahl der 10 Dateien
# ---------------------------------------------------------------------------

def scan_candidates(temp_dir: Path) -> list:
    """Scannt alle Mediendateien, liefert (path, json_path, fields, candidate_dates)
    — aber nur für Dateien mit gültigem photoTakenTime."""
    media = [
        f for f in temp_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in MEDIA_EXTS
        and not f.name.startswith(".trashed-")
    ]
    entries = []
    for mf in media:
        try:
            json_path = find_json_for_media(mf, str(temp_dir))
        except Exception:
            json_path = None
        if not json_path:
            continue
        fields = load_json_fields(json_path)
        ts = fields.get("timestamp")
        if not ts:
            continue
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            continue
        cdates = candidate_dates(ts_int)
        entries.append({
            "path": mf,
            "rel": mf.relative_to(temp_dir),
            "json_path": json_path,
            "timestamp": ts_int,
            "formatted": fields.get("formatted"),
            "has_gps": fields.get("has_gps"),
            "candidate_dates": cdates,
            "distinct": count_distinct(cdates),
        })
    return entries


def _utc_bucket(ts: int) -> str:
    """3h-Bucket der UTC-Stunde — je breiter gestreut, desto besser
    können die 4 Kandidaten-TZs unterschieden werden. Kritisch:
      - 03-06 UTC: unterscheidet Eastern (UTC-4/5) von Pacific (UTC-7/8)
      - 22-24 UTC: unterscheidet UTC von Europe/Berlin (+1/+2)
      - 00-03 UTC: Eastern/Pacific zeigen Vortag, UTC/Berlin zeigen selben Tag
    """
    h = datetime.fromtimestamp(int(ts), tz=timezone.utc).hour
    return f"{(h // 3) * 3:02d}-{(h // 3) * 3 + 3:02d}"


def pick_10(entries: list) -> list:
    """Wählt 10 strategisch aussagekräftige Dateien aus.

    Zwei konkurrierende Ziele:
      a) Dateien müssen die 4 Kandidaten-TZs **paarweise** unterscheiden
         (distinct >= 2 ist Pflicht, sonst nutzlos).
      b) UTC-Stunden müssen möglichst breit gestreut sein — sonst
         unterscheiden die Samples nur UTC-vs-Amerika, aber nicht
         NY-vs-LA bzw. UTC-vs-Berlin.

    Heuristik: zuerst round-robin über 3h-UTC-Buckets ziehen, innerhalb
    eines Buckets die mit höchstem distinct zuerst. Zusätzlich Dedup
    per (UTC-Datum, GPS-Flag), damit wir keine 10 Dateien aus demselben
    Upload-Batch bekommen.
    """
    entries = [e for e in entries if e["distinct"] >= 2]
    if not entries:
        return []

    # In Buckets nach 3h-UTC-Stunde gruppieren, innerhalb nach distinct absteigend
    buckets: dict = {}
    for e in entries:
        b = _utc_bucket(e["timestamp"])
        buckets.setdefault(b, []).append(e)
    for b in buckets:
        buckets[b].sort(key=lambda e: (-e["distinct"], e["timestamp"]))

    # Round-robin: je Runde eine Datei aus jedem Bucket, bis 10 voll oder alle leer
    result = []
    seen_keys = set()

    def dedup_key(e):
        # Dedup über die KOMBINATION der vier Kandidaten-Tage — zwei
        # Dateien mit exakt demselben 4er-Tupel geben uns keine neue
        # Info. Aber Dateien mit gleichem UTC-Tag, aber unterschiedlicher
        # NY/LA-Diskrimination, bleiben beide drin.
        cd = e["candidate_dates"]
        return (
            cd["UTC"], cd["Europe/Berlin"],
            cd["America/New_York"], cd["America/Los_Angeles"],
            e["has_gps"],
        )

    bucket_keys = sorted(buckets.keys())
    while len(result) < 10 and any(buckets[b] for b in bucket_keys):
        for b in bucket_keys:
            if len(result) >= 10:
                break
            while buckets[b]:
                cand = buckets[b].pop(0)
                k = dedup_key(cand)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                result.append(cand)
                break

    return result[:10]


# ---------------------------------------------------------------------------
# Teil 2: Schreiben des Ausfüll-Templates
# ---------------------------------------------------------------------------

TEMPLATE_HEADER = """\
================================================================
  GOOGLE PHOTOS TZ-KALIBRIERUNG - Ausfüllen und zurückgeben
================================================================

SO FUELLST DU DAS AUS:

  Für jede der 10 Dateien unten:
    1. Dateiname (fett markiert) kopieren
    2. Google Photos öffnen (photos.google.com)
    3. Oben in die Suchleiste den Dateinamen einfügen
    4. Das passende Foto anklicken
    5. Das Datum, das Google Photos OBEN zeigt
       ("Mo., 29. Juli 2023" oder ähnlich), ablesen
    6. In die Zeile 'GOOGLE_ZEIGT:' eintragen — Format: YYYY-MM-DD
       (Beispiel: GOOGLE_ZEIGT: 2023-07-29)

  WICHTIG: nur das DATUM, keine Uhrzeit.
  Bei jedem Eintrag steht rechts, welcher Tag bei welcher
  Zeitzone heraus käme — das hilft dir beim schnellen Tippen.

Anschließend diese Datei speichern und zurück an Claude geben, oder
lokal laufen lassen:
    python analyze_tz.py --verify tz_analysis.txt

================================================================

"""

TEMPLATE_FOOTER = """\

================================================================
  NACH DEM AUSFUELLEN
================================================================

Ist bei allen 10 Dateien GOOGLE_ZEIGT ausgefüllt, prüft der Script,
welche der vier Kandidaten-TZs zu ALLEN 10 Antworten passt. Wenn
genau eine passt, ist das deine Google-Photos-Anzeige-TZ.

Diese dann beim Repair-Lauf angeben:
    python repair.py --cluster-by-json-date --google-tz <TZ>

Oder in repair.bat eintragen.
"""


def write_template(entries: list, out_path: Path) -> None:
    lines = [TEMPLATE_HEADER]
    for i, e in enumerate(entries, start=1):
        cd = e["candidate_dates"]
        lines.append(f"----- Datei {i}/10 -----")
        lines.append(f"FILENAME : {e['path'].name}")
        lines.append(f"PATH     : {e['rel']}")
        lines.append(f"TIMESTAMP: {e['timestamp']}  ({e['formatted']})")
        lines.append(f"GPS      : {'ja' if e['has_gps'] else 'nein (geoData=0,0 oder fehlt)'}")
        lines.append(f"KANDIDATEN-TAGE (je nach TZ):")
        lines.append(f"  - UTC                : {cd['UTC']}")
        lines.append(f"  - Europe/Berlin      : {cd['Europe/Berlin']}")
        lines.append(f"  - America/New_York   : {cd['America/New_York']}")
        lines.append(f"  - America/Los_Angeles: {cd['America/Los_Angeles']}")
        lines.append(f"GOOGLE_ZEIGT: ")
        lines.append("")
    lines.append(TEMPLATE_FOOTER)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Teil 3: Verifizieren
# ---------------------------------------------------------------------------

ENTRY_RE = re.compile(
    r"FILENAME\s*:\s*(?P<filename>.+?)\s*\n"
    r"PATH\s*:\s*.+?\n"
    r"TIMESTAMP:\s*(?P<ts>\d+).+?\n"
    r"GPS\s*:.+?\n"
    r"KANDIDATEN-TAGE.+?\n"
    r"\s*-\s*UTC\s*:\s*(?P<utc>\d{4}-\d{2}-\d{2})\s*\n"
    r"\s*-\s*Europe/Berlin\s*:\s*(?P<berlin>\d{4}-\d{2}-\d{2})\s*\n"
    r"\s*-\s*America/New_York\s*:\s*(?P<ny>\d{4}-\d{2}-\d{2})\s*\n"
    r"\s*-\s*America/Los_Angeles\s*:\s*(?P<la>\d{4}-\d{2}-\d{2})\s*\n"
    r"GOOGLE_ZEIGT\s*:\s*(?P<answer>[^\n]*)\n",
    re.DOTALL,
)

DATE_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
DATE_DE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")


def parse_user_date(s: str):
    s = s.strip()
    if not s:
        return None
    m = DATE_RE.match(s)
    if m:
        y, mo, d = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    m = DATE_DE_RE.match(s)
    if m:
        d, mo, y = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    return None


def verify(path: Path, tz_out_path: Path = None) -> int:
    text = path.read_text(encoding="utf-8")
    entries = list(ENTRY_RE.finditer(text))
    if not entries:
        print(f"[FEHLER] Keine Einträge gefunden in {path}.")
        print("        Bitte die von analyze_tz.py erzeugte Datei benutzen.")
        return 2

    filled = []
    for m in entries:
        d = m.groupdict()
        ans = parse_user_date(d["answer"])
        filled.append({
            "filename": d["filename"].strip(),
            "candidates": {
                "UTC": d["utc"],
                "Europe/Berlin": d["berlin"],
                "America/New_York": d["ny"],
                "America/Los_Angeles": d["la"],
            },
            "answer": ans,
        })

    n_total = len(filled)
    n_answered = sum(1 for e in filled if e["answer"])
    print(f"Einträge: {n_total}, ausgefüllt: {n_answered}")
    if n_answered == 0:
        print("[FEHLER] Kein einziges GOOGLE_ZEIGT ausgefüllt.")
        return 2
    if n_answered < n_total:
        print(f"[WARNUNG] {n_total - n_answered} Einträge leer — "
              f"Auswertung läuft mit den übrigen, Ergebnis könnte mehrdeutig "
              f"sein.")

    # Für jede Kandidaten-TZ: wie viele Antworten stimmen überein?
    scores = {label: {"match": 0, "mismatch": 0, "details": []}
              for label, _ in CANDIDATES}
    for e in filled:
        if not e["answer"]:
            continue
        for label, _ in CANDIDATES:
            cand = e["candidates"][label]
            if cand == e["answer"]:
                scores[label]["match"] += 1
            else:
                scores[label]["mismatch"] += 1
                scores[label]["details"].append(
                    f"  {e['filename']}: erwartet {e['answer']}, {label} sagt {cand}"
                )

    print("\nErgebnis pro Kandidaten-TZ:")
    print(f"  {'TZ':<22} {'Treffer':>8} {'Fehl':>6}")
    for label, _ in CANDIDATES:
        s = scores[label]
        flag = "  *" if s["mismatch"] == 0 else ""
        print(f"  {label:<22} {s['match']:>8} {s['mismatch']:>6}{flag}")

    perfect = [lbl for lbl, _ in CANDIDATES if scores[lbl]["mismatch"] == 0
               and scores[lbl]["match"] > 0]
    if len(perfect) == 1:
        detected_tz = perfect[0]
        print(f"\n=> EINDEUTIG: Google Photos nutzt die TZ '{detected_tz}'.")
        if tz_out_path is not None:
            tz_out_path.write_text(detected_tz + "\n", encoding="utf-8")
            print(f"   TZ gespeichert in: {tz_out_path}")
        print(f"   Für den Repair-Lauf:")
        print(f"       python repair.py --cluster-by-json-date "
              f"--google-tz {detected_tz}")
        return 0

    if len(perfect) > 1:
        print(f"\n=> Mehrdeutig: diese TZs passen alle: {perfect}")
        print("   Die verwendeten Dateien unterscheiden diese TZs nicht.")
        print("   Füge bitte noch Dateien mit Timestamps zwischen 04:00 "
              "und 08:00 UTC hinzu, oder starte analyze_tz.py nochmal "
              "(die Auswahl ist deterministisch, bringt aber mehr Vielfalt "
              "bei grösseren Takeouts).")
        return 1

    print("\n=> Keine der Kandidaten-TZs passt zu allen deinen Antworten.")
    print("   Entweder ist Google Photos' TZ-Logik bei dir inkonsistent,")
    print("   oder eine Antwort wurde falsch eingetragen. Fehlerdetails:")
    worst = min(scores.items(), key=lambda x: x[1]["mismatch"])
    print(f"   (engste Kandidaten-TZ: {worst[0]}, nur {worst[1]['mismatch']} Fehl)")
    for line in worst[1]["details"][:5]:
        print(line)
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="TZ-Kalibrierung für Google Photos Repair."
    )
    ap.add_argument("--temp", default="./temp_analyze",
                    help="Entpackter Takeout-Ordner (Standard: ./temp_analyze)")
    ap.add_argument("--out", default="tz_analysis.txt",
                    help="Auszufüllendes Template (Standard: tz_analysis.txt)")
    ap.add_argument("--verify", metavar="FILE", default=None,
                    help="Ausgefülltes Template auswerten statt neues "
                         "zu erzeugen.")
    ap.add_argument("--save-tz", default="google_tz.txt",
                    help="Pfad wohin die erkannte TZ bei erfolgreicher "
                         "Verifikation geschrieben wird "
                         "(Standard: google_tz.txt)")
    args = ap.parse_args()

    if args.verify:
        sys.exit(verify(
            Path(args.verify).resolve(),
            Path(args.save_tz).resolve() if args.save_tz else None,
        ))

    temp_dir = Path(args.temp).resolve()
    if not temp_dir.is_dir():
        print(f"[FEHLER] Temp-Ordner nicht gefunden: {temp_dir}")
        sys.exit(1)

    print(f"Scanne {temp_dir} ...")
    entries = scan_candidates(temp_dir)
    print(f"  {len(entries)} Dateien mit gültigem photoTakenTime gefunden.")

    picked = pick_10(entries)
    if len(picked) < 10:
        print(f"[WARNUNG] Nur {len(picked)} brauchbare Kalibrierungs-"
              f"Kandidaten — kleinerer Takeout als erwartet.")

    out_path = Path(args.out).resolve()
    write_template(picked, out_path)

    distinct_dist = {}
    for p in picked:
        distinct_dist.setdefault(p["distinct"], 0)
        distinct_dist[p["distinct"]] += 1
    print(f"\nAusgewaehlte Dateien (distinct-Klasse = {distinct_dist}):")
    for i, p in enumerate(picked, 1):
        cd = p["candidate_dates"]
        gps = "+GPS" if p["has_gps"] else "-GPS"
        print(f"  {i:2d}. {p['path'].name}  [{gps}]  "
              f"UTC={cd['UTC']}  Berl={cd['Europe/Berlin']}  "
              f"NY={cd['America/New_York']}  LA={cd['America/Los_Angeles']}")

    print(f"\nTemplate geschrieben: {out_path}")
    print()
    print("NÄCHSTER SCHRITT:")
    print(f"  1. {out_path.name} öffnen")
    print("  2. Jede der 10 Dateinamen in Google Photos suchen")
    print("  3. Das von Google gezeigte Datum in 'GOOGLE_ZEIGT:' eintragen")
    print(f"  4. Speichern und dann:")
    print(f"     python analyze_tz.py --verify {out_path.name}")


if __name__ == "__main__":
    main()
