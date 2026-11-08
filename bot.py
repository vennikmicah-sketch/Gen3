"""
Stock / generator Discord bot (discord.py 2.x, slash commands, SQLite storage).

Admins create "services", restock them with items (codes, keys, prizes, etc.),
and members run /gen to receive one item by DM, subject to cooldowns, optional
role requirements and a blacklist.
"""
import asyncio
import io
import os
import re
import sqlite3
import time
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "gen.db")
DEFAULT_COOLDOWN = int(os.getenv("DEFAULT_COOLDOWN", "3600"))  # seconds


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS services (
                name TEXT PRIMARY KEY,
                cooldown INTEGER NOT NULL,
                required_role_id INTEGER,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                item TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gen_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_stock_service ON stock(service);
            CREATE INDEX IF NOT EXISTS idx_log_user ON gen_log(user_id, service);
            CREATE TABLE IF NOT EXISTS premium (
                user_id INTEGER PRIMARY KEY,
                expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bonus (
                user_id INTEGER PRIMARY KEY,
                extra INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vouches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                rating INTEGER NOT NULL,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                ts INTEGER NOT NULL
            );
            """
        )
        for stmt in (
            "ALTER TABLE services ADD COLUMN description TEXT",
            "ALTER TABLE services ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE services ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE services ADD COLUMN max_per_user INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE services ADD COLUMN low_threshold INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur


db = DB(DB_PATH)


def get_setting(key: str, default=None):
    row = db.one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value):
    db.run("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def is_premium(member) -> bool:
    if db.one("SELECT 1 FROM premium WHERE user_id=? AND expires>?", (member.id, int(time.time()))):
        return True
    rid = get_setting("premium_role")
    return bool(rid) and any(r.id == int(rid) for r in getattr(member, "roles", []))


def embed_color() -> discord.Color:
    try:
        return discord.Color(int(get_setting("color", "5865F2"), 16))
    except ValueError:
        return discord.Color.blurple()


# --------------------------------------------------------------------------- #
# Bot setup
# --------------------------------------------------------------------------- #
class GenBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.started = time.time()

    async def setup_hook(self):
        await self.tree.sync()


bot = GenBot()
admin_only = app_commands.checks.has_permissions(administrator=True)


def norm(name: str) -> str:
    return name.strip().lower()


def fmt_time(seconds: int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "none"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts)


async def service_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.q("SELECT name FROM services WHERE name LIKE ? ORDER BY name LIMIT 25",
                (f"%{current.lower()}%",))
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


async def send_log(guild: Optional[discord.Guild], embed: discord.Embed):
    row = db.one("SELECT value FROM settings WHERE key='log_channel'")
    if not row or not guild:
        return
    channel = guild.get_channel(int(row["value"]))
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game("/gen | /help"))
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("Invite:", discord.utils.oauth_url(
        bot.user.id,
        permissions=discord.Permissions(send_messages=True, embed_links=True),
        scopes=("bot", "applications.commands"),
    ))


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need **Administrator** to use this command."
    else:
        msg = f"Something went wrong: `{error}`"
        print("Command error:", repr(error))
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# --------------------------------------------------------------------------- #
# Member commands
# --------------------------------------------------------------------------- #
@bot.tree.command(name="gen", description="Generate an item from a service")
@app_commands.describe(service="Which service to generate from")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.guild_only()
async def gen(interaction: discord.Interaction, service: str):
    await interaction.response.defer(ephemeral=True)
    service = norm(service)
    uid = interaction.user.id

    if db.one("SELECT 1 FROM blacklist WHERE user_id=?", (uid,)):
        return await interaction.followup.send("You are blacklisted from using this bot.", ephemeral=True)

    svc = db.one("SELECT * FROM services WHERE name=?", (service,))
    if not svc:
        return await interaction.followup.send(f"Service `{service}` doesn't exist. Try `/services`.", ephemeral=True)

    if get_setting("maintenance", "0") == "1":
        return await interaction.followup.send("The generator is in maintenance mode. Try again later.", ephemeral=True)
    if not svc["enabled"]:
        return await interaction.followup.send("That service is currently disabled.", ephemeral=True)

    if svc["required_role_id"]:
        if not any(r.id == svc["required_role_id"] for r in interaction.user.roles):
            return await interaction.followup.send(
                f"You need the <@&{svc['required_role_id']}> role for this service.", ephemeral=True)

    premium = is_premium(interaction.user)
    now = int(time.time())

    limit = int(get_setting("premium_limit" if premium else "daily_limit", 0))
    if limit > 0:
        bonus = db.one("SELECT extra FROM bonus WHERE user_id=?", (uid,))
        limit += bonus["extra"] if bonus else 0
        used = db.one("SELECT COUNT(*) AS n FROM gen_log WHERE user_id=? AND ts>?", (uid, now - 86400))["n"]
        if used >= limit:
            return await interaction.followup.send(f"Daily limit reached ({limit} per 24h).", ephemeral=True)

    if svc["max_per_user"] > 0:
        got = db.one("SELECT COUNT(*) AS n FROM gen_log WHERE user_id=? AND service=?", (uid, service))["n"]
        if got >= svc["max_per_user"]:
            return await interaction.followup.send(
                f"You've reached the limit of {svc['max_per_user']} for this service.", ephemeral=True)

    cd = svc["cooldown"] * (int(get_setting("premium_mult", 50)) / 100 if premium else 1)
    reset_ts = int(get_setting("cd_reset_ts", 0))
    last = db.one("SELECT MAX(ts) AS t FROM gen_log WHERE user_id=? AND service=?", (uid, service))["t"]
    if last and cd > 0 and last > reset_ts:
        remaining = cd - (now - last)
        if remaining > 0:
            return await interaction.followup.send(
                f"Cooldown active. Try again in **{fmt_time(remaining)}**.", ephemeral=True)

    async with db.lock:
        row = db.one("SELECT id, item FROM stock WHERE service=? ORDER BY id LIMIT 1", (service,))
        if not row:
            return await interaction.followup.send(f"`{service}` is out of stock.", ephemeral=True)
        db.run("DELETE FROM stock WHERE id=?", (row["id"],))

    embed = discord.Embed(title=f"{service.title()} item", description=f"```{row['item']}```",
                          color=embed_color())
    embed.set_footer(text=get_setting("footer", "Keep this private."))
    try:
        await interaction.user.send(embed=embed)
    except discord.Forbidden:
        # DMs closed: put the item back at the front of the queue
        async with db.lock:
            db.run("INSERT INTO stock (id, service, item) VALUES (?, ?, ?)", (row["id"], service, row["item"]))
        return await interaction.followup.send(
            "I couldn't DM you. Enable DMs from server members and try again.", ephemeral=True)

    db.run("INSERT INTO gen_log (user_id, service, ts) VALUES (?, ?, ?)", (uid, service, int(time.time())))
    await interaction.followup.send("Sent! Check your DMs.", ephemeral=True)

    log = discord.Embed(title="Item generated", color=discord.Color.blurple(),
                        description=f"{interaction.user.mention} generated **{service}**")
    await send_log(interaction.guild, log)

    left = db.one("SELECT COUNT(*) AS n FROM stock WHERE service=?", (service,))["n"]
    if svc["low_threshold"] > 0 and left <= svc["low_threshold"]:
        await send_log(interaction.guild, discord.Embed(
            title="Low stock", color=discord.Color.orange(),
            description=f"**{service}** has only **{left}** left."))


@bot.tree.command(name="services", description="List all services and their status")
async def services(interaction: discord.Interaction):
    rows = db.q("""SELECT s.name, s.cooldown, s.required_role_id,
                   (SELECT COUNT(*) FROM stock k WHERE k.service = s.name) AS n
                   FROM services s WHERE s.hidden=0 ORDER BY s.name""")
    if not rows:
        return await interaction.response.send_message("No services yet.", ephemeral=True)
    embed = discord.Embed(title="Services", color=discord.Color.blurple())
    for r in rows:
        role = f" | role: <@&{r['required_role_id']}>" if r["required_role_id"] else ""
        embed.add_field(name=r["name"].title(),
                        value=f"Stock: **{r['n']}** | Cooldown: {fmt_time(r['cooldown'])}{role}",
                        inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stock", description="Show stock for one service or all")
@app_commands.autocomplete(service=service_autocomplete)
async def stock(interaction: discord.Interaction, service: Optional[str] = None):
    if service:
        service = norm(service)
        if not db.one("SELECT 1 FROM services WHERE name=?", (service,)):
            return await interaction.response.send_message("Unknown service.", ephemeral=True)
        n = db.one("SELECT COUNT(*) AS n FROM stock WHERE service=?", (service,))["n"]
        return await interaction.response.send_message(f"**{service.title()}**: {n} in stock")
    rows = db.q("""SELECT s.name, (SELECT COUNT(*) FROM stock k WHERE k.service=s.name) AS n
                   FROM services s ORDER BY s.name""")
    if not rows:
        return await interaction.response.send_message("No services yet.", ephemeral=True)
    text = "\n".join(f"**{r['name'].title()}**: {r['n']}" for r in rows)
    await interaction.response.send_message(embed=discord.Embed(title="Stock", description=text,
                                                                color=discord.Color.blurple()))


@bot.tree.command(name="cooldown", description="Check your cooldown for a service")
@app_commands.autocomplete(service=service_autocomplete)
async def cooldown(interaction: discord.Interaction, service: str):
    service = norm(service)
    svc = db.one("SELECT cooldown FROM services WHERE name=?", (service,))
    if not svc:
        return await interaction.response.send_message("Unknown service.", ephemeral=True)
    last = db.one("SELECT MAX(ts) AS t FROM gen_log WHERE user_id=? AND service=?",
                  (interaction.user.id, service))["t"]
    cd = svc["cooldown"] * (int(get_setting("premium_mult", 50)) / 100 if is_premium(interaction.user) else 1)
    remaining = cd - (int(time.time()) - last) if last and last > int(get_setting("cd_reset_ts", 0)) else 0
    msg = "You're ready to generate!" if remaining <= 0 else f"Ready in **{fmt_time(remaining)}**."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="mystats", description="See how many items you've generated")
async def mystats(interaction: discord.Interaction):
    uid = interaction.user.id
    total = db.one("SELECT COUNT(*) AS n FROM gen_log WHERE user_id=?", (uid,))["n"]
    rows = db.q("SELECT service, COUNT(*) AS n FROM gen_log WHERE user_id=? GROUP BY service ORDER BY n DESC", (uid,))
    desc = "\n".join(f"{r['service'].title()}: {r['n']}" for r in rows) or "Nothing yet."
    embed = discord.Embed(title=f"{interaction.user.display_name}'s stats",
                          description=f"**Total:** {total}\n{desc}", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Top generators")
async def leaderboard(interaction: discord.Interaction):
    rows = db.q("SELECT user_id, COUNT(*) AS n FROM gen_log GROUP BY user_id ORDER BY n DESC LIMIT 10")
    if not rows:
        return await interaction.response.send_message("No data yet.", ephemeral=True)
    text = "\n".join(f"**{i}.** <@{r['user_id']}> - {r['n']}" for i, r in enumerate(rows, 1))
    await interaction.response.send_message(
        embed=discord.Embed(title="Leaderboard", description=text, color=discord.Color.gold()))


@bot.tree.command(name="ping", description="Bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms", ephemeral=True)


@bot.tree.command(name="botinfo", description="Uptime and totals")
async def botinfo(interaction: discord.Interaction):
    total = db.one("SELECT COUNT(*) AS n FROM gen_log")["n"]
    items = db.one("SELECT COUNT(*) AS n FROM stock")["n"]
    svcs = db.one("SELECT COUNT(*) AS n FROM services")["n"]
    embed = discord.Embed(title="Bot info", color=discord.Color.blurple())
    embed.add_field(name="Uptime", value=fmt_time(time.time() - bot.started))
    embed.add_field(name="Servers", value=str(len(bot.guilds)))
    embed.add_field(name="Services", value=str(svcs))
    embed.add_field(name="Items in stock", value=str(items))
    embed.add_field(name="Total generated", value=str(total))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Commands", color=discord.Color.blurple())
    embed.add_field(name="Everyone", inline=False, value=(
        "`/gen` generate an item\n`/services` list services\n`/stock` view stock\n"
        "`/cooldown` check your cooldown\n`/mystats` your stats\n`/leaderboard` top users\n"
        "`/ping` `/botinfo`"))
    embed.add_field(name="Admins", inline=False, value=(
        "`/addservice` `/removeservice` `/restock` `/clearstock` `/exportstock`\n"
        "`/setcooldown` `/setrole` `/blacklist` `/unblacklist` `/blacklisted`\n"
        "`/give` `/resetcooldown` `/setlogchannel` `/serverstats` `/drop`"))
    embed.add_field(name="Command groups (type the name, then pick a subcommand)", inline=False, value=(
        "`/util` fun + info (everyone) | `/vouch` reputation (everyone) | `/ticket` support (everyone)\n"
        "Admins: `/premium` `/config` `/service` `/user` `/stats` `/announce` `/bulk` `/backup`"))
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Admin commands
# --------------------------------------------------------------------------- #
@bot.tree.command(name="addservice", description="Create a new service")
@app_commands.describe(name="Service name", cooldown="Cooldown in seconds (default 3600)",
                       role="Role required to generate (optional)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def addservice(interaction: discord.Interaction, name: str,
                     cooldown: Optional[app_commands.Range[int, 0, 2_592_000]] = None,
                     role: Optional[discord.Role] = None):
    name = norm(name)
    if not re.fullmatch(r"[a-z0-9_\- ]{1,32}", name):
        return await interaction.response.send_message(
            "Names must be 1-32 chars: letters, numbers, spaces, `-` or `_`.", ephemeral=True)
    if db.one("SELECT 1 FROM services WHERE name=?", (name,)):
        return await interaction.response.send_message("That service already exists.", ephemeral=True)
    db.run("INSERT INTO services (name, cooldown, required_role_id, created_at) VALUES (?,?,?,?)",
           (name, DEFAULT_COOLDOWN if cooldown is None else cooldown, role.id if role else None, int(time.time())))
    await interaction.response.send_message(f"Service **{name}** created. Use `/restock` to add items.")


@bot.tree.command(name="removeservice", description="Delete a service and its stock")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def removeservice(interaction: discord.Interaction, service: str):
    service = norm(service)
    if not db.one("SELECT 1 FROM services WHERE name=?", (service,)):
        return await interaction.response.send_message("Unknown service.", ephemeral=True)
    db.run("DELETE FROM stock WHERE service=?", (service,))
    db.run("DELETE FROM services WHERE name=?", (service,))
    await interaction.response.send_message(f"Service **{service}** removed.")


@bot.tree.command(name="restock", description="Add items to a service (file or text)")
@app_commands.describe(service="Service to restock",
                       file=".txt file with one item per line",
                       items="Items separated by ; (alternative to a file)")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def restock(interaction: discord.Interaction, service: str,
                  file: Optional[discord.Attachment] = None, items: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    service = norm(service)
    if not db.one("SELECT 1 FROM services WHERE name=?", (service,)):
        return await interaction.followup.send("Unknown service. Create it with `/addservice`.")
    if not file and not items:
        return await interaction.followup.send("Provide a `.txt` file or `items`.")

    raw = ""
    if file:
        if file.size > 5_000_000:
            return await interaction.followup.send("File too large (5 MB max).")
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if items:
        raw += items
    new = [s.strip() for s in re.split(r"[\n;]", raw) if s.strip()]
    if not new:
        return await interaction.followup.send("No items found.")

    async with db.lock:
        existing = {r["item"] for r in db.q("SELECT item FROM stock WHERE service=?", (service,))}
        fresh, seen = [], set()
        for it in new:
            if it not in existing and it not in seen:
                fresh.append(it)
                seen.add(it)
        db.conn.executemany("INSERT INTO stock (service, item) VALUES (?, ?)", [(service, i) for i in fresh])
        db.conn.commit()

    total = db.one("SELECT COUNT(*) AS n FROM stock WHERE service=?", (service,))["n"]
    await interaction.followup.send(
        f"Added **{len(fresh)}** items to **{service}** ({len(new) - len(fresh)} duplicates skipped). "
        f"Stock is now **{total}**.")
    await send_log(interaction.guild, discord.Embed(
        title="Restocked", color=discord.Color.green(),
        description=f"{interaction.user.mention} added {len(fresh)} to **{service}**"))


@bot.tree.command(name="clearstock", description="Remove all items from a service")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def clearstock(interaction: discord.Interaction, service: str):
    service = norm(service)
    n = db.run("DELETE FROM stock WHERE service=?", (service,)).rowcount
    await interaction.response.send_message(f"Cleared **{n}** items from **{service}**.", ephemeral=True)


@bot.tree.command(name="exportstock", description="Download a service's remaining stock as a file")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def exportstock(interaction: discord.Interaction, service: str):
    service = norm(service)
    rows = db.q("SELECT item FROM stock WHERE service=? ORDER BY id", (service,))
    if not rows:
        return await interaction.response.send_message("Nothing in stock.", ephemeral=True)
    data = io.BytesIO("\n".join(r["item"] for r in rows).encode())
    await interaction.response.send_message(file=discord.File(data, f"{service}_stock.txt"), ephemeral=True)


@bot.tree.command(name="setcooldown", description="Change a service's cooldown")
@app_commands.describe(seconds="Cooldown in seconds (0 = none)")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def setcooldown(interaction: discord.Interaction, service: str,
                      seconds: app_commands.Range[int, 0, 2_592_000]):
    service = norm(service)
    if db.run("UPDATE services SET cooldown=? WHERE name=?", (seconds, service)).rowcount == 0:
        return await interaction.response.send_message("Unknown service.", ephemeral=True)
    await interaction.response.send_message(f"**{service}** cooldown set to {fmt_time(seconds)}.", ephemeral=True)


@bot.tree.command(name="setrole", description="Require a role for a service (leave role empty to remove)")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def setrole(interaction: discord.Interaction, service: str, role: Optional[discord.Role] = None):
    service = norm(service)
    if db.run("UPDATE services SET required_role_id=? WHERE name=?",
              (role.id if role else None, service)).rowcount == 0:
        return await interaction.response.send_message("Unknown service.", ephemeral=True)
    await interaction.response.send_message(
        f"**{service}** now requires {role.mention if role else 'no role'}.", ephemeral=True)


@bot.tree.command(name="give", description="Send a stock item to a user by DM")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def give(interaction: discord.Interaction, user: discord.Member, service: str):
    await interaction.response.defer(ephemeral=True)
    service = norm(service)
    async with db.lock:
        row = db.one("SELECT id, item FROM stock WHERE service=? ORDER BY id LIMIT 1", (service,))
        if not row:
            return await interaction.followup.send("Out of stock or unknown service.")
        db.run("DELETE FROM stock WHERE id=?", (row["id"],))
    try:
        await user.send(embed=discord.Embed(title=f"{service.title()} item",
                                            description=f"```{row['item']}```", color=discord.Color.green()))
    except discord.Forbidden:
        async with db.lock:
            db.run("INSERT INTO stock (id, service, item) VALUES (?,?,?)", (row["id"], service, row["item"]))
        return await interaction.followup.send("User has DMs closed. Item returned to stock.")
    await interaction.followup.send(f"Sent a **{service}** item to {user.mention}.")


@bot.tree.command(name="resetcooldown", description="Reset a user's cooldown (one service or all)")
@app_commands.autocomplete(service=service_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def resetcooldown(interaction: discord.Interaction, user: discord.Member, service: Optional[str] = None):
    if service:
        db.run("DELETE FROM gen_log WHERE user_id=? AND service=?", (user.id, norm(service)))
    else:
        db.run("DELETE FROM gen_log WHERE user_id=?", (user.id,))
    await interaction.response.send_message(f"Cooldown reset for {user.mention}.", ephemeral=True)


@bot.tree.command(name="blacklist", description="Block a user from /gen")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def blacklist(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    db.run("INSERT OR REPLACE INTO blacklist (user_id, reason) VALUES (?, ?)", (user.id, reason))
    await interaction.response.send_message(f"{user.mention} blacklisted.", ephemeral=True)


@bot.tree.command(name="unblacklist", description="Allow a user to use /gen again")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def unblacklist(interaction: discord.Interaction, user: discord.User):
    db.run("DELETE FROM blacklist WHERE user_id=?", (user.id,))
    await interaction.response.send_message(f"{user.mention} removed from the blacklist.", ephemeral=True)


@bot.tree.command(name="blacklisted", description="List blacklisted users")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def blacklisted(interaction: discord.Interaction):
    rows = db.q("SELECT user_id, reason FROM blacklist")
    text = "\n".join(f"<@{r['user_id']}> - {r['reason'] or 'no reason'}" for r in rows) or "Nobody."
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="setlogchannel", description="Set the channel for generation/restock logs")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    db.run("INSERT OR REPLACE INTO settings (key, value) VALUES ('log_channel', ?)", (str(channel.id),))
    await interaction.response.send_message(f"Logs will go to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="serverstats", description="Generation stats per service")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def serverstats(interaction: discord.Interaction):
    day = int(time.time()) - 86400
    rows = db.q("""SELECT service, COUNT(*) AS total,
                   SUM(CASE WHEN ts > ? THEN 1 ELSE 0 END) AS today
                   FROM gen_log GROUP BY service ORDER BY total DESC""", (day,))
    text = "\n".join(f"**{r['service'].title()}**: {r['total']} total, {r['today']} in last 24h" for r in rows)
    await interaction.response.send_message(text or "No generations yet.", ephemeral=True)


import sys  # noqa: E402
import extras  # noqa: E402

extras.setup(bot, db, sys.modules[__name__])

bot.run(TOKEN)
