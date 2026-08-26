import sys
from pathlib import Path

from extractor.config import DATA_DIR
from extractor.hashlist import ensure_jedipedia_hash_list
from extractor.myp_archive import (
    _safe_output_path,
    discover_archives,
    iter_archive_files,
)

ASSETS_PATH = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Star Wars - The Old Republic\Assets"
)
OUTPUT_DIR = Path("./extracted_icons")


def is_icon_path(hash_path: str) -> bool:
    normalized = hash_path.replace("\\", "/").lower().strip("/")
    return (
        normalized.startswith("resources/gfx/icons/")
        or normalized.startswith("gfx/icons/")
        or "/gfx/icons/" in normalized
    ) and normalized.endswith(".dds")


def main():
    print(f"Working Directory: {Path.cwd()}")
    print(f"Target Output Folder: {OUTPUT_DIR.resolve()}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\n1. Loading hash list...")
    hash_dict = ensure_jedipedia_hash_list(DATA_DIR)
    print(f"   Loaded {len(hash_dict):,} hashes.")

    print("\n2. Discovering archives...")
    archives = discover_archives(ASSETS_PATH)
    print(f"   Found {len(archives)} archives.")

    total_saved = 0
    for archive in archives:
        count = 0
        for hash_path, data in iter_archive_files(archive, hash_dict, path_filter=is_icon_path):
            rel = hash_path.lstrip("/").replace("\\", "/")
            if rel.startswith("resources/"):
                rel = rel[len("resources/") :]

            dest = _safe_output_path(OUTPUT_DIR, rel)
            if dest is None:
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            count += 1
            total_saved += 1

            if total_saved % 5000 == 0:
                print(f"   ... saved {total_saved:,} icons so far")

        if count > 0:
            print(f"\n[DONE] Extracted {count:,} icons from {archive.name}")

    print(f"\nFinished! Total files written: {total_saved:,}")
    print(f"Files are located in: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[ERROR]: {e}", file=sys.stderr)