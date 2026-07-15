# PF2e Remaster Encounter Generator

This local tool selects a rules-valid encounter from `creature_db.db` and formats it for Obsidian. It does not modify the database.

## Start

Double-click **Start Random Encounter Generator.bat**. Your browser opens to `http://127.0.0.1:8765`.

The generator needs Python 3 but no third-party packages.

## Optional encounter treasure

The **Treasure** selector offers None (the default), Minor, Standard, and Major parcels. Item selection is delegated to the separate **Encounter Loot Generator** in the sibling `PF2e_Encounter_Generator` folder when present, with `F:\Obsidian\PF2e_Encounter_Generator` retained as a fallback. That helper supplies its item database, runes, consumables, spells, rarity handling, and installed Python environment. This random encounter generator does not modify the loot-generator project.

Treasure value uses the supplied Pathfinder 2e Remaster Total Treasure Value by Level table. A Standard parcel is:

`level total value × encounter threat XP ÷ 1,000 × party size ÷ 4`

This maps Trivial, Low, Moderate, Severe, and Extreme to 4%, 6%, 8%, 12%, and 16% of the level's total value. Minor applies 0.5× and Major applies 1.5×. The loot generator initially aims for 40% coins, and any unspent value is returned as coins. The integration reconciles the final displayed item prices so items plus coins never exceed the calculated parcel value.

To use a copy of the loot generator in another location, set its path before starting:

```powershell
$env:PF2E_LOOT_GENERATOR_PATH = "D:\Tools\PF2e_Encounter_Generator"
.\"Start Generator.bat"
```

The selected parcel is written as structured Obsidian treasure entries. When OpenAI details are enabled, OpenAI may explain where the treasure is found but cannot change the selected items, quantities, prices, coins, or budget. If the external loot generator is unavailable, the encounter still succeeds and returns the calculated value as coins.

## Optional OpenAI encounter details

The creature selection and XP calculation are always performed locally. Check **Use OpenAI when generating this encounter** to also write the title, read-aloud text, GM description, tactics, area features, investigation checks, and treasure.

Enter an API key and check **Save a newly entered key securely** to reuse it on future runs. The key is encrypted using Windows user-scoped data protection, stored under your local application-data folder, and can only be decrypted by the same Windows account. It is never stored in `creature_db.db`. Use **Forget the saved key** to remove it.

The default model can be changed for that session:

```powershell
$env:OPENAI_MODEL = "gpt-5.5"
```

The `OPENAI_API_KEY` environment variable remains supported as a fallback. Encounter context is sent to the OpenAI Responses API only when **Use OpenAI** is checked.

## Rules implemented

The four-character budgets are Trivial 40, Low 60, Moderate 80, Severe 120, and Extreme 160 XP. The tool adjusts the budget by `(players - 4) × character adjustment`, then prices each creature from its level relative to the party (-4 through +4).

Biome matches include `Any Land` creatures when a land biome is selected. Biome and condition weights influence random selection. The generator prefers an exact XP match and otherwise returns the closest encounter below budget, showing both target and actual totals.

Condition controls are generated from `condition_modifiers` (with `conditions_modifier` also recognized for compatibility). Each condition type, such as Time of Day or Weather, receives its own optional selector. A creature with no row matching a selected condition remains available at its normal biome weight.

Matching condition weights use 3 as neutral: 1 is a strong penalty and values above 3 increase selection likelihood. When several conditions match a creature, the generator combines their relative modifiers using a geometric mean. This lets every condition contribute without multiplicative weight growth overwhelming the normal creature and biome weighting.

You can optionally require one or more creature traits. When multiple traits are selected, every selected trait must appear somewhere among the generated creatures. Use Ctrl-click in the traits list to select more than one.

**Standard Only** is the default creature-adjustment mode. Allow Elite, Allow Weak, and Allow Elite and Weak add those variants to the standard creature pool rather than converting every creature. Elite increases effective level by 1 for XP and encounter-framework matching; Weak decreases it by 1.

An encounter may contain at most one Elite variant, and it must be tied for the encounter's highest effective level so it functions as a boss, commander, or lieutenant. Multi-creature encounters always retain at least one Standard creature. Weak variants are selected less often than Standard creatures and can fill supporting, minion, or diminished-foe roles. The generated Markdown labels each adjustment while linking back to the base creature note. The appropriate Pathfinder adjustment must still be applied to the creature's statistics at the table.

Terrain features are optional and may be combined. The selected features are sent to OpenAI as required encounter context and also appear as prompts in the Markdown skeleton when the API is unavailable.

Campaign theme is optional and loaded from `campaign_themes`. Themes are grouped by the category prefix before the colon. When selected, both the theme name and description are sent to OpenAI to guide the encounter's situation, motivation, read-aloud text, and tactics. Themes do not affect creature selection or XP.

Regions are loaded from active rows in the SQLite `region` table. The generator checks the party level against the selected region's encounter-level range and sends its description to OpenAI as context.

Primary biome is required and secondary biome is optional. `biomes_region.Primary Allowed` controls the primary list; `Secondary Allowed` controls the secondary list. Creatures matching either selected biome can be used. The two biomes cannot be the same.

Choose **Undetermined Region** to ignore regional availability and select the primary and optional secondary biome directly from every creature biome.

In the current schema, `biomes_region.Name` is the biome, `ID` identifies its region, and `Primary Allowed` / `Secondary Allowed` control whether that biome is offered for the region's selected role. If the table is absent, the form displays a compatibility warning and temporarily exposes all creature biomes. Creature habitat matching uses `biomes_creature`.

Every selected creature is joined to `tactical_creature`. Its `Tactical Tags` are sent to OpenAI so the generated tactics reflect how the creatures fight and coordinate.

The encounter structure selector includes Boss and Lackeys, Boss and Lieutenant, Elite Enemies, Lieutenant and Lackeys, Mated Pair, Troop, and Mook Squad. These structures preserve their listed four-player composition. The generator reports both the structure's actual XP and the party-size-adjusted target when those differ.

## API rate limits

Temporary HTTP 429 rate limits are retried twice with a short delay. If the account has no available API quota, or remains rate-limited, the generator still returns the rules-valid encounter and Markdown skeleton. Check API usage and billing before trying narrative generation again. ChatGPT subscriptions and API billing are separate.

## Using another database location

Set `PF2E_DB_PATH` before starting:

```powershell
$env:PF2E_DB_PATH = "F:\Obsidian\Encounter Test\creature_db.db"
.\"Start Generator.bat"
```
