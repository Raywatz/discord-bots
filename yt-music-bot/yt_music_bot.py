# yt_music_bot.py — YouTube / YouTube Music Discord Bot
#
# Windows-compatible. Does NOT modify any existing bot files.
#
# Requirements:
#   pip install "discord.py[voice]" yt-dlp PyNaCl
#   FFmpeg must be installed and in PATH
#     Windows: https://www.gyan.dev/ffmpeg/builds/
#     Extract ffmpeg.exe + ffprobe.exe, add their folder to PATH.
#
# Setup:
#   1. https://discord.com/developers/applications → New Application → Bot
#   2. Enable: SERVER MEMBERS INTENT + VOICE under Privileged Gateway Intents
#   3. Replace YOUR_MUSIC_BOT_TOKEN_HERE below
#   4. Run — slash commands sync automatically on first start (~1 min to appear)

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import yt_dlp
import os
import sys
import json
import time
import random
import logging
import threading
import urllib.parse
from logging.handlers import RotatingFileHandler
from collections import deque
from typing import Optional

# ── Configuration ─────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("DISCORD_YT_MUSIC_BOT_TOKEN", "")
BOT_NAME  = "yt_music_bot"

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
LOG_DIR       = os.path.join(BASE_DIR, "logs")
PLAYLIST_FILE = os.path.join(BASE_DIR, "playlists.json")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
# Writes to both terminal AND logs/yt_music_bot.log simultaneously.
# Files rotate at 5 MB; 5 backups kept.

_log_path   = os.path.join(LOG_DIR, f"{BOT_NAME}.log")
_formatter  = logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_h     = RotatingFileHandler(_log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
_file_h.setFormatter(_formatter)
_stream_h   = logging.StreamHandler(sys.stdout)
_stream_h.setFormatter(_formatter)

log = logging.getLogger(BOT_NAME)
log.setLevel(logging.INFO)
log.addHandler(_file_h)
log.addHandler(_stream_h)
log.info(f"Logger initialized — writing to {_log_path}")

# ── Dashboard heartbeat / events (optional — safe to ignore if not using dashboard) ──

HEARTBEAT_FILE = os.path.join(BASE_DIR, "heartbeat.json")
EVENTS_FILE    = os.path.join(BASE_DIR, "events.json")
_hb_lock = threading.Lock()
_ev_lock = threading.Lock()


def _atomic_write(path: str, data) -> None:
    """Write JSON via a PID-unique temp file + os.replace so a reader never
    sees a truncated/corrupt file mid-write (matches shared/bot_utils.py)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _write_heartbeat() -> None:
    try:
        with _hb_lock:
            try:
                with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            data[BOT_NAME] = time.time()
            _atomic_write(HEARTBEAT_FILE, data)
    except Exception as e:
        log.warning(f"Heartbeat write failed: {e}")


def _write_event(guild_id: int, kind: str, detail: str) -> None:
    try:
        with _ev_lock:
            try:
                with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            key = str(guild_id)
            if key not in data:
                data[key] = []
            data[key].append({"bot": BOT_NAME, "kind": kind, "detail": detail, "ts": time.time()})
            data[key] = data[key][-100:]
            _atomic_write(EVENTS_FILE, data)
    except Exception as e:
        log.warning(f"Event write failed: {e}")

# ── Playlist storage ──────────────────────────────────────────────────────────
# playlists.json structure:
#   { "<guild_id>": { "<playlist_name>": [{"title": str, "url": str}, ...] } }

_pl_lock = threading.Lock()


def _load_playlists() -> dict:
    with _pl_lock:
        try:
            with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def _save_playlists(data: dict) -> None:
    with _pl_lock:
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _get_guild_playlists(guild_id: int) -> dict:
    return _load_playlists().get(str(guild_id), {})


def _set_guild_playlists(guild_id: int, guild_pls: dict) -> None:
    data = _load_playlists()
    data[str(guild_id)] = guild_pls
    _save_playlists(data)

# ── yt-dlp options ────────────────────────────────────────────────────────────

_YTDL_COMMON = {
    "format":         "bestaudio/best",
    "quiet":          True,
    "no_warnings":    True,
    "source_address": "0.0.0.0",
}

FFMPEG_OPTS: dict = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options":        "-vn",
}

ytdl = yt_dlp.YoutubeDL({**_YTDL_COMMON, "default_search": "ytsearch"})

# ── Guild music state ─────────────────────────────────────────────────────────


class GuildMusicState:
    def __init__(self) -> None:
        self.queue:        deque                       = deque()
        self.current:      Optional[dict]              = None
        self.vc:           Optional[discord.VoiceClient] = None
        self.loop:         bool                        = False
        self.volume:       float                       = 0.5
        self.text_channel: Optional[discord.TextChannel] = None
        self.vc_lock:      asyncio.Lock                = asyncio.Lock()

    def is_playing(self) -> bool:
        return self.vc is not None and self.vc.is_playing()

    def is_paused(self) -> bool:
        return self.vc is not None and self.vc.is_paused()

    def is_active(self) -> bool:
        return self.is_playing() or self.is_paused()


_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in _states:
        _states[guild_id] = GuildMusicState()
    return _states[guild_id]


async def _ensure_connected(state: GuildMusicState, channel: discord.VoiceChannel) -> None:
    """Connect to (or move to) a voice channel, guarding against two
    concurrently-invoked commands both passing the not-connected check and
    both calling connect()."""
    async with state.vc_lock:
        if state.vc is not None and state.vc.is_connected():
            if state.vc.channel.id != channel.id:
                await state.vc.move_to(channel)
        else:
            state.vc = await channel.connect()

# ── Audio fetching ────────────────────────────────────────────────────────────


def _extract_song(raw: dict) -> dict:
    return {
        "title":      raw.get("title", "Unknown"),
        "url":        raw.get("webpage_url") or raw.get("url", ""),
        "stream_url": raw.get("url", ""),
        "duration":   raw.get("duration") or 0,
        "thumbnail":  raw.get("thumbnail", ""),
        "uploader":   raw.get("uploader", "Unknown"),
    }


_ALLOWED_URL_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}


def _is_allowed_url(query: str) -> bool:
    """Reject non-YouTube URLs — yt-dlp's generic extractor will otherwise
    fetch whatever URL a user supplies, including internal/private addresses."""
    if not query.startswith("http"):
        return True  # treated as a search term, not fetched as a URL
    host = (urllib.parse.urlparse(query).hostname or "").lower()
    return host in _ALLOWED_URL_HOSTS


async def _fetch(query: str, search_prefix: str = "ytsearch1") -> Optional[dict]:
    """Fetch a single song. search_prefix controls the search engine."""
    loop = asyncio.get_event_loop()
    try:
        if not _is_allowed_url(query):
            log.warning(f"Rejected non-YouTube URL: {query}")
            return None
        if not query.startswith("http"):
            query = f"{search_prefix}:{query}"
        opts = {**_YTDL_COMMON, "default_search": search_prefix, "noplaylist": True}
        raw = await loop.run_in_executor(
            None, lambda: yt_dlp.YoutubeDL(opts).extract_info(query, download=False)
        )
        if raw is None:
            return None
        if "entries" in raw:
            entries = [e for e in raw["entries"] if e]
            if not entries:
                return None
            raw = entries[0]
            if not raw.get("url", "").startswith("http") or "youtu" in raw.get("url", ""):
                refetch = await loop.run_in_executor(
                    None,
                    lambda: yt_dlp.YoutubeDL(opts).extract_info(
                        raw.get("webpage_url") or raw.get("url", ""), download=False
                    ),
                )
                if refetch:
                    raw = refetch
        return _extract_song(raw)
    except Exception as e:
        log.error(f"_fetch failed for '{query}': {e}")
        return None


async def _fetch_playlist_url(url: str) -> tuple[str, list[dict]]:
    """Fetch all songs from a YouTube/YT Music playlist URL.
    Returns (playlist_title, [song_dicts])."""
    loop = asyncio.get_event_loop()
    opts = {**_YTDL_COMMON, "extract_flat": False, "noplaylist": False}
    try:
        if not _is_allowed_url(url):
            log.warning(f"Rejected non-YouTube playlist URL: {url}")
            return "Playlist", []
        raw = await loop.run_in_executor(
            None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False)
        )
        if raw is None or "entries" not in raw:
            single = _extract_song(raw) if raw else None
            title  = raw.get("title", "Playlist") if raw else "Playlist"
            return title, [single] if single else []
        pl_title = raw.get("title", "Playlist")
        songs    = [_extract_song(e) for e in raw["entries"] if e]
        return pl_title, songs
    except Exception as e:
        log.error(f"_fetch_playlist_url failed for '{url}': {e}")
        return "Playlist", []


async def _refresh(song: dict) -> dict:
    fresh = await _fetch(song["url"])
    return {**song, "stream_url": fresh["stream_url"]} if fresh else song


def _make_source(stream_url: str, volume: float) -> discord.PCMVolumeTransformer:
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS), volume=volume
    )

# ── Playback engine ───────────────────────────────────────────────────────────


async def play_next(guild: discord.Guild) -> None:
    state = get_state(guild.id)
    if state.vc is None or not state.vc.is_connected():
        return

    if state.loop and state.current:
        next_song = await _refresh(state.current)
    elif state.queue:
        next_song = await _refresh(state.queue.popleft())
    else:
        state.current = None
        log.info(f"[{guild.name}] Queue exhausted.")
        if state.text_channel:
            await state.text_channel.send("Queue finished. I'll auto-leave if the channel is idle.")
        return

    state.current = next_song
    log.info(f"[{guild.name}] Now playing: {next_song['title']}")

    def after_play(err: Optional[Exception]) -> None:
        if err:
            log.error(f"[{guild.name}] Playback error: {err}")
            _write_event(guild.id, "error", f"Playback error: {err}")
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    try:
        state.vc.play(_make_source(next_song["stream_url"], state.volume), after=after_play)
        if state.text_channel:
            await state.text_channel.send(embed=_np_embed(next_song, state))
    except Exception as e:
        log.error(f"[{guild.name}] Failed to start playback: {e}")
        _write_event(guild.id, "error", f"Failed to start playback: {e}")
        await play_next(guild)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fmt_dur(seconds: int) -> str:
    if not seconds:
        return "Unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _np_embed(song: dict, state: GuildMusicState) -> discord.Embed:
    embed = discord.Embed(
        title="Now Playing",
        description=f"**[{song['title']}]({song['url']})**",
        color=discord.Color.red(),
    )
    embed.add_field(name="Duration", value=_fmt_dur(song.get("duration", 0)), inline=True)
    embed.add_field(name="Uploader", value=song.get("uploader", "Unknown"),   inline=True)
    embed.add_field(name="Volume",   value=f"{int(state.volume * 100)}%",      inline=True)
    embed.add_field(name="Loop",     value="On" if state.loop else "Off",       inline=True)
    embed.add_field(name="Queue",    value=str(len(state.queue)),               inline=True)
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    return embed


async def _ensure_voice(interaction: discord.Interaction) -> bool:
    if interaction.user.voice is None:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return False
    return True


async def _queue_or_play(
    interaction: discord.Interaction,
    song: dict,
    state: GuildMusicState,
    *,
    to_top: bool = False,
) -> None:
    """Queue a single song and start playback if nothing is active."""
    song["requester"] = interaction.user
    if state.is_active():
        if to_top:
            state.queue.appendleft(song)
        else:
            state.queue.append(song)
        pos = 1 if to_top else len(state.queue)
        await interaction.followup.send(
            f"Added to queue: **{song['title']}** ({_fmt_dur(song.get('duration', 0))}) — #{pos}"
        )
    else:
        state.queue.appendleft(song)
        await play_next(interaction.guild)
        if state.current:
            await interaction.followup.send(embed=_np_embed(state.current, state))
        else:
            await interaction.followup.send("Could not start playback.")

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!music ", intents=intents)

# ── Playback commands ─────────────────────────────────────────────────────────


@bot.tree.command(name="play", description="Play a YouTube video or search term (audio only)")
@app_commands.describe(query="YouTube URL or search term")
async def cmd_play(interaction: discord.Interaction, query: str) -> None:
    if not await _ensure_voice(interaction):
        return
    await interaction.response.defer()
    state = get_state(interaction.guild.id)
    state.text_channel = interaction.channel
    await _ensure_connected(state, interaction.user.voice.channel)

    if "list=" in query and query.startswith("http"):
        await interaction.followup.send("Loading playlist…")
        _, songs = await _fetch_playlist_url(query)
        if not songs:
            await interaction.followup.send("Could not load that playlist.")
            return
        for s in songs:
            s["requester"] = interaction.user
            state.queue.append(s)
        if not state.is_active():
            await play_next(interaction.guild)
        await interaction.followup.send(f"Queued **{len(songs)}** tracks.")
        return

    song = await _fetch(query, search_prefix="ytsearch1")
    if not song:
        await interaction.followup.send("Could not find that song.")
        return
    await _queue_or_play(interaction, song, state)


@bot.tree.command(name="yt", description="Search and play from YouTube (regular videos — audio only)")
@app_commands.describe(query="YouTube URL or search term")
async def cmd_yt(interaction: discord.Interaction, query: str) -> None:
    if not await _ensure_voice(interaction):
        return
    await interaction.response.defer()
    state = get_state(interaction.guild.id)
    state.text_channel = interaction.channel
    await _ensure_connected(state, interaction.user.voice.channel)

    song = await _fetch(query, search_prefix="ytsearch1")
    if not song:
        await interaction.followup.send("Could not find that video on YouTube.")
        return
    log.info(f"[{interaction.guild.name}] /yt: {song['title']}")
    await _queue_or_play(interaction, song, state)


@bot.tree.command(name="ytm", description="Search and play from YouTube Music")
@app_commands.describe(query="YouTube Music URL or search term")
async def cmd_ytm(interaction: discord.Interaction, query: str) -> None:
    if not await _ensure_voice(interaction):
        return
    await interaction.response.defer()
    state = get_state(interaction.guild.id)
    state.text_channel = interaction.channel
    await _ensure_connected(state, interaction.user.voice.channel)

    song = await _fetch(query, search_prefix="ytmsearch1")
    if not song:
        await interaction.followup.send("Could not find that song on YouTube Music.")
        return
    log.info(f"[{interaction.guild.name}] /ytm: {song['title']}")
    await _queue_or_play(interaction, song, state)


@bot.tree.command(name="playtop", description="Add a song to the top of the queue (plays next)")
@app_commands.describe(query="YouTube URL or search term")
async def cmd_playtop(interaction: discord.Interaction, query: str) -> None:
    if not await _ensure_voice(interaction):
        return
    await interaction.response.defer()
    state = get_state(interaction.guild.id)
    state.text_channel = interaction.channel
    await _ensure_connected(state, interaction.user.voice.channel)
    song = await _fetch(query)
    if not song:
        await interaction.followup.send("Could not find that song.")
        return
    await _queue_or_play(interaction, song, state, to_top=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def cmd_skip(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if not state.is_active():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    title = state.current.get("title", "?") if state.current else "?"
    state.vc.stop()
    log.info(f"[{interaction.guild.name}] Skipped: {title}")
    await interaction.response.send_message(f"Skipped **{title}**.")


@bot.tree.command(name="pause", description="Pause playback")
async def cmd_pause(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if state.is_playing():
        state.vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume paused playback")
async def cmd_resume(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if state.is_paused():
        state.vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def cmd_stop(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if state.vc is None or not state.vc.is_connected():
        await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
        return
    state.queue.clear()
    state.loop = False
    state.current = None
    state.vc.stop()
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="nowplaying", description="Show the currently playing song")
async def cmd_nowplaying(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if not state.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_np_embed(state.current, state))


@bot.tree.command(name="queue", description="Show the music queue")
async def cmd_queue(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if not state.current and not state.queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines: list[str] = []
    if state.current:
        lines.append(f"**Now Playing:** [{state.current['title']}]({state.current['url']}) ({_fmt_dur(state.current.get('duration', 0))})")
    for i, s in enumerate(list(state.queue)[:20], 1):
        lines.append(f"`{i}.` [{s['title']}]({s['url']}) ({_fmt_dur(s.get('duration', 0))})")
    if len(state.queue) > 20:
        lines.append(f"*…and {len(state.queue) - 20} more*")
    embed = discord.Embed(
        title=f"Queue — {len(state.queue)} remaining",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="volume", description="Set playback volume (0–100)")
@app_commands.describe(amount="0 to 100")
async def cmd_volume(interaction: discord.Interaction, amount: int) -> None:
    if not 0 <= amount <= 100:
        await interaction.response.send_message("Volume must be 0–100.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    state.volume = amount / 100.0
    if state.vc and state.vc.source:
        state.vc.source.volume = state.volume
    await interaction.response.send_message(f"Volume set to **{amount}%**.")


@bot.tree.command(name="loop", description="Toggle loop mode for the current song")
async def cmd_loop(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    state.loop = not state.loop
    await interaction.response.send_message(f"Loop **{'enabled' if state.loop else 'disabled'}**.")


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def cmd_shuffle(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if not state.queue:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return
    q = list(state.queue)
    random.shuffle(q)
    state.queue = deque(q)
    await interaction.response.send_message(f"Shuffled **{len(q)}** songs.")


@bot.tree.command(name="clear", description="Clear the queue (current song keeps playing)")
async def cmd_clear(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    count = len(state.queue)
    state.queue.clear()
    await interaction.response.send_message(f"Cleared **{count}** song(s) from the queue.")


@bot.tree.command(name="remove", description="Remove a song from the queue by position")
@app_commands.describe(position="1-based position in the queue")
async def cmd_remove(interaction: discord.Interaction, position: int) -> None:
    state = get_state(interaction.guild.id)
    if not state.queue or not (1 <= position <= len(state.queue)):
        await interaction.response.send_message(f"Position must be 1–{len(state.queue)}.", ephemeral=True)
        return
    q = list(state.queue)
    removed = q.pop(position - 1)
    state.queue = deque(q)
    await interaction.response.send_message(f"Removed **{removed['title']}**.")


@bot.tree.command(name="move", description="Move a song to a different queue position")
@app_commands.describe(from_pos="Current position", to_pos="Target position")
async def cmd_move(interaction: discord.Interaction, from_pos: int, to_pos: int) -> None:
    state = get_state(interaction.guild.id)
    n = len(state.queue)
    if n == 0 or not (1 <= from_pos <= n) or not (1 <= to_pos <= n):
        await interaction.response.send_message(f"Positions must be 1–{n}.", ephemeral=True)
        return
    q = list(state.queue)
    song = q.pop(from_pos - 1)
    q.insert(to_pos - 1, song)
    state.queue = deque(q)
    await interaction.response.send_message(f"Moved **{song['title']}** to #{to_pos}.")


@bot.tree.command(name="replay", description="Restart the current song from the beginning")
async def cmd_replay(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if not state.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    state.queue.appendleft(state.current)
    state.vc.stop()
    await interaction.response.send_message(f"Restarting **{state.current['title']}**.")


@bot.tree.command(name="join", description="Join your current voice channel")
async def cmd_join(interaction: discord.Interaction) -> None:
    if not await _ensure_voice(interaction):
        return
    ch    = interaction.user.voice.channel
    state = get_state(interaction.guild.id)
    await _ensure_connected(state, ch)
    state.text_channel = interaction.channel
    await interaction.response.send_message(f"Joined **{ch.name}**.")


@bot.tree.command(name="leave", description="Leave the voice channel and clear the queue")
async def cmd_leave(interaction: discord.Interaction) -> None:
    state = get_state(interaction.guild.id)
    if state.vc and state.vc.is_connected():
        state.queue.clear()
        state.current = None
        await state.vc.disconnect()
        state.vc = None
        await interaction.response.send_message("Left and cleared the queue.")
    else:
        await interaction.response.send_message("Not in a voice channel.", ephemeral=True)

# ── Playlist commands ─────────────────────────────────────────────────────────


@bot.tree.command(
    name="playlist",
    description="Play a saved playlist by name, OR pass a YT/YT Music playlist URL to import + play it",
)
@app_commands.describe(name_or_url="Saved playlist name OR a YouTube Music playlist URL")
async def cmd_playlist(interaction: discord.Interaction, name_or_url: str) -> None:
    if not await _ensure_voice(interaction):
        return
    await interaction.response.defer()

    state = get_state(interaction.guild.id)
    state.text_channel = interaction.channel
    await _ensure_connected(state, interaction.user.voice.channel)

    guild_pls = _get_guild_playlists(interaction.guild.id)

    if name_or_url.startswith("http"):
        # Import from URL
        await interaction.followup.send("Fetching playlist from URL, please wait…")
        pl_title, songs = await _fetch_playlist_url(name_or_url)
        if not songs:
            await interaction.followup.send("Could not load that playlist URL.")
            return

        # Save locally
        save_name = pl_title[:50]
        guild_pls[save_name] = [{"title": s["title"], "url": s["url"]} for s in songs]
        _set_guild_playlists(interaction.guild.id, guild_pls)

        for s in songs:
            s["requester"] = interaction.user
            state.queue.append(s)
        if not state.is_active():
            await play_next(interaction.guild)
        log.info(f"[{interaction.guild.name}] Imported playlist '{save_name}' ({len(songs)} tracks)")
        await interaction.followup.send(
            f"Imported and queued **{len(songs)}** tracks from **{save_name}**."
        )
        return

    # Play by name
    if name_or_url not in guild_pls:
        names = ", ".join(f"**{n}**" for n in guild_pls) or "*(none yet)*"
        await interaction.followup.send(
            f"No playlist named **{name_or_url}**. Available: {names}"
        )
        return

    songs = guild_pls[name_or_url]
    if not songs:
        await interaction.followup.send(f"Playlist **{name_or_url}** is empty.")
        return

    await interaction.followup.send(f"Queuing **{len(songs)}** tracks from **{name_or_url}**…")
    for entry in songs:
        # Resolve stream URL lazily — just store the webpage URL, play_next refreshes it
        state.queue.append({
            "title":      entry["title"],
            "url":        entry["url"],
            "stream_url": "",
            "duration":   0,
            "thumbnail":  "",
            "uploader":   "Playlist",
            "requester":  interaction.user,
        })
    if not state.is_active():
        await play_next(interaction.guild)
    log.info(f"[{interaction.guild.name}] Playing playlist '{name_or_url}' ({len(songs)} tracks)")
    await interaction.followup.send(f"Playing **{name_or_url}**.")


@bot.tree.command(name="createplaylist", description="Create a new empty playlist")
@app_commands.describe(name="Name for your new playlist")
async def cmd_createplaylist(interaction: discord.Interaction, name: str) -> None:
    guild_pls = _get_guild_playlists(interaction.guild.id)
    if name in guild_pls:
        await interaction.response.send_message(
            f"A playlist named **{name}** already exists.", ephemeral=True
        )
        return
    guild_pls[name] = []
    _set_guild_playlists(interaction.guild.id, guild_pls)
    log.info(f"[{interaction.guild.name}] Created playlist '{name}'")
    await interaction.response.send_message(
        f"Created playlist **{name}**. Use `/add {name} <song>` to add songs."
    )


@bot.tree.command(name="add", description="Add a song to a saved playlist")
@app_commands.describe(playlist="Playlist name", song="Song name or YouTube URL")
async def cmd_add(interaction: discord.Interaction, playlist: str, song: str) -> None:
    guild_pls = _get_guild_playlists(interaction.guild.id)
    if playlist not in guild_pls:
        await interaction.response.send_message(
            f"No playlist named **{playlist}**. Create it first with `/createplaylist {playlist}`.",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    info = await _fetch(song)
    if not info:
        await interaction.followup.send("Could not find that song.")
        return
    guild_pls[playlist].append({"title": info["title"], "url": info["url"]})
    _set_guild_playlists(interaction.guild.id, guild_pls)
    log.info(f"[{interaction.guild.name}] Added '{info['title']}' to playlist '{playlist}'")
    await interaction.followup.send(
        f"Added **{info['title']}** to **{playlist}** (now {len(guild_pls[playlist])} songs)."
    )


@bot.tree.command(name="listplaylists", description="Show all saved playlists for this server")
async def cmd_listplaylists(interaction: discord.Interaction) -> None:
    guild_pls = _get_guild_playlists(interaction.guild.id)
    if not guild_pls:
        await interaction.response.send_message("No playlists saved yet.", ephemeral=True)
        return
    lines = [f"**{name}** — {len(songs)} song(s)" for name, songs in guild_pls.items()]
    embed = discord.Embed(
        title="Saved Playlists",
        description="\n".join(lines),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)

# ── Admin commands ────────────────────────────────────────────────────────────


@bot.tree.command(name="logs", description="Show recent bot log entries (admin only)")
@app_commands.describe(lines="Lines to show (default 20, max 50)")
async def cmd_logs(interaction: discord.Interaction, lines: int = 20) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    lines = max(1, min(lines, 50))
    try:
        with open(_log_path, "r", encoding="utf-8") as f:
            tail = "".join(f.readlines()[-lines:]).strip()
        if not tail:
            await interaction.response.send_message("Log file is empty.", ephemeral=True)
            return
        if len(tail) > 1900:
            tail = "…" + tail[-1900:]
        await interaction.response.send_message(f"```\n{tail}\n```", ephemeral=True)
    except FileNotFoundError:
        await interaction.response.send_message("Log file not found yet.", ephemeral=True)


@bot.tree.command(name="musichelp", description="Show all music bot commands")
async def cmd_musichelp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="Music Bot — Commands", color=discord.Color.red())
    sections = [
        ("Playback",
         [("/play <query>",        "Play a YouTube URL or search term"),
          ("/yt <query>",          "Search YouTube (regular videos — audio only)"),
          ("/ytm <query>",         "Search YouTube Music"),
          ("/playtop <query>",     "Add to top of queue (plays next)"),
          ("/skip",                "Skip current song"),
          ("/pause / /resume",     "Pause or resume"),
          ("/stop",                "Stop and clear queue"),
          ("/replay",              "Restart current song"),
          ("/nowplaying",          "Show current song"),
          ("/queue",               "Show queue (up to 20)"),
          ("/volume <0–100>",      "Set volume"),
          ("/loop",                "Toggle loop"),
          ("/shuffle",             "Shuffle queue"),
          ("/clear",               "Clear queue"),
          ("/remove <pos>",        "Remove song at position"),
          ("/move <from> <to>",    "Move song in queue"),
          ("/join / /leave",       "Join or leave voice channel")]),
        ("Playlists",
         [("/playlist <name|url>", "Play a saved playlist, or import + play a YT playlist URL"),
          ("/createplaylist <n>",  "Create a new empty playlist"),
          ("/add <playlist> <song>","Add a song to a playlist"),
          ("/listplaylists",       "Show all saved playlists")]),
        ("Admin",
         [("/logs [lines]",        "Show recent log entries (admin only)")]),
    ]
    for section, cmds in sections:
        embed.add_field(
            name=f"— {section} —",
            value="\n".join(f"`{n}` {d}" for n, d in cmds),
            inline=False,
        )
    await interaction.response.send_message(embed=embed)

# ── Bot events ────────────────────────────────────────────────────────────────


@bot.event
async def on_ready() -> None:
    log.info(f"Logged in as {bot.user} (ID {bot.user.id})")
    await bot.tree.sync()
    log.info("Slash commands synced.")
    if not _heartbeat.is_running():
        _heartbeat.start()


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    if member.bot:
        return
    state = get_state(member.guild.id)
    if state.vc is None or not state.vc.is_connected():
        return
    non_bots = [m for m in state.vc.channel.members if not m.bot]
    if not non_bots:
        await asyncio.sleep(30)
        # Re-check after the sleep — another overlapping voice-state event may
        # have already disconnected (or reconnected) this guild's voice client.
        if state.vc is None or not state.vc.is_connected():
            return
        non_bots = [m for m in state.vc.channel.members if not m.bot]
        if not non_bots:
            log.info(f"[{member.guild.name}] Auto-leaving empty channel.")
            state.queue.clear()
            state.current = None
            await state.vc.disconnect()
            state.vc = None


@bot.event
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    msg      = str(error)
    cmd_name = interaction.command.name if interaction.command else "unknown"
    log.error(f"[{interaction.guild.name}] /{cmd_name}: {msg}")
    _write_event(interaction.guild.id, "error", f"/{cmd_name}: {msg}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"Error: {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Error: {msg}", ephemeral=True)
    except Exception:
        pass

# ── Heartbeat ─────────────────────────────────────────────────────────────────


@tasks.loop(seconds=30)
async def _heartbeat() -> None:
    _write_heartbeat()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_MUSIC_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN to your Discord bot token before running.")
        sys.exit(1)
    log.info("Starting YT Music Bot…")
    bot.run(BOT_TOKEN, log_handler=None)
