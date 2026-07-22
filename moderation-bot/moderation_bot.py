import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
import datetime
import time
import uuid
import bot_utils

TOKEN            = os.environ.get("DISCORD_MOD_BOT_TOKEN", "")
RESTRICT_FILE    = "restricted_words.json"
RATE_FILE        = "rate_limits.json"
DISABLE_FILE     = "disable_data.json"
ACCESS_LOG       = "access_log.json"
WARN_FILE        = "warnings.json"
VIEWER_FILE      = "viewer_data.json"
RATE_LOG_FILE    = "rate_log.json"
MOD_SETUP_FILE   = "mod_setup.json"
PERM_SCHED_FILE  = "perm_schedule.json"
MODNOTES_FILE    = "modnotes.json"
HUB_FILE         = "hub_data.json"
BLOCKED_FILE     = "counting_blocked.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("mod")

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
        bot_utils.log_event("mod", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("mod", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("mod", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Bot is being rate limited. Try again in a moment.", ephemeral=True)
            return
    bot_utils.log_event("mod", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


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


restricted   = load_json(RESTRICT_FILE)
rate_configs = load_json(RATE_FILE)
warnings     = load_json(WARN_FILE)

def _load_rate_log():
    raw = load_json(RATE_LOG_FILE)
    now = time.time()
    result = {}
    for k, timestamps in raw.items():
        cleaned = [t for t in timestamps if now - t <= 10]
        if cleaned:
            result[k] = cleaned
    return result

def _save_rate_log(log):
    save_json(RATE_LOG_FILE, {k: v for k, v in log.items() if v})

def is_bot_disabled(guild_id):
    return load_json(DISABLE_FILE).get(str(guild_id), {}).get("mod", False)

def get_mod_role_id(guild_id):
    return load_json(MOD_SETUP_FILE).get(str(guild_id), {}).get("mod_role_id")

def is_mod(interaction: discord.Interaction):
    guild_id    = str(interaction.guild_id)
    mod_role_id = get_mod_role_id(guild_id)
    if interaction.user.guild_permissions.administrator:
        return True
    if mod_role_id:
        role = discord.utils.get(interaction.guild.roles, id=int(mod_role_id))
        if role and role in interaction.user.roles:
            return True
    return False

def log_action(guild_id, action, mod, member, channel, extra=""):
    logs = load_json(ACCESS_LOG)
    if guild_id not in logs:
        logs[guild_id] = []
    entry = {
        "action":  action,
        "mod":     f"{mod.name} ({mod.id})",
        "member":  f"{member.name} ({member.id})" if member else "N/A",
        "channel": channel or "N/A",
        "extra":   extra,
        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    }
    logs[guild_id].insert(0, entry)
    logs[guild_id] = logs[guild_id][:200]
    save_json(ACCESS_LOG, logs)


# ── /setup ────────────────────────────────────────────────────────────────────
class ModSetupView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=120)
        role_options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in guild.roles
            if not role.is_default() and not role.managed
        ][:25]
        select = discord.ui.Select(placeholder="Choose the mod role", options=role_options)
        select.callback = self.role_callback
        self.add_item(select)

    async def role_callback(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        role_id  = int(interaction.data["values"][0])
        setup    = load_json(MOD_SETUP_FILE)
        if guild_id not in setup:
            setup[guild_id] = {}
        setup[guild_id]["mod_role_id"] = role_id
        save_json(MOD_SETUP_FILE, setup)
        role = interaction.guild.get_role(role_id)
        role_name = role.name if role else f"ID {role_id}"
        await interaction.response.send_message(
            f"Mod role set to **{role_name}**. Only members with this role (or admins) can use mod commands.",
            ephemeral=True
        )
        self.stop()


@tree.command(name="setup", description="Set the mod role for this server")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    view = ModSetupView(interaction.guild)
    await interaction.response.send_message("Select the mod role:", view=view, ephemeral=True)


# ── /allow ────────────────────────────────────────────────────────────────────
@tree.command(name="allow", description="Give a member access to a channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member", channel="Channel name", minutes="Duration in minutes (optional)", reallow_after="Re-allow after this many minutes after removal (optional)")
async def allow(interaction: discord.Interaction, member: discord.Member, channel: str, minutes: int = None, reallow_after: int = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    ch = discord.utils.get(interaction.guild.text_channels, name=channel)
    if not ch:
        await interaction.response.send_message(f"Channel `{channel}` not found.", ephemeral=True)
        return
    overwrite = ch.overwrites_for(member)
    overwrite.view_channel = True
    overwrite.send_messages = True
    await ch.set_permissions(member, overwrite=overwrite)
    duration_msg = f" for {minutes} minute(s)" if minutes else " permanently"
    log_action(str(interaction.guild_id), "allow", interaction.user, member, channel, duration_msg.strip())
    await interaction.response.send_message(f"Gave {member.mention} access to **#{channel}**{duration_msg}.", ephemeral=True)
    try:
        await member.send(f"You have been given access to **#{channel}** in **{interaction.guild.name}**{duration_msg}.")
    except discord.Forbidden:
        pass
    if minutes:
        _schedule_perm(interaction.guild_id, member.id, channel, "remove",
                       minutes * 60, interaction.user.name)
        if reallow_after:
            _schedule_perm(interaction.guild_id, member.id, channel, "allow",
                           (minutes + reallow_after) * 60, interaction.user.name)


# ── /remove ───────────────────────────────────────────────────────────────────
@tree.command(name="remove", description="Remove a member's access to a channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member", channel="Channel name", reallow_after="Re-allow after this many minutes (optional)")
async def remove(interaction: discord.Interaction, member: discord.Member, channel: str, reallow_after: int = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    ch = discord.utils.get(interaction.guild.text_channels, name=channel)
    if not ch:
        await interaction.response.send_message(f"Channel `{channel}` not found.", ephemeral=True)
        return
    overwrite = ch.overwrites_for(member)
    overwrite.view_channel = False
    overwrite.send_messages = False
    await ch.set_permissions(member, overwrite=overwrite)
    log_action(str(interaction.guild_id), "remove", interaction.user, member, channel)
    reallow_msg = f" They will be re-allowed in {reallow_after} minute(s)." if reallow_after else ""
    await interaction.response.send_message(f"Removed {member.mention}'s access to **#{channel}**.{reallow_msg}", ephemeral=True)
    try:
        await member.send(f"Your access to **#{channel}** in **{interaction.guild.name}** has been removed.{reallow_msg}")
    except discord.Forbidden:
        pass
    if reallow_after:
        _schedule_perm(interaction.guild_id, member.id, channel, "allow",
                       reallow_after * 60, interaction.user.name)


# ── /restrict / /unrestrict ───────────────────────────────────────────────────
@tree.command(name="unrestrict", description="Remove a word from the restricted list")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(word="Word to unrestrict")
async def unrestrict(interaction: discord.Interaction, word: str):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    if word.lower() in restricted.get(guild_id, []):
        restricted[guild_id].remove(word.lower())
        save_json(RESTRICT_FILE, restricted)
        await interaction.response.send_message(f"Word `{word}` is no longer restricted.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Word `{word}` is not in the restricted list.", ephemeral=True)


@tree.command(name="restrict", description="Restrict a word from being sent")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(word="Word to restrict")
async def restrict(interaction: discord.Interaction, word: str):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    if guild_id not in restricted:
        restricted[guild_id] = []
    if word.lower() not in restricted[guild_id]:
        restricted[guild_id].append(word.lower())
        save_json(RESTRICT_FILE, restricted)
    await interaction.response.send_message(f"Word `{word}` is now restricted.", ephemeral=True)


# ── /timeout_config ───────────────────────────────────────────────────────────
@tree.command(name="timeout_config", description="Set auto-timeout for a channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel to monitor", amount="Max messages per 10 seconds", time="Timeout in minutes")
async def timeout_config(interaction: discord.Interaction, channel: discord.TextChannel, amount: int, minutes: int):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    if guild_id not in rate_configs:
        rate_configs[guild_id] = {}
    rate_configs[guild_id][str(channel.id)] = {"amount": amount, "timeout_mins": minutes}
    save_json(RATE_FILE, rate_configs)
    await interaction.response.send_message(
        f"Auto-timeout set for **#{channel.name}**: >{amount} messages/10s = {minutes} min timeout.", ephemeral=True
    )


# ── /warn ─────────────────────────────────────────────────────────────────────
@tree.command(name="warn", description="Warn a member")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to warn", reason="Reason for the warning", ban_threshold="Auto-ban after this many warnings (default 3)")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str, ban_threshold: int = 3):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    uid      = str(member.id)
    if guild_id not in warnings:
        warnings[guild_id] = {}
    if uid not in warnings[guild_id]:
        warnings[guild_id][uid] = []
    warnings[guild_id][uid].append({
        "reason": reason,
        "time": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "by": interaction.user.name
    })
    save_json(WARN_FILE, warnings)
    count = len(warnings[guild_id][uid])
    log_action(guild_id, f"warn ({count}/{ban_threshold})", interaction.user, member, None, reason)
    await interaction.response.send_message(
        f"Warned {member.mention} — **{count}/{ban_threshold}** warnings. Reason: {reason}", ephemeral=True
    )
    try:
        await member.send(f"You have been warned in **{interaction.guild.name}**. Reason: {reason} ({count}/{ban_threshold} warnings)")
    except discord.Forbidden:
        pass
    # If mod_counting_link is enabled, block this user from counting for 60 seconds
    hub = load_json(HUB_FILE)
    if hub.get(guild_id, {}).get("mod_counting_link", False):
        blocked = load_json(BLOCKED_FILE)
        if guild_id not in blocked:
            blocked[guild_id] = {}
        blocked[guild_id][str(member.id)] = time.time() + 60
        save_json(BLOCKED_FILE, blocked)

    if count >= ban_threshold:
        try:
            await member.ban(reason=f"Auto-ban: reached {ban_threshold} warnings")
            log_action(guild_id, "auto-ban", interaction.user, member, None, f"Reached {ban_threshold} warnings")
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ {member.mention} has reached **{ban_threshold}** warnings but I couldn't ban them — missing Ban Members permission.",
                ephemeral=True
            )


# ── /unwarn ───────────────────────────────────────────────────────────────────
@tree.command(name="unwarn", description="Remove the most recent warning from a member")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to unwarn")
async def unwarn(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    uid      = str(member.id)
    if warnings.get(guild_id, {}).get(uid):
        warnings[guild_id][uid].pop()
        save_json(WARN_FILE, warnings)
        count = len(warnings[guild_id][uid])
        await interaction.response.send_message(f"Removed latest warning from {member.mention}. They now have **{count}** warning(s).", ephemeral=True)
    else:
        await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)


# ── /kick ────────────────────────────────────────────────────────────────────
@tree.command(name="kick", description="Kick a member from the server")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to kick", reason="Reason for the kick")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    try:
        await member.send(f"You have been kicked from **{interaction.guild.name}**. Reason: {reason}")
    except discord.Forbidden:
        pass
    try:
        await member.kick(reason=reason)
        log_action(str(interaction.guild_id), "kick", interaction.user, member, None, reason)
        await interaction.response.send_message(f"Kicked {member.mention}. Reason: {reason}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to kick this member.", ephemeral=True)


# ── /ban ─────────────────────────────────────────────────────────────────────
@tree.command(name="ban", description="Ban a member from the server")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to ban", reason="Reason for the ban", delete_days="Days of messages to delete (0-7)")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given", delete_days: int = 0):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    delete_days = max(0, min(7, delete_days))
    try:
        await member.send(f"You have been banned from **{interaction.guild.name}**. Reason: {reason}")
    except discord.Forbidden:
        pass
    try:
        await member.ban(reason=reason, delete_message_days=delete_days)
        log_action(str(interaction.guild_id), "ban", interaction.user, member, None, reason)
        await interaction.response.send_message(f"Banned {member.mention}. Reason: {reason}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to ban this member.", ephemeral=True)


# ── /mute ─────────────────────────────────────────────────────────────────────
@tree.command(name="mute", description="Timeout (mute) a member for a set number of minutes")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to mute", minutes="Duration in minutes (max 40320 = 28 days)", reason="Reason for the mute")
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason given"):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    if minutes < 1 or minutes > 40320:
        await interaction.response.send_message("Minutes must be between 1 and 40320 (28 days).", ephemeral=True)
        return
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    try:
        await member.timeout(until, reason=reason)
        log_action(str(interaction.guild_id), "mute", interaction.user, member, None, f"{minutes}m — {reason}")
        await interaction.response.send_message(f"Timed out {member.mention} for **{minutes}** minute(s). Reason: {reason}", ephemeral=True)
        try:
            await member.send(f"You have been timed out in **{interaction.guild.name}** for **{minutes}** minute(s). Reason: {reason}")
        except discord.Forbidden:
            pass
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to timeout this member.", ephemeral=True)


# ── /purge ────────────────────────────────────────────────────────────────────
@tree.command(name="purge", description="Delete the last N messages in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(amount="Number of messages to delete (1-100)", member="Only delete messages from this member (optional)")
async def purge(interaction: discord.Interaction, amount: int, member: discord.Member = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    if amount < 1 or amount > 100:
        await interaction.response.send_message("Amount must be between 1 and 100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    check = (lambda m: m.author.id == member.id) if member else None
    try:
        deleted = await interaction.channel.purge(limit=amount, check=check)
        target_str = f" from {member.mention}" if member else ""
        log_action(str(interaction.guild_id), "purge", interaction.user, member, interaction.channel.name, f"{len(deleted)} messages")
        await interaction.followup.send(f"Deleted **{len(deleted)}** message(s){target_str}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to delete messages here.", ephemeral=True)


# ── /warnings ─────────────────────────────────────────────────────────────────
@tree.command(name="warnings", description="View all warnings for a member")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to check")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    uid      = str(member.id)
    warns    = load_json(WARN_FILE).get(guild_id, {}).get(uid, [])
    if not warns:
        await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        return
    lines = [f"**{i+1}.** {w['reason']} — by {w['by']} on {w['time']}" for i, w in enumerate(warns)]
    msg   = f"**Warnings for {member.display_name} ({len(warns)} total):**\n" + "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await interaction.response.send_message(msg, ephemeral=True)


# ── /unmute ───────────────────────────────────────────────────────────────────
@tree.command(name="unmute", description="Remove a timeout from a member")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to unmute")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    try:
        await member.timeout(None)
        log_action(str(interaction.guild_id), "unmute", interaction.user, member, None)
        await interaction.response.send_message(f"Removed timeout from {member.mention}.", ephemeral=True)
        try:
            await member.send(f"Your timeout in **{interaction.guild.name}** has been removed.")
        except discord.Forbidden:
            pass
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to remove this timeout.", ephemeral=True)


# ── /unban ────────────────────────────────────────────────────────────────────
@tree.command(name="unban", description="Unban a user by ID")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_id="The user ID to unban")
async def unban(interaction: discord.Interaction, user_id: str):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    try:
        user = await client.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        log_action(str(interaction.guild_id), "unban", interaction.user, user, None)
        await interaction.response.send_message(f"Unbanned **{user.name}**.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Could not unban: {e}", ephemeral=True)


# ── /viewer ───────────────────────────────────────────────────────────────────
@tree.command(name="viewer", description="Assign the Viewer role to a member (read-only, below @everyone)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to assign Viewer role to")
async def viewer(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild    = interaction.guild
    # Find or create Viewer role
    role = discord.utils.get(guild.roles, name="Viewer")
    if not role:
        role = await guild.create_role(
            name="Viewer",
            permissions=discord.Permissions(view_channel=True, read_message_history=True),
            reason="Viewer role created by mod bot"
        )
        try:
            await role.edit(position=1)
        except (discord.Forbidden, discord.HTTPException):
            pass
    # Enforce read-only: deny send_messages on every text channel for this role
    for ch in guild.text_channels:
        try:
            ow = ch.overwrites_for(role)
            ow.send_messages = False
            ow.add_reactions  = False
            await ch.set_permissions(role, overwrite=ow)
        except (discord.Forbidden, discord.HTTPException):
            pass
    if role not in member.roles:
        await member.add_roles(role)
    log_action(str(interaction.guild_id), "viewer assigned", interaction.user, member, None)
    await interaction.response.send_message(f"Assigned **Viewer** role to {member.mention}.", ephemeral=True)
    try:
        await member.send(f"You have been assigned the **Viewer** role in **{guild.name}**. You can view channels but have limited permissions.")
    except discord.Forbidden:
        pass


# ── /log ──────────────────────────────────────────────────────────────────────
@tree.command(name="log", description="View recent mod actions")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(filter="Filter by action type (optional): allow, remove, warn, ban, mute, join, leave, edit, delete")
async def log(interaction: discord.Interaction, filter: str = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    logs     = load_json(ACCESS_LOG).get(guild_id, [])
    if not logs:
        await interaction.response.send_message("No log entries yet.", ephemeral=True)
        return
    if filter:
        logs = [e for e in logs if filter.lower() in e["action"].lower()]
    if not logs:
        await interaction.response.send_message(f"No entries matching `{filter}`.", ephemeral=True)
        return
    lines = []
    icons = {
        "allow": "✅", "remove": "❌", "warn": "⚠️", "ban": "🔨",
        "unmute": "🔊", "unban": "🔓", "join": "👋", "leave": "🚪",
        "edit": "✏️", "delete": "🗑️", "viewer": "👁️", "auto": "🤖"
    }
    for e in logs[:20]:
        icon = next((v for k, v in icons.items() if k in e["action"].lower()), "📋")
        line = f"{icon} **{e['action']}** | {e['member']} | {e.get('channel', 'N/A')} | {e.get('extra', '')} | by {e['mod']} | {e['time']}"
        lines.append(line)
    header = "**Mod Log:**\n"
    body   = "\n".join(lines)
    full   = header + body
    if len(full) <= 1900:
        await interaction.response.send_message(full, ephemeral=True)
    else:
        # Send first chunk as the response, rest as followups
        chunks = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            chunks.append(current)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)


# ── PERMISSION SCHEDULING (survives restarts) ─────────────────────────────────
def _schedule_perm(guild_id, member_id, channel_name, action, delay_secs, created_by="system"):
    """Persist a future permission change to disk and create an asyncio task."""
    entry = {
        "id":           str(uuid.uuid4()),
        "guild_id":     str(guild_id),
        "member_id":    member_id,
        "channel_name": channel_name,
        "action":       action,  # "remove" or "allow"
        "execute_at":   time.time() + delay_secs,
        "created_by":   created_by,
    }
    sched = load_json(PERM_SCHED_FILE)
    sched.setdefault("entries", []).append(entry)
    save_json(PERM_SCHED_FILE, sched)
    asyncio.create_task(_run_perm_task(entry["id"], delay_secs, entry))
    return entry["id"]

async def _run_perm_task(entry_id, delay_secs, entry):
    await asyncio.sleep(max(0, delay_secs))
    # Confirm entry is still pending (not manually cancelled)
    sched = load_json(PERM_SCHED_FILE)
    if not any(e["id"] == entry_id for e in sched.get("entries", [])):
        return
    guild = client.get_guild(int(entry["guild_id"]))
    if guild:
        if entry["action"] == "unban":
            try:
                user = await client.fetch_user(entry["member_id"])
                await guild.unban(user, reason="Scheduled unban (tempban expired)")
                log_action(entry["guild_id"], "unban (scheduled)", guild.me, user, None, "Tempban expired")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass
            except Exception:
                pass
        else:
            if not entry.get("channel_name"):  # skip channel lookup for unban entries
                pass
            else:
                member = guild.get_member(entry["member_id"])
                ch     = discord.utils.get(guild.text_channels, name=entry["channel_name"])
                if member and ch:
                    ow = ch.overwrites_for(member)
                    if entry["action"] == "remove":
                        ow.view_channel = False
                        ow.send_messages = False
                    else:
                        ow.view_channel = True
                        ow.send_messages = True
                    try:
                        await ch.set_permissions(member, overwrite=ow)
                        log_action(entry["guild_id"], f"perm {entry['action']} (scheduled)", guild.me, member, entry["channel_name"])
                    except discord.Forbidden:
                        pass
                    try:
                        dm_msg = (f"Your access to **#{entry['channel_name']}** has been {'removed' if entry['action'] == 'remove' else 'restored'}.")
                        await member.send(dm_msg)
                    except discord.Forbidden:
                        pass
    # Remove from schedule
    sched["entries"] = [e for e in sched.get("entries", []) if e["id"] != entry_id]
    save_json(PERM_SCHED_FILE, sched)

async def _recover_perm_schedule():
    """On startup, re-arm any pending permission changes."""
    sched = load_json(PERM_SCHED_FILE)
    now   = time.time()
    for entry in sched.get("entries", []):
        delay = entry["execute_at"] - now
        asyncio.create_task(_run_perm_task(entry["id"], delay, entry))


# ── ON READY ──────────────────────────────────────────────────────────────────
_perm_schedule_recovered = False

@client.event
async def on_ready():
    global _perm_schedule_recovered
    await tree.sync()
    if not _perm_schedule_recovered:
        await _recover_perm_schedule()
        _perm_schedule_recovered = True
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"Moderation Bot logged in as {client.user}")


# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────
message_log = _load_rate_log()

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if not message.guild:
        return

    guild_id   = str(message.guild.id)
    channel_id = str(message.channel.id)
    user_id    = message.author.id

    if is_bot_disabled(guild_id):
        return

    # Restricted words
    words = restricted.get(guild_id, [])
    if any(w in message.content.lower() for w in words):
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await message.channel.send(f"{message.author.mention} That word is restricted here.")
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    # Auto-timeout rate limiter
    config = rate_configs.get(guild_id, {}).get(channel_id)
    if config and not message.author.guild_permissions.administrator:
        now = time.time()
        key = f"{guild_id}:{channel_id}:{user_id}"
        log = message_log.get(key, [])
        log = [t for t in log if now - t <= 10]
        log.append(now)
        message_log[key] = log
        _save_rate_log(message_log)
        if len(log) > config["amount"]:
            duration_secs = config["timeout_mins"] * 60
            try:
                until = discord.utils.utcnow() + datetime.timedelta(seconds=duration_secs)
                await message.author.timeout(until)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await message.channel.send(
                f"{message.author.mention} you have been timed out for {config['timeout_mins']} minute(s) for spamming.",
                delete_after=10
            )
            try:
                await message.author.send(
                    f"You were timed out in **{message.guild.name}** / **#{message.channel.name}** "
                    f"for {config['timeout_mins']} minute(s) for sending too many messages."
                )
            except discord.Forbidden:
                pass
            message_log[key] = []
            _save_rate_log(message_log)
            return


# ── EVENT LOGGING ─────────────────────────────────────────────────────────────
@client.event
async def on_message_edit(before, after):
    if not after.guild or before.content == after.content:
        return
    guild_id = str(after.guild.id)
    if is_bot_disabled(guild_id):
        return
    logs = load_json(ACCESS_LOG)
    if guild_id not in logs:
        logs[guild_id] = []
    logs[guild_id].insert(0, {
        "action":  "edit",
        "mod":     "system",
        "member":  f"{after.author.name} ({after.author.id})",
        "channel": after.channel.name,
        "extra":   f"Before: {before.content[:50]} | After: {after.content[:50]}",
        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    })
    logs[guild_id] = logs[guild_id][:200]
    save_json(ACCESS_LOG, logs)


@client.event
async def on_message_delete(message):
    if not message.guild:
        return
    if message.author.bot:
        return  # don't log bot-triggered deletions (e.g. restricted word removal)
    guild_id = str(message.guild.id)
    if is_bot_disabled(guild_id):
        return
    logs = load_json(ACCESS_LOG)
    if guild_id not in logs:
        logs[guild_id] = []
    logs[guild_id].insert(0, {
        "action":  "delete",
        "mod":     "system",
        "member":  f"{message.author.name} ({message.author.id})",
        "channel": message.channel.name,
        "extra":   message.content[:80],
        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    })
    logs[guild_id] = logs[guild_id][:200]
    save_json(ACCESS_LOG, logs)


@client.event
async def on_member_join(member):
    guild_id = str(member.guild.id)
    if is_bot_disabled(guild_id):
        return
    logs = load_json(ACCESS_LOG)
    if guild_id not in logs:
        logs[guild_id] = []
    logs[guild_id].insert(0, {
        "action":  "join",
        "mod":     "system",
        "member":  f"{member.name} ({member.id})",
        "channel": "N/A",
        "extra":   "",
        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    })
    logs[guild_id] = logs[guild_id][:200]
    save_json(ACCESS_LOG, logs)


@client.event
async def on_member_remove(member):
    guild_id = str(member.guild.id)
    if is_bot_disabled(guild_id):
        return
    logs = load_json(ACCESS_LOG)
    if guild_id not in logs:
        logs[guild_id] = []
    logs[guild_id].insert(0, {
        "action":  "leave",
        "mod":     "system",
        "member":  f"{member.name} ({member.id})",
        "channel": "N/A",
        "extra":   "",
        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    })
    logs[guild_id] = logs[guild_id][:200]
    save_json(ACCESS_LOG, logs)


@client.event
async def on_member_ban(guild, user):
    guild_id = str(guild.id)
    if is_bot_disabled(guild_id):
        return
    logs = load_json(ACCESS_LOG)
    if guild_id not in logs:
        logs[guild_id] = []
    logs[guild_id].insert(0, {
        "action":  "ban",
        "mod":     "system",
        "member":  f"{user.name} ({user.id})",
        "channel": "N/A",
        "extra":   "",
        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    })
    logs[guild_id] = logs[guild_id][:200]
    save_json(ACCESS_LOG, logs)


# ── /tempban ──────────────────────────────────────────────────────────────────
@tree.command(name="tempban", description="Temporarily ban a member for a set number of minutes")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    member="Member to ban",
    minutes="Duration in minutes (1–44640 = 31 days)",
    reason="Reason for the ban"
)
async def tempban(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason given"):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    if minutes < 1 or minutes > 44640:
        await interaction.response.send_message("Minutes must be between 1 and 44640 (31 days).", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    try:
        await member.send(
            f"You have been temporarily banned from **{interaction.guild.name}** for **{minutes}** minute(s).\nReason: {reason}\nYou will be automatically unbanned."
        )
    except discord.Forbidden:
        pass
    try:
        await member.ban(reason=f"Tempban ({minutes}m): {reason}", delete_message_days=0)
        log_action(guild_id, f"tempban ({minutes}m)", interaction.user, member, None, reason)
        _schedule_perm(interaction.guild_id, member.id, None, "unban", minutes * 60, interaction.user.name)
        expire_dt = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
        await interaction.response.send_message(
            f"⏱ Temporarily banned {member.mention} for **{minutes}** minute(s).\n"
            f"Auto-unban scheduled for **{expire_dt.strftime('%Y-%m-%d %H:%M UTC')}**.\nReason: {reason}",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to ban this member.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Ban failed: {e}", ephemeral=True)


# ── /slowmode ─────────────────────────────────────────────────────────────────
@tree.command(name="slowmode", description="Set slowmode on a channel (0 to disable)")
@app_commands.describe(
    seconds="Slowmode seconds (0 to disable, max 21600)",
    channel="Channel to apply slowmode (defaults to current channel)"
)
async def slowmode(interaction: discord.Interaction, seconds: int, channel: discord.TextChannel = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    if seconds < 0 or seconds > 21600:
        await interaction.response.send_message("Seconds must be 0–21600.", ephemeral=True)
        return
    target = channel or interaction.channel
    try:
        await target.edit(slowmode_delay=seconds)
        log_action(str(interaction.guild_id), "slowmode", interaction.user, None, target.name, f"{seconds}s")
        msg = f"🐢 Slowmode {'disabled' if seconds == 0 else f'set to **{seconds}s**'} in {target.mention}."
        await interaction.response.send_message(msg, ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to edit that channel.", ephemeral=True)


# ── /lock ─────────────────────────────────────────────────────────────────────
@tree.command(name="lock", description="Lock a channel so no one can send messages")
@app_commands.describe(channel="Channel to lock (defaults to current channel)")
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    target   = channel or interaction.channel
    everyone = interaction.guild.default_role
    try:
        overwrite = target.overwrites_for(everyone)
        overwrite.send_messages = False
        await target.set_permissions(everyone, overwrite=overwrite)
        log_action(str(interaction.guild_id), "lock", interaction.user, None, target.name, "")
        await interaction.response.send_message(f"🔒 {target.mention} locked.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to lock that channel.", ephemeral=True)


# ── /unlock ───────────────────────────────────────────────────────────────────
@tree.command(name="unlock", description="Unlock a previously locked channel")
@app_commands.describe(channel="Channel to unlock (defaults to current channel)")
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    target   = channel or interaction.channel
    everyone = interaction.guild.default_role
    try:
        overwrite = target.overwrites_for(everyone)
        overwrite.send_messages = None  # reset to default
        await target.set_permissions(everyone, overwrite=overwrite)
        log_action(str(interaction.guild_id), "unlock", interaction.user, None, target.name, "")
        await interaction.response.send_message(f"🔓 {target.mention} unlocked.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to unlock that channel.", ephemeral=True)


# ── /modnote ──────────────────────────────────────────────────────────────────
@tree.command(name="modnote", description="Add a private mod note about a user (never shown to them)")
@app_commands.describe(user="Who to note", text="The note content")
async def modnote(interaction: discord.Interaction, user: discord.Member, text: str):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    if len(text) > 1000:
        await interaction.response.send_message("Note must be under 1000 characters.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    notes = load_json(MODNOTES_FILE)
    if guild_id not in notes:
        notes[guild_id] = {}
    uid = str(user.id)
    if uid not in notes[guild_id]:
        notes[guild_id][uid] = []
    notes[guild_id][uid].insert(0, {
        "note": text,
        "by":   interaction.user.display_name,
        "time": time.time()
    })
    notes[guild_id][uid] = notes[guild_id][uid][:50]  # max 50 notes per user
    save_json(MODNOTES_FILE, notes)
    await interaction.response.send_message(
        f"📝 Note saved for **{user.display_name}**. Total notes: {len(notes[guild_id][uid])}.",
        ephemeral=True
    )


# ── /modnotes ─────────────────────────────────────────────────────────────────
@tree.command(name="modnotes", description="View all mod notes for a user")
@app_commands.describe(user="Who to check")
async def modnotes(interaction: discord.Interaction, user: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    notes    = load_json(MODNOTES_FILE)
    user_notes = notes.get(guild_id, {}).get(str(user.id), [])
    if not user_notes:
        await interaction.response.send_message(f"No mod notes for **{user.display_name}**.", ephemeral=True)
        return
    lines = []
    for i, n in enumerate(user_notes[:15], 1):
        ts  = datetime.datetime.utcfromtimestamp(n.get("time", 0)).strftime("%Y-%m-%d")
        lines.append(f"**{i}.** [{ts}] by {n.get('by', '?')}: {n.get('note', '')}")
    msg = f"**Mod Notes for {user.display_name}** ({len(user_notes)} total):\n" + "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await interaction.response.send_message(msg, ephemeral=True)


# ── /activepunishments ────────────────────────────────────────────────────────
@tree.command(name="activepunishments", description="List all active scheduled punishments (tempbans, removes, etc.)")
async def activepunishments(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission.", ephemeral=True)
        return
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Mod bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    schedule = load_json(PERM_SCHED_FILE) if os.path.exists(PERM_SCHED_FILE) else {"entries": []}
    entries  = schedule.get("entries", [])
    now      = time.time()
    active   = [e for e in entries if e.get("guild_id") == guild_id and e.get("execute_at", 0) > now]
    if not active:
        await interaction.response.send_message("No active scheduled punishments.", ephemeral=True)
        return
    lines = []
    for e in sorted(active, key=lambda x: x.get("execute_at", 0)):
        mins_left = int((e["execute_at"] - now) / 60)
        action    = e.get("action", "?")
        member_id = e.get("member_id", "?")
        channel   = e.get("channel_name", "") or ""
        extra     = f" in #{channel}" if channel else ""
        lines.append(f"• **{action}** — user `{member_id}`{extra} — expires in **{mins_left}m**")
    msg = f"**Active Scheduled Punishments ({len(active)}):**\n" + "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await interaction.response.send_message(msg, ephemeral=True)


client.run(TOKEN)
