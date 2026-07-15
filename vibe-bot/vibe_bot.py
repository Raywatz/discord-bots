import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
import datetime
import random
import bot_utils

TOKEN        = os.environ.get("DISCORD_VIBE_BOT_TOKEN", "")
VIBE_FILE    = "vibe_data.json"
ECONOMY_FILE = "economy_data.json"
BIRTHDAY_FILE= "birthday_data.json"
DISABLE_FILE = "disable_data.json"
HUB_FILE     = "hub_data.json"
LINK_FILE    = "link_data.json"

DAILY_AMOUNT   = 50
DAILY_COOLDOWN = 86400  # 24 hours in seconds

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


vibe_data    = load_json(VIBE_FILE)
birthday_data= load_json(BIRTHDAY_FILE)

def get_vibe(guild_id):
    if guild_id not in vibe_data:
        vibe_data[guild_id] = {
            "welcome_enabled": True,
            "welcome_channel": None,
            "welcome_message": "Welcome to the server, {user}!",
            "auto_role": None,
            "birthday_channel": None,
            "birthday_messages": {}
        }
    return vibe_data[guild_id]

def is_bot_disabled(guild_id):
    return load_json(DISABLE_FILE).get(str(guild_id), {}).get("vibe", False)

def get_canonical_guild(guild_id):
    """Return the shared economy key for this guild.
    Linked servers share the same canonical ID (numerically smaller),
    so their economy data is transparently unified.
    """
    links = load_json(LINK_FILE).get("links", {})
    partner = links.get(str(guild_id))
    if partner:
        return str(min(int(guild_id), int(partner)))
    return str(guild_id)

def is_mod(member, guild_id):
    if member.guild_permissions.administrator:
        return True
    mod_setup = load_json("mod_setup.json")
    mod_role_id = mod_setup.get(str(guild_id), {}).get("mod_role_id")
    if mod_role_id:
        role = discord.utils.get(member.guild.roles, id=int(mod_role_id))
        if role and role in member.roles:
            return True
    return False

# Economy: always reload from disk to reduce race with games_bot
def get_balance(guild_id, user_id):
    return load_json(ECONOMY_FILE).get(str(guild_id), {}).get(str(user_id), {}).get("balance", 0)

def set_balance(guild_id, user_id, amount):
    eco = load_json(ECONOMY_FILE)
    gid = str(guild_id)
    uid = str(user_id)
    if gid not in eco:
        eco[gid] = {}
    if uid not in eco[gid]:
        eco[gid][uid] = {"balance": 0, "name": ""}
    eco[gid][uid]["balance"] = max(0, amount)
    save_json(ECONOMY_FILE, eco)

def add_balance(guild_id, user_id, amount, name=""):
    eco = load_json(ECONOMY_FILE)
    gid = str(guild_id)
    uid = str(user_id)
    if gid not in eco:
        eco[gid] = {}
    if uid not in eco[gid]:
        eco[gid][uid] = {"balance": 0, "name": name or ""}
    eco[gid][uid]["balance"] = max(0, eco[gid][uid].get("balance", 0) + amount)
    if name:
        eco[gid][uid]["name"] = name
    save_json(ECONOMY_FILE, eco)


# ── /setup ────────────────────────────────────────────────────────────────────
class VibeSetupView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=120)
        self.guild            = guild
        self.welcome_channel  = None
        self.birthday_channel = None
        self.auto_role        = None

        ch_options = [
            discord.SelectOption(label=ch.name, value=str(ch.id))
            for ch in guild.text_channels[:25]
        ]
        welcome_select = discord.ui.Select(placeholder="Welcome channel", options=ch_options)
        welcome_select.callback = self.welcome_callback
        self.add_item(welcome_select)

        bday_select = discord.ui.Select(placeholder="Birthday announcement channel", options=ch_options)
        bday_select.callback = self.bday_callback
        self.add_item(bday_select)

        role_options = [
            discord.SelectOption(label=r.name, value=str(r.id))
            for r in guild.roles if not r.is_default() and not r.managed
        ][:25]
        if role_options:
            role_select = discord.ui.Select(placeholder="Auto-assign role for new members", options=role_options)
            role_select.callback = self.role_callback
            self.add_item(role_select)

    async def welcome_callback(self, interaction: discord.Interaction):
        self.welcome_channel = int(interaction.data["values"][0])
        await interaction.response.defer()
        await self.try_finish(interaction)

    async def bday_callback(self, interaction: discord.Interaction):
        self.birthday_channel = int(interaction.data["values"][0])
        await interaction.response.defer()
        await self.try_finish(interaction)

    async def role_callback(self, interaction: discord.Interaction):
        self.auto_role = int(interaction.data["values"][0])
        await interaction.response.defer()
        await self.try_finish(interaction)

    async def try_finish(self, interaction: discord.Interaction):
        if not self.welcome_channel or not self.birthday_channel:
            return
        guild_id = str(interaction.guild_id)
        data     = get_vibe(guild_id)
        data["welcome_channel"]  = self.welcome_channel
        data["birthday_channel"] = self.birthday_channel
        if self.auto_role:
            data["auto_role"] = self.auto_role
        save_json(VIBE_FILE, vibe_data)
        await interaction.followup.send("Vibe bot setup complete!", ephemeral=True)
        self.stop()


@tree.command(name="setup", description="Set up the Vibe bot (welcome, birthday channels, auto-role)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    view = VibeSetupView(interaction.guild)
    await interaction.response.send_message("Set up the Vibe bot:", view=view, ephemeral=True)


# ── /birthday ─────────────────────────────────────────────────────────────────
@tree.command(name="birthday", description="Set your birthday")
@app_commands.describe(month="Month (1-12)", day="Day (1-31)")
@app_commands.guild_only()
async def birthday(interaction: discord.Interaction, month: int, day: int):
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        await interaction.response.send_message("Invalid date.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    uid      = str(interaction.user.id)
    if guild_id not in birthday_data:
        birthday_data[guild_id] = {}
    birthday_data[guild_id][uid] = {
        "month": month,
        "day": day,
        "name": interaction.user.name
    }
    save_json(BIRTHDAY_FILE, birthday_data)
    await interaction.response.send_message(f"Your birthday has been set to **{month}/{day}**.", ephemeral=True)


# ── /balance ──────────────────────────────────────────────────────────────────
@tree.command(name="balance", description="Check your economy balance")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    canonical = get_canonical_guild(guild_id)
    bal = get_balance(canonical, str(interaction.user.id))
    linked = canonical != guild_id
    suffix = " *(synced across linked servers)*" if linked else ""
    if is_mod(interaction.user, guild_id):
        await interaction.response.send_message(f"Your balance: **∞** (mod unlimited money){suffix}", ephemeral=True)
    else:
        await interaction.response.send_message(f"Your balance: **${bal}**{suffix}", ephemeral=True)


# ── /daily ────────────────────────────────────────────────────────────────────
@tree.command(name="daily", description="Claim your daily economy reward")
@app_commands.guild_only()
async def daily(interaction: discord.Interaction):
    guild_id  = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    uid       = str(interaction.user.id)
    canonical = get_canonical_guild(guild_id)
    eco       = load_json(ECONOMY_FILE)
    now       = datetime.datetime.utcnow().timestamp()
    last      = eco.get(canonical, {}).get(uid, {}).get("last_daily", 0)
    if now - last < DAILY_COOLDOWN:
        remaining = DAILY_COOLDOWN - (now - last)
        hours = int(remaining // 3600)
        mins  = int((remaining % 3600) // 60)
        await interaction.response.send_message(
            f"You already claimed your daily! Come back in **{hours}h {mins}m**.", ephemeral=True
        )
        return
    if canonical not in eco:
        eco[canonical] = {}
    if uid not in eco[canonical]:
        eco[canonical][uid] = {"balance": 0, "name": interaction.user.name}
    eco[canonical][uid]["balance"] = eco[canonical][uid].get("balance", 0) + DAILY_AMOUNT
    eco[canonical][uid]["last_daily"] = now
    eco[canonical][uid]["name"] = interaction.user.name
    save_json(ECONOMY_FILE, eco)
    bal = eco[canonical][uid]["balance"]
    await interaction.response.send_message(
        f"💰 You claimed your daily **${DAILY_AMOUNT}**! New balance: **${bal}**", ephemeral=True
    )


# ── /donate ───────────────────────────────────────────────────────────────────
@tree.command(name="donate", description="Give money to another member")
@app_commands.describe(member="Who to donate to", amount="Amount to donate")
async def donate(interaction: discord.Interaction, member: discord.Member, amount: int):
    guild_id  = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("You can't donate to yourself.", ephemeral=True)
        return

    canonical    = get_canonical_guild(guild_id)
    giver_bal    = get_balance(canonical, str(interaction.user.id))
    is_giver_mod = is_mod(interaction.user, guild_id)

    if not is_giver_mod and giver_bal < amount:
        await interaction.response.send_message(f"You only have **${giver_bal}**.", ephemeral=True)
        return

    if not is_giver_mod:
        set_balance(canonical, str(interaction.user.id), giver_bal - amount)
    add_balance(canonical, str(member.id), amount, member.name)
    await interaction.response.send_message(f"Donated **${amount}** to {member.mention}.", ephemeral=True)
    try:
        await member.send(f"You received **${amount}** from **{interaction.user.name}** in **{interaction.guild.name}**!")
    except discord.Forbidden:
        pass


# ── /leaderboard ─────────────────────────────────────────────────────────────
@tree.command(name="leaderboard", description="Show the richest members in this server")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    guild_id  = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    canonical = get_canonical_guild(guild_id)
    eco = load_json(ECONOMY_FILE).get(canonical, {})
    if not eco:
        await interaction.response.send_message("No economy data yet!", ephemeral=True)
        return
    sorted_players = sorted(eco.items(), key=lambda x: x[1].get("balance", 0), reverse=True)[:10]
    lines  = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, data) in enumerate(sorted_players):
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        name   = data.get("name", f"User {uid}")
        bal    = data.get("balance", 0)
        lines.append(f"{prefix} {name} — **${bal}**")
    await interaction.response.send_message("**Economy Leaderboard:**\n" + "\n".join(lines))


# ── /setmessage ───────────────────────────────────────────────────────────────
@tree.command(name="setmessage", description="Set a custom welcome message (use {user} for the mention)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(message="Welcome message text. Use {user} for the member mention.")
@app_commands.guild_only()
async def setmessage(interaction: discord.Interaction, message: str):
    guild_id = str(interaction.guild_id)
    if not is_mod(interaction.user, guild_id):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if len(message) > 500:
        await interaction.response.send_message("Message must be under 500 characters.", ephemeral=True)
        return
    data = get_vibe(guild_id)
    data["welcome_message"] = message
    save_json(VIBE_FILE, vibe_data)
    preview = message.replace("{user}", interaction.user.mention)
    await interaction.response.send_message(f"Welcome message updated!\n**Preview:** {preview}", ephemeral=True)


# ── /addmoney (mod only) ──────────────────────────────────────────────────────
@tree.command(name="addmoney", description="Add money to a member (mod only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member", amount="Amount to add (positive to give, negative to deduct)")
async def addmoney(interaction: discord.Interaction, member: discord.Member, amount: int):
    guild_id  = str(interaction.guild_id)
    if not is_mod(interaction.user, guild_id):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if amount == 0:
        await interaction.response.send_message("Amount cannot be 0.", ephemeral=True)
        return
    canonical = get_canonical_guild(guild_id)
    add_balance(canonical, str(member.id), amount, member.name)
    action = "Added" if amount > 0 else "Deducted"
    await interaction.response.send_message(f"{action} **${abs(amount)}** {'to' if amount > 0 else 'from'} {member.mention}.", ephemeral=True)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("vibe")

@heartbeat_task.before_loop
async def before_heartbeat():
    await client.wait_until_ready()


@client.event
async def on_error(event_method, *args, **kwargs):
    import traceback, sys
    exc = sys.exc_info()[1]
    guild_id = None
    if args and hasattr(args[0], 'guild') and args[0].guild:
        guild_id = str(args[0].guild.id)
    if isinstance(exc, discord.HTTPException) and exc.status == 429:
        bot_utils.log_event("vibe", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("vibe", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("vibe", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Bot is being rate limited. Try again in a moment.", ephemeral=True)
            return
    bot_utils.log_event("vibe", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


# ── ON READY + birthday checker ───────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    print(f"Vibe Bot logged in as {client.user}")
    if not birthday_check.is_running():
        birthday_check.start()
    if not heartbeat_task.is_running():
        heartbeat_task.start()


@tasks.loop(hours=1)
async def birthday_check():
    now = datetime.datetime.utcnow()
    for guild_id, birthdays in birthday_data.items():
        guild = client.get_guild(int(guild_id))
        if not guild:
            continue
        data     = get_vibe(guild_id)
        bday_ch_id = data.get("birthday_channel")
        if not bday_ch_id:
            continue
        channel = guild.get_channel(int(bday_ch_id))
        if not channel:
            continue
        for uid, bday in birthdays.items():
            month, day = bday.get("month"), bday.get("day")
            if not month or not day:
                continue
            if month == now.month and day == now.day:
                announced = data.get("birthday_messages", {}).get(uid)
                if announced == str(now.date()):
                    continue
                member = guild.get_member(int(uid))
                if not member:
                    continue
                try:
                    msg = await channel.send(
                        f"🎂 Happy Birthday {member.mention}! React to wish them well and earn **$1**!"
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[birthday_check] Could not send birthday message in guild {guild_id}: {e}")
                    continue
                if "birthday_messages" not in data:
                    data["birthday_messages"] = {}
                data["birthday_messages"][uid] = str(now.date())
                save_json(VIBE_FILE, vibe_data)
                add_balance(get_canonical_guild(guild_id), uid, 1, member.name)
                if "birthday_msg_ids" not in data:
                    data["birthday_msg_ids"] = {}
                data["birthday_msg_ids"][str(msg.id)] = uid
                # Reset reactor tracking for the new message
                data.setdefault("birthday_reactors", {})[str(msg.id)] = []
                save_json(VIBE_FILE, vibe_data)


# ── Reaction handler for birthday $1 ─────────────────────────────────────────
@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return
    if not payload.guild_id:
        return
    guild_id = str(payload.guild_id)
    data     = get_vibe(guild_id)
    msg_ids  = data.get("birthday_msg_ids", {})
    msg_id   = str(payload.message_id)
    if msg_id not in msg_ids:
        return
    # Prevent farming: each user only earns once per birthday message
    reactors = data.setdefault("birthday_reactors", {})
    if msg_id not in reactors:
        reactors[msg_id] = []
    uid_str = str(payload.user_id)
    if uid_str in reactors[msg_id]:
        return
    reactors[msg_id].append(uid_str)
    save_json(VIBE_FILE, vibe_data)
    canonical    = get_canonical_guild(guild_id)
    birthday_uid = msg_ids[msg_id]
    user_name    = payload.member.name if payload.member else ""
    add_balance(canonical, uid_str, 1, user_name)
    add_balance(canonical, birthday_uid, 1)


# ── Welcome new members ───────────────────────────────────────────────────────
@client.event
async def on_member_join(member):
    guild_id = str(member.guild.id)
    if is_bot_disabled(guild_id):
        return
    data = get_vibe(guild_id)
    hub  = load_json(HUB_FILE).get(guild_id, {})
    if not data.get("welcome_enabled", True) or not hub.get("welcome_enabled", True):
        return

    # Auto role
    auto_role_id = data.get("auto_role")
    if auto_role_id:
        role = member.guild.get_role(int(auto_role_id))
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass

    # Welcome message
    welcome_ch_id = data.get("welcome_channel")
    if welcome_ch_id:
        channel = member.guild.get_channel(int(welcome_ch_id))
        if channel:
            msg = data.get("welcome_message", "Welcome to the server, {user}!")
            try:
                await channel.send(msg.replace("{user}", member.mention))
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Welcome economy bonus
    start_amount = hub.get("welcome_economy_amount", 0)
    if start_amount:
        add_balance(get_canonical_guild(guild_id), str(member.id), start_amount, member.name)


# ── Economy: earn by chatting ─────────────────────────────────────────────────
@client.event
async def on_message(message):
    if message.author.bot:
        return
    if not message.guild:
        return
    guild_id = str(message.guild.id)
    if is_bot_disabled(guild_id):
        return
    # Earn 1 point per message — use canonical guild so linked servers share economy
    canonical = get_canonical_guild(guild_id)
    add_balance(canonical, str(message.author.id), 1, message.author.name)


# ── /birthdays ────────────────────────────────────────────────────────────────
@tree.command(name="birthdays", description="List upcoming birthdays in the next 30 days")
@app_commands.guild_only()
async def birthdays(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    bdays = birthday_data.get(guild_id, {})
    if not bdays:
        await interaction.response.send_message("No birthdays set yet! Use `/birthday` to add yours.", ephemeral=True)
        return
    today = datetime.date.today()
    results = []
    for uid, info in bdays.items():
        m, d = info.get("month"), info.get("day")
        if not m or not d:
            continue
        try:
            this_year = datetime.date(today.year, m, d)
            next_occ = this_year if this_year >= today else datetime.date(today.year + 1, m, d)
        except ValueError:
            continue
        days_away = (next_occ - today).days
        if days_away <= 30:
            results.append((days_away, next_occ, info.get("name", f"User {uid}")))
    results.sort()
    if not results:
        await interaction.response.send_message("No birthdays in the next 30 days.", ephemeral=True)
        return
    lines = []
    for days_away, date, name in results:
        if days_away == 0:
            label = "**TODAY!** 🎂"
        elif days_away == 1:
            label = "tomorrow 🎉"
        else:
            label = f"in {days_away} days"
        lines.append(f"• **{name}** — {date.strftime('%B %d')} ({label})")
    await interaction.response.send_message("**🎂 Upcoming Birthdays (next 30 days):**\n" + "\n".join(lines))


# ── /economystats ─────────────────────────────────────────────────────────────
@tree.command(name="economystats", description="Show economy overview for this server")
@app_commands.guild_only()
async def economystats(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    canonical = get_canonical_guild(guild_id)
    eco = load_json(ECONOMY_FILE).get(canonical, {})
    if not eco:
        await interaction.response.send_message("No economy data yet!", ephemeral=True)
        return
    balances     = [v.get("balance", 0) for v in eco.values()]
    total        = sum(balances)
    avg          = total // max(len(balances), 1)
    richest      = max(eco.items(), key=lambda x: x[1].get("balance", 0))
    richest_name = richest[1].get("name", f"User {richest[0]}")
    richest_bal  = richest[1].get("balance", 0)
    await interaction.response.send_message(
        f"**💰 Economy Stats:**\n"
        f"Total in circulation: **${total:,}**\n"
        f"Average balance: **${avg:,}**\n"
        f"Richest user: **{richest_name}** — **${richest_bal:,}**\n"
        f"Total users tracked: **{len(eco)}**"
    )


# ── /rob ──────────────────────────────────────────────────────────────────────
@tree.command(name="rob", description="Attempt to rob another user's coins (risky!)")
@app_commands.describe(user="Who to rob")
async def rob(interaction: discord.Interaction, user: discord.Member):
    import time as _time
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't rob yourself.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't rob a bot.", ephemeral=True)
        return
    canonical   = get_canonical_guild(guild_id)
    eco         = load_json(ECONOMY_FILE)
    robber_data = eco.get(canonical, {}).get(str(interaction.user.id), {})
    target_data = eco.get(canonical, {}).get(str(user.id), {})
    target_bal  = target_data.get("balance", 0)
    if target_bal < 10:
        await interaction.response.send_message(
            f"{user.display_name} is too broke to rob (balance < $10).", ephemeral=True
        )
        return
    last_rob = robber_data.get("last_rob", 0)
    if _time.time() - last_rob < 3600:
        remaining = int(3600 - (_time.time() - last_rob))
        mins = remaining // 60
        await interaction.response.send_message(
            f"You need to wait **{mins}m** before robbing again.", ephemeral=True
        )
        return
    # Update last_rob timestamp
    if canonical not in eco:
        eco[canonical] = {}
    if str(interaction.user.id) not in eco[canonical]:
        eco[canonical][str(interaction.user.id)] = {"balance": 0, "name": interaction.user.display_name}
    eco[canonical][str(interaction.user.id)]["last_rob"] = _time.time()
    success = random.random() < 0.40  # 40% success
    if success:
        pct    = random.uniform(0.10, 0.30)
        stolen = max(1, int(target_bal * pct))
        eco[canonical][str(user.id)]["balance"] = max(0, target_bal - stolen)
        robber_bal = eco[canonical][str(interaction.user.id)].get("balance", 0)
        eco[canonical][str(interaction.user.id)]["balance"] = robber_bal + stolen
        save_json(ECONOMY_FILE, eco)
        await interaction.response.send_message(
            f"💰 **Rob successful!** You stole **${stolen}** from {user.display_name}! (took {pct*100:.0f}%)"
        )
    else:
        penalty = 25
        robber_bal = eco[canonical][str(interaction.user.id)].get("balance", 0)
        eco[canonical][str(interaction.user.id)]["balance"] = max(0, robber_bal - penalty)
        save_json(ECONOMY_FILE, eco)
        await interaction.response.send_message(
            f"🚨 **Caught!** You were caught robbing {user.display_name} and fined **${penalty}**."
        )


# ── /slots ────────────────────────────────────────────────────────────────────
@tree.command(name="slots", description="Spin the slot machine for $10")
@app_commands.guild_only()
async def slots(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    canonical = get_canonical_guild(guild_id)
    bal       = get_balance(canonical, interaction.user.id)
    cost      = 10
    if bal < cost:
        await interaction.response.send_message(
            f"You need at least **${cost}** to play slots (you have **${bal}**).", ephemeral=True
        )
        return
    set_balance(canonical, interaction.user.id, bal - cost)
    SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "⭐", "💎"]
    WEIGHTS = [30,   25,   20,   15,   7,    3]
    reels   = random.choices(SYMBOLS, weights=WEIGHTS, k=3)
    display = " | ".join(reels)
    if reels[0] == reels[1] == reels[2]:
        payout = cost * 10
        result = f"🎉 **JACKPOT!** Three {reels[0]}! You win **${payout}**!"
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        payout = cost * 2
        result = f"✨ Two matching! You win **${payout}**!"
    else:
        payout = 0
        result = "❌ No match. Better luck next time!"
    if payout > 0:
        add_balance(canonical, interaction.user.id, payout, interaction.user.display_name)
    net  = payout - cost
    sign = "+" if net >= 0 else "-"
    await interaction.response.send_message(
        f"🎰 **[ {display} ]**\n{result}\n*Net: {sign}${abs(net)}*"
    )


# ── /give ─────────────────────────────────────────────────────────────────────
@tree.command(name="give", description="Give coins to another user (alias for /donate)")
@app_commands.describe(user="Who to give coins to", amount="Amount to give")
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Vibe bot is disabled.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't give coins to yourself.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't give coins to a bot.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    canonical    = get_canonical_guild(guild_id)
    bal          = get_balance(canonical, interaction.user.id)
    is_giver_mod = is_mod(interaction.user, guild_id)
    if not is_giver_mod and bal < amount:
        await interaction.response.send_message(
            f"You only have **${bal}** — not enough to give **${amount}**.", ephemeral=True
        )
        return
    if not is_giver_mod:
        set_balance(canonical, interaction.user.id, bal - amount)
    add_balance(canonical, user.id, amount, user.display_name)
    new_bal = get_balance(canonical, interaction.user.id)
    await interaction.response.send_message(
        f"💸 Gave **${amount}** to {user.mention}. Your new balance: **${new_bal}**"
    )


client.run(TOKEN)
