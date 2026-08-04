import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
import datetime
import bot_utils

TOKEN         = os.environ.get("DISCORD_INBOX_BOT_TOKEN", "")
INBOX_FILE    = "inbox_data.json"
ARCHIVE_FILE  = "inbox_archive.json"
DISABLE_FILE  = "disable_data.json"
HUB_FILE      = "hub_data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("inbox")

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
        bot_utils.log_event("inbox", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("inbox", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("inbox", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Bot is being rate limited. Try again in a moment.", ephemeral=True)
            return
    bot_utils.log_event("inbox", "error",
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


inbox_data = load_json(INBOX_FILE)

def get_guild_data(guild_id):
    if guild_id not in inbox_data:
        inbox_data[guild_id] = {
            "category_id": None,
            "mod_role_id": None,
            "available": [],
            "pending_channels": [],
            "open_tickets": {}
        }
    return inbox_data[guild_id]

def is_bot_disabled(guild_id):
    return load_json(DISABLE_FILE).get(str(guild_id), {}).get("inbox", False)

def get_max_tickets(guild_id):
    return load_json(HUB_FILE).get(str(guild_id), {}).get("inbox_max_tickets", None)


# ── /setup ──────────────────────────────────────────────────────────────────────────────────
class SetupView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=120)
        self.guild    = guild
        self.category = None
        self.mod_role = None

        cat_options = [
            discord.SelectOption(label=cat.name, value=str(cat.id))
            for cat in guild.categories[:25]
        ]
        cat_select = discord.ui.Select(placeholder="Choose a category for inbox channels", options=cat_options)
        cat_select.callback = self.category_callback
        self.add_item(cat_select)

        role_options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in guild.roles
            if not role.is_default() and not role.managed
        ][:25]
        role_select = discord.ui.Select(placeholder="Choose the moderator role", options=role_options)
        role_select.callback = self.role_callback
        self.add_item(role_select)

    async def category_callback(self, interaction: discord.Interaction):
        self.category = interaction.guild.get_channel(int(interaction.data["values"][0]))
        await interaction.response.defer()
        await self.try_finish(interaction)

    async def role_callback(self, interaction: discord.Interaction):
        self.mod_role = interaction.guild.get_role(int(interaction.data["values"][0]))
        await interaction.response.defer()
        await self.try_finish(interaction)

    async def try_finish(self, interaction: discord.Interaction):
        if not self.category or not self.mod_role:
            return
        if getattr(self, "_finished", False):
            return  # prevent double-execution if both selects fire near-simultaneously
        self._finished = True
        guild    = interaction.guild
        guild_id = str(guild.id)
        data     = get_guild_data(guild_id)
        data["category_id"] = self.category.id
        data["mod_role_id"] = self.mod_role.id
        save_json(INBOX_FILE, inbox_data)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            self.mod_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        try:
            await self.category.edit(overwrites=overwrites)
            for ch in self.category.channels:
                await ch.edit(overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    self.mod_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                })
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to edit that category/channel permissions. "
                "Please check my Manage Channels permission and run `/setup` again.",
                ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to apply permissions: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Setup complete! Category: **{self.category.name}** | Mod role: **{self.mod_role.name}**.",
            ephemeral=True
        )
        self.stop()


@tree.command(name="setup", description="Set up the inbox bot for this server")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    if len(interaction.guild.categories) == 0:
        await interaction.response.send_message("No categories found. Create one first.", ephemeral=True)
        return
    eligible_roles = [r for r in interaction.guild.roles if not r.is_default() and not r.managed]
    if not eligible_roles:
        await interaction.response.send_message("No eligible roles found for the moderator role. Create one first.", ephemeral=True)
        return
    view = SetupView(interaction.guild)
    await interaction.response.send_message("Select the inbox category and mod role:", view=view, ephemeral=True)


# ── /available / /gone ───────────────────────────────────────────────────
@tree.command(name="available", description="Mark yourself as available for inbox tickets")
async def available(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    data     = get_guild_data(guild_id)
    uid      = interaction.user.id
    if uid not in data["available"]:
        data["available"].append(uid)
        save_json(INBOX_FILE, inbox_data)
    await interaction.response.send_message("You are now available for inbox tickets.", ephemeral=True)
    pending = data.get("pending_channels", [])
    if pending:
        for entry in pending:
            ch = interaction.guild.get_channel(entry["channel_id"])
            if ch:
                overwrites = dict(ch.overwrites)
                overwrites[interaction.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                await ch.edit(overwrites=overwrites)
                await ch.send(f"{interaction.user.mention} is now available and has joined this ticket.")
        data["pending_channels"] = []
        save_json(INBOX_FILE, inbox_data)


@tree.command(name="gone", description="Mark yourself as unavailable for inbox tickets")
async def gone(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    data     = get_guild_data(guild_id)
    uid      = interaction.user.id
    if uid in data["available"]:
        data["available"].remove(uid)
        save_json(INBOX_FILE, inbox_data)
    await interaction.response.send_message("You are now unavailable for inbox tickets.", ephemeral=True)


# ── /inbox ────────────────────────────────────────────────────────────────────────────
class InboxModal(discord.ui.Modal, title="Submit a Ticket"):
    subject = discord.ui.TextInput(label="Subject", placeholder="Brief summary", max_length=100)
    body    = discord.ui.TextInput(label="Message", placeholder="Describe your issue", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        guild    = interaction.guild
        guild_id = str(guild.id)
        data     = get_guild_data(guild_id)
        user     = interaction.user

        # Check max tickets
        max_t = get_max_tickets(guild_id)
        if max_t:
            user_open = sum(1 for t in data.get("open_tickets", {}).values() if t.get("user_id") == user.id)
            if user_open >= max_t:
                await interaction.response.send_message(f"You already have {user_open} open ticket(s). Max is {max_t}.", ephemeral=True)
                return

        category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
        if not category:
            await interaction.response.send_message("Inbox not set up. Ask an admin to run `/setup`.", ephemeral=True)
            return

        mod_role = guild.get_role(data.get("mod_role_id")) if data.get("mod_role_id") else None
        mod = None
        for uid in data["available"]:
            member = guild.get_member(uid)
            if member:
                mod = member
                break

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if mod:
            overwrites[mod] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel_name = f"ticket-{user.name}".lower().replace(" ", "-")[:90]
        try:
            channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to create channels. Please check my permissions.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Failed to create ticket channel: {e}", ephemeral=True)
            return

        if "open_tickets" not in data:
            data["open_tickets"] = {}
        data["open_tickets"][str(channel.id)] = {
            "user_id": user.id,
            "user_name": user.name,
            "subject": self.subject.value,
            "opened": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        }
        save_json(INBOX_FILE, inbox_data)

        if mod:
            await channel.send(f"{user.mention} {mod.mention}\n**Subject:** {self.subject.value}\n\n{self.body.value}")
        else:
            await channel.send(
                f"{user.mention}\n**Subject:** {self.subject.value}\n\n{self.body.value}\n\n"
                f"⚠️ No moderators are available right now. One will join when they return."
            )
            data["pending_channels"].append({"channel_id": channel.id, "user_id": user.id})
            save_json(INBOX_FILE, inbox_data)

        await interaction.response.send_message(f"Your ticket has been created: {channel.mention}", ephemeral=True)


@tree.command(name="inbox", description="Submit a ticket to the moderators")
async def inbox(interaction: discord.Interaction):
    if is_bot_disabled(str(interaction.guild_id)):
        await interaction.response.send_message("Inbox bot is disabled.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    data     = get_guild_data(guild_id)
    if not data.get("category_id"):
        await interaction.response.send_message("Inbox isn't set up yet. Ask an admin to run `/setup`.", ephemeral=True)
        return
    await interaction.response.send_modal(InboxModal())


# ── /close ───────────────────────────────────────────────────────────────────────────────
def is_mod_or_admin(interaction: discord.Interaction, data: dict) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    mod_role_id = data.get("mod_role_id")
    if mod_role_id:
        role = interaction.guild.get_role(int(mod_role_id))
        if role and role in interaction.user.roles:
            return True
    return False


@tree.command(name="close", description="Close this ticket, save transcript, and delete the channel")
async def close(interaction: discord.Interaction):
    guild_id   = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    data       = get_guild_data(guild_id)

    if not is_mod_or_admin(interaction, data):
        await interaction.response.send_message("You need the mod role or admin to close tickets.", ephemeral=True)
        return

    if channel_id not in data.get("open_tickets", {}):
        await interaction.response.send_message("This doesn't appear to be an active ticket channel.", ephemeral=True)
        return

    ticket_info = data["open_tickets"][channel_id]
    await interaction.response.send_message("Closing ticket and saving transcript...", ephemeral=True)

    # Collect transcript
    messages = []
    async for msg in interaction.channel.history(limit=500, oldest_first=True):
        messages.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.name}: {msg.content}")
    transcript = "\n".join(messages)

    # Save to archive
    archive = load_json(ARCHIVE_FILE)
    if guild_id not in archive:
        archive[guild_id] = {}
    archive[guild_id][channel_id] = {
        "ticket_info": ticket_info,
        "transcript": transcript,
        "closed": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "closed_by": interaction.user.name
    }
    save_json(ARCHIVE_FILE, archive)

    # Remove from open tickets and pending list
    del data["open_tickets"][channel_id]
    data["pending_channels"] = [e for e in data.get("pending_channels", []) if e.get("channel_id") != int(channel_id)]
    save_json(INBOX_FILE, inbox_data)

    await asyncio.sleep(2)
    try:
        await interaction.channel.delete()
    except discord.Forbidden:
        await interaction.followup.send(
            "⚠️ Could not delete channel — missing Manage Channels permission. Please delete it manually.",
            ephemeral=True
        )
    except discord.HTTPException:
        pass


# ── /archive ────────────────────────────────────────────────────────────────────────────
@tree.command(name="archive", description="View and reopen closed tickets")
@app_commands.default_permissions(administrator=True)
async def archive(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    arc      = load_json(ARCHIVE_FILE).get(guild_id, {})
    if not arc:
        await interaction.response.send_message("No archived tickets.", ephemeral=True)
        return

    options = []
    for ch_id, info in list(arc.items())[-25:]:
        ti   = info.get("ticket_info", {})
        label = f"{ti.get('user_name', 'Unknown')} — {ti.get('subject', 'No subject')[:50]}"
        options.append(discord.SelectOption(label=label, value=ch_id))

    view = ArchiveView(options, guild_id)
    await interaction.response.send_message("Select a ticket to view or reopen:", view=view, ephemeral=True)


class ArchiveView(discord.ui.View):
    def __init__(self, options, guild_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        select = discord.ui.Select(placeholder="Choose a closed ticket", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        ch_id   = interaction.data["values"][0]
        arc     = load_json(ARCHIVE_FILE)
        info    = arc.get(self.guild_id, {}).get(ch_id)
        if not info:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        ti = info.get("ticket_info", {})
        summary = (
            f"**Ticket from:** {ti.get('user_name', 'Unknown')}\n"
            f"**Subject:** {ti.get('subject', 'N/A')}\n"
            f"**Opened:** {ti.get('opened', 'N/A')}\n"
            f"**Closed:** {info.get('closed', 'N/A')} by {info.get('closed_by', 'N/A')}\n\n"
            f"**Transcript preview:**\n```{info.get('transcript', '')[:800]}```"
        )

        reopen_view = ReopenView(ch_id, self.guild_id, ti)
        await interaction.response.send_message(summary, view=reopen_view, ephemeral=True)


class ReopenView(discord.ui.View):
    def __init__(self, ch_id, guild_id, ticket_info):
        super().__init__(timeout=60)
        self.ch_id       = ch_id
        self.guild_id    = guild_id
        self.ticket_info = ticket_info
        self._reopened   = False

    @discord.ui.button(label="Reopen Ticket", style=discord.ButtonStyle.green)
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._reopened:
            await interaction.response.send_message("This ticket has already been reopened.", ephemeral=True)
            return
        self._reopened = True
        data     = get_guild_data(self.guild_id)
        guild    = interaction.guild
        category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
        mod_role = guild.get_role(data.get("mod_role_id")) if data.get("mod_role_id") else None

        if not category:
            self._reopened = False
            await interaction.response.send_message("❌ The inbox category no longer exists. Run `/setup` again.", ephemeral=True)
            return

        user_id = self.ticket_info.get("user_id")
        user    = guild.get_member(user_id) if user_id else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if user:
            overwrites[user] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel_name = f"ticket-{self.ticket_info.get('user_name', 'unknown')}".lower().replace(" ", "-")[:90]
        try:
            channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
        except discord.Forbidden:
            self._reopened = False
            await interaction.response.send_message("❌ Missing permission to create channels.", ephemeral=True)
            return
        except discord.HTTPException as e:
            self._reopened = False
            await interaction.response.send_message(f"❌ Failed to create channel: {e}", ephemeral=True)
            return
        await channel.send(
            f"🔄 Ticket reopened by {interaction.user.mention}.\n"
            f"**Original subject:** {self.ticket_info.get('subject', 'N/A')}"
        )
        if "open_tickets" not in data:
            data["open_tickets"] = {}
        data["open_tickets"][str(channel.id)] = {
            "user_id": user_id,
            "user_name": self.ticket_info.get("user_name"),
            "subject": self.ticket_info.get("subject"),
            "opened": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        }
        save_json(INBOX_FILE, inbox_data)
        await interaction.response.send_message(f"Ticket reopened: {channel.mention}", ephemeral=True)


@tree.command(name="tickets", description="List open tickets (mods see all; users see their own)")
async def tickets(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Inbox bot is disabled.", ephemeral=True)
        return
    data         = get_guild_data(guild_id)
    open_tickets = data.get("open_tickets", {})
    if not open_tickets:
        await interaction.response.send_message("No open tickets.", ephemeral=True)
        return
    is_mod_user = is_mod_or_admin(interaction, data)
    uid         = interaction.user.id
    lines       = []
    for ch_id, info in open_tickets.items():
        if not is_mod_user and info.get("user_id") != uid:
            continue
        ch         = interaction.guild.get_channel(int(ch_id)) if ch_id.isdigit() else None
        ch_ref     = ch.mention if ch else f"#deleted ({ch_id})"
        subject    = info.get("subject", "No subject")
        opener     = info.get("user_name", "Unknown")
        priority   = info.get("priority", "")
        pri_icon   = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(priority, "")
        assigned   = info.get("assigned_mod_name", "")
        extra      = f" → {assigned}" if assigned else ""
        lines.append(f"{pri_icon} {ch_ref} — **{subject}** by {opener}{extra}")
    if not lines:
        msg = "No open tickets for you." if not is_mod_user else "No open tickets."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    header = "**All Open Tickets:**\n" if is_mod_user else "**Your Open Tickets:**\n"
    msg    = header + "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="note", description="Add a private mod note to this ticket channel (pinned)")
@app_commands.describe(text="The note content")
async def note(interaction: discord.Interaction, text: str):
    guild_id   = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    data       = get_guild_data(guild_id)
    if not is_mod_or_admin(interaction, data):
        await interaction.response.send_message("Only mods can add notes.", ephemeral=True)
        return
    if channel_id not in data.get("open_tickets", {}):
        await interaction.response.send_message("This command only works inside a ticket channel.", ephemeral=True)
        return
    if len(text) > 1000:
        await interaction.response.send_message("Note must be under 1000 characters.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        note_msg = await interaction.channel.send(
            f"📌 **Mod Note** by {interaction.user.mention}:\n{text}"
        )
        try:
            await note_msg.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.followup.send("Note added and pinned.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to send messages here.", ephemeral=True)


@tree.command(name="assign", description="Assign this ticket to a specific mod")
@app_commands.describe(mod="Mod to assign this ticket to")
async def assign(interaction: discord.Interaction, mod: discord.Member):
    guild_id   = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    data       = get_guild_data(guild_id)
    if not is_mod_or_admin(interaction, data):
        await interaction.response.send_message("Only mods can assign tickets.", ephemeral=True)
        return
    open_tickets = data.get("open_tickets", {})
    if channel_id not in open_tickets:
        await interaction.response.send_message("This command only works inside a ticket channel.", ephemeral=True)
        return
    open_tickets[channel_id]["assigned_mod_id"]   = mod.id
    open_tickets[channel_id]["assigned_mod_name"] = mod.display_name
    # Save
    all_data = load_json(INBOX_FILE)
    if guild_id not in all_data:
        all_data[guild_id] = {}
    all_data[guild_id]["open_tickets"] = open_tickets
    save_json(INBOX_FILE, all_data)
    await interaction.response.send_message(
        f"✅ Ticket assigned to {mod.mention}. They have been notified."
    )
    try:
        await mod.send(
            f"📬 You have been assigned a ticket in **{interaction.guild.name}**: {interaction.channel.mention}"
        )
    except discord.Forbidden:
        pass


@tree.command(name="priority", description="Set the priority of this ticket")
@app_commands.describe(level="Priority level")
@app_commands.choices(level=[
    app_commands.Choice(name="🔴 High",   value="high"),
    app_commands.Choice(name="🟡 Medium", value="medium"),
    app_commands.Choice(name="⚪ Low",    value="low"),
])
async def priority(interaction: discord.Interaction, level: str):
    guild_id   = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    data       = get_guild_data(guild_id)
    if not is_mod_or_admin(interaction, data):
        await interaction.response.send_message("Only mods can set ticket priority.", ephemeral=True)
        return
    open_tickets = data.get("open_tickets", {})
    if channel_id not in open_tickets:
        await interaction.response.send_message("This command only works inside a ticket channel.", ephemeral=True)
        return
    open_tickets[channel_id]["priority"] = level
    all_data = load_json(INBOX_FILE)
    if guild_id not in all_data:
        all_data[guild_id] = {}
    all_data[guild_id]["open_tickets"] = open_tickets
    save_json(INBOX_FILE, all_data)
    icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}[level]
    await interaction.response.send_message(f"{icon} Ticket priority set to **{level}**.")


@client.event
async def on_ready():
    await tree.sync()
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"Inbox Bot logged in as {client.user}")


client.run(TOKEN)
