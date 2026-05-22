# Discord Bots

A collection of Discord bots organized by function.

## Structure

| Directory | Bot | Description |
|-----------|-----|-------------|
| `hub-bot/` | Hub Bot | Central control panel — server linking, bot enable/disable/pause, settings dashboard |
| `counting-bot/` | Counting Bot | Multi-mode counting game (sequential, powers of 2, Fibonacci) |
| `vibe-bot/` | Vibe Bot | Economy system, welcome messages, birthday tracking |
| `games-bot/` | Games Bot | Mini-games (hangman, etc.) with economy integration |
| `inbox-bot/` | Inbox Bot | Ticket/inbox system for user support |
| `moderation-bot/` | Moderation Bot | Word filtering, rate limiting, automated moderation |
| `python-bot/` | Python Bot | Execute Python snippets in Discord with sandbox support |
| `file-uploader-bot/` | File Uploader Bot | Managed file uploads with size limits and logging |
| `yt-music-bot/` | YT Music Bot | YouTube/YouTube Music playback in voice channels |
| `yt-discord-rpc/` | YT Discord RPC | Rich Presence integration for YouTube Music (Node.js) |
| `shared/` | Shared Utilities | `bot_utils.py` (heartbeats, event logging), `watchdog.py` (process monitor) |

## Setup

### Python bots

```bash
pip install "discord.py[voice]" yt-dlp PyNaCl aiohttp
```

Copy `.env.example` to `.env` and fill in your bot tokens:

```bash
cp .env.example .env
```

Each bot reads its token from the environment:

```bash
source .env
python hub-bot/hub_bot.py
```

Or use the watchdog to run all bots together:

```bash
python shared/watchdog.py
```

### YT Discord RPC (Node.js)

```bash
cd yt-discord-rpc
npm install
node index.js
```

## Shared utilities

`shared/bot_utils.py` provides heartbeat tracking and event logging used by all Python bots. Copy or symlink it into the same directory as the bot you're running, or add `shared/` to your `PYTHONPATH`.

`shared/watchdog.py` monitors and auto-restarts all Python bots. Run it instead of launching bots individually.
