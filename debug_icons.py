import struct
from pathlib import Path

from extractor.config import DATA_DIR
from extractor.hashlist import ensure_jedipedia_hash_list
from extractor.myp_archive import (
    decompress_payload,
    discover_archives,
    read_entry,
    read_header,
)

# Set your assets path here
ASSETS_PATH = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Star Wars - The Old Republic\Assets"
)
TEST_OUTPUT_DIR = Path("./test_icon_out")


def run_diagnostics():
    print("=" * 60)
    print("STEP 1: Checking Hash Dictionary for 'gfx/icons/*.dds'")
    print("=" * 60)
    hash_dict = ensure_jedipedia_hash_list(DATA_DIR)

    # Filter strictly for dds textures in gfx/icons
    target_hashes = {}
    for h, path in hash_dict.items():
        norm = path.replace("\\", "/").lower()
        if "gfx/icons" in norm and norm.endswith(".dds"):
            target_hashes[h] = path

    print(f"Total entries in hash dictionary: {len(hash_dict):,}")
    print(f"Target 'gfx/icons/*.dds' hashes: {len(target_hashes):,}")

    if not target_hashes:
        print("[FAIL] No target paths found in hash dictionary.")
        return

    sample_items = list(target_hashes.items())[:3]
    for h, p in sample_items:
        print(f"  - Sample hash: 0x{h:016x} -> {p}")

    print("\n" + "=" * 60)
    print("STEP 2: Scanning .tor Archive Indexes")
    print("=" * 60)
    archives = discover_archives(ASSETS_PATH)
    print(f"Found {len(archives)} archives under {ASSETS_PATH}")

    if not archives:
        print("[FAIL] No archives discovered. Check ASSETS_PATH.")
        return

    found_in_archives = {}
    decompression_successes = 0
    decompression_failures = 0
    saved_samples = 0

    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for archive in archives:
        matches_in_this_archive = 0
        with archive.open("rb") as stream:
            header = read_header(stream)
            next_table_offset = header.index_offset
            file_size = stream.seek(0, 2)

            while next_table_offset != 0 and next_table_offset < file_size:
                stream.seek(next_table_offset)
                table_capacity = struct.unpack("<i", stream.read(4))[0]
                next_table_offset = struct.unpack("<q", stream.read(8))[0]

                for _ in range(table_capacity):
                    entry, next_pos = read_entry(stream, header.version)
                    if next_pos == 0:
                        break
                    if entry is None:
                        stream.seek(next_pos)
                        continue

                    if entry.hash in target_hashes:
                        matches_in_this_archive += 1
                        file_path = target_hashes[entry.hash]

                        # Test extraction and decompression
                        stream.seek(entry.data_offset)
                        compressed_data = stream.read(entry.compressed_size)

                        try:
                            data = decompress_payload(compressed_data, entry)
                            if len(data) == entry.decompressed_size:
                                decompression_successes += 1
                                magic = data[:4]

                                # Write first 5 matched files as a sample test
                                if saved_samples < 5:
                                    out_file = TEST_OUTPUT_DIR / Path(file_path).name
                                    out_file.write_bytes(data)
                                    saved_samples += 1
                                    print(f"\n[SUCCESS] Extracted test file from {archive.name}:")
                                    print(f"  Path: {file_path}")
                                    print(f"  Size: {len(data):,} bytes | Header magic: {magic!r}")
                                    print(f"  Saved to: {out_file}")
                            else:
                                decompression_failures += 1
                        except Exception as exc:
                            decompression_failures += 1
                            if decompression_failures <= 3:
                                print(f"\n[ERROR] Decompression failed in {archive.name} for {file_path}: {exc}")

                        stream.seek(next_pos)

                    stream.seek(next_pos)

                if next_table_offset == 0:
                    break
                stream.seek(next_table_offset)

        if matches_in_this_archive > 0:
            found_in_archives[archive.name] = matches_in_this_archive

    print("\n" + "=" * 60)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 60)
    print(f"Archives containing target icon hashes: {len(found_in_archives)}")
    for arc_name, count in found_in_archives.items():
        print(f"  - {arc_name}: {count:,} icon entries")

    print(f"\nTotal icon entries matched in index: {sum(found_in_archives.values()):,}")
    print(f"Successful decompressions: {decompression_successes:,}")
    print(f"Decompression errors: {decompression_failures:,}")
    print(f"Sample files written to: {TEST_OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    run_diagnostics()