import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import secrets
import asyncio
import time
import bot_utils

TOKEN        = os.environ.get("DISCORD_HUB_BOT_TOKEN", "")
BOT_DIR       = os.path.dirname(os.path.abspath(__file__))
HUB_FILE     = os.path.join(BOT_DIR, "hub_data.json")
LINK_FILE    = os.path.join(BOT_DIR, "link_data.json")
DISABLE_FILE = os.path.join(BOT_DIR, "disable_data.json")

VALID_BOTS    = {"counting", "file", "mod", "inbox", "vibe", "games", "python"}
LINK_CODE_TTL = 3600  # link codes expire after 1 hour

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

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

def get_hub(guild_id):
    """Return the guild-specific hub settings dict, creating defaults if missing."""
    data = load_json(HUB_FILE)
    if guild_id not in data:
        data[guild_id] = {
            "counting_modes": {"mode1": True, "mode2": True, "mode3": True},
            "file_limit_mb": None,
            "mod_counting_link": False,
            "sudo_enabled": False,
            "scripts_public": False,
        }
        save_json(HUB_FILE, data)
    return data[guild_id]

def get_link_data():
    return load_json(LINK_FILE)

def get_disable_data():
    return load_json(DISABLE_FILE)


# ── Autocomplete helpers ───────────────────────────────────────────────────────

async def hub_bot_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=b, value=b)
        for b in sorted(BOT_OPTIONS.keys())
        if current.lower() in b
    ]

async def valid_bot_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=b, value=b)
        for b in sorted(VALID_BOTS)
        if current.lower() in b
    ]


# ── Embed builders ─────────────────────────────────────────────────────────────

def build_hub_embed(guild_id: str, bot_name: str) -> discord.Embed:
    hub = get_hub(guild_id)
    embed = discord.Embed(
        title=f"⚙️ {bot_name.title()} Bot — Settings",
        color=discord.Color.blurple()
    )

    if bot_name == "counting":
        modes = hub.get("counting_modes", {})
        lines = []
        descs = ["1 · 2 · 3 (sequential)", "Powers of 2", "Fibonacci"]
        for i in range(1, 4):
            on = modes.get(f"mode{i}", True)
            lines.append(f"{'🟢' if on else '🔴'} **Mode {i}** — {descs[i-1]}")
        embed.description = "\n".join(lines)

    elif bot_name == "file":
        limit = hub.get("file_limit_mb")
        embed.description = (
            f"File size limit: **{limit} MB**" if limit
            else "File size limit: **not set** (8 MB Discord default applies)"
        )

    elif bot_name == "mod":
        link = hub.get("mod_counting_link", False)
        embed.description = (
            f"{'🟢' if link else '🔴'} **Counting bot link** — "
            f"{'violations reset the counting channel' if link else 'off'}"
        )

    elif bot_name == "vibe":
        welcome = hub.get("welcome_enabled", True)
        bonus   = hub.get("welcome_economy_amount", 0)
        wait    = hub.get("vibe_join_wait", 0)
        embed.description = (
            f"{'🟢' if welcome else '🔴'} **Welcome messages**: {'on' if welcome else 'off'}\n"
            f"💰 **Welcome bonus**: ${bonus}\n"
            f"⏱ **Join wait**: {wait}s"
        )

    elif bot_name == "games":
        cost  = hub.get("game_cost", 20)
        wait  = hub.get("game_join_wait", 30)
        min_l = hub.get("hangman_min_letters", 4)
        max_l = hub.get("hangman_max_letters", 10)
        embed.description = (
            f"💰 **Entry cost**: ${cost}\n"
            f"⏱ **Join wait**: {wait}s\n"
            f"🔤 **Hangman word length**: {min_l}–{max_l} letters"
        )

    elif bot_name == "inbox":
        max_t = hub.get("inbox_max_tickets", 1)
        embed.description = f"🎫 **Max open tickets per user**: {max_t}"

    elif bot_name == "python":
        sudo   = hub.get("sudo_enabled", False)
        public = hub.get("scripts_public", False)
        embed.description = (
            f"{'🟢' if sudo else '🔴'} **Sudo commands**: {'on' if sudo else 'off'}\n"
            f"{'🟢' if public else '🔴'} **Public scripts**: {'on' if public else 'off'}"
        )

    embed.set_footer(text="Buttons below change settings • changes apply immediately")
    return embed


def build_status_embed(guild_id: str, guild_name: str) -> discord.Embed:
    disable_data = get_disable_data().get(guild_id, {})
    hub          = get_hub(guild_id)
    link_data    = get_link_data()
    partner      = link_data.get("links", {}).get(guild_id)
    now          = time.time()
    pause_until  = disable_data.get("pause_until", 0)

    bot_display = {
        "counting": "Counting",
        "file":     "File Upload",
        "mod":      "Moderation",
        "inbox":    "Inbox / Tickets",
        "vibe":     "Vibe / Economy",
        "games":    "Games",
        "python":   "Python Scripts",
    }

    any_disabled = any(disable_data.get(b, False) for b in VALID_BOTS)
    color = discord.Color.orange() if any_disabled else discord.Color.green()
    if pause_until and now < pause_until:
        color = discord.Color.red()

    lines = []
    for key, display in bot_display.items():
        disabled = disable_data.get(key, False)
        lines.append(f"{'🔴' if disabled else '🟢'} **{display}**")

    embed = discord.Embed(
        title=f"🤖 Bot Status — {guild_name}",
        description="\n".join(lines),
        color=color
    )

    # Pause status
    if pause_until and now < pause_until:
        remaining = int(pause_until - now)
        mins, secs = divmod(remaining, 60)
        embed.add_field(name="⏸ Paused", value=f"**{mins}m {secs}s** remaining\nUse `/resume` to end early", inline=True)
    else:
        embed.add_field(name="⏸ Paused", value="No", inline=True)

    # Link status
    if partner:
        canonical = min(guild_id, partner)
        embed.add_field(name="🔗 Linked Server", value=f"`{partner}`\nShared economy key: `{canonical}`", inline=True)
    else:
        embed.add_field(name="🔗 Linked", value="Not linked\n`/link` to pair servers", inline=True)

    # Key settings snapshot
    modes      = hub.get("counting_modes", {})
    active_m   = [str(i) for i in range(1, 4) if modes.get(f"mode{i}", True)]
    file_limit = hub.get("file_limit_mb")
    game_cost  = hub.get("game_cost", 20)
    max_t      = hub.get("inbox_max_tickets", 1)
    welcome    = hub.get("welcome_enabled", True)

    settings = (
        f"Counting modes: {', '.join(active_m) or 'none'}\n"
        f"File limit: {f'{file_limit} MB' if file_limit else 'default'}\n"
        f"Game entry cost: ${game_cost}\n"
        f"Max tickets: {max_t}/user\n"
        f"Welcome messages: {'on' if welcome else 'off'}"
    )
    embed.add_field(name="⚙️ Key Settings", value=settings, inline=False)
    embed.set_footer(text="/hub <bot> to configure  •  /disable /enable /pause /resume to control")
    return embed


# ── /link ─────────────────────────────────────────────────────────────────────
@tree.command(name="link", description="Generate a code to link this server with another")
@app_commands.default_permissions(administrator=True)
async def link(interaction: discord.Interaction):
    guild_id  = str(interaction.guild_id)
    code      = secrets.token_hex(4).upper()
    link_data = get_link_data()
    if "pending" not in link_data:
        link_data["pending"] = {}
    link_data["pending"][code] = {"guild_id": guild_id, "created_at": time.time()}
    save_json(LINK_FILE, link_data)
    await interaction.response.send_message(
        f"Your link code is: **`{code}`**\n"
        "Share this with the other server's admin. Expires in **1 hour**.",
        ephemeral=True
    )


# ── /connect ──────────────────────────────────────────────────────────────────
@tree.command(name="connect", description="Connect this server to another using a link code")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(code="The code from the other server's /link command")
async def connect(interaction: discord.Interaction, code: str):
    guild_id  = str(interaction.guild_id)
    link_data = get_link_data()
    pending   = link_data.get("pending", {})
    if code not in pending:
        await interaction.response.send_message("Invalid or expired code.", ephemeral=True)
        return
    entry = pending[code]
    if isinstance(entry, dict):
        if time.time() - entry.get("created_at", 0) > LINK_CODE_TTL:
            del pending[code]
            save_json(LINK_FILE, link_data)
            await interaction.response.send_message(
                "This code has expired. Please generate a new one with `/link`.", ephemeral=True
            )
            return
        other_guild_id = entry["guild_id"]
    else:
        other_guild_id = entry  # legacy string format
    if other_guild_id == guild_id:
        await interaction.response.send_message("You can't link a server to itself.", ephemeral=True)
        return
    if "links" not in link_data:
        link_data["links"] = {}
    link_data["links"][guild_id]       = other_guild_id
    link_data["links"][other_guild_id] = guild_id
    del pending[code]
    save_json(LINK_FILE, link_data)
    await interaction.response.send_message(
        "✅ Servers linked! Economy balances and leaderboards are now shared between both servers.\n"
        "Use `/linkstatus` to confirm the connection.",
        ephemeral=True
    )


# ── /hub ──────────────────────────────────────────────────────────────────────
BOT_OPTIONS = {
    "counting": ["Toggle Mode 1", "Toggle Mode 2", "Toggle Mode 3"],
    "file":     ["Set file size limit"],
    "mod":      ["Toggle counting bot link"],
    "python":   ["Toggle sudo commands", "Toggle public scripts"],
    "vibe":     ["Toggle welcome messages", "Set welcome economy bonus", "Set join wait time"],
    "games":    ["Set game cost", "Set game join wait", "Set hangman min letters", "Set hangman max letters"],
    "inbox":    ["Set max open tickets per user"],
}

# Options that open a modal (can't edit parent message after)
MODAL_OPTIONS = {
    "Set file size limit", "Set welcome economy bonus", "Set join wait time",
    "Set game cost", "Set game join wait", "Set hangman min letters",
    "Set hangman max letters", "Set max open tickets per user",
}


class FileLimitModal(discord.ui.Modal, title="Set File Size Limit"):
    limit = discord.ui.TextInput(label="Limit in MB", placeholder="e.g. 25", max_length=10)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val     = float(self.limit.value.strip())
            hub_all = load_json(HUB_FILE)
            if self.guild_id not in hub_all:
                hub_all[self.guild_id] = {}
            hub_all[self.guild_id]["file_limit_mb"] = val
            save_json(HUB_FILE, hub_all)
            await interaction.response.send_message(f"File size limit set to **{val} MB**.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a number.", ephemeral=True)


class NumericSettingModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Value", max_length=10)

    def __init__(self, title_str, label, guild_id, hub_key, description, cast=int):
        super().__init__(title=title_str)
        self.value.label = label
        self.guild_id    = guild_id
        self.hub_key     = hub_key
        self.description = description
        self.cast        = cast

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val     = self.cast(self.value.value.strip())
            hub_all = load_json(HUB_FILE)
            if self.guild_id not in hub_all:
                hub_all[self.guild_id] = {}
            hub_all[self.guild_id][self.hub_key] = val
            save_json(HUB_FILE, hub_all)
            await interaction.response.send_message(f"{self.description} set to **{val}**.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


class HubView(discord.ui.View):
    def __init__(self, guild_id, bot_name):
        super().__init__()
        self.guild_id = guild_id
        self.bot_name = bot_name
        hub = get_hub(guild_id)

        for i, opt in enumerate(BOT_OPTIONS[bot_name]):
            is_modal = opt in MODAL_OPTIONS
            style = discord.ButtonStyle.primary if is_modal else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=opt, style=style, row=min(i, 4))
            btn.callback = self.make_callback(opt, i)
            self.add_item(btn)

    def make_callback(self, opt, idx):
        async def callback(interaction: discord.Interaction):
            hub     = get_hub(self.guild_id)
            hub_all = load_json(HUB_FILE)

            if self.bot_name == "counting":
                mode_key = f"mode{idx+1}"
                hub["counting_modes"][mode_key] = not hub["counting_modes"].get(mode_key, True)
                hub_all[self.guild_id] = hub
                save_json(HUB_FILE, hub_all)
                await interaction.response.edit_message(
                    embed=build_hub_embed(self.guild_id, self.bot_name),
                    view=HubView(self.guild_id, self.bot_name)
                )

            elif self.bot_name == "mod":
                hub["mod_counting_link"] = not hub["mod_counting_link"]
                hub_all[self.guild_id] = hub
                save_json(HUB_FILE, hub_all)
                await interaction.response.edit_message(
                    embed=build_hub_embed(self.guild_id, self.bot_name),
                    view=HubView(self.guild_id, self.bot_name)
                )

            elif self.bot_name == "python":
                if opt == "Toggle sudo commands":
                    hub["sudo_enabled"] = not hub["sudo_enabled"]
                else:
                    hub["scripts_public"] = not hub["scripts_public"]
                hub_all[self.guild_id] = hub
                save_json(HUB_FILE, hub_all)
                await interaction.response.edit_message(
                    embed=build_hub_embed(self.guild_id, self.bot_name),
                    view=HubView(self.guild_id, self.bot_name)
                )

            elif self.bot_name == "file":
                await interaction.response.send_modal(FileLimitModal(self.guild_id))

            elif self.bot_name == "vibe":
                if opt == "Toggle welcome messages":
                    hub["welcome_enabled"] = not hub.get("welcome_enabled", True)
                    hub_all[self.guild_id] = hub
                    save_json(HUB_FILE, hub_all)
                    await interaction.response.edit_message(
                        embed=build_hub_embed(self.guild_id, self.bot_name),
                        view=HubView(self.guild_id, self.bot_name)
                    )
                elif opt == "Set welcome economy bonus":
                    await interaction.response.send_modal(NumericSettingModal(
                        "Set Welcome Economy Bonus", "Amount ($)", self.guild_id,
                        "welcome_economy_amount", "Welcome economy bonus"
                    ))
                elif opt == "Set join wait time":
                    await interaction.response.send_modal(NumericSettingModal(
                        "Set Vibe Join Wait", "Seconds (0–60)", self.guild_id,
                        "vibe_join_wait", "Vibe join wait"
                    ))

            elif self.bot_name == "games":
                if opt == "Set game cost":
                    await interaction.response.send_modal(NumericSettingModal(
                        "Set Game Entry Cost", "Cost in $ (default 20)", self.guild_id,
                        "game_cost", "Game entry cost"
                    ))
                elif opt == "Set game join wait":
                    await interaction.response.send_modal(NumericSettingModal(
                        "Set Game Join Wait", "Seconds (0–60)", self.guild_id,
                        "game_join_wait", "Game join wait"
                    ))
                elif opt == "Set hangman min letters":
                    await interaction.response.send_modal(NumericSettingModal(
                        "Set Hangman Min Letters", "Min word length (1–10)", self.guild_id,
                        "hangman_min_letters", "Hangman min word length"
                    ))
                elif opt == "Set hangman max letters":
                    await interaction.response.send_modal(NumericSettingModal(
                        "Set Hangman Max Letters", "Max word length (5–20)", self.guild_id,
                        "hangman_max_letters", "Hangman max word length"
                    ))

            elif self.bot_name == "inbox":
                await interaction.response.send_modal(NumericSettingModal(
                    "Set Max Open Tickets", "Max tickets per user (e.g. 2)", self.guild_id,
                    "inbox_max_tickets", "Max open tickets per user"
                ))

        return callback


@tree.command(name="hub", description="Configure a bot's settings for this server")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(bot="Which bot to configure")
@app_commands.autocomplete(bot=hub_bot_autocomplete)
async def hub(interaction: discord.Interaction, bot: str):
    guild_id = str(interaction.guild_id)
    bot      = bot.lower()
    if bot not in BOT_OPTIONS:
        opts = ", ".join(f"`{b}`" for b in sorted(BOT_OPTIONS))
        await interaction.response.send_message(f"Unknown bot `{bot}`. Options: {opts}.", ephemeral=True)
        return
    embed = build_hub_embed(guild_id, bot)
    view  = HubView(guild_id, bot)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ── /status ───────────────────────────────────────────────────────────────────
@tree.command(name="status", description="Show the current status of all bots in this server")
@app_commands.default_permissions(administrator=True)
async def status(interaction: discord.Interaction):
    guild_id   = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else "Unknown"
    embed      = build_status_embed(guild_id, guild_name)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /linkstatus ───────────────────────────────────────────────────────────────
@tree.command(name="linkstatus", description="Show whether this server is linked to another")
@app_commands.default_permissions(administrator=True)
async def linkstatus(interaction: discord.Interaction):
    guild_id  = str(interaction.guild_id)
    link_data = get_link_data()
    partner   = link_data.get("links", {}).get(guild_id)
    if partner:
        canonical = min(guild_id, partner)
        await interaction.response.send_message(
            f"✅ This server is linked to server ID `{partner}`.\n"
            f"Shared economy key: `{canonical}`\n"
            "Economy balances and leaderboards are unified between both servers.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ This server is not linked to any other server.\n"
            "Use `/link` to generate a code and `/connect` on the other server to pair them.",
            ephemeral=True
        )


# ── /unlink ───────────────────────────────────────────────────────────────────
@tree.command(name="unlink", description="Unlink this server from its paired server")
@app_commands.default_permissions(administrator=True)
async def unlink(interaction: discord.Interaction):
    guild_id  = str(interaction.guild_id)
    link_data = get_link_data()
    partner   = link_data.get("links", {}).get(guild_id)
    if not partner:
        await interaction.response.send_message("This server isn't linked to any other server.", ephemeral=True)
        return
    link_data["links"].pop(guild_id, None)
    link_data["links"].pop(partner, None)
    save_json(LINK_FILE, link_data)
    await interaction.response.send_message(
        f"🔗 Server unlinked from `{partner}`. Economy data is no longer shared.\n"
        "Existing balances remain intact under each server's own key.",
        ephemeral=True
    )


# ── /disable ──────────────────────────────────────────────────────────────────
@tree.command(name="disable", description="Disable a bot for this server")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(bot="Which bot to disable")
@app_commands.autocomplete(bot=valid_bot_autocomplete)
async def disable(interaction: discord.Interaction, bot: str):
    bot_name = bot.lower()
    if bot_name not in VALID_BOTS:
        await interaction.response.send_message(
            f"Unknown bot `{bot}`. Valid options: `{'`, `'.join(sorted(VALID_BOTS))}`", ephemeral=True
        )
        return
    guild_id     = str(interaction.guild_id)
    disable_data = get_disable_data()
    if guild_id not in disable_data:
        disable_data[guild_id] = {}
    disable_data[guild_id][bot_name] = True
    save_json(DISABLE_FILE, disable_data)
    await interaction.response.send_message(f"🔴 **{bot_name}** bot disabled.", ephemeral=True)


# ── /enable ───────────────────────────────────────────────────────────────────
@tree.command(name="enable", description="Re-enable a bot for this server")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(bot="Which bot to enable")
@app_commands.autocomplete(bot=valid_bot_autocomplete)
async def enable(interaction: discord.Interaction, bot: str):
    bot_name = bot.lower()
    if bot_name not in VALID_BOTS:
        await interaction.response.send_message(
            f"Unknown bot `{bot}`. Valid options: `{'`, `'.join(sorted(VALID_BOTS))}`", ephemeral=True
        )
        return
    guild_id     = str(interaction.guild_id)
    disable_data = get_disable_data()
    if guild_id not in disable_data:
        disable_data[guild_id] = {}
    disable_data[guild_id][bot_name] = False
    save_json(DISABLE_FILE, disable_data)
    await interaction.response.send_message(f"🟢 **{bot_name}** bot re-enabled.", ephemeral=True)


# ── /pause ────────────────────────────────────────────────────────────────────
@tree.command(name="pause", description="Pause all bots for a set number of minutes")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(minutes="How many minutes to pause (1–1440)")
async def pause(interaction: discord.Interaction, minutes: int):
    if minutes < 1 or minutes > 1440:
        await interaction.response.send_message("Minutes must be between 1 and 1440 (24h).", ephemeral=True)
        return
    guild_id     = str(interaction.guild_id)
    disable_data = get_disable_data()
    if guild_id not in disable_data:
        disable_data[guild_id] = {}
    for bot in VALID_BOTS:
        disable_data[guild_id][bot] = True
    disable_data[guild_id]["pause_until"] = time.time() + minutes * 60
    save_json(DISABLE_FILE, disable_data)
    await interaction.response.send_message(
        f"⏸ All bots paused for **{minutes}** minute(s). Use `/resume` to end early.", ephemeral=True
    )


# ── /resume ───────────────────────────────────────────────────────────────────
@tree.command(name="resume", description="Resume all bots immediately (cancels an active pause)")
@app_commands.default_permissions(administrator=True)
async def resume(interaction: discord.Interaction):
    guild_id     = str(interaction.guild_id)
    disable_data = get_disable_data()
    if guild_id not in disable_data:
        await interaction.response.send_message("No active pause for this server.", ephemeral=True)
        return
    pause_until = disable_data[guild_id].get("pause_until", 0)
    if not pause_until or time.time() >= pause_until:
        await interaction.response.send_message("No active pause to cancel.", ephemeral=True)
        return
    for bot in VALID_BOTS:
        disable_data[guild_id][bot] = False
    disable_data[guild_id].pop("pause_until", None)
    save_json(DISABLE_FILE, disable_data)
    await interaction.response.send_message("▶️ All bots resumed.", ephemeral=True)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("hub")

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
        bot_utils.log_event("hub", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("hub", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("hub", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Bot is being rate limited. Try again in a moment.", ephemeral=True)
            return
    bot_utils.log_event("hub", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


@tasks.loop(minutes=1)
async def check_pause_expiry():
    """Re-enable bots when their pause timer expires."""
    now          = time.time()
    disable_data = get_disable_data()
    changed      = False
    for guild_id, settings in disable_data.items():
        expiry = settings.get("pause_until")
        if expiry and now >= expiry:
            for bot in VALID_BOTS:
                settings[bot] = False
            settings.pop("pause_until", None)
            changed = True
    if changed:
        save_json(DISABLE_FILE, disable_data)


# ── /error ────────────────────────────────────────────────────────────────────
@tree.command(name="error", description="Manually report an error for a bot (visible in dashboard)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    bot_name="Which bot: hub, mod, counting, file, inbox, vibe, games",
    description="Description of the error"
)
async def error_report(interaction: discord.Interaction, bot_name: str, description: str):
    valid = {"hub", "mod", "counting", "file", "inbox", "vibe", "games"}
    if bot_name.lower() not in valid:
        await interaction.response.send_message(
            f"Unknown bot. Valid: {', '.join(sorted(valid))}", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    event_id = bot_utils.log_event(
        bot_name.lower(), "user_report",
        f"[Manual report by {interaction.user.name}]: {description}",
        guild_id=guild_id
    )
    await interaction.response.send_message(
        f"Error reported for **{bot_name}** (ID: `{event_id[:8]}…`). It will appear in the dashboard.",
        ephemeral=True
    )


# ── /announce ─────────────────────────────────────────────────────────────────
@tree.command(name="announce", description="Post a message to any channel in this server")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    channel="Channel to post in",
    message="Message to send"
)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if len(message) > 2000:
        await interaction.response.send_message("Message too long (max 2000 characters).", ephemeral=True)
        return
    try:
        await channel.send(message)
        await interaction.response.send_message(
            f"✅ Message sent to {channel.mention}.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I don't have permission to send messages in {channel.mention}.", ephemeral=True
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Failed to send message: {e}", ephemeral=True)


# ── /backup ───────────────────────────────────────────────────────────────────
@tree.command(name="backup", description="Zip all data files and DM them to you")
@app_commands.default_permissions(administrator=True)
async def backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    import zipfile, io
    buf = io.BytesIO()
    json_files = [f for f in os.listdir(BOT_DIR) if f.endswith(".json")]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in json_files:
            fpath = os.path.join(BOT_DIR, fname)
            try:
                zf.write(fpath, fname)
            except Exception:
                pass
    buf.seek(0)
    size_mb = buf.getbuffer().nbytes / 1024 / 1024
    if size_mb > 8:
        # Too big for Discord DM — list files and sizes instead
        lines = []
        for fname in json_files:
            fpath = os.path.join(BOT_DIR, fname)
            try:
                sz = os.path.getsize(fpath) / 1024
                lines.append(f"• `{fname}` — {sz:.1f} KB")
            except Exception:
                pass
        await interaction.followup.send(
            f"Backup zip is {size_mb:.1f} MB — too large for Discord DMs.\n\n**Files:**\n" + "\n".join(lines),
            ephemeral=True
        )
        return
    try:
        await interaction.user.send(
            content=f"📦 Bot data backup — {len(json_files)} JSON files",
            file=discord.File(buf, filename="bot_backup.zip")
        )
        await interaction.followup.send(
            f"✅ Backup sent to your DMs ({size_mb:.1f} MB, {len(json_files)} files).",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't DM you — please enable DMs from server members.", ephemeral=True
        )


@client.event
async def on_ready():
    await tree.sync()
    if not check_pause_expiry.is_running():
        check_pause_expiry.start()
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"Hub Bot logged in as {client.user}")


client.run(TOKEN)
