import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
import json
import os
import time
import datetime
import bot_utils

TOKEN        = os.environ.get("DISCORD_FILE_BOT_TOKEN", "")
DISABLE_FILE = "disable_data.json"
HUB_FILE     = "hub_data.json"
FILE_LOG     = "file_log.json"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("file")

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
        bot_utils.log_event("file", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("file", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)


# ── JSON helpers ────────────────────────────────────────────────────────────

def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def is_bot_disabled(guild_id):
    data = load_json(DISABLE_FILE) if os.path.exists(DISABLE_FILE) else {}
    return data.get(str(guild_id), {}).get("file", False)

def get_file_limit(guild_id):
    limit = load_json(HUB_FILE).get(str(guild_id), {}).get("file_limit_mb", None)
    return limit


# ── File-log helpers ─────────────────────────────────────────────────────────

def load_file_log():
    if not os.path.exists(FILE_LOG):
        return {}
    with open(FILE_LOG, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def log_file_upload(guild_id, user_id, user_name, file_name, size_bytes, url, channel_id):
    data = load_file_log()
    gid = str(guild_id)
    if gid not in data:
        data[gid] = []
    data[gid].insert(0, {
        "user_id":    str(user_id),
        "user_name":  user_name,
        "file_name":  file_name,
        "size_bytes": size_bytes,
        "url":        url,
        "timestamp":  time.time(),
        "channel_id": str(channel_id)
    })
    data[gid] = data[gid][:1000]
    tmp = FILE_LOG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, FILE_LOG)


# ── Renderable file types ─────────────────────────────────────────────────────

# Extension → code fence language tag. None = render as plain Discord text (e.g. .md)
LANG_MAP = {
    ".md":    None,
    ".txt":   "",
    ".py":    "py",    ".pyw":  "py",
    ".js":    "js",    ".mjs":  "js",
    ".ts":    "ts",
    ".json":  "json",  ".jsonc": "json",
    ".yaml":  "yaml",  ".yml":  "yaml",
    ".toml":  "toml",
    ".xml":   "xml",   ".svg":  "xml",
    ".html":  "html",  ".htm":  "html",
    ".css":   "css",
    ".sh":    "sh",    ".bash": "sh",    ".zsh":  "sh",
    ".c":     "c",     ".h":    "c",
    ".cpp":   "cpp",   ".cc":   "cpp",   ".hpp":  "cpp",
    ".java":  "java",
    ".rs":    "rust",
    ".go":    "go",
    ".rb":    "rb",
    ".swift": "swift",
    ".kt":    "kotlin",
    ".csv":   "",
    ".ini":   "ini",   ".cfg":  "ini",
    ".env":   "",
}

MAX_RENDER_BYTES = 1_000_000   # 1 MB per chunk read; files split across messages


def _chunk(text: str, max_len: int = 1900) -> list:
    """Split text into chunks ≤ max_len chars, preferring newline boundaries."""
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split = text.rfind("\n", 0, max_len)
        if split == -1:
            split = max_len
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    return chunks


def build_render_chunks(filename: str, text: str, truncated: bool) -> list:
    """Return a list of Discord message strings ready to send."""
    ext  = os.path.splitext(filename)[1].lower()
    lang = LANG_MAP.get(ext)          # None → plain text, str → code fence

    trunc_note = " *(truncated — file too large)*" if truncated else ""
    header     = f"📄 **{filename}**{trunc_note}"

    if lang is None:
        # .md: send as raw Discord text so markdown renders
        sep  = "\n" + "─" * 28 + "\n"
        full = header + sep + text
        return _chunk(full, 1900)
    else:
        fence  = f"```{lang}\n"
        result = [header]
        for c in _chunk(text, 1850):
            result.append(f"{fence}{c}\n```")
        return result


# ── Catbox upload ─────────────────────────────────────────────────────────────

CATBOX_API = "https://catbox.moe/user/api.php"

async def upload_to_catbox(attachment):
    """Pass the Discord CDN URL to Catbox — Catbox fetches it directly, no size limit."""
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        form = aiohttp.FormData()
        form.add_field("reqtype", "urlupload")
        form.add_field("userhash", "")
        form.add_field("url", attachment.url)
        async with session.post(CATBOX_API, data=form) as resp:
            return await resp.text()

async def upload_url_to_catbox(url: str) -> str:
    """Tell Catbox to fetch a URL directly — no file size limit on our end."""
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        form = aiohttp.FormData()
        form.add_field("reqtype", "urlupload")
        form.add_field("userhash", "")
        form.add_field("url", url)
        async with session.post(CATBOX_API, data=form) as resp:
            return await resp.text()


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    await tree.sync()
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"File Uploader Bot logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if not message.guild:
        return  # skip DMs — don't upload private files to a public host
    if not message.attachments:
        return

    guild_id = str(message.guild.id)
    if is_bot_disabled(guild_id):
        return

    # ── Auto-render text/code/markdown files ──────────────────────────────────
    for attachment in message.attachments:
        ext = os.path.splitext(attachment.filename)[1].lower()
        if ext not in LANG_MAP:
            continue
        try:
            raw      = await attachment.read()
            text     = raw[:MAX_RENDER_BYTES].decode("utf-8", errors="replace")
            truncated = len(raw) > MAX_RENDER_BYTES
        except Exception as e:
            await message.reply(f"❌ Could not read `{attachment.filename}`: {e}")
            continue

        chunks = build_render_chunks(attachment.filename, text, truncated)
        for chunk in chunks:
            await message.channel.send(chunk)

    # ── Catbox upload for files that exceed Discord's size limit ──────────────
    limit_mb  = get_file_limit(guild_id) if guild_id else None
    threshold = (limit_mb * 1024 * 1024) if limit_mb else (8 * 1024 * 1024)

    large = [a for a in message.attachments if a.size > threshold]
    if not large:
        return

    # Build a human-readable size summary for the status message
    size_parts = [f"`{a.filename}` ({a.size / 1024 / 1024:.1f} MB)" for a in large]
    size_str   = ", ".join(size_parts)

    status_msg = await message.reply(f"⏫ Uploading {size_str}...")

    result_lines = []
    for attachment in large:
        try:
            url = await upload_to_catbox(attachment)
            if url.startswith("https://"):
                result_lines.append(f"**{attachment.filename}** → {url}")
                log_file_upload(
                    guild_id,
                    message.author.id,
                    message.author.name,
                    attachment.filename,
                    attachment.size,
                    url,
                    message.channel.id,
                )
            else:
                result_lines.append(f"**{attachment.filename}** → Upload failed: {url}")
        except Exception as e:
            result_lines.append(f"**{attachment.filename}** → Error: {e}")

    if result_lines:
        header = "File too large for Discord — uploaded to Catbox:\n"
        body   = "\n".join(result_lines)
        full   = header + body

        if len(full) <= 1900:
            await status_msg.edit(content=full)
        else:
            # Chunk into multiple messages; edit the first, send the rest
            chunks  = []
            current = header
            for line in result_lines:
                if len(current) + len(line) + 1 > 1900:
                    chunks.append(current)
                    current = line + "\n"
                else:
                    current += line + "\n"
            if current:
                chunks.append(current)
            await status_msg.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)


# ── Slash commands ────────────────────────────────────────────────────────────

@tree.command(name="upload", description="Display a text file in Discord, upload a file to Catbox, or paste a URL for any file size")
@app_commands.describe(
    file="Attach a file (up to Discord's size limit)",
    url="Direct URL to a file — Catbox fetches it directly, no size limit",
)
async def upload_cmd(interaction: discord.Interaction,
                     file: discord.Attachment = None,
                     url: str = None):
    guild_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("File uploader bot is disabled.", ephemeral=True)
        return

    if file is None and url is None:
        await interaction.response.send_message(
            "Provide either a `file` attachment or a `url`.", ephemeral=True
        )
        return

    # ── URL path — Catbox fetches it directly, truly unlimited size ───────────
    if url is not None:
        await interaction.response.defer()
        try:
            result = await upload_url_to_catbox(url)
            if result.startswith("https://"):
                filename = url.split("/")[-1].split("?")[0] or "file"
                await interaction.followup.send(
                    f"✅ **{filename}** → {result}", ephemeral=False
                )
            else:
                await interaction.followup.send(f"❌ Catbox error: {result}", ephemeral=False)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=False)
        return

    ext = os.path.splitext(file.filename)[1].lower()

    # ── Text / code / markdown → render publicly in Discord ───────────────────
    if ext in LANG_MAP:
        await interaction.response.defer()
        try:
            raw       = await file.read()
            text      = raw[:MAX_RENDER_BYTES].decode("utf-8", errors="replace")
            truncated = len(raw) > MAX_RENDER_BYTES
        except Exception as e:
            await interaction.followup.send(f"❌ Could not read `{file.filename}`: {e}", ephemeral=False)
            return

        chunks = build_render_chunks(file.filename, text, truncated)
        first  = True
        for chunk in chunks:
            if first:
                await interaction.followup.send(chunk, ephemeral=False)
                first = False
            else:
                await interaction.channel.send(chunk)
        return

    # ── Everything else → upload to Catbox ────────────────────────────────────
    mb = file.size / 1024 / 1024
    await interaction.response.defer()
    try:
        url = await upload_to_catbox(file)
        if url.startswith("https://"):
            log_file_upload(guild_id, interaction.user.id, interaction.user.name,
                            file.filename, file.size, url, interaction.channel_id or 0)
            await interaction.followup.send(
                f"✅ **{file.filename}** ({mb:.1f} MB) → {url}", ephemeral=False
            )
        else:
            await interaction.followup.send(f"❌ Upload failed: {url}", ephemeral=False)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=False)


@tree.command(name="myfiles", description="Show your last 10 uploaded files")
async def myfiles(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    data = load_file_log()
    entries = data.get(guild_id, [])
    user_entries = [e for e in entries if e.get("user_id") == str(interaction.user.id)][:10]
    if not user_entries:
        await interaction.response.send_message("You haven't uploaded any files yet.", ephemeral=True)
        return
    lines = []
    for e in user_entries:
        mb  = e.get("size_bytes", 0) / 1024 / 1024
        ts  = datetime.datetime.utcfromtimestamp(e["timestamp"]).strftime("%Y-%m-%d")
        lines.append(f"• [{e['file_name']}]({e['url']}) — {mb:.1f} MB — {ts}")
    await interaction.response.send_message(
        f"**Your last {len(user_entries)} upload(s):**\n" + "\n".join(lines),
        ephemeral=True
    )


@tree.command(name="view", description="Display the contents of a text or code file")
@app_commands.describe(file="The file to view (.md, .py, .json, .txt, etc.)")
async def view_cmd(interaction: discord.Interaction, file: discord.Attachment):
    guild_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("File uploader bot is disabled.", ephemeral=True)
        return

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in LANG_MAP:
        supported = ", ".join(sorted(LANG_MAP.keys()))
        await interaction.response.send_message(
            f"❌ `{file.filename}` is not a supported text type.\nSupported: {supported}",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        raw       = await file.read()
        text      = raw[:MAX_RENDER_BYTES].decode("utf-8", errors="replace")
        truncated = len(raw) > MAX_RENDER_BYTES
    except Exception as e:
        await interaction.followup.send(f"❌ Could not read file: {e}", ephemeral=False)
        return

    chunks = build_render_chunks(file.filename, text, truncated)
    first = True
    for chunk in chunks:
        if first:
            await interaction.followup.send(chunk, ephemeral=False)
            first = False
        else:
            await interaction.channel.send(chunk)


# ── App-command error handler ─────────────────────────────────────────────────

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("file", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Rate limited. Try again shortly.", ephemeral=True)
            return
    bot_utils.log_event("file", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


# ── Run ───────────────────────────────────────────────────────────────────────

client.run(TOKEN)
