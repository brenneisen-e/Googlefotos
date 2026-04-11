"""Report generation module – CSV and summary output."""

import csv
import logging
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def write_repair_log(
    output_dir: str,
    process_results: list,
    rename_results: list,
) -> str:
    """Write repair_log.csv with per-file processing results.

    Columns: original_path, new_filename, timestamp_used, timestamp_source,
             exif_written, exiftool_used, date_mismatch, status
    """
    csv_path = Path(output_dir) / "repair_log.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # Merge process and rename results by original_path
    rename_map = {}
    for rr in rename_results:
        rename_map[rr["original_path"]] = rr

    rows = []
    for pr in process_results:
        orig = pr["original_path"]
        rr = rename_map.get(orig, {})
        rows.append({
            "original_path": orig,
            "new_filename": rr.get("new_filename", ""),
            "timestamp_used": pr.get("timestamp_used", ""),
            "timestamp_source": pr.get("timestamp_source", ""),
            "json_date": pr.get("json_date", "") or "",
            "exif_written": str(pr.get("exif_written", False)),
            "exiftool_used": str(pr.get("exiftool_used", False)),
            "date_mismatch": pr.get("date_mismatch", ""),
            "status": pr.get("status", rr.get("status", "")),
        })

    fieldnames = [
        "original_path", "new_filename", "timestamp_used",
        "timestamp_source", "json_date", "exif_written", "exiftool_used",
        "date_mismatch", "status",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Repair log written: %s (%d entries)", csv_path, len(rows))
    return str(csv_path)


def write_duplicates_report(
    output_dir: str,
    exact_deletions: list,
    visual_deletions: list,
) -> str:
    """Write duplicates_report.csv.

    Columns: kept_file, deleted_file, duplicate_type, similarity_score,
             timestamp_kept, timestamp_deleted
    """
    csv_path = Path(output_dir) / "duplicates_report.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    all_deletions = exact_deletions + visual_deletions

    fieldnames = [
        "kept_file", "deleted_file", "duplicate_type",
        "similarity_score", "timestamp_kept", "timestamp_deleted",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_deletions)

    logger.info("Duplicates report written: %s (%d entries)", csv_path, len(all_deletions))
    return str(csv_path)


def write_summary(
    output_dir: str,
    total_files: int,
    exif_written: int,
    timestamps_set: int,
    exact_dupes: int,
    visual_dupes: int,
    no_json: int,
    no_timestamp: int,
    exiftool_available: bool,
    processing_time: float,
    date_mismatches: int = 0,
    flagged_mtime: int = 0,
    source_counts: dict = None,
) -> str:
    """Write summary.txt with overall statistics."""
    summary_path = Path(output_dir) / "summary.txt"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    duration = str(timedelta(seconds=int(processing_time)))

    lines = [
        "=" * 60,
        "  Google Photos Repair – Summary",
        "=" * 60,
        "",
        f"  Total files processed:         {total_files:>8}",
        f"  EXIF written:                  {exif_written:>8}",
        f"  Timestamps set:                {timestamps_set:>8}",
        f"  Exact duplicates deleted:      {exact_dupes:>8}",
        f"  Visual duplicates deleted:     {visual_dupes:>8}",
        f"  Files with no JSON sidecar:    {no_json:>8}",
        f"  Files with no timestamp found: {no_timestamp:>8}",
        f"  Date mismatches (fn vs json):  {date_mismatches:>8}",
        f"  Flagged (mtime only):          {flagged_mtime:>8}",
        f"  ExifTool available:            {'yes':>8}" if exiftool_available else f"  ExifTool available:            {'no':>8}",
        "",
    ]

    # Timestamp source breakdown
    if source_counts:
        lines.append("  Timestamp sources:")
        for src in sorted(source_counts, key=source_counts.get, reverse=True):
            lines.append(f"    {src:<30s} {source_counts[src]:>8}")
        lines.append("")

    lines.extend([
        f"  Processing time:               {duration:>8}",
        "",
        "  Timestamp priority order:",
        "    1. Filename (highest trust)",
        "    2. Google JSON (photoTakenTime / creationTime)",
        "    3. Existing EXIF DateTimeOriginal",
        "    4. File modification time (flagged)",
        "",
        "=" * 60,
    ])

    text = "\n".join(lines) + "\n"

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(text)

    logger.info("Summary written: %s", summary_path)
    return str(summary_path)
