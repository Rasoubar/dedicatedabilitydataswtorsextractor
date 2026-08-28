# SWTOR Data Extraction & Combat Analytics Pipeline

## System Architecture

The pipeline processes raw SWTOR client assets into structured JSON node dumps, combat capability profiles, mirror-matching databases, and tabular CSV indexes.

```
SWTOR Client Archives (.tor) + Jedipedia Definition Tables (gom.js, fnv1a64.js)
                                     │
                                     ▼
                     [ 1. Pipeline Orchestrator (main.py) ]
                     │  ├── extract_relevant_files()
                     │  ├── traverse_combat_graph()
                     │  └── write_node_dump()
                                     │
         ┌───────────────────────────┴───────────────────────────┐
         ▼                                                       ▼
  Raw Node Tree (/abl, /tal, /apc)                       Initial Pass-1 CSV
  (data/extracted/)                                      (ability_icons.csv)
         │                                                       │
         └───────────────────────────┬───────────────────────────┘
                                     ▼
                  [ 2. Analytical Engine (matching.py) ]
                  │  ├── find_spec_exclusive_abilities()
                  │  ├── extract_all_matching_attributes()
                  │  └── run_mirror_matching()
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
spec_exclusive_abilities.json   spec_abilities_            spec_abilities_strict_
                                matching_data.json         cascade_mirrors.json
         │                                                       │
         └───────────────────────────┬───────────────────────────┘
                                     ▼
                     [ 3. Finalization (main.py) ]
                     │  ├── Pass-2 CSV Injection (mirror_fqn, exclusive_to_spec)
                     │  └── extract_icons() (Texture asset extraction)
                                     │
                                     ▼
                     Final Production Artifacts
```

---

## Module 1: Pipeline Orchestrator (`main.py`)

`main.py` controls extraction, graph dependency resolution, attribute parsing, and final file persistence.

### 1. Graph Traversal & Extraction
* **Entry Points:** Discovers roots across disciplines (`dis.`), advanced player classes (`apc.`), baseline origin story abilities, and item/relic/adrenal definitions.
* **Dependency Walking:** `traverse_combat_graph()` recursively walks references (`effEffect`, `ablEffectIDs`, `conEntitySpec`) while resolving FNV-1a 64-bit hashed tags and GOM field identifiers.
* **Disk Output:** `write_node_dump()` structures nodes into categorized folders (`/abl`, `/tal`, `/apc`, `/dis`).

### 2. Ability Trait Evaluation (`AbilityFeatureExtractor`)
Inspects the resolved field tree and action definitions to classify combat behavior:

```python
# Field Identification Constants
NAME_FIELD_ID = "4611686102842470023"         # locTextRetrieverMap
GCD_FIELD_ID = "4611686019453829630"          # ablGlobalCooldownTime
IS_PASSIVE_FIELD_ID = "4611686019453829615"   # ablIsPassive
COMBAT_ID_LOOKUP_KEY = "15685385242400905286" # Global Combat ID key inside locTextRetrieverMap
```

* **Attack Type Classification:**
  * **Melee / Ranged:** Triggered if `effAction_WeaponDamage` is present in sub-effects, evaluated against `ablCombatMode` (`staCombatModeMelee` vs. `staCombatModeRanged`).
  * **Tech / Force:** Triggered if `effAction_SpellDamage` contains `effParam_SpellType` with value `"2"` (Tech) or `"3"` (Force).
  * Combines into compound strings (e.g., `Melee/Tech`, `Ranged/Force`, or `None`).
* **Passive Evaluation (`is_passive`):** Checks if `ablIsPassive` is present and explicitly set to `True`.
* **Global Combat ID (`global_combat_id`):** Traverses `locTextRetrieverMap` looking for lookup key `15685385242400905286` and extracts the associated 64-bit integer ID string.
* **Tactical Flags:** Inspects effect action blocks to flag `is_interrupt` (`effAction_AbilityInterrupt`), `grants_absorb` (`effAction_AbsorbDamage`), `its_revive` (`offer_revive`), and `ignore_alacrity`.

### 3. Two-Pass CSV Exporter
1. **Pass 1 (Bootstrap):** Writes `ability_icons.csv` with resolved names and tooltips. This output is consumed by `matching.py` for semantic text comparisons.
2. **Pass 2 (Enrichment):** Reads the generated `spec_exclusive_abilities.json` and `spec_abilities_strict_cascade_mirrors.json` to populate `mirror_fqn` and `exclusive_to_spec`.

---

## Module 2: Analytical Engine (`matching.py`)

`matching.py` performs data mining on the extracted JSON graph without modifying raw assets.

### 1. True Spec Exclusivity Analyzer (`find_spec_exclusive_abilities`)
Identifies abilities and passives locked to exactly one discipline across the entire game.

* **Step 1 (Global Aggregation):** Walks `/apc` packages, isolating non-base abilities across all Advanced Classes and disciplines into `global_ability_spec_counts`.
* **Step 2 (Exclusivity Filtering):** An ability is marked exclusive **if and only if** its global appearance count equals 1:
  $$\text{Global Spec Count}(A) = 1 \quad \text{and} \quad A \notin \text{Base Class Abilities}$$
* **Shared Discipline Handling:** Shared archetype abilities (e.g., *Creeping Terror* appearing in both Hatred Assassin and Madness Sorcerer) register a count of 2 and are excluded from the single-spec mapping.
* **Replacement Mapping:** Captures replacement chains from `ablAbilityReplacementInfo.json` to link baseline abilities with their upgraded counterparts (e.g., *Serrated Bolt* replacing *Charged Bolts*).

### 2. Matching Metadata Extractor (`extract_all_matching_attributes`)
Constructs an analytical database (`spec_abilities_matching_data.json`) across every ability and talent node:
* **Combat Invariants:** Cooldowns, cast times, channel durations, energy/force costs, max range, and target arc.
* **Talent Modifiers:** Extracts `talStatInfoStat` enums and numeric `talStatInfoStatValue` entries from `talRankList`.
* **Sub-Effect Disk Fingerprints:** Measures child file counts and byte sizes (`<slug>_X_Y.json`) to quantify the underlying effect tree.
* **Description Tokens:** Normalizes token types and multiplier tuples from `ablDescriptionTokens`.

---

## The 16-Tier Cascade Matching Algorithm

Mirror matching runs through an elimination cascade. Candidate pools are filtered through sixteen ordered criteria.

```
               Incoming Candidate Pool (Opposing Faction)
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 1. Structural Checks                                                │
│    • replacement_graph_inversion                                    │
│    • shared_cooldown_timers                                         │
│    • exact_base_slug                                                │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. Combat & Talent Profiles                                         │
│    • talent_rank_stats / talent_root_stats / talent_values_only     │
│    • ability_complete_profile / ability_core_combat_stats           │
│    • range_and_arc / energy_cost                                    │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. Semantic & Filesystem Heuristics                                 │
│    • tooltip_similarity (with numeric/Jaccard tie-breaking)         │
│    • child_files_disk_profile (with 2% gap disambiguation)          │
│    • description_tokens / action_names                              │
│    • root_file_disk_size / shared_icon_exact                        │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                   Candidate Pool Size Evaluated:
                   • Size == 1: Verify Reverse Pass
                   • Size > 1:  Apply Next Filter
                   • Size == 0: Reject Filter Step
```

### Elimination Rules & Fallback Layers

| Priority | Filter Identifier | Invariant & Logic Applied |
| :---: | :--- | :--- |
| **1** | `replacement_graph_inversion` | Resolves deduced replacements bridged from Pass 1 matches. |
| **2** | `shared_cooldown_timers` | Match on shared numeric cooldown timer spec ID: $\{T_A\} \cap \{T_B\} \neq \emptyset$. |
| **3** | `exact_base_slug` | Match on identical terminal FQN slug (e.g., `abl.trooper.guard` $\leftrightarrow$ `abl.bounty_hunter.guard`). |
| **4** | `talent_rank_stats` | Exact equality of sorted stat modification enums and numeric values. |
| **5** | `talent_root_stats` | Exact equality of root `talStatType` and `talStatValue`. |
| **6** | `talent_values_only` | Fallback matching sorted modifier values when stat enums are generic. |
| **7** | `ability_complete_profile` | Exact match on cast time, channel, cooldown, cost, range, arc, target rule, and combat mode. |
| **8** | `ability_core_combat_stats` | Exact match on cooldown, cast, channel, and range. |
| **9** | `range_and_arc` | Exact match on `max_range` and `target_arc`. |
| **10** | `energy_cost` | Match on identical non-zero resource consumption. |
| **11** | `tooltip_similarity` | Semantic Jaccard text overlap $\ge 0.45$ (or $\ge 0.30$ if shared numbers exist). |
| **12** | `child_files_disk_profile` | Identical child counts, individual file sizes $\le 12\%$ delta, total size $\le 8\%$ delta. |
| **13** | `description_tokens` | Exact sequence match of description token types and multipliers. |
| **14** | `action_names` | Exact match on sub-effect action name lists. |
| **15** | `root_file_disk_size` | Root JSON file size variation $\le 8\%$ delta. |
| **16** | `shared_icon_exact` | Match on identical `.dds` texture asset references. |

### Disambiguation Mechanics
* **Tooltip Tie-Breaking:** If multiple candidates pass `tooltip_similarity`, ties are broken by:
  1. Exact matching of ordered numeric sequences extracted from the tooltip text.
  2. Exact set equality of numeric tokens.
  3. Top-ranked candidate selection if its Jaccard score leads the runner-up by $\Delta J \ge 0.05$.
* **Disk Profile Tie-Breaking:** If multiple candidates pass `child_files_disk_profile`, the closest byte-size match is chosen only if the gap to the next-closest candidate exceeds $2\%$ of the total file size:
  $$\frac{\delta_{\text{runner-up}} - \delta_{\text{closest}}}{\max(Size_A, 1)} > 0.02$$
* **Bidirectional Consensus:** An association $(A, B)$ is accepted **only** when forward elimination ($A \rightarrow B$) and reverse elimination ($B \rightarrow A$) independently resolve to the exact same pair.

---

## Output Data Specifications

### 1. `ability_icons.csv`
Primary tabular export generated in `data/extracted/ability_icons.csv`:

| Column Name | Type | Description |
| :--- | :---: | :--- |
| `fqn` | `string` | Fully Qualified Node Name (e.g., `abl.sith_inquisitor.spike`). |
| `global_combat_id` | `string` | Game engine localized combat ID (from lookup key `15685385242400905286`). |
| `name` | `string` | In-game localized name. |
| `tooltip` | `string` | In-game localized descriptive tooltip. |
| `icon` | `string` | Icon asset stem (maps to `icons/<icon>.dds`). |
| `attack_type` | `string` | Attack category (`Melee`, `Ranged`, `Tech`, `Force`, or combinations). |
| `ablGlobalCooldownTime` | `float` | GCD duration (`-1.0` if off-GCD, `0.0` or `>0` for standard GCD). |
| `mirror_fqn` | `string` | Cross-faction mirror node FQN. |
| `exclusive_to_spec` | `string` | Formatted discipline identifier (`class.spec`) if truly exclusive. |
| `is_interrupt` | `bool` | `True` if ability interrupts enemy spell casts. |
| `grants_absorb` | `bool` | `True` if ability grants a damage absorption shield. |
| `its_revive` | `bool` | `True` if ability offers in-combat or out-of-combat resurrection. |
| `ignore_alacrity` | `bool` | `True` if ability cooldown/cast time ignores alacrity stat scaling. |
| `is_passive` | `bool` | `True` if node is an innate passive (`ablIsPassive == True`). |

### 2. `spec_exclusive_abilities.json`
Hierarchy mapping Advanced Classes and specs to exclusive node IDs and replacement references:
```json
{
  "agent.operative": {
    "concealment": [
      {
        "id": "abl.agent.skill.concealment.veiled_strike",
        "replaces": "abl.agent.shiv"
      }
    ]
  }
}
```

### 3. `spec_abilities_strict_cascade_mirrors.json`
Verified 1:1 cross-faction mirror mapping table:
```json
[
  {
    "rep_folder": "abl/jedi_consular",
    "imp_folder": "abl/sith_inquisitor",
    "rep_name": "Mind Snap",
    "rep_fqn": "abl.jedi_consular.mind_snap",
    "imp_name": "Jolt",
    "imp_fqn": "abl.sith_inquisitor.jolt",
    "reason": "ability_complete_profile"
  }
]
```

---

## Developer Extension Guide

### Adding a New Boolean Trait
1. Locate `# --- Field Constants ---` in `main.py` and register the field identifiers:
   ```python
   NEW_TRAIT_FIELD_ID = "4611686XXXXXXXXXXXX"
   NEW_TRAIT_FIELD_NAME = "ablNewTraitName"
   ```
2. Add an evaluation method to `AbilityFeatureExtractor`:
   ```python
   def check_new_trait(self, record: NodeRecord) -> bool:
       val = get_field_val(record, NEW_TRAIT_FIELD_NAME, NEW_TRAIT_FIELD_ID)
       return val is True
   ```
3. Register the method in `TRAIT_EXTRACTORS`:
   ```python
   TRAIT_EXTRACTORS: dict[str, Callable[[AbilityFeatureExtractor, NodeRecord], bool]] = {
       ...
       "new_trait_column": lambda ext, rec: ext.check_new_trait(rec),
   }
   ```
   *The column will automatically be included in `ability_icons.csv` headers and evaluated during extraction.*

### Modifying or Adding Mirror Matching Filters
1. Open `matching.py` and implement the filter function:
   ```python
   def check_custom_property(a: dict, b: dict) -> bool:
       return bool(a.get("custom_val") and a.get("custom_val") == b.get("custom_val"))
   ```
2. Register the filter name in `ALL_FILTER_STEPS`:
   ```python
   ALL_FILTER_STEPS = [
       ...
       "custom_property_step",
       ...
   ]
   ```
3. Insert the filter inside `get_candidate_filters()` at the desired priority tier:
   ```python
   if node_a.get("custom_val"):
       filters.append(("custom_property_step", check_custom_property))
   ```