#!/usr/bin/env python3
"""
Google Photos Repair CLI – Main entry point.

Processes Google Takeout exports: extracts ZIPs, matches JSON sidecars,
repairs EXIF metadata, renames files, detects duplicates, and generates reports.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from tqdm import tqdm

from modules.extractor import extract_all
from modules.matcher import match_all
from modules.metadata import (
    check_exiftool,
    process_file,
    ALL_IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from modules.renamer import rename_all
from modules.duplicates import run_duplicate_detection
from modules.reporter import write_repair_log, write_duplicates_report, write_summary

console = Console()

MEDIA_EXTENSIONS = ALL_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {
    ".gif", ".bmp", ".svg", ".raw", ".cr2", ".nef", ".arw", ".dng",
}

BATCH_SIZE = 1000


def parse_args():
    parser = argparse.ArgumentParser(
        prog="google-photos-repair",
        description="Repair Google Takeout photo/video exports: "
                    "fix EXIF, set timestamps, rename, deduplicate.",
    )
    parser.add_argument(
        "--input", default="./input",
        help="Input folder containing ZIP files (default: ./input)",
    )
    parser.add_argument(
        "--output", default="./output",
        help="Output folder for repaired files (default: ./output)",
    )
    parser.add_argument(
        "--temp", default="./temp",
        help="Temp folder for ZIP extraction (default: ./temp)",
    )
    parser.add_argument(
        "--phash-threshold", type=int, default=8,
        help="Hamming distance threshold for visual duplicates (default: 8)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate everything, delete nothing, still generate reports",
    )
    parser.add_argument(
        "--skip-duplicates", action="store_true",
        help="Skip the duplicate detection phase",
    )
    parser.add_argument(
        "--skip-extraction", action="store_true",
        help="Skip ZIP extraction (use if already extracted to temp)",
    )
    return parser.parse_args()


def scan_media_files(temp_dir: str) -> list:
    """Recursively find all media files in temp directory."""
    temp_path = Path(temp_dir)
    media_files = []

    for f in temp_path.rglob("*"):
        if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS:
            media_files.append(f)

    return sorted(media_files)


def print_config(args, exiftool_ok: bool):
    """Print startup configuration summary."""
    table = Table(title="Configuration", show_header=False, border_style="blue")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    table.add_row("Input folder", str(args.input))
    table.add_row("Output folder", str(args.output))
    table.add_row("Temp folder", str(args.temp))
    table.add_row("pHash threshold", str(args.phash_threshold))
    table.add_row("Dry run", str(args.dry_run))
    table.add_row("Skip duplicates", str(args.skip_duplicates))
    table.add_row("Skip extraction", str(args.skip_extraction))
    table.add_row("ExifTool available", "Yes" if exiftool_ok else "No")
    console.print(table)
    console.print()


def print_phase(name: str, number: int, total: int):
    """Print a phase header."""
    console.print(Panel(
        f"[bold white]Phase {number}/{total}: {name}[/bold white]",
        border_style="green",
    ))


def print_phase_stats(stats: dict):
    """Print stats after a phase."""
    table = Table(show_header=False, border_style="dim")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    for key, val in stats.items():
        table.add_row(str(key).replace("_", " ").title(), str(val))
    console.print(table)
    console.print()


def main():
    args = parse_args()
    start_time = time.time()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("repair.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logger = logging.getLogger("repair")

    console.print(Panel(
        "[bold cyan]Google Photos Repair CLI[/bold cyan]\n"
        "Process Google Takeout exports with metadata repair & deduplication",
        border_style="cyan",
    ))

    # Check ExifTool
    exiftool_ok = check_exiftool()
    if not exiftool_ok:
        console.print(
            "[yellow]Warning:[/yellow] ExifTool not found. "
            "HEIC/HEIF and video EXIF writing will be skipped.\n"
            "Install from: https://exiftool.org/\n"
        )

    print_config(args, exiftool_ok)

    total_phases = 5 if not args.skip_duplicates else 4
    if args.skip_extraction:
        total_phases -= 1

    phase_num = 0

    # ------------------------------------------------------------------
    # Phase 1: Extract ZIPs
    # ------------------------------------------------------------------
    if not args.skip_extraction:
        phase_num += 1
        print_phase("ZIP Extraction", phase_num, total_phases)

        input_path = Path(args.input)
        if not input_path.exists():
            console.print(f"[red]Error:[/red] Input folder not found: {args.input}")
            sys.exit(1)

        extract_stats = extract_all(args.input, args.temp)
        print_phase_stats(extract_stats)
    else:
        console.print("[dim]Skipping ZIP extraction (--skip-extraction)[/dim]\n")

    # ------------------------------------------------------------------
    # Phase 2: Scan & Match
    # ------------------------------------------------------------------
    phase_num += 1
    print_phase("Scan & Match Media Files", phase_num, total_phases)

    temp_path = Path(args.temp)
    if not temp_path.exists():
        console.print(f"[red]Error:[/red] Temp folder not found: {args.temp}")
        sys.exit(1)

    console.print("[dim]Scanning for media files...[/dim]")
    media_files = scan_media_files(args.temp)
    console.print(f"Found [bold]{len(media_files)}[/bold] media files\n")

    if not media_files:
        console.print("[yellow]No media files found. Nothing to do.[/yellow]")
        sys.exit(0)

    console.print("[dim]Matching JSON sidecars...[/dim]")
    matched_files = match_all(media_files, args.temp)
    matched_count = sum(1 for _, j in matched_files if j is not None)
    unmatched_count = len(matched_files) - matched_count

    print_phase_stats({
        "total_media_files": len(media_files),
        "json_matched": matched_count,
        "no_json_found": unmatched_count,
    })

    # ------------------------------------------------------------------
    # Phase 3: Metadata Repair (EXIF + timestamps)
    # ------------------------------------------------------------------
    phase_num += 1
    print_phase("Metadata Repair (EXIF + Timestamps)", phase_num, total_phases)

    process_results = []
    exif_written_count = 0
    exiftool_used_count = 0
    timestamps_set_count = 0
    no_timestamp_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Processing metadata", total=len(matched_files))

        for i in range(0, len(matched_files), BATCH_SIZE):
            batch = matched_files[i : i + BATCH_SIZE]

            for media_path, json_path in batch:
                try:
                    result = process_file(media_path, json_path)
                    process_results.append(result)

                    if result.get("exif_written"):
                        exif_written_count += 1
                    if result.get("exiftool_used"):
                        exiftool_used_count += 1
                    if result.get("timestamp_used"):
                        timestamps_set_count += 1
                    else:
                        no_timestamp_count += 1

                except Exception as e:
                    logger.error("Unhandled error processing %s: %s", media_path, e)
                    process_results.append({
                        "original_path": str(media_path),
                        "timestamp_used": None,
                        "timestamp_source": None,
                        "exif_written": False,
                        "exiftool_used": False,
                        "status": f"error: {e}",
                    })

                progress.advance(task)

    print_phase_stats({
        "exif_written": exif_written_count,
        "exiftool_used": exiftool_used_count,
        "timestamps_set": timestamps_set_count,
        "no_timestamp": no_timestamp_count,
    })

    # ------------------------------------------------------------------
    # Phase 4: Rename & Copy to Output
    # ------------------------------------------------------------------
    phase_num += 1
    print_phase("Rename & Copy to Output", phase_num, total_phases)

    rename_results = rename_all(matched_files, process_results, args.temp, args.output)

    copy_ok = sum(1 for r in rename_results if r["status"] == "ok")
    copy_fail = len(rename_results) - copy_ok

    print_phase_stats({
        "files_copied": copy_ok,
        "copy_failures": copy_fail,
    })

    # ------------------------------------------------------------------
    # Phase 5: Duplicate Detection
    # ------------------------------------------------------------------
    exact_dupes = 0
    visual_dupes = 0
    exact_deletions = []
    visual_deletions = []

    if not args.skip_duplicates:
        phase_num += 1
        print_phase("Duplicate Detection", phase_num, total_phases)

        dup_results = run_duplicate_detection(
            output_dir=args.output,
            db_path="photos.db",
            phash_threshold=args.phash_threshold,
            dry_run=args.dry_run,
        )

        exact_dupes = dup_results["exact_duplicates_deleted"]
        visual_dupes = dup_results["visual_duplicates_deleted"]
        exact_deletions = dup_results["exact_deletions"]
        visual_deletions = dup_results["visual_deletions"]

        print_phase_stats({
            "files_scanned": dup_results["total_files_scanned"],
            "exact_duplicates_deleted": exact_dupes,
            "visual_duplicates_deleted": visual_dupes,
        })
    else:
        console.print("[dim]Skipping duplicate detection (--skip-duplicates)[/dim]\n")

    # ------------------------------------------------------------------
    # Generate Reports
    # ------------------------------------------------------------------
    console.print(Panel("[bold white]Generating Reports[/bold white]", border_style="green"))

    processing_time = time.time() - start_time

    repair_log_path = write_repair_log(args.output, process_results, rename_results)
    dupes_report_path = write_duplicates_report(args.output, exact_deletions, visual_deletions)
    summary_path = write_summary(
        output_dir=args.output,
        total_files=len(media_files),
        exif_written=exif_written_count,
        timestamps_set=timestamps_set_count,
        exact_dupes=exact_dupes,
        visual_dupes=visual_dupes,
        no_json=unmatched_count,
        no_timestamp=no_timestamp_count,
        exiftool_available=exiftool_ok,
        processing_time=processing_time,
    )

    console.print(f"  Repair log:        {repair_log_path}")
    console.print(f"  Duplicates report: {dupes_report_path}")
    console.print(f"  Summary:           {summary_path}")
    console.print()

    # ------------------------------------------------------------------
    # Final Summary
    # ------------------------------------------------------------------
    final_table = Table(title="Final Summary", border_style="cyan")
    final_table.add_column("Metric", style="bold")
    final_table.add_column("Value", justify="right")
    final_table.add_row("Total files processed", str(len(media_files)))
    final_table.add_row("EXIF written", str(exif_written_count))
    final_table.add_row("Timestamps set", str(timestamps_set_count))
    final_table.add_row("Files copied to output", str(copy_ok))
    final_table.add_row("Exact duplicates deleted", str(exact_dupes))
    final_table.add_row("Visual duplicates deleted", str(visual_dupes))
    final_table.add_row("Files with no JSON", str(unmatched_count))
    final_table.add_row("Processing time", f"{processing_time:.1f}s")

    if args.dry_run:
        final_table.add_row("[yellow]Mode[/yellow]", "[yellow]DRY RUN[/yellow]")

    console.print(final_table)
    console.print("\n[bold green]Done![/bold green]\n")


if __name__ == "__main__":
    main()
