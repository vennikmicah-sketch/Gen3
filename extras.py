"""
Extra command groups for the stock bot. Loaded by bot.py via extras.setup(...).

Discord allows only 100 top-level slash commands per bot, so these are grouped:
type /premium, /config, /service, ... and pick a subcommand.
"""
import asyncio
import csv
import io
import json
import random
import re
import time
from typing import Optional

import discord
from discord import app_commands

db = None    # set in setup()
core = None  # the bot.py module (helpers live there)
BOT = None

NAME_RE = re.compile(r"[a-z0-9_\- ]{1,32}")
ADMIN = discord.Permissions(administrator=True)
admin_only = app_commands.checks.has_permissions(administrator=True)
_tasks = set()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ts() -> int:
    return int(time.time())


def S(key, default=None):
    return core.get_setting(key, default)


def color() -> discord.Color:
    return core.embed_color()


def trunc(text: str, n: int = 3900) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


async def say(i: discord.Interaction, text: Optional[str] = None, *, embed=None, ephemeral=True, **kw):
    await i.response.send_message(text, embed=embed, ephemeral=ephemeral, **kw)


async def svc_ac(interaction: discord.Interaction, current: str):
    rows = db.q("SELECT name FROM services WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{current.lower()}%",))
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


def get_service(name: str):
    return db.one("SELECT * FROM services WHERE name=?", (core.norm(name),))


def stock_count(service: str) -> int:
    return db.one("SELECT COUNT(*) AS n FROM stock WHERE service=?", (service,))["n"]


async def post_to(guild: Optional[discord.Guild], key: str, embed: discord.Embed) -> bool:
    cid = S(key)
    channel = guild.get_channel(int(cid)) if cid and guild else None
    if not channel:
        return False
    try:
        await channel.send(embed=embed)
        return True
    except discord.HTTPException:
        return False


def is_admin(i: discord.Interaction) -> bool:
    perms = getattr(i.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


class OpenGroup(app_commands.Group):
    """A group anyone can use."""

    def __init__(self, name: str, description: str, guild_only: bool = True):
        super().__init__(name=name, description=description, guild_only=guild_only)


class AdminGroup(app_commands.Group):
    """Every subcommand in a subclass requires Administrator."""

    def __init__(self, name: str, description: str):
        super().__init__(name=name, description=description, default_permissions=ADMIN, guild_only=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            raise app_commands.MissingPermissions(["administrator"])
        return True


# --------------------------------------------------------------------------- #
# /premium  (7)
# --------------------------------------------------------------------------- #
class Premium(AdminGroup):
    @app_commands.command(description="Give a user premium for N days")
    async def add(self, i: discord.Interaction, user: discord.Member, days: app_commands.Range[int, 1, 3650]):
        exp = ts() + days * 86400
        db.run("INSERT OR REPLACE INTO premium (user_id, expires) VALUES (?, ?)", (user.id, exp))
        await say(i, f"{user.mention} has premium until <t:{exp}:F>.")

    @app_commands.command(description="Remove a user's premium")
    async def remove(self, i: discord.Interaction, user: discord.Member):
        db.run("DELETE FROM premium WHERE user_id=?", (user.id,))
        await say(i, f"Removed premium from {user.mention}.")

    @app_commands.command(description="Check a user's premium status")
    async def check(self, i: discord.Interaction, user: discord.Member):
        row = db.one("SELECT expires FROM premium WHERE user_id=?", (user.id,))
        if row and row["expires"] > ts():
            msg = f"{user.mention} is premium until <t:{row['expires']}:R>."
        elif core.is_premium(user):
            msg = f"{user.mention} is premium through the premium role."
        else:
            msg = f"{user.mention} is not premium."
        await say(i, msg)

    @app_commands.command(name="list", description="List active premium members")
    async def list_(self, i: discord.Interaction):
        rows = db.q("SELECT user_id, expires FROM premium WHERE expires>? ORDER BY expires", (ts(),))
        text = "\n".join(f"<@{r['user_id']}> - <t:{r['expires']}:R>" for r in rows) or "Nobody."
        await say(i, trunc(text, 1900))

    @app_commands.command(description="Add days to a user's premium")
    async def extend(self, i: discord.Interaction, user: discord.Member, days: app_commands.Range[int, 1, 3650]):
        row = db.one("SELECT expires FROM premium WHERE user_id=?", (user.id,))
        base = max(row["expires"], ts()) if row else ts()
        exp = base + days * 86400
        db.run("INSERT OR REPLACE INTO premium (user_id, expires) VALUES (?, ?)", (user.id, exp))
        await say(i, f"{user.mention}'s premium now ends <t:{exp}:F>.")

    @app_commands.command(description="Role that counts as premium (leave empty to clear)")
    async def setrole(self, i: discord.Interaction, role: Optional[discord.Role] = None):
        core.set_setting("premium_role", role.id if role else "")
        await say(i, f"Premium role: {role.mention if role else 'none'}.")

    @app_commands.command(description="Delete expired premium records")
    async def purge(self, i: discord.Interaction):
        n = db.run("DELETE FROM premium WHERE expires<=?", (ts(),)).rowcount
        await say(i, f"Removed {n} expired records.")


# --------------------------------------------------------------------------- #
# /config  (11)
# --------------------------------------------------------------------------- #
class ConfigG(AdminGroup):
    @app_commands.command(description="Show current settings")
    async def show(self, i: discord.Interaction):
        def ch(key):
            v = S(key)
            return f"<#{v}>" if v else "not set"
        e = discord.Embed(title="Settings", color=color())
        e.add_field(name="Daily limit (free)", value=S("daily_limit", "0") + " (0 = unlimited)")
        e.add_field(name="Daily limit (premium)", value=S("premium_limit", "0") + " (0 = unlimited)")
        e.add_field(name="Premium cooldown", value=S("premium_mult", "50") + "% of normal")
        e.add_field(name="Maintenance", value="ON" if S("maintenance", "0") == "1" else "off")
        e.add_field(name="Footer", value=S("footer", "Keep this private."))
        e.add_field(name="Color", value="#" + S("color", "5865F2"))
        e.add_field(name="Log channel", value=ch("log_channel"))
        e.add_field(name="Announce channel", value=ch("announce_channel"))
        e.add_field(name="Vouch channel", value=ch("vouch_channel"))
        e.add_field(name="Ticket channel", value=ch("ticket_channel"))
        await say(i, embed=e)

    @app_commands.command(description="Max /gen uses per 24h for free users (0 = unlimited)")
    async def dailylimit(self, i: discord.Interaction, amount: app_commands.Range[int, 0, 1000]):
        core.set_setting("daily_limit", amount)
        await say(i, f"Free daily limit set to {amount}.")

    @app_commands.command(description="Max /gen uses per 24h for premium users (0 = unlimited)")
    async def premiumlimit(self, i: discord.Interaction, amount: app_commands.Range[int, 0, 1000]):
        core.set_setting("premium_limit", amount)
        await say(i, f"Premium daily limit set to {amount}.")

    @app_commands.command(description="Premium cooldown as a percent of normal (50 = half)")
    async def premiummult(self, i: discord.Interaction, percent: app_commands.Range[int, 1, 100]):
        core.set_setting("premium_mult", percent)
        await say(i, f"Premium cooldown is now {percent}% of normal.")

    @app_commands.command(description="Turn maintenance mode on or off (blocks /gen)")
    async def maintenance(self, i: discord.Interaction, enabled: bool):
        core.set_setting("maintenance", "1" if enabled else "0")
        await say(i, f"Maintenance mode {'ON' if enabled else 'off'}.")

    @app_commands.command(description="Footer text on delivery DMs (empty = default)")
    async def footer(self, i: discord.Interaction, text: Optional[app_commands.Range[str, 1, 100]] = None):
        core.set_setting("footer", text or "Keep this private.")
        await say(i, "Footer updated.")

    @app_commands.command(name="color", description="Embed color as hex, e.g. 5865F2")
    async def color_(self, i: discord.Interaction, code: str):
        code = code.lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", code):
            return await say(i, "Use a 6-digit hex code like `5865F2`.")
        core.set_setting("color", code.upper())
        await say(i, f"Color set to #{code.upper()}.")

    @app_commands.command(description="Channel for restock announcements")
    async def announcechannel(self, i: discord.Interaction, channel: discord.TextChannel):
        core.set_setting("announce_channel", channel.id)
        await say(i, f"Announcements go to {channel.mention}.")

    @app_commands.command(description="Channel where new vouches are posted")
    async def vouchchannel(self, i: discord.Interaction, channel: discord.TextChannel):
        core.set_setting("vouch_channel", channel.id)
        await say(i, f"Vouches are posted in {channel.mention}.")

    @app_commands.command(description="Channel where new tickets are posted")
    async def ticketchannel(self, i: discord.Interaction, channel: discord.TextChannel):
        core.set_setting("ticket_channel", channel.id)
        await say(i, f"Tickets are posted in {channel.mention}.")

    @app_commands.command(description="Reset all settings to defaults (keeps log channel)")
    async def reset(self, i: discord.Interaction):
        db.run("DELETE FROM settings WHERE key != 'log_channel'")
        await say(i, "Settings reset.")


# --------------------------------------------------------------------------- #
# /service  (16)
# --------------------------------------------------------------------------- #
class ServiceG(AdminGroup):
    @app_commands.command(description="Show everything about a service")
    @app_commands.autocomplete(name=svc_ac)
    async def info(self, i: discord.Interaction, name: str):
        s = get_service(name)
        if not s:
            return await say(i, "Unknown service.")
        gens = db.one("SELECT COUNT(*) AS n FROM gen_log WHERE service=?", (s["name"],))["n"]
        e = discord.Embed(title=s["name"].title(), description=s["description"] or "No description", color=color())
        e.add_field(name="Stock", value=str(stock_count(s["name"])))
        e.add_field(name="Cooldown", value=core.fmt_time(s["cooldown"]))
        e.add_field(name="Role", value=f"<@&{s['required_role_id']}>" if s["required_role_id"] else "none")
        e.add_field(name="Enabled", value="yes" if s["enabled"] else "no")
        e.add_field(name="Hidden", value="yes" if s["hidden"] else "no")
        e.add_field(name="Max per user", value=str(s["max_per_user"] or "unlimited"))
        e.add_field(name="Low-stock alert", value=str(s["low_threshold"] or "off"))
        e.add_field(name="Total generated", value=str(gens))
        await say(i, embed=e)

    @app_commands.command(description="Rename a service")
    @app_commands.autocomplete(name=svc_ac)
    async def rename(self, i: discord.Interaction, name: str, new_name: str):
        old, new = core.norm(name), core.norm(new_name)
        if not get_service(old):
            return await say(i, "Unknown service.")
        if not NAME_RE.fullmatch(new):
            return await say(i, "Names must be 1-32 chars: letters, numbers, spaces, `-` or `_`.")
        if get_service(new):
            return await say(i, "That name is taken.")
        async with db.lock:
            for sql in ("UPDATE services SET name=? WHERE name=?",
                        "UPDATE stock SET service=? WHERE service=?",
                        "UPDATE gen_log SET service=? WHERE service=?"):
                db.conn.execute(sql, (new, old))
            db.conn.commit()
        await say(i, f"Renamed **{old}** to **{new}**.")

    @app_commands.command(description="Set a service description")
    @app_commands.autocomplete(name=svc_ac)
    async def describe(self, i: discord.Interaction, name: str, text: app_commands.Range[str, 1, 200]):
        n = db.run("UPDATE services SET description=? WHERE name=?", (text, core.norm(name))).rowcount
        await say(i, "Updated." if n else "Unknown service.")

    @app_commands.command(description="Enable a service")
    @app_commands.autocomplete(name=svc_ac)
    async def enable(self, i: discord.Interaction, name: str):
        n = db.run("UPDATE services SET enabled=1 WHERE name=?", (core.norm(name),)).rowcount
        await say(i, "Enabled." if n else "Unknown service.")

    @app_commands.command(description="Disable a service (blocks /gen for it)")
    @app_commands.autocomplete(name=svc_ac)
    async def disable(self, i: discord.Interaction, name: str):
        n = db.run("UPDATE services SET enabled=0 WHERE name=?", (core.norm(name),)).rowcount
        await say(i, "Disabled." if n else "Unknown service.")

    @app_commands.command(description="Hide a service from /services")
    @app_commands.autocomplete(name=svc_ac)
    async def hide(self, i: discord.Interaction, name: str):
        n = db.run("UPDATE services SET hidden=1 WHERE name=?", (core.norm(name),)).rowcount
        await say(i, "Hidden." if n else "Unknown service.")

    @app_commands.command(description="Show a hidden service in /services again")
    @app_commands.autocomplete(name=svc_ac)
    async def show(self, i: discord.Interaction, name: str):
        n = db.run("UPDATE services SET hidden=0 WHERE name=?", (core.norm(name),)).rowcount
        await say(i, "Visible." if n else "Unknown service.")

    @app_commands.command(description="Lifetime limit of items per user (0 = unlimited)")
    @app_commands.autocomplete(name=svc_ac)
    async def maxperuser(self, i: discord.Interaction, name: str, amount: app_commands.Range[int, 0, 1000]):
        n = db.run("UPDATE services SET max_per_user=? WHERE name=?", (amount, core.norm(name))).rowcount
        await say(i, "Updated." if n else "Unknown service.")

    @app_commands.command(description="Alert in the log channel when stock drops to this number (0 = off)")
    @app_commands.autocomplete(name=svc_ac)
    async def lowstock(self, i: discord.Interaction, name: str, threshold: app_commands.Range[int, 0, 10000]):
        n = db.run("UPDATE services SET low_threshold=? WHERE name=?", (threshold, core.norm(name))).rowcount
        await say(i, "Updated." if n else "Unknown service.")

    @app_commands.command(description="Copy a service's settings to a new, empty service")
    @app_commands.autocomplete(name=svc_ac)
    async def clone(self, i: discord.Interaction, name: str, new_name: str):
        s, new = get_service(name), core.norm(new_name)
        if not s:
            return await say(i, "Unknown service.")
        if not NAME_RE.fullmatch(new) or get_service(new):
            return await say(i, "Invalid or already-used name.")
        db.run("""INSERT INTO services (name, cooldown, required_role_id, created_at, description,
                  enabled, hidden, max_per_user, low_threshold) VALUES (?,?,?,?,?,?,?,?,?)""",
               (new, s["cooldown"], s["required_role_id"], ts(), s["description"],
                s["enabled"], s["hidden"], s["max_per_user"], s["low_threshold"]))
        await say(i, f"Created **{new}** with the same settings (no stock).")

    @app_commands.command(description="See how much is in stock and a masked sample")
    @app_commands.autocomplete(name=svc_ac)
    async def peek(self, i: discord.Interaction, name: str):
        s = get_service(name)
        if not s:
            return await say(i, "Unknown service.")
        row = db.one("SELECT item FROM stock WHERE service=? ORDER BY id LIMIT 1", (s["name"],))
        if not row:
            return await say(i, "Out of stock.")
        it = row["item"]
        sample = it[:3] + "*" * min(max(len(it) - 3, 3), 12)
        await say(i, f"**{s['name']}**: {stock_count(s['name'])} in stock. Next item: `{sample}`")

    @app_commands.command(description="Randomize the order items are handed out")
    @app_commands.autocomplete(name=svc_ac)
    async def shuffle(self, i: discord.Interaction, name: str):
        service = core.norm(name)
        async with db.lock:
            items = [r["item"] for r in db.q("SELECT item FROM stock WHERE service=?", (service,))]
            if not items:
                return await say(i, "Nothing to shuffle.")
            random.shuffle(items)
            db.conn.execute("DELETE FROM stock WHERE service=?", (service,))
            db.conn.executemany("INSERT INTO stock (service, item) VALUES (?, ?)", [(service, x) for x in items])
            db.conn.commit()
        await say(i, f"Shuffled {len(items)} items.")

    @app_commands.command(description="Remove duplicate items from a service")
    @app_commands.autocomplete(name=svc_ac)
    async def dedupe(self, i: discord.Interaction, name: str):
        service = core.norm(name)
        n = db.run("""DELETE FROM stock WHERE service=? AND id NOT IN
                      (SELECT MIN(id) FROM stock WHERE service=? GROUP BY item)""", (service, service)).rowcount
        await say(i, f"Removed {n} duplicates.")

    @app_commands.command(description="Remove one exact item from a service's stock")
    @app_commands.autocomplete(name=svc_ac)
    async def removeitem(self, i: discord.Interaction, name: str, item: str):
        n = db.run("DELETE FROM stock WHERE service=? AND item=?", (core.norm(name), item.strip())).rowcount
        await say(i, f"Removed {n} matching item(s).")

    @app_commands.command(description="Count stock items containing some text, per service")
    async def search(self, i: discord.Interaction, term: str):
        rows = db.q("SELECT service, COUNT(*) AS n FROM stock WHERE item LIKE ? GROUP BY service",
                    (f"%{term}%",))
        text = "\n".join(f"**{r['service']}**: {r['n']}" for r in rows) or "No matches."
        await say(i, text)

    @app_commands.command(description="Move items from one service to another")
    @app_commands.autocomplete(name=svc_ac, target=svc_ac)
    async def move(self, i: discord.Interaction, name: str, target: str, amount: app_commands.Range[int, 1, 100000]):
        src, dst = core.norm(name), core.norm(target)
        if not get_service(src) or not get_service(dst) or src == dst:
            return await say(i, "Both services must exist and be different.")
        async with db.lock:
            n = db.run("""UPDATE stock SET service=? WHERE id IN
                          (SELECT id FROM stock WHERE service=? ORDER BY id LIMIT ?)""", (dst, src, amount)).rowcount
        await say(i, f"Moved {n} items from **{src}** to **{dst}**.")


# --------------------------------------------------------------------------- #
# /user  (8)
# --------------------------------------------------------------------------- #
class UserG(AdminGroup):
    @app_commands.command(description="Overview of a user")
    async def info(self, i: discord.Interaction, user: discord.Member):
        total = db.one("SELECT COUNT(*) AS n FROM gen_log WHERE user_id=?", (user.id,))["n"]
        last = db.one("SELECT MAX(ts) AS t FROM gen_log WHERE user_id=?", (user.id,))["t"]
        bonus = db.one("SELECT extra FROM bonus WHERE user_id=?", (user.id,))
        black = db.one("SELECT reason FROM blacklist WHERE user_id=?", (user.id,))
        e = discord.Embed(title=str(user), color=color())
        e.add_field(name="Premium", value="yes" if core.is_premium(user) else "no")
        e.add_field(name="Blacklisted", value=f"yes ({black['reason'] or 'no reason'})" if black else "no")
        e.add_field(name="Total generated", value=str(total))
        e.add_field(name="Last generated", value=f"<t:{last}:R>" if last else "never")
        e.add_field(name="Bonus daily gens", value=str(bonus["extra"] if bonus else 0))
        await say(i, embed=e)

    @app_commands.command(description="A user's last 15 generations")
    async def history(self, i: discord.Interaction, user: discord.Member):
        rows = db.q("SELECT service, ts FROM gen_log WHERE user_id=? ORDER BY ts DESC LIMIT 15", (user.id,))
        text = "\n".join(f"{r['service']} - <t:{r['ts']}:R>" for r in rows) or "No history."
        await say(i, text)

    @app_commands.command(description="Give a user extra daily generations")
    async def bonus(self, i: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 100]):
        db.run("""INSERT INTO bonus (user_id, extra) VALUES (?, ?)
                  ON CONFLICT(user_id) DO UPDATE SET extra = extra + ?""", (user.id, amount, amount))
        await say(i, f"{user.mention} now has +{amount} bonus daily gens (stacking).")

    @app_commands.command(description="Remove a user's bonus generations")
    async def clearbonus(self, i: discord.Interaction, user: discord.Member):
        db.run("DELETE FROM bonus WHERE user_id=?", (user.id,))
        await say(i, "Bonus cleared.")

    @app_commands.command(description="Reset a user's daily limit (last 24h of usage)")
    async def resetlimit(self, i: discord.Interaction, user: discord.Member):
        n = db.run("DELETE FROM gen_log WHERE user_id=? AND ts>?", (user.id, ts() - 86400)).rowcount
        await say(i, f"Cleared {n} recent entries for {user.mention}.")

    @app_commands.command(description="Add a staff note to a user")
    async def note(self, i: discord.Interaction, user: discord.Member, text: app_commands.Range[str, 1, 300]):
        db.run("INSERT INTO notes (user_id, author_id, text, ts) VALUES (?,?,?,?)",
               (user.id, i.user.id, text, ts()))
        await say(i, "Note saved.")

    @app_commands.command(description="Show staff notes about a user")
    async def notes(self, i: discord.Interaction, user: discord.Member):
        rows = db.q("SELECT * FROM notes WHERE user_id=? ORDER BY ts DESC LIMIT 10", (user.id,))
        text = "\n".join(f"<t:{r['ts']}:d> by <@{r['author_id']}>: {r['text']}" for r in rows) or "No notes."
        await say(i, trunc(text, 1900))

    @app_commands.command(description="Delete ALL of a user's generation history")
    async def purgehistory(self, i: discord.Interaction, user: discord.Member):
        n = db.run("DELETE FROM gen_log WHERE user_id=?", (user.id,)).rowcount
        await say(i, f"Deleted {n} history entries.")


# --------------------------------------------------------------------------- #
# /stats  (8)
# --------------------------------------------------------------------------- #
def _counts(seconds: int):
    return db.q("SELECT service, COUNT(*) AS n FROM gen_log WHERE ts>? GROUP BY service ORDER BY n DESC",
                (ts() - seconds,))


class StatsG(AdminGroup):
    @app_commands.command(description="Generations in the last 24 hours")
    async def today(self, i: discord.Interaction):
        rows = _counts(86400)
        text = "\n".join(f"**{r['service']}**: {r['n']}" for r in rows) or "Nothing yet."
        await say(i, text)

    @app_commands.command(description="Generations in the last 7 days")
    async def week(self, i: discord.Interaction):
        rows = _counts(7 * 86400)
        text = "\n".join(f"**{r['service']}**: {r['n']}" for r in rows) or "Nothing yet."
        await say(i, text)

    @app_commands.command(description="Detailed stats for one service")
    @app_commands.autocomplete(name=svc_ac)
    async def service(self, i: discord.Interaction, name: str):
        s = get_service(name)
        if not s:
            return await say(i, "Unknown service.")
        q = lambda sql, *a: db.one(sql, a)["n"]  # noqa: E731
        e = discord.Embed(title=f"{s['name'].title()} stats", color=color())
        e.add_field(name="In stock", value=str(stock_count(s["name"])))
        e.add_field(name="Total", value=str(q("SELECT COUNT(*) AS n FROM gen_log WHERE service=?", s["name"])))
        e.add_field(name="Last 24h", value=str(q("SELECT COUNT(*) AS n FROM gen_log WHERE service=? AND ts>?",
                                                 s["name"], ts() - 86400)))
        e.add_field(name="Last 7d", value=str(q("SELECT COUNT(*) AS n FROM gen_log WHERE service=? AND ts>?",
                                                s["name"], ts() - 7 * 86400)))
        e.add_field(name="Unique users", value=str(q(
            "SELECT COUNT(DISTINCT user_id) AS n FROM gen_log WHERE service=?", s["name"])))
        await say(i, embed=e)

    @app_commands.command(description="A user's usage per service")
    async def user(self, i: discord.Interaction, user: discord.Member):
        rows = db.q("SELECT service, COUNT(*) AS n FROM gen_log WHERE user_id=? GROUP BY service ORDER BY n DESC",
                    (user.id,))
        text = "\n".join(f"**{r['service']}**: {r['n']}" for r in rows) or "No usage."
        await say(i, text)

    @app_commands.command(description="Generations per day for the last 7 days")
    async def daily(self, i: discord.Interaction):
        rows = db.q("""SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS d, COUNT(*) AS n FROM gen_log
                       WHERE ts>? GROUP BY d ORDER BY d DESC""", (ts() - 7 * 86400,))
        await say(i, "\n".join(f"{r['d']}: {r['n']}" for r in rows) or "Nothing yet.")

    @app_commands.command(description="Services running low on stock")
    async def lowstock(self, i: discord.Interaction, threshold: app_commands.Range[int, 0, 10000] = 5):
        rows = db.q("""SELECT s.name, (SELECT COUNT(*) FROM stock k WHERE k.service=s.name) AS n
                       FROM services s ORDER BY n""")
        low = [r for r in rows if r["n"] <= threshold]
        await say(i, "\n".join(f"**{r['name']}**: {r['n']}" for r in low) or "Nothing is low.")

    @app_commands.command(description="Most popular services of all time")
    async def topservices(self, i: discord.Interaction):
        rows = db.q("SELECT service, COUNT(*) AS n FROM gen_log GROUP BY service ORDER BY n DESC LIMIT 10")
        text = "\n".join(f"**{k}.** {r['service']} - {r['n']}" for k, r in enumerate(rows, 1))
        await say(i, text or "Nothing yet.")

    @app_commands.command(description="Download the generation log as CSV")
    async def exportlog(self, i: discord.Interaction):
        rows = db.q("SELECT user_id, service, ts FROM gen_log ORDER BY ts")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "service", "timestamp"])
        w.writerows([(r["user_id"], r["service"], r["ts"]) for r in rows])
        await say(i, file=discord.File(io.BytesIO(buf.getvalue().encode()), "gen_log.csv"))


# --------------------------------------------------------------------------- #
# /vouch  (7)  - open to everyone, two admin subcommands
# --------------------------------------------------------------------------- #
class Vouch(OpenGroup):
    @app_commands.command(description="Vouch for a user")
    async def add(self, i: discord.Interaction, user: discord.Member, text: app_commands.Range[str, 1, 300],
                  rating: app_commands.Range[int, 1, 5] = 5):
        if user.id == i.user.id or user.bot:
            return await say(i, "You can't vouch for yourself or a bot.")
        recent = db.one("SELECT 1 FROM vouches WHERE author_id=? AND target_id=? AND ts>?",
                        (i.user.id, user.id, ts() - 86400))
        if recent:
            return await say(i, "You already vouched for them in the last 24h.")
        db.run("INSERT INTO vouches (target_id, author_id, text, rating, ts) VALUES (?,?,?,?,?)",
               (user.id, i.user.id, text, rating, ts()))
        e = discord.Embed(title=f"New vouch for {user.display_name}", description=text, color=color())
        e.add_field(name="Rating", value="⭐" * rating)
        e.set_footer(text=f"from {i.user.display_name}")
        await post_to(i.guild, "vouch_channel", e)
        await say(i, "Vouch added. Thanks!")

    @app_commands.command(name="list", description="Recent vouches for a user (default: you)")
    async def list_(self, i: discord.Interaction, user: Optional[discord.Member] = None):
        user = user or i.user
        rows = db.q("SELECT * FROM vouches WHERE target_id=? ORDER BY ts DESC LIMIT 10", (user.id,))
        text = "\n".join(f"`#{r['id']}` {'⭐' * r['rating']} <@{r['author_id']}>: {r['text']}" for r in rows)
        await say(i, trunc(text or "No vouches yet.", 1900), ephemeral=False)

    @app_commands.command(description="Users with the most vouches")
    async def top(self, i: discord.Interaction):
        rows = db.q("SELECT target_id, COUNT(*) AS n FROM vouches GROUP BY target_id ORDER BY n DESC LIMIT 10")
        text = "\n".join(f"**{k}.** <@{r['target_id']}> - {r['n']}" for k, r in enumerate(rows, 1))
        await say(i, text or "No vouches yet.", ephemeral=False)

    @app_commands.command(description="How many vouches a user has")
    async def count(self, i: discord.Interaction, user: Optional[discord.Member] = None):
        user = user or i.user
        n = db.one("SELECT COUNT(*) AS n FROM vouches WHERE target_id=?", (user.id,))["n"]
        await say(i, f"{user.mention} has **{n}** vouches.", ephemeral=False)

    @app_commands.command(description="A user's average vouch rating")
    async def average(self, i: discord.Interaction, user: Optional[discord.Member] = None):
        user = user or i.user
        row = db.one("SELECT AVG(rating) AS a, COUNT(*) AS n FROM vouches WHERE target_id=?", (user.id,))
        msg = f"{user.mention}: **{row['a']:.2f}/5** from {row['n']} vouches." if row["n"] else "No vouches yet."
        await say(i, msg, ephemeral=False)

    @app_commands.command(description="(Admin) Remove a vouch by id")
    @admin_only
    async def remove(self, i: discord.Interaction, vouch_id: int):
        n = db.run("DELETE FROM vouches WHERE id=?", (vouch_id,)).rowcount
        await say(i, "Removed." if n else "No vouch with that id.")

    @app_commands.command(description="(Admin) Delete all vouches for a user")
    @admin_only
    async def clear(self, i: discord.Interaction, user: discord.Member):
        n = db.run("DELETE FROM vouches WHERE target_id=?", (user.id,)).rowcount
        await say(i, f"Deleted {n} vouches.")


# --------------------------------------------------------------------------- #
# /ticket  (6)  - open to everyone, admin subcommands marked
# --------------------------------------------------------------------------- #
class Ticket(OpenGroup):
    @app_commands.command(name="open", description="Open a support ticket")
    async def open_(self, i: discord.Interaction, subject: app_commands.Range[str, 3, 200]):
        n = db.one("SELECT COUNT(*) AS n FROM tickets WHERE user_id=? AND status='open'", (i.user.id,))["n"]
        if n >= 3:
            return await say(i, "You already have 3 open tickets. Close one first.")
        tid = db.run("INSERT INTO tickets (user_id, subject, status, ts) VALUES (?, ?, 'open', ?)",
                     (i.user.id, subject, ts())).lastrowid
        e = discord.Embed(title=f"Ticket #{tid}", description=subject, color=discord.Color.orange())
        e.set_footer(text=f"from {i.user} ({i.user.id})")
        await post_to(i.guild, "ticket_channel", e)
        await say(i, f"Ticket **#{tid}** opened. Staff will reply by DM.")

    @app_commands.command(description="Your tickets")
    async def mine(self, i: discord.Interaction):
        rows = db.q("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10", (i.user.id,))
        text = "\n".join(f"`#{r['id']}` [{r['status']}] {r['subject']}" for r in rows) or "No tickets."
        await say(i, text)

    @app_commands.command(description="View a ticket")
    async def view(self, i: discord.Interaction, ticket_id: int):
        t = db.one("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if not t or (t["user_id"] != i.user.id and not is_admin(i)):
            return await say(i, "Ticket not found.")
        await say(i, f"**#{t['id']}** [{t['status']}] by <@{t['user_id']}> <t:{t['ts']}:R>\n{t['subject']}")

    @app_commands.command(description="Close a ticket")
    async def close(self, i: discord.Interaction, ticket_id: int):
        t = db.one("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if not t or (t["user_id"] != i.user.id and not is_admin(i)):
            return await say(i, "Ticket not found.")
        db.run("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
        await say(i, f"Ticket #{ticket_id} closed.")

    @app_commands.command(name="list", description="(Admin) All open tickets")
    @admin_only
    async def list_(self, i: discord.Interaction):
        rows = db.q("SELECT * FROM tickets WHERE status='open' ORDER BY id LIMIT 25")
        text = "\n".join(f"`#{r['id']}` <@{r['user_id']}>: {r['subject']}" for r in rows) or "No open tickets."
        await say(i, trunc(text, 1900))

    @app_commands.command(description="(Admin) Reply to a ticket by DM")
    @admin_only
    async def reply(self, i: discord.Interaction, ticket_id: int, text: app_commands.Range[str, 1, 1500]):
        t = db.one("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if not t:
            return await say(i, "Ticket not found.")
        user = i.guild.get_member(t["user_id"]) if i.guild else None
        if not user:
            return await say(i, "That user isn't in this server.")
        try:
            await user.send(embed=discord.Embed(title=f"Reply to ticket #{ticket_id}", description=text, color=color()))
        except discord.Forbidden:
            return await say(i, "They have DMs closed.")
        await say(i, "Reply sent.")


# --------------------------------------------------------------------------- #
# /announce  (3)
# --------------------------------------------------------------------------- #
class Announce(AdminGroup):
    @app_commands.command(description="Send an embed to a channel")
    async def send(self, i: discord.Interaction, channel: discord.TextChannel,
                   message: app_commands.Range[str, 1, 1800], title: Optional[str] = None):
        await channel.send(embed=discord.Embed(title=title, description=message, color=color()))
        await say(i, f"Sent to {channel.mention}.")

    @app_commands.command(description="Announce a restock in the announce channel")
    @app_commands.autocomplete(service=svc_ac)
    async def restock(self, i: discord.Interaction, service: str, note: Optional[str] = None):
        s = get_service(service)
        if not s:
            return await say(i, "Unknown service.")
        e = discord.Embed(title="Restocked!", color=discord.Color.green(),
                          description=f"**{s['name'].title()}** now has **{stock_count(s['name'])}** in stock."
                                      + (f"\n{note}" if note else ""))
        ok = await post_to(i.guild, "announce_channel", e)
        await say(i, "Announced." if ok else "Set a channel first with `/config announcechannel`.")

    @app_commands.command(description="Post a maintenance notice")
    async def maintenance(self, i: discord.Interaction, message: Optional[str] = None):
        e = discord.Embed(title="Maintenance", color=discord.Color.orange(),
                          description=message or "The generator is down for maintenance. Back soon!")
        ok = await post_to(i.guild, "announce_channel", e)
        await say(i, "Posted." if ok else "Set a channel first with `/config announcechannel`.")


# --------------------------------------------------------------------------- #
# /bulk  (5)
# --------------------------------------------------------------------------- #
class Bulk(AdminGroup):
    @app_commands.command(description="Create several services at once (comma separated)")
    async def addservices(self, i: discord.Interaction, names: str,
                          cooldown: Optional[app_commands.Range[int, 0, 2_592_000]] = None):
        made, skipped = [], []
        for raw in names.split(","):
            n = core.norm(raw)
            if not NAME_RE.fullmatch(n) or get_service(n):
                skipped.append(n or "?")
                continue
            db.run("INSERT INTO services (name, cooldown, created_at) VALUES (?,?,?)",
                   (n, core.DEFAULT_COOLDOWN if cooldown is None else cooldown, ts()))
            made.append(n)
        await say(i, f"Created: {', '.join(made) or 'none'}\nSkipped: {', '.join(skipped) or 'none'}")

    @app_commands.command(description="Blacklist many user IDs at once (paste IDs)")
    async def blacklist(self, i: discord.Interaction, ids: str, reason: Optional[str] = None):
        found = [int(x) for x in re.findall(r"\d{15,20}", ids)]
        for uid in found:
            db.run("INSERT OR REPLACE INTO blacklist (user_id, reason) VALUES (?, ?)", (uid, reason))
        await say(i, f"Blacklisted {len(found)} users.")

    @app_commands.command(description="Delete all services that have no stock")
    async def removeempty(self, i: discord.Interaction):
        rows = db.q("""SELECT name FROM services WHERE
                       (SELECT COUNT(*) FROM stock WHERE service=services.name)=0""")
        for r in rows:
            db.run("DELETE FROM services WHERE name=?", (r["name"],))
        await say(i, f"Removed {len(rows)} empty services.")

    @app_commands.command(description="Reset everyone's cooldowns right now")
    async def resetcooldowns(self, i: discord.Interaction):
        core.set_setting("cd_reset_ts", ts())
        await say(i, "All cooldowns reset (daily limits and stats unchanged).")

    @app_commands.command(description="Set the same cooldown on every service")
    async def setcooldownall(self, i: discord.Interaction, seconds: app_commands.Range[int, 0, 2_592_000]):
        n = db.run("UPDATE services SET cooldown=?", (seconds,)).rowcount
        await say(i, f"Updated {n} services to {core.fmt_time(seconds)}.")


# --------------------------------------------------------------------------- #
# /util  (10) - open to everyone
# --------------------------------------------------------------------------- #
EIGHT_BALL = ["It is certain.", "Without a doubt.", "Yes.", "Most likely.", "Ask again later.",
              "Can't predict now.", "Don't count on it.", "My sources say no.", "Very doubtful.", "No."]


async def _remind(user, minutes, text):
    await asyncio.sleep(minutes * 60)
    try:
        await user.send(f"Reminder: {text}")
    except discord.HTTPException:
        pass


class Util(OpenGroup):
    @app_commands.command(description="Show someone's avatar")
    async def avatar(self, i: discord.Interaction, user: Optional[discord.User] = None):
        user = user or i.user
        e = discord.Embed(title=f"{user.display_name}'s avatar", color=color())
        e.set_image(url=user.display_avatar.url)
        await say(i, embed=e, ephemeral=False)

    @app_commands.command(description="Info about a user")
    async def userinfo(self, i: discord.Interaction, user: Optional[discord.Member] = None):
        user = user or i.user
        e = discord.Embed(title=str(user), color=color())
        e.add_field(name="Account created", value=discord.utils.format_dt(user.created_at, "R"))
        if isinstance(user, discord.Member) and user.joined_at:
            e.add_field(name="Joined server", value=discord.utils.format_dt(user.joined_at, "R"))
            e.add_field(name="Roles", value=str(len(user.roles) - 1))
        e.set_thumbnail(url=user.display_avatar.url)
        await say(i, embed=e, ephemeral=False)

    @app_commands.command(description="Info about this server")
    async def serverinfo(self, i: discord.Interaction):
        g = i.guild
        if not g:
            return await say(i, "Use this in a server.")
        e = discord.Embed(title=g.name, color=color())
        e.add_field(name="Members", value=str(g.member_count))
        e.add_field(name="Channels", value=str(len(g.channels)))
        e.add_field(name="Roles", value=str(len(g.roles)))
        e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        await say(i, embed=e, ephemeral=False)

    @app_commands.command(description="Roll dice")
    async def roll(self, i: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6,
                   dice: app_commands.Range[int, 1, 20] = 1):
        rolls = [random.randint(1, sides) for _ in range(dice)]
        await say(i, f"🎲 {', '.join(map(str, rolls))} (total {sum(rolls)})", ephemeral=False)

    @app_commands.command(description="Flip a coin")
    async def coinflip(self, i: discord.Interaction):
        await say(i, f"🪙 {random.choice(['Heads', 'Tails'])}!", ephemeral=False)

    @app_commands.command(description="Pick one option (comma separated)")
    async def choose(self, i: discord.Interaction, options: str):
        opts = [o.strip() for o in options.split(",") if o.strip()]
        if len(opts) < 2:
            return await say(i, "Give at least two options, separated by commas.")
        await say(i, f"I choose: **{random.choice(opts)}**", ephemeral=False)

    @app_commands.command(name="8ball", description="Ask the magic 8-ball")
    async def eightball(self, i: discord.Interaction, question: str):
        await say(i, f"🎱 {random.choice(EIGHT_BALL)}", ephemeral=False)

    @app_commands.command(description="Get a DM reminder (up to 24h; lost if the bot restarts)")
    async def remind(self, i: discord.Interaction, minutes: app_commands.Range[int, 1, 1440],
                     text: app_commands.Range[str, 1, 200]):
        t = asyncio.create_task(_remind(i.user, minutes, text))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        await say(i, f"I'll DM you in {minutes} minute(s).")

    @app_commands.command(description="How long the bot has been up")
    async def uptime(self, i: discord.Interaction):
        await say(i, f"Up for {core.fmt_time(time.time() - BOT.started)}.", ephemeral=False)

    @app_commands.command(description="Get the bot's invite link")
    async def invite(self, i: discord.Interaction):
        url = discord.utils.oauth_url(
            BOT.user.id, permissions=discord.Permissions(send_messages=True, embed_links=True),
            scopes=("bot", "applications.commands"))
        await say(i, url)


# --------------------------------------------------------------------------- #
# /backup  (2)
# --------------------------------------------------------------------------- #
class Backup(AdminGroup):
    @app_commands.command(description="Download the whole database file")
    async def database(self, i: discord.Interaction):
        await say(i, file=discord.File(core.DB_PATH, filename="gen-backup.db"))

    @app_commands.command(description="Download all stock as JSON")
    async def stockjson(self, i: discord.Interaction):
        data = {}
        for r in db.q("SELECT service, item FROM stock ORDER BY id"):
            data.setdefault(r["service"], []).append(r["item"])
        await say(i, file=discord.File(io.BytesIO(json.dumps(data, indent=2).encode()), "stock.json"))


# --------------------------------------------------------------------------- #
# /drop  - first click wins
# --------------------------------------------------------------------------- #
class DropView(discord.ui.View):
    def __init__(self, service: str):
        super().__init__(timeout=600)
        self.service = service
        self.claimed = False

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.green)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if db.one("SELECT 1 FROM blacklist WHERE user_id=?", (interaction.user.id,)):
            return await interaction.response.send_message("You're blacklisted.", ephemeral=True)
        async with db.lock:
            if self.claimed:
                return await interaction.response.send_message("Already claimed.", ephemeral=True)
            row = db.one("SELECT id, item FROM stock WHERE service=? ORDER BY id LIMIT 1", (self.service,))
            if not row:
                return await interaction.response.send_message("Out of stock.", ephemeral=True)
            db.run("DELETE FROM stock WHERE id=?", (row["id"],))
            self.claimed = True
        db.run("INSERT INTO gen_log (user_id, service, ts) VALUES (?, ?, ?)",
               (interaction.user.id, self.service, ts()))
        button.disabled = True
        button.label = f"Claimed by {interaction.user.display_name}"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"Yours:\n```{row['item']}```", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


@app_commands.command(name="drop", description="Post a drop: the first person to click gets the item")
@app_commands.describe(service="Service to drop from", message="Optional text for the drop")
@app_commands.autocomplete(service=svc_ac)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def drop(i: discord.Interaction, service: str, message: Optional[str] = None):
    s = get_service(service)
    if not s:
        return await say(i, "Unknown service.")
    if stock_count(s["name"]) == 0:
        return await say(i, "That service is out of stock.")
    e = discord.Embed(title=f"{s['name'].title()} drop!", color=discord.Color.gold(),
                      description=message or "First to click **Claim** gets it.")
    await say(i, embed=e, ephemeral=False, view=DropView(s["name"]))


# --------------------------------------------------------------------------- #
def setup(bot, _db, _core):
    global db, core, BOT
    db, core, BOT = _db, _core, bot
    for group in (Premium("premium", "Manage premium members"),
                  ConfigG("config", "Bot settings"),
                  ServiceG("service", "Edit and maintain services"),
                  UserG("user", "Look up and manage users"),
                  StatsG("stats", "Usage statistics"),
                  Vouch("vouch", "Vouches and reputation", guild_only=True),
                  Ticket("ticket", "Support tickets", guild_only=True),
                  Announce("announce", "Post announcements"),
                  Bulk("bulk", "Bulk admin actions"),
                  Util("util", "Fun and info commands", guild_only=False),
                  Backup("backup", "Back up bot data")):
        bot.tree.add_command(group)
    bot.tree.add_command(drop)
