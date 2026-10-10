"""
Recommendation bot
  /recommendation  - recommend a player (needs the Player role), 3 recommenders -> admin review
  /info            - Roblox + server info about a player (via Bloxlink)
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reco")

# ───────────────────────── CONFIG (всё берётся из .env) ─────────────────────────
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
PLAYER_ROLE_ID = int(os.getenv("PLAYER_ROLE_ID", "1555169634192199770"))  # кто может рекомендовать
ADMIN_CHANNEL_ID = int(os.environ["ADMIN_CHANNEL_ID"])                    # куда падают заявки
REVIEWER_ROLE_ID = int(os.environ["REVIEWER_ROLE_ID"])                    # роль "Bot Perms": пингуется + может жать кнопки
BLOXLINK_API_KEY = os.getenv("BLOXLINK_API_KEY", "")                      # ключ с https://blox.link/dashboard
GAME_API_URL = os.getenv("GAME_API_URL", "")                              # (необязательно) API игры: часы / shards

REQUIRED = int(os.getenv("REQUIRED", "3"))  # сколько рекомендаций нужно для заявки (для теста поставь 1)
GIVE_ROLE_ON_APPROVE = True     # выдавать роль Player при Approve
COOLDOWN_SECONDS = 2 * 24 * 3600  # кулдаун между рекомендациями одного человека (0 = выключить)
PURPLE, GREEN, RED = 0x9B59F7, 0x2ECC71, 0xED4245
GUILD = discord.Object(GUILD_ID)


def load_links():
    """LINK_1=Название|https://ссылка ... LINK_10 -> [(название, ссылка)]"""
    keys = sorted((k for k in os.environ if re.fullmatch(r"LINK_\d+", k)), key=lambda k: int(k[5:]))
    out = []
    for k in keys:
        name, sep, url = os.environ[k].partition("|")
        name, url = name.strip(), url.strip()
        if sep and name and url.startswith(("http://", "https://")):
            out.append((name, url))
    return out[:25]


LINKS = load_links()

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_discord_id INTEGER NOT NULL,
    target_roblox_id INTEGER NOT NULL,
    target_roblox_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    note TEXT,
    message_id INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_discord_id INTEGER NOT NULL,
    target_roblox_id INTEGER NOT NULL,
    recommender_discord_id INTEGER NOT NULL,
    recommender_roblox_id INTEGER,
    recommender_roblox_name TEXT NOT NULL,
    hours INTEGER,
    created_at INTEGER NOT NULL,
    application_id INTEGER
);
"""


# ───────────────────────── helpers ─────────────────────────
def profile_url(rid) -> str:
    return f"https://www.roblox.com/users/{rid}/profile"


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def has_role(member, role_id: int) -> bool:
    return any(r.id == role_id for r in getattr(member, "roles", []))


async def dm(user_id: int, embed: discord.Embed):
    """Отправить эмбед в ЛС. Если ЛС закрыты - молча пропускаем."""
    try:
        u = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await u.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.info("couldn't DM %s (DMs closed?)", user_id)


def as_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_ts(s: Optional[str]) -> Optional[int]:
    try:
        base = s.split(".")[0].rstrip("Z")
        return int(datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


class Web:
    """Roblox / Bloxlink / game API."""

    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def _json(self, method: str, url: str, **kw):
        try:
            async with self.s.request(method, url, timeout=aiohttp.ClientTimeout(total=10), **kw) as r:
                if r.status != 200:
                    log.warning("%s %s -> %s", method, url, r.status)
                    return None
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("request failed: %s %s", method, url)
            return None

    # --- Roblox ---
    async def user_by_id(self, uid) -> Optional[dict]:
        return await self._json("GET", f"https://users.roblox.com/v1/users/{uid}")

    async def user_by_name(self, name: str) -> Optional[dict]:
        data = await self._json(
            "POST", "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [name], "excludeBannedUsers": False},
        )
        if data and data.get("data"):
            return await self.user_by_id(data["data"][0]["id"])
        return None

    async def resolve(self, query: str) -> Optional[dict]:
        """ID или ник -> профиль Roblox."""
        query = query.strip()
        return await (self.user_by_id(query) if query.isdigit() else self.user_by_name(query))

    async def avatar(self, uid) -> Optional[str]:
        data = await self._json(
            "GET", "https://thumbnails.roblox.com/v1/users/avatar",
            params={"userIds": uid, "size": "420x420", "format": "Png", "isCircular": "false"},
        )
        try:
            return data["data"][0].get("imageUrl")
        except (TypeError, KeyError, IndexError):
            return None

    # --- Bloxlink (guild API) ---
    async def bloxlink(self, discord_id: int) -> Optional[int]:
        if not BLOXLINK_API_KEY:
            return None
        data = await self._json(
            "GET", f"https://api.blox.link/v4/public/guilds/{GUILD_ID}/discord-to-roblox/{discord_id}",
            headers={"Authorization": BLOXLINK_API_KEY},
        )
        return as_int((data or {}).get("robloxID"))

    async def bloxlink_reverse(self, roblox_id: int) -> Optional[int]:
        if not BLOXLINK_API_KEY:
            return None
        data = await self._json(
            "GET", f"https://api.blox.link/v4/public/guilds/{GUILD_ID}/roblox-to-discord/{roblox_id}",
            headers={"Authorization": BLOXLINK_API_KEY},
        )
        ids = (data or {}).get("discordIDs") or []
        return as_int(ids[0]) if ids else None

    # --- твоя игра (необязательно): GET GAME_API_URL?robloxId=123 -> {"hours": 488, "shards": 75} ---
    async def game_stats(self, roblox_id: int) -> dict:
        if not GAME_API_URL:
            return {}
        return await self._json("GET", GAME_API_URL, params={"robloxId": roblox_id}) or {}


# ───────────────────────── embeds ─────────────────────────
def rec_line(r) -> str:
    if r["recommender_roblox_id"]:
        name = f"[{r['recommender_roblox_name']}]({profile_url(r['recommender_roblox_id'])})"
    else:
        name = r["recommender_roblox_name"]
    parts = [name]
    if r["hours"] is not None:
        parts.append(f"`{r['hours']}h`")
    parts.append(f"<t:{r['created_at']}:R>")
    return " • ".join(parts)


def full_embed(app, recs, avatar: Optional[str]) -> discord.Embed:
    e = discord.Embed(
        title=f"Recommendation — {app['target_roblox_name']}",
        color=PURPLE,
        timestamp=datetime.fromtimestamp(app["created_at"], timezone.utc),
    )
    e.add_field(
        name="Player",
        value=f"[{app['target_roblox_name']}]({profile_url(app['target_roblox_id'])})\n`{app['target_roblox_id']}`",
        inline=False,
    )
    e.add_field(
        name=f"Recommended By ({len(recs)})",
        value="\n".join(rec_line(r) for r in recs) or "Unknown",
        inline=False,
    )
    e.add_field(name="Status", value="⏳ Pending review", inline=False)
    e.set_footer(text=f"Full recommendation #{app['id']}")
    if avatar:
        e.set_thumbnail(url=avatar)
    return e


def partial_embed(target: discord.Member, target_rb: dict, me: discord.Member, me_rb: Optional[dict],
                  hours: Optional[int], shards: Optional[int], count: int, avatar: Optional[str],
                  title: Optional[str] = None) -> discord.Embed:
    role = target.top_role.name if target.top_role != target.guild.default_role else "No role"
    e = discord.Embed(
        title=title or ("Recommendation Complete" if count >= REQUIRED else "Partial Recommendation"),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(
        name="Player",
        value=f"[{target_rb['name']}]({profile_url(target_rb['id'])})\n`{target_rb['id']}` • {role}",
        inline=True,
    )
    if me_rb:
        who = f"[{me_rb['name']}]({profile_url(me_rb['id'])})\n`{me_rb['id']}`"
    else:
        who = f"{me.display_name}\n`{me.id}`"
    if hours is not None:
        who += f" • `{hours}h played`"
    e.add_field(name="Recommended By", value=who, inline=True)
    e.add_field(name="Progress", value=f"```\n{count}/{REQUIRED} recommenders\n```", inline=False)
    e.set_footer(text=f"{shards} Shards paid in-game" if shards is not None else "Partial recommendation")
    if avatar:
        e.set_thumbnail(url=avatar)
    return e


# ───────────────────────── bot ─────────────────────────
class RecoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # включи "Server Members Intent" в Developer Portal
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.web = Web(self.session)
        self.lock = asyncio.Lock()
        self.db = await aiosqlite.connect("reco.db")
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        self.add_view(ReviewView())  # кнопки работают и после перезапуска бота
        await self.tree.sync(guild=GUILD)

    async def close(self):
        await self.session.close()
        await self.db.close()
        await super().close()


bot = RecoBot()


# ───────────────────────── Approve / Deny ─────────────────────────
class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _decide(self, interaction: discord.Interaction, approved: bool):
        m = interaction.user
        if not (m.guild_permissions.administrator or has_role(m, REVIEWER_ROLE_ID)):
            return await interaction.response.send_message(
                "❌ You don't have permission to review recommendations.", ephemeral=True)

        async with bot.lock:
            cur = await bot.db.execute("SELECT * FROM applications WHERE message_id=?", (interaction.message.id,))
            app = await cur.fetchone()
            if not app or app["status"] != "pending":
                return await interaction.response.send_message(
                    "❌ This recommendation was already handled.", ephemeral=True)
            note = "Ranked to Players." if approved else "Denied"
            await bot.db.execute(
                "UPDATE applications SET status=?, decided_by=?, note=? WHERE id=?",
                ("approved" if approved else "denied", m.name, note, app["id"]),
            )
            await bot.db.commit()

        embed = interaction.message.embeds[0]
        embed.color = GREEN if approved else RED
        embed.set_field_at(
            2, name="Status",
            value=f"{'✅ Approved' if approved else '❌ Denied'}\nby {m.name}", inline=False,
        )
        embed.add_field(name="Note", value=note, inline=False)
        await interaction.response.edit_message(embed=embed, view=None)

        # ЛС игроку с решением (без имени проверяющего)
        d = embed.copy()
        d.set_field_at(2, name="Status", value="✅ Approved" if approved else "❌ Denied", inline=False)
        d.description = (f"🎉 Your recommendation in **{interaction.guild.name}** was approved!" if approved
                         else f"Your recommendation in **{interaction.guild.name}** was denied.")
        await dm(app["target_discord_id"], d)

        if approved and GIVE_ROLE_ON_APPROVE:
            member = interaction.guild.get_member(app["target_discord_id"])
            role = interaction.guild.get_role(PLAYER_ROLE_ID)
            try:
                if not member:
                    raise LookupError("member left the server")
                await member.add_roles(role, reason=f"Recommendation #{app['id']} approved by {m}")
            except Exception as ex:
                log.warning("couldn't give role: %s", ex)
                await interaction.followup.send(
                    f"⚠️ Approved, but I couldn't give the Player role automatically ({ex}).", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.secondary, custom_id="reco:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary, custom_id="reco:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, False)


async def submit_application(target: discord.Member, rb: dict, avatar: Optional[str]):
    """3/3 набрано -> заявка в админ-канал. Коммит делает вызывающий код."""
    db = bot.db
    cur = await db.execute(
        "INSERT INTO applications(target_discord_id, target_roblox_id, target_roblox_name, created_at) VALUES (?,?,?,?)",
        (target.id, rb["id"], rb["name"], now_ts()),
    )
    app_id = cur.lastrowid
    await db.execute(
        "UPDATE recommendations SET application_id=? WHERE target_discord_id=? AND application_id IS NULL",
        (app_id, target.id),
    )
    recs = await (await db.execute(
        "SELECT * FROM recommendations WHERE application_id=? ORDER BY created_at", (app_id,))).fetchall()
    app = await (await db.execute("SELECT * FROM applications WHERE id=?", (app_id,))).fetchone()

    channel = bot.get_channel(ADMIN_CHANNEL_ID) or await bot.fetch_channel(ADMIN_CHANNEL_ID)
    pings = f"<@&{REVIEWER_ROLE_ID}> <@{target.id}> " + " ".join(f"<@{r['recommender_discord_id']}>" for r in recs)
    embed = full_embed(app, recs, avatar)
    msg = await channel.send(
        content=pings,
        embed=embed,
        view=ReviewView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )
    await db.execute("UPDATE applications SET message_id=? WHERE id=?", (msg.id, app_id))
    return embed


# ───────────────────────── /recommendation ─────────────────────────
@bot.tree.command(name="recommendation", description="Recommend a player for the Player rank", guild=GUILD)
@app_commands.describe(
    user="The Discord user you are recommending",
    roblox="Their Roblox profile ID or username (used for the avatar)",
)
async def recommendation(interaction: discord.Interaction, user: discord.Member, roblox: Optional[str] = None):
    me = interaction.user
    if not has_role(me, PLAYER_ROLE_ID):
        return await interaction.response.send_message(
            "❌ Only members with the Player role can submit recommendations.", ephemeral=True)
    if user.bot or user.id == me.id:
        return await interaction.response.send_message(
            "❌ You can't recommend yourself or a bot.", ephemeral=True)
    if has_role(user, PLAYER_ROLE_ID):
        return await interaction.response.send_message(
            "❌ This user already has the Player role.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    async def fail(msg: str):
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)

    db = bot.db

    # --- кулдаун ---
    last = (await (await db.execute(
        "SELECT MAX(created_at) FROM recommendations WHERE recommender_discord_id=?", (me.id,))).fetchone())[0]
    if last and now_ts() < last + COOLDOWN_SECONDS:
        return await fail(f"Cooldown: you can recommend again <t:{last + COOLDOWN_SECONDS}:R>.")

    # --- Roblox-аккаунт того, кого рекомендуют ---
    prev = await (await db.execute(
        "SELECT target_roblox_id FROM recommendations WHERE target_discord_id=? AND application_id IS NULL LIMIT 1",
        (user.id,))).fetchone()
    if roblox:
        target_rb = await bot.web.resolve(roblox)
    elif prev:
        target_rb = await bot.web.user_by_id(prev["target_roblox_id"])
    else:
        rid = await bot.web.bloxlink(user.id)
        target_rb = await bot.web.user_by_id(rid) if rid else None
    if not target_rb:
        return await fail("Couldn't find that Roblox account. Enter a valid Roblox profile ID or username.")
    if prev and prev["target_roblox_id"] != target_rb["id"]:
        return await fail(
            f"Earlier recommendations for this player used Roblox ID `{prev['target_roblox_id']}`. Use the same one.")

    # --- Roblox-аккаунт рекомендующего + статы из игры ---
    rid = await bot.web.bloxlink(me.id)
    me_rb = await bot.web.user_by_id(rid) if rid else None
    stats = await bot.web.game_stats(me_rb["id"]) if me_rb else {}
    hours, shards = as_int(stats.get("hours")), as_int(stats.get("shards"))
    avatar = await bot.web.avatar(target_rb["id"])

    full = None
    try:
        async with bot.lock:
            if await (await db.execute(
                    "SELECT 1 FROM applications WHERE target_discord_id=? AND status='pending'", (user.id,))).fetchone():
                return await fail("This player already has a recommendation pending review.")
            if await (await db.execute(
                    "SELECT 1 FROM recommendations WHERE target_discord_id=? AND recommender_discord_id=? "
                    "AND application_id IS NULL", (user.id, me.id))).fetchone():
                return await fail("You've already recommended this player.")

            await db.execute(
                "INSERT INTO recommendations(target_discord_id, target_roblox_id, recommender_discord_id, "
                "recommender_roblox_id, recommender_roblox_name, hours, created_at) VALUES (?,?,?,?,?,?,?)",
                (user.id, target_rb["id"], me.id, me_rb["id"] if me_rb else None,
                 me_rb["name"] if me_rb else me.display_name, hours, now_ts()),
            )
            count = (await (await db.execute(
                "SELECT COUNT(*) FROM recommendations WHERE target_discord_id=? AND application_id IS NULL",
                (user.id,))).fetchone())[0]
            if count >= REQUIRED:
                full = await submit_application(user, target_rb, avatar)
            await db.commit()
    except Exception:
        await db.rollback()
        log.exception("recommendation failed")
        return await fail("Something went wrong (check ADMIN_CHANNEL_ID and my permissions in that channel).")

    await interaction.followup.send("✅ Recommendation submitted.", ephemeral=True)
    await interaction.followup.send(
        content=f"📨 {REQUIRED}/{REQUIRED} reached — sent to staff for review." if count >= REQUIRED else None,
        embed=partial_embed(user, target_rb, me, me_rb, hours, shards, count, avatar),
    )

    # --- ЛС тому, кого рекомендовали ---
    gname = interaction.guild.name
    if full:  # набрано 3/3 -> заявка ушла админам
        d = full.copy()
        d.description = (f"Your recommendation in **{gname}** is complete "
                         f"({REQUIRED}/{REQUIRED}) and is now waiting for staff review.")
    else:
        d = partial_embed(user, target_rb, me, me_rb, hours, shards, count, avatar, title="You've Been Recommended")
        d.description = f"You were recommended in **{gname}**."
    await dm(user.id, d)


# ───────────────────────── /info ─────────────────────────
@bot.tree.command(name="info", description="Show a player's Roblox and server info", guild=GUILD)
@app_commands.describe(
    user="Discord user (default: you)",
    roblox="Roblox username or ID (instead of a Discord user)",
)
async def info(interaction: discord.Interaction, user: Optional[discord.Member] = None,
               roblox: Optional[str] = None):
    await interaction.response.defer()
    rb, member = None, None

    if roblox:
        rb = await bot.web.resolve(roblox)
        if not rb:
            return await interaction.followup.send("❌ Roblox account not found.")
        did = await bot.web.bloxlink_reverse(rb["id"])
        member = interaction.guild.get_member(did) if did else None
    else:
        member = user or interaction.user
        rid = await bot.web.bloxlink(member.id)
        rb = await bot.web.user_by_id(rid) if rid else None

    e = discord.Embed(
        title=f"Info — {rb['name'] if rb else member.display_name}",
        color=PURPLE, timestamp=datetime.now(timezone.utc),
    )
    if rb:
        e.add_field(name="Player", value=f"[{rb['name']}]({profile_url(rb['id'])})\n`{rb['id']}`", inline=True)
        e.add_field(name="Display Name", value=rb.get("displayName") or rb["name"], inline=True)
        created = parse_ts(rb.get("created"))
        if created:
            e.add_field(name="Account Created", value=f"<t:{created}:D> • <t:{created}:R>", inline=False)
        if avatar := await bot.web.avatar(rb["id"]):
            e.set_thumbnail(url=avatar)
    else:
        e.add_field(name="Player", value="Not linked with Bloxlink", inline=False)

    if member:
        roles = [r.mention for r in reversed(member.roles) if r != member.guild.default_role]
        e.add_field(name="Discord", value=f"{member.mention}\n`{member.id}`", inline=True)
        e.add_field(name="Top Role", value=roles[0] if roles else "None", inline=True)
        e.add_field(name="Server Roles", value=" ".join(roles[:20]) or "None", inline=False)

        open_cnt = (await (await bot.db.execute(
            "SELECT COUNT(*) FROM recommendations WHERE target_discord_id=? AND application_id IS NULL",
            (member.id,))).fetchone())[0]
        last = await (await bot.db.execute(
            "SELECT id, status FROM applications WHERE target_discord_id=? ORDER BY id DESC LIMIT 1",
            (member.id,))).fetchone()
        if open_cnt:
            reco = f"`{open_cnt}/{REQUIRED} recommenders`"
        elif last:
            icon = {"pending": "⏳", "approved": "✅", "denied": "❌"}[last["status"]]
            reco = f"{icon} {last['status'].title()} (#{last['id']})"
        else:
            reco = "None"
        e.add_field(name="Recommendations", value=reco, inline=False)
    else:
        e.add_field(name="Discord", value="Not in this server / not linked", inline=False)

    await interaction.followup.send(embed=e)


# ───────────────────────── /links ─────────────────────────
@bot.tree.command(name="links", description="Useful links", guild=GUILD)
async def links(interaction: discord.Interaction):
    e = discord.Embed(title="Links", color=PURPLE, timestamp=datetime.now(timezone.utc))
    if not LINKS:
        e.description = "No links configured yet."
    for name, url in LINKS:
        e.add_field(name=name, value=f"[Open]({url})", inline=False)
    if interaction.guild and interaction.guild.icon:
        e.set_thumbnail(url=interaction.guild.icon.url)
    await interaction.response.send_message(embed=e)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("command error", exc_info=error)
    msg = "❌ Unexpected error."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
