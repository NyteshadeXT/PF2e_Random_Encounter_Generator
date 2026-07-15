from __future__ import annotations

import base64
import ctypes
import html
import json
import math
import os
import random
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("PF2E_DB_PATH", ROOT / "creature_db.db"))
PORT = int(os.getenv("PF2E_PORT", "8765"))
UNDETERMINED_REGION = "__undetermined__"
SETTINGS_PATH = Path(os.getenv("PF2E_SETTINGS_PATH", Path(os.getenv("LOCALAPPDATA", ROOT)) / "PF2e Encounter Generator" / "settings.json"))
DEFAULT_LOOT_GENERATOR_PATH = ROOT.parent / "PF2e_Encounter_Generator"
if not DEFAULT_LOOT_GENERATOR_PATH.exists():
    DEFAULT_LOOT_GENERATOR_PATH = Path(r"F:\Obsidian\PF2e_Encounter_Generator")
LOOT_GENERATOR_PATH = Path(os.getenv("PF2E_LOOT_GENERATOR_PATH", DEFAULT_LOOT_GENERATOR_PATH))

BUDGETS = {
    "Trivial": (40, 10), "Low": (60, 20), "Moderate": (80, 20),
    "Severe": (120, 30), "Extreme": (160, 40),
}
XP_BY_DELTA = {-4: 10, -3: 15, -2: 20, -1: 30, 0: 40, 1: 60, 2: 80, 3: 120, 4: 160}
LAND_BIOMES = {"Arctic", "Desert", "Forest", "Hills", "Mountains", "Plains", "Ruins", "Swamp", "Underground", "Urban", "Volcanic"}
FRAMEWORKS = {
    "Automatic": None,
    "Boss and Lackeys (120 XP)": [(2, 1), (-4, 4)],
    "Boss and Lieutenant (120 XP)": [(2, 1), (0, 1)],
    "Elite Enemies (120 XP)": [(0, 3)],
    "Lieutenant and Lackeys (80 XP)": [(0, 1), (-4, 4)],
    "Mated Pair (80 XP)": [(0, 2)],
    "Troop (80 XP)": [(0, 1), (-2, 2)],
    "Mook Squad (60 XP)": [(-4, 6)],
}
TERRAIN_FEATURES = [
    "River", "Lake", "Cliff", "Ravine", "Crystal Growth", "Ancient Road",
    "Standing Stones", "Dense Canopy", "Cave Entrance", "Waterfall",
    "Sinkhole", "Giant Tree", "Dungeon Entrance",
]
CREATURE_ADJUSTMENTS = {"Standard": 0, "Elite": 1, "Weak": -1}
ADJUSTMENT_MODES = {
    "Standard Only": ("Standard",),
    "Allow Elite": ("Standard", "Elite"),
    "Allow Weak": ("Standard", "Weak"),
    "Allow Elite and Weak": ("Standard", "Elite", "Weak"),
}
ADJUSTMENT_SELECTION_FACTOR = {"Standard": 1.0, "Elite": 0.45, "Weak": 0.6}
TREASURE_PROFILES = {"None": 0, "Minor": 0.5, "Standard": 1.0, "Major": 1.5}
TOTAL_VALUE_BY_LEVEL = {
    1: 175, 2: 300, 3: 500, 4: 850, 5: 1350, 6: 2000, 7: 2900,
    8: 4000, 9: 5700, 10: 8000, 11: 11500, 12: 16500, 13: 25000,
    14: 36500, 15: 54500, 16: 82500, 17: 128000, 18: 208000,
    19: 355000, 20: 490000,
}


@dataclass(frozen=True)
class Creature:
    id: str
    name: str
    level: int
    size: str
    rarity: str
    source: str
    traits: str
    tactical_tags: str
    database_id: str
    base_name: str
    adjustment: str
    weight: int
    xp: int


def connection():
    return sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def protect_secret(secret):
    if os.name != "nt":
        raise RuntimeError("Encrypted API-key storage currently requires Windows.")
    raw = secret.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "PF2e Encounter Generator", None, None, None, 0, ctypes.byref(destination)):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(destination.pbData, destination.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(destination.pbData)


def unprotect_secret(encoded):
    raw = base64.b64decode(encoded)
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(destination)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(destination.pbData, destination.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(destination.pbData)


def load_settings():
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


def saved_api_key():
    encoded = load_settings().get("encrypted_api_key")
    if not encoded:
        return ""
    try:
        return unprotect_secret(encoded)
    except (OSError, ValueError):
        return ""


def options():
    with connection() as db:
        biomes = [r[0] for r in db.execute("SELECT DISTINCT Biome FROM biomes_creature ORDER BY Biome")]
        classes = [r[0] for r in db.execute('SELECT DISTINCT "Encounter Class" FROM encounterclass ORDER BY 1')]
        raw_traits = [r[0] for r in db.execute("SELECT Trait FROM creature_traits")]
    traits = sorted({x.strip() for value in raw_traits for x in value.replace("\u00a0", " ").split(",") if x.strip()})
    return biomes, classes, traits


def condition_options():
    with connection() as db:
        modifier_table = condition_table(db)
        if modifier_table:
            rows = db.execute(f'SELECT DISTINCT "Condition Type", Condition FROM "{modifier_table}" WHERE "Condition Type" <> "" AND Condition <> "" ORDER BY 1, 2')
            result = {}
            for condition_type, condition in rows:
                result.setdefault(condition_type, []).append(condition)
            return result, True
        if table_exists(db, "activity"):
            values = [r[0] for r in db.execute("SELECT DISTINCT Activity FROM activity WHERE Activity <> 'Any' ORDER BY Activity")]
            return {"Time of Day": values}, False
    return {}, False


def campaign_theme_records():
    with connection() as db:
        if not table_exists(db, "campaign_themes"):
            return []
        rows = db.execute('SELECT ID, Theme, Description FROM campaign_themes WHERE ID <> "" AND Theme <> "" ORDER BY Theme')
        return [{"id": row[0], "theme": row[1], "description": row[2] or ""} for row in rows]


def selected_campaign_theme(theme_id):
    if not theme_id:
        return None
    themes = {theme["id"]: theme for theme in campaign_theme_records()}
    if theme_id not in themes:
        raise ValueError("Select a valid campaign theme or choose No campaign theme.")
    return themes[theme_id]


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y", "active"}


def region_records():
    with connection() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT * FROM region ORDER BY Name').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if truthy(item.get("Active", 1)):
            result.append({
                "id": item["ID"], "name": item["Name"], "description": item.get("Description", ""),
                "level_min": int(item.get("Enounter Level Min", item.get("Encounter Level Min", -1))),
                "level_max": int(item.get("Encounter Level Max", 25)),
                "primary_biome_required": truthy(item.get("Primary Biome Required", 1)),
                "secondary_biome_allowed": truthy(item.get("Secondary Biome Allowed", 1)),
            })
    return result


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def condition_table(db):
    for name in ("condition_modifiers", "conditions_modifier"):
        if table_exists(db, name):
            return name
    return ""


def region_biome_map(all_biomes):
    records = region_records()
    fallback = {r["id"]: list(all_biomes) for r in records}
    fallback[UNDETERMINED_REGION] = list(all_biomes)
    with connection() as db:
        if not table_exists(db, "biomes_region"):
            return {"primary": fallback, "secondary": fallback}, False
        columns = [r[1] for r in db.execute('PRAGMA table_info("biomes_region")')]
        normalized = {c.lower().replace(" ", "").replace("_", ""): c for c in columns}
        biome_column = normalized.get("biome") or (normalized.get("name") if "primaryallowed" in normalized else None)
        primary_allowed = normalized.get("primaryallowed")
        secondary_allowed = normalized.get("secondaryallowed")
        region_columns = [c for c in columns if "region" in c.lower().replace(" ", "").replace("_", "")]
        if not region_columns and "ID" in columns:
            region_columns = ["ID"]
        if not biome_column or not region_columns:
            raise ValueError("biomes_region must contain a Biome or Name column and a region ID or region name column.")
        mapping = {"primary": {}, "secondary": {}}
        for record in records:
            where = " OR ".join(f'"{c}" IN (?, ?)' for c in region_columns)
            params = [value for _ in region_columns for value in (record["id"], record["name"])]
            select_columns = [biome_column] + ([primary_allowed] if primary_allowed else []) + ([secondary_allowed] if secondary_allowed else [])
            select_sql = ", ".join(f'"{c}"' for c in select_columns)
            rows = db.execute(f'SELECT DISTINCT {select_sql} FROM biomes_region WHERE {where} ORDER BY 1', params).fetchall()
            mapping["primary"][record["id"]] = [row[0] for row in rows if not primary_allowed or truthy(row[1])]
            secondary_index = 2 if primary_allowed and secondary_allowed else 1
            mapping["secondary"][record["id"]] = [row[0] for row in rows if not secondary_allowed or truthy(row[secondary_index])]
        mapping["primary"][UNDETERMINED_REGION] = list(all_biomes)
        mapping["secondary"][UNDETERMINED_REGION] = []
        return mapping, True


def selected_location_context(region_id, party_level, primary_biome, secondary_biome, all_biomes):
    records = {r["id"]: r for r in region_records()}
    mapping, mapped = region_biome_map(all_biomes)
    if secondary_biome and secondary_biome == primary_biome:
        raise ValueError("Primary and secondary biomes must be different.")
    if region_id == UNDETERMINED_REGION:
        return [], mapping, mapped
    if region_id not in records:
        raise ValueError("Select a valid region.")
    region = records[region_id]
    if not region["level_min"] <= party_level <= region["level_max"]:
        raise ValueError(f"{region['name']} supports encounter levels {region['level_min']}–{region['level_max']}, not level {party_level}.")
    if mapped and primary_biome not in mapping["primary"][region_id]:
        raise ValueError(f"{primary_biome} is not allowed as a primary biome in {region['name']}.")
    if secondary_biome:
        if not region["secondary_biome_allowed"]:
            raise ValueError(f"{region['name']} does not allow a secondary biome.")
        if mapped and secondary_biome not in mapping["secondary"][region_id]:
            raise ValueError(f"{secondary_biome} is not allowed as a secondary biome in {region['name']}.")
    return [region], mapping, mapped


def adjusted_budget(difficulty: str, players: int) -> int:
    base, adjustment = BUDGETS[difficulty]
    return base + (players - 4) * adjustment


def treasure_budget_gp(profile: str, difficulty: str, party_level: int, players: int) -> float:
    if profile not in TREASURE_PROFILES:
        raise ValueError("Select a valid treasure profile.")
    if profile == "None":
        return 0.0
    if party_level not in TOTAL_VALUE_BY_LEVEL:
        raise ValueError("Encounter treasure is available for party levels 1 through 20.")
    base_xp = BUDGETS[difficulty][0]
    return round(TOTAL_VALUE_BY_LEVEL[party_level] * base_xp / 1000 * TREASURE_PROFILES[profile] * players / 4, 2)


def coins_from_gp(value: float):
    copper = round(value * 100)
    pp, copper = divmod(copper, 1000)
    gp, copper = divmod(copper, 100)
    sp, cp = divmod(copper, 10)
    return {"pp": pp, "gp": gp, "sp": sp, "cp": cp}


def generate_treasure(profile: str, difficulty: str, party_level: int, players: int):
    budget_gp = treasure_budget_gp(profile, difficulty, party_level, players)
    if profile == "None":
        return {"profile": profile, "budget_gp": 0, "coins": coins_from_gp(0), "items": []}
    python = LOOT_GENERATOR_PATH / ".venv" / "Scripts" / "python.exe"
    bridge = ROOT / "treasure_bridge.py"
    if not python.exists():
        raise RuntimeError(f"Treasure generator Python environment was not found at {python}.")
    if not bridge.exists():
        raise RuntimeError(f"Treasure integration file was not found at {bridge}.")
    request = {
        "item_generator_root": str(LOOT_GENERATOR_PATH), "profile": profile,
        "difficulty": difficulty, "party_size": players, "party_level": party_level,
        "seed": random.SystemRandom().randrange(1, 2 ** 31),
    }
    completed = subprocess.run(
        [str(python), str(bridge)], input=json.dumps(request), text=True,
        capture_output=True, cwd=LOOT_GENERATOR_PATH, timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        try:
            error = json.loads(completed.stdout).get("error")
        except (json.JSONDecodeError, AttributeError):
            error = completed.stderr.strip() or "Unknown treasure generator error"
        raise RuntimeError(error)
    result = json.loads(completed.stdout)
    # The bridge and this process use the same formula. Keep this check explicit so
    # a future external-generator change cannot silently alter encounter value.
    if abs(float(result.get("budget_gp", -1)) - budget_gp) > 0.01:
        raise RuntimeError("The treasure generator returned a value that does not match the encounter budget.")
    return result


def creature_traits(creature: Creature):
    return {x.strip() for x in creature.traits.replace("\u00a0", " ").split(",") if x.strip()}


def candidates(party_level: int, selected_biomes, conditions, encounter_class: str, selected_traits=(), allowed_adjustments=("Standard",)):
    acceptable_biomes = list(dict.fromkeys(selected_biomes))
    if any(biome in LAND_BIOMES for biome in selected_biomes):
        acceptable_biomes.append("Any Land")
    marks = ",".join("?" for _ in acceptable_biomes)
    sql = f"""
        SELECT c.ID, c.Name, c.Level, c.Size, c.Rarity, c.Source, t.Trait, tc."Tactical Tags",
               MAX(b.Weight) AS biome_weight
        FROM creatures c
        JOIN creature_traits t ON t.ID=c.ID
        JOIN tactical_creature tc ON tc.ID=c.ID
        JOIN encounterclass e ON e.ID=c.ID
        JOIN biomes_creature b ON b.ID=c.ID AND b.Biome IN ({marks})
        WHERE e."Encounter Class"=? AND c.Level BETWEEN ? AND ?
        GROUP BY c.ID
    """
    params = acceptable_biomes + [encounter_class, party_level - 5, party_level + 5]
    with connection() as db:
        rows = db.execute(sql, params).fetchall()
        modifiers = {}
        selected_conditions = {k: v for k, v in conditions.items() if v}
        modifier_table = condition_table(db)
        if selected_conditions and modifier_table:
            where = " OR ".join('(\"Condition Type\"=? AND Condition=?)' for _ in selected_conditions)
            condition_params = [value for pair in selected_conditions.items() for value in pair]
            modifier_rows = db.execute(f'SELECT ID, "Condition Type", Weight FROM "{modifier_table}" WHERE {where}', condition_params)
            for creature_id, condition_type, weight in modifier_rows:
                modifiers.setdefault(creature_id, {})[condition_type] = max(float(weight), modifiers.get(creature_id, {}).get(condition_type, 0))
        elif selected_conditions.get("Time of Day") and table_exists(db, "activity"):
            modifier_rows = db.execute("SELECT ID, Weight FROM activity WHERE Activity=?", (selected_conditions["Time of Day"],))
            for creature_id, weight in modifier_rows:
                modifiers.setdefault(creature_id, {})["Time of Day"] = float(weight)
    result = []
    for r in rows:
        matched = list(modifiers.get(r[0], {}).values())
        condition_factor = math.prod(weight / 3.0 for weight in matched) ** (1 / len(matched)) if matched else 1.0
        combined_weight = max(1, min(25, round(r[8] * condition_factor)))
        for adjustment in allowed_adjustments:
            if adjustment not in CREATURE_ADJUSTMENTS:
                continue
            effective_level = r[2] + CREATURE_ADJUSTMENTS[adjustment]
            delta = effective_level - party_level
            if delta not in XP_BY_DELTA:
                continue
            display_name = r[1] if adjustment == "Standard" else f"{adjustment} {r[1]}"
            variant_id = f"{r[0]}::{adjustment.lower()}"
            variant_weight = max(1, round(combined_weight * ADJUSTMENT_SELECTION_FACTOR[adjustment]))
            result.append(Creature(variant_id, display_name, effective_level, *r[3:8], r[0], r[1], adjustment, variant_weight, XP_BY_DELTA[delta]))
    wanted = set(selected_traits)
    return [c for c in result if not wanted or creature_traits(c) & wanted]


def covers_traits(party, selected_traits):
    present = set().union(*(creature_traits(c) for c in party)) if party else set()
    return set(selected_traits) <= present


def valid_adjustment_composition(party):
    elite = [c for c in party if c.adjustment == "Elite"]
    if len(elite) > 1:
        return False
    if len(party) > 1 and not any(c.adjustment == "Standard" for c in party):
        return False
    if elite and elite[0].level < max(c.level for c in party):
        return False
    return True


def choose_framework(pool, framework: str, selected_traits, rng: random.Random):
    slots = FRAMEWORKS[framework]
    for _ in range(1000):
        party = []
        for delta, quantity in slots:
            matching = [c for c in pool if c.xp == XP_BY_DELTA[delta]]
            if not matching:
                raise ValueError(f"{framework} needs a party-level {delta:+d} creature, but none match the selected filters.")
            weighted = [c for c in matching for _ in range(min(c.weight, 25))]
            party.extend(rng.choice(weighted) for _ in range(quantity))
        if covers_traits(party, selected_traits) and valid_adjustment_composition(party):
            return party, sum(c.xp for c in party)
    raise ValueError("No encounter can satisfy that framework together with the selected traits and creature-adjustment rules. Try another framework or adjustment mode.")


def choose_encounter(pool, budget: int, rng: random.Random, selected_traits=()):
    if not pool:
        raise ValueError("No creatures match all selected filters and the allowed level range.")
    # Randomized bounded search. Exact-budget encounters win; otherwise select the closest result below budget.
    best = None
    best_score = (-1, -999)
    weighted = [c for c in pool for _ in range(min(c.weight, 25))]
    for _ in range(5000):
        party = []
        total = 0
        distinct = set()
        for _slot in range(10):
            valid = [c for c in weighted if total + c.xp <= budget and (c.id in distinct or len(distinct) < 3) and (c.adjustment != "Elite" or not any(x.adjustment == "Elite" for x in party))]
            if not valid:
                break
            c = rng.choice(valid)
            party.append(c)
            distinct.add(c.id)
            total += c.xp
            if total == budget or (party and rng.random() < 0.18):
                break
        if not party:
            continue
        if not covers_traits(party, selected_traits):
            continue
        if not valid_adjustment_composition(party):
            continue
        score = (total, -len(distinct))
        if score > best_score:
            best, best_score = party, score
        if total == budget and 1 <= len(distinct) <= 3:
            best = party
            break
    if not best:
        smallest = min(pool, key=lambda c: c.xp)
        raise ValueError(f"The {budget} XP budget is too small for matching creatures (minimum is {smallest.xp} XP).")
    return best, sum(c.xp for c in best)


def ai_details(context: dict, key="") -> dict | None:
    key = key or os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "title": {"type": "string"}, "read_aloud": {"type": "string"},
            "description": {"type": "string"}, "tactics": {"type": "string"},
            "area_features": {"type": "array", "items": {"type": "string"}},
            "investigation": {"type": "array", "items": {"type": "string"}},
            "treasure": {"type": "string"},
        },
        "required": ["title", "read_aloud", "description", "tactics", "area_features", "investigation", "treasure"],
    }
    prompt = """Create concise Pathfinder 2e Remaster encounter dressing for the supplied rules-selected creatures.
Do not change creature names, quantities, levels, XP, or difficulty. Avoid inventing creature statistics.
Elite and Weak are rules-selected creature adjustments already reflected in effective level and XP. Preserve every adjustment exactly and describe tactics without inventing adjusted statistics.
Treat the selected campaign theme, region, biomes, conditions, and terrain features as required context. When a campaign theme is supplied, use its name and description as the encounter's central situation or motivation. Incorporate every selected terrain feature and condition into the area description, encounter setup, or tactics without changing the rules-selected creatures.
Each creature includes tactical tags answering how it fights. Make the tactics actionable and synthesize how Brutes, Skirmishers, Ambushers, Scouts, or other tagged roles cooperate, use terrain, choose targets, reposition, and retreat. Do not merely repeat the tag names.
Write read-aloud text without revealing hidden threats. Investigation entries must include a skill and DC; use level-appropriate DCs. If structured treasure is supplied, describe where it is found and why it belongs in the encounter, but do not change its item names, levels, quantities, prices, coins, profile, or total budget. If its profile is None, treasure may say none. Return only the requested schema."""
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5.5"),
        "instructions": prompt,
        "input": json.dumps(context),
        "text": {"format": {"type": "json_schema", "name": "encounter_details", "strict": True, "schema": schema}},
    }
    request_data = json.dumps(body).encode()
    payload = None
    for attempt in range(3):
        req = urllib.request.Request("https://api.openai.com/v1/responses", data=request_data, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                error = json.loads(detail).get("error", {})
                api_message = error.get("message", detail)
                api_code = error.get("code", "")
            except json.JSONDecodeError:
                api_message, api_code = detail, ""
            if exc.code == 429 and attempt < 2 and api_code != "insufficient_quota":
                delay = min(float(exc.headers.get("retry-after", 2 ** attempt)), 10)
                time.sleep(delay)
                continue
            if exc.code == 429:
                raise RuntimeError("OpenAI could not add narrative details because the API account is rate-limited or has no available quota. The rules-valid encounter was still generated. Check API billing/usage, or try again later.") from exc
            raise RuntimeError(f"OpenAI could not add narrative details: {api_message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI could not be reached: {exc.reason}") from exc
    try:
        text = payload.get("output_text")
        if not text:
            for item in payload.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text = content.get("text")
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        raise RuntimeError("OpenAI returned details in an unexpected format; the rules-valid encounter was still generated.") from exc


def format_gp(value):
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


def treasure_markdown(treasure, narrative):
    if not treasure or treasure.get("profile") == "None":
        return f"> {narrative.replace(chr(10), chr(10) + '> ')}"
    lines = [
        f"> **{treasure['profile']} treasure — {format_gp(treasure['budget_gp'])} gp value**",
        f"> {narrative.replace(chr(10), chr(10) + '> ')}",
    ]
    coins = treasure.get("coins", {})
    coin_text = ", ".join(f"{coins.get(kind, 0):,} {kind}" for kind in ("pp", "gp", "sp", "cp") if coins.get(kind, 0)) or "none"
    lines.append(f"> - [ ] coins:: {coin_text}")
    for item in treasure.get("items", []):
        quantity = item.get("quantity", 1)
        count = f"{quantity}× " if quantity != 1 else ""
        target = item.get("link_target") or item["name"]
        display = item["name"]
        link = f"[[{target}]]" if target == display else f"[[{target}|{display}]]"
        lines.append(f"> - [ ] {item['kind']}::{count}{link} [ilvl::{item['level']}] [gp::{format_gp(item['price_gp'])}]")
    return "\n".join(lines)


def markdown(form, party, total_xp, details, treasure=None):
    counts = Counter(c.id for c in party)
    unique = {c.id: c for c in party}
    names = ", ".join(f"{counts[i]}× {unique[i].name}" for i in counts)
    selected_features = form.get("terrain_features", [])
    feature_placeholders = [f"{feature}: Describe how this feature changes movement, visibility, cover, or tactics." for feature in selected_features]
    biome_label = form["primary_biome"] + (f" / {form['secondary_biome']}" if form.get("secondary_biome") else "")
    tactical_roles = ", ".join(sorted({tag.strip() for creature in party for tag in creature.tactical_tags.split(",") if tag.strip()}))
    d = details or {
        "title": f"{biome_label} {form['difficulty']} Encounter",
        "read_aloud": "Add sensory details and an initial impression of the encounter here.",
        "description": f"A {form['encounter_class'].lower()} encounter featuring {names}.",
        "tactics": f"Develop coordinated tactics for these tactical roles: {tactical_roles}. Account for the selected biomes, terrain, time, weather, and other conditions.",
        "area_features": feature_placeholders or ["Add lighting, terrain, dimensions, cover, hazards, or interactive features."],
        "investigation": ["Perception DC XX: Notice an important clue or concealed feature."],
        "treasure": "None specified.",
    }
    def creature_link(creature):
        return f"[[{creature.base_name}]]" if creature.adjustment == "Standard" else f"[[{creature.base_name}|{creature.name}]]"
    foe_lines = "\n".join(f" - {counts[i]}: {creature_link(unique[i])} ({unique[i].adjustment.lower()}, level {unique[i].level}; {unique[i].xp} XP each)" for i in counts)
    encounter_lines = "\n".join(f"  - {counts[i]}: {unique[i].name} # {counts[i] * unique[i].xp} XP total" for i in counts)
    features = "\n".join(f"- {x}" for x in d["area_features"])
    investigation = "\n".join(f"- {x}" for x in d["investigation"])
    treasure_lines = treasure_markdown(treasure, d["treasure"])
    return f"""### {d['title']}
**Encounter Difficulty: {form['difficulty']}**  
- [ ] {form['difficulty']} ({total_xp} XP) Level {form['party_level']}

![[ImagePlaceholder.png]]

> [!note]+ Read Aloud
> {d['read_aloud'].replace(chr(10), chr(10) + '> ')}

**Description** {d['description']}

At the start of the encounter, place the following foes on the map:
{foe_lines}

```encounter
name: {d['title']}
party: PartyName
creatures:
{encounter_lines}
```

#### Tactics
{d['tactics']}

#### Features of the Area
{features}

#### Investigation
{investigation}

> [!tip]+ Treasure
{treasure_lines}
"""


def page(message="", output="", values=None):
    biomes, classes, traits = options()
    available_conditions, has_condition_table = condition_options()
    region_rows = region_records()
    if not region_rows:
        raise ValueError("The region table has no active regions.")
    biome_map, has_biome_map = region_biome_map(biomes)
    settings = load_settings()
    v = values or {"players": "4", "party_level": "1", "difficulty": "Moderate", "primary_biome": "Forest" if "Forest" in biomes else biomes[0], "secondary_biome": "", "conditions": {}, "encounter_class": "Wild", "framework": "Automatic", "campaign_theme_id": "", "treasure_profile": "None", "traits": [], "adjustment_mode": "Standard Only", "region": UNDETERMINED_REGION, "terrain_features": [], "use_openai": bool(settings.get("use_openai", False))}
    def choices(items, key):
        return "".join(f'<option{(" selected" if str(x)==str(v.get(key)) else "")}>{html.escape(str(x))}</option>' for x in items)
    def biome_choices(key, optional=False):
        blank = '<option value="">None</option>' if optional else ""
        return blank + "".join(f'<option{(" selected" if b == v.get(key) else "")}>{html.escape(b)}</option>' for b in biomes)
    region_choices = f'<option value="{UNDETERMINED_REGION}"{(" selected" if v.get("region") == UNDETERMINED_REGION else "")}>Undetermined Region</option>' + "".join(f'<option value="{html.escape(r["id"])}" title="{html.escape(r["description"])}"{(" selected" if r["id"] == v.get("region") else "")}>{html.escape(r["name"])}</option>' for r in region_rows)
    region_json = json.dumps(biome_map).replace("</", "<\\/")
    region_rules = {r["id"]: {"secondary_allowed": r["secondary_biome_allowed"]} for r in region_rows}
    region_rules[UNDETERMINED_REGION] = {"secondary_allowed": True}
    rules_json = json.dumps(region_rules).replace("</", "<\\/")
    map_notice = "" if has_biome_map else '<div class=msg>biomes_region is not present in the database. Region selectors work, but all creature biomes remain available until that table is added.</div>'
    condition_notice = "" if has_condition_table else '<div class=msg>condition_modifiers is not present yet. Time of Day is temporarily loaded from the legacy activity table.</div>'
    def condition_control(condition_type, condition_values):
        selected = v.get("conditions", {}).get(condition_type)
        entries = "".join(f'<option{(" selected" if value == selected else "")}>{html.escape(value)}</option>' for value in condition_values)
        return f'<label>{html.escape(condition_type)}<select name="condition:{html.escape(condition_type)}"><option value="">Not specified</option>{entries}</select></label>'
    condition_controls = "".join(condition_control(condition_type, condition_values) for condition_type, condition_values in available_conditions.items())
    theme_groups = {}
    for theme in campaign_theme_records():
        category = theme["theme"].split(":", 1)[0] if ":" in theme["theme"] else "Other"
        theme_groups.setdefault(category, []).append(theme)
    theme_options = '<option value="">No campaign theme</option>'
    for category, themes in theme_groups.items():
        entries = "".join(f'<option value="{html.escape(theme["id"])}" title="{html.escape(theme["description"])}"{(" selected" if theme["id"] == v.get("campaign_theme_id") else "")}>{html.escape(theme["theme"])}</option>' for theme in themes)
        theme_options += f'<optgroup label="{html.escape(category)}">{entries}</optgroup>'
    treasure_values_json = json.dumps(TOTAL_VALUE_BY_LEVEL)
    treasure_factors_json = json.dumps(TREASURE_PROFILES)
    threat_xp_json = json.dumps({name: values[0] for name, values in BUDGETS.items()})
    return f"""<!doctype html><html><head><meta charset=utf-8><title>PF2e Encounter Generator</title>
<style>
:root{{--ink:#1c1917;--charcoal:#171516;--panel:#242021;--panel-deep:#1d1a1b;--parchment:#f4ecd9;--parchment-dark:#dfd0b1;--crimson:#7a1118;--crimson-bright:#a01d25;--gold:#c7a34d;--gold-soft:#e0c77c;--muted:#c9bdab}}
*{{box-sizing:border-box}}body{{font:15px/1.45 "Segoe UI",Arial,sans-serif;max-width:1120px;margin:0 auto;padding:2.25rem 1.25rem 3rem;background:radial-gradient(circle at 50% -15%,#3a3030 0,#211d1e 35rem,var(--charcoal) 70rem);color:#f7f0e4;min-height:100vh}}h1,h2,legend,label{{font-family:Georgia,"Times New Roman",serif}}h1{{margin:0;color:var(--parchment);font-size:clamp(2rem,4vw,3.15rem);font-variant:small-caps;letter-spacing:.055em;line-height:1.05;text-align:center;text-shadow:0 2px 0 #000,0 0 22px rgba(199,163,77,.2)}}h1::after{{content:"";display:block;width:min(34rem,80%);height:3px;margin:.8rem auto 1rem;background:linear-gradient(90deg,transparent,var(--gold),var(--crimson-bright),var(--gold),transparent)}}h1+p{{margin:0 auto 1.5rem;text-align:center;color:var(--muted);font-style:italic}}form{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem 1.1rem;background:linear-gradient(145deg,var(--panel),var(--panel-deep));padding:1.5rem;border:1px solid #705d32;border-top:4px solid var(--crimson);border-radius:4px;box-shadow:0 18px 45px rgba(0,0,0,.38),inset 0 1px rgba(255,255,255,.035)}}label{{display:grid;align-content:start;gap:.38rem;color:var(--parchment);font-size:1rem;letter-spacing:.012em}}input,select,button,textarea{{font:15px/1.3 "Segoe UI",Arial,sans-serif;padding:.72rem .8rem;border:1px solid #8b7d68;border-radius:3px}}input,select,textarea{{background:linear-gradient(#fffdf7,var(--parchment));color:var(--ink);box-shadow:inset 0 1px 2px rgba(0,0,0,.08)}}input:focus,select:focus,textarea:focus{{outline:2px solid var(--gold-soft);outline-offset:1px;border-color:var(--crimson)}}select[multiple]{{min-height:11rem;padding:.45rem}}option:checked{{background:var(--crimson);color:white}}.wide{{grid-column:span 2}}.pickers{{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;padding-top:.15rem}}fieldset.wide{{display:grid;grid-template-columns:1fr 1fr;gap:.8rem 1rem;margin:0;padding:1rem;border:1px solid #705d32;border-radius:3px;background:rgba(0,0,0,.12)}}legend{{padding:0 .45rem;color:var(--gold-soft);font-weight:700;font-variant:small-caps;letter-spacing:.04em}}.check{{display:flex;align-items:center;gap:.55rem;font-family:"Segoe UI",Arial,sans-serif;font-size:.92rem}}.check input{{width:auto;accent-color:var(--crimson)}}button{{align-self:end;background:linear-gradient(#981e27,#681016);color:#fff8e8;border:1px solid var(--gold);font-family:Georgia,"Times New Roman",serif;font-size:1.05rem;font-weight:700;font-variant:small-caps;letter-spacing:.045em;cursor:pointer;box-shadow:0 3px 8px rgba(0,0,0,.3)}}button:hover{{background:linear-gradient(#ad2932,#7b121a);border-color:var(--gold-soft)}}textarea{{width:100%;min-height:560px;margin-top:.5rem}}.msg{{padding:1rem 1.1rem;background:#3b2223;border-left:4px solid var(--gold);border-radius:3px;margin:1rem 0;color:#fff2df}}small{{font-family:"Segoe UI",Arial,sans-serif;font-size:.8rem;line-height:1.35;color:var(--muted)}}body>h2{{color:var(--gold-soft);font-variant:small-caps;letter-spacing:.035em}}@media(max-width:760px){{body{{padding:1.25rem .75rem}}form{{grid-template-columns:1fr;padding:1rem}}.wide,.pickers{{grid-column:auto}}.pickers,fieldset.wide{{grid-template-columns:1fr}}button{{width:100%}}}}
</style></head><body>
<h1>PF2e Remaster Encounter Generator</h1><p>Rules-first creature selection from your SQLite database, with optional OpenAI encounter dressing.</p>
{f'<div class=msg>{html.escape(message)}</div>' if message else ''}
{map_notice}
{condition_notice}
<form method=post><label>Players<input id=players name=players type=number min=1 max=12 value="{v['players']}" required></label>
<label>Party level<input id=party_level name=party_level type=number min=0 max=25 value="{v['party_level']}" required></label>
<label>Difficulty<select id=difficulty name=difficulty>{choices(BUDGETS, 'difficulty')}</select></label>
<label>Region<select id=region name=region required>{region_choices}</select><small>Choose Undetermined Region to ignore regional biome restrictions.</small></label>
<label>Primary biome<select id=primary_biome name=primary_biome required>{biome_choices('primary_biome')}</select></label>
<label>Secondary biome<select id=secondary_biome name=secondary_biome>{biome_choices('secondary_biome', True)}</select><small>Optional transition or boundary biome.</small></label>
{condition_controls}
<label>Encounter class<select name=encounter_class>{choices(classes, 'encounter_class')}</select></label>
<label class=wide>Encounter structure<select name=framework>{choices(FRAMEWORKS, 'framework')}</select><small>Named structures use their fixed four-player composition and may differ from the adjusted party-size budget.</small></label>
<label>Creature adjustments<select name=adjustment_mode>{choices(ADJUSTMENT_MODES, 'adjustment_mode')}</select><small>Standard creatures always remain available; Elite and Weak add occasional role-appropriate variants.</small></label>
<label class=wide>Campaign theme<select name=campaign_theme_id>{theme_options}</select><small>Optional. The selected theme guides OpenAI's encounter situation, motivation, and narrative details without changing creature selection.</small></label>
<label>Treasure<select id=treasure_profile name=treasure_profile>{choices(TREASURE_PROFILES, 'treasure_profile')}</select><small id=treasure_preview>None: no generated treasure parcel.</small></label>
<div class=pickers><label>Terrain features<select id=terrain_features name=terrain_features multiple>{''.join(f'<option value="{html.escape(t)}"{(" selected" if t in v.get("terrain_features", []) else "")}>{html.escape(t)}</option>' for t in TERRAIN_FEATURES)}</select><small>Ctrl-click to select multiple. Selected features are required encounter context.</small></label>
<label>Required traits<select name=traits multiple>{''.join(f'<option value="{html.escape(t)}"{(" selected" if t in v.get("traits", []) else "")}>{html.escape(t)}</option>' for t in traits)}</select><small>Ctrl-click to select multiple. Every selected trait must appear somewhere in the encounter.</small></label></div>
<fieldset class=wide><legend>OpenAI encounter details</legend><label class=check><input type=checkbox name=use_openai value=yes{(" checked" if v.get("use_openai") else "")}> Use OpenAI when generating this encounter</label><label>API key<input type=password name=api_key autocomplete=off placeholder="{'Saved key available—leave blank to reuse' if settings.get('encrypted_api_key') else 'Enter an OpenAI API key'}"></label><label class=check><input type=checkbox name=save_api_key value=yes> Save a newly entered key securely</label><label class=check><input type=checkbox name=clear_saved_key value=yes> Forget the saved key</label><small class=wide>The saved key is encrypted for your current Windows account and is not stored in the campaign database.</small></fieldset>
<button type=submit name=action value=generate>Generate encounter</button></form>
<p><small>Set OPENAI_API_KEY before starting to generate narrative details. Without it, the tool still produces a rules-valid Markdown skeleton.</small></p>
{f'<h2>Obsidian Markdown</h2><textarea id=o>{html.escape(output)}</textarea><button onclick="navigator.clipboard.writeText(document.getElementById(\'o\').value)">Copy Markdown</button>' if output else ''}
<script>const regionBiomes={region_json},regionRules={rules_json},undetermined='{UNDETERMINED_REGION}';const region=document.getElementById('region'),primary=document.getElementById('primary_biome'),secondary=document.getElementById('secondary_biome');function applyLocation(){{const primaryAllowed=new Set(regionBiomes.primary[region.value]||[]),secondaryAllowed=new Set(regionBiomes.secondary[region.value]||[]),canUseSecondary=regionRules[region.value].secondary_allowed;for(const o of primary.options){{o.disabled=!primaryAllowed.has(o.value);o.hidden=o.disabled;if(o.disabled&&o.selected)o.selected=false}}secondary.disabled=!canUseSecondary;if(!canUseSecondary)secondary.value='';for(const o of secondary.options){{if(!o.value)continue;o.disabled=!canUseSecondary||!secondaryAllowed.has(o.value)||o.value===primary.value;o.hidden=o.disabled;if(o.disabled&&o.selected)o.selected=false}}}}region.addEventListener('change',applyLocation);primary.addEventListener('change',applyLocation);applyLocation();const treasureValues={treasure_values_json},treasureFactors={treasure_factors_json},threatXP={threat_xp_json},treasureProfile=document.getElementById('treasure_profile'),players=document.getElementById('players'),partyLevel=document.getElementById('party_level'),difficulty=document.getElementById('difficulty'),treasurePreview=document.getElementById('treasure_preview');function updateTreasurePreview(){{if(treasureProfile.value==='None'){{treasurePreview.textContent='None: no generated treasure parcel.';return}}const total=treasureValues[partyLevel.value];if(!total){{treasurePreview.textContent='Treasure values are available for party levels 1 through 20.';return}}const gp=total*threatXP[difficulty.value]/1000*treasureFactors[treasureProfile.value]*Number(players.value)/4;treasurePreview.textContent=`Estimated parcel value: ${{gp.toLocaleString(undefined,{{maximumFractionDigits:2}})}} gp. Items and coins are selected by the encounter loot generator.`}}for(const control of [treasureProfile,players,partyLevel,difficulty])control.addEventListener('change',updateTreasurePreview);updateTreasurePreview();</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond(page())

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = parse_qs(self.rfile.read(length).decode())
            entered_api_key = raw.get("api_key", [""])[0].strip()
            use_openai = raw.get("use_openai", [""])[0] == "yes"
            save_api_key_requested = raw.get("save_api_key", [""])[0] == "yes"
            clear_saved_key = raw.get("clear_saved_key", [""])[0] == "yes"
            conditions = {k.split(":", 1)[1]: x[0] for k, x in raw.items() if k.startswith("condition:") and x[0]}
            form = {k: x[0] for k, x in raw.items() if k not in {"api_key", "save_api_key", "clear_saved_key", "use_openai"} and not k.startswith("condition:")}
            selected_traits = raw.get("traits", [])
            adjustment_mode = raw.get("adjustment_mode", ["Standard Only"])[0]
            if adjustment_mode not in ADJUSTMENT_MODES:
                raise ValueError("Select a valid creature-adjustment mode.")
            allowed_adjustments = ADJUSTMENT_MODES[adjustment_mode]
            selected_features = raw.get("terrain_features", [])
            form["traits"] = selected_traits
            form["adjustment_mode"] = adjustment_mode
            form["terrain_features"] = selected_features
            form["conditions"] = conditions
            form["use_openai"] = use_openai
            form["players"] = int(form["players"])
            form["party_level"] = int(form["party_level"])
            treasure_profile = form.get("treasure_profile", "None")
            if treasure_profile not in TREASURE_PROFILES:
                raise ValueError("Select a valid treasure profile.")
            theme_context = selected_campaign_theme(form.get("campaign_theme_id", ""))
            settings = load_settings()
            if clear_saved_key:
                settings.pop("encrypted_api_key", None)
            if entered_api_key and save_api_key_requested:
                settings["encrypted_api_key"] = protect_secret(entered_api_key)
            settings["use_openai"] = use_openai
            save_settings(settings)
            api_key = entered_api_key or saved_api_key() or os.getenv("OPENAI_API_KEY", "")
            budget = adjusted_budget(form["difficulty"], form["players"])
            if budget <= 0:
                raise ValueError("The adjusted XP budget must be positive; increase party size or difficulty.")
            all_biomes = options()[0]
            region_context, _, has_biome_map = selected_location_context(form["region"], form["party_level"], form["primary_biome"], form.get("secondary_biome", ""), all_biomes)
            selected_biomes = [form["primary_biome"]] + ([form["secondary_biome"]] if form.get("secondary_biome") else [])
            pool = candidates(form["party_level"], selected_biomes, conditions, form["encounter_class"], selected_traits, allowed_adjustments)
            rng = random.SystemRandom()
            if form["framework"] == "Automatic":
                party, actual = choose_encounter(pool, budget, rng, selected_traits)
            else:
                party, actual = choose_framework(pool, form["framework"], selected_traits, rng)
            summary = [{"name": c.name, "base_name": c.base_name, "adjustment": c.adjustment, "effective_level": c.level, "xp_each": c.xp, "quantity": sum(x.id == c.id for x in party), "traits": c.traits, "tactical_tags": c.tactical_tags} for c in {x.id: x for x in party}.values()]
            treasure_warning = ""
            try:
                treasure = generate_treasure(treasure_profile, form["difficulty"], form["party_level"], form["players"])
            except RuntimeError as exc:
                fallback_budget = treasure_budget_gp(treasure_profile, form["difficulty"], form["party_level"], form["players"])
                treasure = {"profile": treasure_profile, "budget_gp": fallback_budget, "coins": coins_from_gp(fallback_budget), "items": []}
                treasure_warning = f" The encounter loot generator could not be used ({exc}); the same value was returned as coins instead."
            api_warning = ""
            details = None
            if use_openai and not api_key:
                api_warning = " OpenAI details were skipped because no API key is available. Enter a key or save one for future runs."
            elif use_openai:
                try:
                    details = ai_details({"inputs": form, "campaign_theme": theme_context, "regions": region_context, "target_xp": budget, "actual_xp": actual, "creatures": summary, "structured_treasure": treasure}, api_key)
                except RuntimeError as exc:
                    api_warning = f" {exc}"
            if use_openai and details is None and not api_warning:
                api_warning = " OpenAI did not return narrative details; the rules-valid encounter was still generated."
            note = markdown(form, party, actual, details, treasure)
            mapping_warning = " biomes_region is missing, so biome availability was not region-restricted." if not has_biome_map else ""
            msg = f"Generated {actual} XP against a {budget} XP adjusted budget from {len(pool)} matching creatures.{mapping_warning}{treasure_warning}{api_warning}"
            values = {k: (v if isinstance(v, (list, bool, dict)) else str(v)) for k, v in form.items()}
            self.respond(page(msg, note, values))
        except Exception as exc:
            self.respond(page(str(exc)), 400)

    def log_message(self, *_):
        pass

    def respond(self, body, status=200):
        data = body.encode()
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


if __name__ == "__main__":
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    url = f"http://127.0.0.1:{PORT}"
    print(f"PF2e Encounter Generator running at {url} (Ctrl+C to stop)")
    if os.getenv("PF2E_NO_BROWSER") != "1":
        Timer(0.8, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
