import argparse
import csv
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

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

# Import matching & exclusivity algorithms from matching.py
try:
    from extractor.matching import (
        extract_all_matching_attributes,
        find_spec_exclusive_abilities,
        run_mirror_matching,
    )
except ImportError:
    from matching import (
        extract_all_matching_attributes,
        find_spec_exclusive_abilities,
        run_mirror_matching,
    )

# --- Field Constants ---
NAME_FIELD_ID = "4611686102842470023"
GCD_FIELD_ID = "4611686019453829630"
GCD_FIELD_NAME = "ablGlobalCooldownTime"
TARGET_RULE_FIELD_ID = "4611686019453829670"
TARGET_RULE_FIELD_NAME = "ablTargetRule"
COMBAT_MODE_FIELD_ID = "4611686019453829671"
COMBAT_MODE_FIELD_NAME = "ablCombatMode"

IS_PASSIVE_FIELD_ID = "4611686019453829615"
IS_PASSIVE_FIELD_NAME = "ablIsPassive"

IGNORE_ALACRITY_FIELD_NAMES = frozenset({"ablIgnoreAlacrity", "effIgnoreAlacrity"})
IGNORE_ALACRITY_FIELD_IDS = frozenset(
    {
        "4611686188651010047",  # ablIgnoreAlacrity
        "4611686188651060046",  # effIgnoreAlacrity
    }
)

PARENT_SPEC_FIELD_ID = "4611686061870631196"
PARENT_SPEC_FIELD_NAME = "effAbilitySpec"
ACTION_NAME_FIELD_ID = "4611686039404270028"
ACTION_NAME_FIELD_NAME = "effActionName"
INT_PARAMS_FIELD_ID = "4611686039404270037"
INT_PARAMS_FIELD_NAME = "effIntParams"

REVIVE_TAG_STRING = "offer_revive"
REVIVE_TAG_HASH = "7381586163280004179"

TOOLTIP_LOOKUP_KEY = "2806211896052149513"
COMBAT_ID_LOOKUP_KEY = "15685385242400905286"
LOC_STRING_ID_FIELD = "4611686093000569992"
LOC_BUCKET_FIELD = "4611686093000569993"

ICON_FIELD_NAMES = frozenset({"ablIconSpec", "talTalentIcon"})
ICON_FIELD_IDS = frozenset(
    {
        "4611686019453829629",  # ablIconSpec
        "4611686296340730018",  # talTalentIcon
    }
)


# --- Helper Utilities ---
def get_field_val(record: NodeRecord, field_name: str, field_id: str = "") -> Any:
    for f in record.resolved_fields:
        if f.get("name") == field_name or (field_id and str(f.get("id")) == field_id):
            return f.get("value")
    return None


def extract_resolved_name(field: dict) -> str:
    if field.get("resolved_text"):
        return str(field["resolved_text"]).strip()

    val = field.get("value")
    if isinstance(val, dict) and "resolved_text" in val:
        return str(val.get("resolved_text") or "").strip()
    if isinstance(val, str):
        return val.strip()

    return ""


def extract_global_combat_id(field: dict) -> str:
    val = field.get("value")
    if not isinstance(val, dict):
        return ""

    loc_retriever = val.get("loc_retriever")
    if not isinstance(loc_retriever, dict):
        return ""

    entries = loc_retriever.get("list")
    if not isinstance(entries, list):
        return ""

    for item in entries:
        if str(item.get("key")) == COMBAT_ID_LOOKUP_KEY:
            sub_fields = item.get("value")
            if isinstance(sub_fields, list):
                for sf in sub_fields:
                    if str(sf.get("id")) == LOC_STRING_ID_FIELD or sf.get("type_name") == "Integer" or sf.get("type") == 2:
                        v = sf.get("value")
                        if v is not None:
                            return str(v).strip()
    return ""


def extract_resolved_tooltip(field: dict, strings: StringResolver) -> str:
    val = field.get("value")
    if not isinstance(val, dict):
        return ""

    loc_retriever = val.get("loc_retriever")
    if not isinstance(loc_retriever, dict):
        return ""

    entries = loc_retriever.get("list")
    if not isinstance(entries, list):
        return ""

    for item in entries:
        if str(item.get("key")) == TOOLTIP_LOOKUP_KEY:
            sub_fields = item.get("value")
            if isinstance(sub_fields, list):
                str_id = None
                bucket = "str.abl"
                for sf in sub_fields:
                    sf_id = str(sf.get("id"))
                    if sf_id == LOC_STRING_ID_FIELD:
                        str_id = sf.get("value")
                    elif sf_id == LOC_BUCKET_FIELD:
                        bucket = sf.get("value") or bucket

                if str_id:
                    resolved = strings.resolve(str(bucket), str(str_id))
                    if resolved:
                        return resolved.strip()
    return ""


# --- Feature & Attack Type Extractor ---
class AbilityFeatureExtractor:
    """Single-pass indexer across sub-effects to evaluate modular traits & attack classifications."""

    def __init__(self, records: dict[str, NodeRecord]):
        self.records = records
        self.ability_actions: dict[str, set[str]] = defaultdict(set)
        self.has_weapon_damage: dict[str, bool] = defaultdict(bool)
        self.spell_damage_types: dict[str, set[str]] = defaultdict(set)
        self.ability_revives: set[str] = set()
        self._build_index()

    def _inspect_node_recursive(self, data: Any, parent_fqn: str) -> None:
        if isinstance(data, dict):
            for k, v in data.items():
                if k in (REVIVE_TAG_STRING, REVIVE_TAG_HASH) or v in (REVIVE_TAG_STRING, REVIVE_TAG_HASH):
                    self.ability_revives.add(parent_fqn)
                self._inspect_node_recursive(v, parent_fqn)

        elif isinstance(data, list):
            is_action_block = False
            action_name = None
            spell_type = None

            for element in data:
                if isinstance(element, dict):
                    fid = str(element.get("id"))
                    fname = element.get("name")
                    if fid == ACTION_NAME_FIELD_ID or fname == ACTION_NAME_FIELD_NAME:
                        is_action_block = True
                        action_name = element.get("value")
                    elif fid == INT_PARAMS_FIELD_ID or fname == INT_PARAMS_FIELD_NAME:
                        val = element.get("value")
                        if isinstance(val, list):
                            for param in val:
                                if isinstance(param, dict) and param.get("key") == "effParam_SpellType":
                                    spell_type = str(param.get("value") or "").strip()

            if is_action_block and action_name:
                cleaned_name = str(action_name).strip()
                self.ability_actions[parent_fqn].add(cleaned_name)

                if cleaned_name == "effAction_WeaponDamage":
                    self.has_weapon_damage[parent_fqn] = True
                elif cleaned_name == "effAction_SpellDamage" and spell_type:
                    self.spell_damage_types[parent_fqn].add(spell_type)

            for item in data:
                if item in (REVIVE_TAG_STRING, REVIVE_TAG_HASH):
                    self.ability_revives.add(parent_fqn)
                self._inspect_node_recursive(item, parent_fqn)

    def _build_index(self) -> None:
        for record in self.records.values():
            fqn = record.entry.fqn
            if "/" not in fqn and not re.search(r"_\d+_\d+$", fqn):
                continue

            parent_fqn = get_field_val(record, PARENT_SPEC_FIELD_NAME, PARENT_SPEC_FIELD_ID)
            if not parent_fqn or not isinstance(parent_fqn, str):
                parent_fqn = fqn.split("/")[0]

            for field in record.resolved_fields:
                self._inspect_node_recursive(field, parent_fqn)

    def get_attack_type(self, record: NodeRecord) -> str:
        fqn = record.entry.fqn
        has_weapon = self.has_weapon_damage.get(fqn, False)
        spell_types = self.spell_damage_types.get(fqn, set())
        combat_mode = str(get_field_val(record, COMBAT_MODE_FIELD_NAME, COMBAT_MODE_FIELD_ID) or "")

        types = []
        if has_weapon:
            if "staCombatModeMelee" in combat_mode:
                types.append("Melee")
            elif "staCombatModeRanged" in combat_mode:
                types.append("Ranged")

        if "2" in spell_types:
            types.append("Tech")
        if "3" in spell_types:
            types.append("Force")

        return "/".join(types) if types else "None"

    def is_interrupt(self, record: NodeRecord) -> bool:
        target_rule = get_field_val(record, TARGET_RULE_FIELD_NAME, TARGET_RULE_FIELD_ID)
        if target_rule != "tgtRuleAttackable":
            return False
        return "effAction_AbilityInterrupt" in self.ability_actions.get(record.entry.fqn, set())

    def grants_absorb(self, record: NodeRecord) -> bool:
        return "effAction_AbsorbDamage" in self.ability_actions.get(record.entry.fqn, set())

    def its_revive(self, record: NodeRecord) -> bool:
        return record.entry.fqn in self.ability_revives

    def ignore_alacrity(self, record: NodeRecord) -> bool:
        for field in record.resolved_fields:
            name = field.get("name")
            fid = str(field.get("id"))
            if name in IGNORE_ALACRITY_FIELD_NAMES or fid in IGNORE_ALACRITY_FIELD_IDS:
                if field.get("value") is True:
                    return True
        return False

    def is_passive(self, record: NodeRecord) -> bool:
        val = get_field_val(record, IS_PASSIVE_FIELD_NAME, IS_PASSIVE_FIELD_ID)
        return val is True


TRAIT_EXTRACTORS: dict[str, Callable[[AbilityFeatureExtractor, NodeRecord], bool]] = {
    "is_interrupt": lambda ext, rec: ext.is_interrupt(rec),
    "grants_absorb": lambda ext, rec: ext.grants_absorb(rec),
    "its_revive": lambda ext, rec: ext.its_revive(rec),
    "ignore_alacrity": lambda ext, rec: ext.ignore_alacrity(rec),
    "is_passive": lambda ext, rec: ext.is_passive(rec),
}


def collect_used_icon_specs(records: dict[str, NodeRecord]) -> set[str]:
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
    records: dict[str, NodeRecord],
    strings: StringResolver,
    csv_path: Path,
    mirror_map: dict[str, str] | None = None,
    exclusive_map: dict[str, str] | None = None,
) -> int:
    feature_extractor = AbilityFeatureExtractor(records)
    rows: list[dict[str, str]] = []

    for record in records.values():
        fqn = record.entry.fqn
        if not (fqn.startswith("abl.") or fqn.startswith("tal.")):
            continue

        if re.search(r"_\d+$|/\d+", fqn):
            continue

        resolved_name = ""
        resolved_tooltip = ""
        global_combat_id = ""
        icon_spec = ""
        gcd_value = ""

        for field in record.resolved_fields:
            fid = str(field.get("id"))
            name = field.get("name")

            if fid == NAME_FIELD_ID or name == "locTextRetrieverMap":
                resolved_name = extract_resolved_name(field)
                resolved_tooltip = extract_resolved_tooltip(field, strings)
                global_combat_id = extract_global_combat_id(field)

            if name in ICON_FIELD_NAMES or fid in ICON_FIELD_IDS:
                val = field.get("value")
                if isinstance(val, str) and val.strip():
                    icon_spec = val.strip()

            if fid == GCD_FIELD_ID or name == GCD_FIELD_NAME:
                val = field.get("value")
                if val is not None:
                    gcd_value = str(val)

        if resolved_name or icon_spec:
            row_data = {
                "fqn": fqn,
                "global_combat_id": global_combat_id,
                "name": resolved_name,
                "tooltip": resolved_tooltip,
                "icon": icon_spec,
                "attack_type": feature_extractor.get_attack_type(record),
                "ablGlobalCooldownTime": gcd_value,
                "mirror_fqn": (mirror_map or {}).get(fqn, ""),
                "exclusive_to_spec": (exclusive_map or {}).get(fqn, ""),
            }

            for col_name, func in TRAIT_EXTRACTORS.items():
                row_data[col_name] = str(func(feature_extractor, record))

            rows.append(row_data)

    rows.sort(key=lambda r: r["fqn"])

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "fqn",
        "global_combat_id",
        "name",
        "tooltip",
        "icon",
        "attack_type",
        "ablGlobalCooldownTime",
        "mirror_fqn",
        "exclusive_to_spec",
        *TRAIT_EXTRACTORS.keys(),
    ]

    try:
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        fallback_path = csv_path.with_name("ability_data_new.csv")
        print(f"\n[WARNING] {csv_path.name} is locked. Writing to {fallback_path.name} instead.")
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

    # Generate initial CSV mapping (needed for tooltip lookups during mirror matching)
    csv_out_path = dest_dir / "ability_data.csv"
    export_ability_icon_map_csv(records, strings, csv_out_path)

    # 7. Spec Exclusivity Analysis (Executed from matching.py)
    print("\n7. Analyzing spec-exclusive abilities & replacements...")
    exclusive_data = find_spec_exclusive_abilities(dest_dir)
    exclusive_json_path = dest_dir / "spec_exclusive_abilities.json"
    with exclusive_json_path.open("w", encoding="utf-8") as f:
        json.dump(exclusive_data, f, indent=2)
    print(f"   -> Exported spec-exclusive mapping to: {exclusive_json_path}")

    # Build exclusive lookup map (FQN -> Spec Name)
    exclusive_map: dict[str, str] = {}
    for adv_class, specs in exclusive_data.items():
        for spec_name, item_list in specs.items():
            for item in item_list:
                item_id = item.get("id")
                if item_id:
                    exclusive_map[item_id] = f"{adv_class}.{spec_name}"

    # 8. Extract Matching Metadata Database (via matching.py)
    print("\n8. Generating matching metadata database...")
    matching_metadata = extract_all_matching_attributes(dest_dir)
    matching_data_json_path = dest_dir / "spec_abilities_matching_data.json"
    with matching_data_json_path.open("w", encoding="utf-8") as f:
        json.dump(matching_metadata, f, indent=2)
    print(f"   -> Exported {len(matching_metadata):,} matching records to: {matching_data_json_path}")

    # 9. Mirror Matching Cascade (via matching.py)
    print("\n9. Running mirror-matching cascade...")
    mirrors_data = run_mirror_matching(dest_dir, matching_metadata, csv_out_path)
    mirrors_json_path = dest_dir / "spec_abilities_strict_cascade_mirrors.json"
    with mirrors_json_path.open("w", encoding="utf-8") as f:
        json.dump(mirrors_data, f, indent=2)
    print(f"   -> Successfully matched and exported {len(mirrors_data):,} mirror pairs to: {mirrors_json_path}")

    # Build bidirectional mirror lookup map (FQN -> Mirrored FQN)
    mirror_map: dict[str, str] = {}
    for entry in mirrors_data:
        rep = entry.get("rep_fqn")
        imp = entry.get("imp_fqn")
        if rep and imp:
            mirror_map[rep] = imp
            mirror_map[imp] = rep

    # Re-export CSV mapping populated with mirror_fqn, exclusive_to_spec, and global_combat_id columns
    mapped_count = export_ability_icon_map_csv(
        records,
        strings,
        csv_out_path,
        mirror_map=mirror_map,
        exclusive_map=exclusive_map,
    )
    print(f"\n   -> Exported {mapped_count:,} primary ability/talent mappings with mirror, exclusivity, and combat ID columns to: {csv_out_path}")

    # 10. Extract icon textures
    used_icons = collect_used_icon_specs(records)
    print(
        f"\n10. Found {len(used_icons):,} unique icon references across abilities & talents."
    )
    print("    Extracting matching icon textures from .tor archives...")

    icon_output_dir = dest_dir / "icons"
    icon_count = extract_icons(
        config.assets_path,
        icon_output_dir,
        config.data_dir,
        allowed_icons=used_icons,
        pts=config.pts,
    )
    print(
        f"    -> Successfully extracted {icon_count:,} referenced icons to {icon_output_dir}"
    )

    if not config.keep_work_files and config.work_dir.exists():
        shutil.rmtree(config.work_dir, ignore_errors=True)

    print("\nAll extraction, matching, and export tasks completed successfully.")


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