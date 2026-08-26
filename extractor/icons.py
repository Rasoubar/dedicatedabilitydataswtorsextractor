from __future__ import annotations

from pathlib import Path

from extractor.hashlist import ensure_jedipedia_hash_list
from extractor.myp_archive import (
    _safe_output_path,
    discover_archives,
    iter_archive_files,
)


def extract_icons(
    assets_path: Path,
    output_dir: Path,
    cache_dir: Path,
    *,
    allowed_icons: set[str] | None = None,
    pts: bool = False,
) -> int:
    """Extract gfx/icons/*.dds files, optionally restricted to a specific name set."""
    hash_dictionary = ensure_jedipedia_hash_list(cache_dir)
    archives = discover_archives(assets_path, pts=pts)
    output_dir.mkdir(parents=True, exist_ok=True)

    normalized_allowed = (
        {name.lower().strip() for name in allowed_icons if name}
        if allowed_icons is not None
        else None
    )

    def is_target_icon(hash_path: str) -> bool:
        normalized = hash_path.replace("\\", "/").lower().strip("/")
        if not (
            normalized.startswith("resources/gfx/icons/")
            or normalized.startswith("gfx/icons/")
            or "/gfx/icons/" in normalized
        ) or not normalized.endswith(".dds"):
            return False

        if normalized_allowed is not None:
            stem = Path(normalized).stem.lower()
            return stem in normalized_allowed

        return True

    extracted_count = 0
    for archive in archives:
        archive_matches = 0
        for hash_path, data in iter_archive_files(
            archive, hash_dictionary, path_filter=is_target_icon
        ):
            rel = hash_path.lstrip("/").replace("\\", "/")
            if rel.startswith("resources/"):
                rel = rel[len("resources/") :]

            dest = _safe_output_path(output_dir, rel)
            if dest is None:
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            archive_matches += 1
            extracted_count += 1

        if archive_matches > 0:
            print(f"  -> Extracted {archive_matches:,} matching icons from {archive.name}")

    return extracted_count