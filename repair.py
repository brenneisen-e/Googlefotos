#!/usr/bin/env python3
"""
Google Photos Repair CLI – Main entry point.

Processes Google Takeout exports: extracts ZIPs, matches JSON sidecars,
repairs EXIF metadata, renames files, detects duplicates, and generates reports.
"""

import argparse
import logging
import math
import random
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from tqdm import tqdm

from modules.extractor import extract_all
from modules.matcher import match_all, find_json_for_media
from modules.metadata import (
    check_exiftool,
    process_file,
    ALL_IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from modules.renamer import rename_all
from modules.duplicates import run_duplicate_detection, init_db
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
    parser.add_argument(
        "--sample", type=int, default=None, metavar="N",
        help="Randomly sample N files for a test run (e.g. --sample 500)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume a previous run, skipping already-processed files",
    )
    parser.add_argument(
        "--cluster-by-json-date", action="store_true",
        help="Group output files into folders named after the date Google "
             "currently shows (JSON photoTakenTime, YYYY-MM-DD). Perfect for "
             "fixing cluster-misdated imports: delete all photos from one "
             "day in Google Photos, then re-upload the matching folder.",
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


# ---------------------------------------------------------------------------
# Resume tracking via SQLite
# ---------------------------------------------------------------------------

def init_resume_db(db_path: str) -> sqlite3.Connection:
    """Open/create the resume tracking database."""
    conn = init_db(db_path)  # reuses the duplicates schema + adds processed table
    return conn


def get_processed_files(conn: sqlite3.Connection, phase: str) -> set:
    """Get set of filepaths already processed for a given phase."""
    cursor = conn.execute(
        "SELECT filepath FROM processed WHERE phase = ?", (phase,)
    )
    return {row[0] for row in cursor.fetchall()}


def mark_processed(conn: sqlite3.Connection, filepath: str, phase: str) -> None:
    """Mark a file as processed for a given phase."""
    conn.execute(
        "INSERT OR REPLACE INTO processed (filepath, phase, completed_at) VALUES (?, ?, ?)",
        (filepath, phase, datetime.now(timezone.utc).isoformat()),
    )


def create_sample(
    media_files: list,
    sample_size: int,
    temp_dir: str,
    sample_dir: str,
) -> list:
    """Create a stratified random sample of media files.

    Selects files proportionally across subfolders so all years/albums
    are represented. Copies sampled media files + their JSON sidecars
    into sample_dir, preserving subfolder structure.

    Returns list of sampled media Paths (inside sample_dir).
    """
    random.seed(42)
    temp_path = Path(temp_dir)
    sample_path = Path(sample_dir)

    # Clean up any previous sample
    if sample_path.exists():
        shutil.rmtree(sample_path)
    sample_path.mkdir(parents=True, exist_ok=True)

    # Group files by their immediate subfolder relative to temp_dir
    groups = defaultdict(list)
    for mf in media_files:
        try:
            rel = mf.relative_to(temp_path)
            # Use the top-level subfolder as the stratum key
            parts = rel.parts
            key = parts[0] if len(parts) > 1 else "."
        except ValueError:
            key = "."
        groups[key].append(mf)

    # Cap sample_size to total available
    total = len(media_files)
    sample_size = min(sample_size, total)

    # Stratified sampling: allocate proportionally, at least 1 per group
    sampled = []

    # Sort groups for reproducibility
    sorted_keys = sorted(groups.keys())
    allocations = {}

    for key in sorted_keys:
        count = len(groups[key])
        alloc = max(1, math.floor(sample_size * count / total))
        allocations[key] = min(alloc, count)

    # Adjust if we over-allocated
    total_alloc = sum(allocations.values())
    if total_alloc > sample_size:
        # Trim from largest groups
        for key in sorted(allocations, key=lambda k: allocations[k], reverse=True):
            excess = total_alloc - sample_size
            if excess <= 0:
                break
            trim = min(excess, allocations[key] - 1)
            allocations[key] -= trim
            total_alloc -= trim

    # Distribute remaining slots to largest groups
    total_alloc = sum(allocations.values())
    if total_alloc < sample_size:
        deficit = sample_size - total_alloc
        for key in sorted(allocations, key=lambda k: len(groups[k]), reverse=True):
            can_add = len(groups[key]) - allocations[key]
            add = min(deficit, can_add)
            allocations[key] += add
            deficit -= add
            if deficit <= 0:
                break

    # Sample from each group
    for key in sorted_keys:
        group_files = groups[key]
        n = allocations.get(key, 0)
        chosen = random.sample(group_files, min(n, len(group_files)))
        sampled.extend(chosen)

    # Copy sampled files + their JSON sidecars to sample_dir
    sampled_in_sample_dir = []
    for mf in sampled:
        try:
            rel = mf.relative_to(temp_path)
        except ValueError:
            rel = Path(mf.name)

        dest = sample_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(mf), str(dest))

        # Find and copy JSON sidecar
        json_path = find_json_for_media(mf)
        if json_path and json_path.exists():
            try:
                json_rel = json_path.relative_to(temp_path)
            except ValueError:
                json_rel = Path(json_path.name)
            json_dest = sample_path / json_rel
            json_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(json_path), str(json_dest))

        sampled_in_sample_dir.append(dest)

    return sorted(sampled_in_sample_dir)


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
    if args.sample:
        table.add_row("Sample mode", f"{args.sample} files (seed=42)")
    if args.resume:
        table.add_row("Resume mode", "ON (skip already-processed)")
    if args.cluster_by_json_date:
        table.add_row("Cluster mode", "ON (folders by JSON date)")
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

    # ------------------------------------------------------------------
    # Sample mode: pick N files, copy to temp_sample, redirect paths
    # ------------------------------------------------------------------
    is_sample = args.sample is not None
    effective_temp = args.temp
    effective_output = args.output

    if is_sample:
        sample_temp = "./temp_sample"
        sample_output = "./output_sample"
        console.print(Panel(
            f"[bold yellow]SAMPLE MODE:[/bold yellow] Selecting {args.sample} "
            f"of {len(media_files)} files (seed=42, stratified)",
            border_style="yellow",
        ))
        sampled = create_sample(media_files, args.sample, args.temp, sample_temp)
        console.print(
            f"Sampled [bold]{len(sampled)}[/bold] files across "
            f"{len(set(p.parent for p in sampled))} subfolders → {sample_temp}/\n"
        )
        media_files = sampled
        effective_temp = sample_temp
        effective_output = sample_output

    console.print("[dim]Matching JSON sidecars...[/dim]")
    matched_files = match_all(media_files, effective_temp)
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

    # Resume tracking
    db_name = "photos_sample.db" if is_sample else "photos.db"
    resume_conn = init_resume_db(db_name) if args.resume else None
    already_metadata = get_processed_files(resume_conn, "metadata") if resume_conn else set()
    already_renamed = get_processed_files(resume_conn, "rename") if resume_conn else set()

    if args.resume and already_metadata:
        console.print(
            f"[dim]Resume: {len(already_metadata)} files already processed "
            f"(metadata), {len(already_renamed)} already renamed[/dim]\n"
        )

    process_results = []
    exif_written_count = 0
    exiftool_used_count = 0
    timestamps_set_count = 0
    no_timestamp_count = 0
    skipped_resume_count = 0
    date_mismatch_count = 0
    flagged_mtime_count = 0
    source_counts = {}

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
                file_key = str(media_path)

                # Skip if already processed in a previous run
                if file_key in already_metadata:
                    process_results.append({
                        "original_path": file_key,
                        "timestamp_used": None,
                        "timestamp_source": "resumed",
                        "exif_written": False,
                        "exiftool_used": False,
                        "date_mismatch": None,
                        "json_date": None,
                        "status": "skipped_resume",
                    })
                    skipped_resume_count += 1
                    progress.advance(task)
                    continue

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

                    if result.get("date_mismatch"):
                        date_mismatch_count += 1

                    src = result.get("timestamp_source", "unknown")
                    source_counts[src] = source_counts.get(src, 0) + 1

                    if result.get("status") == "flag_mtime_only":
                        flagged_mtime_count += 1

                    # Track in resume DB
                    if resume_conn:
                        mark_processed(resume_conn, file_key, "metadata")

                except Exception as e:
                    logger.error("Unhandled error processing %s: %s", media_path, e)
                    process_results.append({
                        "original_path": file_key,
                        "timestamp_used": None,
                        "timestamp_source": None,
                        "exif_written": False,
                        "exiftool_used": False,
                        "date_mismatch": None,
                        "json_date": None,
                        "status": f"error: {e}",
                    })

                progress.advance(task)

            # Commit resume progress per batch
            if resume_conn:
                resume_conn.commit()

    phase3_stats = {
        "exif_written": exif_written_count,
        "exiftool_used": exiftool_used_count,
        "timestamps_set": timestamps_set_count,
        "no_timestamp": no_timestamp_count,
    }
    if skipped_resume_count:
        phase3_stats["skipped_resume"] = skipped_resume_count
    if date_mismatch_count:
        phase3_stats["date_mismatches"] = date_mismatch_count
    if flagged_mtime_count:
        phase3_stats["flagged_mtime_only"] = flagged_mtime_count
    print_phase_stats(phase3_stats)

    # Show timestamp source breakdown
    if source_counts:
        src_table = Table(title="Timestamp Sources", show_header=False, border_style="dim")
        src_table.add_column("Source", style="bold")
        src_table.add_column("Count", justify="right")
        for src in sorted(source_counts, key=source_counts.get, reverse=True):
            src_table.add_row(src, str(source_counts[src]))
        console.print(src_table)
        console.print()

    # ------------------------------------------------------------------
    # Phase 4: Rename & Copy to Output
    # ------------------------------------------------------------------
    phase_num += 1
    print_phase("Rename & Copy to Output", phase_num, total_phases)

    # Filter out already-renamed files if resuming
    if args.resume and already_renamed:
        files_to_rename = []
        results_to_rename = []
        rename_results_skipped = []
        for (mp, jp), pr in zip(matched_files, process_results):
            if str(mp) in already_renamed:
                rename_results_skipped.append({
                    "original_path": str(mp),
                    "new_path": None,
                    "new_filename": None,
                    "status": "skipped_resume",
                })
            else:
                files_to_rename.append((mp, jp))
                results_to_rename.append(pr)

        new_rename_results = rename_all(
            files_to_rename, results_to_rename, effective_temp, effective_output,
            cluster_by_json_date=args.cluster_by_json_date,
        )

        # Mark newly renamed files
        if resume_conn:
            for rr in new_rename_results:
                if rr["status"] == "ok":
                    mark_processed(resume_conn, rr["original_path"], "rename")
            resume_conn.commit()

        rename_results = rename_results_skipped + new_rename_results
    else:
        rename_results = rename_all(
            matched_files, process_results, effective_temp, effective_output,
            cluster_by_json_date=args.cluster_by_json_date,
        )
        if resume_conn:
            for rr in rename_results:
                if rr["status"] == "ok":
                    mark_processed(resume_conn, rr["original_path"], "rename")
            resume_conn.commit()

    copy_ok = sum(1 for r in rename_results if r["status"] == "ok")
    copy_skip = sum(1 for r in rename_results if r["status"] == "skipped_resume")
    copy_fail = len(rename_results) - copy_ok - copy_skip

    phase4_stats = {"files_copied": copy_ok, "copy_failures": copy_fail}
    if copy_skip:
        phase4_stats["skipped_resume"] = copy_skip
    print_phase_stats(phase4_stats)

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

        dup_db = "photos_sample.db" if is_sample else "photos.db"
        dup_results = run_duplicate_detection(
            output_dir=effective_output,
            db_path=dup_db,
            phash_threshold=args.phash_threshold,
            dry_run=args.dry_run,
            resume=args.resume,
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

    # Close resume DB
    if resume_conn:
        resume_conn.close()

    # ------------------------------------------------------------------
    # Generate Reports
    # ------------------------------------------------------------------
    console.print(Panel("[bold white]Generating Reports[/bold white]", border_style="green"))

    processing_time = time.time() - start_time

    repair_log_path = write_repair_log(effective_output, process_results, rename_results)
    dupes_report_path = write_duplicates_report(effective_output, exact_deletions, visual_deletions)
    summary_path = write_summary(
        output_dir=effective_output,
        total_files=len(media_files),
        exif_written=exif_written_count,
        timestamps_set=timestamps_set_count,
        exact_dupes=exact_dupes,
        visual_dupes=visual_dupes,
        no_json=unmatched_count,
        no_timestamp=no_timestamp_count,
        exiftool_available=exiftool_ok,
        processing_time=processing_time,
        date_mismatches=date_mismatch_count,
        flagged_mtime=flagged_mtime_count,
        source_counts=source_counts,
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
    if date_mismatch_count:
        final_table.add_row("[yellow]Date mismatches (fn vs json)[/yellow]", f"[yellow]{date_mismatch_count}[/yellow]")
    if flagged_mtime_count:
        final_table.add_row("[yellow]Flagged (mtime only)[/yellow]", f"[yellow]{flagged_mtime_count}[/yellow]")
    final_table.add_row("Processing time", f"{processing_time:.1f}s")

    if args.dry_run:
        final_table.add_row("[yellow]Mode[/yellow]", "[yellow]DRY RUN[/yellow]")

    if is_sample:
        final_table.add_row("[yellow]Mode[/yellow]", f"[yellow]SAMPLE ({args.sample} files)[/yellow]")

    console.print(final_table)

    if is_sample:
        console.print()
        console.print(Panel(
            "[bold yellow]TESTLAUF mit {n} Dateien abgeschlossen.\n"
            "Starte den vollst\u00e4ndigen Export mit: python repair.py[/bold yellow]".format(
                n=args.sample
            ),
            border_style="yellow",
        ))
    else:
        console.print("\n[bold green]Done![/bold green]\n")


if __name__ == "__main__":
    main()
