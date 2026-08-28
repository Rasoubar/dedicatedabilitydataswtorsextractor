import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

# ==========================================
# 1. Spec Exclusivity Analyzer (True Exclusivity)
# ==========================================

def extract_ids_from_apc_file(file_path: Path) -> set[str]:
    """Extract all ability and talent identifier strings from an APC package JSON file.

    Parses package fields such as 'ablPackageAbilitiesList', 'ablPackageTalentsList',
    'ablPackageActiveAbilitiesList', and 'ablPackageConditionalAbilitiesList'.

    Args:
        file_path: Path to the target APC package JSON file.

    Returns:
        A set of unique ability/talent FQN identifier strings.
    """
    ids = set()
    if not file_path.exists() or file_path.suffix != ".json":
        return ids

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        for field in data.get("fields", []):
            name = field.get("name")
            if name in ("ablPackageAbilitiesList", "ablPackageTalentsList"):
                for item in field.get("value", []):
                    if "key" in item:
                        ids.add(item["key"])
            elif name == "ablPackageActiveAbilitiesList":
                val = field.get("value", {})
                if isinstance(val, dict) and "list" in val:
                    ids.update(val["list"])
            elif name == "ablPackageConditionalAbilitiesList":
                for item in field.get("value", []):
                    if "value" in item and isinstance(item["value"], str):
                        ids.add(item["value"])
    except Exception:
        pass

    return ids


def load_replacement_map(replacement_file: Path) -> dict[str, dict[str, str]]:
    """Load the discipline-level ability replacement map from client definition files.

    Parses 'ablAbilityReplacementMap' in ablAbilityReplacementInfo.json to determine
    which base abilities are upgraded/replaced by specialized discipline abilities.

    Args:
        replacement_file: Path to 'ablAbilityReplacementInfo.json'.

    Returns:
        A nested dictionary mapping spec FQNs to dictionaries of
        `{upgraded_ability_fqn: base_ability_fqn}`.
    """
    replacement_map = defaultdict(dict)
    if not replacement_file.exists():
        return replacement_map

    with replacement_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    for field in data.get("fields", []):
        if field.get("name") == "ablAbilityReplacementMap":
            for spec_entry in field.get("value", []):
                spec_key = spec_entry.get("key")
                for repl in spec_entry.get("value", []):
                    new_abl = repl.get("key")
                    old_abl = repl.get("value")
                    if spec_key and new_abl and old_abl:
                        replacement_map[spec_key][new_abl] = old_abl

    return replacement_map


def find_spec_exclusive_abilities(extracted_root: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Identify true spec-exclusive abilities across all Advanced Classes and disciplines.

    Performs global aggregation across all class and spec package files. An ability is
    designated as exclusive if and only if it is not part of the base/shared class kit
    and appears in exactly one discipline across the entire game dataset.

    Args:
        extracted_root: Root directory containing the extracted game JSON dumps.

    Returns:
        A dictionary structured as:
        `{adv_class_name: {spec_name: [{"id": abl_id, "replaces": base_id_or_none}]}}`.
    """
    apc_dir = extracted_root / "apc"
    replacement_file = extracted_root / "ablAbilityReplacementInfo.json"
    replacements = load_replacement_map(replacement_file)
    results = {}

    if not apc_dir.exists():
        return results

    # Pass 1: Global aggregation of all specs across all base and advanced classes
    spec_data = defaultdict(dict)
    global_ability_spec_counts = defaultdict(set)

    for class_dir in [d for d in apc_dir.iterdir() if d.is_dir()]:
        class_base_ids = extract_ids_from_apc_file(class_dir / "base.json")

        for adv_dir in [d for d in class_dir.iterdir() if d.is_dir()]:
            adv_class_name = f"{class_dir.name}.{adv_dir.name}"
            adv_base_ids = extract_ids_from_apc_file(adv_dir / "base.json")
            combined_base_ids = class_base_ids | adv_base_ids

            spec_files = defaultdict(list)
            for file in adv_dir.glob("*.json"):
                if file.name == "base.json":
                    continue
                spec_name = file.stem.replace("_mods", "")
                spec_files[spec_name].append(file)

            for spec_name, files in spec_files.items():
                all_spec_ids = set()
                for f in files:
                    all_spec_ids.update(extract_ids_from_apc_file(f))

                spec_data[adv_class_name][spec_name] = {
                    "abilities": all_spec_ids,
                    "combined_base_ids": combined_base_ids,
                    "class_name": class_dir.name,
                    "adv_name": adv_dir.name,
                }

                # Record non-base ability presence across (adv_class, spec)
                for abl_id in all_spec_ids:
                    if abl_id not in combined_base_ids:
                        global_ability_spec_counts[abl_id].add((adv_class_name, spec_name))

    # Pass 2: Filter for abilities that appear in exactly 1 spec globally
    for adv_class_name, specs in spec_data.items():
        results[adv_class_name] = {}
        for spec_name, info in specs.items():
            class_name = info["class_name"]
            adv_name = info["adv_name"]
            spec_fqn = f"apc.{class_name}.{adv_name}.{spec_name}"
            spec_replacements = replacements.get(spec_fqn, {})
            combined_base_ids = info["combined_base_ids"]
            abilities = info["abilities"]

            exclusive = []
            for abl_id in sorted(abilities):
                if abl_id in combined_base_ids:
                    continue
                if len(global_ability_spec_counts[abl_id]) == 1:
                    replaces = spec_replacements.get(abl_id)
                    exclusive.append({
                        "id": abl_id,
                        "replaces": replaces if replaces else None,
                    })

            results[adv_class_name][spec_name] = exclusive

    return results


# ==========================================
# 2. Matching Metadata Extractor
# ==========================================

def safe_get_list(obj: Any) -> list:
    """Safely extract a list from a raw object or dictionary wrapper.

    Handles game schema structures where lists are encapsulated inside
    a dictionary containing a `'list'` key.

    Args:
        obj: The object to inspect.

    Returns:
        A Python list containing the items, or an empty list if invalid.
    """
    if isinstance(obj, dict):
        return obj.get("list", [])
    elif isinstance(obj, list):
        return obj
    return []


def extract_fields_map(data: dict) -> dict[str, Any]:
    """Convert a node JSON object's fields list into a key-value mapping.

    Args:
        data: The loaded JSON node dictionary.

    Returns:
        A dictionary mapping field names to their raw values.
    """
    if not isinstance(data, dict):
        return {}
    return {
        item["name"]: item.get("value")
        for item in data.get("fields", [])
        if isinstance(item, dict) and "name" in item
    }


def extract_loc_id(loc_map: Any) -> int | str | None:
    """Extract the localized global combat ID integer from a locTextRetrieverMap structure.

    Searches the internal retriever list for the lookup key '15685385242400905286'
    and extracts the corresponding integer ID value.

    Args:
        loc_map: The raw value of the locTextRetrieverMap field.

    Returns:
        An integer combat ID if found and valid, otherwise string or None.
    """
    if not isinstance(loc_map, dict):
        return None
    retriever = loc_map.get("loc_retriever")
    if not isinstance(retriever, dict):
        return None
    for entry in retriever.get("list", []):
        if isinstance(entry, dict) and str(entry.get("key")) == "15685385242400905286":
            for item in entry.get("value", []):
                if isinstance(item, dict) and (item.get("type_name") == "Integer" or item.get("type") == 2):
                    val = item.get("value")
                    return int(val) if val and str(val).isdigit() else val
    return None


def extract_talent_rank_stats(fields: dict) -> tuple[list[str], list[float]]:
    """Extract sorted stat modification types and numeric values from talent rank definitions.

    Parses 'talRankList' containers including 'talStatsList', 'talModStatsList',
    and 'talTalentStatsIfTagExists'.

    Args:
        fields: Mapping of field names to values for a talent node.

    Returns:
        A tuple of `(sorted_stat_types, sorted_stat_values)`.
    """
    rank_list_wrapper = fields.get("talRankList")
    if not isinstance(rank_list_wrapper, dict):
        return [], []

    stat_types = []
    stat_values = []

    for rank in rank_list_wrapper.get("list", []):
        if not isinstance(rank, list):
            continue

        rank_dict = {
            item.get("name"): item.get("value")
            for item in rank
            if isinstance(item, dict) and "name" in item
        }

        for list_key in ["talStatsList", "talModStatsList", "talTalentStatsIfTagExists"]:
            container = rank_dict.get(list_key)
            if not isinstance(container, dict):
                continue

            for stat_entry in container.get("list", []):
                if not isinstance(stat_entry, list):
                    continue

                stat_dict = {
                    elem.get("name"): elem.get("value")
                    for elem in stat_entry
                    if isinstance(elem, dict) and "name" in elem
                }

                stat_enum = stat_dict.get("talStatInfoStat")
                stat_val = stat_dict.get("talStatInfoStatValue")

                if stat_enum:
                    stat_types.append(str(stat_enum))
                if stat_val is not None:
                    try:
                        stat_values.append(round(float(stat_val), 4))
                    except (ValueError, TypeError):
                        pass

    return sorted(stat_types), sorted(stat_values)


def parse_matching_node(file_path: Path, child_profiles: dict) -> dict[str, Any] | None:
    """Parse a single ability or talent JSON node file into a normalized matching profile.

    Extracts combat parameters, cooldown timers, resource costs, targeting geometries,
    talent stat modifiers, description token signatures, and filesystem footprints.

    Args:
        file_path: Path to the node JSON file.
        child_profiles: Precomputed mapping of node slugs to child sub-effect file stats.

    Returns:
        A normalized dictionary of matching metadata attributes, or None if invalid.
    """
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    fqn = data.get("fqn")
    if not fqn or fqn.startswith("eff."):
        return None

    fields = extract_fields_map(data)

    loc_map = fields.get("locTextRetrieverMap")
    resolved_name = (loc_map.get("resolved_text") if isinstance(loc_map, dict) else None) or fqn.split(".")[-1].replace("_", " ").title()
    loc_id = extract_loc_id(loc_map)

    icon_spec = fields.get("ablIconSpec") or fields.get("talTalentIcon") or fields.get("talIconSpec") or ""
    icon_file = f"{icon_spec}.dds" if icon_spec else "Unknown"

    timer_specs = safe_get_list(fields.get("ablCooldownTimerSpecs"))
    single_timer = fields.get("ablCooldownTimerSpec")
    if single_timer:
        timer_specs.append(single_timer)
    numeric_timers = sorted([str(t) for t in timer_specs if str(t).isdigit() and str(t) != "0"])

    token_list = safe_get_list(fields.get("ablDescriptionTokens"))
    token_seq = []
    for token_group in token_list:
        if isinstance(token_group, list):
            group_dict = {
                elem.get("name"): elem.get("value")
                for elem in token_group
                if isinstance(elem, dict) and "name" in elem
            }
            t_type = group_dict.get("ablDescriptionTokenType")
            t_mult = group_dict.get("ablDescriptionTokenMultiplier")
            if t_type is not None or t_mult is not None:
                token_seq.append({
                    "type": str(t_type),
                    "multiplier": round(float(t_mult), 4) if t_mult is not None else None,
                })

    is_talent = fqn.startswith("tal.") or data.get("base_class_name") == "talTalent"
    tal_stat_types, tal_stat_values = extract_talent_rank_stats(fields) if is_talent else ([], [])

    base_slug = fqn.split(".")[-1].lower()
    child_info = child_profiles.get(base_slug, {"count": 0, "size": 0})

    cast_time = fields.get("ablCastingTime") or fields.get("ablCastTime")
    channel_time = fields.get("ablChannelTime")
    cooldown_time = fields.get("ablCooldownTime")
    energy_cost = fields.get("ablEnergyCost")
    max_range = fields.get("ablMaxRange")
    target_arc = fields.get("ablTargetArc")

    action_names = sorted([str(a) for a in safe_get_list(fields.get("ablHasEffectActionName"))])

    return {
        "fqn": fqn,
        "name": resolved_name,
        "ability_id": loc_id,
        "icon_file": icon_file,
        "base_slug": base_slug,
        "node_type": "talent" if is_talent else "ability",
        "cast_time": round(float(cast_time), 4) if cast_time is not None else None,
        "channel_time": round(float(channel_time), 4) if channel_time is not None else None,
        "cooldown_time": round(float(cooldown_time), 4) if cooldown_time is not None else None,
        "energy_cost": round(float(energy_cost), 4) if energy_cost is not None else None,
        "max_range": round(float(max_range), 4) if max_range is not None else None,
        "target_arc": round(float(target_arc), 4) if target_arc is not None else None,
        "target_rule": str(fields.get("ablTargetRule")) if fields.get("ablTargetRule") is not None else None,
        "combat_mode": str(fields.get("ablCombatMode")) if fields.get("ablCombatMode") is not None else None,
        "shared_cooldown_timers": numeric_timers,
        "action_names": action_names,
        "description_tokens": token_seq,
        "stat_type": str(fields.get("talStatType")) if fields.get("talStatType") is not None else None,
        "stat_value": round(float(fields.get("talStatModifierSpec") or fields.get("talStatValue")), 4) if (fields.get("talStatModifierSpec") or fields.get("talStatValue")) is not None else None,
        "tal_stat_types": tal_stat_types,
        "tal_stat_values": tal_stat_values,
        "child_file_count": child_info["count"],
        "child_file_size": child_info["size"],
    }


def extract_all_matching_attributes(extracted_root: Path) -> dict[str, dict]:
    """Index and profile matching attributes for all ability and talent nodes in the extraction tree.

    First aggregates child sub-effect file counts and sizes across the filesystem,
    then parses each main node into a structured combat attribute profile.

    Args:
        extracted_root: Root directory containing extracted node files.

    Returns:
        A dictionary mapping node FQNs to their matching attribute profile dictionaries.
    """
    child_pattern = re.compile(r"^(.*?)_(\d+)_(\d+)\.json$", re.IGNORECASE)
    child_profiles = defaultdict(lambda: {"count": 0, "size": 0})
    main_files = []

    for file_path in extracted_root.rglob("*.json"):
        if "disciplines" in file_path.parts:
            continue

        match = child_pattern.match(file_path.name)
        if match:
            slug = match.group(1).lower()
            child_profiles[slug]["count"] += 1
            child_profiles[slug]["size"] += file_path.stat().st_size
        else:
            main_files.append(file_path)

    extracted_nodes = {}
    for file_path in main_files:
        node_data = parse_matching_node(file_path, child_profiles)
        if node_data:
            extracted_nodes[node_data["fqn"]] = node_data

    return extracted_nodes


# ==========================================
# 3. Mirror Matching Engine & Disambiguation
# ==========================================

CLASS_MIRRORS = {
    "sith_inquisitor": "jedi_consular",
    "jedi_consular": "sith_inquisitor",
    "sith_warrior": "jedi_knight",
    "jedi_knight": "sith_warrior",
    "bounty_hunter": "trooper",
    "trooper": "bounty_hunter",
    "agent": "smuggler",
    "smuggler": "agent",
}

SPEC_MIRRORS = {
    ("sith_inquisitor", "deception"): "infiltration",
    ("jedi_consular", "infiltration"): "deception",
    ("sith_inquisitor", "darkness"): "combat",
    ("jedi_consular", "combat"): "darkness",
    ("sith_inquisitor", "hatred"): "serenity",
    ("jedi_consular", "serenity"): "hatred",
    ("sith_inquisitor", "corruption"): "seer",
    ("jedi_consular", "seer"): "corruption",
    ("sith_inquisitor", "lightning"): "telekinetics",
    ("jedi_consular", "telekinetics"): "lightning",
    ("sith_inquisitor", "madness"): "balance",
    ("jedi_consular", "balance"): "madness",
    ("sith_warrior", "immortal"): "defense",
    ("jedi_knight", "defense"): "immortal",
    ("sith_warrior", "vengeance"): "vigilance",
    ("jedi_knight", "vigilance"): "vengeance",
    ("sith_warrior", "rage"): "focus",
    ("jedi_knight", "focus"): "rage",
    ("sith_warrior", "annihilation"): "watchman",
    ("jedi_knight", "watchman"): "annihilation",
    ("sith_warrior", "carnage"): "combat",
    ("jedi_knight", "combat"): "carnage",
    ("sith_warrior", "fury"): "concentration",
    ("jedi_knight", "concentration"): "fury",
    ("bounty_hunter", "shield_tech"): "shield_specialist",
    ("trooper", "shield_specialist"): "shield_tech",
    ("bounty_hunter", "firebug"): "plasmatech",
    ("trooper", "plasmatech"): "firebug",
    ("bounty_hunter", "pyrotech"): "plasmatech",
    ("bounty_hunter", "advanced_prototype"): "tactics",
    ("trooper", "tactics"): "advanced_prototype",
    ("bounty_hunter", "bodyguard"): "combat_medic",
    ("trooper", "combat_medic"): "bodyguard",
    ("bounty_hunter", "arsenal"): "gunnery",
    ("trooper", "gunnery"): "arsenal",
    ("bounty_hunter", "innovative_ordnance"): "assault_specialist",
    ("trooper", "assault_specialist"): "innovative_ordnance",
    ("agent", "medic"): "sawbones",
    ("smuggler", "sawbones"): "medic",
    ("agent", "medicine"): "sawbones",
    ("agent", "concealment"): "scrapper",
    ("smuggler", "scrapper"): "concealment",
    ("agent", "lethality"): "ruffian",
    ("smuggler", "ruffian"): "lethality",
    ("agent", "engineering"): "saboteur",
    ("smuggler", "saboteur"): "engineering",
    ("agent", "marksmanship"): "sharpshooter",
    ("smuggler", "sharpshooter"): "marksmanship",
    ("agent", "virulence"): "dirty_fighting",
    ("smuggler", "dirty_fighting"): "virulence",
}

ALL_FILTER_STEPS = [
    "replacement_graph_inversion",
    "shared_cooldown_timers",
    "exact_base_slug",
    "talent_rank_stats",
    "talent_root_stats",
    "talent_values_only",
    "ability_complete_profile",
    "ability_core_combat_stats",
    "range_and_arc",
    "energy_cost",
    "tooltip_similarity",
    "child_files_disk_profile",
    "description_tokens",
    "action_names",
    "root_file_disk_size",
    "shared_icon_exact",
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "for", "by",
    "your", "you", "is", "that", "this", "with", "from", "on", "as", "at", "target"
}


def load_tooltip_csv(csv_path: Path) -> dict[str, dict]:
    """Load Pass-1 CSV export to provide raw tooltip text and icon references for matching.

    Args:
        csv_path: Path to the generated ability_icons.csv file.

    Returns:
        A dictionary mapping node FQNs to their `{"tooltip_text": ..., "icon_file": ...}` data.
    """
    tooltips = {}
    if not csv_path.exists():
        return tooltips

    with csv_path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fqn = (
                row.get("fqn")
                or row.get("FQN")
                or row.get("fqn_name")
                or row.get("node_id")
                or row.get("id")
            )
            if not fqn:
                keys = list(row.keys())
                if keys:
                    fqn = row[keys[0]]
            if not fqn:
                continue

            tooltip = (
                row.get("tooltip")
                or row.get("Tooltip")
                or row.get("description")
                or row.get("Description")
                or row.get("desc")
                or row.get("text")
                or row.get("Text")
                or ""
            )
            icon = (
                row.get("icon")
                or row.get("Icon")
                or row.get("icon_file")
                or row.get("icon_name")
                or ""
            )

            tooltips[fqn.strip()] = {
                "tooltip_text": tooltip.strip(),
                "icon_file": icon.strip(),
            }
    return tooltips


def get_fqn_from_file(file_path: Path, root_folder: Path) -> str:
    """Retrieve the Fully Qualified Name (FQN) of a node file.

    Attempts to read the 'fqn' key from the JSON file; falls back to converting
    the filesystem path relative to root_folder into dot notation.

    Args:
        file_path: Path to the node JSON file.
        root_folder: Base extraction directory.

    Returns:
        The FQN string identifier.
    """
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and data.get("fqn"):
                return data["fqn"]
    except Exception:
        pass
    rel = file_path.relative_to(root_folder).with_suffix("")
    return ".".join(rel.parts)


def get_normalized_spec_key(folder_path: str) -> tuple[str, str, str] | None:
    """Parse a relative folder path into a normalized `(node_root, class_name, spec_name)` tuple.

    Filters out legacy paths and maps internal folder structures into canonical triples.

    Args:
        folder_path: Forward-slash separated relative folder path.

    Returns:
        A tuple of `(node_root, class_name, spec_name)`, or None if not a valid spec path.
    """
    if "legacy" in folder_path.lower():
        return None

    parts = folder_path.split("/")
    if len(parts) < 2:
        return None

    node_root = parts[0]
    matched_class = None
    for p in parts:
        if p in CLASS_MIRRORS:
            matched_class = p
            break

    if not matched_class:
        return None

    spec_name = "base"
    if "skill" in parts:
        s_idx = parts.index("skill")
        if len(parts) > s_idx + 1:
            spec_name = parts[s_idx + 1]

    return (node_root, matched_class, spec_name)


def get_mirror_spec_key(spec_key: tuple[str, str, str]) -> tuple[str, str, str] | None:
    """Determine the cross-faction counterpart spec key for a given normalized spec key.

    Uses CLASS_MIRRORS and SPEC_MIRRORS to map Republic disciplines to Imperial disciplines
    and vice-versa.

    Args:
        spec_key: A tuple of `(node_root, class_name, spec_name)`.

    Returns:
        The mirrored `(node_root, mirror_class_name, mirror_spec_name)` tuple, or None.
    """
    node_root, cls, spec = spec_key
    mirror_cls = CLASS_MIRRORS.get(cls)
    if not mirror_cls:
        return None

    if spec in ("base", "utility"):
        return (node_root, mirror_cls, spec)

    mirror_spec = SPEC_MIRRORS.get((cls, spec))
    if not mirror_spec:
        return None

    return (node_root, mirror_cls, mirror_spec)


def load_replacement_lookup(extracted_root: Path) -> dict[str, str]:
    """Construct a direct lookup map of upgraded ability FQNs to their baseline ability FQNs.

    Args:
        extracted_root: Root directory containing 'ablAbilityReplacementInfo.json'.

    Returns:
        A dictionary of `{upgraded_ability_fqn: base_ability_fqn}`.
    """
    rep_file = extracted_root / "ablAbilityReplacementInfo.json"
    if not rep_file.exists():
        return {}

    with rep_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    upgrade_to_base = {}
    for field in data.get("fields", []):
        if field.get("name") == "ablAbilityReplacementMap":
            for apc_entry in field.get("value", []):
                for pair in apc_entry.get("value", []):
                    upgrade_fqn = pair.get("key")
                    base_fqn = pair.get("value")
                    if upgrade_fqn and base_fqn:
                        upgrade_to_base[upgrade_fqn] = base_fqn
    return upgrade_to_base


def get_filesystem_node_profile(file_path: Path) -> dict[str, Any]:
    """Profile the disk footprint of a node file and all its child sub-effect JSON files.

    Measures root JSON file size, child file count, sorted individual child sizes,
    and total aggregated sub-effect byte size.

    Args:
        file_path: Path to the root node JSON file.

    Returns:
        A dictionary containing filesystem footprint metrics.
    """
    parent = file_path.parent
    stem = file_path.stem
    root_size = file_path.stat().st_size

    children = []
    for sibling in parent.iterdir():
        if not sibling.is_file() or sibling.suffix != ".json" or sibling.stem == stem:
            continue
        if sibling.stem.startswith(f"{stem}_"):
            suffix = sibling.stem[len(stem) + 1:]
            if suffix and (suffix[0].isdigit() or "_" in suffix):
                children.append(sibling)

    child_sizes = sorted([c.stat().st_size for c in children])
    return {
        "root_file_size": root_size,
        "child_file_count": len(children),
        "child_file_sizes": child_sizes,
        "child_file_total_size": sum(child_sizes),
    }


def normalize_tokens(tok_list: Any) -> tuple:
    """Normalize a list of description token dicts/tuples into a canonical, hashable tuple.

    Args:
        tok_list: Raw description token sequence.

    Returns:
        A tuple of `((token_type_str, rounded_multiplier), ...)`.
    """
    if not tok_list or not isinstance(tok_list, list):
        return ()
    norm = []
    for item in tok_list:
        if isinstance(item, dict):
            t = str(item.get("type", ""))
            m = item.get("multiplier")
            norm.append((t, round(float(m), 4) if m is not None else None))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            t = str(item[0])
            m = item[1]
            norm.append((t, round(float(m), 4) if m is not None else None))
    return tuple(norm)


def extract_tooltip_numbers(text: str) -> list[float]:
    """Extract all integer and floating-point numbers from a tooltip text string in order.

    Args:
        text: Raw tooltip text.

    Returns:
        A list of parsed float values.
    """
    if not text:
        return []
    return [float(x) for x in re.findall(r"\b\d+(?:\.\d+)?\b", text)]


def clean_proper_nouns(text: str) -> str:
    """Strip capitalized proper nouns and multi-word proper names from tooltip text.

    Prevents faction-specific terminology (e.g. 'Jedi', 'Sith', 'Trooper') from
    artificially degrading Jaccard text similarity scores between mirror abilities.

    Args:
        text: Tooltip string.

    Returns:
        The cleaned tooltip string with proper nouns removed.
    """
    if not text:
        return ""
    return re.sub(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", "", text)


def stem(word: str) -> str:
    """Apply lightweight suffix stemming to a word token.

    Strips common trailing suffixes ('ing', 'ed', 'es', 's') if sufficient stem length remains.

    Args:
        word: Lowercase word token.

    Returns:
        Stemmed word string.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def get_tooltip_tokens(text: str) -> set[str]:
    """Tokenize, clean, and stem tooltip text into a normalized set of semantic keywords.

    Strips proper nouns, excludes stopwords, filters out short tokens (<3 chars),
    and applies suffix stemming.

    Args:
        text: Localized tooltip text.

    Returns:
        A set of processed word tokens.
    """
    if not text:
        return set()
    cleaned = clean_proper_nouns(text)
    words = re.findall(r"\b[a-z]{3,}\b", cleaned.lower())
    return {stem(w) for w in words if w not in STOPWORDS}


def compute_tooltip_jaccard(a: dict, b: dict) -> float:
    """Calculate the Jaccard similarity index between the tooltip token sets of two nodes.

    $$J(A, B) = \\frac{|Tokens_A \\cap Tokens_B|}{|Tokens_A \\cup Tokens_B|}$$

    Args:
        a: Metadata profile of the first node.
        b: Metadata profile of the second node.

    Returns:
        A float similarity coefficient between 0.0 and 1.0.
    """
    t_a = a.get("tooltip_text", "")
    t_b = b.get("tooltip_text", "")
    if not t_a or not t_b:
        return 0.0
    toks_a = get_tooltip_tokens(t_a)
    toks_b = get_tooltip_tokens(t_b)
    if not toks_a or not toks_b:
        toks_a = {stem(w) for w in re.findall(r"\b[a-z]{3,}\b", t_a.lower()) if w not in STOPWORDS}
        toks_b = {stem(w) for w in re.findall(r"\b[a-z]{3,}\b", t_b.lower()) if w not in STOPWORDS}
    if not toks_a or not toks_b:
        return 0.0
    return len(toks_a & toks_b) / len(toks_a | toks_b)


def check_cooldown_timers(a: dict, b: dict) -> bool:
    """Check if two nodes share at least one non-zero cooldown timer spec ID.

    Criteria: `{T_A} ∩ {T_B} ≠ ∅` (Priority 2).
    """
    a_t, b_t = set(a.get("shared_cooldown_timers", [])), set(b.get("shared_cooldown_timers", []))
    return bool(a_t and b_t and (a_t & b_t))


def check_exact_base_slug(a: dict, b: dict) -> bool:
    """Check if two nodes share an identical terminal FQN slug.

    Criteria: `a.base_slug == b.base_slug` (Priority 3).
    """
    return bool(a.get("base_slug") and a.get("base_slug") == b.get("base_slug"))


def check_talent_rank_stats(a: dict, b: dict) -> bool:
    """Check if two talent nodes have identical sorted stat modification enums and values.

    Criteria: Exact equality of rank stat modifier lists (Priority 4).
    """
    t_a, t_b = a.get("tal_stat_types", []), b.get("tal_stat_types", [])
    v_a, v_b = a.get("tal_stat_values", []), b.get("tal_stat_values", [])
    return bool((t_a or v_a) and t_a == t_b and v_a == v_b)


def check_talent_root_stats(a: dict, b: dict) -> bool:
    """Check if two talent nodes share identical root-level stat types and values.

    Criteria: Exact equality of `talStatType` and `talStatValue` (Priority 5).
    """
    st_a, st_b = a.get("stat_type"), b.get("stat_type")
    sv_a, sv_b = a.get("stat_value"), b.get("stat_value")
    return bool(st_a and st_b and sv_a is not None and sv_b is not None and st_a == st_b and sv_a == sv_b)


def check_talent_values_only(a: dict, b: dict) -> bool:
    """Check if two talent nodes share identical rank stat modifier values when enums are generic.

    Criteria: Exact equality of `tal_stat_values` (Priority 6).
    """
    v_a, v_b = a.get("tal_stat_values", []), b.get("tal_stat_values", [])
    return bool(v_a and v_b and v_a == v_b)


def check_ability_complete_profile(a: dict, b: dict) -> bool:
    """Check if two ability nodes have exact equality across all 8 core combat attributes.

    Evaluates cast time, channel time, cooldown, energy cost, max range, target arc,
    target rule, and combat mode (Priority 7).
    """
    keys = ["cast_time", "channel_time", "cooldown_time", "energy_cost", "max_range", "target_arc", "target_rule", "combat_mode"]
    has_profile = any(a.get(k) is not None and a.get(k) > 0 for k in ["cast_time", "channel_time", "cooldown_time", "energy_cost"])
    return bool(has_profile and all(a.get(k) == b.get(k) for k in keys))


def check_ability_core_combat_stats(a: dict, b: dict) -> bool:
    """Check if two ability nodes have exact equality across core combat throughput stats.

    Evaluates cooldown, cast time, channel time, and max range (Priority 8).
    """
    keys = ["cooldown_time", "cast_time", "channel_time", "max_range"]
    has_stat = any(a.get(k) is not None and a.get(k) > 0 for k in ["cooldown_time", "cast_time", "channel_time"])
    return bool(has_stat and all(a.get(k) == b.get(k) for k in keys))


def check_range_and_arc(a: dict, b: dict) -> bool:
    """Check if two nodes have identical spatial targeting geometry.

    Evaluates max range and target arc equality (Priority 9).
    """
    has_geo = (a.get("max_range") is not None and a.get("max_range") > 0) or a.get("target_arc") is not None
    return bool(has_geo and a.get("max_range") == b.get("max_range") and a.get("target_arc") == b.get("target_arc"))


def check_energy_cost(a: dict, b: dict) -> bool:
    """Check if two nodes have identical non-zero resource/energy consumption (Priority 10)."""
    return bool(a.get("energy_cost") is not None and a.get("energy_cost") > 0 and a.get("energy_cost") == b.get("energy_cost"))


def check_tooltip_similarity(a: dict, b: dict) -> bool:
    """Check if two nodes satisfy semantic tooltip similarity thresholds.

    Requires Jaccard >= 0.30 if shared numbers exist in both tooltips,
    or Jaccard >= 0.45 otherwise (Priority 11).
    """
    t_a = a.get("tooltip_text", "")
    t_b = b.get("tooltip_text", "")
    if not t_a or not t_b:
        return False

    nums_a = set(extract_tooltip_numbers(t_a))
    nums_b = set(extract_tooltip_numbers(t_b))
    jaccard = compute_tooltip_jaccard(a, b)

    if nums_a and nums_b:
        shared_nums = nums_a & nums_b
        if shared_nums:
            return jaccard >= 0.30
        return False
    return jaccard >= 0.45


def check_child_files_disk_profile(a: dict, b: dict) -> bool:
    """Check if two nodes share an identical filesystem sub-effect tree profile.

    Enforces:
    1. Identical child file count.
    2. Tooltip semantic guard ($J \\ge 0.15$) if tooltips exist.
    3. Individual child file size deltas $\\le 12\\%$.
    4. Total aggregated child byte size delta $\\le 8\\%$ (Priority 12).
    """
    cnt_a, cnt_b = a.get("child_file_count", 0), b.get("child_file_count", 0)
    if cnt_a == 0 or cnt_b == 0 or cnt_a != cnt_b:
        return False

    if a.get("tooltip_text") and b.get("tooltip_text"):
        if compute_tooltip_jaccard(a, b) < 0.15:
            return False

    sizes_a = a.get("child_file_sizes", [])
    sizes_b = b.get("child_file_sizes", [])
    if len(sizes_a) != len(sizes_b):
        return False

    for sa, sb in zip(sizes_a, sizes_b):
        max_s = max(sa, sb)
        if max_s > 0 and (abs(sa - sb) / max_s) > 0.12:
            return False

    tot_a, tot_b = a.get("child_file_total_size", 0), b.get("child_file_total_size", 0)
    max_tot = max(tot_a, tot_b)
    if max_tot > 0 and (abs(tot_a - tot_b) / max_tot) > 0.08:
        return False

    return True


def check_description_tokens(a: dict, b: dict) -> bool:
    """Check if two nodes share an exact sequence of description token types and multipliers (Priority 13)."""
    t_a, t_b = normalize_tokens(a.get("description_tokens")), normalize_tokens(b.get("description_tokens"))
    return bool(t_a and t_b and t_a == t_b)


def check_action_names(a: dict, b: dict) -> bool:
    """Check if two nodes reference an identical sorted list of sub-effect action names (Priority 14)."""
    act_a, act_b = a.get("action_names", []), b.get("action_names", [])
    return bool(act_a and act_b and act_a == act_b)


def check_root_file_disk_size(a: dict, b: dict) -> bool:
    """Check if two nodes have nearly identical root JSON file sizes on disk.

    Guarded by a semantic tooltip shield ($J \\ge 0.15$), requires relative byte size
    variation $\\le 8\\%$ (Priority 15).
    """
    ra, rb = a.get("root_file_size", 0), b.get("root_file_size", 0)
    if ra == 0 or rb == 0:
        return False

    if a.get("tooltip_text") and b.get("tooltip_text"):
        if compute_tooltip_jaccard(a, b) < 0.15:
            return False

    max_r = max(ra, rb)
    return (abs(ra - rb) / max_r) <= 0.08


def check_shared_icon_exact(a: dict, b: dict) -> bool:
    """Check if two nodes reference the exact same icon texture .dds filename (Priority 16)."""
    i_a, i_b = a.get("icon_file", "Unknown"), b.get("icon_file", "Unknown")
    return bool(i_a and i_b and i_a != "Unknown" and i_b != "Unknown" and i_a.lower() == i_b.lower())


def get_candidate_filters(
    node_a: dict,
    replacement_deductions: dict | None = None,
    allow_root_size: bool = True,
) -> list[tuple[str, Any]]:
    """Assemble the prioritized list of elimination filter predicates applicable to a given node.

    Filters are ordered strictly according to the 16-tier cascade hierarchy.

    Args:
        node_a: Profile dictionary of the source node.
        replacement_deductions: Optional dictionary of Pass-1 replacement bridge targets.
        allow_root_size: Whether to enable root JSON file size matching (disabled in Pass 3).

    Returns:
        A list of `(filter_name, filter_predicate_callable)` tuples.
    """
    filters = []

    if replacement_deductions and node_a.get("fqn") in replacement_deductions:
        target_fqn = replacement_deductions[node_a["fqn"]]
        filters.append(("replacement_graph_inversion", lambda a, b: b.get("fqn") == target_fqn))

    if node_a.get("shared_cooldown_timers"):
        filters.append(("shared_cooldown_timers", check_cooldown_timers))
    if node_a.get("base_slug"):
        filters.append(("exact_base_slug", check_exact_base_slug))

    if node_a.get("node_type") == "talent":
        if node_a.get("tal_stat_types") or node_a.get("tal_stat_values"):
            filters.append(("talent_rank_stats", check_talent_rank_stats))
        if node_a.get("stat_type") and node_a.get("stat_value") is not None:
            filters.append(("talent_root_stats", check_talent_root_stats))
        if node_a.get("tal_stat_values"):
            filters.append(("talent_values_only", check_talent_values_only))

    if node_a.get("node_type") == "ability":
        filters.append(("ability_complete_profile", check_ability_complete_profile))
        filters.append(("ability_core_combat_stats", check_ability_core_combat_stats))
        if node_a.get("max_range") is not None or node_a.get("target_arc") is not None:
            filters.append(("range_and_arc", check_range_and_arc))
        if node_a.get("energy_cost") is not None:
            filters.append(("energy_cost", check_energy_cost))

    if node_a.get("tooltip_text"):
        filters.append(("tooltip_similarity", check_tooltip_similarity))

    if node_a.get("child_file_count", 0) > 0:
        filters.append(("child_files_disk_profile", check_child_files_disk_profile))

    if node_a.get("description_tokens"):
        filters.append(("description_tokens", check_description_tokens))
    if node_a.get("action_names"):
        filters.append(("action_names", check_action_names))

    if allow_root_size and node_a.get("root_file_size", 0) > 0:
        filters.append(("root_file_disk_size", check_root_file_disk_size))

    if node_a.get("icon_file") and node_a.get("icon_file") != "Unknown":
        filters.append(("shared_icon_exact", check_shared_icon_exact))

    return filters


def disambiguate_tooltip_candidates(node_a: dict, surviving: list[dict]) -> list[dict]:
    """Break ties among candidates surviving tooltip similarity matching.

    Applies sequential tie-breaking heuristics:
    1. Exact ordered sequence of extracted numbers.
    2. Exact set equality of extracted numbers.
    3. Jaccard similarity score lead of $\\ge 0.05$ over the runner-up.

    Args:
        node_a: Profile dictionary of the source node.
        surviving: List of surviving candidate dictionaries.

    Returns:
        The refined list of candidate dictionaries.
    """
    if len(surviving) <= 1:
        return surviving

    t_a = node_a.get("tooltip_text", "")
    nums_a = extract_tooltip_numbers(t_a)

    exact_matches = [
        b for b in surviving if extract_tooltip_numbers(b.get("tooltip_text", "")) == nums_a
    ]
    if len(exact_matches) == 1:
        return exact_matches
    if len(exact_matches) > 1:
        surviving = exact_matches
    else:
        set_a = set(nums_a)
        set_matches = [
            b for b in surviving if set(extract_tooltip_numbers(b.get("tooltip_text", ""))) == set_a
        ]
        if len(set_matches) == 1:
            return set_matches
        if len(set_matches) > 1:
            surviving = set_matches

    scored = sorted(surviving, key=lambda b: compute_tooltip_jaccard(node_a, b), reverse=True)
    if len(scored) >= 2:
        j_best = compute_tooltip_jaccard(node_a, scored[0])
        j_second = compute_tooltip_jaccard(node_a, scored[1])
        if j_best - j_second >= 0.05:
            return [scored[0]]

    return surviving


def eliminate_candidates(
    node_a: dict,
    candidate_pool: list[dict],
    replacement_deductions: dict | None = None,
    allow_root_size: bool = True,
) -> tuple[list[dict], str]:
    """Filter an opposing candidate pool against a source node through the cascade hierarchy.

    Evaluates filters sequentially. If a filter reduces candidate count without wiping
    the pool, the candidate pool is pruned. Terminates when exactly one candidate remains.

    Args:
        node_a: Profile of the source node being matched.
        candidate_pool: Pool of opposing candidates from the mirrored discipline.
        replacement_deductions: Optional dictionary of Pass-1 replacement deductions.
        allow_root_size: Whether to enable root file disk size checks.

    Returns:
        A tuple of `(surviving_candidates, winning_filter_name_or_reason)`.
    """
    candidates = [b for b in candidate_pool if b.get("node_type") == node_a.get("node_type")]
    if not candidates:
        return [], "no_type_candidates"

    filters = get_candidate_filters(node_a, replacement_deductions, allow_root_size=allow_root_size)
    last_filter = "no_filter_passed"

    for filter_name, filter_fn in filters:
        surviving = [b for b in candidates if filter_fn(node_a, b)]

        if filter_name == "tooltip_similarity" and len(surviving) > 1:
            surviving = disambiguate_tooltip_candidates(node_a, surviving)

        if filter_name == "child_files_disk_profile" and len(surviving) > 1:
            sz_a = node_a.get("child_file_total_size", 0)
            deltas = [abs(sz_a - b.get("child_file_total_size", 0)) for b in surviving]
            min_delta = min(deltas)
            sorted_deltas = sorted(deltas)

            is_clear_winner = False
            if len(sorted_deltas) > 1:
                runner_up = sorted_deltas[1]
                gap = (runner_up - min_delta) / max(sz_a, 1)
                if gap > 0.02:
                    is_clear_winner = True
            else:
                is_clear_winner = True

            if is_clear_winner:
                closest = [
                    b
                    for b in surviving
                    if abs(sz_a - b.get("child_file_total_size", 0)) == min_delta
                ]
                if 0 < len(closest) < len(surviving):
                    surviving = closest

        if 0 < len(surviving) < len(candidates):
            candidates = surviving
            last_filter = filter_name
        elif len(candidates) == 1 and len(surviving) == 1:
            if last_filter == "no_filter_passed":
                last_filter = filter_name

        if len(candidates) == 1 and last_filter != "no_filter_passed":
            break

    if len(candidates) == 1 and last_filter != "no_filter_passed":
        return candidates, last_filter

    return candidates, "no_filter_passed"


def resolve_node(target: Path | str, root_folder: Path, meta_db: dict, tooltip_db: dict) -> dict[str, Any]:
    """Hydrate a complete matching profile for a node from metadata, filesystem, and CSV data.

    Args:
        target: Filepath or FQN string of the target node.
        root_folder: Root extraction directory.
        meta_db: Precomputed metadata dictionary keyed by FQN.
        tooltip_db: Precomputed tooltip and icon dictionary from Pass-1 CSV.

    Returns:
        A fully enriched node profile dictionary ready for matching.
    """
    if isinstance(target, Path):
        file_path = target
        fqn = get_fqn_from_file(file_path, root_folder)
        fs_profile = get_filesystem_node_profile(file_path)
    else:
        fqn = str(target)
        fs_profile = {"root_file_size": 0, "child_file_count": 0, "child_file_sizes": [], "child_file_total_size": 0}

    base_data = meta_db.get(fqn, {
        "fqn": fqn,
        "name": fqn.split(".")[-1].replace("_", " ").title(),
        "ability_id": None,
        "icon_file": "Unknown",
        "base_slug": fqn.split(".")[-1].lower(),
        "node_type": "talent" if fqn.startswith("tal.") else "ability",
        "shared_cooldown_timers": [],
        "description_tokens": [],
        "action_names": [],
        "tal_stat_types": [],
        "tal_stat_values": [],
    })

    node = dict(base_data)
    node.update(fs_profile)

    if tooltip_db and fqn in tooltip_db:
        t_info = tooltip_db[fqn]
        if t_info.get("tooltip_text"):
            node["tooltip_text"] = t_info["tooltip_text"]
        if t_info.get("icon_file") and node.get("icon_file") in (None, "Unknown"):
            node["icon_file"] = t_info["icon_file"]

    return node


def run_mirror_matching(extracted_root: Path, meta_db: dict, tooltip_csv_path: Path) -> list[dict]:
    """Execute the deterministic 3-pass mirror matching cascade with bidirectional consensus.

    Pipeline stages:
    - Pass 1: Strict Normalized Spec Pools (Direct spec-to-spec counterpart matching).
    - Pass 2: Replacement Inversion Bridges (Deducing baseline abilities from upgraded spec matches).
    - Pass 3: Class-Family Merged Pools (Cross-spec consolidation for shared class utilities).

    Requires bidirectional validation: a pair `(A, B)` is only accepted if forward elimination
    $A \\to B$ and reverse elimination $B \\to A$ independently resolve to the exact same nodes.

    Args:
        extracted_root: Root directory of extracted JSON nodes.
        meta_db: Precomputed matching metadata profile database.
        tooltip_csv_path: Path to the Pass-1 `ability_icons.csv` export.

    Returns:
        A list of verified mirror match records containing Republic/Imperial names, FQNs,
        folder locations, and the winning filter rule reason.
    """
    tooltip_db = load_tooltip_csv(tooltip_csv_path)
    upgrade_to_base = load_replacement_lookup(extracted_root)

    spec_pools = {}
    node_to_folder = {}

    for base_dir in [extracted_root / "abl", extracted_root / "tal"]:
        if not base_dir.exists():
            continue
        for file_path in base_dir.rglob("*.json"):
            if not file_path.is_file() or "legacy" in file_path.parts:
                continue
            if file_path.stem and file_path.stem[-1].isdigit():
                continue

            fqn = get_fqn_from_file(file_path, extracted_root)
            folder_key = file_path.parent.relative_to(extracted_root).as_posix()
            node_to_folder[fqn] = folder_key

            spec_key = get_normalized_spec_key(folder_key)
            if not spec_key:
                continue

            spec_pools.setdefault(spec_key, []).append(file_path)

    seen_spec_pairs = set()
    spec_pairs = []

    for key in spec_pools:
        mirror_key = get_mirror_spec_key(key)
        if not mirror_key or mirror_key not in spec_pools:
            continue

        pair_id = tuple(sorted([key, mirror_key]))
        if pair_id in seen_spec_pairs:
            continue
        seen_spec_pairs.add(pair_id)

        is_rep = key[1] in ["jedi_consular", "jedi_knight", "trooper", "smuggler"]
        pub_key = key if is_rep else mirror_key
        imp_key = mirror_key if is_rep else key
        spec_pairs.append((pub_key, imp_key))

    spec_state = {}
    for pub_key, imp_key in spec_pairs:
        spec_state[(pub_key, imp_key)] = {
            "pub_nodes": [resolve_node(p, extracted_root, meta_db, tooltip_db) for p in spec_pools[pub_key]],
            "imp_nodes": [resolve_node(p, extracted_root, meta_db, tooltip_db) for p in spec_pools[imp_key]],
            "matched": [],
        }

    # PASS 1: Strict Normalized Spec Pools
    for (pub_key, imp_key), state in spec_state.items():
        unmatched_pub = state["pub_nodes"]
        unmatched_imp = state["imp_nodes"]

        while True:
            newly_pub, newly_imp = set(), set()
            for a in unmatched_pub:
                if a["fqn"] in newly_pub:
                    continue
                available_imp = [b for b in unmatched_imp if b["fqn"] not in newly_imp]
                cands_a, reason = eliminate_candidates(a, available_imp, allow_root_size=True)

                if len(cands_a) == 1 and reason != "no_filter_passed":
                    b = cands_a[0]
                    available_pub = [x for x in unmatched_pub if x["fqn"] not in newly_pub]
                    cands_b, rev_reason = eliminate_candidates(b, available_pub, allow_root_size=True)
                    if len(cands_b) == 1 and cands_b[0]["fqn"] == a["fqn"] and rev_reason != "no_filter_passed":
                        state["matched"].append((a, b, reason))
                        newly_pub.add(a["fqn"])
                        newly_imp.add(b["fqn"])

            if not newly_pub:
                break
            unmatched_pub = [p for p in unmatched_pub if p["fqn"] not in newly_pub]
            unmatched_imp = [i for i in unmatched_imp if i["fqn"] not in newly_imp]

        state["pub_nodes"] = unmatched_pub
        state["imp_nodes"] = unmatched_imp

    # Build replacement graph inversion bridges
    replacement_deductions = {}
    for state in spec_state.values():
        for a, b, _ in state["matched"]:
            base_a = upgrade_to_base.get(a["fqn"])
            base_b = upgrade_to_base.get(b["fqn"])
            if base_a and base_b:
                replacement_deductions[base_a] = base_b
                replacement_deductions[base_b] = base_a

    # PASS 2: Pass with Replacement Bridges
    for (pub_key, imp_key), state in spec_state.items():
        unmatched_pub = state["pub_nodes"]
        unmatched_imp = state["imp_nodes"]

        while True:
            newly_pub, newly_imp = set(), set()
            for a in unmatched_pub:
                if a["fqn"] in newly_pub:
                    continue
                available_imp = [b for b in unmatched_imp if b["fqn"] not in newly_imp]
                cands_a, reason = eliminate_candidates(a, available_imp, replacement_deductions=replacement_deductions, allow_root_size=True)

                if len(cands_a) == 1 and reason != "no_filter_passed":
                    b = cands_a[0]
                    available_pub = [x for x in unmatched_pub if x["fqn"] not in newly_pub]
                    cands_b, rev_reason = eliminate_candidates(b, available_pub, replacement_deductions=replacement_deductions, allow_root_size=True)
                    if len(cands_b) == 1 and cands_b[0]["fqn"] == a["fqn"] and rev_reason != "no_filter_passed":
                        state["matched"].append((a, b, reason))
                        newly_pub.add(a["fqn"])
                        newly_imp.add(b["fqn"])

            if not newly_pub:
                break
            unmatched_pub = [p for p in unmatched_pub if p["fqn"] not in newly_pub]
            unmatched_imp = [i for i in unmatched_imp if i["fqn"] not in newly_imp]

        state["pub_nodes"] = unmatched_pub
        state["imp_nodes"] = unmatched_imp

    # PASS 3: Class Family Merged Pools
    class_pairs = [
        ("smuggler", "agent"),
        ("trooper", "bounty_hunter"),
        ("jedi_consular", "sith_inquisitor"),
        ("jedi_knight", "sith_warrior"),
    ]

    cross_folder_matches = []
    for rep_cls, imp_cls in class_pairs:
        rep_unmatched, imp_unmatched = [], []

        for (pub_k, imp_k), state in spec_state.items():
            if pub_k[1] == rep_cls:
                rep_unmatched.extend(state["pub_nodes"])
                state["pub_nodes"] = []
            if imp_k[1] == imp_cls:
                imp_unmatched.extend(state["imp_nodes"])
                state["imp_nodes"] = []

        while True:
            newly_pub, newly_imp = set(), set()
            for a in rep_unmatched:
                if a["fqn"] in newly_pub:
                    continue
                available_imp = [b for b in imp_unmatched if b["fqn"] not in newly_imp]
                cands_a, reason = eliminate_candidates(a, available_imp, replacement_deductions=replacement_deductions, allow_root_size=False)

                if len(cands_a) == 1 and reason != "no_filter_passed":
                    b = cands_a[0]
                    available_pub = [x for x in rep_unmatched if x["fqn"] not in newly_pub]
                    cands_b, rev_reason = eliminate_candidates(b, available_pub, replacement_deductions=replacement_deductions, allow_root_size=False)
                    if len(cands_b) == 1 and cands_b[0]["fqn"] == a["fqn"] and rev_reason != "no_filter_passed":
                        cross_folder_matches.append((a, b, f"MergedClass_{reason}"))
                        newly_pub.add(a["fqn"])
                        newly_imp.add(b["fqn"])

            if not newly_pub:
                break
            rep_unmatched = [p for p in rep_unmatched if p["fqn"] not in newly_pub]
            imp_unmatched = [i for i in imp_unmatched if i["fqn"] not in newly_imp]

    output_data = []
    for (pub_k, imp_k), state in spec_state.items():
        for a, b, reason in state["matched"]:
            output_data.append({
                "rep_folder": node_to_folder.get(a["fqn"], "unknown"),
                "imp_folder": node_to_folder.get(b["fqn"], "unknown"),
                "rep_name": a["name"],
                "rep_fqn": a["fqn"],
                "imp_name": b["name"],
                "imp_fqn": b["fqn"],
                "reason": reason,
            })

    for a, b, reason in cross_folder_matches:
        output_data.append({
            "rep_folder": node_to_folder.get(a["fqn"], "unknown"),
            "imp_folder": node_to_folder.get(b["fqn"], "unknown"),
            "rep_name": a["name"],
            "rep_fqn": a["fqn"],
            "imp_name": b["name"],
            "imp_fqn": b["fqn"],
            "reason": reason,
        })

    return output_data