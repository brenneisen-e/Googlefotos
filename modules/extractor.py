"""ZIP extraction module for Google Takeout exports."""

import os
import zipfile
import logging
from pathlib import Path

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

logger = logging.getLogger(__name__)


def extract_all(input_dir: str, temp_dir: str) -> dict:
    """Extract all ZIP files from input_dir to temp_dir.

    Returns dict with stats: total_zips, extracted, skipped, nested_extracted.
    """
    input_path = Path(input_dir)
    temp_path = Path(temp_dir)
    temp_path.mkdir(parents=True, exist_ok=True)

    zip_files = sorted(input_path.glob("*.zip"))
    stats = {
        "total_zips": len(zip_files),
        "extracted": 0,
        "skipped": 0,
        "nested_extracted": 0,
    }

    if not zip_files:
        logger.warning("No ZIP files found in %s", input_dir)
        return stats

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Extracting ZIPs", total=len(zip_files))

        for zf in zip_files:
            dest = temp_path / zf.stem
            dest.mkdir(parents=True, exist_ok=True)
            try:
                _extract_zip(zf, dest, stats, progress)
                stats["extracted"] += 1
                logger.info("Extracted: %s", zf.name)
            except (zipfile.BadZipFile, OSError) as e:
                stats["skipped"] += 1
                logger.error("Corrupt/unreadable ZIP skipped: %s – %s", zf.name, e)
            progress.advance(task)

    return stats


def _extract_zip(zip_path: Path, dest: Path, stats: dict, progress) -> None:
    """Extract a single ZIP, handling nested ZIPs recursively."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest)

    # Check for nested ZIPs
    nested_zips = list(dest.rglob("*.zip"))
    for nested in nested_zips:
        nested_dest = nested.parent / nested.stem
        nested_dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(nested, "r") as nzf:
                nzf.extractall(nested_dest)
            stats["nested_extracted"] += 1
            logger.info("Nested ZIP extracted: %s", nested.name)
            nested.unlink()  # Remove nested ZIP after extraction
        except (zipfile.BadZipFile, OSError) as e:
            logger.error("Corrupt nested ZIP skipped: %s – %s", nested.name, e)
