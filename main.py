import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

from extractor.config import (
    ABILITY_REPLACEMENT_NODE_ID,
    ALWAYS_EXTRACTED_ABILITY_FQNS,
    ITEM_ABILITY_FQN_PREFIXES,
    ORIGIN_STORIES,
    ExtractorConfig,
)
from extractor.dump import write_node_dump
from extractor.extract import extract_relevant_files
from extractor.gom.gom import GomLookup, parse_gom_js
from extractor.gom_cache import ensure_jedipedia_gom_js
from extractor.graph import (
    BucketStore,
    NodeRecord,
    discover_adrenal_ability_nodes,
    discover_apc_base_nodes,
    discover_dis_nodes,
    discover_item_ability_nodes,
    discover_scaled_relic_ability_nodes,
    traverse_combat_graph,
)
from extractor.icons import extract_icons
from extractor.stable_ids import TagResolver, ensure_jedipedia_fnv1a64_js
from extractor.strings import StringResolver

NAME_FIELD_ID = "4611686102842470023"
GCD_FIELD_ID = "4611686019453829630"
GCD_FIELD_NAME = "ablGlobalCooldownTime"

ICON_FIELD_NAMES = frozenset({"ablIconSpec", "talTalentIcon"})
ICON_FIELD_IDS = frozenset(
    {
        "4611686019453829629",  # ablIconSpec
        "4611686296953210018",  # talTalentIcon
    }
)


def extract_resolved_name(field: dict) -> str:
    """Extract string name whether resolved_text is root-level or nested in value."""
    if field.get("resolved_text"):
        return str(field["resolved_text"]).strip()

    val = field.get("value")
    if isinstance(val, dict) and "resolved_text" in val:
        return str(val.get("resolved_text") or "").strip()
    if isinstance(val, str):
        return val.strip()

    return ""


def collect_used_icon_specs(records: dict[str, NodeRecord]) -> set[str]:
    """Scan abl and tal nodes for ablIconSpec and talTalentIcon values."""
    icon_specs: set[str] = set()
    for record in records.values():
        fqn = record.entry.fqn
        if not (fqn.startswith("abl.") or fqn.startswith("tal.")):
            continue

        for field in record.resolved_fields:
            name = field.get("name")
            fid = str(field.get("id"))

            if name in ICON_FIELD_NAMES or fid in ICON_FIELD_IDS:
                val = field.get("value")
                if isinstance(val, str) and val.strip():
                    icon_specs.add(val.strip().lower())

    return icon_specs


def export_ability_icon_map_csv(
    records: dict[str, NodeRecord], csv_path: Path
) -> int:
    """Export a CSV table mapping FQNs, resolved names, icon specs, and GCD times."""
    rows: list[dict[str, str]] = []

    for record in records.values():
        fqn = record.entry.fqn
        if not (fqn.startswith("abl.") or fqn.startswith("tal.")):
            continue

        # Skip sub-effect and sub-action nodes ending in _<number> (e.g. _1, _2)
        if re.search(r"_\d+$", fqn):
            continue

        resolved_name = ""
        icon_spec = ""
        gcd_value = ""

        for field in record.resolved_fields:
            fid = str(field.get("id"))
            name = field.get("name")

            # Extract localized ability/talent name
            if fid == NAME_FIELD_ID or name == "locTextRetrieverMap":
                resolved_name = extract_resolved_name(field)

            # Extract icon specifier
            if name in ICON_FIELD_NAMES or fid in ICON_FIELD_IDS:
                val = field.get("value")
                if isinstance(val, str) and val.strip():
                    icon_spec = val.strip()

            # Extract global cooldown time
            if fid == GCD_FIELD_ID or name == GCD_FIELD_NAME:
                val = field.get("value")
                if val is not None:
                    gcd_value = str(val)

        # Only export entries that have a resolved name or icon spec
        if resolved_name or icon_spec:
            rows.append(
                {
                    "fqn": fqn,
                    "name": resolved_name,
                    "icon": icon_spec,
                    "ablGlobalCooldownTime": gcd_value,
                }
            )

    rows.sort(key=lambda r: r["fqn"])

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["fqn", "name", "icon", "ablGlobalCooldownTime"]

    try:
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        fallback_path = csv_path.with_name("ability_icons_new.csv")
        print(f"\n[WARNING] {csv_path.name} is locked by another program. Writing to {fallback_path.name} instead.")
        with fallback_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return len(rows)


def run(
    assets_path: Path,
    output_dir: Path | None = None,
    pts: bool = False,
    force_hash_update: bool = False,
    keep_work: bool = False,
) -> None:
    config = ExtractorConfig(
        assets_path=assets_path,
        pts=pts,
        force_hash_update=force_hash_update,
        keep_work_files=keep_work,
    )
    dest_dir = output_dir or config.output_dir

    print("1. Fetching Jedipedia definition resources...")
    gom_js_path = ensure_jedipedia_gom_js(
        config.data_dir, force_download=config.force_hash_update
    )
    gom_names = parse_gom_js(gom_js_path)
    fnv1a64_js_path = ensure_jedipedia_fnv1a64_js(
        config.data_dir, force_download=config.force_hash_update
    )
    tag_resolver = TagResolver.from_jedipedia_js(fnv1a64_js_path)

    print("2. Extracting raw buckets and GOM tables from archives...")
    resources_root = extract_relevant_files(
        config.assets_path,
        config.work_dir,
        config.data_dir,
        force_hash_update=config.force_hash_update,
        pts=config.pts,
    )

    print("3. Indexing bucket files...")
    gom = GomLookup.from_resources(resources_root, names=gom_names)
    store = BucketStore(resources_root)
    store.build_index(gom)
    strings = StringResolver(resources_root)

    print("4. Discovering root entry points...")
    dis_roots = discover_dis_nodes(store)
    if not dis_roots:
        raise RuntimeError("No discipline root nodes found in bucket index.")

    item_ability_roots = [
        *discover_item_ability_nodes(store),
        *discover_scaled_relic_ability_nodes(store),
        *discover_adrenal_ability_nodes(store),
    ]
    base_apc_roots = discover_apc_base_nodes(store, ORIGIN_STORIES)

    print("5. Traversing combat dependency graph...")
    records = traverse_combat_graph(
        store,
        gom,
        strings,
        roots=dis_roots,
        additional_roots=[
            *item_ability_roots,
            *base_apc_roots,
            *ALWAYS_EXTRACTED_ABILITY_FQNS,
        ],
        additional_node_ids=[ABILITY_REPLACEMENT_NODE_ID],
        tag_resolver=tag_resolver,
    )

    print(f"6. Writing extracted node dump to {dest_dir}...")
    index_path = write_node_dump(
        records,
        dest_dir,
        dis_roots,
        included_fqn_prefixes=ITEM_ABILITY_FQN_PREFIXES,
        flat_node_ids=frozenset({ABILITY_REPLACEMENT_NODE_ID}),
    )
    print(f"   -> Successfully wrote {len(records):,} nodes.")
    print(f"   -> Index written to: {index_path}")

    # Generate filtered CSV mapping
    csv_out_path = dest_dir / "ability_icons.csv"
    mapped_count = export_ability_icon_map_csv(records, csv_out_path)
    print(f"   -> Exported {mapped_count:,} primary ability/talent mappings to: {csv_out_path}")

    # Extract icon textures
    used_icons = collect_used_icon_specs(records)
    print(
        f"\n7. Found {len(used_icons):,} unique icon references across abilities & talents."
    )
    print("   Extracting matching icon textures from .tor archives...")

    icon_output_dir = dest_dir / "icons"
    icon_count = extract_icons(
        config.assets_path,
        icon_output_dir,
        config.data_dir,
        allowed_icons=used_icons,
        pts=config.pts,
    )
    print(
        f"   -> Successfully extracted {icon_count:,} referenced icons to {icon_output_dir}"
    )

    if not config.keep_work_files and config.work_dir.exists():
        shutil.rmtree(config.work_dir, ignore_errors=True)

    print("\nAll tasks completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract raw SWTOR combat graph nodes and referenced icon textures."
    )
    parser.add_argument(
        "--assets",
        type=Path,
        required=True,
        help="Path to SWTOR Assets folder containing .tor archives",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Destination directory (default: data/extracted)",
    )
    parser.add_argument(
        "--pts",
        action="store_true",
        help="Use PTS archives",
    )
    parser.add_argument(
        "--force-hash-update",
        action="store_true",
        help="Force redownload of Jedipedia hash lists",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep temporary files in data/extract_work",
    )
    args = parser.parse_args()

    try:
        run(
            assets_path=args.assets,
            output_dir=args.output,
            pts=args.pts,
            force_hash_update=args.force_hash_update,
            keep_work=args.keep_work,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)