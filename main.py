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

APP_VERSION = "Alaris_TournamentBot_v012"

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

SOLO_EVENTS = {"duel", "jousting"}
OPEN_EVENTS = {"grand_melee", "archery", "great_hunt", "horse_racing"}

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
ANNOUNCEMENT_CHANNEL_ID = env_int("TOURNEY_ANNOUNCEMENT_CHANNEL_ID", 1501997730908606624)
TOURNEY_ANNOUNCEMENT_ROLE_ID = env_int("TOURNEY_ANNOUNCEMENT_ROLE_ID", 1505325360613687336)
ECON_LOG_CHANNEL_ID = env_int("ECON_LOG_CHANNEL_ID", 1504528860237136022)
XP_LOG_CHANNEL_ID = env_int("XP_LOG_CHANNEL_ID", 1500571564217860177)

# Reward placeholders. Defaults are intentionally 0 until reward tuning is locked.
PARTICIPATION_XP = env_int("TOURNEY_PARTICIPATION_XP", 25) or 25
THIRD_XP = env_int("TOURNEY_THIRD_XP", 50) or 50
RUNNER_UP_XP = env_int("TOURNEY_RUNNER_UP_XP", 75) or 75
EVENT_WIN_XP = env_int("TOURNEY_EVENT_WIN_XP", 125) or 125
OVERALL_WIN_XP = env_int("TOURNEY_OVERALL_WIN_XP", 200) or 200

PARTICIPATION_PAY = env_int("TOURNEY_PARTICIPATION_EMBERS", 50) or 50
THIRD_PAY = env_int("TOURNEY_THIRD_EMBERS", 150) or 150
RUNNER_PAY = env_int("TOURNEY_RUNNER_UP_EMBERS", 250) or 250
EVENT_WIN_PAY = env_int("TOURNEY_EVENT_WIN_EMBERS", 500) or 500
OVERALL_WIN_PAY = env_int("TOURNEY_OVERALL_WIN_EMBERS", 1000) or 1000

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
            cur.execute("ALTER TABLE tourney.tournaments ADD COLUMN IF NOT EXISTS announcement_channel_id BIGINT;")
            cur.execute("ALTER TABLE tourney.tournaments ADD COLUMN IF NOT EXISTS announcement_message_id BIGINT;")
            cur.execute("ALTER TABLE tourney.tournaments ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ;")
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
            cur.execute("ALTER TABLE tourney.events ADD COLUMN IF NOT EXISTS bracket_message_id BIGINT;")
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
                CREATE TABLE IF NOT EXISTS public.alaris_xp_award_queue (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    character_id BIGINT NOT NULL,
                    source_bot TEXT NOT NULL DEFAULT 'unknown',
                    source_type TEXT NOT NULL DEFAULT 'unspecified',
                    amount_xp INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    requested_by_user_id BIGINT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    claimed_at TIMESTAMPTZ,
                    processed_at TIMESTAMPTZ,
                    error_text TEXT
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
                WHERE guild_id=%s AND status NOT IN ('completed','cancelled') AND name ILIKE %s
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


def xp_pay(cur, guild_id: int, cid: int, actor: Optional[int], amount_xp: int, action: str, details: dict[str, Any]) -> None:
    """Queue XP for AlarisBot to process through the central advancement system.

    TournamentBot must not update xp_total, level, damage die, or level-up state directly.
    AlarisBot v109+ consumes this queue via LISTEN/NOTIFY plus fallback polling.
    """
    amount = int(amount_xp or 0)
    if amount <= 0:
        return
    reason = str(details.get("reason") or details.get("event") or details.get("tournament") or action)
    payload = {**details, "xp_awarded": amount, "queued_by": APP_VERSION}
    cur.execute("""
        INSERT INTO public.alaris_xp_award_queue (
            guild_id, character_id, source_bot, source_type, amount_xp,
            reason, details_json, requested_by_user_id, status, created_at
        )
        VALUES (%s,%s,'Alaris_TournamentBot',%s,%s,%s,%s::jsonb,%s,'pending',NOW());
    """, (guild_id, cid, action, amount, reason, json.dumps(payload), actor))


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
    # v007: registration-related character choices are owner-only for everyone, including staff.
    return await run_db(search_chars, int(interaction.guild_id or GUILD_ID), current or "", int(interaction.user.id))


async def tourney_auto(interaction: discord.Interaction, current: str):
    return await run_db(search_tourneys, int(interaction.guild_id or GUILD_ID), current or "")


async def event_auto(interaction: discord.Interaction, current: str):
    tname = None
    try:
        tname = getattr(interaction.namespace, "tournament", None)
    except Exception:
        pass
    return await run_db(search_event_names, int(interaction.guild_id or GUILD_ID), tname, current or "")


async def send_channel(
    channel_id: Optional[int],
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
):
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
        return await ch.send(content=content, embed=embed, allowed_mentions=allowed_mentions or discord.AllowedMentions.none())
    except Exception as exc:
        LOG.warning("Could not send to channel %s: %s", channel_id, exc)
        return None


async def announcement_post(embed: discord.Embed, *, ping_role: bool = False, content: Optional[str] = None):
    msg_content = content
    mentions = discord.AllowedMentions.none()
    if ping_role and TOURNEY_ANNOUNCEMENT_ROLE_ID:
        msg_content = content or f"<@&{int(TOURNEY_ANNOUNCEMENT_ROLE_ID)}>"
        mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)
    return await send_channel(ANNOUNCEMENT_CHANNEL_ID, content=msg_content, embed=embed, allowed_mentions=mentions)


async def edit_announcement_message(channel_id: Optional[int], message_id: Optional[int], embed: discord.Embed) -> bool:
    if not channel_id or not message_id:
        return False
    ch = client.get_channel(int(channel_id))
    if ch is None:
        try:
            ch = await client.fetch_channel(int(channel_id))
        except Exception:
            return False
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return False
    try:
        msg = await ch.fetch_message(int(message_id))
        await msg.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception as exc:
        LOG.warning("Could not edit tournament announcement %s/%s: %s", channel_id, message_id, exc)
        return False


async def event_post(event: dict[str, Any], embed: discord.Embed, *, content: Optional[str] = None, mention_users: bool = False):
    channel_id = event.get("public_channel_id")
    return await send_channel(
        int(channel_id) if channel_id else None,
        content=content,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=mention_users, roles=False, everyone=False) if mention_users else discord.AllowedMentions.none(),
    )


async def log_econ(lines: list[str]):
    await send_channel(ECON_LOG_CHANNEL_ID, content=("**TOURNEY ECON PAYOUT**\n" + "\n".join(lines))[:1900])


async def log_xp(lines: list[str]):
    await send_channel(XP_LOG_CHANNEL_ID, content=("**TOURNEY XP PAYOUT**\n" + "\n".join(lines))[:1900])


def tournament_open_announcement_embed(status: dict[str, Any]) -> discord.Embed:
    t = status["tournament"]
    events = status["events"]
    metrics = status.get("metrics", {})
    emb = discord.Embed(
        title=f"🏆 {clean(t['name'])} Is Open",
        color=discord.Color.gold(),
        description="Registration is live. Competitors may now register characters they own for the enabled events."
    )
    emb.add_field(name="Host", value=clean(t.get("host_kingdom") or "Unassigned"), inline=True)
    emb.add_field(name="Status", value="Registration Open", inline=True)
    lines = []
    for e in events:
        count = int(metrics.get(int(e["id"]), 0))
        lines.append(f"• **{event_label(e['event_type'])}** — `{clean(e['name'])}` — {count} registered")
    emb.add_field(name="Enabled Events", value="\n".join(lines) if lines else "No events enabled yet.", inline=False)
    emb.add_field(
        name="How to Register",
        value="Use `/tourney-register`, choose one of your characters, then select one or more enabled events.",
        inline=False,
    )
    emb.add_field(
        name="Registration Rules",
        value="You may only register characters you own. A character may enter multiple events, but cannot be registered twice for the same event.",
        inline=False,
    )
    emb.set_footer(text="Tournament updates will edit this announcement silently. You will only be pinged again when the tournament concludes.")
    return emb


def tournament_close_announcement_embed(result: dict[str, Any]) -> discord.Embed:
    t = result["tournament"]
    champ = result["champion"]
    standings = result.get("standings", [])
    event_winners = result.get("event_winners", [])
    emb = discord.Embed(
        title=f"👑 {clean(t['name'])} Has Concluded",
        color=discord.Color.gold(),
        description=f"**{clean(champ['name'])}** is crowned the overall tournament champion."
    )
    if event_winners:
        lines = [f"• **{clean(r['event_name'])}** ({event_label(r['event_type'])}) — **{clean(r['winner_name'])}**" for r in event_winners]
        for i, ch in enumerate(chunk(lines, 1000), start=1):
            emb.add_field(name="Event Champions" if i == 1 else f"Event Champions {i}", value=ch, inline=False)
    if standings:
        lines = [f"{i}. **{clean(r['name'])}** — {int(r['score'] or 0)} pts" for i, r in enumerate(standings[:10], start=1)]
        emb.add_field(name="Overall Standings", value="\n".join(lines), inline=False)
    if int(result.get("payout") or 0):
        emb.add_field(name="Overall Champion Reward", value=f"{fmt_money(int(result['payout']))} | {int(result.get('xp') or 0):,} XP", inline=False)
    emb.set_footer(text="Tournament rewards have been routed through the economy and XP systems.")
    return emb


def open_tournament_with_events(guild_id: int, name: str, kingdom: str, notes: Optional[str], actor: int, selected_events: list[str]) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        return {"ok": False, "reason": "missing_name"}
    valid_events = [e for e in selected_events if e in EVENT_TYPES]
    if not valid_events:
        return {"ok": False, "reason": "no_events"}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tourney.tournaments (guild_id,name,host_kingdom,status,notes,created_by_user_id,opened_at)
                VALUES (%s,%s,%s,'active',%s,%s,NOW())
                ON CONFLICT (guild_id,name) DO UPDATE SET
                    host_kingdom=EXCLUDED.host_kingdom,
                    notes=EXCLUDED.notes,
                    status='active',
                    opened_at=COALESCE(tourney.tournaments.opened_at, NOW()),
                    updated_at=NOW()
                RETURNING *;
            """, (guild_id, clean_name, kingdom, notes, actor))
            t = dict(cur.fetchone())
            for etype in valid_events:
                label = event_label(etype)
                cur.execute("""
                    INSERT INTO tourney.events (guild_id,tournament_id,name,event_type,status,max_entrants,format_type)
                    VALUES (%s,%s,%s,%s,'draft',NULL,%s)
                    ON CONFLICT (guild_id,tournament_id,name) DO UPDATE SET
                        event_type=EXCLUDED.event_type,
                        format_type=EXCLUDED.format_type,
                        updated_at=NOW()
                    RETURNING *;
                """, (guild_id, t["id"], label, etype, 'solo' if etype in SOLO_EVENTS else 'open'))
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s ORDER BY id ASC;", (guild_id, t["id"]))
            events = [dict(r) for r in cur.fetchall()]
            metrics = {int(e["id"]): 0 for e in events}
        conn.commit()
    return {"ok": True, "tournament": t, "events": events, "metrics": metrics}


def store_tournament_announcement(guild_id: int, tournament_id: int, channel_id: int, message_id: int) -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tourney.tournaments
                   SET announcement_channel_id=%s, announcement_message_id=%s, updated_at=NOW()
                 WHERE guild_id=%s AND id=%s;
            """, (channel_id, message_id, guild_id, tournament_id))
        conn.commit()

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


def add_event(guild_id: int, tournament: str, name: str, etype: str, max_entrants: Optional[int], channel_id: Optional[int] = None) -> dict[str, Any]:
    """Create/update an event definition only.

    v007 deliberately does NOT auto-link the event to the command channel.
    Staff must run /post-event inside the desired discussion/forum post to
    create and link the live bracket/standings board.
    """
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            cur.execute("""
                INSERT INTO tourney.events (guild_id,tournament_id,name,event_type,status,max_entrants,format_type)
                VALUES (%s,%s,%s,%s,'draft',%s,%s)
                ON CONFLICT (guild_id,tournament_id,name)
                DO UPDATE SET event_type=EXCLUDED.event_type,
                              max_entrants=EXCLUDED.max_entrants,
                              format_type=EXCLUDED.format_type,
                              updated_at=NOW()
                RETURNING *;
            """, (guild_id, t["id"], name.strip(), etype, max_entrants, "solo_match" if etype in SOLO_EVENTS else "open_round"))
            e = dict(cur.fetchone())
        conn.commit()
    return {"ok": True, "tournament": dict(t), "event": e}


def link_event_board_message(guild_id: int, event_id: int, channel_id: int, message_id: int) -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tourney.events
                   SET public_channel_id=%s, public_message_id=%s, bracket_message_id=%s, updated_at=NOW()
                 WHERE guild_id=%s AND id=%s;
            """, (int(channel_id), int(message_id), int(message_id), int(guild_id), int(event_id)))
        conn.commit()


def set_event_board_message(guild_id: int, event_id: int, message_id: int) -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tourney.events SET public_message_id=%s, bracket_message_id=%s, updated_at=NOW() WHERE guild_id=%s AND id=%s;", (message_id, message_id, guild_id, event_id))
        conn.commit()


def event_context(guild_id: int, tournament: str, event: str) -> Optional[dict[str, Any]]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return None
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event))
            e = cur.fetchone()
            if not e:
                return None
            cur.execute("""
                SELECT en.*, c.name, c.user_id, COALESCE(p.renown_points,0) AS renown_points
                FROM tourney.entries en
                JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id
                LEFT JOIN tourney.competitor_profiles p ON p.guild_id=en.guild_id AND p.character_id=en.character_id
                WHERE en.guild_id=%s AND en.event_id=%s AND en.registration_status <> 'withdrawn'
                ORDER BY
                  CASE en.registration_status WHEN 'registered' THEN 0 WHEN 'advanced' THEN 1 WHEN 'eliminated' THEN 2 ELSE 3 END,
                  COALESCE(p.renown_points,0) DESC,
                  c.name ASC;
            """, (guild_id, e["id"]))
            entries = [dict(r) for r in cur.fetchall()]
    return {"tournament": dict(t), "event": dict(e), "entries": entries}


def event_board_embed(ctx: dict[str, Any]) -> discord.Embed:
    t = ctx["tournament"]; e = ctx["event"]; entries = ctx["entries"]
    emb = discord.Embed(title=f"{clean(e['name'])} — {event_label(e['event_type'])}", color=discord.Color.blurple())
    emb.description = (
        f"**Tournament:** {clean(t['name'])}\n"
        f"**Status:** `{clean(e.get('status'))}`\n"
        f"**Format:** {'Solo match-by-match' if e['event_type'] in SOLO_EVENTS else 'Open round elimination'}\n"
        f"**Round:** {int(e.get('round_number') or 0)}"
    )
    active = [r for r in entries if r.get('registration_status') in ('registered','advanced')]
    eliminated = [r for r in entries if r.get('registration_status') == 'eliminated']
    if active:
        lines = [f"• **{clean(r['name'])}** — <@{int(r['user_id'])}> | Renown {int(r.get('renown_points') or 0)}" for r in active]
        for i, ch in enumerate(chunk(lines, 1000), start=1):
            emb.add_field(name="Active Contestants" if i == 1 else f"Active Contestants {i}", value=ch, inline=False)
    else:
        emb.add_field(name="Active Contestants", value="None yet.", inline=False)
    if eliminated:
        lines = [f"• ~~{clean(r['name'])}~~" for r in eliminated]
        for i, ch in enumerate(chunk(lines, 1000), start=1):
            emb.add_field(name="Eliminated" if i == 1 else f"Eliminated {i}", value=ch, inline=False)
    emb.set_footer(text="Safe tournament mode: results are nonlethal and injuries are narrative-only.")
    return emb

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

    # v007: tournament registration is owner-only for everyone, including staff.
    # Staff reward/admin tools should not become a way to register other players' characters.
    if int(c.user_id) != int(actor):
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

            cur.execute("""
                SELECT registration_status
                  FROM tourney.entries
                 WHERE guild_id=%s AND event_id=%s AND character_id=%s
                 LIMIT 1;
            """, (guild_id, e["id"], c.character_id))
            existing = cur.fetchone()
            if existing and existing.get("registration_status") != "withdrawn":
                return {"ok": False, "reason": "already_registered"}

            cur.execute("SELECT COUNT(*) AS n FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND registration_status='registered';", (guild_id, e["id"]))
            if e.get("max_entrants") and int(cur.fetchone()["n"] or 0) >= int(e["max_entrants"]):
                return {"ok": False, "reason": "event_full"}

            cur.execute("""
                INSERT INTO tourney.entries (guild_id,tournament_id,event_id,character_id,user_id,registration_status)
                VALUES (%s,%s,%s,%s,%s,'registered')
                ON CONFLICT (guild_id,event_id,character_id)
                DO UPDATE SET registration_status='registered', updated_at=NOW()
                WHERE tourney.entries.registration_status='withdrawn';
            """, (guild_id, t["id"], e["id"], c.character_id, c.user_id))

            cur.execute("""
                INSERT INTO tourney.competitor_profiles (guild_id,character_id,events_entered)
                VALUES (%s,%s,1)
                ON CONFLICT (guild_id,character_id)
                DO UPDATE SET events_entered=tourney.competitor_profiles.events_entered+1, updated_at=NOW();
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



def active_event_competitors(cur, guild_id: int, event_id: int) -> list[tuple[dict[str, Any], CharacterRef, dict[str, Any]]]:
    cur.execute("""
        SELECT en.id AS entry_id, en.registration_status, c.character_id, c.user_id, c.name, c.kingdom, c.species, c.class_name,
               COALESCE(c.level,1) AS level, COALESCE(p.renown_points,0) AS renown_points
        FROM tourney.entries en
        JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id
        LEFT JOIN tourney.competitor_profiles p ON p.guild_id=en.guild_id AND p.character_id=en.character_id
        WHERE en.guild_id=%s AND en.event_id=%s AND en.registration_status='registered'
        ORDER BY COALESCE(p.renown_points,0) DESC, c.name ASC;
    """, (guild_id, event_id))
    out: list[tuple[dict[str, Any], CharacterRef, dict[str, Any]]] = []
    for r in cur.fetchall():
        c = CharacterRef(guild_id, int(r["character_id"]), int(r["user_id"]), str(r["name"]), r.get("kingdom"), r.get("species"), r.get("class_name"), int(r.get("level") or 1))
        out.append((dict(r), c, combat_profile(guild_id, c)))
    return out


def record_final_results(cur, guild_id: int, t: dict[str, Any], e: dict[str, Any], ordered: list[tuple[CharacterRef, int, str, dict[str, Any]]]) -> None:
    # ordered rows are (character, score, narrative_note, combat_profile)
    for place, (c, score, note, cp) in enumerate(ordered, start=1):
        points = EVENT_SCORE.get(place, 0)
        cur.execute("""
            INSERT INTO tourney.event_results (guild_id,tournament_id,event_id,character_id,place,score,details_json,narrative)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (guild_id,event_id,character_id)
            DO UPDATE SET place=EXCLUDED.place, score=EXCLUDED.score, details_json=EXCLUDED.details_json, narrative=EXCLUDED.narrative;
        """, (guild_id, t["id"], e["id"], c.character_id, place, int(score), json.dumps({"note": note, "class": cp.get("class"), "level": cp.get("level"), "renown": cp.get("renown")}), note))
        cur.execute("UPDATE tourney.entries SET tournament_score=tournament_score+%s, registration_status=%s, updated_at=NOW() WHERE guild_id=%s AND event_id=%s AND character_id=%s;", (points, 'champion' if place == 1 else 'runner_up' if place == 2 else 'eliminated', guild_id, e["id"], c.character_id))


def run_match_db(guild_id: int, tournament: str, event: str) -> dict[str, Any]:
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
            e = dict(e); t = dict(t)
            if e["event_type"] not in SOLO_EVENTS:
                return {"ok": False, "reason": "not_solo_event"}
            if e["status"] in ("ready_to_finalize", "completed"):
                return {"ok": False, "reason": "event_already_resolved"}
            active = active_event_competitors(cur, guild_id, int(e["id"]))
            if len(active) < 2:
                return {"ok": False, "reason": "not_enough_entrants"}
            # Renown seeding: highest seed faces lowest remaining seed.
            first = active[0]
            second = active[-1]
            competitors = [(first[1], first[2]), (second[1], second[2])]
            results, lines = simulate_event(e["event_type"], competitors)
            winner = results[0]["character"]
            loser = results[1]["character"]
            cur.execute("UPDATE tourney.entries SET registration_status='eliminated', updated_at=NOW() WHERE guild_id=%s AND event_id=%s AND character_id=%s;", (guild_id, e["id"], loser.character_id))
            cur.execute("UPDATE tourney.events SET status='active', round_number=round_number+1, updated_at=NOW() WHERE id=%s;", (e["id"],))
            remaining = [x for x in active if x[1].character_id != loser.character_id]
            final_ready = len(remaining) == 1
            if final_ready:
                ordered: list[tuple[CharacterRef, int, str, dict[str, Any]]] = []
                ordered.append((winner, int(results[0]["score"]), str(results[0].get("note") or "Victory in the final match."), results[0]["cp"]))
                ordered.append((loser, int(results[1]["score"]), str(results[1].get("note") or "Defeated in the final match."), results[1]["cp"]))
                # Add prior eliminated contestants as shared lower placements, preserving enough data for participation rewards.
                cur.execute("""
                    SELECT c.character_id, c.user_id, c.name, c.kingdom, c.species, c.class_name, COALESCE(c.level,1) AS level
                    FROM tourney.entries en JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id
                    WHERE en.guild_id=%s AND en.event_id=%s AND en.registration_status='eliminated' AND c.character_id NOT IN (%s,%s)
                    ORDER BY c.name ASC;
                """, (guild_id, e["id"], winner.character_id, loser.character_id))
                for r in cur.fetchall():
                    c = CharacterRef(guild_id, int(r["character_id"]), int(r["user_id"]), str(r["name"]), r.get("kingdom"), r.get("species"), r.get("class_name"), int(r.get("level") or 1))
                    cp = combat_profile(guild_id, c)
                    ordered.append((c, 0, "Eliminated in an earlier match.", cp))
                record_final_results(cur, guild_id, t, e, ordered)
                cur.execute("UPDATE tourney.events SET status='ready_to_finalize', updated_at=NOW() WHERE id=%s;", (e["id"],))
            conn.commit()
    return {"ok": True, "tournament": t, "event": e, "winner": winner, "loser": loser, "results": results, "lines": lines, "final_ready": final_ready}


def run_open_round_db(guild_id: int, tournament: str, event: str) -> dict[str, Any]:
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
            e = dict(e); t = dict(t)
            if e["event_type"] not in OPEN_EVENTS:
                return {"ok": False, "reason": "not_open_event"}
            if e["status"] in ("ready_to_finalize", "completed"):
                return {"ok": False, "reason": "event_already_resolved"}
            active = active_event_competitors(cur, guild_id, int(e["id"]))
            if len(active) < 2:
                return {"ok": False, "reason": "not_enough_entrants"}
            competitors = [(c, cp) for _, c, cp in active]
            results, lines = simulate_event(e["event_type"], competitors)
            final_ready = len(results) <= 2
            if final_ready:
                ordered = [(r["character"], int(r["score"]), str(r.get("note") or "Final result."), r["cp"]) for r in results]
                record_final_results(cur, guild_id, t, e, ordered)
                cur.execute("UPDATE tourney.events SET status='ready_to_finalize', round_number=round_number+1, updated_at=NOW() WHERE id=%s;", (e["id"],))
            else:
                advance_count = max(2, len(results) // 2)
                advancing = {int(r["character"].character_id) for r in results[:advance_count]}
                for r in results:
                    cid = int(r["character"].character_id)
                    cur.execute("UPDATE tourney.entries SET registration_status=%s, updated_at=NOW() WHERE guild_id=%s AND event_id=%s AND character_id=%s;", ('registered' if cid in advancing else 'eliminated', guild_id, e["id"], cid))
                cur.execute("UPDATE tourney.events SET status='active', round_number=round_number+1, updated_at=NOW() WHERE id=%s;", (e["id"],))
            conn.commit()
    return {"ok": True, "tournament": t, "event": e, "results": results, "lines": lines, "final_ready": final_ready}

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
            total_xp = 0
            for r in rows:
                cid = int(r["character_id"]); place = int(r["place"]); points = EVENT_SCORE.get(place, 0)
                rp = RP_EVENT_WIN if place == 1 else RP_RUNNER_UP if place == 2 else 0
                champs = 1 if place == 1 else 0; runners = 1 if place == 2 else 0
                pay = EVENT_WIN_PAY if place == 1 else RUNNER_PAY if place == 2 else THIRD_PAY if place == 3 else PARTICIPATION_PAY
                add_rp(cur, guild_id, cid, rp, champs, runners)
                xp = EVENT_WIN_XP if place == 1 else RUNNER_UP_XP if place == 2 else THIRD_XP if place == 3 else PARTICIPATION_XP
                econ_pay(cur, guild_id, cid, actor, pay, "tournament_event_payout", {"tournament": tournament, "event": event, "place": place})
                xp_pay(cur, guild_id, cid, actor, xp, "tournament_event_xp", {"tournament": tournament, "event": event, "place": place})
                total_pay += pay
                total_xp += xp
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
    return {"ok": True, "tournament": dict(t), "event": dict(e), "rows": rows, "total_pay": total_pay, "total_xp": total_xp}


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
                WHERE en.guild_id=%s AND en.tournament_id=%s AND en.registration_status <> 'withdrawn'
                GROUP BY en.character_id,c.name ORDER BY score DESC, c.name ASC;
            """, (guild_id, t["id"]))
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return {"ok": False, "reason": "no_entries"}
            champ = rows[0]
            add_rp(cur, guild_id, int(champ["character_id"]), RP_OVERALL_CHAMPION, overall=1)
            econ_pay(cur, guild_id, int(champ["character_id"]), actor, OVERALL_WIN_PAY, "tournament_overall_champion_payout", {"tournament": tournament})
            xp_pay(cur, guild_id, int(champ["character_id"]), actor, OVERALL_WIN_XP, "tournament_overall_champion_xp", {"tournament": tournament})
            cut = int(round(OVERALL_WIN_PAY * 0.10)) if OVERALL_WIN_PAY and t.get("host_kingdom") else 0
            if cut:
                cur.execute("""
                    INSERT INTO econ.kingdoms (guild_id,kingdom,treasury_embers,updated_at)
                    VALUES (%s,%s,%s,NOW())
                    ON CONFLICT (guild_id,kingdom) DO UPDATE SET treasury_embers=econ.kingdoms.treasury_embers+EXCLUDED.treasury_embers, updated_at=NOW();
                """, (guild_id, t["host_kingdom"], cut))
            cur.execute("""
                INSERT INTO tourney.awards (guild_id,tournament_id,event_id,character_id,award_code,award_name,points_awarded,renown_awarded,payout_embers)
                VALUES (%s,%s,NULL,%s,'overall_champion',%s,%s,%s,%s);
            """, (guild_id, t["id"], int(champ["character_id"]), f"Overall Champion of {clean(tournament)}", int(champ["score"] or 0), RP_OVERALL_CHAMPION, OVERALL_WIN_PAY))
            cur.execute("UPDATE tourney.tournaments SET status='completed', updated_at=NOW() WHERE id=%s;", (t["id"],))
            cur.execute("""
                SELECT e.name AS event_name, e.event_type, c.name AS winner_name, er.character_id
                  FROM tourney.event_results er
                  JOIN tourney.events e ON e.guild_id=er.guild_id AND e.id=er.event_id
                  JOIN public.characters c ON c.guild_id=er.guild_id AND c.character_id=er.character_id
                 WHERE er.guild_id=%s AND er.tournament_id=%s AND er.place=1
                 ORDER BY e.id ASC;
            """, (guild_id, t["id"]))
            event_winners = [dict(r) for r in cur.fetchall()]
        conn.commit()
    return {"ok": True, "tournament": dict(t), "standings": rows, "champion": champ, "event_winners": event_winners, "payout": OVERALL_WIN_PAY, "cut": cut, "xp": OVERALL_WIN_XP}




def force_close_tournament_db(guild_id: int, tournament: str, actor: int, reason: str = "") -> dict[str, Any]:
    """Emergency close a stuck tournament without deleting records."""
    reason = clean(reason or "Emergency closed by staff.")[:500]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            if t.get("status") == "completed":
                return {"ok": False, "reason": "already_completed"}
            cur.execute("""
                UPDATE tourney.events
                   SET status='cancelled', updated_at=NOW()
                 WHERE guild_id=%s AND tournament_id=%s AND status <> 'completed';
            """, (guild_id, t["id"]))
            events_changed = int(cur.rowcount or 0)
            cur.execute("""
                UPDATE tourney.tournaments
                   SET status='cancelled', notes=CONCAT(COALESCE(notes,''), '\n[Force close] ', %s::text), updated_at=NOW()
                 WHERE guild_id=%s AND id=%s
                 RETURNING *;
            """, (reason, guild_id, t["id"]))
            updated = dict(cur.fetchone())
        conn.commit()
    return {"ok": True, "tournament": updated, "events_changed": events_changed, "reason_text": reason}


RESET_ACTION_CHOICES = [
    app_commands.Choice(name="Cancel entire tournament", value="cancel_tournament"),
    app_commands.Choice(name="Reset selected event to draft", value="reset_event"),
    app_commands.Choice(name="Clear selected event registrations", value="clear_registrations"),
    app_commands.Choice(name="Reopen selected event registration", value="reopen_event"),
]


def admin_reset_db(guild_id: int, tournament: str, action: str, event: Optional[str], actor: int, reason: str = "") -> dict[str, Any]:
    """Staff recovery utility for stuck tests. Additive schema, no full DB wipes."""
    reason = clean(reason or "Admin recovery action.")[:500]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}
            t = dict(t)

            if action == "cancel_tournament":
                cur.execute("UPDATE tourney.events SET status='cancelled', updated_at=NOW() WHERE guild_id=%s AND tournament_id=%s AND status <> 'completed';", (guild_id, t["id"]))
                events_changed = int(cur.rowcount or 0)
                cur.execute("UPDATE tourney.tournaments SET status='cancelled', notes=CONCAT(COALESCE(notes,''), '\n[Admin cancel] ', %s::text), updated_at=NOW() WHERE guild_id=%s AND id=%s;", (reason, guild_id, t["id"]))
                conn.commit()
                return {"ok": True, "action": action, "tournament": t, "events_changed": events_changed, "message": "Tournament cancelled."}

            if not event:
                return {"ok": False, "reason": "event_required"}
            cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event))
            e = cur.fetchone()
            if not e:
                return {"ok": False, "reason": "event_not_found"}
            e = dict(e)

            if action == "reset_event":
                if e.get("status") == "completed":
                    return {"ok": False, "reason": "event_completed_use_cancel_instead"}
                cur.execute("DELETE FROM tourney.event_results WHERE guild_id=%s AND event_id=%s;", (guild_id, e["id"]))
                results_deleted = int(cur.rowcount or 0)
                cur.execute("DELETE FROM tourney.awards WHERE guild_id=%s AND event_id=%s;", (guild_id, e["id"]))
                awards_deleted = int(cur.rowcount or 0)
                cur.execute("""
                    UPDATE tourney.entries
                       SET registration_status='registered', tournament_score=0, seed=NULL, updated_at=NOW()
                     WHERE guild_id=%s AND event_id=%s AND registration_status <> 'withdrawn';
                """, (guild_id, e["id"]))
                entries_reset = int(cur.rowcount or 0)
                cur.execute("UPDATE tourney.events SET status='draft', round_number=0, updated_at=NOW() WHERE guild_id=%s AND id=%s;", (guild_id, e["id"]))
                cur.execute("UPDATE tourney.tournaments SET status='draft', updated_at=NOW() WHERE guild_id=%s AND id=%s AND status <> 'cancelled';", (guild_id, t["id"]))
                conn.commit()
                return {"ok": True, "action": action, "tournament": t, "event": e, "results_deleted": results_deleted, "awards_deleted": awards_deleted, "entries_reset": entries_reset, "message": "Event reset to draft."}

            if action == "clear_registrations":
                if e.get("status") not in ("draft", "cancelled"):
                    return {"ok": False, "reason": "event_already_started_reset_event_first"}
                cur.execute("UPDATE tourney.entries SET registration_status='withdrawn', updated_at=NOW() WHERE guild_id=%s AND event_id=%s AND registration_status <> 'withdrawn';", (guild_id, e["id"]))
                cleared = int(cur.rowcount or 0)
                cur.execute("UPDATE tourney.events SET status='draft', round_number=0, updated_at=NOW() WHERE guild_id=%s AND id=%s;", (guild_id, e["id"]))
                conn.commit()
                return {"ok": True, "action": action, "tournament": t, "event": e, "cleared": cleared, "message": "Event registrations cleared."}

            if action == "reopen_event":
                if e.get("status") == "completed":
                    return {"ok": False, "reason": "event_completed_cannot_reopen"}
                cur.execute("UPDATE tourney.events SET status='draft', updated_at=NOW() WHERE guild_id=%s AND id=%s;", (guild_id, e["id"]))
                cur.execute("UPDATE tourney.tournaments SET status='draft', updated_at=NOW() WHERE guild_id=%s AND id=%s AND status <> 'cancelled';", (guild_id, t["id"]))
                conn.commit()
                return {"ok": True, "action": action, "tournament": t, "event": e, "message": "Event reopened for registration."}

            return {"ok": False, "reason": "unknown_action"}



async def update_event_board(guild_id: int, tournament: str, event: str) -> Optional[discord.Message]:
    """Edit an existing /post-event board only.

    v007 intentionally does not create/link an event post from registration or
    round commands. Staff must run /post-event in the desired discussion/forum
    post first.
    """
    ctx = await run_db(event_context, guild_id, tournament, event)
    if not ctx:
        return None
    e = ctx["event"]
    channel_id = e.get("public_channel_id")
    msg_id = e.get("public_message_id") or e.get("bracket_message_id")
    if not (channel_id and msg_id):
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
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(embed=event_board_embed(ctx), allowed_mentions=discord.AllowedMentions.none())
        return msg
    except Exception:
        return None


async def require_event_post_here(interaction: discord.Interaction, tournament: str, event: str) -> tuple[bool, Optional[dict[str, Any]], str]:
    ctx = await run_db(event_context, int(interaction.guild_id or GUILD_ID), tournament, event)
    if not ctx:
        return False, None, "event_not_found"
    e = ctx["event"]
    if not (e.get("public_channel_id") and (e.get("public_message_id") or e.get("bracket_message_id"))):
        return False, ctx, "no_event_post"
    if int(e.get("public_channel_id")) != int(interaction.channel_id or 0):
        return False, ctx, "wrong_event_post"
    return True, ctx, ""


def format_results_lines(results: list[dict[str, Any]], limit: int = 10) -> list[str]:
    return [f"{int(r['place']) if 'place' in r else i}. **{clean(r['character'].name)}** — {int(r['score'])}" for i, r in enumerate(results[:limit], start=1)]
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



class TournamentOpenView(discord.ui.View):
    def __init__(self, *, owner_id: int, guild_id: int, name: str, host_kingdom: str, notes: Optional[str]):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.name = name
        self.host_kingdom = host_kingdom
        self.notes = notes
        self.selected_events: list[str] = []
        options = [discord.SelectOption(label=v["label"], value=k) for k, v in EVENT_TYPES.items()]
        self.add_item(TournamentEventMultiSelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Only the staff member who opened this setup panel may use it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Open Tournament", style=discord.ButtonStyle.success)
    async def open_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_events:
            await interaction.response.send_message("Select at least one event before opening the tournament.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await run_db(open_tournament_with_events, self.guild_id, self.name, self.host_kingdom, self.notes, interaction.user.id, self.selected_events)
        if not result.get("ok"):
            await interaction.followup.send(f"Could not open tournament: `{clean(result.get('reason'))}`.", ephemeral=True)
            return
        embed = tournament_open_announcement_embed(result)
        msg = await announcement_post(embed, ping_role=True)
        if msg:
            await run_db(store_tournament_announcement, self.guild_id, int(result["tournament"]["id"]), int(msg.channel.id), int(msg.id))
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        await interaction.followup.send(f"Opened **{clean(self.name)}** with **{len(self.selected_events)}** event(s). Announcement posted and role pinged.", ephemeral=True)


class TournamentEventMultiSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="Select enabled events", min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, TournamentOpenView):
            view.selected_events = list(self.values)
            labels = [event_label(v) for v in self.values]
            await interaction.response.send_message("Selected events: " + ", ".join(labels), ephemeral=True)


@tree.command(name="tourney-open", description="Staff: open a tournament with selected events and post the public announcement.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.choices(host_kingdom=KINGDOM_CHOICES)
async def tourney_open(interaction: discord.Interaction, name: str, host_kingdom: app_commands.Choice[str], notes: Optional[str] = None):
    view = TournamentOpenView(owner_id=interaction.user.id, guild_id=int(interaction.guild_id or GUILD_ID), name=name, host_kingdom=host_kingdom.value, notes=notes)
    embed = discord.Embed(
        title="Open Tournament Setup",
        color=discord.Color.gold(),
        description=f"Tournament: **{clean(name)}**\nHost: **{clean(host_kingdom.value)}**\n\nSelect the events to enable, then click **Open Tournament**."
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.command(name="tourney-create", description="Staff: create a tournament.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.choices(host_kingdom=KINGDOM_CHOICES)
async def tourney_create(interaction: discord.Interaction, name: str, host_kingdom: app_commands.Choice[str], notes: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    row = await run_db(create_tournament, int(interaction.guild_id or GUILD_ID), name, host_kingdom.value, notes, interaction.user.id)
    await interaction.followup.send(f"Created **{clean(row['name'])}** hosted by **{clean(row['host_kingdom'])}**.", ephemeral=True)



@tree.command(name="tourney-event-add", description="Staff: create an event definition for a tournament.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
@app_commands.choices(event_type=EVENT_CHOICES)
async def tourney_event_add(interaction: discord.Interaction, tournament: str, name: str, event_type: app_commands.Choice[str], max_entrants: Optional[int] = None):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    res = await run_db(add_event, gid, tournament, name, event_type.value, max_entrants, None)
    if not res.get("ok"):
        await interaction.followup.send("Tournament not found.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Added **{clean(name)}** as **{event_label(event_type.value)}**. Run `/post-event` inside the desired discussion/forum post to create the live bracket/contestant board.",
        ephemeral=True,
    )


@tree.command(name="post-event", description="Staff: post and link the live bracket/contestant board in this channel/post.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
async def post_event(interaction: discord.Interaction, tournament: str, event: str):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    ctx = await run_db(event_context, gid, tournament, event)
    if not ctx:
        await interaction.followup.send("Tournament/event not found.", ephemeral=True)
        return
    emb = event_board_embed(ctx)
    try:
        msg = await interaction.channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())  # type: ignore[union-attr]
    except Exception:
        await interaction.followup.send("Could not post the event board here. Check channel permissions.", ephemeral=True)
        return
    await run_db(link_event_board_message, gid, int(ctx["event"]["id"]), int(interaction.channel_id or msg.channel.id), int(msg.id))
    await interaction.followup.send("Event board posted and linked. Registrations and round results will update this post.", ephemeral=True)


def registerable_events_for_tournament(guild_id: int, tournament: str) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found", "events": []}
            cur.execute(
                """
                SELECT e.*,
                       COUNT(en.*) FILTER (WHERE en.registration_status='registered') AS registered_count
                  FROM tourney.events e
                  LEFT JOIN tourney.entries en
                    ON en.guild_id=e.guild_id
                   AND en.event_id=e.id
                 WHERE e.guild_id=%s
                   AND e.tournament_id=%s
                   AND e.status='draft'
                 GROUP BY e.id
                 ORDER BY e.id ASC;
                """,
                (guild_id, t["id"]),
            )
            events = [dict(r) for r in cur.fetchall()]
    return {"ok": True, "tournament": dict(t), "events": events}


def register_many(guild_id: int, tournament: str, event_names: list[str], character: str, actor: int) -> dict[str, Any]:
    c = character_by_name(guild_id, character)
    if not c:
        return {"ok": False, "reason": "character_not_found"}
    if int(c.user_id) != int(actor):
        return {"ok": False, "reason": "not_owner"}

    selected = [str(e or "").strip() for e in event_names if str(e or "").strip()]
    if not selected:
        return {"ok": False, "reason": "no_events_selected"}

    registered: list[str] = []
    skipped: list[dict[str, str]] = []

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s LIMIT 1;", (guild_id, tournament))
            t = cur.fetchone()
            if not t:
                return {"ok": False, "reason": "tournament_not_found"}

            for event_name in selected:
                cur.execute("SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s LIMIT 1;", (guild_id, t["id"], event_name))
                e = cur.fetchone()
                if not e:
                    skipped.append({"event": event_name, "reason": "event_not_found"})
                    continue
                if e["status"] != "draft":
                    skipped.append({"event": event_name, "reason": "registration_closed"})
                    continue

                cur.execute(
                    """
                    SELECT registration_status
                      FROM tourney.entries
                     WHERE guild_id=%s AND event_id=%s AND character_id=%s
                     LIMIT 1;
                    """,
                    (guild_id, e["id"], c.character_id),
                )
                existing = cur.fetchone()
                if existing and existing.get("registration_status") != "withdrawn":
                    skipped.append({"event": event_name, "reason": "already_registered"})
                    continue

                cur.execute("SELECT COUNT(*) AS n FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND registration_status='registered';", (guild_id, e["id"]))
                if e.get("max_entrants") and int(cur.fetchone()["n"] or 0) >= int(e["max_entrants"]):
                    skipped.append({"event": event_name, "reason": "event_full"})
                    continue

                cur.execute(
                    """
                    INSERT INTO tourney.entries (guild_id,tournament_id,event_id,character_id,user_id,registration_status)
                    VALUES (%s,%s,%s,%s,%s,'registered')
                    ON CONFLICT (guild_id,event_id,character_id)
                    DO UPDATE SET registration_status='registered', updated_at=NOW()
                    WHERE tourney.entries.registration_status='withdrawn';
                    """,
                    (guild_id, t["id"], e["id"], c.character_id, c.user_id),
                )
                registered.append(event_name)

            if registered:
                cur.execute(
                    """
                    INSERT INTO tourney.competitor_profiles (guild_id,character_id,events_entered)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (guild_id,character_id)
                    DO UPDATE SET events_entered=tourney.competitor_profiles.events_entered+EXCLUDED.events_entered,
                                  updated_at=NOW();
                    """,
                    (guild_id, c.character_id, len(registered)),
                )
        conn.commit()

    return {"ok": True, "character": c, "registered": registered, "skipped": skipped}


class RegisterEventsView(discord.ui.View):
    def __init__(self, *, owner_id: int, guild_id: int, tournament: str, character: str, events: list[dict[str, Any]]):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.tournament = tournament
        self.character = character
        self.selected_events: list[str] = []
        options: list[discord.SelectOption] = []
        for e in events[:25]:
            count = int(e.get("registered_count") or 0)
            label = clean(e.get("name"))[:100]
            desc = f"{event_label(e.get('event_type'))} | {count} registered"
            options.append(discord.SelectOption(label=label, value=label, description=desc[:100]))

        # Component order matters. Add dropdowns first and the submit button last
        # so Discord displays the button beneath the selections instead of above them.
        self.add_item(RegisterEventMultiSelect(options))
        submit = discord.ui.Button(label="Register Selected Events", style=discord.ButtonStyle.success, row=1)
        submit.callback = self.submit_selected_events
        self.add_item(submit)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Only the player who opened this registration panel may use it.", ephemeral=True)
            return False
        return True

    async def submit_selected_events(self, interaction: discord.Interaction):
        if not self.selected_events:
            await interaction.response.send_message("Select at least one event first, then press **Register Selected Events**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        res = await run_db(register_many, self.guild_id, self.tournament, self.selected_events, self.character, interaction.user.id)
        if not res.get("ok"):
            await interaction.followup.send(f"Could not register: `{clean(res.get('reason'))}`.", ephemeral=True)
            return
        for event_name in res.get("registered", []):
            await update_event_board(self.guild_id, self.tournament, event_name)
        if res.get("registered"):
            await update_tournament_announcement(self.guild_id, self.tournament)

        registered = res.get("registered", [])
        skipped = res.get("skipped", [])
        lines = [f"**Registration result for {clean(res['character'].name)}**"]
        if registered:
            lines.append("✅ Registered for: " + ", ".join(f"**{clean(e)}**" for e in registered))
        if skipped:
            lines.append("⚠️ Skipped: " + ", ".join(f"**{clean(s['event'])}** (`{clean(s['reason'])}`)" for s in skipped))
        if not registered and not skipped:
            lines.append("No registrations were made.")

        # Disable controls and rewrite the original panel so the user has a visible confirmation
        # even if they miss the ephemeral follow-up.
        for child in self.children:
            child.disabled = True
        try:
            done_embed = discord.Embed(
                title=f"Registration Submitted — {clean(self.tournament)}",
                description="\n".join(lines),
                color=discord.Color.green() if registered else discord.Color.orange(),
            )
            await interaction.message.edit(embed=done_embed, view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class RegisterEventMultiSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="Select one or more events to register for", min_values=1, max_values=max(1, len(options)), options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, RegisterEventsView):
            view.selected_events = list(self.values)
            await interaction.response.send_message("Selected: " + ", ".join(f"**{clean(v)}**" for v in self.values) + "\nNow press **Register Selected Events** below the dropdown.", ephemeral=True)


@tree.command(name="tourney-register", description="Register one of your characters for one or more tournament events.", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(tournament=tourney_auto, character=owned_char_auto)
async def tourney_register(interaction: discord.Interaction, tournament: str, character: str):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    c = await run_db(character_by_name, gid, character)
    if not c:
        await interaction.followup.send("Character not found.", ephemeral=True)
        return
    if int(c.user_id) != int(interaction.user.id):
        await interaction.followup.send("You may only register characters you own.", ephemeral=True)
        return
    data = await run_db(registerable_events_for_tournament, gid, tournament)
    if not data.get("ok"):
        await interaction.followup.send(f"Could not load events: `{clean(data.get('reason'))}`.", ephemeral=True)
        return
    events = data.get("events", [])
    if not events:
        await interaction.followup.send("There are no open events available for registration in that tournament.", ephemeral=True)
        return
    view = RegisterEventsView(owner_id=interaction.user.id, guild_id=gid, tournament=tournament, character=character, events=events)
    embed = discord.Embed(
        title=f"Register for {clean(tournament)}",
        color=discord.Color.gold(),
        description=f"Character: **{clean(c.name)}**\nSelect one or more events, then press **Register Selected Events**."
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@tree.command(name="tourney-withdraw", description="Withdraw one of your characters before an event is run.", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto, character=owned_char_auto)
async def tourney_withdraw(interaction: discord.Interaction, tournament: str, event: str, character: str):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    res = await run_db(withdraw, gid, tournament, event, character, interaction.user.id, await is_staff(interaction))
    if not res.get("ok"):
        await interaction.followup.send(f"Could not withdraw: `{clean(res.get('reason'))}`.", ephemeral=True)
        return
    await update_event_board(gid, tournament, event)
    await update_tournament_announcement(gid, tournament)
    await interaction.followup.send(f"Withdrew **{clean(res['character'].name)}** from **{clean(event)}**.", ephemeral=True)


@tree.command(name="tourney-status", description="Staff: view tournament status.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def tourney_status_cmd(interaction: discord.Interaction, tournament: str):
    await interaction.response.defer(ephemeral=True)
    s = await run_db(tournament_status, int(interaction.guild_id or GUILD_ID), tournament)
    if not s:
        await interaction.followup.send("Tournament not found.", ephemeral=True)
        return
    emb = discord.Embed(title=f"{clean(tournament)} — Status", color=discord.Color.blurple())
    lines = [f"• **{clean(e['name'])}** — {event_label(e['event_type'])} | `{e['status']}` | Entrants: {s['metrics'].get(int(e['id']),0)}" for e in s["events"]]
    emb.description = f"Host: **{clean(s['tournament'].get('host_kingdom') or 'Unassigned')}**\nStatus: `{s['tournament'].get('status')}`"
    for i, ch in enumerate(chunk(lines or ["No events yet."]), start=1):
        emb.add_field(name="Events" if i == 1 else f"Events {i}", value=ch, inline=False)
    await interaction.followup.send(embed=emb, ephemeral=True)


@tree.command(name="tourney-post-announcement", description="Staff fallback: repost the tournament opening announcement and ping the tournament role.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def post_announcement(interaction: discord.Interaction, tournament: str):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    s = await run_db(tournament_status, gid, tournament)
    if not s:
        await interaction.followup.send("Tournament not found.", ephemeral=True)
        return
    emb = tournament_open_announcement_embed(s)
    msg = await announcement_post(emb, ping_role=True)
    if msg:
        await run_db(store_tournament_announcement, gid, int(s["tournament"]["id"]), int(msg.channel.id), int(msg.id))
    await interaction.followup.send("Opening announcement posted to the tournament announcement channel and role pinged.", ephemeral=True)


@tree.command(name="tourney-run-match", description="Staff: run the next solo match for Duel or Jousting in this event post.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
async def tourney_run_match(interaction: discord.Interaction, tournament: str, event: str):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    ok, _ctx, reason = await require_event_post_here(interaction, tournament, event)
    if not ok:
        if reason == "no_event_post":
            await interaction.followup.send("No event board is linked yet. Run `/post-event` inside this event's discussion/forum post first.", ephemeral=True)
        elif reason == "wrong_event_post":
            await interaction.followup.send("Run this command inside the linked event discussion/forum post.", ephemeral=True)
        else:
            await interaction.followup.send("Tournament/event not found.", ephemeral=True)
        return
    res = await run_db(run_match_db, gid, tournament, event)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not run match: `{clean(res.get('reason'))}`.", ephemeral=True)
        return
    e = res["event"]
    emb = discord.Embed(title=f"{clean(event)} — Match Result", color=discord.Color.gold())
    emb.description = f"**{clean(res['winner'].name)}** defeats **{clean(res['loser'].name)}**."
    for i, ch in enumerate(chunk(res["lines"], 1000)[:4], start=1):
        emb.add_field(name="Cinematic Match" if i == 1 else f"Cinematic Match {i}", value=ch, inline=False)
    emb.set_footer(text="Safe tournament mode: all injuries are narrative-only.")
    content = f"<@{int(res['winner'].user_id)}> vs <@{int(res['loser'].user_id)}>"
    await event_post(e, emb, content=content, mention_users=True)
    await update_event_board(gid, tournament, event)
    msg = "Match resolved and posted in this event post."
    if res.get("final_ready"):
        msg += " This event is ready to finalize."
    await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="tourney-run-round", description="Staff: run the next open-event round for Melee, Archery, Hunt, or Racing.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
async def tourney_run_round(interaction: discord.Interaction, tournament: str, event: str):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    ok, _ctx, reason = await require_event_post_here(interaction, tournament, event)
    if not ok:
        if reason == "no_event_post":
            await interaction.followup.send("No event board is linked yet. Run `/post-event` inside this event's discussion/forum post first.", ephemeral=True)
        elif reason == "wrong_event_post":
            await interaction.followup.send("Run this command inside the linked event discussion/forum post.", ephemeral=True)
        else:
            await interaction.followup.send("Tournament/event not found.", ephemeral=True)
        return
    res = await run_db(run_open_round_db, gid, tournament, event)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not run round: `{clean(res.get('reason'))}`.", ephemeral=True)
        return
    e = res["event"]
    emb = discord.Embed(title=f"{clean(event)} — Round Result", color=discord.Color.gold())
    standings = [f"{i}. **{clean(r['character'].name)}** — {int(r['score'])}" for i, r in enumerate(res["results"][:10], start=1)]
    emb.add_field(name="Round Standings", value="\n".join(standings), inline=False)
    for i, ch in enumerate(chunk(res["lines"], 1000)[:4], start=1):
        emb.add_field(name="Cinematic Round" if i == 1 else f"Cinematic Round {i}", value=ch, inline=False)
    emb.set_footer(text="Safe tournament mode: all injuries are narrative-only.")
    await event_post(e, emb)
    await update_event_board(gid, tournament, event)
    msg = "Round resolved and posted in this event post."
    if res.get("final_ready"):
        msg += " This event is ready to finalize."
    await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="tourney-finalize-event", description="Staff: finalize an event and post results in the linked event post.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
async def tourney_finalize_event(interaction: discord.Interaction, tournament: str, event: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(finalize_event_db, int(interaction.guild_id or GUILD_ID), tournament, event, interaction.user.id)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not finalize event: `{clean(res.get('reason'))}`.", ephemeral=True)
        return
    rows = res["rows"]
    emb = discord.Embed(title=f"{clean(event)} — Final Results", color=discord.Color.gold())
    lines = [f"{int(r['place'])}. **{clean(r['name'])}** — {EVENT_SCORE.get(int(r['place']),0)} tournament pts" for r in rows[:10]]
    emb.description = "\n".join(lines)
    if int(res.get("total_pay") or 0):
        emb.add_field(name="Economy", value=f"Total paid: **{fmt_money(int(res['total_pay']))}**", inline=False)
    if int(res.get("total_xp") or 0):
        emb.add_field(name="XP", value=f"Total awarded: **{int(res['total_xp']):,} XP**", inline=False)
    # v010: event results stay in the linked event post. The announcement channel is reserved for tournament open and tournament close only.
    if res.get("event"):
        await event_post(res["event"], emb)
    await update_event_board(int(interaction.guild_id or GUILD_ID), tournament, event)
    if int(res.get("total_pay") or 0):
        await log_econ([f"Event: **{clean(event)}**", f"Total paid: **{fmt_money(int(res['total_pay']))}**"])
    if int(res.get("total_xp") or 0):
        await log_xp([f"Event: **{clean(event)}**", f"Total XP: **{int(res['total_xp']):,}**"])
    await interaction.followup.send("Event finalized. Results were posted in the linked event post; no announcement ping was sent.", ephemeral=True)


@tree.command(name="tourney-finalize", description="Staff: finalize the tournament and announce all event winners plus the overall champion.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def tourney_finalize(interaction: discord.Interaction, tournament: str):
    await interaction.response.defer(ephemeral=True)
    res = await run_db(finalize_tournament_db, int(interaction.guild_id or GUILD_ID), tournament, interaction.user.id)
    if not res.get("ok"):
        await interaction.followup.send(f"Could not finalize tournament: `{clean(res.get('reason'))}`.", ephemeral=True)
        return
    champ = res["champion"]
    emb = tournament_close_announcement_embed(res)
    await announcement_post(emb, ping_role=True)
    if int(res.get("payout") or 0):
        await log_econ([f"Tournament: **{clean(tournament)}**", f"Overall champion payout: **{fmt_money(int(res['payout']))}**"])
    if int(res.get("xp") or 0):
        await log_xp([f"Tournament: **{clean(tournament)}**", f"Overall champion XP: **{int(res['xp']):,}**"])
    await interaction.followup.send("Tournament finalized. Closing announcement posted and tournament role pinged.", ephemeral=True)


@tree.command(name="tourney-force-close", description="Staff emergency: force-close a stuck tournament without deleting records.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto)
async def tourney_force_close(interaction: discord.Interaction, tournament: str, reason: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    res = await run_db(force_close_tournament_db, gid, tournament, interaction.user.id, reason or "Emergency force-close requested by staff.")
    if not res.get("ok"):
        await interaction.followup.send(f"Could not force-close tournament: `{clean(res.get('reason'))}`.", ephemeral=True)
        return
    emb = discord.Embed(title=f"{clean(tournament)} — Tournament Closed", color=discord.Color.red())
    emb.description = "This tournament has been force-closed by staff. Existing records were preserved."
    emb.add_field(name="Reason", value=clean(res.get("reason_text") or "—")[:1024], inline=False)
    emb.add_field(name="Events Closed", value=str(int(res.get("events_changed") or 0)), inline=True)
    await announcement_post(emb)
    await interaction.followup.send(f"Force-closed **{clean(tournament)}**. Existing records were preserved.", ephemeral=True)


@tree.command(name="tourney-admin-reset", description="Staff recovery: cancel a tournament, reset an event, or clear stuck registrations.", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourney_auto, event=event_auto)
@app_commands.choices(action=RESET_ACTION_CHOICES)
async def tourney_admin_reset(interaction: discord.Interaction, tournament: str, action: app_commands.Choice[str], event: Optional[str] = None, reason: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    gid = int(interaction.guild_id or GUILD_ID)
    res = await run_db(admin_reset_db, gid, tournament, action.value, event, interaction.user.id, reason or "Admin recovery action.")
    if not res.get("ok"):
        reason_code = clean(res.get("reason"))
        if reason_code == "event_required":
            await interaction.followup.send("That reset action requires an event name.", ephemeral=True)
        else:
            await interaction.followup.send(f"Could not reset: `{reason_code}`.", ephemeral=True)
        return
    if event:
        await update_event_board(gid, tournament, event)
    if action.value == "cancel_tournament":
        emb = discord.Embed(title=f"{clean(tournament)} — Tournament Cancelled", color=discord.Color.red())
        emb.description = "This tournament has been cancelled by staff. Existing historical records were preserved."
        emb.add_field(name="Reason", value=clean(reason or "Admin recovery action.")[:1024], inline=False)
        await announcement_post(emb)
    summary_bits = [f"Action: `{clean(action.value)}`", f"Tournament: **{clean(tournament)}**"]
    if event:
        summary_bits.append(f"Event: **{clean(event)}**")
    if "entries_reset" in res:
        summary_bits.append(f"Entries reset: **{int(res.get('entries_reset') or 0)}**")
    if "cleared" in res:
        summary_bits.append(f"Registrations cleared: **{int(res.get('cleared') or 0)}**")
    if "events_changed" in res:
        summary_bits.append(f"Events changed: **{int(res.get('events_changed') or 0)}**")
    await interaction.followup.send("\n".join(summary_bits), ephemeral=True)

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
