# Google Photos Repair CLI

A Python CLI tool for processing Google Takeout photo/video exports at scale (100,000+ files). Repairs EXIF metadata, sets correct timestamps, renames files with dates, and detects/removes duplicates.

## Features

- **ZIP Extraction** – Automatically extracts Google Takeout ZIP files, including nested ZIPs
- **JSON Sidecar Matching** – Matches media files to their Google Takeout JSON metadata using multiple strategies
- **EXIF Metadata Repair** – Writes correct dates to EXIF (JPEG/TIFF via piexif, HEIC/video via ExifTool)
- **Timestamp Repair** – Sets file system Created/Modified timestamps
- **Smart Renaming** – Renames files to `YYYY-MM-DD_HHMMSS_originalname.ext` format
- **Duplicate Detection** – Removes exact duplicates (MD5) and visual duplicates (pHash with BK-tree)
- **Detailed Reports** – Generates CSV logs and a summary of all operations

## Installation

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Install ExifTool (optional but recommended)

ExifTool is required for writing EXIF data to HEIC/HEIF images and video files (MP4, MOV, AVI, M4V).

- **macOS:** `brew install exiftool`
- **Ubuntu/Debian:** `sudo apt install libimage-exiftool-perl`
- **Windows:** Download from [https://exiftool.org/](https://exiftool.org/)

Without ExifTool, the tool will still process these files (rename, set timestamps) but won't write EXIF metadata to them.

## Usage

### Basic usage

1. Place your Google Takeout ZIP files in `./input/`
2. Run:

```bash
python repair.py
```

3. Find repaired files in `./output/`

### Common scenarios

```bash
# Standard run with all features
python repair.py

# Custom input/output folders
python repair.py --input /path/to/zips --output /path/to/output

# Skip extraction if ZIPs are already extracted
python repair.py --skip-extraction

# Dry run – simulate everything, delete nothing
python repair.py --dry-run

# Skip duplicate detection for faster processing
python repair.py --skip-duplicates

# Adjust visual duplicate sensitivity (lower = stricter)
python repair.py --phash-threshold 4
```

### CLI arguments

| Argument | Default | Description |
|---|---|---|
| `--input` | `./input` | Folder containing Google Takeout ZIP files |
| `--output` | `./output` | Output folder for repaired files |
| `--temp` | `./temp` | Temp folder for ZIP extraction |
| `--phash-threshold` | `8` | Hamming distance for visual duplicate detection |
| `--dry-run` | off | Simulate everything, generate reports, delete nothing |
| `--skip-duplicates` | off | Skip the duplicate detection phase |
| `--skip-extraction` | off | Skip ZIP extraction (if already done) |

## How it works

### Processing pipeline

1. **Extract** – All `.zip` files from `./input/` are extracted to `./temp/`
2. **Scan & Match** – Media files are found and matched to their JSON sidecars
3. **Metadata Repair** – Timestamps are read, EXIF is written, file times are set
4. **Rename & Copy** – Files are copied to `./output/` with new date-based names
5. **Deduplicate** – Exact (MD5) and visual (pHash) duplicates are removed
6. **Report** – CSV logs and a summary file are generated

### JSON sidecar matching

Google Takeout creates JSON metadata files alongside each photo/video. The matching logic handles all of Google's naming patterns:

| Pattern | Example |
|---|---|
| **Exact** | `photo.jpg` → `photo.jpg.json` |
| **Stem only** | `photo.jpg` → `photo.json` |
| **Truncated (46 chars)** | `VeryLongFilename_that_exceeds_fortysix_chars.jpg` → `VeryLongFilename_that_exceeds_fortysix_char.jpg.json` |
| **Numbered duplicates** | `photo(1).jpg` → `photo(1).jpg.json` or `photo.jpg(1).json` |

All matching is **case-insensitive**.

### Timestamp priority

The tool picks the best available timestamp in this order:

1. `photoTakenTime.timestamp` from the Google JSON
2. `creationTime.timestamp` from the Google JSON
3. Parsed from the filename (patterns: `YYYYMMDD_HHMMSS`, `YYYY-MM-DD`, `IMG_YYYYMMDD_*`, etc.)
4. File modification time (fallback)

### Duplicate detection

**Step 1 – Exact duplicates (MD5):**
Files with identical content (same MD5 hash) are grouped. The file with the earliest timestamp is kept; all others are deleted.

**Step 2 – Visual duplicates (pHash, images only):**
Perceptual hashes are computed for all image files. Images with a Hamming distance ≤ threshold (default 8) are considered visual duplicates. Uses a BK-tree for efficient sublinear lookup, avoiding O(n²) comparisons. The file with the earliest timestamp is kept.

**Step 3 – Videos:**
Videos are deduplicated by MD5 only (no visual comparison).

All deletions are logged in `duplicates_report.csv`.

## Output structure

```
output/
├── <preserved subfolder structure>/
│   ├── 2021-06-15_143022_IMG_1234.jpg
│   ├── 2021-06-15_143025_IMG_1235.jpg
│   └── ...
├── repair_log.csv
├── duplicates_report.csv
└── summary.txt
```

### Generated reports

| File | Description |
|---|---|
| `repair_log.csv` | Per-file log: original path, new name, timestamp source, EXIF status |
| `duplicates_report.csv` | Every duplicate deletion: kept file, deleted file, type, similarity |
| `summary.txt` | Overall statistics: totals, timing, ExifTool status |

## Performance

- Files are processed in batches of 1,000
- MD5 and pHash computation uses multiprocessing (`os.cpu_count() - 1` workers)
- pHash comparison uses a BK-tree index for sublinear lookup
- SQLite with WAL mode for concurrent database writes
- Designed to handle 100,000+ files on a standard laptop

## Error handling

The tool is designed to never crash on individual file errors:

- **Corrupt ZIP** → logged and skipped
- **Missing JSON** → continues with filename/mtime fallback
- **Corrupt EXIF** → EXIF dict is reset and retried
- **Unopenable image** → EXIF skipped, file still renamed and timestamped
- **Filename collision** → auto-incremented suffix (`_2`, `_3`, ...)
- **ExifTool timeout** → skipped and logged (30s per file limit)
