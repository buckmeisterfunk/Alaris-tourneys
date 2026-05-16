
from __future__ import annotations
import os, re, json, random, asyncio, logging, traceback
from dataclasses import dataclass
from typing import Any, Optional
import discord
from discord import app_commands
import psycopg
from psycopg.rows import dict_row

APP_VERSION='Alaris_TournamentBot_v002'
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] TourneyBot: %(message)s')
log=logging.getLogger('TourneyBot')
CANON_KINGDOMS=['Ephel Duath','Galadon','Mullaghmore','Frerinn','Vornladuhr','Vidalia','Idolea','Chiron']
EVENT_TYPES={'joust':('Joust','head_to_head'),'archery':('Archery','scored_round'),'grand_melee':('Grand Melee','scored_round'),'duel':('Duel','head_to_head'),'horse_race':('Horse Race','scored_round'),'hunt':('Hunt','scored_round')}
EVENT_CHOICES=[app_commands.Choice(name=v[0], value=k) for k,v in EVENT_TYPES.items()]
KINGDOM_CHOICES=[app_commands.Choice(name=k,value=k) for k in CANON_KINGDOMS]
RANKS=[(0,'Newcomer',0),(10,'Proven',1),(25,'Seasoned',2),(50,'Renowned',3),(90,'Champion',4),(150,'Legend',5)]
CURRENCY=[(100000000,'Astral','Astrals'),(1000000,'Throne','Thrones'),(10000,'Sovereign','Sovereigns'),(100,'Crown','Crowns'),(1,'Ember','Embers')]

def env(n,d=None):
    v=os.getenv(n); return d if v is None or not v.strip() else v.strip()
def env_int(n,d=None):
    v=env(n); 
    if v is None: return d
    s=re.sub(r'[^0-9]','',v); return int(s) if s else d
def env_ids(*names):
    out=set()
    for n in names:
        for p in (env(n,'') or '').replace(';',',').split(','):
            s=re.sub(r'[^0-9]','',p)
            if s: out.add(int(s))
    return out
DISCORD_TOKEN=env('DISCORD_TOKEN'); DATABASE_URL=env('DATABASE_URL'); GUILD_ID=env_int('GUILD_ID')
STAFF_ROLE_IDS=env_ids('TOURNEY_STAFF_ROLE_IDS','STAFF_ROLE_IDS')
PUBLIC_CHANNEL_ID=env_int('TOURNEY_PUBLIC_CHANNEL_ID'); LOG_CHANNEL_ID=env_int('TOURNEY_LOG_CHANNEL_ID'); ECON_LOG_CHANNEL_ID=env_int('ECON_LOG_CHANNEL_ID',1504528860237136022)
EVENT_CHAMPION_PAY=env_int('TOURNEY_EVENT_CHAMPION_REWARD_EMBERS',0) or 0
EVENT_RUNNER_PAY=env_int('TOURNEY_EVENT_RUNNER_UP_REWARD_EMBERS',0) or 0
OVERALL_CHAMPION_PAY=env_int('TOURNEY_OVERALL_CHAMPION_REWARD_EMBERS',0) or 0
if not DISCORD_TOKEN: raise RuntimeError('Missing DISCORD_TOKEN')
if not DATABASE_URL: raise RuntimeError('Missing DATABASE_URL')
if not GUILD_ID: raise RuntimeError('Missing GUILD_ID')
intents=discord.Intents.default(); intents.members=True
client=discord.Client(intents=intents); tree=app_commands.CommandTree(client)
@dataclass(frozen=True)
class CRef: guild_id:int; character_id:int; user_id:int; name:str; kingdom:Optional[str]=None

def clean(x): return str(x or '').replace('`',"'").replace('@','@\u200b').replace('\n',' ').replace('\r',' ').strip()
def fmt_money(n:int)->str:
    n=int(n or 0); sign='-' if n<0 else ''; r=abs(n); parts=[]
    for val,s,p in CURRENCY:
        q,r=divmod(r,val)
        if q: parts.append(f'{q:,} {s if q==1 else p}')
    if not parts: parts=['0 Embers']
    return sign+', '.join(parts)+f' ({n:,} Copper Embers)'
def mod(score): return (int(score or 10)-10)//2
def rank_from_rp(rp:int):
    name='Newcomer'; bonus=0
    for th,n,b in RANKS:
        if int(rp or 0)>=th: name=n; bonus=b
    return name,bonus
def roll():
    a=random.randint(1,6); b=random.randint(1,6); return a,b,a+b
def db(): return psycopg.connect(DATABASE_URL,row_factory=dict_row)
async def run_db(fn,*a,**k): return await asyncio.to_thread(fn,*a,**k)

def ensure_schema():
    with db() as conn, conn.cursor() as cur:
        cur.execute('CREATE SCHEMA IF NOT EXISTS tourney;')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.tournaments(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,name TEXT NOT NULL,host_kingdom TEXT,status TEXT NOT NULL DEFAULT 'draft',notes TEXT,created_by_user_id BIGINT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(guild_id,name));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.events(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,tournament_id BIGINT NOT NULL,name TEXT NOT NULL,event_type TEXT NOT NULL,format_type TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',max_entrants INTEGER,min_entrants INTEGER NOT NULL DEFAULT 2,round_number INTEGER NOT NULL DEFAULT 0,public_channel_id BIGINT,public_message_id BIGINT,settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(guild_id,tournament_id,name));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.entries(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,tournament_id BIGINT NOT NULL,event_id BIGINT NOT NULL,character_id BIGINT NOT NULL,user_id BIGINT NOT NULL,registration_status TEXT NOT NULL DEFAULT 'registered',seed INTEGER,tournament_score INTEGER NOT NULL DEFAULT 0,entered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(guild_id,event_id,character_id));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.matches(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,tournament_id BIGINT NOT NULL,event_id BIGINT NOT NULL,round_number INTEGER NOT NULL,match_order INTEGER NOT NULL DEFAULT 1,match_type TEXT NOT NULL DEFAULT 'head_to_head',status TEXT NOT NULL DEFAULT 'pending',winner_character_id BIGINT,narrative_summary TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),completed_at TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.match_participants(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,match_id BIGINT NOT NULL,entry_id BIGINT,character_id BIGINT NOT NULL,slot_number INTEGER NOT NULL DEFAULT 1,final_position INTEGER,eliminated BOOLEAN NOT NULL DEFAULT FALSE,points INTEGER NOT NULL DEFAULT 0,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.match_rolls(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,match_id BIGINT NOT NULL,character_id BIGINT,phase_code TEXT NOT NULL,roll_formula TEXT NOT NULL,die_1 INTEGER,die_2 INTEGER,base_total INTEGER,modifier_total INTEGER,rank_bonus INTEGER NOT NULL DEFAULT 0,final_total INTEGER,detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.competitor_profiles(guild_id BIGINT NOT NULL,character_id BIGINT NOT NULL,renown_points INTEGER NOT NULL DEFAULT 0,events_entered INTEGER NOT NULL DEFAULT 0,match_wins INTEGER NOT NULL DEFAULT 0,event_championships INTEGER NOT NULL DEFAULT 0,event_runner_ups INTEGER NOT NULL DEFAULT 0,overall_championships INTEGER NOT NULL DEFAULT 0,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(guild_id,character_id));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.records(guild_id BIGINT NOT NULL,character_id BIGINT NOT NULL,event_type TEXT NOT NULL,entries_count INTEGER NOT NULL DEFAULT 0,wins_count INTEGER NOT NULL DEFAULT 0,runner_up_count INTEGER NOT NULL DEFAULT 0,third_place_count INTEGER NOT NULL DEFAULT 0,points_total INTEGER NOT NULL DEFAULT 0,last_played_at TIMESTAMPTZ,PRIMARY KEY(guild_id,character_id,event_type));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS tourney.awards(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,tournament_id BIGINT,event_id BIGINT,character_id BIGINT NOT NULL,award_code TEXT NOT NULL,award_name TEXT NOT NULL,points_awarded INTEGER NOT NULL DEFAULT 0,renown_awarded INTEGER NOT NULL DEFAULT 0,payout_embers BIGINT NOT NULL DEFAULT 0,awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        cur.execute('CREATE SCHEMA IF NOT EXISTS econ;')
        cur.execute('''CREATE TABLE IF NOT EXISTS econ.balances(guild_id BIGINT NOT NULL,character_id BIGINT NOT NULL,balance_embers BIGINT NOT NULL DEFAULT 0,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(guild_id,character_id));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS econ.transactions(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,character_id BIGINT,actor_user_id BIGINT,action TEXT NOT NULL,amount_embers BIGINT NOT NULL DEFAULT 0,details_json JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        cur.execute('''CREATE TABLE IF NOT EXISTS econ.kingdoms(guild_id BIGINT NOT NULL,kingdom TEXT NOT NULL,tax_rate_bp INTEGER NOT NULL DEFAULT 1000,treasury_embers BIGINT NOT NULL DEFAULT 0,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(guild_id,kingdom));''')
        cur.execute('''CREATE TABLE IF NOT EXISTS public.alaris_character_refresh_queue(id BIGSERIAL PRIMARY KEY,guild_id BIGINT NOT NULL,character_id BIGINT NOT NULL,reason TEXT NOT NULL DEFAULT 'tournament_update',requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),processed_at TIMESTAMPTZ);''')
        for k in CANON_KINGDOMS: cur.execute('INSERT INTO econ.kingdoms(guild_id,kingdom) VALUES(%s,%s) ON CONFLICT DO NOTHING;',(GUILD_ID,k))
        cur.execute('CREATE INDEX IF NOT EXISTS tourney_events_idx ON tourney.events(guild_id,tournament_id);')
        cur.execute('CREATE INDEX IF NOT EXISTS tourney_entries_idx ON tourney.entries(guild_id,event_id);')
        conn.commit()

def sync_chars(guild_id:int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.alaris_characters') AS t;")
        if not (cur.fetchone() or {}).get('t'): return {'synced':0,'has_alaris_characters':0}
        cur.execute('''CREATE TABLE IF NOT EXISTS public.characters(character_id BIGINT PRIMARY KEY,guild_id BIGINT NOT NULL,user_id BIGINT NOT NULL,name TEXT NOT NULL,normalized_name TEXT,species TEXT,class_name TEXT,kingdom TEXT,level INTEGER NOT NULL DEFAULT 1,xp_total BIGINT NOT NULL DEFAULT 0,archived BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS characters_guild_character_id_uidx ON public.characters(guild_id,character_id);')
        cur.execute('''INSERT INTO public.characters(guild_id,character_id,user_id,name,normalized_name,species,class_name,kingdom,level,xp_total,archived,created_at,updated_at)
        SELECT guild_id,id,user_id,name,COALESCE(normalized_name,lower(name)),COALESCE(species,''),COALESCE(class_name,''),NULLIF(COALESCE(kingdom,''),''),COALESCE(level,1),COALESCE(xp_total,0),CASE WHEN COALESCE(status,'active')='active' THEN FALSE ELSE TRUE END,COALESCE(created_at,NOW()),NOW()
        FROM public.alaris_characters WHERE guild_id=%s
        ON CONFLICT(guild_id,character_id) DO UPDATE SET user_id=EXCLUDED.user_id,name=EXCLUDED.name,kingdom=EXCLUDED.kingdom,level=EXCLUDED.level,xp_total=EXCLUDED.xp_total,archived=EXCLUDED.archived,updated_at=NOW();''',(guild_id,))
        rc=cur.rowcount; conn.commit(); return {'synced':int(rc or 0),'has_alaris_characters':1}

def fetch_char(guild_id:int,name:str)->Optional[CRef]:
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT character_id,user_id,name,kingdom FROM public.characters WHERE guild_id=%s AND archived=FALSE AND name=%s LIMIT 1;',(guild_id,name))
        r=cur.fetchone(); return CRef(guild_id,int(r['character_id']),int(r['user_id']),str(r['name']),r.get('kingdom')) if r else None

def fetch_char_id(guild_id:int,cid:int)->Optional[CRef]:
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT character_id,user_id,name,kingdom FROM public.characters WHERE guild_id=%s AND archived=FALSE AND character_id=%s LIMIT 1;',(guild_id,cid))
        r=cur.fetchone(); return CRef(guild_id,int(r['character_id']),int(r['user_id']),str(r['name']),r.get('kingdom')) if r else None

def search_chars(guild_id:int,current:str,owner:Optional[int]=None):
    where='AND user_id=%s' if owner else ''; params=(guild_id,owner,f'%{current}%') if owner else (guild_id,f'%{current}%')
    with db() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT name FROM public.characters WHERE guild_id=%s {where} AND archived=FALSE AND name ILIKE %s ORDER BY name LIMIT 25;',params)
        return [app_commands.Choice(name=clean(r['name'])[:100], value=clean(r['name'])[:100]) for r in cur.fetchall()]

def _table_columns(cur, table: str) -> set[str]:
    schema, name = table.split('.', 1)
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s;
        """,
        (schema, name),
    )
    return {str(r['column_name']).lower() for r in cur.fetchall()}


def stats_for(guild_id:int,cid:int):
    """Fetch Alaris stats without assuming every table has guild_id.

    v002 safety fix: live public.alaris_character_stats may be keyed only by
    character_id/id and may not contain guild_id. We inspect columns first and
    build the WHERE clause from columns that actually exist.
    """
    defaults={'str':10,'dex':10,'con':10,'int':10,'wis':10,'cha':10}
    aliases={
        'str':['str','strength','stat_str','stat_strength'],
        'dex':['dex','dexterity','agility','stat_dex','stat_dexterity'],
        'con':['con','constitution','endurance','stat_con','stat_constitution'],
        'int':['int','intelligence','stat_int','stat_intelligence'],
        'wis':['wis','wisdom','stat_wis','stat_wisdom'],
        'cha':['cha','charisma','presence','stat_cha','stat_charisma'],
    }
    def norm(row):
        lower={str(k).lower():v for k,v in dict(row).items()}; out=dict(defaults)
        for key,names in aliases.items():
            for n in names:
                if n in lower and lower[n] is not None:
                    try:
                        out[key]=int(lower[n]); break
                    except Exception:
                        pass
        return out

    candidates=[
        ('public.alaris_character_stats',['character_id','id','alaris_character_id']),
        ('public.alaris_characters',['id','character_id']),
        ('public.characters',['character_id','id']),
    ]
    with db() as conn, conn.cursor() as cur:
        for table, id_cols in candidates:
            cur.execute('SELECT to_regclass(%s) AS t;',(table,))
            if not (cur.fetchone() or {}).get('t'):
                continue
            cols=_table_columns(cur, table)
            id_col=next((c for c in id_cols if c.lower() in cols), None)
            if not id_col:
                continue
            where=[f'{id_col}=%s']; params=[cid]
            if 'guild_id' in cols:
                where.insert(0,'guild_id=%s'); params.insert(0,guild_id)
            # Avoid archived/inactive rows when the columns exist.
            if 'archived' in cols:
                where.append('COALESCE(archived, FALSE) = FALSE')
            if 'status' in cols:
                where.append("COALESCE(status, 'active') = 'active'")
            cur.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where)} LIMIT 1;", tuple(params))
            r=cur.fetchone()
            if r:
                s=norm(r)
                if s!=defaults or table.endswith('characters'):
                    return s
    return defaults

def profile(guild_id:int,cid:int):
    with db() as conn, conn.cursor() as cur:
        cur.execute('INSERT INTO tourney.competitor_profiles(guild_id,character_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;',(guild_id,cid))
        cur.execute('SELECT * FROM tourney.competitor_profiles WHERE guild_id=%s AND character_id=%s;',(guild_id,cid)); r=dict(cur.fetchone()); conn.commit(); return r

def skills(guild_id:int,cid:int):
    st=stats_for(guild_id,cid); p=profile(guild_id,cid); rn,rb=rank_from_rp(p.get('renown_points',0)); m={k:mod(v) for k,v in st.items()}
    return {'riding':m['dex']+m['con']+rb,'weapon':m['str']+m['dex']+rb,'archery':m['dex']+m['wis']+rb,'duel':m['str']+m['dex']+m['wis']+rb,'stamina':m['con']+m['str']+rb,'composure':m['wis']+m['cha']+rb,'_rank':rn,'_bonus':rb,'_rp':int(p.get('renown_points') or 0),'_stats':st}

def seed_score(et,sk):
    return {'joust':sk['riding']*3+sk['weapon']*3+sk['composure']*2,'duel':sk['duel']*3+sk['weapon']*2+sk['stamina']*2,'archery':sk['archery']*4+sk['composure']*2,'horse_race':sk['riding']*4+sk['stamina']*2+sk['composure'],'hunt':sk['archery']*3+sk['stamina']*2+sk['composure']*2,'grand_melee':sk['duel']*3+sk['stamina']*3+sk['weapon']}.get(et,0)

def add_rp(cur,guild_id,cid,rp,**counters):
    cur.execute('INSERT INTO tourney.competitor_profiles(guild_id,character_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;',(guild_id,cid))
    parts=['renown_points=renown_points+%s','updated_at=NOW()']; vals=[int(rp)]
    for k,v in counters.items():
        if v: parts.append(f'{k}={k}+%s'); vals.append(int(v))
    vals += [guild_id,cid]
    cur.execute(f'UPDATE tourney.competitor_profiles SET {", ".join(parts)} WHERE guild_id=%s AND character_id=%s;',tuple(vals))

def queue_refresh(cur,guild_id,cid,reason): cur.execute('INSERT INTO public.alaris_character_refresh_queue(guild_id,character_id,reason) VALUES(%s,%s,%s);',(guild_id,cid,reason))
def payout(cur,guild_id,cid,actor,amount,action,details):
    amount=int(amount or 0)
    if amount<=0: return 0
    cur.execute('INSERT INTO econ.balances(guild_id,character_id,balance_embers) VALUES(%s,%s,%s) ON CONFLICT(guild_id,character_id) DO UPDATE SET balance_embers=econ.balances.balance_embers+EXCLUDED.balance_embers, updated_at=NOW();',(guild_id,cid,amount))
    cur.execute('INSERT INTO econ.transactions(guild_id,character_id,actor_user_id,action,amount_embers,details_json) VALUES(%s,%s,%s,%s,%s,%s::jsonb);',(guild_id,cid,actor,action,amount,json.dumps(details)))
    queue_refresh(cur,guild_id,cid,'tournament_payout'); return amount

def get_t(cur,guild_id,name): cur.execute('SELECT * FROM tourney.tournaments WHERE guild_id=%s AND name=%s ORDER BY id DESC LIMIT 1;',(guild_id,name)); return cur.fetchone()
def get_e(cur,guild_id,tid,name): cur.execute('SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name=%s ORDER BY id DESC LIMIT 1;',(guild_id,tid,name)); return cur.fetchone()

def list_tournaments(guild_id,current):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM tourney.tournaments WHERE guild_id=%s AND status<>'completed' AND name ILIKE %s ORDER BY id DESC LIMIT 25;",(guild_id,f'%{current}%'))
        return [app_commands.Choice(name=clean(r['name'])[:100],value=clean(r['name'])[:100]) for r in cur.fetchall()]
def list_events(guild_id,tname,current):
    with db() as conn, conn.cursor() as cur:
        if tname:
            t=get_t(cur,guild_id,tname)
            if not t: return []
            cur.execute('SELECT name FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND name ILIKE %s ORDER BY id DESC LIMIT 25;',(guild_id,t['id'],f'%{current}%'))
        else: cur.execute('SELECT name FROM tourney.events WHERE guild_id=%s AND name ILIKE %s ORDER BY id DESC LIMIT 25;',(guild_id,f'%{current}%'))
        return [app_commands.Choice(name=clean(r['name'])[:100],value=clean(r['name'])[:100]) for r in cur.fetchall()]

def chunk(lines,max_len=1000):
    out=[]; cur=''
    for l in lines:
        l=clean(l) or '—'; cand=l if not cur else cur+'\n'+l
        if len(cand)>max_len:
            if cur: out.append(cur); cur=l
            else: out.append(l[:max_len])
        else: cur=cand
    if cur: out.append(cur)
    return out or ['—']
async def staff(inter):
    m=inter.user
    return isinstance(m,discord.Member) and (m.guild_permissions.administrator or m.guild_permissions.manage_guild or m.guild_permissions.manage_events or any(r.id in STAFF_ROLE_IDS for r in m.roles))
def staff_only():
    async def pred(inter):
        if await staff(inter): return True
        await inter.response.send_message('You do not have permission to use this tournament staff command.',ephemeral=True); return False
    return app_commands.check(pred)
async def char_ac(inter,current): return await run_db(search_chars,int(inter.guild_id or GUILD_ID),current or '',None)
async def own_char_ac(inter,current): return await run_db(search_chars,int(inter.guild_id or GUILD_ID),current or '',None if await staff(inter) else int(inter.user.id))
async def tourn_ac(inter,current): return await run_db(list_tournaments,int(inter.guild_id or GUILD_ID),current or '')
async def event_ac(inter,current):
    t=getattr(getattr(inter,'namespace',None),'tournament',None)
    return await run_db(list_events,int(inter.guild_id or GUILD_ID),t,current or '')
async def send_chan(cid,embed=None,content=None):
    if not cid: return None
    ch=client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
    if isinstance(ch,(discord.TextChannel,discord.Thread)): return await ch.send(content=content,embed=embed,allowed_mentions=discord.AllowedMentions.none())
async def public_post(inter,embed): return await send_chan(PUBLIC_CHANNEL_ID or inter.channel_id,embed=embed)

@tree.command(name='tourney-profile-view',description="View a character's derived tournament profile.",guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(character=char_ac)
async def profile_view(inter,character:str):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID); c=await run_db(fetch_char,gid,character)
    if not c: return await inter.followup.send('Character not found.',ephemeral=True)
    sk=await run_db(skills,gid,c.character_id); p=await run_db(profile,gid,c.character_id); st=sk['_stats']
    e=discord.Embed(title=f'{clean(c.name)} — Tournament Profile',color=discord.Color.gold(),description=f"**Kingdom:** {clean(c.kingdom or 'Unassigned')}\n**Rank:** {sk['_rank']} (+{sk['_bonus']})\n**Renown Points:** {sk['_rp']}")
    e.add_field(name='Alaris Stats',value=f"STR {st['str']} | DEX {st['dex']} | CON {st['con']} | INT {st['int']} | WIS {st['wis']} | CHA {st['cha']}",inline=False)
    e.add_field(name='Derived Skills',value=f"Riding **{sk['riding']}** | Weapon **{sk['weapon']}** | Archery **{sk['archery']}**\nDuel **{sk['duel']}** | Stamina **{sk['stamina']}** | Composure **{sk['composure']}**",inline=False)
    e.add_field(name='Career',value=f"Events Entered: **{p.get('events_entered',0)}**\nMatch/Heat Wins: **{p.get('match_wins',0)}**\nEvent Championships: **{p.get('event_championships',0)}**\nOverall Championships: **{p.get('overall_championships',0)}**",inline=False)
    await inter.followup.send(embed=e,ephemeral=True)

@tree.command(name='tourney-create',description='Staff: create a tournament.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.choices(host_kingdom=KINGDOM_CHOICES)
async def tourney_create(inter,name:str,host_kingdom:app_commands.Choice[str],notes:Optional[str]=None):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID)
    def f():
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO tourney.tournaments(guild_id,name,host_kingdom,status,notes,created_by_user_id) VALUES(%s,%s,%s,'draft',%s,%s) ON CONFLICT(guild_id,name) DO UPDATE SET host_kingdom=EXCLUDED.host_kingdom,notes=EXCLUDED.notes,updated_at=NOW() RETURNING *;",(gid,name.strip(),host_kingdom.value,notes,inter.user.id)); r=dict(cur.fetchone()); conn.commit(); return r
    r=await run_db(f); await inter.followup.send(f"Created **{clean(r['name'])}** hosted by **{clean(r['host_kingdom'])}**.",ephemeral=True)

@tree.command(name='tourney-event-add',description='Staff: add an event.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourn_ac)
@app_commands.choices(event_type=EVENT_CHOICES)
async def event_add(inter,tournament:str,name:str,event_type:app_commands.Choice[str],max_entrants:Optional[int]=None):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID); et=event_type.value; fmt=EVENT_TYPES[et][1]
    def f():
        with db() as conn, conn.cursor() as cur:
            t=get_t(cur,gid,tournament)
            if not t: return None
            cur.execute("INSERT INTO tourney.events(guild_id,tournament_id,name,event_type,format_type,status,max_entrants) VALUES(%s,%s,%s,%s,%s,'draft',%s) ON CONFLICT(guild_id,tournament_id,name) DO UPDATE SET event_type=EXCLUDED.event_type,format_type=EXCLUDED.format_type,max_entrants=EXCLUDED.max_entrants,updated_at=NOW() RETURNING *;",(gid,t['id'],name.strip(),et,fmt,max_entrants)); e=dict(cur.fetchone()); conn.commit(); return e
    e=await run_db(f)
    if not e: return await inter.followup.send('Tournament not found.',ephemeral=True)
    await inter.followup.send(f"Added **{clean(e['name'])}** as **{EVENT_TYPES[e['event_type']][0]}**.",ephemeral=True)

@tree.command(name='tourney-register',description='Register a character for an event.',guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(tournament=tourn_ac,event=event_ac,character=own_char_ac)
async def register(inter,tournament:str,event:str,character:str):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID); is_staff=await staff(inter)
    def f():
        with db() as conn, conn.cursor() as cur:
            t=get_t(cur,gid,tournament); 
            if not t: return 'Tournament not found.'
            e=get_e(cur,gid,t['id'],event)
            if not e: return 'Event not found.'
            if e['status'] in ('completed','ready_to_finalize') or int(e['round_number'] or 0)>0: return 'Registration is closed for that event.'
            c=fetch_char(gid,character)
            if not c: return 'Character not found.'
            if not is_staff and c.user_id!=inter.user.id: return 'You may only register characters you own.'
            cur.execute("SELECT COUNT(*) AS n FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND registration_status<>'withdrawn';",(gid,e['id']))
            if e['max_entrants'] and int(cur.fetchone()['n'] or 0)>=int(e['max_entrants']): return 'That event is full.'
            cur.execute("INSERT INTO tourney.entries(guild_id,tournament_id,event_id,character_id,user_id,registration_status) VALUES(%s,%s,%s,%s,%s,'registered') ON CONFLICT(guild_id,event_id,character_id) DO UPDATE SET registration_status='registered',updated_at=NOW();",(gid,t['id'],e['id'],c.character_id,c.user_id))
            cur.execute('INSERT INTO tourney.competitor_profiles(guild_id,character_id,events_entered) VALUES(%s,%s,1) ON CONFLICT(guild_id,character_id) DO UPDATE SET events_entered=tourney.competitor_profiles.events_entered+1,updated_at=NOW();',(gid,c.character_id))
            conn.commit(); return f'Registered **{clean(c.name)}** for **{clean(event)}**.'
    await inter.followup.send(await run_db(f),ephemeral=True)

@tree.command(name='tourney-withdraw',description='Withdraw a character before an event starts.',guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(tournament=tourn_ac,event=event_ac,character=own_char_ac)
async def withdraw(inter,tournament:str,event:str,character:str):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID); is_staff=await staff(inter)
    def f():
        with db() as conn, conn.cursor() as cur:
            t=get_t(cur,gid,tournament); 
            if not t: return 'Tournament not found.'
            e=get_e(cur,gid,t['id'],event)
            if not e: return 'Event not found.'
            if e['status'] in ('completed','ready_to_finalize') or int(e['round_number'] or 0)>0: return 'Registration is closed for that event.'
            c=fetch_char(gid,character)
            if not c: return 'Character not found.'
            if not is_staff and c.user_id!=inter.user.id: return 'You may only withdraw characters you own.'
            cur.execute("UPDATE tourney.entries SET registration_status='withdrawn',updated_at=NOW() WHERE guild_id=%s AND event_id=%s AND character_id=%s AND registration_status<>'withdrawn';",(gid,e['id'],c.character_id)); n=cur.rowcount; conn.commit(); return f'Withdrew **{clean(c.name)}** from **{clean(event)}**.' if n else 'That character is not registered for that event.'
    await inter.followup.send(await run_db(f),ephemeral=True)

def init_h2h(cur,gid,t,e):
    cur.execute('SELECT COUNT(*) AS n FROM tourney.matches WHERE guild_id=%s AND event_id=%s;',(gid,e['id']))
    if int(cur.fetchone()['n'] or 0)>0: return True,''
    cur.execute("SELECT en.id AS entry_id,en.character_id,en.user_id,c.name FROM tourney.entries en JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id WHERE en.guild_id=%s AND en.event_id=%s AND en.registration_status='registered' ORDER BY c.name;",(gid,e['id']))
    rows=[dict(r) for r in cur.fetchall()]
    if len(rows)<2: return False,'not_enough_entrants'
    scored=[]
    for r in rows:
        sk=skills(gid,int(r['character_id'])); r['score']=seed_score(e['event_type'],sk); scored.append(r)
    scored.sort(key=lambda x:(-x['score'],clean(x['name'])))
    for i,r in enumerate(scored,1): cur.execute('UPDATE tourney.entries SET seed=%s WHERE id=%s;',(i,r['entry_id']))
    order=1; work=list(scored)
    while len(work)>1:
        a=work.pop(0); idx=len(work)-1
        for j in range(len(work)-1,-1,-1):
            if work[j]['user_id']!=a['user_id']: idx=j; break
        b=work.pop(idx)
        cur.execute("INSERT INTO tourney.matches(guild_id,tournament_id,event_id,round_number,match_order,status,match_type) VALUES(%s,%s,%s,1,%s,'pending','head_to_head') RETURNING id;",(gid,t['id'],e['id'],order)); mid=cur.fetchone()['id']
        cur.execute('INSERT INTO tourney.match_participants(guild_id,match_id,entry_id,character_id,slot_number) VALUES(%s,%s,%s,%s,1);',(gid,mid,a['entry_id'],a['character_id']))
        cur.execute('INSERT INTO tourney.match_participants(guild_id,match_id,entry_id,character_id,slot_number) VALUES(%s,%s,%s,%s,2);',(gid,mid,b['entry_id'],b['character_id']))
        order+=1
    if work:
        a=work[0]; cur.execute("INSERT INTO tourney.matches(guild_id,tournament_id,event_id,round_number,match_order,status,match_type,winner_character_id,narrative_summary,completed_at) VALUES(%s,%s,%s,1,%s,'completed','bye',%s,'Automatic bye advancement.',NOW()) RETURNING id;",(gid,t['id'],e['id'],order,a['character_id'])); mid=cur.fetchone()['id']; cur.execute('INSERT INTO tourney.match_participants(guild_id,match_id,entry_id,character_id,slot_number,final_position) VALUES(%s,%s,%s,%s,1,1);',(gid,mid,a['entry_id'],a['character_id'])); cur.execute("UPDATE tourney.entries SET registration_status='advanced' WHERE id=%s;",(a['entry_id'],))
    cur.execute("UPDATE tourney.events SET status='active',round_number=1,updated_at=NOW() WHERE id=%s;",(e['id'],)); return True,''

def auto_advance(cur,gid,t,e):
    notes=[]
    while True:
        cur.execute('SELECT COALESCE(MAX(round_number),0) AS r FROM tourney.matches WHERE guild_id=%s AND event_id=%s;',(gid,e['id'])); rno=int(cur.fetchone()['r'] or 0)
        cur.execute("SELECT COUNT(*) FILTER(WHERE match_type='head_to_head') AS total, COUNT(*) FILTER(WHERE match_type='head_to_head' AND status='completed') AS done FROM tourney.matches WHERE guild_id=%s AND event_id=%s AND round_number=%s;",(gid,e['id'],rno)); st=cur.fetchone()
        if int(st['total'] or 0)==0 or int(st['total'] or 0)!=int(st['done'] or 0): return notes
        cur.execute('SELECT winner_character_id FROM tourney.matches WHERE guild_id=%s AND event_id=%s AND round_number=%s AND winner_character_id IS NOT NULL ORDER BY match_order,id;',(gid,e['id'],rno)); winners=[int(x['winner_character_id']) for x in cur.fetchall()]
        if len(winners)<=1:
            cur.execute("UPDATE tourney.events SET status='ready_to_finalize',updated_at=NOW() WHERE id=%s;",(e['id'],)); notes.append('Event is ready to finalize.'); return notes
        cur.execute('SELECT COUNT(*) AS n FROM tourney.matches WHERE guild_id=%s AND event_id=%s AND round_number=%s;',(gid,e['id'],rno+1))
        if int(cur.fetchone()['n'] or 0)>0: return notes
        entries=[]
        for cid in winners:
            cur.execute('SELECT id AS entry_id,character_id,user_id FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND character_id=%s LIMIT 1;',(gid,e['id'],cid)); rr=cur.fetchone()
            if rr: entries.append(dict(rr))
        order=1; work=entries
        while len(work)>1:
            a=work.pop(0); b=work.pop(-1); cur.execute("INSERT INTO tourney.matches(guild_id,tournament_id,event_id,round_number,match_order,status,match_type) VALUES(%s,%s,%s,%s,%s,'pending','head_to_head') RETURNING id;",(gid,t['id'],e['id'],rno+1,order)); mid=cur.fetchone()['id']; cur.execute('INSERT INTO tourney.match_participants(guild_id,match_id,entry_id,character_id,slot_number) VALUES(%s,%s,%s,%s,1);',(gid,mid,a['entry_id'],a['character_id'])); cur.execute('INSERT INTO tourney.match_participants(guild_id,match_id,entry_id,character_id,slot_number) VALUES(%s,%s,%s,%s,2);',(gid,mid,b['entry_id'],b['character_id'])); order+=1
        cur.execute('UPDATE tourney.events SET round_number=%s,status=%s WHERE id=%s;',(rno+1,'active',e['id'])); notes.append(f'Round {rno+1} generated automatically.')

def run_h2h(gid,tname,ename):
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tname); 
        if not t: return {'ok':False,'reason':'tournament_not_found'}
        e=get_e(cur,gid,t['id'],ename)
        if not e: return {'ok':False,'reason':'event_not_found'}
        ok,reason=init_h2h(cur,gid,t,e)
        if not ok: conn.rollback(); return {'ok':False,'reason':reason}
        cur.execute("SELECT * FROM tourney.matches WHERE guild_id=%s AND event_id=%s AND status IN ('pending','active') AND match_type='head_to_head' ORDER BY round_number,match_order,id LIMIT 1;",(gid,e['id'])); m=cur.fetchone()
        if not m: conn.commit(); return {'ok':False,'reason':'no_pending_match'}
        cur.execute('SELECT mp.*,c.name FROM tourney.match_participants mp JOIN public.characters c ON c.guild_id=mp.guild_id AND c.character_id=mp.character_id WHERE mp.match_id=%s ORDER BY slot_number;',(m['id'],)); ps=[dict(r) for r in cur.fetchall()]
        a,b=ps[0],ps[1]; ask=skills(gid,a['character_id']); bsk=skills(gid,b['character_id']); ap=bp=0; summary=[f"**{clean(a['name'])}** faces **{clean(b['name'])}** in the {EVENT_TYPES[e['event_type']][0]}."]
        for i in range(1,4):
            if e['event_type']=='joust': am=ask['riding']+ask['weapon']+ask['composure']//2; bm=bsk['riding']+bsk['weapon']+bsk['composure']//2; formula='2d6 + riding + weapon + composure_half'; unit='Pass'
            else: am=ask['duel']+ask['weapon']//2+ask['composure']//2; bm=bsk['duel']+bsk['weapon']//2+bsk['composure']//2; formula='2d6 + duel + weapon_half + composure_half'; unit='Exchange'
            ad1,ad2,abase=roll(); bd1,bd2,bbase=roll(); at=abase+am; bt=bbase+bm
            cur.execute('INSERT INTO tourney.match_rolls(guild_id,match_id,character_id,phase_code,roll_formula,die_1,die_2,base_total,modifier_total,rank_bonus,final_total,detail_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb);',(gid,m['id'],a['character_id'],f'round_{i}',formula,ad1,ad2,abase,am,ask['_bonus'],at,json.dumps({})))
            cur.execute('INSERT INTO tourney.match_rolls(guild_id,match_id,character_id,phase_code,roll_formula,die_1,die_2,base_total,modifier_total,rank_bonus,final_total,detail_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb);',(gid,m['id'],b['character_id'],f'round_{i}',formula,bd1,bd2,bbase,bm,bsk['_bonus'],bt,json.dumps({})))
            if at>bt: ap+=1; summary.append(f'{unit} {i}: **{clean(a["name"])}** takes the advantage.')
            elif bt>at: bp+=1; summary.append(f'{unit} {i}: **{clean(b["name"])}** answers with superior form.')
            else: summary.append(f'{unit} {i}: The competitors are evenly matched.')
        winner=a if (ap>bp or (ap==bp and seed_score(e['event_type'],ask)>=seed_score(e['event_type'],bsk))) else b; loser=b if winner is a else a
        summary.append(f"**{clean(winner['name']).upper()} advances!**")
        cur.execute("UPDATE tourney.matches SET status='completed',winner_character_id=%s,narrative_summary=%s,completed_at=NOW(),updated_at=NOW() WHERE id=%s;",(winner['character_id'],'\n'.join(summary),m['id']))
        cur.execute('UPDATE tourney.match_participants SET final_position=CASE WHEN character_id=%s THEN 1 ELSE 2 END, eliminated=CASE WHEN character_id=%s THEN FALSE ELSE TRUE END WHERE match_id=%s;',(winner['character_id'],winner['character_id'],m['id']))
        cur.execute("UPDATE tourney.entries SET registration_status='advanced' WHERE id=%s;",(winner['entry_id'],)); cur.execute("UPDATE tourney.entries SET registration_status='eliminated' WHERE id=%s;",(loser['entry_id'],))
        add_rp(cur,gid,winner['character_id'],1,match_wins=1); notes=auto_advance(cur,gid,t,e); conn.commit(); return {'ok':True,'event':dict(e),'summary':summary,'winner':winner,'loser':loser,'notes':notes}

def scored_points(total):
    if total>=18: return 5,'commanding'
    if total>=15: return 4,'excellent'
    if total>=12: return 3,'strong'
    if total>=9: return 2,'steady'
    return 0,'faltered'

def run_scored(gid,tname,ename):
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tname); 
        if not t: return {'ok':False,'reason':'tournament_not_found'}
        e=get_e(cur,gid,t['id'],ename)
        if not e: return {'ok':False,'reason':'event_not_found'}
        cur.execute("SELECT en.id AS entry_id,en.character_id,en.user_id,c.name FROM tourney.entries en JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id WHERE en.guild_id=%s AND en.event_id=%s AND en.registration_status IN ('registered','advanced') ORDER BY COALESCE(en.seed,999999),c.name;",(gid,e['id'])); entrants=[dict(r) for r in cur.fetchall()]
        if len(entrants)<2: return {'ok':False,'reason':'not_enough_entrants'}
        rno=int(e['round_number'] or 0)+1; cur.execute("INSERT INTO tourney.matches(guild_id,tournament_id,event_id,round_number,match_order,status,match_type) VALUES(%s,%s,%s,%s,1,'active','scored_round') RETURNING id;",(gid,t['id'],e['id'],rno)); mid=cur.fetchone()['id']; results=[]; summary=[f"**{EVENT_TYPES[e['event_type']][0]} Round {rno} begins.**"]
        for slot,en in enumerate(entrants,1):
            sk=skills(gid,en['character_id'])
            if e['event_type']=='archery': m=sk['archery']+sk['composure']//2; formula='2d6 + archery + composure_half'
            elif e['event_type']=='horse_race': m=sk['riding']+sk['stamina']//2+sk['composure']//3; formula='2d6 + riding + stamina_half + composure_third'
            elif e['event_type']=='hunt': m=sk['archery']+sk['stamina']//2+sk['composure']//3; formula='2d6 + archery + stamina_half + composure_third'
            else: m=sk['duel']+sk['stamina']//2+sk['weapon']//3; formula='2d6 + duel + stamina_half + weapon_third'
            pts=best=0
            for seg in range(1,4):
                d1,d2,base=roll(); total=base+m; p,label=scored_points(total); pts+=p; best=max(best,p); summary.append(f"{clean(en['name'])}'s segment {seg} is {label}."); cur.execute('INSERT INTO tourney.match_rolls(guild_id,match_id,character_id,phase_code,roll_formula,die_1,die_2,base_total,modifier_total,rank_bonus,final_total,detail_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb);',(gid,mid,en['character_id'],f'segment_{seg}',formula,d1,d2,base,m,sk['_bonus'],total,json.dumps({'points':p,'label':label})))
            cur.execute('INSERT INTO tourney.match_participants(guild_id,match_id,entry_id,character_id,slot_number,points) VALUES(%s,%s,%s,%s,%s,%s);',(gid,mid,en['entry_id'],en['character_id'],slot,pts)); results.append({'entry_id':en['entry_id'],'character_id':en['character_id'],'name':clean(en['name']),'points':pts,'best':best,'tie':seed_score(e['event_type'],sk)})
        results.sort(key=lambda x:(-x['points'],-x['best'],-x['tie'],x['name']))
        final=len(results)==2; advance=max(2,len(results)//2)
        for pos,r in enumerate(results,1):
            r['position']=pos; adv=pos<=advance
            cur.execute('UPDATE tourney.match_participants SET final_position=%s,eliminated=%s WHERE match_id=%s AND character_id=%s;',(pos,not adv,mid,r['character_id']))
            cur.execute('UPDATE tourney.entries SET registration_status=%s WHERE id=%s;',('advanced' if adv else 'eliminated',r['entry_id']))
            if adv and not final: add_rp(cur,gid,r['character_id'],1,match_wins=1)
        cur.execute("UPDATE tourney.events SET status=%s,round_number=%s,updated_at=NOW() WHERE id=%s;",('ready_to_finalize' if final else 'active',rno,e['id']))
        cur.execute("UPDATE tourney.matches SET status='completed',winner_character_id=%s,narrative_summary=%s,completed_at=NOW(),updated_at=NOW() WHERE id=%s;",(results[0]['character_id'],'\n'.join(summary),mid)); conn.commit(); return {'ok':True,'event':dict(e),'results':results,'summary':summary}

def finalize_event(gid,tname,ename,actor):
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tname); 
        if not t: return {'ok':False,'reason':'tournament_not_found'}
        e=get_e(cur,gid,t['id'],ename)
        if not e: return {'ok':False,'reason':'event_not_found'}
        if e['status']!='ready_to_finalize': return {'ok':False,'reason':'not_ready'}
        cur.execute("SELECT mp.character_id,c.name,mp.final_position FROM tourney.match_participants mp JOIN tourney.matches m ON m.id=mp.match_id JOIN public.characters c ON c.guild_id=mp.guild_id AND c.character_id=mp.character_id WHERE m.guild_id=%s AND m.event_id=%s ORDER BY m.round_number DESC,m.id DESC,mp.final_position ASC NULLS LAST LIMIT 10;",(gid,e['id'])); placements=[]
        seen=set()
        for r in cur.fetchall():
            if r['character_id'] not in seen: seen.add(r['character_id']); placements.append(dict(r))
        if not placements: return {'ok':False,'reason':'no_placements'}
        total_pay=0
        for idx,r in enumerate(placements[:3],1):
            pts={1:5,2:3,3:2}.get(idx,0); rp=0; code='third_place'; name='Third Place'; pay=0; counters={}
            if idx==1: rp=4; code='event_champion'; name=f"Champion of the {EVENT_TYPES[e['event_type']][0]}"; pay=EVENT_CHAMPION_PAY; counters={'event_championships':1}
            elif idx==2: rp=1; code='event_runner_up'; name=f"Runner-Up of the {EVENT_TYPES[e['event_type']][0]}"; pay=EVENT_RUNNER_PAY; counters={'event_runner_ups':1}
            add_rp(cur,gid,r['character_id'],rp,**counters); payout(cur,gid,r['character_id'],actor,pay,'tournament_event_payout',{'tournament':t['name'],'event':e['name'],'place':idx}); total_pay+=pay
            cur.execute('UPDATE tourney.entries SET tournament_score=tournament_score+%s,registration_status=%s WHERE guild_id=%s AND event_id=%s AND character_id=%s;',(pts,'champion' if idx==1 else 'runner_up' if idx==2 else 'placed',gid,e['id'],r['character_id']))
            cur.execute('INSERT INTO tourney.awards(guild_id,tournament_id,event_id,character_id,award_code,award_name,points_awarded,renown_awarded,payout_embers) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s);',(gid,t['id'],e['id'],r['character_id'],code,name,pts,rp,pay))
            queue_refresh(cur,gid,r['character_id'],'tournament_event_finalized')
        cur.execute("UPDATE tourney.events SET status='completed',updated_at=NOW() WHERE id=%s;",(e['id'],)); cur.execute("SELECT COUNT(*) AS n FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND status<>'completed';",(gid,t['id'])); open_n=int(cur.fetchone()['n'] or 0); cur.execute("UPDATE tourney.tournaments SET status=%s,updated_at=NOW() WHERE id=%s;",('ready_to_finalize' if open_n==0 else 'active',t['id'])); conn.commit(); return {'ok':True,'event':dict(e),'placements':placements,'total_pay':total_pay}

def finalize_tourney(gid,tname,actor):
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tname); 
        if not t: return {'ok':False,'reason':'tournament_not_found'}
        cur.execute("SELECT COUNT(*) AS n FROM tourney.events WHERE guild_id=%s AND tournament_id=%s AND status<>'completed';",(gid,t['id']))
        if int(cur.fetchone()['n'] or 0)>0: return {'ok':False,'reason':'events_not_completed'}
        cur.execute("SELECT en.character_id,c.name,COALESCE(SUM(en.tournament_score),0) AS score FROM tourney.entries en JOIN public.characters c ON c.guild_id=en.guild_id AND c.character_id=en.character_id WHERE en.guild_id=%s AND en.tournament_id=%s AND en.registration_status<>'withdrawn' GROUP BY en.character_id,c.name ORDER BY score DESC,c.name ASC;",(gid,t['id'])); rows=[dict(r) for r in cur.fetchall()]
        if not rows: return {'ok':False,'reason':'no_entries'}
        champ=rows[0]; pay=payout(cur,gid,champ['character_id'],actor,OVERALL_CHAMPION_PAY,'tournament_overall_champion_payout',{'tournament':t['name']}); add_rp(cur,gid,champ['character_id'],5,overall_championships=1); cur.execute('INSERT INTO tourney.awards(guild_id,tournament_id,character_id,award_code,award_name,points_awarded,renown_awarded,payout_embers) VALUES(%s,%s,%s,%s,%s,%s,%s,%s);',(gid,t['id'],champ['character_id'],'overall_champion',f"Overall Champion of {clean(t['name'])}",champ['score'],5,pay))
        cut=round(pay*0.10)
        if cut and t['host_kingdom'] in CANON_KINGDOMS: cur.execute('UPDATE econ.kingdoms SET treasury_embers=treasury_embers+%s,updated_at=NOW() WHERE guild_id=%s AND kingdom=%s;',(cut,gid,t['host_kingdom']))
        cur.execute("UPDATE tourney.tournaments SET status='completed',updated_at=NOW() WHERE id=%s;",(t['id'],)); conn.commit(); return {'ok':True,'champion':champ,'rows':rows,'pay':pay,'cut':cut}

@tree.command(name='tourney-run-round',description='Staff: run the next automated round/match and auto-post results.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourn_ac,event=event_ac)
async def run_round(inter,tournament:str,event:str):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID)
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tournament); e=get_e(cur,gid,t['id'],event) if t else None
    if not t or not e: return await inter.followup.send('Tournament or event not found.',ephemeral=True)
    res=await run_db(run_h2h if e['format_type']=='head_to_head' else run_scored,gid,tournament,event)
    if not res.get('ok'): return await inter.followup.send(f"Could not run round: `{clean(res.get('reason'))}`",ephemeral=True)
    emb=discord.Embed(title=f"{clean(event)} — Round Result",color=discord.Color.gold(),description=f"**Tournament:** {clean(tournament)}\n**Event:** {EVENT_TYPES[e['event_type']][0]}")
    if 'winner' in res: emb.add_field(name='Victor',value=f"**{clean(res['winner']['name'])}**",inline=True); emb.add_field(name='Defeated',value=f"**{clean(res['loser']['name'])}**",inline=True)
    else: emb.add_field(name='Standings',value='\n'.join([f"{r['position']}. **{r['name']}** — {r['points']} pts" for r in res['results'][:10]]),inline=False)
    for i,ch in enumerate(chunk(res.get('summary',[])[:20]),1): emb.add_field(name='Narration' if i==1 else f'Narration ({i})',value=ch,inline=False)
    await public_post(inter,emb); await inter.followup.send('Round resolved and posted publicly.',ephemeral=True)

@tree.command(name='tourney-finalize-event',description='Staff: finalize an event.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourn_ac,event=event_ac)
async def fin_event(inter,tournament:str,event:str):
    await inter.response.defer(ephemeral=True); res=await run_db(finalize_event,int(inter.guild_id or GUILD_ID),tournament,event,inter.user.id)
    if not res.get('ok'): return await inter.followup.send(f"Could not finalize event: `{clean(res.get('reason'))}`",ephemeral=True)
    emb=discord.Embed(title=f'{clean(event)} — Final Results',color=discord.Color.gold(),description=f'**Tournament:** {clean(tournament)}')
    for i,r in enumerate(res['placements'][:3],1): emb.add_field(name={1:'Champion',2:'Runner-Up',3:'Third Place'}[i],value=f"**{clean(r['name'])}**",inline=True)
    await public_post(inter,emb); await inter.followup.send('Event finalized and posted publicly.',ephemeral=True)

@tree.command(name='tourney-finalize',description='Staff: finalize a completed tournament.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourn_ac)
async def fin_tourney(inter,tournament:str):
    await inter.response.defer(ephemeral=True); res=await run_db(finalize_tourney,int(inter.guild_id or GUILD_ID),tournament,inter.user.id)
    if not res.get('ok'): return await inter.followup.send(f"Could not finalize tournament: `{clean(res.get('reason'))}`",ephemeral=True)
    emb=discord.Embed(title=f'{clean(tournament)} — Overall Champion',color=discord.Color.gold(),description=f"**{clean(res['champion']['name'])}** is crowned overall champion.")
    emb.add_field(name='Final Standings',value='\n'.join([f"{i}. **{clean(r['name'])}** — {int(r['score'])} pts" for i,r in enumerate(res['rows'][:10],1)]),inline=False)
    if res['pay']: emb.add_field(name='Payout',value=f"{fmt_money(res['pay'])}\nHost treasury cut: {fmt_money(res['cut'])}",inline=False)
    await public_post(inter,emb); await inter.followup.send('Tournament finalized and posted publicly.',ephemeral=True)

@tree.command(name='tourney-post-announcement',description='Staff: post public tournament announcement.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourn_ac)
async def announcement(inter,tournament:str):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID)
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tournament)
        if not t: return await inter.followup.send('Tournament not found.',ephemeral=True)
        cur.execute('SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s ORDER BY id;',(gid,t['id'])); events=[dict(r) for r in cur.fetchall()]
    emb=discord.Embed(title=f"{clean(t['name'])} — Tournament Announcement",color=discord.Color.gold(),description='The lists are called, the field is marked, and competitors may gather for glory.')
    emb.add_field(name='Host Kingdom',value=clean(t['host_kingdom'] or 'Unassigned'),inline=True); emb.add_field(name='Status',value=clean(t['status']).title(),inline=True)
    if events: emb.add_field(name='Events',value='\n'.join([f"• **{clean(e['name'])}** — {EVENT_TYPES[e['event_type']][0]}" for e in events])[:1024],inline=False)
    await public_post(inter,emb); await inter.followup.send('Announcement posted publicly.',ephemeral=True)

@tree.command(name='tourney-status',description='Staff: view tournament status.',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@staff_only()
@app_commands.autocomplete(tournament=tourn_ac)
async def status(inter,tournament:str):
    await inter.response.defer(ephemeral=True); gid=int(inter.guild_id or GUILD_ID)
    with db() as conn, conn.cursor() as cur:
        t=get_t(cur,gid,tournament)
        if not t: return await inter.followup.send('Tournament not found.',ephemeral=True)
        cur.execute('SELECT * FROM tourney.events WHERE guild_id=%s AND tournament_id=%s ORDER BY id;',(gid,t['id'])); events=[dict(r) for r in cur.fetchall()]
        lines=[]
        for e in events:
            cur.execute("SELECT COUNT(*) AS n FROM tourney.entries WHERE guild_id=%s AND event_id=%s AND registration_status<>'withdrawn';",(gid,e['id'])); n=cur.fetchone()['n']; lines.append(f"• **{clean(e['name'])}** — {EVENT_TYPES[e['event_type']][0]} | {clean(e['status']).title()} | Entrants {n} | Round {e['round_number']}")
    emb=discord.Embed(title=f"{clean(t['name'])} — Tournament Status",color=discord.Color.blue(),description=f"**Host:** {clean(t['host_kingdom'])}\n**Status:** {clean(t['status']).title()}")
    for i,ch in enumerate(chunk(lines or ['No events created yet.']),1): emb.add_field(name='Events' if i==1 else f'Events ({i})',value=ch,inline=False)
    await inter.followup.send(embed=emb,ephemeral=True)

@tree.error
async def err(inter,error):
    traceback.print_exception(type(error),error,error.__traceback__)
    try:
        if inter.response.is_done(): await inter.followup.send('TournamentBot hit an internal error. Check Railway logs.',ephemeral=True)
        else: await inter.response.send_message('TournamentBot hit an internal error. Check Railway logs.',ephemeral=True)
    except Exception: pass
@client.event
async def on_ready():
    log.info('%s logged in as %s',APP_VERSION,client.user)
    try: await run_db(ensure_schema); log.info('Tournament schema ensured.'); log.info('Character sync: %s', await run_db(sync_chars,GUILD_ID))
    except Exception: log.exception('Startup schema/sync failed')
    try: synced=await tree.sync(guild=discord.Object(id=GUILD_ID)); log.info('Synced %s guild command(s): %s',len(synced),sorted(c.name for c in synced))
    except Exception: log.exception('Command sync failed')
def main(): client.run(DISCORD_TOKEN)
if __name__=='__main__': main()
