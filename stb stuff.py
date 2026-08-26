import csv
from pathlib import Path

from extractor.config import ExtractorConfig
from extractor.extract import extract_relevant_files
from extractor.strings import StringResolver

ASSETS_PATH = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Star Wars - The Old Republic\Assets"
)
OUTPUT_CSV = Path("abl_stb.csv")


def extract_strings_from_table(table) -> dict[str, str]:
    """Extract string ID -> text mapping from a StringTable instance."""
    if table is None:
        return {}

    # Check mapping protocol
    if hasattr(table, "items") and callable(table.items):
        return dict(table.items())

    # Check common dictionary storage attributes
    for attr in ["strings", "_strings", "entries", "_entries", "_data", "data", "table", "_table"]:
        val = getattr(table, attr, None)
        if isinstance(val, dict) and val:
            return val

    # Inspect all instance attributes for an internal dictionary
    for val in vars(table).values():
        if isinstance(val, dict) and val:
            return val

    return {}


def dump_abl_stb():
    print("1. Extracting archive resources...")
    config = ExtractorConfig(assets_path=ASSETS_PATH)
    resources_root = extract_relevant_files(
        config.assets_path,
        config.work_dir,
        config.data_dir,
        pts=config.pts,
    )

    print("2. Initializing StringResolver and loading 'abl' bucket...")
    resolver = StringResolver(resources_root)
    table = resolver.load_bucket("abl")

    if table is None:
        table = resolver.load_bucket("str.abl")

    if table is None:
        print("[ERROR] Could not load 'abl' string table.")
        return

    parsed_strings = extract_strings_from_table(table)
    print(f"3. Successfully loaded {len(parsed_strings):,} localized string entries.")

    if parsed_strings:
        sample_id, sample_text = next(iter(parsed_strings.items()))
        print(f"   Sample -> string_id: {sample_id} | text: {sample_text!r}")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["string_id", "text"]

    try:
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for str_id, text in parsed_strings.items():
                writer.writerow({"string_id": str(str_id), "text": str(text)})
    except PermissionError:
        fallback = OUTPUT_CSV.with_name("abl_stb_new.csv")
        print(f"\n[WARNING] {OUTPUT_CSV.name} is locked. Writing to {fallback.name} instead.")
        with fallback.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for str_id, text in parsed_strings.items():
                writer.writerow({"string_id": str(str_id), "text": str(text)})

    print(f"4. Output saved to: {OUTPUT_CSV.resolve()}")


if __name__ == "__main__":
    dump_abl_stb()