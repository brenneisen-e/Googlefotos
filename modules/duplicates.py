"""Duplicate detection and deletion module.

Uses MD5 for exact duplicates and pHash (via imagehash) for visual duplicates.
Stores file metadata in a SQLite database for efficient querying.
Uses a BK-tree for sublinear pHash comparison.
"""

import hashlib
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import List, Optional, Tuple

import imagehash
from PIL import Image

from modules.metadata import ALL_IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


# ---------------------------------------------------------------------------
# BK-tree for efficient pHash comparison
# ---------------------------------------------------------------------------

class BKTreeNode:
    __slots__ = ("hash_val", "filepath", "children")

    def __init__(self, hash_val: int, filepath: str):
        self.hash_val = hash_val
        self.filepath = filepath
        self.children: dict = {}  # distance -> BKTreeNode


class BKTree:
    """BK-tree for Hamming distance queries on integer hashes."""

    def __init__(self):
        self.root: Optional[BKTreeNode] = None
        self.size = 0

    @staticmethod
    def _hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    def insert(self, hash_val: int, filepath: str) -> None:
        node = BKTreeNode(hash_val, filepath)
        if self.root is None:
            self.root = node
            self.size += 1
            return

        current = self.root
        while True:
            d = self._hamming(current.hash_val, hash_val)
            if d == 0 and current.filepath != filepath:
                # Exact hash match but different file – still insert
                # Use a small offset to differentiate
                d = 1
                while d in current.children:
                    d += 1
                current.children[d] = node
                self.size += 1
                return
            if d in current.children:
                current = current.children[d]
            else:
                current.children[d] = node
                self.size += 1
                return

    def query(self, hash_val: int, threshold: int) -> List[Tuple[str, int]]:
        """Find all entries within `threshold` Hamming distance.

        Returns list of (filepath, distance).
        """
        if self.root is None:
            return []

        results = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            d = self._hamming(node.hash_val, hash_val)
            if d <= threshold:
                results.append((node.filepath, d))
            # BK-tree pruning: only explore children with keys in [d-threshold, d+threshold]
            for key, child in node.children.items():
                if d - threshold <= key <= d + threshold:
                    stack.append(child)
        return results


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with WAL mode."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            filepath TEXT PRIMARY KEY,
            md5 TEXT,
            phash INTEGER,
            width INTEGER,
            height INTEGER,
            filesize INTEGER,
            timestamp TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_md5 ON files(md5)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phash ON files(phash)")
    conn.commit()
    return conn


def store_file_info(conn: sqlite3.Connection, info: dict) -> None:
    """Insert or replace file info in the database."""
    conn.execute(
        "INSERT OR REPLACE INTO files (filepath, md5, phash, width, height, filesize, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            info["filepath"],
            info["md5"],
            info.get("phash"),
            info.get("width"),
            info.get("height"),
            info["filesize"],
            info.get("timestamp"),
        ),
    )


# ---------------------------------------------------------------------------
# Hash computation (designed for multiprocessing)
# ---------------------------------------------------------------------------

def _compute_md5(filepath: str) -> str:
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_file_info(filepath: str) -> dict:
    """Compute MD5, pHash (for images), dimensions, and size."""
    info = {
        "filepath": filepath,
        "md5": None,
        "phash": None,
        "width": None,
        "height": None,
        "filesize": 0,
        "timestamp": None,
    }

    try:
        info["filesize"] = os.path.getsize(filepath)
        info["md5"] = _compute_md5(filepath)

        # Get file mtime as timestamp
        mtime = os.path.getmtime(filepath)
        info["timestamp"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        # Compute pHash for images
        ext = Path(filepath).suffix.lower()
        if ext in ALL_IMAGE_EXTENSIONS:
            try:
                with Image.open(filepath) as img:
                    info["width"] = img.width
                    info["height"] = img.height
                    phash = imagehash.phash(img)
                    info["phash"] = int(str(phash), 16)
            except Exception as e:
                logger.debug("Cannot compute pHash for %s: %s", filepath, e)

    except Exception as e:
        logger.warning("Error computing info for %s: %s", filepath, e)

    return info


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def find_exact_duplicates(conn: sqlite3.Connection, dry_run: bool = False) -> list:
    """Step 1: Find and delete exact duplicates (same MD5).

    Keeps the file with the earliest timestamp in each group.
    Returns list of deletion records.
    """
    cursor = conn.execute(
        "SELECT md5, GROUP_CONCAT(filepath, '|||'), GROUP_CONCAT(timestamp, '|||') "
        "FROM files WHERE md5 IS NOT NULL GROUP BY md5 HAVING COUNT(*) > 1"
    )

    deletions = []

    for row in cursor.fetchall():
        md5 = row[0]
        filepaths = row[1].split("|||")
        timestamps = row[2].split("|||") if row[2] else [""] * len(filepaths)

        # Sort by timestamp (earliest first), then by path for stability
        pairs = list(zip(filepaths, timestamps))
        pairs.sort(key=lambda x: (x[1] or "9999", x[0]))

        keeper = pairs[0]
        for fp, ts in pairs[1:]:
            deletions.append({
                "kept_file": keeper[0],
                "deleted_file": fp,
                "duplicate_type": "exact",
                "similarity_score": "1.0",
                "timestamp_kept": keeper[1],
                "timestamp_deleted": ts,
            })

            if not dry_run:
                try:
                    os.unlink(fp)
                    conn.execute("DELETE FROM files WHERE filepath = ?", (fp,))
                    logger.info("Deleted exact duplicate: %s (kept %s)", fp, keeper[0])
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", fp, e)

    if deletions:
        conn.commit()

    return deletions


def find_visual_duplicates(
    conn: sqlite3.Connection,
    threshold: int = 8,
    dry_run: bool = False,
) -> list:
    """Step 2: Find and delete visual duplicates (similar pHash).

    Uses BK-tree for efficient comparison.
    Keeps the file with the earliest timestamp in each group.
    Returns list of deletion records.
    """
    # Load all files with pHash
    cursor = conn.execute(
        "SELECT filepath, phash, timestamp FROM files WHERE phash IS NOT NULL"
    )
    rows = cursor.fetchall()

    if len(rows) < 2:
        return []

    # Build BK-tree
    tree = BKTree()
    file_info = {}
    for filepath, phash, timestamp in rows:
        tree.insert(phash, filepath)
        file_info[filepath] = {"phash": phash, "timestamp": timestamp or ""}

    # Find groups of visual duplicates using Union-Find
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    processed = set()
    for filepath, phash, timestamp in rows:
        matches = tree.query(phash, threshold)
        for match_path, dist in matches:
            if match_path != filepath and match_path not in processed:
                union(filepath, match_path)
        processed.add(filepath)

    # Group by root
    groups = defaultdict(list)
    for filepath in file_info:
        if find(filepath) != filepath or any(
            find(f) == find(filepath) for f in file_info if f != filepath and find(f) == find(filepath)
        ):
            groups[find(filepath)].append(filepath)

    # Filter to groups with 2+ members
    deletions = []
    already_deleted = set()

    for root, members in groups.items():
        if len(members) < 2:
            continue

        # Sort by timestamp (earliest first)
        members.sort(key=lambda fp: (file_info[fp]["timestamp"], fp))
        keeper = members[0]

        for fp in members[1:]:
            if fp in already_deleted:
                continue

            # Compute actual distance for the record
            dist = BKTree._hamming(file_info[keeper]["phash"], file_info[fp]["phash"])
            similarity = 1.0 - (dist / 64.0)

            deletions.append({
                "kept_file": keeper,
                "deleted_file": fp,
                "duplicate_type": "visual",
                "similarity_score": f"{similarity:.4f}",
                "timestamp_kept": file_info[keeper]["timestamp"],
                "timestamp_deleted": file_info[fp]["timestamp"],
            })

            already_deleted.add(fp)

            if not dry_run:
                try:
                    os.unlink(fp)
                    conn.execute("DELETE FROM files WHERE filepath = ?", (fp,))
                    logger.info("Deleted visual duplicate: %s (kept %s)", fp, keeper)
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", fp, e)

    if deletions:
        conn.commit()

    return deletions


def run_duplicate_detection(
    output_dir: str,
    db_path: str = "photos.db",
    phash_threshold: int = 8,
    dry_run: bool = False,
) -> dict:
    """Run full duplicate detection pipeline.

    Returns dict with stats and deletion records.
    """
    conn = init_db(db_path)

    # Collect all files in output dir
    output_path = Path(output_dir)
    all_files = [
        str(f) for f in output_path.rglob("*")
        if f.is_file() and f.suffix.lower() in (ALL_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS)
    ]

    logger.info("Computing hashes for %d files...", len(all_files))

    # Compute file info using multiprocessing
    num_workers = max(1, cpu_count() - 1)
    results = []

    # Process in batches to avoid memory issues
    for i in range(0, len(all_files), BATCH_SIZE):
        batch = all_files[i : i + BATCH_SIZE]
        try:
            with Pool(processes=num_workers) as pool:
                batch_results = pool.map(_compute_file_info, batch)
            results.extend(batch_results)
        except Exception as e:
            logger.warning("Multiprocessing failed, falling back to sequential: %s", e)
            for fp in batch:
                results.append(_compute_file_info(fp))

    # Store in database
    for info in results:
        if info["md5"]:  # Only store if we could compute the hash
            store_file_info(conn, info)
    conn.commit()

    # Step 1: Exact duplicates (MD5)
    logger.info("Finding exact duplicates (MD5)...")
    exact_deletions = find_exact_duplicates(conn, dry_run)

    # Step 2: Visual duplicates (pHash, images only)
    logger.info("Finding visual duplicates (pHash, threshold=%d)...", phash_threshold)
    visual_deletions = find_visual_duplicates(conn, phash_threshold, dry_run)

    conn.close()

    return {
        "total_files_scanned": len(all_files),
        "exact_duplicates_deleted": len(exact_deletions),
        "visual_duplicates_deleted": len(visual_deletions),
        "exact_deletions": exact_deletions,
        "visual_deletions": visual_deletions,
    }
