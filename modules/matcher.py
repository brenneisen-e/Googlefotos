"""JSON sidecar matching for Google Takeout media files."""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum filename length before Google truncates (excluding extension)
GOOGLE_TRUNCATE_LEN = 46


def find_json_for_media(media_path: Path) -> Optional[Path]:
    """Find the Google Takeout JSON sidecar for a media file.

    Matching rules (tried in order):
    1. Exact: photo.jpg → photo.jpg.json
    2. Stem only: photo.jpg → photo.json
    3. Truncated at 46 chars: long filenames get cut before .json is appended
    4. Numbered duplicates: photo(1).jpg → photo(1).jpg.json OR photo.jpg(1).json
    All matching is case-insensitive.
    """
    parent = media_path.parent
    name = media_path.name
    stem = media_path.stem
    suffix = media_path.suffix

    # Build a case-insensitive lookup of all JSON files in the same directory
    json_map = _build_json_map(parent)

    # Rule 1: Exact – photo.jpg.json
    candidate = name + ".json"
    result = _ci_lookup(json_map, candidate)
    if result:
        return result

    # Rule 2: Stem only – photo.json
    candidate = stem + ".json"
    result = _ci_lookup(json_map, candidate)
    if result:
        return result

    # Rule 3: Truncated filename at 46 chars
    # Google truncates the full filename (stem+ext) to 46 chars, then appends .json
    if len(name) > GOOGLE_TRUNCATE_LEN:
        truncated = name[:GOOGLE_TRUNCATE_LEN]
        candidate = truncated + ".json"
        result = _ci_lookup(json_map, candidate)
        if result:
            return result

    # Also try: truncated stem + original extension + .json
    if len(stem) > GOOGLE_TRUNCATE_LEN:
        truncated_stem = stem[:GOOGLE_TRUNCATE_LEN]
        candidate = truncated_stem + suffix + ".json"
        result = _ci_lookup(json_map, candidate)
        if result:
            return result

    # Rule 4: Numbered duplicates
    # Pattern A: photo(1).jpg → photo(1).jpg.json (already covered by rule 1)
    # Pattern B: photo(1).jpg → photo.jpg(1).json
    result = _match_numbered_duplicate(stem, suffix, json_map)
    if result:
        return result

    return None


def _build_json_map(directory: Path) -> dict:
    """Build a case-insensitive map of JSON filenames → full paths."""
    json_map = {}
    try:
        for f in directory.iterdir():
            if f.suffix.lower() == ".json" and f.is_file():
                json_map[f.name.lower()] = f
    except OSError:
        pass
    return json_map


def _ci_lookup(json_map: dict, candidate: str) -> Optional[Path]:
    """Case-insensitive lookup in the JSON map."""
    return json_map.get(candidate.lower())


def _match_numbered_duplicate(stem: str, suffix: str, json_map: dict) -> Optional[Path]:
    """Match numbered duplicate patterns like photo(1).jpg → photo.jpg(1).json."""
    # Check if filename has a number suffix like stem(N)
    import re
    m = re.match(r"^(.+)\((\d+)\)$", stem)
    if not m:
        return None

    base_stem = m.group(1)
    number = m.group(2)

    # Try: basestem.ext(N).json → e.g. photo.jpg(1).json
    candidate = f"{base_stem}{suffix}({number}).json"
    result = _ci_lookup(json_map, candidate)
    if result:
        return result

    # Try: basestem(N).json
    candidate = f"{base_stem}({number}).json"
    result = _ci_lookup(json_map, candidate)
    if result:
        return result

    return None


def match_all(media_files: list, temp_dir: str) -> list:
    """Match all media files to their JSON sidecars.

    Returns list of tuples: (media_path, json_path_or_None).
    """
    results = []
    matched = 0
    unmatched = 0

    for mf in media_files:
        json_path = find_json_for_media(mf)
        results.append((mf, json_path))
        if json_path:
            matched += 1
        else:
            unmatched += 1

    logger.info("JSON matching: %d matched, %d unmatched out of %d files",
                matched, unmatched, len(media_files))
    return results
