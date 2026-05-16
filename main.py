from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import traceback
from dataclasses import dataclass
from typing import Any, Optional

import discord
from discord import app_commands
import psycopg
from psycopg.rows import dict_row

APP_VERSION = "Alaris_TournamentBot_v003"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] TourneyBot: %(message)s")
LOG = logging.getLogger("TourneyBot")

CANON_KINGDOMS = ["Ephel Duath", "Galadon", "Mullaghmore", "Frerinn", "Vornladuhr", "Vidalia", "Idolea", "Chiron"]

# Locked six events. Values are canonical database keys in v003.
EVENT_TYPES: dict[str, dict[str, str]] = {
    "grand_melee": {"label": "Grand Melee", "category": "combat_mass"},
    "duel": {"label": "Duel", "category": "combat_duel"},
    "jousting": {"label": "Jousting", "category": "mounted_impact"},
    "archery": {"label": "Archery", "category": "precision"},
    "great_hunt": {"label": "Great Hunt", "category": "pve_hunt"},
    "horse_racing": {"label": "Horse Racing", "category": "mounted_race"},
}
EVENT_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in EVENT_TYPES.items()]
KINGDOM_CHOICES = [app_commands.Choice(name=k, value=k) for k in CANON_KINGDOMS]

# Renown is prestige only in v003. It is used for seeding and flavor, not combat power.
RANKS = [(0, "Newcomer"), (10, "Proven"), (25, "Seasoned"), (50, "Renowned"), (90, "Champion"), (150, "Legend")]
EVENT_SCORE = {1: 5, 2: 3, 3: 2}
RP_EVENT_WIN = 3
RP_RUNNER_UP = 1
RP_OVERALL_CHAMPION = 5

CURRENCY = [(100000000, "Astral", "Astrals"), (1000000, "Throne", "Thrones"), (10000, "Sovereign", "Sovereigns"), (100, "Crown", "Crowns"), (1, "Ember", "Embers")]


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = env(name)
    if raw is None:
        return default
    cleaned = re.sub(r"[^0-9]", "", raw)
    return int(cleaned) if cleaned else default


def env_ids(*names: str) -> set[int]:
    out: set[int] = set()
    for name in names:
        raw = env(name, "") or ""
        for part in raw.replace(";", ",").replace("\n", ",").split(","):
            cleaned = re.sub(r"[^0-9]", "", part)
            if cleaned:
                out.add(int(cleaned))
    return out


DISCORD_TOKEN = env("DISCORD_TOKEN")
DATABASE_URL = env("DATABASE_URL")
GUILD_ID = env_int("GUILD_ID")
STAFF_ROLE_IDS = env_ids("TOURNEY_STAFF_ROLE_IDS", "STAFF_ROLE_IDS")
PUBLIC_CHANNEL_ID = env_int("TOURNEY_PUBLIC_CHANNEL_ID")
LOG_CHANNEL_ID = env_int("TOURNEY_LOG_CHANNEL_ID")
ECON_LOG_CHANNEL_ID = env_int("ECON_LOG_CHANNEL_ID", 1504528860237136022)
EVENT_CHAMPION_PAY = env_int("TOURNEY_EVENT_CHAMPION_REWARD_EMBERS", 0) or 0
EVENT_RUNNER_PAY = env_int("TOURNEY_EVENT_RUNNER_UP_REWARD_EMBERS", 0) or 0
OVERALL_CHAMPION_PAY = env_int("TOURNEY_OVERALL_CHAMPION_REWARD_EMBERS", 0) or 0

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")
if not GUILD_ID:
    raise RuntimeError("Missing or invalid GUILD_ID")

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@dataclass(frozen=True)
class CharacterRef:
    guild_id: int
    character_id: int
    user_id: int
    name: str
    kingdom: Optional[str]
    species: Optional[str]
    class_name: Optional[str]
    level: int


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("`", "'").replace("@", "@\u200b").replace("\r", " ").replace("\n", " ").strip()


def fmt_money(amount: int) -> str:
    amount = int(amount or 0)
    sign = "-" if amount < 0 else ""
    rem = abs(amount)
    parts = []
    for val, sing, plur in CURRENCY:
        q, rem = divmod(rem, val)
        if q:
            parts.append(f"{q:,} {sing if q == 1 else plur}")
    if not parts:
        parts = ["0 Embers"]
    return sign + ", ".join(parts) + f" ({amount:,} Copper Embers)"


def mod(score: int) -> int:
    try:
        return (int(score) - 10) // 2
    except Exception:
        return 0


def rank_name(rp: int) -> str:
    name = "Newcomer"
    for threshold, rn in RANKS:
        if int(rp or 0) >= threshold:
            name = rn
    return name


def event_label(key: str) -> str:
    return EVENT_TYPES.get(key, {}).get("label", clean(key).replace("_", " ").title())


def chunk(lines: list[str], limit: int = 1000) -> list[str]:
    if not lines:
        return ["—"]
    out, cur = [], ""
    for line in lines:
        line = str(line)
        cand = line if not cur else cur + "\n" + line
        if len(cand) > limit:
            if cur:
                out.append(cur)
            cur = line[:limit]
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out or ["—"]


def db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


async def run_db(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def table_columns(cur, table_schema: str, table_name: str) -> set[str]:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s;
    """, (table_schema, table_name))
    return {str(r["column_name"]) for r in cur.fetchall()}


def ensure_schema() -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS tourney;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tourney.tournaments (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    host_kingdom TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    notes TEXT,
                    created_by_user_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (guild_id, name)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tourney.events (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    tournament_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    max_entrants INTEGER,
                    public_channel_id BIGINT,
                    public_message_id BIGINT,
                    settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (guild_id, tournament_id, name)
                );
            """)
            cur.execute("ALTER TABLE tourney.events ADD COLUMN IF NOT EXISTS format_type TEXT;")
            cur.execute("ALTER TABLE tourney.events ADD COLUMN IF NOT EXISTS round_number INTEGER NOT NULL DEFAULT 0;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tourney.entries (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    tournament_id BIGINT NOT NULL,
                    event_id BIGINT NOT NULL,
                    character_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    registration_status TEXT NOT NULL DEFAULT 'registered',
                    seed INTEGER,
                    tournament_score INTEGER NOT NULL DEFAULT 0,
                    entered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (guild_id, event_id, character_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tourney.competitor_profiles (
                    guild_id BIGINT NOT NULL,
                    character_id BIGINT NOT NULL,
                    renown_points INTEGER NOT NULL DEFAULT 0,
                    events_entered INTEGER NOT NULL DEFAULT 0,
                    event_championships INTEGER NOT NULL DEFAULT 0,
                    event_runner_ups INTEGER NOT NULL DEFAULT 0,
                    overall_championships INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, character_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tourney.event_results (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    tournament_id BIGINT NOT NULL,
                    event_id BIGINT NOT NULL,
                    character_id BIGINT NOT NULL,
                    place INTEGER NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    narrative TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (guild_id, event_id, character_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tourney.awards (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    tournament_id BIGINT,
                    event_id BIGINT,
                    character_id BIGINT NOT NULL,
                    award_code TEXT NOT NULL,
                    award_name TEXT NOT NULL,
                    points_awarded INTEGER NOT NULL DEFAULT 0,
                    renown_awarded INTEGER NOT NULL DEFAULT 0,
                    payout_embers BIGINT NOT NULL DEFAULT 0,
                    awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS econ.balances (
                    guild_id BIGINT NOT NULL,
                    character_id BIGINT NOT NULL,
                    balance_embers BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, character_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS econ.transactions (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    character_id BIGINT,
                    actor_user_id BIGINT,
                    action TEXT NOT NULL,
                    amount_embers BIGINT NOT NULL DEFAULT 0,
                    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS econ.kingdoms (
                    guild_id BIGINT NOT NULL,
                    kingdom TEXT NOT NULL,
                    tax_rate_bp INTEGER NOT NULL DEFAULT 1000,
                    treasury_embers BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, kingdom)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.alaris_character_refresh_queue (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    character_id BIGINT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'tournament_update',
                    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS tourney_events_idx ON tourney.events (guild_id, tournament_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS tourney_entries_idx ON tourney.entries (guild_id, event_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS tourney_results_idx ON tourney.event_results (guild_id, tournament_id, event_id);")
        conn.commit()


def sync_chars(guild_id: int) -> dict[str, int]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.alaris_characters') AS t;")
            if not (cur.fetchone() or {}).get("t"):
                return {"has_alaris_characters": 0, "synced": 0}
            cur.execute("""
                INSERT INTO public.characters (
                    guild_id, character_id, user_id, name, normalized_name, species, class_name,
                    kingdom, level, xp_total, archived, created_at, updated_at
                )
                SELECT guild_id, id, user_id, name, COALESCE(normalized_name, lower(name)), COALESCE(species,''),
                       COALESCE(class_name,''), NULLIF(COALESCE(kingdom,''), ''), COALESCE(level,1), COALESCE(xp_total,0),
                       CASE WHEN COALESCE(status,'active')='active' THEN FALSE ELSE TRUE END,
                       COALESCE(created_at,NOW()), NOW()
                FROM public.alaris_characters
                WHERE guild_id=%s
                ON CONFLICT (guild_id, character_id) DO UPDATE SET
                    user_id=EXCLUDED.user_id, name=EXCLUDED.name, normalized_name=EXCLUDED.normalized_name,
                    species=EXCLUDED.species, class_name=EXCLUDED.class_name, kingdom=EXCLUDED.kingdom,
                    level=EXCLUDED.level, xp_total=EXCLUDED.xp_total, archived=EXCLUDED.archived, updated_at=NOW();
            """, (guild_id,))
            n = int(cur.rowcount or 0)
        conn.commit()
    return {"has_alaris_characters": 1, "synced": n}


def character_by_name(guild_id: int, name: str) -> Optional[CharacterRef]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT character_id, user_id, name, kingdom, species, class_name, COALESCE(level,1) AS level
                FROM public.characters
                WHERE guild_id=%s AND archived=FALSE AND name=%s LIMIT 1;
            """, (guild_id, name.strip()))
            r = cur.fetchone()
            if not r:
                return None
            return CharacterRef(guild_id, int(r["character_id"]), int(r["user_id"]), str(r["name"]), r.get("kingdom"), r.get("species"), r.get("class_name"), int(r.get("level") or 1))


def search_chars(guild_id: int, current: str, owner_id: Optional[int] = None) -> list[app_commands.Choice[str]]:
    needle = f"%{(current or '').strip()}%"
    with db() as conn:
        with conn.cursor() as cur:
            if owner_id:
                cur.execute("""
                    SELECT name FROM public.characters
                    WHERE guild_id=%s AND user_id=%s AND archived=FALSE AND name ILIKE %s
                    ORDER BY name LIMIT 25;
                """, (guild_id, owner_id, needle))
            else:
                cur.execute("""
                    SELECT name FROM public.characters
                    WHERE guild_id=%s AND archived=FALSE AND name ILIKE %s
                    ORDER BY name LIMIT 25;
                """, (guild_id, needle))
            rows = cur.fetchall()
    return [app_commands.Choice(name=clean(r["name"])[:100], value=clean(r["name"])[:100]) for r in rows]


def search_tourneys(guild_id: int, current: str) -> list[app_commands.Choice[str]]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name FROM tourney.tournaments
                WHERE guild_id=%s AND status <> 'completed' AND name ILIKE %s
                ORDER BY id DESC LIMIT 25;
            """, (guild_id, f"%{(current or '').strip()}%"))
            rows = cur.fetchall()
    return [app_commands.Choice(name=clean(r["name"])[:100], value=clean(r["name"])[:100]) for r in rows]


def search_event_names(guild_id: int, tournament_name: Optional[str], current: str) -> list[app_commands.Choice[str]]:
    with db() as conn:
        with conn.cursor() as cur:
            if tournament_name:
                cur.execute("SELECT id FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament_name))
                t = cur.fetchone()
                if not t:
                    return []
                cur.execute("""
                    SELECT name FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name ILIKE %s
                    ORDER BY id DESC LIMIT 25;
                """, (guild_id, t["id"], f"%{(current or '').strip()}%"))
            else:
                cur.execute("""
                    SELECT name FROM tourney.events WHERE guild_id=%s AND name ILIKE %s
                    ORDER BY id DESC LIMIT 25;
                """, (guild_id, f"%{(current or '').strip()}%"))
            rows = cur.fetchall()
    return [app_commands.Choice(name=clean(r["name"])[:100], value=clean(r["name"])[:100]) for r in rows]


def stats_for(guild_id: int, cid: int) -> dict[str, int]:
    defaults = {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10}
    aliases = {
        "str": ["strength", "str", "stat_strength", "stat_str"],
        "dex": ["dexterity", "dex", "agility", "stat_dexterity", "stat_dex"],
        "con": ["constitution", "con", "endurance", "stat_constitution", "stat_con"],
        "int": ["intelligence", "int", "stat_intelligence", "stat_int"],
        "wis": ["wisdom", "wis", "stat_wisdom", "stat_wis"],
        "cha": ["charisma", "cha", "presence", "stat_charisma", "stat_cha"],
    }
    with db() as conn:
        with conn.cursor() as cur:
            # Safely inspect possible stat tables. Never assume guild_id exists.
            for schema, table, id_cols in [
                ("public", "alaris_character_stats", ["character_id", "id"]),
                ("public", "alaris_characters", ["id", "character_id"]),
                ("public", "characters", ["character_id"]),
            ]:
                cur.execute("SELECT to_regclass(%s) AS t;", (f"{schema}.{table}",))
                if not (cur.fetchone() or {}).get("t"):
                    continue
                cols = table_columns(cur, schema, table)
                id_col = next((c for c in id_cols if c in cols), None)
                if not id_col:
                    continue
                where = [f"{id_col}=%s"]
                params: list[Any] = [cid]
                if "guild_id" in cols:
                    where.insert(0, "guild_id=%s")
                    params.insert(0, guild_id)
                sql = f"SELECT * FROM {schema}.{table} WHERE {' AND '.join(where)} LIMIT 1;"
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
                if not row:
                    continue
                lower = {str(k).lower(): v for k, v in dict(row).items()}
                out = dict(defaults)
                found = False
                for key, names in aliases.items():
                    for nm in names:
                        if nm in lower and lower[nm] is not None:
                            try:
                                out[key] = int(lower[nm])
                                found = True
                                break
                            except Exception:
                                pass
                if found:
                    return out
    return defaults


def profile_for(guild_id: int, cid: int) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tourney.competitor_profiles (guild_id, character_id) VALUES (%s,%s) ON CONFLICT DO NOTHING;", (guild_id, cid))
            cur.execute("SELECT * FROM tourney.competitor_profiles WHERE guild_id=%s AND character_id=%s;", (guild_id, cid))
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else {"renown_points": 0}


def combat_profile(guild_id: int, c: CharacterRef) -> dict[str, Any]:
    st = stats_for(guild_id, c.character_id)
    p = profile_for(guild_id, c.character_id)
    level = max(1, int(c.level or 1))
    cls = (c.class_name or "").lower()
    species = (c.species or "").lower()
    mods = {k: mod(v) for k, v in st.items()}

    martial_classes = {"fighter", "barbarian", "paladin", "ranger", "monk", "warden", "captain", "rogue"}
    caster_classes = {"wizard", "sorcerer", "warlock", "cleric", "druid", "bard", "scholar", "artificer"}
    hybrid_classes = {"paladin", "ranger", "bard", "artificer", "warlock", "druid", "cleric", "captain"}
    martial_aff = 2 if cls in martial_classes else 0
    caster_aff = 2 if cls in caster_classes else 0
    hybrid_aff = 1 if cls in hybrid_classes else 0

    hp = 20 + level * 6 + mods["con"] * level + (4 if cls in {"barbarian", "warden", "fighter", "paladin"} else 0)
    ac = 10 + mods["dex"] + (3 if cls in {"fighter", "paladin", "warden"} else 1 if cls in {"rogue", "ranger", "monk", "barbarian"} else 0)
    attack = level + max(mods["str"], mods["dex"]) + martial_aff + hybrid_aff
    spell = level + max(mods["int"], mods["wis"], mods["cha"]) + caster_aff + hybrid_aff
    defense = ac + mods["con"] + level // 2
    sustain = hp // 8 + mods["con"] + (2 if cls in {"cleric", "druid", "paladin", "warden", "barbarian"} else 0)
    burst = max(attack, spell) + (2 if cls in {"sorcerer", "wizard", "rogue", "fighter", "barbarian"} else 0)
    control = spell + mods["wis"] + (2 if cls in {"wizard", "bard", "warlock", "druid", "scholar"} else 0)
    mobility = mods["dex"] + level // 2 + (2 if cls in {"rogue", "monk", "ranger", "kitsune"} else 0)
    precision = mods["dex"] + mods["wis"] + level // 2 + (2 if cls in {"ranger", "rogue", "fighter", "monk"} else 0)
    riding = mods["dex"] + mods["con"] + level // 2 + (2 if cls in {"paladin", "fighter", "ranger", "captain"} else 0)
    survival = mods["wis"] + mods["con"] + level // 2 + (2 if cls in {"ranger", "druid", "warden", "barbarian"} else 0)
    renown = int(p.get("renown_points") or 0)
    seed_power = renown + level * 3 + max(attack, spell) + defense
    return {"stats": st, "mods": mods, "profile": p, "rank": rank_name(renown), "renown": renown, "level": level, "class": c.class_name or "", "species": c.species or "", "hp": hp, "ac": ac, "attack": attack, "spell": spell, "defense": defense, "sustain": sustain, "burst": burst, "control": control, "mobility": mobility, "precision": precision, "riding": riding, "survival": survival, "seed_power": seed_power}


def roll_score(base: int, swing: int = 10) -> int:
    return int(base) + random.randint(1, swing) + random.randint(1, swing)


def simulate_event(event_type: str, competitors: list[tuple[CharacterRef, dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[str]]:
    results: list[dict[str, Any]] = []
    lines: list[str] = []
    label = event_label(event_type)

    if event_type == "duel":
        for c, cp in competitors:
            base = max(cp["attack"] + cp["defense"], cp["spell"] + cp["control"]) + cp["sustain"] + cp["burst"]
            score = roll_score(base, 8)
            style = "arcane pressure" if cp["spell"] > cp["attack"] else "martial command"
            lines.append(f"**{clean(c.name)}** enters the dueling ring with {style}, forcing the match into their preferred rhythm.")
            results.append({"character": c, "cp": cp, "score": score, "note": style})
    elif event_type == "grand_melee":
        for c, cp in competitors:
            chaos = random.randint(2, 20)
            base = cp["defense"] * 2 + cp["sustain"] * 2 + cp["burst"] + cp["control"] + cp["mobility"]
            score = base + chaos
            note = "survives the crush" if cp["defense"] + cp["sustain"] >= cp["burst"] else "strikes with sudden violence"
            lines.append(f"**{clean(c.name)}** {note} as the melee collapses into shields, spellfire, and dust.")
            results.append({"character": c, "cp": cp, "score": score, "note": note})
    elif event_type == "jousting":
        for c, cp in competitors:
            base = cp["riding"] * 3 + cp["attack"] + cp["defense"] + mod(cp["stats"]["str"])
            score = roll_score(base, 9)
            note = "keeps a brutal seat through impact" if cp["defense"] > cp["mobility"] else "leans into a fast and daring pass"
            lines.append(f"**{clean(c.name)}** lowers the lance and {note}, drawing a roar from the lists.")
            results.append({"character": c, "cp": cp, "score": score, "note": note})
    elif event_type == "archery":
        for c, cp in competitors:
            base = cp["precision"] * 4 + cp["control"] // 2 + cp["mobility"]
            score = roll_score(base, 8)
            note = "lands a disciplined cluster near the heart" if cp["precision"] >= cp["control"] else "bends uncanny focus into a nearly impossible shot"
            lines.append(f"**{clean(c.name)}** takes the line, breathes once, and {note}.")
            results.append({"character": c, "cp": cp, "score": score, "note": note})
    elif event_type == "horse_racing":
        for c, cp in competitors:
            base = cp["riding"] * 3 + cp["mobility"] * 2 + cp["sustain"] + mod(cp["stats"]["con"])
            score = roll_score(base, 10)
            note = "paces the mount with veteran patience" if cp["sustain"] > cp["mobility"] else "breaks hard through the dangerous turn"
            lines.append(f"**{clean(c.name)}** {note}, thunder rolling beneath the hooves.")
            results.append({"character": c, "cp": cp, "score": score, "note": note})
    else:  # great_hunt
        beasts = ["a barbed marsh-stalker", "an iron-tusk boar", "a glass-eyed wyvernling", "a moon-mad dire hart", "a cliffside ashmaw"]
        beast = random.choice(beasts)
        for c, cp in competitors:
            base = cp["survival"] * 3 + max(cp["attack"], cp["spell"]) + cp["sustain"] + cp["precision"]
            score = roll_score(base, 12)
            note = "tracks patiently and ends the chase cleanly" if cp["survival"] >= cp["burst"] else "turns a dangerous ambush into a spectacular kill"
            lines.append(f"**{clean(c.name)}** draws {beast} and {note}.")
            results.append({"character": c, "cp": cp, "score": score, "note": note})

    # Renown is a tie-breaker/seed flavor only, not a direct score bonus.
    results.sort(key=lambda r: (-r["score"], -int(r["cp"].get("renown") or 0), clean(r["character"].name)))
    for i, r in enumerate(results, start=1):
        r["place"] = i
    if results:
        champ = results[0]["character"]
        if event_type == "grand_melee":
            lines.append(f"When the safe wards flare and the field is called, **{clean(champ.name)}** is the last competitor standing.")
        elif event_type == "duel":
            lines.append(f"The final exchange ends with **{clean(champ.name)}** holding center-ring as the judges call the duel.")
        elif event_type == "jousting":
            lines.append(f"Splintered lancewood litters the ground as **{clean(champ.name)}** is named victor of the joust.")
        elif event_type == "archery":
            lines.append(f"The final marker is measured twice before **{clean(champ.name)}** is declared master of the range.")
        elif event_type == "horse_racing":
            lines.append(f"Dust streams behind the winner as **{clean(champ.name)}** crosses first before the roaring rail.")
        else:
            lines.append(f"The hunt-master raises the token of victory for **{clean(champ.name)}**, whose quarry proves the finest of the field.")
    return results, lines


def add_rp(cur, guild_id: int, cid: int, delta: int, champs: int = 0, runners: int = 0, overall: int = 0) -> None:
    cur.execute("""
        INSERT INTO tourney.competitor_profiles (guild_id, character_id, renown_points, event_championships, event_runner_ups, overall_championships)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (guild_id, character_id) DO UPDATE SET
            renown_points=tourney.competitor_profiles.renown_points+EXCLUDED.renown_points,
            event_championships=tourney.competitor_profiles.event_championships+EXCLUDED.event_championships,
            event_runner_ups=tourney.competitor_profiles.event_runner_ups+EXCLUDED.event_runner_ups,
            overall_championships=tourney.competitor_profiles.overall_championships+EXCLUDED.overall_championships,
            updated_at=NOW();
    """, (guild_id, cid, int(delta), int(champs), int(runners), int(overall)))


def econ_pay(cur, guild_id: int, cid: int, actor: Optional[int], amount: int, action: str, details: dict[str, Any]) -> None:
    amount = int(amount or 0)
    if amount <= 0:
        return
    cur.execute("""
        INSERT INTO econ.balances (guild_id, character_id, balance_embers, updated_at)
        VALUES (%s,%s,%s,NOW())
        ON CONFLICT (guild_id, character_id) DO UPDATE SET balance_embers=econ.balances.balance_embers+EXCLUDED.balance_embers, updated_at=NOW();
    """, (guild_id, cid, amount))
    cur.execute("""
        INSERT INTO econ.transactions (guild_id, character_id, actor_user_id, action, amount_embers, details_json)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb);
    """, (guild_id, cid, actor, action, amount, json.dumps(details)))
    cur.execute("INSERT INTO public.alaris_character_refresh_queue (guild_id, character_id, reason) VALUES (%s,%s,'tournament_payout');", (guild_id, cid))


async def is_staff(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    m = interaction.user
    if m.guild_permissions.administrator or m.guild_permissions.manage_guild or m.guild_permissions.manage_events:
        return True
    return any(r.id in STAFF_ROLE_IDS for r in m.roles)


def staff_only():
    async def pred(interaction: discord.Interaction) -> bool:
        if await is_staff(interaction):
            return True
        if interaction.response.is_done():
            await interaction.followup.send("You do not have permission to use this tournament staff command.", ephemeral=True)
        else:
            await interaction.response.send_message("You do not have permission to use this tournament staff command.", ephemeral=True)
        return False
    return app_commands.check(pred)


async def char_auto(interaction: discord.Interaction, current: str):
    return await run_db(search_chars, int(interaction.guild_id or GUILD_ID), current or "", None)


async def owned_char_auto(interaction: discord.Interaction, current: str):
    owner = None if await is_staff(interaction) else int(interaction.user.id)
    return await run_db(search_chars, int(interaction.guild_id or GUILD_ID), current or "", owner)


async def tourney_auto(interaction: discord.Interaction, current: str):
    return await run_db(search_tourneys, int(interaction.guild_id or GUILD_ID), current or "")


async def event_auto(interaction: discord.Interaction, current: str):
    tname = None
    try:
        tname = getattr(interaction.namespace, "tournament", None)
    except Exception:
        pass
    return await run_db(search_event_names, int(interaction.guild_id or GUILD_ID), tname, current or "")


async def send_channel(channel_id: Optional[int], *, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    if not channel_id:
        return None
    ch = client.get_channel(int(channel_id))
    if ch is None:
        try:
            ch = await client.fetch_channel(int(channel_id))
        except Exception:
            return None
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return None
    try:
        return await ch.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        LOG.warning("Could not send to channel %s: %s", channel_id, exc)
        return None


async def public_post(interaction: discord.Interaction, embed: discord.Embed):
    return await send_channel(PUBLIC_CHANNEL_ID or interaction.channel_id, embed=embed)


async def log_action(action: str, lines: list[str]):
    await send_channel(LOG_CHANNEL_ID, content=(f"**TOURNEY LOG:** `{action}`\n" + "\n".join(lines))[:1900])


def create_tournament(guild_id: int, name: str, kingdom: str, notes: Optional[str], actor: int) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tourney.tournaments (guild_id,name,host_kingdom,status,notes,created_by_user_id)
                VALUES (%s,%s,%s,'draft',%s,%s)
                ON CONFLICT (guild_id,name) DO UPDATE SET host_kingdom=EXCLUDED.host_kingdom, notes=EXCLUDED.notes, updated_at=NOW()
                RETURNING *;
            """, (guild_id, name.strip(), kingdom, notes, actor))
            row = dict(cur.fetchone())
        conn.commit()
    return row


def add_event(guild_id: int, tournament: str, name: str, etype: str, max_entrants: Optional[int]) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("""
                INSERT INTO tourney.events (guild_id,tournament_id,name,event_type,status,max_entrants,format_type)
                VALUES (%s,%s,%s,%s,'draft',%s,'instant_sim')
                ON CONFLICT (guild_id,tournament_id,name) DO UPDATE SET event_type=EXCLUDED.event_type, max_entrants=EXCLUDED.max_entrants, format_type='instant_sim', updated_at=NOW()
                RETURNING *;
            """, (guild_id, t["id"], name.strip(), etype, max_entrants))
            e = dict(cur.fetchone())
        conn.commit()
    return {"ok": True, "tournament": dict(t), "event": e}


def tournament_status(guild_id: int, tournament: str) -> Optional[dict[str, Any]]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return None
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s ORDER BY id;", (guild_id, t["id"]))
            events = [dict(r) for r in cur.fetchall()]
            metrics = {}
            for e in events:
                cur.execute("SELECT COUNT(*) AS n FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND registration_status='registered';", (guild_id, e["id"]))
                metrics[int(e["id"])] = int(cur.fetchone()["n"] or 0)
    return {"tournament": dict(t), "events": events, "metrics": metrics}


def register(guild_id: int, tournament: str, event: str, character: str, actor: int, staff: bool) -> dict[str, Any]:
    c = character_by_name(guild_id, character)
    if not c:
        return {"ok": False, "reason": "character_not_found"}
    if not staff and c.user_id != actor:
        return {"ok": False, "reason": "not_owner"}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event))
            e = cur.fetchone()
            if not e:
                return {"ok": False, "reason": "event_not_found"}
            if e["status"] != "draft":
                return {"ok": False, "reason": "registration_closed"}
            cur.execute("SELECT COUNT(*) AS n FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND registration_status='registered';", (guild_id, e["id"]))
            if e.get("max_entrants") and int(cur.fetchone()["n"] or 0) >= int(e["max_entrants"]):
                return {"ok": False, "reason": "event_full"}
            cur.execute("""
                INSERT INTO tourney.entries (guild_id,tournament_id,event_id,character_id,user_id,registration_status)
                VALUES (%s,%s,%s,%s,%s,'registered')
                ON CONFLICT (guild_id,event_id,character_id) DO UPDATE SET registration_status='registered', updated_at=NOW();
            """, (guild_id, t["id"], e["id"], c.character_id, c.user_id))
            cur.execute("""
                INSERT INTO tourney.competitor_profiles (guild_id,character_id,events_entered)
                VALUES (%s,%s,1)
                ON CONFLICT (guild_id,character_id) DO UPDATE SET events_entered=tourney.competitor_profiles.events_entered+1, updated_at=NOW();
            """, (guild_id, c.character_id))
        conn.commit()
    return {"ok": True, "character": c}


def withdraw(guild_id: int, tournament: str, event: str, character: str, actor: int, staff: bool) -> dict[str, Any]:
    c = character_by_name(guild_id, character)
    if not c:
        return {"ok": False, "reason": "character_not_found"}
    if not staff and c.user_id != actor:
        return {"ok": False, "reason": "not_owner"}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event))
            e = cur.fetchone()
            if not e:
                return {"ok": False, "reason": "event_not_found"}
            if e["status"] != "draft":
                return {"ok": False, "reason": "registration_closed"}
            cur.execute("""
                UPDATE tourney.entries SET registration_status='withdrawn', updated_at=NOW()
                WHERE guild_id=%s AND event_id=%s AND character_id=%s AND registration_status='registered';
            """, (guild_id, e["id"], c.character_id))
            n = cur.rowcount
        conn.commit()
    return {"ok": bool(n), "reason": "not_registered" if not n else "", "character": c}


def run_event(guild_id: int, tournament: str, event: str) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event))
            e = cur.fetchone()
            if not e:
                return {"ok": False, "reason": "event_not_found"}
            if e["status"] == "completed":
                return {"ok": False, "reason": "already_completed"}
            cur.execute("""
                SELECT en.id AS entry_id, c.character_id, c.user_id, c.name, c.kingdom, c.species, c.class_name, COALESCE(c.level,1) AS level,
                       COALESCE(p.renown_points,0) AS renown_points
                FROM tourney.entries en
                JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id
                LEFT JOIN tourney.competitor_profiles p ON p.guild_id=en.guild_id AND p.character_id=en.character_id
                WHERE en.guild_id=%s AND en.event_id=%s AND en.registration_status='registered'
                ORDER BY COALESCE(p.renown_points,0) DESC, c.name ASC;
            """, (guild_id, e["id"]))
            rows = cur.fetchall()
            if len(rows) < 2:
                return {"ok": False, "reason": "not_enough_entrants"}
            competitors: list[tuple[CharacterRef, dict[str, Any]]] = []
            for r in rows:
                c = CharacterRef(guild_id, int(r["character_id"]), int(r["user_id"]), str(r["name"]), r.get("kingdom"), r.get("species"), r.get("class_name"), int(r.get("level") or 1))
                competitors.append((c, combat_profile(guild_id, c)))
            results, lines = simulate_event(str(e["event_type"]), competitors)
            for r in results:
                c = r["character"]
                place = int(r["place"])
                points = EVENT_SCORE.get(place, 0)
                cur.execute("""
                    INSERT INTO tourney.event_results (guild_id,tournament_id,event_id,character_id,place,score,details_json,narrative)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT (guild_id,event_id,character_id) DO UPDATE SET place=EXCLUDED.place, score=EXCLUDED.score, details_json=EXCLUDED.details_json, narrative=EXCLUDED.narrative;
                """, (guild_id, t["id"], e["id"], c.character_id, place, int(r["score"]), json.dumps({"note": r.get("note"), "class": r["cp"].get("class"), "level": r["cp"].get("level"), "renown": r["cp"].get("renown")}), r.get("note")))
                cur.execute("UPDATE tourney.entries SET tournament_score=tournament_score+%s, updated_at=NOW() WHERE guild_id=%s AND event_id=%s AND character_id=%s;", (points, guild_id, e["id"], c.character_id))
            cur.execute("UPDATE tourney.events SET status='ready_to_finalize', updated_at=NOW() WHERE id=%s;", (e["id"],))
        conn.commit()
    return {"ok": True, "tournament": dict(t), "event": dict(e), "results": results, "lines": lines}


def finalize_event_db(guild_id: int, tournament: str, event: str, actor: int) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event))
            e = cur.fetchone()
            if not e:
                return {"ok": False, "reason": "event_not_found"}
            if e["status"] == "completed":
                return {"ok": False, "reason": "already_completed"}
            if e["status"] != "ready_to_finalize":
                return {"ok": False, "reason": "not_ready"}
            cur.execute("""
                SELECT er.*, c.name FROM tourney.event_results er
                JOIN public.characters c ON c.guild_id=er.guild_id AND c.character_id=er.character_id
                WHERE er.guild_id=%s AND er.event_id=%s ORDER BY er.place ASC;
            """, (guild_id, e["id"]))
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return {"ok": False, "reason": "no_results"}
            total_pay = 0
            for r in rows:
                cid = int(r["character_id"]); place = int(r["place"]); points = EVENT_SCORE.get(place, 0)
                rp = RP_EVENT_WIN if place == 1 else RP_RUNNER_UP if place == 2 else 0
                champs = 1 if place == 1 else 0; runners = 1 if place == 2 else 0
                pay = EVENT_CHAMPION_PAY if place == 1 else EVENT_RUNNER_PAY if place == 2 else 0
                add_rp(cur, guild_id, cid, rp, champs, runners)
                econ_pay(cur, guild_id, cid, actor, pay, "tournament_event_payout", {"tournament": tournament, "event": event, "place": place})
                total_pay += pay
                cur.execute("""
                    INSERT INTO tourney.awards (guild_id,tournament_id,event_id,character_id,award_code,award_name,points_awarded,renown_awarded,payout_embers)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);
                """, (guild_id, t["id"], e["id"], cid, "event_place", f"{event_label(e['event_type'])} Place {place}", points, rp, pay))
                cur.execute("INSERT INTO public.alaris_character_refresh_queue (guild_id,character_id,reason) VALUES (%s,%s,'tournament_event_finalized');", (guild_id, cid))
            cur.execute("UPDATE tourney.events SET status='completed', updated_at=NOW() WHERE id=%s;", (e["id"],))
            cur.execute("SELECT COUNT(*) AS n FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND status <> 'completed';", (guild_id, t["id"]))
            open_n = int(cur.fetchone()["n"] or 0)
            cur.execute("UPDATE tourney.tournaments SET status=%s, updated_at=NOW() WHERE id=%s;", ("ready_to_finalize" if open_n == 0 else "active", t["id"]))
        conn.commit()
    return {"ok": True, "tournament": dict(t), "event": dict(e), "rows": rows, "total_pay": total_pay}


def finalize_tournament_db(guild_id: int, tournament: str, actor: int) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("SELECT COUNT(*) AS n FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND status <> 'completed';", (guild_id, t["id"]))
            if int(cur.fetchone()["n"] or 0) > 0:
                return {"ok": False, "reason": "events_not_completed"}
            cur.execute("""
                SELECT en.character_id, c.name, COALESCE(SUM(en.tournament_score),0) AS score
                FROM tourney.entries en JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id
                WHERE en.guild_id=%s AND en.tournament_id=%s AND en.registration_status='registered'
                GROUP BY en.character_id,c.name ORDER BY score DESC, c.name ASC;
            """, (guild_id, t["id"]))
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return {"ok": False, "reason": "no_entries"}
            champ = rows[0]
            add_rp(cur, guild_id, int(champ["character_id"]), RP_OVERALL_CHAMPION, overall=1)
            econ_pay(cur, guild_id, int(champ["character_id"]), actor, OVERALL_CHAMPION_PAY, "tournament_overall_champion_payout", {"tournament": tournament})
            cut = int(round(OVERALL_CHAMPION_PAY * 0.10)) if OVERALL_CHAMPION_PAY and t.get("host_kingdom") else 0
            if cut:
                cur.execute("""
                    INSERT INTO econ.kingdoms (guild_id,kingdom,treasury_embers,updated_at)
                    VALUES (%s,%s,%s,NOW())
                    ON CONFLICT (guild_id,kingdom) DO UPDATE SET treasury_embers=econ.kingdoms.treasury_embers+EXCLUDED.treasury_embers, updated_at=NOW();
                """, (guild_id, t["host_kingdom"], cut))
            cur.execute("""
                INSERT INTO tourney.awards (guild_id,tournament_id,event_id,character_id,award_code,award_name,points_awarded,renown_awarded,payout_embers)
                VALUES (%s,%s,NULL,%s,'overall_champion',%s,%s,%s,%s);
            """, (guild_id, t["id"], int(champ["character_id"]), f"Overall Champion of {clean(tournament)}", int(champ["score"] or 0), RP_OVERALL_CHAMPION, OVERALL_CHAMPION_PAY))
            cur.execute("UPDATE tourney.tournaments SET status='completed', updated_at=NOW() WHERE id=%s;", (t["id"],))
        conn.commit()
    return {"ok": True, "tournament": dict(t), "standings": rows, "champion": champ, "payout": OVERALL_CHAMPION_PAY, "cut": cut}


@tree.command(name="tourney-profile-view", description="View a character's tournament prestige profile.", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(character=char_auto)
async def profile_view(interaction: discord.Interaction, character: str):
    await interaction.response.defer(ephemeral=True)
    c = await run_db(character_by_name, int(interaction.guild_id or GUILD_ID), character)
    if not c:
        await interaction.followup.send("Character not found.", ephemeral=True); return
    cp = await run_db(combat_profile, int(interaction.guild_id or GUILD_ID), c)
    st = cp["stats"]
    emb = discord.Embed(title=f"{clean(c.name)} — Tournament Profile", color=discord.Color.gold())
    emb.description = f"**Rank:** {cp['rank']}\n**Renown:** {cp['renown']} RP\n**Class:** {clean(c.class_name or '—')} | **Level:** {c.level}\n**Kingdom:** {clean(c.kingdom or 'Unassigned')}"
    emb.add_field(name="Alaris Stats", value=f"STR {st['str']} | DEX {st['dex']} | CON {st['con']} | INT {st['int']} | WIS {st['wis']} | CHA {st['cha']}", inline=False)
    emb.add_field(name="Simulation Profile", value=f"HP {cp['hp']} | AC {cp['ac']} | Attack {cp['attack']} | Spell {cp['spell']}\nDefense {cp['defense']} | Sustain {cp['sustain']} | Burst {cp['burst']} | Control {cp['control']}\nRiding {cp['riding']} | Precision {cp['precision']} | Survival {cp['survival']}", inline=False)
    emb.set_footer(text="Renown affects seeding and prestige only, not direct combat power.")
    await interaction.followup.send(embed=emb, ephemeral=True)


@tree.command(name="tourney-create", description="Staff: create a tournament.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.choices(host_kingdom=KINGDOM_CHOICES)
async def tourney_create(interaction: discord.Interaction, name: str, host_kingdom: app_commands.Choice[str], notes: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    row = await run_db(create_tournament, int(interaction.guild_id or GUILD_ID), name, host_kingdom.value, notes, interaction.user.id)
    await interaction.followup.send(f"Created **{clean(row['name'])}** hosted by **{clean(row['host_kingdom'])}**.", ephemeral=True)


@tree.command(name="tourney-event-add", description="Staff: add one of the six event types to a tournament.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
@app_commands.choices(event_type=EVENT_CHOICES)
async def tourney_event_add(interaction: discord.Interaction, tournament: str, name: str, event_type: app_commands.Choice[str], max_entrants: Optional[int] = None):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(add_event, int(interaction.guild_id or GUILD_ID), tournament, name, event_type.value, max_entrants)
    if not res.get("ok"):
        await interaction.followup.send("Tournament not found.", ephemeral=True); return
    await interaction.followup.send(f"Added **{clean(name)}** as **{event_label(event_type.value)}**.", ephemeral=True)


@tree.command(name="tourney-register", description="Register one of your characters for a tournament event.", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto, character=owned_char_auto)
async def tourney_register(interaction: discord.Interaction, tournament: str, event: str, character: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(register, int(interaction.guild_id or GUILD_ID), tournament, event, character, interaction.user.id, await is_staff(interaction))
    if not res.get("ok"):
        await interaction.followup.send(f"Could not register: `{clean(res.get('reason'))}`.", ephemeral=True); return
    await interaction.followup.send(f"Registered **{clean(res['character'].name)}** for **{clean(event)}**.", ephemeral=True)


@tree.command(name="tourney-withdraw", description="Withdraw one of your characters before an event is run.", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto, character=owned_char_auto)
async def tourney_withdraw(interaction: discord.Interaction, tournament: str, event: str, character: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(withdraw, int(interaction.guild_id or GUILD_ID), tournament, event, character, interaction.user.id, await is_staff(interaction))
    if not res.get("ok"):
        await interaction.followup.send(f"Could not withdraw: `{clean(res.get('reason'))}`.", ephemeral=True); return
    await interaction.followup.send(f"Withdrew **{clean(res['character'].name)}** from **{clean(event)}**.", ephemeral=True)


@tree.command(name="tourney-status", description="Staff: view tournament status.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def tourney_status_cmd(interaction: discord.Interaction, tournament: str):
    await interaction.response.defer(ephemeral=True)
    s = await run_db(tournament_status, int(interaction.guild_id or GUILD_ID), tournament)
    if not s:
        await interaction.followup.send("Tournament not found.", ephemeral=True); return
    emb = discord.Embed(title=f"{clean(tournament)} — Status", color=discord.Color.blurple())
    lines = [f"• **{clean(e['name'])}** — {event_label(e['event_type'])} | `{e['status']}` | Entrants: {s['metrics'].get(int(e['id']),0)}" for e in s["events"]]
    emb.description = f"Host: **{clean(s['tournament'].get('host_kingdom') or 'Unassigned')}**\nStatus: `{s['tournament'].get('status')}`"
    for i, ch in enumerate(chunk(lines or ["No events yet."]), start=1):
        emb.add_field(name="Events" if i == 1 else f"Events {i}", value=ch, inline=False)
    await interaction.followup.send(embed=emb, ephemeral=True)


@tree.command(name="tourney-post-announcement", description="Staff: post tournament announcement publicly.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def post_announcement(interaction: discord.Interaction, tournament: str):
    await interaction.response.defer(ephemeral=True)
    s = await run_db(tournament_status, int(interaction.guild_id or GUILD_ID), tournament)
    if not s:
        await interaction.followup.send("Tournament not found.", ephemeral=True); return
    emb = discord.Embed(title=f"{clean(tournament)} — Tournament Announcement", color=discord.Color.gold())
    emb.description = "The lists are opened. Competitors may register for the host-selected events."
    emb.add_field(name="Host", value=clean(s['tournament'].get('host_kingdom') or 'Unassigned'), inline=True)
    lines = [f"• **{clean(e['name'])}** — {event_label(e['event_type'])}" for e in s["events"]]
    emb.add_field(name="Events", value="\n".join(lines) if lines else "No events yet.", inline=False)
    await public_post(interaction, emb)
    await interaction.followup.send("Announcement posted publicly.", ephemeral=True)


@tree.command(name="tourney-run-round", description="Staff: instantly simulate an event and post cinematic results.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
async def tourney_run_round(interaction: discord.Interaction, tournament: str, event: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(run_event, int(interaction.guild_id or GUILD_ID), tournament, event)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not run event: `{clean(res.get('reason'))}`.", ephemeral=True); return
    e = res["event"]
    emb = discord.Embed(title=f"{clean(event)} — {event_label(e['event_type'])} Results", color=discord.Color.gold())
    standings = [f"{r['place']}. **{clean(r['character'].name)}** — {r['score']}" for r in res["results"][:10]]
    emb.add_field(name="Standings", value="\n".join(standings), inline=False)
    for i, ch in enumerate(chunk(res["lines"], 1000)[:4], start=1):
        emb.add_field(name="Cinematic Result" if i == 1 else f"Cinematic Result {i}", value=ch, inline=False)
    emb.set_footer(text="Safe tournament mode: all injuries are narrative-only.")
    await public_post(interaction, emb)
    await interaction.followup.send("Event simulated and posted publicly. Use `/tourney-finalize-event` to award points/RP.", ephemeral=True)


@tree.command(name="tourney-finalize-event", description="Staff: finalize a simulated event and award score/RP.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
async def tourney_finalize_event(interaction: discord.Interaction, tournament: str, event: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(finalize_event_db, int(interaction.guild_id or GUILD_ID), tournament, event, interaction.user.id)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not finalize event: `{clean(res.get('reason'))}`.", ephemeral=True); return
    rows = res["rows"]
    emb = discord.Embed(title=f"{clean(event)} — Finalized", color=discord.Color.gold())
    lines = [f"{int(r['place'])}. **{clean(r['name'])}** — {EVENT_SCORE.get(int(r['place']),0)} tournament pts" for r in rows[:10]]
    emb.description = "\n".join(lines)
    if int(res.get("total_pay") or 0):
        emb.add_field(name="Payouts", value=f"Total paid: **{fmt_money(int(res['total_pay']))}**", inline=False)
    await public_post(interaction, emb)
    await interaction.followup.send("Event finalized and posted publicly.", ephemeral=True)


@tree.command(name="tourney-finalize", description="Staff: finalize the tournament and crown the overall champion.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def tourney_finalize(interaction: discord.Interaction, tournament: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(finalize_tournament_db, int(interaction.guild_id or GUILD_ID), tournament, interaction.user.id)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not finalize tournament: `{clean(res.get('reason'))}`.", ephemeral=True); return
    champ = res["champion"]
    emb = discord.Embed(title=f"{clean(tournament)} — Overall Champion", color=discord.Color.gold())
    emb.description = f"**{clean(champ['name'])}** is crowned overall champion."
    lines = [f"{i}. **{clean(r['name'])}** — {int(r['score'] or 0)} pts" for i, r in enumerate(res["standings"][:10], start=1)]
    emb.add_field(name="Final Standings", value="\n".join(lines), inline=False)
    if int(res.get("payout") or 0):
        emb.add_field(name="Economy", value=f"Champion payout: **{fmt_money(int(res['payout']))}**\nHost treasury cut: **{fmt_money(int(res['cut']))}**", inline=False)
    await public_post(interaction, emb)
    await interaction.followup.send("Tournament finalized and posted publicly.", ephemeral=True)


@tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("TournamentBot hit an internal error. Check Railway logs.", ephemeral=True)
        else:
            await interaction.response.send_message("TournamentBot hit an internal error. Check Railway logs.", ephemeral=True)
    except Exception:
        pass


@client.event
async def on_ready():
    LOG.info("%s logged in as %s", APP_VERSION, client.user)
    try:
        await run_db(ensure_schema)
        LOG.info("Tournament schema ensured.")
        sync = await run_db(sync_chars, int(GUILD_ID))
        LOG.info("Character sync: %s", sync)
    except Exception:
        LOG.exception("Startup schema/sync failed.")
    try:
        synced = await tree.sync(guild=discord.Object(id=int(GUILD_ID)))
        LOG.info("Synced %s guild command(s): %s", len(synced), sorted(c.name for c in synced))
    except Exception:
        LOG.exception("Command sync failed.")


def main():
    client.run(str(DISCORD_TOKEN))


if __name__ == "__main__":
    main()
