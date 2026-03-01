#!/usr/bin/env python3
"""
Test data generator for google-photos-repair.

Generates 500 dummy JPEG files with:
- Intentional exact duplicates (same file content)
- Intentional visual duplicates (slightly altered images)
- Files with and without JSON sidecars
- Various filename patterns (IMG_*, VID_*, Screenshot_*, long names)
- Nested subfolder structure simulating Google Takeout albums

Usage:
    python test_generate.py
    python test_generate.py --count 200
    python test_generate.py --output ./test_temp

Then run:
    python repair.py --skip-extraction --temp ./test_temp --output ./test_output
"""

import argparse
import json
import os
import random
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def parse_args():
    parser = argparse.ArgumentParser(description="Generate test data for google-photos-repair")
    parser.add_argument("--count", type=int, default=500, help="Number of files to generate (default: 500)")
    parser.add_argument("--output", default="./test_temp", help="Output directory (default: ./test_temp)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    return parser.parse_args()


def random_color(rng):
    return (rng.randint(30, 230), rng.randint(30, 230), rng.randint(30, 230))


def create_test_image(path: Path, width: int, height: int, text: str, color: tuple):
    """Create a simple test JPEG image with text overlay."""
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)

    # Draw some shapes to make images distinguishable
    for i in range(5):
        x1 = random.randint(0, width - 50)
        y1 = random.randint(0, height - 50)
        x2 = x1 + random.randint(20, 80)
        y2 = y1 + random.randint(20, 80)
        shape_color = random_color(random)
        draw.rectangle([x1, y1, x2, y2], fill=shape_color)

    # Add text
    try:
        draw.text((10, 10), text, fill=(255, 255, 255))
    except Exception:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "JPEG", quality=85)


def create_visual_duplicate(original_path: Path, dest_path: Path):
    """Create a visual duplicate by slightly altering the original."""
    with Image.open(original_path) as img:
        draw = ImageDraw.Draw(img)
        # Add a tiny invisible change (1 pixel different)
        w, h = img.size
        x = random.randint(0, w - 1)
        y = random.randint(0, h - 1)
        pixel = img.getpixel((x, y))
        if isinstance(pixel, tuple):
            new_pixel = tuple(min(255, c + 1) for c in pixel[:3])
        else:
            new_pixel = min(255, pixel + 1)
        img.putpixel((x, y), new_pixel)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(dest_path), "JPEG", quality=85)


def create_json_sidecar(json_path: Path, taken_timestamp: int):
    """Create a Google Takeout JSON sidecar file."""
    data = {
        "title": json_path.stem.replace(".jpg", "").replace(".json", ""),
        "description": "",
        "imageViews": "0",
        "creationTime": {
            "timestamp": str(taken_timestamp + 3600),
            "formatted": "1. Jan. 2021, 00:00:00 UTC",
        },
        "photoTakenTime": {
            "timestamp": str(taken_timestamp),
            "formatted": "1. Jan. 2021, 00:00:00 UTC",
        },
        "geoData": {
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
        },
        "geoDataExif": {
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
        },
        "googlePhotosOrigin": {
            "mobileUpload": {
                "deviceType": "ANDROID_PHONE",
            }
        },
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main():
    args = parse_args()
    rng = random.Random(args.seed)
    random.seed(args.seed)

    output = Path(args.output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    # Simulated Google Takeout album structure
    albums = [
        "Google Fotos/2019-Urlaub",
        "Google Fotos/2020-Familie",
        "Google Fotos/2021-Sommer",
        "Google Fotos/2022-Weihnachten",
        "Google Fotos/Screenshots",
        "Google Fotos/WhatsApp",
        "Google Fotos/Kamera",
    ]

    # Filename patterns to test all matcher strategies
    patterns = [
        lambda i, dt: f"IMG_{dt.strftime('%Y%m%d_%H%M%S')}.jpg",
        lambda i, dt: f"VID_{dt.strftime('%Y%m%d_%H%M%S')}.jpg",
        lambda i, dt: f"Screenshot_{dt.strftime('%Y%m%d-%H%M%S')}.jpg",
        lambda i, dt: f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}_photo.jpg",
        lambda i, dt: f"DCIM_{i:04d}.jpg",
        lambda i, dt: f"photo_{i:04d}.jpg",
        lambda i, dt: f"PXL_{dt.strftime('%Y%m%d_%H%M%S')}_{rng.randint(100,999)}.jpg",
    ]

    # Track for duplicate creation
    created_files = []
    exact_dup_sources = []
    visual_dup_sources = []

    count = args.count
    # Reserve slots: 10% exact dupes, 10% visual dupes, 5% no JSON, rest normal
    n_exact_dupes = max(5, count // 10)
    n_visual_dupes = max(5, count // 10)
    n_no_json = max(5, count // 20)
    n_normal = count - n_exact_dupes - n_visual_dupes - n_no_json
    # Long filename test cases (subset of normal)
    n_long_names = max(3, count // 50)
    # Numbered duplicate test cases (subset of normal)
    n_numbered = max(3, count // 50)

    base_date = datetime(2019, 3, 15, 10, 0, 0, tzinfo=timezone.utc)

    print(f"Generating {count} test files in {output}/")
    print(f"  Normal files:     {n_normal}")
    print(f"  Exact duplicates: {n_exact_dupes}")
    print(f"  Visual duplicates:{n_visual_dupes}")
    print(f"  No JSON sidecar:  {n_no_json}")
    print(f"  Long filenames:   {n_long_names}")
    print(f"  Numbered dupes:   {n_numbered}")
    print()

    file_idx = 0

    # --- Step 1: Generate normal files ---
    print("Creating normal files...")
    for i in range(n_normal):
        album = albums[i % len(albums)]
        dt = base_date + timedelta(days=rng.randint(0, 1500), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        pattern = patterns[i % len(patterns)]
        filename = pattern(file_idx, dt)

        # Long filename test
        if i < n_long_names:
            filename = f"VeryLongFilename_that_exceeds_fortysix_characters_limit_{file_idx:04d}.jpg"

        # Numbered duplicate test (filename pattern)
        if n_long_names <= i < n_long_names + n_numbered:
            base = f"photo_{file_idx:04d}"
            num = rng.randint(1, 5)
            filename = f"{base}({num}).jpg"

        filepath = output / album / filename
        create_test_image(filepath, 640, 480, f"Test #{file_idx}", random_color(rng))

        # JSON sidecar (exact match pattern)
        ts = int(dt.timestamp())
        json_path = filepath.parent / (filename + ".json")
        create_json_sidecar(json_path, ts)

        created_files.append(filepath)
        if i < n_exact_dupes + n_visual_dupes:
            if i < n_exact_dupes:
                exact_dup_sources.append((filepath, album, dt))
            else:
                visual_dup_sources.append((filepath, album, dt))

        file_idx += 1

    # --- Step 2: Create exact duplicates (same file content) ---
    print("Creating exact duplicates...")
    for src_path, album, dt in exact_dup_sources:
        dup_album = albums[rng.randint(0, len(albums) - 1)]
        dup_name = f"copy_of_{src_path.name}"
        dup_path = output / dup_album / dup_name
        dup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(dup_path))

        # JSON for the duplicate too
        dup_dt = dt + timedelta(days=rng.randint(1, 30))
        json_path = dup_path.parent / (dup_name + ".json")
        create_json_sidecar(json_path, int(dup_dt.timestamp()))

        created_files.append(dup_path)
        file_idx += 1

    # --- Step 3: Create visual duplicates (slightly altered) ---
    print("Creating visual duplicates...")
    for src_path, album, dt in visual_dup_sources:
        dup_album = albums[rng.randint(0, len(albums) - 1)]
        dup_name = f"similar_to_{src_path.name}"
        dup_path = output / dup_album / dup_name
        create_visual_duplicate(src_path, dup_path)

        # JSON for the visual duplicate
        dup_dt = dt + timedelta(days=rng.randint(1, 60))
        json_path = dup_path.parent / (dup_name + ".json")
        create_json_sidecar(json_path, int(dup_dt.timestamp()))

        created_files.append(dup_path)
        file_idx += 1

    # --- Step 4: Files without JSON sidecar ---
    print("Creating files without JSON...")
    for i in range(n_no_json):
        album = albums[rng.randint(0, len(albums) - 1)]
        dt = base_date + timedelta(days=rng.randint(0, 1500))
        filename = f"no_json_{dt.strftime('%Y%m%d_%H%M%S')}_{i:03d}.jpg"
        filepath = output / album / filename
        create_test_image(filepath, 320, 240, f"NoJSON #{i}", random_color(rng))
        # Deliberately no JSON sidecar!
        created_files.append(filepath)
        file_idx += 1

    # --- Summary ---
    total = len(created_files)
    json_count = sum(
        1 for f in output.rglob("*.json") if f.is_file()
    )
    jpg_count = sum(
        1 for f in output.rglob("*.jpg") if f.is_file()
    )

    print()
    print("=" * 50)
    print(f"  Test data generated: {output}/")
    print(f"  Total JPEG files:    {jpg_count}")
    print(f"  Total JSON files:    {json_count}")
    print(f"  Exact duplicates:    {n_exact_dupes}")
    print(f"  Visual duplicates:   {n_visual_dupes}")
    print(f"  No JSON sidecar:     {n_no_json}")
    print("=" * 50)
    print()
    print("Run the repair tool:")
    print(f"  python repair.py --skip-extraction --temp {args.output} --output ./test_output")
    print()
    print("Or with sample mode:")
    print(f"  python repair.py --skip-extraction --temp {args.output} --sample 100")


if __name__ == "__main__":
    main()
