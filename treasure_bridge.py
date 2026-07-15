from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

TOTAL_VALUE_BY_LEVEL = {
    1: 175, 2: 300, 3: 500, 4: 850, 5: 1350, 6: 2000, 7: 2900,
    8: 4000, 9: 5700, 10: 8000, 11: 11500, 12: 16500, 13: 25000,
    14: 36500, 15: 54500, 16: 82500, 17: 128000, 18: 208000,
    19: 355000, 20: 490000,
}
THREAT_XP = {"Trivial": 40, "Low": 60, "Moderate": 80, "Severe": 120, "Extreme": 160}
PROFILE_FACTOR = {"Minor": 0.5, "Standard": 1.0, "Major": 1.5}


def coins_from_copper(copper: int):
    pp, copper = divmod(copper, 1000)
    gp, copper = divmod(copper, 100)
    sp, cp = divmod(copper, 10)
    return {"pp": pp, "gp": gp, "sp": sp, "cp": cp}


def derived_treasure_table(profile: str):
    factor = PROFILE_FACTOR[profile]
    return {
        level: {difficulty: round(total * xp / 1000.0 * factor, 2) for difficulty, xp in THREAT_XP.items()}
        for level, total in TOTAL_VALUE_BY_LEVEL.items()
    }


def main():
    request = json.load(sys.stdin)
    root = Path(request["item_generator_root"]).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["LOOTGEN_DB_PATH"] = str(root / "data" / "PF2e_Treasure_Generator_Backend.db")

    noise = io.StringIO()
    with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
        from services.db import connect_from_csv_or_db
        from services.loot_logic import GeneratorConfig, generate_loot

        cfg = GeneratorConfig(
            pool_size="Medium",
            difficulty=request["difficulty"],
            party_size=int(request["party_size"]),
            encounter_level=int(request["party_level"]),
            coin_pct=40,
            seed=request.get("seed"),
            treasure_value_table=derived_treasure_table(request["profile"]),
        )
        connection = connect_from_csv_or_db()
        try:
            result = generate_loot(connection, cfg)
        finally:
            connection.close()

    items = []
    consumable_buckets = {"consumables", "scrolls"}
    for bucket, bucket_items in result.get("items_by_category", {}).items():
        for item in bucket_items:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            target = str(item.get("name_base") or item.get("aon_target") or name).strip()
            items.append({
                "name": name,
                "link_target": target,
                "level": int(item.get("level") or 0),
                "rarity": str(item.get("rarity") or "Common").title(),
                "quantity": int(item.get("qty") or 1),
                "price_gp": round(float(item.get("price_est_gp") or 0), 2),
                "kind": "consumable" if bucket in consumable_buckets else "permanent",
                "category": bucket,
                "flavor": str(item.get("flavor") or ""),
            })

    # The source generator can decorate an item after its initial budget allocation
    # (for example by adding a rune), making its displayed price exceed the budget.
    # Reconcile against displayed prices so the final parcel always honors the value
    # selected by this encounter generator.
    budget_gp = float(result.get("total_value_gp") or 0)
    remaining_cp = round(budget_gp * 100)
    reconciled = []
    for item in items:
        unit_cp = round(item["price_gp"] * 100)
        if unit_cp <= 0:
            continue
        quantity = min(item["quantity"], remaining_cp // unit_cp)
        if quantity:
            item["quantity"] = quantity
            reconciled.append(item)
            remaining_cp -= quantity * unit_cp

    response = {
        "profile": request["profile"],
        "budget_gp": budget_gp,
        "coins": coins_from_copper(remaining_cp),
        "items": reconciled,
        "summary": f"{len(reconciled)} item selection(s) plus the remaining value in coins.",
        "budget_details": result.get("config_used", {}),
    }
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1)
