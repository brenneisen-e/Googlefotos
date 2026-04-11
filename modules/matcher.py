"""JSON sidecar matching for Google Takeout media files.

Ports the comprehensive matching logic from analyze.py. Key properties:

- **Per-directory cache** of JSON file listings. Matching used to re-read
  each directory for every single media file, which is quadratic in
  directory size and makes large Takeout exports (100k+ files) hang for
  hours in Phase 2. The cache turns it into O(dirs) scans.
- **Prefix-based matching** that transparently handles the newer
  ``.supplemental-metadata.json`` format and every truncation variant
  Google currently produces, without needing to enumerate them.
- Extension equivalents (``.jpg`` ↔ ``.jpeg``) for cross-extension JSON
  matches.
- ``-edited`` / ``-cropped`` / ``-bearbeitet`` fallback to the original
  file's JSON, so edited copies inherit their parent's metadata.
- Special-character normalization: Google replaces ``&?;'`` with ``_``
  in exported filenames but keeps the original in the JSON ``title``
  field, so a title index is consulted as a fallback.
- URL-encoded / URL-decoded fallback for filenames that contain escape
  sequences (e.g. ``100%25 atzen.jpg`` vs ``100% atzen.jpg``).
- **Cross-directory title index** for files where the JSON lives in an
  album folder while the media lives in a year folder. Built lazily on
  the first same-directory miss.
"""

import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Per-directory JSON map cache: avoids re-scanning the same folder once
# per media file. This is THE critical performance optimization for
# large Takeout exports.
_json_map_cache: dict = {}

# Per-directory title index cache: maps normalized title → json Path
# within a single directory.
_dir_title_cache: dict = {}

# Global title index: maps normalized title → json Path. Built lazily
# on first cross-directory lookup.
_title_index: dict = {}
_title_index_built = False

# Characters Google replaces with _ in exported filenames
# (the JSON title field keeps the originals).
_SPECIAL_CHAR_MAP = str.maketrans("&?;'", "____")

# Equivalent extensions (.jpg ↔ .jpeg).
_EXT_EQUIVALENTS = {
    ".jpg": [".jpeg"],
    ".jpeg": [".jpg"],
}


def reset_matcher_caches() -> None:
    """Drop all module-level caches. Primarily useful for tests."""
    global _json_map_cache, _dir_title_cache, _title_index, _title_index_built
    _json_map_cache = {}
    _dir_title_cache = {}
    _title_index = {}
    _title_index_built = False


def _get_json_map(directory: Path) -> dict:
    """Cached, case-insensitive map of JSON filenames → full Path in directory."""
    key = str(directory)
    if key in _json_map_cache:
        return _json_map_cache[key]
    json_map: dict = {}
    try:
        for f in directory.iterdir():
            if f.is_file() and f.name.lower().endswith(".json"):
                json_map[f.name.lower()] = f
    except OSError:
        pass
    _json_map_cache[key] = json_map
    return json_map


def _find_json_in_map(
    json_map: dict,
    media_name: str,
    media_stem: str,
    media_suffix: str,
) -> Optional[Path]:
    """Apply all matching rules against a single directory's json_map."""

    def lookup(candidate: str) -> Optional[Path]:
        return json_map.get(candidate.lower())

    def prefix_match(prefix: str) -> Optional[Path]:
        """Find any JSON file starting with ``<prefix>.`` and ending with ``.json``."""
        prefix_lower = prefix.lower() + "."
        for json_name, json_file in json_map.items():
            if json_name.startswith(prefix_lower) and json_name.endswith(".json"):
                return json_file
        return None

    # --- Rule 1: Prefix match ---
    # photo.jpg → photo.jpg.json, photo.jpg.supplemental-metadata.json,
    #             photo.jpg.supplemental-me.json, photo.jpg.s.json, etc.
    r = prefix_match(media_name)
    if r:
        return r

    # --- Rule 2: Stem only (photo.jpg → photo.json) ---
    r = lookup(media_stem + ".json")
    if r:
        return r

    # --- Rule 3: Extension equivalents (.jpg ↔ .jpeg) ---
    alt_exts = _EXT_EQUIVALENTS.get(media_suffix.lower(), [])
    for alt_ext in alt_exts:
        alt_name = media_stem + alt_ext
        r = prefix_match(alt_name)
        if r:
            return r

    # --- Rule 4: Truncated filenames (46/47/51 char limits) ---
    for trunc_len in (46, 47, 51):
        if len(media_name) > trunc_len:
            r = prefix_match(media_name[:trunc_len])
            if r:
                return r
        if len(media_stem) > trunc_len:
            r = prefix_match(media_stem[:trunc_len] + media_suffix)
            if r:
                return r

    # --- Rule 5: Numbered duplicates ---
    # Media:  photo(1).jpg
    # JSON:   photo.jpg(1).json  OR  photo.JPG(1).supplemental-metadata.json
    m = re.match(r"^(.+)\((\d+)\)$", media_stem)
    if m:
        base, num = m.group(1), m.group(2)
        for ext_variant in {media_suffix, media_suffix.upper(), media_suffix.lower()}:
            r = prefix_match(f"{base}{ext_variant}({num})")
            if r:
                return r
        r = prefix_match(f"{base}({num})")
        if r:
            return r
        for alt_ext in alt_exts:
            r = prefix_match(f"{base}{alt_ext}({num})")
            if r:
                return r

    # --- Rule 6: -edited / -cropped / -bearbeitet fallback ---
    # These files often have no JSON of their own; inherit from original.
    for edit_suffix in ("-edited", "-cropped", "-bearbeitet", "-edit", "-edi", "-ed"):
        if media_stem.lower().endswith(edit_suffix):
            original_stem = media_stem[: -len(edit_suffix)]
            original_name = original_stem + media_suffix
            r = prefix_match(original_name)
            if r:
                return r
            r = lookup(original_stem + ".json")
            if r:
                return r
            for alt_ext in alt_exts:
                r = prefix_match(original_stem + alt_ext)
                if r:
                    return r

    # --- Rule 7: URL-decoded / URL-encoded fallback ---
    decoded_name = urllib.parse.unquote(media_name)
    if decoded_name != media_name:
        decoded_stem = Path(decoded_name).stem
        r = prefix_match(decoded_name)
        if r:
            return r
        r = lookup(decoded_stem + ".json")
        if r:
            return r
    encoded_name = urllib.parse.quote(media_name, safe=" ")
    if encoded_name != media_name:
        r = prefix_match(encoded_name)
        if r:
            return r

    # --- Rule 8: Title-based match (cached per directory) ---
    dir_key = id(json_map)
    if dir_key not in _dir_title_cache:
        title_map: dict = {}
        for json_name, json_file in json_map.items():
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                title = data.get("title", "")
                if title and isinstance(title, str):
                    title_lower = title.lower()
                    title_normalized = title_lower.translate(_SPECIAL_CHAR_MAP)
                    title_map[title_lower] = json_file
                    if title_normalized != title_lower:
                        title_map[title_normalized] = json_file
                    title_stem = Path(title).stem.lower()
                    if title_stem not in title_map:
                        title_map[title_stem] = json_file
            except (json.JSONDecodeError, OSError):
                continue
        _dir_title_cache[dir_key] = title_map
    else:
        title_map = _dir_title_cache[dir_key]

    name_lower = media_name.lower()
    name_normalized = media_name.lower().translate(_SPECIAL_CHAR_MAP)
    stem_lower = media_stem.lower()

    r = (title_map.get(name_lower)
         or title_map.get(name_normalized)
         or title_map.get(stem_lower))
    if r:
        return r

    return None


def _build_title_index(temp_dir: Path) -> None:
    """Build a global JSON title → path index for cross-directory matching.

    Lazy: only scanned on the first same-directory miss. Skipped entirely
    if every media file finds its JSON in its own directory.
    """
    global _title_index, _title_index_built
    if _title_index_built:
        return
    try:
        iterator = temp_dir.rglob("*.json")
    except OSError:
        _title_index_built = True
        return
    for json_file in iterator:
        if not json_file.is_file():
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            title = data.get("title", "")
            if title and isinstance(title, str):
                key = title.lower()
                # Keep the first match (year folders tend to be more complete).
                if key not in _title_index:
                    _title_index[key] = json_file
        except (json.JSONDecodeError, OSError):
            continue
    _title_index_built = True


def find_json_for_media(
    media_path: Path,
    temp_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Find the Google Takeout JSON sidecar for a media file.

    Same-directory matching is tried first (fastest, cache-accelerated).
    If that misses and ``temp_dir`` is provided, a global title index is
    consulted for cross-directory matches.
    """
    name = media_path.name
    stem = media_path.stem
    suffix = media_path.suffix

    # Same-directory matching
    json_map = _get_json_map(media_path.parent)
    if json_map:
        r = _find_json_in_map(json_map, name, stem, suffix)
        if r:
            return r

    # Cross-directory: global title index
    if temp_dir is not None:
        _build_title_index(Path(temp_dir))
        name_lower = name.lower()
        name_normalized = name.lower().translate(_SPECIAL_CHAR_MAP)
        r = _title_index.get(name_lower)
        if r:
            return r
        r = _title_index.get(name_normalized)
        if r:
            return r

    return None


def match_all(media_files: list, temp_dir: str) -> list:
    """Match all media files to their JSON sidecars.

    Returns a list of tuples: ``(media_path, json_path_or_None)``.
    """
    temp_path = Path(temp_dir) if temp_dir else None
    results = []
    matched = 0
    unmatched = 0

    for mf in media_files:
        json_path = find_json_for_media(mf, temp_dir=temp_path)
        results.append((mf, json_path))
        if json_path:
            matched += 1
        else:
            unmatched += 1

    logger.info(
        "JSON matching: %d matched, %d unmatched out of %d files",
        matched, unmatched, len(media_files),
    )
    return results
