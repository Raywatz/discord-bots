import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
import random
import datetime
import bot_utils

TOKEN        = os.environ.get("DISCORD_GAMES_BOT_TOKEN", "")
GAMES_FILE   = "games_data.json"
DISABLE_FILE = "disable_data.json"
HUB_FILE     = "hub_data.json"
ECONOMY_FILE = "economy_data.json"
LINK_FILE    = "link_data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("games")

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
        bot_utils.log_event("games", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("games", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("games", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Bot is being rate limited. Try again in a moment.", ephemeral=True)
            return
    bot_utils.log_event("games", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


GAME_COST = 20  # default; overridden per-guild via hub settings

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

games_data = load_json(GAMES_FILE)
_launch_in_progress = set()  # (guild_id, game) pairs currently in their wait→launch sequence

def get_guild_data(guild_id):
    if guild_id not in games_data:
        games_data[guild_id] = {
            "categories": {},
            "results_channel": None,
            "mod_role_id": None,
            "waitlists": {}
        }
    return games_data[guild_id]

def is_bot_disabled(guild_id):
    return load_json(DISABLE_FILE).get(str(guild_id), {}).get("games", False)

def get_balance(guild_id, user_id):
    return load_json(ECONOMY_FILE).get(str(guild_id), {}).get(str(user_id), {}).get("balance", 0)

def deduct_balance(guild_id, user_id, amount):
    eco = load_json(ECONOMY_FILE)
    if guild_id not in eco:
        eco[guild_id] = {}
    uid = str(user_id)
    if uid not in eco[guild_id]:
        eco[guild_id][uid] = {"balance": 0}
    eco[guild_id][uid]["balance"] = max(0, eco[guild_id][uid]["balance"] - amount)
    save_json(ECONOMY_FILE, eco)

def add_balance(guild_id, user_id, amount, name=""):
    eco = load_json(ECONOMY_FILE)
    if guild_id not in eco:
        eco[guild_id] = {}
    uid = str(user_id)
    if uid not in eco[guild_id]:
        eco[guild_id][uid] = {"balance": 0, "name": name}
    eco[guild_id][uid]["balance"] = max(0, eco[guild_id][uid]["balance"] + amount)
    save_json(ECONOMY_FILE, eco)

def is_mod(member):
    if member.guild_permissions.administrator:
        return True
    mod_role_id = get_guild_data(str(member.guild.id)).get("mod_role_id")
    if mod_role_id:
        role = discord.utils.get(member.guild.roles, id=int(mod_role_id))
        if role and role in member.roles:
            return True
    return False

def get_game_cost(guild_id):
    return load_json(HUB_FILE).get(guild_id, {}).get("game_cost", GAME_COST)

def get_canonical_guild(guild_id):
    """Return the shared economy key — same logic as vibe_bot."""
    links = load_json(LINK_FILE).get("links", {})
    partner = links.get(str(guild_id))
    if partner:
        return min(str(guild_id), str(partner))
    return str(guild_id)

def get_join_wait(guild_id):
    return load_json(HUB_FILE).get(guild_id, {}).get("game_join_wait", 10)

def get_max_hangman_players(guild_id):
    return load_json(HUB_FILE).get(guild_id, {}).get("hangman_max_players", 5)

def get_hangman_word_limits(guild_id):
    h = load_json(HUB_FILE).get(guild_id, {})
    return h.get("hangman_min_letters", 4), h.get("hangman_max_letters", 8)

GAME_LOG_FILE = "game_log.json"

def log_game_result(guild_id, game, winner_id, winner_name, loser_id=None, loser_name=None):
    import time as _t
    path = GAME_LOG_FILE
    if not os.path.exists(path):
        data = {}
    else:
        with open(path, "r") as f:
            try: data = json.load(f)
            except Exception: data = {}
    gid = str(guild_id)
    if gid not in data:
        data[gid] = []
    data[gid].insert(0, {
        "game":        game,
        "winner_id":   str(winner_id) if winner_id else None,
        "winner_name": winner_name,
        "loser_id":    str(loser_id) if loser_id else None,
        "loser_name":  loser_name,
        "timestamp":   _t.time()
    })
    data[gid] = data[gid][:500]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# Active games state
active_games = {}  # channel_id -> game state


# ── /setup ────────────────────────────────────────────────────────────────────
# Setup is split into 2 steps because Discord limits views to 5 items

setup_state = {}  # user_id -> partial setup data

def make_select(placeholder, options, callback):
    s = discord.ui.Select(placeholder=placeholder, options=options)
    s.callback = callback
    return s

class GamesSetupView1(discord.ui.View):
    """Step 1: mod role, results channel, dice, ttt — 4 selects + next button"""
    def __init__(self, guild, user_id):
        super().__init__(timeout=180)
        self.guild   = guild
        self.user_id = user_id
        cat_opts  = [discord.SelectOption(label=c.name, value=str(c.id)) for c in guild.categories[:25]]
        ch_opts   = [discord.SelectOption(label=c.name, value=str(c.id)) for c in guild.text_channels[:25]]
        role_opts = [discord.SelectOption(label=r.name, value=str(r.id)) for r in guild.roles if not r.is_default() and not r.managed][:25]

        async def role_cb(i):
            setup_state.setdefault(self.user_id, {})["mod_role"] = int(i.data["values"][0])
            await i.response.defer()
        async def res_cb(i):
            setup_state.setdefault(self.user_id, {})["results_ch"] = int(i.data["values"][0])
            await i.response.defer()
        async def dice_cb(i):
            setup_state.setdefault(self.user_id, {})["dice"] = int(i.data["values"][0])
            await i.response.defer()
        async def ttt_cb(i):
            setup_state.setdefault(self.user_id, {})["ttt"] = int(i.data["values"][0])
            await i.response.defer()

        for sel in [
            make_select("Mod role", role_opts, role_cb),
            make_select("Results channel", ch_opts, res_cb),
            make_select("Dice Roll category", cat_opts, dice_cb),
            make_select("Tic Tac Toe category", cat_opts, ttt_cb),
        ]:
            self.add_item(sel)

        next_btn = discord.ui.Button(label="Next →", style=discord.ButtonStyle.primary, row=4)
        async def next_cb(i):
            state = setup_state.get(self.user_id, {})
            required = ["mod_role", "results_ch", "dice", "ttt"]
            missing = [k for k in required if k not in state]
            if missing:
                await i.response.send_message(f"Please select all options before continuing. Missing: {', '.join(missing)}", ephemeral=True)
                return
            view2 = GamesSetupView2(self.guild, self.user_id)
            await i.response.edit_message(content="**Step 2 of 3:** Connect 4, Chess, Higher or Lower:", view=view2)
        next_btn.callback = next_cb
        self.add_item(next_btn)


class GamesSetupView2(discord.ui.View):
    """Step 2: c4, chess, hol"""
    def __init__(self, guild, user_id):
        super().__init__(timeout=180)
        self.guild   = guild
        self.user_id = user_id
        cat_opts = [discord.SelectOption(label=c.name, value=str(c.id)) for c in guild.categories[:25]]

        async def c4_cb(i):
            setup_state.setdefault(self.user_id, {})["c4"] = int(i.data["values"][0])
            await i.response.defer()
        async def chess_cb(i):
            setup_state.setdefault(self.user_id, {})["chess"] = int(i.data["values"][0])
            await i.response.defer()
        async def hol_cb(i):
            setup_state.setdefault(self.user_id, {})["hol"] = int(i.data["values"][0])
            await i.response.defer()

        for sel in [
            make_select("Connect 4 category", cat_opts, c4_cb),
            make_select("Chess category", cat_opts, chess_cb),
            make_select("Higher or Lower category", cat_opts, hol_cb),
        ]:
            self.add_item(sel)

        next_btn = discord.ui.Button(label="Next →", style=discord.ButtonStyle.primary, row=4)
        async def next_cb(i):
            state = setup_state.get(self.user_id, {})
            required = ["c4", "chess", "hol"]
            missing = [k for k in required if k not in state]
            if missing:
                await i.response.send_message(f"Please select all options. Missing: {', '.join(missing)}", ephemeral=True)
                return
            view3 = GamesSetupView3(self.guild, self.user_id)
            await i.response.edit_message(content="**Step 3 of 3:** Confusion and Hangman categories:", view=view3)
        next_btn.callback = next_cb
        self.add_item(next_btn)


class GamesSetupView3(discord.ui.View):
    """Step 3: confusion, hangman"""
    def __init__(self, guild, user_id):
        super().__init__(timeout=180)
        self.guild   = guild
        self.user_id = user_id
        cat_opts = [discord.SelectOption(label=c.name, value=str(c.id)) for c in guild.categories[:25]]

        async def confusion_cb(i):
            setup_state.setdefault(self.user_id, {})["confusion"] = int(i.data["values"][0])
            await i.response.defer()
        async def hangman_cb(i):
            setup_state.setdefault(self.user_id, {})["hangman"] = int(i.data["values"][0])
            await i.response.defer()

        for sel in [
            make_select("Confusion category", cat_opts, confusion_cb),
            make_select("Hangman category", cat_opts, hangman_cb),
        ]:
            self.add_item(sel)

        finish_btn = discord.ui.Button(label="Finish Setup ✓", style=discord.ButtonStyle.green, row=4)
        async def finish_cb(i):
            state    = setup_state.get(self.user_id, {})
            guild_id = str(i.guild_id)
            data     = get_guild_data(guild_id)
            data["mod_role_id"]     = state.get("mod_role")
            data["results_channel"] = state.get("results_ch")
            data["categories"] = {
                "dice":      state.get("dice"),
                "ttt":       state.get("ttt"),
                "c4":        state.get("c4"),
                "chess":     state.get("chess"),
                "hol":       state.get("hol"),
                "confusion": state.get("confusion"),
                "hangman":   state.get("hangman"),
            }
            save_json(GAMES_FILE, games_data)
            setup_state.pop(self.user_id, None)
            await i.response.edit_message(content="✅ Games bot setup complete!", view=None)
        finish_btn.callback = finish_cb
        self.add_item(finish_btn)


@tree.command(name="setup", description="Set up the games bot")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    setup_state[interaction.user.id] = {}
    view = GamesSetupView1(interaction.guild, interaction.user.id)
    await interaction.response.send_message("**Step 1 of 3:** Select mod role, results channel, and first 4 game categories:", view=view, ephemeral=True)


async def launch_game(game_name, guild, guild_id, players, data, canonical, game_cost, fallback_channel=None):
    """Remove launched players from waitlist, deduct balances, create channel, and start the game."""
    # Re-validate balances now — a player's balance may have dropped (e.g. spent
    # elsewhere, or joined multiple waitlists) since they were checked at join time.
    # deduct_balance() clamps at 0 instead of failing, so without this check a
    # player could dodge part or all of the entry fee.
    cant_afford = []
    for p in players:
        member = guild.get_member(p["id"])
        if member and not is_mod(member) and get_balance(canonical, str(p["id"])) < game_cost:
            cant_afford.append(p)
    if cant_afford:
        names = ", ".join(p.get("name", str(p["id"])) for p in cant_afford)
        if fallback_channel:
            try:
                await fallback_channel.send(
                    f"❌ Game cancelled: {names} no longer has enough coins for the **${game_cost}** entry fee.",
                    delete_after=15
                )
            except Exception:
                pass
        return

    # Only remove the players actually being launched — any remaining
    # waitlist entries (joined after the cutoff) must stay queued for the
    # next launch, not be discarded.
    if game_name in data["waitlists"]:
        data["waitlists"][game_name] = data["waitlists"][game_name][len(players):]
    save_json(GAMES_FILE, games_data)

    for p in players:
        member = guild.get_member(p["id"])
        if member and not is_mod(member):
            deduct_balance(canonical, str(p["id"]), game_cost)

    info   = GAME_INFO[game_name]
    cat_id = data.get("categories", {}).get(game_name)
    cat    = guild.get_channel(int(cat_id)) if cat_id else None

    player_members = [guild.get_member(p["id"]) for p in players if guild.get_member(p["id"])]

    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
    for m in player_members:
        overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        ch = await guild.create_text_channel(
            f"{info['name'].lower().replace(' ', '-')}-{random.randint(1000,9999)}",
            category=cat,
            overwrites=overwrites
        )
    except Exception as e:
        for p in players:
            member = guild.get_member(p["id"])
            if member and not is_mod(member):
                add_balance(canonical, str(p["id"]), game_cost)
        if fallback_channel:
            try:
                await fallback_channel.send(
                    f"❌ Failed to create game channel: {e}\nAll entry fees have been refunded.",
                    delete_after=15
                )
            except Exception:
                pass
        return

    await ch.send(f"**{info['name']}** — Players: {', '.join(m.mention for m in player_members)}\n\nGame starting now!")
    asyncio.create_task(run_game(game_name, ch, player_members, guild_id, data))


# ── /game ─────────────────────────────────────────────────────────────────────
GAME_INFO = {
    "dice":      {"name": "Dice Roll",      "min": 3, "max": 10},
    "ttt":       {"name": "Tic Tac Toe",    "min": 2, "max": 2},
    "c4":        {"name": "Connect 4",      "min": 2, "max": 2},
    "chess":     {"name": "Chess",          "min": 2, "max": 2},
    "hol":       {"name": "Higher or Lower","min": 2, "max": 2},
    "confusion": {"name": "Confusion",      "min": 4, "max": 6},
    "hangman":   {"name": "Hangman",        "min": 2, "max": 5},
}

@tree.command(name="game", description="Join the waitlist for a game")
@app_commands.describe(game="Game name: dice, ttt, c4, chess, hol, confusion, hangman")
async def game(interaction: discord.Interaction, game: str):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return

    game = game.lower()
    if game not in GAME_INFO:
        await interaction.response.send_message(
            f"Unknown game. Options: {', '.join(GAME_INFO.keys())}", ephemeral=True
        )
        return

    # Check balance
    game_cost = get_game_cost(guild_id)
    canonical = get_canonical_guild(guild_id)
    bal = get_balance(canonical, str(interaction.user.id))
    if not is_mod(interaction.user) and bal < game_cost:
        await interaction.response.send_message(
            f"You need **${game_cost}** to play. You have **${bal}**.", ephemeral=True
        )
        return

    data = get_guild_data(guild_id)
    if "waitlists" not in data:
        data["waitlists"] = {}
    if game not in data["waitlists"]:
        data["waitlists"][game] = []

    # Check not already in waitlist
    if any(p["id"] == interaction.user.id for p in data["waitlists"][game]):
        await interaction.response.send_message("You're already in the waitlist for this game.", ephemeral=True)
        return

    data["waitlists"][game].append({"id": interaction.user.id, "name": interaction.user.name})
    save_json(GAMES_FILE, games_data)

    info    = GAME_INFO[game]
    current = len(data["waitlists"][game])
    await interaction.response.send_message(
        f"Added to **{info['name']}** waitlist. Players: {current}/{info['min']} minimum.",
        ephemeral=True
    )

    # If minimum reached, wait then start (guarded so a re-trigger during the
    # wait window can't launch the same waitlist twice concurrently)
    launch_key = (guild_id, game)
    if current == info["min"] and launch_key not in _launch_in_progress:
        _launch_in_progress.add(launch_key)
        try:
            wait = get_join_wait(guild_id) if info["min"] != info["max"] else 0
            if wait > 0:
                try:
                    await interaction.channel.send(
                        f"**{info['name']}** has enough players! Starting in **{wait}** seconds — type `/game {game}` to join!"
                    )
                except Exception:
                    pass
                await asyncio.sleep(wait)

            # Players may have left via /cancelwait during the wait above — re-check
            # the minimum still holds before launching, otherwise games can start
            # short-handed (e.g. a 1-player Tic Tac Toe) and crash after fees are charged.
            current_wl = data["waitlists"].get(game, [])
            if len(current_wl) < info["min"]:
                try:
                    await interaction.channel.send(
                        f"Not enough players remain for **{info['name']}** — waiting for more to join."
                    )
                except Exception:
                    pass
                return

            max_players = get_max_hangman_players(guild_id) if game == "hangman" else info["max"]
            players = current_wl[:max_players]
            await launch_game(game, interaction.guild, guild_id, players, data, canonical, game_cost, interaction.channel)
        finally:
            _launch_in_progress.discard(launch_key)


async def post_result(guild_id, guild, result_text):
    data    = get_guild_data(guild_id)
    res_ch  = data.get("results_channel")
    if res_ch:
        ch = guild.get_channel(int(res_ch))
        if ch:
            await ch.send(result_text)


async def run_game(game, channel, players, guild_id, data):
    try:
        if game == "dice":
            await run_dice(channel, players, guild_id)
        elif game == "ttt":
            await run_ttt(channel, players, guild_id)
        elif game == "c4":
            await run_c4(channel, players, guild_id)
        elif game == "hol":
            await run_hol(channel, players, guild_id)
        elif game == "confusion":
            await run_confusion(channel, players, guild_id)
        elif game == "hangman":
            await run_hangman(channel, players, guild_id)
        elif game == "chess":
            await run_chess(channel, players, guild_id)
    except Exception as e:
        await channel.send(f"Game error: {e}")
    finally:
        # Clean up any stale active_games entry (games that timed out without self-cleaning)
        active_games.pop(str(channel.id), None)
        await asyncio.sleep(5)
        try:
            await channel.delete()
        except Exception:
            pass




# ── CHESS ─────────────────────────────────────────────────────────────────────
# Pieces: uppercase = white, lowercase = black
# K=king Q=queen R=rook B=bishop N=knight P=pawn

CHESS_EMOJI = {
    'K': '♔', 'Q': '♕', 'R': '♖', 'B': '♗', 'N': '♘', 'P': '♙',
    'k': '♚', 'q': '♛', 'r': '♜', 'b': '♝', 'n': '♞', 'p': '♟',
    '.': '⬜', ',': '⬛'  # light and dark empty squares
}

def make_chess_board():
    """Return starting position as 8x8 list, index [row][col], row 0 = rank 8."""
    return [
        list('rnbqkbnr'),
        list('pppppppp'),
        [None]*8,
        [None]*8,
        [None]*8,
        [None]*8,
        list('PPPPPPPP'),
        list('RNBQKBNR'),
    ]

def render_chess(board, last_move=None):
    ranks = '87654321'
    files = 'abcdefgh'
    lines = []
    for r, rank in enumerate(ranks):
        row = rank + ' '
        for c, file in enumerate(files):
            piece = board[r][c]
            # Checkerboard pattern
            light = (r + c) % 2 == 0
            if piece:
                row += CHESS_EMOJI[piece]
            else:
                row += '⬜' if light else '⬛'
        lines.append(row)
    lines.append('\u3000 a b c d e f g h')
    return '\n'.join(lines)

def parse_move(move_str):
    """Parse 'e2 e4' or 'e2e4' into ((fr, fc), (tr, tc)) or None."""
    m = move_str.strip().replace(' ', '').lower()
    if len(m) != 4:
        return None
    files = 'abcdefgh'
    ranks = '87654321'
    if m[0] not in files or m[2] not in files:
        return None
    if m[1] not in '12345678' or m[3] not in '12345678':
        return None
    fc = files.index(m[0])
    fr = ranks.index(m[1])
    tc = files.index(m[2])
    tr = ranks.index(m[3])
    return (fr, fc), (tr, tc)

def is_white(piece):
    return piece and piece.isupper()

def is_black(piece):
    return piece and piece.islower()

def in_bounds(r, c):
    return 0 <= r < 8 and 0 <= c < 8

def get_moves(board, fr, fc, en_passant=None, castling_rights=None):
    """Get all pseudo-legal destination squares for piece at (fr, fc)."""
    piece = board[fr][fc]
    if not piece:
        return []
    white = piece.isupper()
    p = piece.upper()
    moves = []

    def add(r, c):
        if in_bounds(r, c):
            target = board[r][c]
            if target is None:
                moves.append((r, c))
                return True
            elif (white and is_black(target)) or (not white and is_white(target)):
                moves.append((r, c))
            return False
        return False

    def slide(dr, dc):
        r, c = fr + dr, fc + dc
        while in_bounds(r, c):
            target = board[r][c]
            if target is None:
                moves.append((r, c))
            elif (white and is_black(target)) or (not white and is_white(target)):
                moves.append((r, c))
                break
            else:
                break
            r += dr
            c += dc

    if p == 'P':
        dir = -1 if white else 1
        start_rank = 6 if white else 1
        # Forward
        if in_bounds(fr+dir, fc) and board[fr+dir][fc] is None:
            moves.append((fr+dir, fc))
            if fr == start_rank and board[fr+2*dir][fc] is None:
                moves.append((fr+2*dir, fc))
        # Captures
        for dc in [-1, 1]:
            r, c = fr+dir, fc+dc
            if in_bounds(r, c):
                target = board[r][c]
                if target and ((white and is_black(target)) or (not white and is_white(target))):
                    moves.append((r, c))
                # En passant
                if en_passant and (r, c) == en_passant:
                    moves.append((r, c))

    elif p == 'N':
        for dr, dc in [(-2,-1),(-2,1),(-1,-2),(-1,2),(1,-2),(1,2),(2,-1),(2,1)]:
            add(fr+dr, fc+dc)

    elif p == 'B':
        for dr, dc in [(-1,-1),(-1,1),(1,-1),(1,1)]:
            slide(dr, dc)

    elif p == 'R':
        for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
            slide(dr, dc)

    elif p == 'Q':
        for dr, dc in [(-1,-1),(-1,1),(1,-1),(1,1),(-1,0),(1,0),(0,-1),(0,1)]:
            slide(dr, dc)

    elif p == 'K':
        for dr, dc in [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]:
            add(fr+dr, fc+dc)
        # Castling — only when not currently in check
        if castling_rights and not is_in_check(board, white):
            if white and fr == 7 and fc == 4:
                if castling_rights.get("K") and board[7][5] is None and board[7][6] is None:
                    if not is_attacked(board, 7, 5, False) and not is_attacked(board, 7, 6, False):
                        moves.append((7, 6))
                if castling_rights.get("Q") and board[7][3] is None and board[7][2] is None and board[7][1] is None:
                    if not is_attacked(board, 7, 3, False) and not is_attacked(board, 7, 2, False):
                        moves.append((7, 2))
            elif not white and fr == 0 and fc == 4:
                if castling_rights.get("k") and board[0][5] is None and board[0][6] is None:
                    if not is_attacked(board, 0, 5, True) and not is_attacked(board, 0, 6, True):
                        moves.append((0, 6))
                if castling_rights.get("q") and board[0][3] is None and board[0][2] is None and board[0][1] is None:
                    if not is_attacked(board, 0, 3, True) and not is_attacked(board, 0, 2, True):
                        moves.append((0, 2))

    return moves

def find_king(board, white):
    target = 'K' if white else 'k'
    for r in range(8):
        for c in range(8):
            if board[r][c] == target:
                return r, c
    return None

def is_in_check(board, white, en_passant=None):
    king = find_king(board, white)
    if not king:
        return True
    for r in range(8):
        for c in range(8):
            piece = board[r][c]
            if piece and ((not white and is_white(piece)) or (white and is_black(piece))):
                if king in get_moves(board, r, c, en_passant):
                    return True
    return False

def is_attacked(board, r, c, by_white):
    """Is square (r, c) attacked by pieces of the given color? (No castling_rights to avoid recursion.)"""
    for row in range(8):
        for col in range(8):
            piece = board[row][col]
            if piece and ((by_white and is_white(piece)) or (not by_white and is_black(piece))):
                if (r, c) in get_moves(board, row, col):
                    return True
    return False

def apply_move(board, fr, fc, tr, tc, en_passant=None, castling_rights=None):
    """Apply move, return new board. Handles en passant, promotion, and castling."""
    import copy
    new_board = copy.deepcopy(board)
    piece = new_board[fr][fc]
    new_board[tr][tc] = piece
    new_board[fr][fc] = None

    # En passant capture
    if piece and piece.upper() == 'P' and en_passant and (tr, tc) == en_passant:
        cap_row = tr + (1 if piece.isupper() else -1)
        new_board[cap_row][tc] = None

    # Castling: move the rook alongside the king
    if piece == 'K' and fr == 7 and fc == 4:
        if tc == 6:   # kingside
            new_board[7][5] = new_board[7][7]
            new_board[7][7] = None
        elif tc == 2:  # queenside
            new_board[7][3] = new_board[7][0]
            new_board[7][0] = None
    elif piece == 'k' and fr == 0 and fc == 4:
        if tc == 6:   # kingside
            new_board[0][5] = new_board[0][7]
            new_board[0][7] = None
        elif tc == 2:  # queenside
            new_board[0][3] = new_board[0][0]
            new_board[0][0] = None

    # Pawn promotion (auto-queen)
    if piece == 'P' and tr == 0:
        new_board[tr][tc] = 'Q'
    if piece == 'p' and tr == 7:
        new_board[tr][tc] = 'q'

    return new_board

def is_legal_move(board, fr, fc, tr, tc, white_turn, en_passant=None, castling_rights=None):
    piece = board[fr][fc]
    if not piece:
        return False, "No piece there."
    if white_turn and is_black(piece):
        return False, "That's not your piece."
    if not white_turn and is_white(piece):
        return False, "That's not your piece."
    if (tr, tc) not in get_moves(board, fr, fc, en_passant, castling_rights):
        return False, "That move is not possible."
    # Check if move leaves own king in check
    new_board = apply_move(board, fr, fc, tr, tc, en_passant, castling_rights)
    if is_in_check(new_board, white_turn, None):
        return False, "That move leaves your king in check."
    return True, ""

def has_legal_moves(board, white_turn, en_passant=None, castling_rights=None):
    for fr in range(8):
        for fc in range(8):
            piece = board[fr][fc]
            if piece and ((white_turn and is_white(piece)) or (not white_turn and is_black(piece))):
                for tr, tc in get_moves(board, fr, fc, en_passant, castling_rights):
                    new_board = apply_move(board, fr, fc, tr, tc, en_passant, castling_rights)
                    if not is_in_check(new_board, white_turn, None):
                        return True
    return False


def _chess_mention(member, uid):
    """Safe mention text — falls back to a raw mention if the member has left the guild."""
    return member.mention if member else f"<@{uid}>"

def _chess_name(member, uid):
    """Safe display name — falls back to the raw id if the member has left the guild."""
    return member.display_name if member else str(uid)


async def run_chess(channel, players, guild_id):
    random.shuffle(players)
    white_player, black_player = players[0], players[1]
    board            = make_chess_board()
    white_turn       = True
    en_passant       = None
    castling_rights  = {"K": True, "Q": True, "k": True, "q": True}
    ch_id            = str(channel.id)

    active_games[ch_id] = {
        "game":            "chess",
        "board":           board,
        "white":           white_player.id,
        "black":           black_player.id,
        "white_turn":      white_turn,
        "en_passant":      en_passant,
        "castling_rights": castling_rights,
        "guild_id":        guild_id,
        "channel":         channel,
        "msg_id":          None,
    }

    board_str = render_chess(board)
    msg = await channel.send(
        f"**Chess**\n{white_player.mention} ♔ (White) vs {black_player.mention} ♚ (Black)\n\n"
        f"{board_str}\n\n"
        f"**{white_player.mention}'s turn (White)**\n"
        f"Type your move like `e2 e4` · Castling: `e1 g1` (kingside) or `e1 c1` (queenside) · Type `resign` to forfeit"
    )
    active_games[ch_id]["msg_id"] = msg.id
    # Poll every 30s until game ends naturally or times out (30 min)
    for _ in range(60):
        await asyncio.sleep(30)
        if ch_id not in active_games:
            return  # game ended — run_game finally handles channel deletion
    if ch_id in active_games:
        del active_games[ch_id]
        try:
            await channel.send("⏰ Chess game timed out after 30 minutes — no winner declared.")
        except Exception:
            pass

# ── DICE ROLL ─────────────────────────────────────────────────────────────────
async def run_dice(channel, players, guild_id):
    rolls   = {}
    mentions = {p.id: p.mention for p in players}
    await channel.send("Type `/roll` to roll your dice! You have **2 minutes**.")
    active_games[str(channel.id)] = {"game": "dice", "players": [p.id for p in players], "rolls": rolls, "guild_id": guild_id}

    for _ in range(120):
        await asyncio.sleep(1)
        if len(rolls) == len(players):
            break

    del active_games[str(channel.id)]
    if not rolls:
        await channel.send("No one rolled. Game cancelled.")
        return

    results   = sorted(rolls.items(), key=lambda x: x[1], reverse=True)
    lines     = [f"{mentions.get(uid, str(uid))}: **{roll}**" for uid, roll in results]
    top_score = results[0][1]
    winners   = [uid for uid, roll in results if roll == top_score]
    if len(winners) > 1:
        winner_mentions = ", ".join(mentions.get(uid, str(uid)) for uid in winners)
        result_text = f"**Dice Roll Results:**\n" + "\n".join(lines) + f"\n\n🎲 **Tie!** {winner_mentions} all rolled **{top_score}**!"
        # Tie — skip logging (no single winner)
    else:
        winner = results[0]
        result_text = f"**Dice Roll Results:**\n" + "\n".join(lines) + f"\n\n🎲 Winner: {mentions.get(winner[0], str(winner[0]))} with **{winner[1]}**!"
        loser_entry = results[-1] if len(results) > 1 else (None, None)
        loser_name_val = None
        if loser_entry[0]:
            loser_member = channel.guild.get_member(loser_entry[0])
            loser_name_val = loser_member.display_name if loser_member else str(loser_entry[0])
        winner_member = channel.guild.get_member(winner[0])
        winner_name_val = winner_member.display_name if winner_member else str(winner[0])
        log_game_result(guild_id, "dice", winner[0], winner_name_val, loser_entry[0], loser_name_val)
    await channel.send(result_text)
    await post_result(guild_id, channel.guild, result_text)


@tree.command(name="roll", description="Roll the dice (only in active dice game channel)")
async def roll(interaction: discord.Interaction):
    ch_id = str(interaction.channel_id)
    game  = active_games.get(ch_id)
    if not game or game["game"] != "dice":
        await interaction.response.send_message("No active dice game here.", ephemeral=True)
        return
    uid = interaction.user.id
    if uid not in game["players"]:
        await interaction.response.send_message("You're not in this game.", ephemeral=True)
        return
    if uid in game["rolls"]:
        await interaction.response.send_message("You already rolled!", ephemeral=True)
        return
    result = random.randint(1, 6)
    game["rolls"][uid] = result
    await interaction.response.send_message(f"You rolled a **{result}**!")


# ── TIC TAC TOE ───────────────────────────────────────────────────────────────
def render_ttt(board):
    symbols = {0: "⬜", 1: "❌", 2: "⭕"}
    rows = []
    for r in range(3):
        rows.append(" ".join(symbols[board[r*3+c]] for c in range(3)))
    return "\n".join(rows)

def check_ttt_winner(board):
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a,b,c in wins:
        if board[a] == board[b] == board[c] != 0:
            return board[a]
    if 0 not in board:
        return -1  # draw
    return 0

class TTTView(discord.ui.View):
    def __init__(self, board, current, players, channel, guild_id):
        super().__init__(timeout=300)
        self.board    = board
        self.current  = current  # index 0 or 1
        self.players  = players
        self.channel  = channel
        self.guild_id = guild_id
        self.msg      = None
        for i in range(9):
            btn = discord.ui.Button(label="\u200b", style=discord.ButtonStyle.secondary, row=i//3, custom_id=str(i))
            if board[i] == 1:
                btn.label = "❌"
                btn.disabled = True
            elif board[i] == 2:
                btn.label = "⭕"
                btn.disabled = True
            btn.callback = self.make_callback(i)
            self.add_item(btn)

    def make_callback(self, idx):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.players[self.current].id:
                await interaction.response.send_message("It's not your turn!", ephemeral=True)
                return
            if self.board[idx] != 0:
                await interaction.response.send_message("That square is taken.", ephemeral=True)
                return
            self.board[idx] = self.current + 1
            winner = check_ttt_winner(self.board)
            if winner:
                for item in self.children:
                    item.disabled = True
                if winner == -1:
                    result = "It's a draw!"
                else:
                    winner_player = self.players[winner-1]
                    loser_player  = self.players[2-winner]
                    result = f"{winner_player.mention} wins!"
                    log_game_result(self.guild_id, "ttt",
                                    winner_player.id, winner_player.display_name,
                                    loser_player.id, loser_player.display_name)
                await interaction.response.edit_message(content=f"{render_ttt(self.board)}\n\n{result}", view=self)
                await post_result(self.guild_id, interaction.guild, f"**Tic Tac Toe:** {result}")
                await asyncio.sleep(5)
                await self.channel.delete()
            else:
                self.current = 1 - self.current
                new_view = TTTView(self.board, self.current, self.players, self.channel, self.guild_id)
                new_view.msg = self.msg
                await interaction.response.edit_message(
                    content=f"{render_ttt(self.board)}\n\n{self.players[self.current].mention}'s turn",
                    view=new_view
                )
        return callback


async def run_ttt(channel, players, guild_id):
    random.shuffle(players)
    board = [0] * 9
    view  = TTTView(board, 0, players, channel, guild_id)
    msg   = await channel.send(
        f"{render_ttt(board)}\n\n{players[0].mention}'s turn (❌)",
        view=view
    )
    view.msg = msg
    await asyncio.sleep(300)


# ── CONNECT 4 ─────────────────────────────────────────────────────────────────
def render_c4(board):
    # Compact render — no spaces between cells so the grid fits on mobile
    symbols = {0: "⬜", 1: "🔴", 2: "🟡"}
    rows = []
    for r in range(6):
        rows.append("".join(symbols[board[r][c]] for c in range(7)))
    rows.append("1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣")
    return "\n".join(rows)

def check_c4_winner(board):
    for r in range(6):
        for c in range(4):
            if board[r][c] != 0 and all(board[r][c+i] == board[r][c] for i in range(4)):
                return board[r][c]
    for r in range(3):
        for c in range(7):
            if board[r][c] != 0 and all(board[r+i][c] == board[r][c] for i in range(4)):
                return board[r][c]
    for r in range(3):
        for c in range(4):
            if board[r][c] != 0 and all(board[r+i][c+i] == board[r][c] for i in range(4)):
                return board[r][c]
    for r in range(3, 6):
        for c in range(4):
            if board[r][c] != 0 and all(board[r-i][c+i] == board[r][c] for i in range(4)):
                return board[r][c]
    if all(board[0][c] != 0 for c in range(7)):
        return -1
    return 0

class C4View(discord.ui.View):
    def __init__(self, board, current, players, channel, guild_id):
        super().__init__(timeout=300)
        self.board    = board
        self.current  = current
        self.players  = players
        self.channel  = channel
        self.guild_id = guild_id
        for col in range(7):
            btn = discord.ui.Button(label=f"{col+1}", style=discord.ButtonStyle.primary, row=0, custom_id=str(col))
            btn.callback = self.make_callback(col)
            self.add_item(btn)

    def make_callback(self, col):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.players[self.current].id:
                await interaction.response.send_message("It's not your turn!", ephemeral=True)
                return
            # Drop piece
            row = None
            for r in range(5, -1, -1):
                if self.board[r][col] == 0:
                    row = r
                    break
            if row is None:
                await interaction.response.send_message("That column is full.", ephemeral=True)
                return
            self.board[row][col] = self.current + 1
            winner = check_c4_winner(self.board)
            if winner:
                for item in self.children:
                    item.disabled = True
                if winner == -1:
                    result = "It's a draw!"
                else:
                    winner_player = self.players[winner-1]
                    loser_player  = self.players[2-winner]
                    result = f"{winner_player.mention} wins!"
                    log_game_result(self.guild_id, "c4",
                                    winner_player.id, winner_player.display_name,
                                    loser_player.id, loser_player.display_name)
                await interaction.response.edit_message(content=f"{render_c4(self.board)}\n\n{result}", view=self)
                await post_result(self.guild_id, interaction.guild, f"**Connect 4:** {result}")
                await asyncio.sleep(5)
                await self.channel.delete()
            else:
                self.current = 1 - self.current
                new_view = C4View(self.board, self.current, self.players, self.channel, self.guild_id)
                await interaction.response.edit_message(
                    content=f"{render_c4(self.board)}\n\n{self.players[self.current].mention}'s turn",
                    view=new_view
                )
        return callback


async def run_c4(channel, players, guild_id):
    random.shuffle(players)
    board = [[0]*7 for _ in range(6)]
    view  = C4View(board, 0, players, channel, guild_id)
    await channel.send(f"{render_c4(board)}\n\n{players[0].mention}'s turn (🔴)", view=view)
    await asyncio.sleep(300)


# ── HIGHER OR LOWER ───────────────────────────────────────────────────────────
class HOLNumberModal(discord.ui.Modal, title="Pick a Number (1-1000)"):
    number = discord.ui.TextInput(label="Your secret number", placeholder="1-1000", max_length=4)

    def __init__(self, channel, guesser, guild_id):
        super().__init__()
        self.channel  = channel
        self.guesser  = guesser
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.number.value)
            if not (1 <= n <= 1000):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Invalid number.", ephemeral=True)
            return
        await interaction.response.send_message("Number set! The game begins.", ephemeral=True)
        active_games[str(self.channel.id)] = {
            "game": "hol",
            "secret": n,
            "guesser": self.guesser.id,
            "guesses": 0,
            "max_guesses": 8,
            "guild_id": self.guild_id,
            "channel": self.channel
        }
        await self.channel.send(f"{self.guesser.mention} — guess a number between 1 and 1000. You have **8** guesses!")


async def run_hol(channel, players, guild_id):
    random.shuffle(players)
    picker, guesser = players[0], players[1]
    active_games[str(channel.id)] = {"game": "hol_waiting"}

    class PickerView(discord.ui.View):
        @discord.ui.button(label="Pick Your Secret Number", style=discord.ButtonStyle.primary)
        async def pick(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != picker.id:
                await interaction.response.send_message("Only the picker can do this.", ephemeral=True)
                return
            await interaction.response.send_modal(HOLNumberModal(channel, guesser, guild_id))
            self.stop()

    view = PickerView()
    await channel.send(
        f"{picker.mention} — click the button below to secretly pick a number (only you can see it).\n"
        f"{guesser.mention} — wait for the picker to set the number!",
        view=view
    )
    # Poll until game finishes naturally or times out (5 min)
    for _ in range(60):
        await asyncio.sleep(5)
        if str(channel.id) not in active_games:
            return  # game ended — run_game finally handles channel deletion
    ch_id = str(channel.id)
    if ch_id in active_games:
        del active_games[ch_id]
        try:
            await channel.send("⏰ Higher or Lower timed out — game cancelled.")
        except Exception:
            pass


@client.event
async def on_message(message):
    if message.author.bot:
        return
    ch_id = str(message.channel.id)
    game  = active_games.get(ch_id)
    if not game:
        return

    if game["game"] == "hol":
        if message.author.id != game["guesser"]:
            return
        try:
            guess = int(message.content.strip())
        except ValueError:
            return
        secret  = game["secret"]
        guesses = game["guesses"] + 1
        game["guesses"] = guesses
        remaining = game["max_guesses"] - guesses
        if guess == secret:
            await message.channel.send(f"🎉 Correct! The number was **{secret}**! Well done!")
            await post_result(game["guild_id"], message.guild, f"**Higher or Lower:** {message.author.mention} guessed **{secret}** correctly in {guesses} guesses!")
            log_game_result(game["guild_id"], "hol",
                            message.author.id, message.author.display_name)
            del active_games[ch_id]
            await asyncio.sleep(5)
            await message.channel.delete()
        elif guesses >= game["max_guesses"]:
            await message.channel.send(f"Out of guesses! The number was **{secret}**.")
            del active_games[ch_id]
            await asyncio.sleep(5)
            await message.channel.delete()
        elif guess < secret:
            await message.reply(f"📈 Higher! ({remaining} guesses left)")
        else:
            await message.reply(f"📉 Lower! ({remaining} guesses left)")

    elif game["game"] == "chess":
        await handle_chess_move(message, game, ch_id)

    elif game["game"] == "hangman":
        if message.author.id not in game["guesser_order"]:
            return
        if message.author.id != game["guesser_order"][game["current_guesser"]]:
            return
        content = message.content.strip().lower()
        if len(content) != 1 or not content.isalpha():
            return
        await handle_hangman_guess(message, game, ch_id, content)


# ── CONFUSION ─────────────────────────────────────────────────────────────────
def jumble_sentence(sentence):
    words = sentence.split()
    random.shuffle(words)
    return " ".join(words)

def sentence_similarity(a, b):
    a_words = set(a.lower().split())
    b_words = set(b.lower().split())
    if not a_words and not b_words:
        return 100
    intersection = a_words & b_words
    union        = a_words | b_words
    return round(len(intersection) / len(union) * 100)

class ConfusionModal(discord.ui.Modal, title="Guess the Original Sentence"):
    guess = discord.ui.TextInput(label="Your guess", placeholder="What do you think the original was?", max_length=500)

    def __init__(self, channel, game_state, ch_id, player_idx):
        super().__init__()
        self.channel    = channel
        self.game_state = game_state
        self.ch_id      = ch_id
        self.player_idx = player_idx

    async def on_submit(self, interaction: discord.Interaction):
        self.game_state["sentences"].append(self.guess.value)
        await interaction.response.send_message("Your guess has been recorded!", ephemeral=True)
        next_idx = self.player_idx + 1
        if next_idx < len(self.game_state["players"]):
            next_player = self.channel.guild.get_member(self.game_state["players"][next_idx])
            if not next_player:
                # Player left the server — no one will ever be notified to continue,
                # so the game would otherwise hang silently until it times out.
                await self.channel.send("A player is no longer in the server — game aborted.")
                del active_games[self.ch_id]
                await asyncio.sleep(3)
                await self.channel.delete()
                return
            jumbled = jumble_sentence(self.guess.value)
            view    = ConfusionGuessView(self.channel, self.game_state, self.ch_id, next_idx, jumbled)
            try:
                await next_player.send(
                    f"**Confusion game!** Here's what the previous player passed on:\n> *{jumbled}*\n\nClick below to guess the original:",
                    view=view
                )
            except discord.Forbidden:
                await self.channel.send(f"{next_player.mention} has DMs disabled — game aborted.")
                del active_games[self.ch_id]
                await asyncio.sleep(3)
                await self.channel.delete()
                return
            await self.channel.send(f"Sentence {next_idx}/{len(self.game_state['players'])} recorded. Waiting for next player...")
        else:
            # Show results
            sentences  = self.game_state["sentences"]
            original   = sentences[0]
            final      = sentences[-1]
            similarity = sentence_similarity(original, final)
            lines = [f"**Player {i+1} ({self.channel.guild.get_member(self.game_state['players'][i]).name if self.channel.guild.get_member(self.game_state['players'][i]) else 'Unknown'}):** {s}" for i, s in enumerate(sentences)]
            result = "\n".join(lines) + f"\n\n**Similarity: {similarity}%**"
            await self.channel.send(f"**Confusion Results:**\n{result}")
            await post_result(self.game_state["guild_id"], self.channel.guild, f"**Confusion:** {similarity}% similarity\n{result}")
            del active_games[self.ch_id]
            await asyncio.sleep(10)
            await self.channel.delete()


class ConfusionGuessView(discord.ui.View):
    def __init__(self, channel, game_state, ch_id, player_idx, jumbled):
        super().__init__(timeout=120)
        self.channel    = channel
        self.game_state = game_state
        self.ch_id      = ch_id
        self.player_idx = player_idx
        self.jumbled    = jumbled

    @discord.ui.button(label="Submit My Guess", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ConfusionModal(self.channel, self.game_state, self.ch_id, self.player_idx))


class FirstSentenceModal(discord.ui.Modal, title="Enter Your Sentence"):
    sentence = discord.ui.TextInput(label="Your sentence", placeholder="Type any sentence", max_length=500)

    def __init__(self, channel, game_state, ch_id):
        super().__init__()
        self.channel    = channel
        self.game_state = game_state
        self.ch_id      = ch_id

    async def on_submit(self, interaction: discord.Interaction):
        self.game_state["sentences"].append(self.sentence.value)
        await interaction.response.send_message("Sentence submitted!", ephemeral=True)
        jumbled     = jumble_sentence(self.sentence.value)
        next_player = self.channel.guild.get_member(self.game_state["players"][1])
        if not next_player:
            # Player left the server — no one will ever be notified to continue,
            # so the game would otherwise hang silently until it times out.
            await self.channel.send("A player is no longer in the server — game aborted.")
            del active_games[self.ch_id]
            await asyncio.sleep(3)
            await self.channel.delete()
            return
        view = ConfusionGuessView(self.channel, self.game_state, self.ch_id, 1, jumbled)
        try:
            await next_player.send(
                f"**Confusion game!** Here's a jumbled sentence:\n> *{jumbled}*\n\nClick below to guess the original:",
                view=view
            )
        except discord.Forbidden:
            await self.channel.send(f"{next_player.mention} has DMs disabled — game aborted.")
            del active_games[self.ch_id]
            await asyncio.sleep(3)
            await self.channel.delete()
            return
        await self.channel.send("First sentence submitted. Passing it along...")


class FirstSentenceView(discord.ui.View):
    def __init__(self, channel, game_state, ch_id, first_player):
        super().__init__(timeout=120)
        self.channel      = channel
        self.game_state   = game_state
        self.ch_id        = ch_id
        self.first_player = first_player

    @discord.ui.button(label="Enter Your Sentence", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.first_player.id:
            await interaction.response.send_message("Only the first player enters the sentence.", ephemeral=True)
            return
        await interaction.response.send_modal(FirstSentenceModal(self.channel, self.game_state, self.ch_id))


async def run_confusion(channel, players, guild_id):
    random.shuffle(players)
    ch_id = str(channel.id)
    game_state = {
        "game": "confusion",
        "players": [p.id for p in players],
        "sentences": [],
        "guild_id": guild_id
    }
    active_games[ch_id] = game_state
    first_player = players[0]
    view = FirstSentenceView(channel, game_state, ch_id, first_player)
    await channel.send(
        f"**Confusion!** Players: {', '.join(p.mention for p in players)}\n\n"
        f"{first_player.mention} — click below to enter your sentence!",
        view=view
    )
    # Poll until game finishes naturally or times out (10 min)
    for _ in range(120):
        await asyncio.sleep(5)
        if str(channel.id) not in active_games:
            return  # game ended — run_game finally handles channel deletion
    ch_id = str(channel.id)
    if ch_id in active_games:
        del active_games[ch_id]
        try:
            await channel.send("⏰ Confusion game timed out — game cancelled.")
        except Exception:
            pass


# ── HANGMAN ───────────────────────────────────────────────────────────────────
HANGMAN_STAGES = [
    "```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========```",
]

class WordModal(discord.ui.Modal, title="Enter Your Word"):
    word = discord.ui.TextInput(label="Secret word", placeholder="4-8 letters", max_length=50)

    def __init__(self, channel, players, guild_id):
        super().__init__()
        self.channel  = channel
        self.players  = players
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        word     = self.word.value.strip().lower()
        min_l, max_l = get_hangman_word_limits(self.guild_id)
        if not word.isalpha() or not (min_l <= len(word) <= max_l):
            await interaction.response.send_message(f"Word must be {min_l}-{max_l} letters and only letters.", ephemeral=True)
            return

        await interaction.response.send_message("Word set! Game starting.", ephemeral=True)
        ch_id = str(self.channel.id)
        guessers = [p for p in self.players if p.id != interaction.user.id]
        random.shuffle(guessers)

        game_state = {
            "game": "hangman",
            "word": word,
            "guessed": [],
            "wrong": 0,
            "guesser_order": [g.id for g in guessers],
            "current_guesser": 0,
            "guild_id": self.guild_id,
            "channel": self.channel
        }
        active_games[ch_id] = game_state

        display = " ".join("\\_" if c not in game_state["guessed"] else c for c in word)
        msg = await self.channel.send(
            f"{HANGMAN_STAGES[0]}\n**Word:** {display}\n**Wrong guesses:** 0/6\n\n"
            f"{guessers[0].mention}'s turn — type a letter!"
        )
        game_state["msg_id"] = msg.id



async def handle_chess_move(message, game, ch_id):
    content = message.content.strip().lower()

    # Support resign
    if content in ("resign", "ff", "forfeit"):
        # Resign must apply to whoever actually typed it, not whoever's turn it
        # currently is — otherwise a player could force their opponent to lose
        # by typing "resign" while it's the opponent's move.
        if message.author.id == game["white"]:
            loser_id, winner_id = game["white"], game["black"]
        elif message.author.id == game["black"]:
            loser_id, winner_id = game["black"], game["white"]
        else:
            return  # not a player in this game
        white_player = message.guild.get_member(game["white"])
        black_player = message.guild.get_member(game["black"])
        loser  = message.guild.get_member(loser_id)
        winner = message.guild.get_member(winner_id)
        result = f"**{_chess_mention(loser, loser_id)} resigned.** {_chess_mention(winner, winner_id)} wins!"
        board_str = render_chess(game["board"])
        content_msg = (
            f"**Chess**\n{_chess_mention(white_player, game['white'])} ♔ vs {_chess_mention(black_player, game['black'])} ♚\n\n"
            f"{board_str}\n\n{result}"
        )
        await message.channel.send(content_msg)
        await post_result(game["guild_id"], message.guild, f"**Chess:** {result}")
        log_game_result(game["guild_id"], "chess",
                        winner_id, _chess_name(winner, winner_id),
                        loser_id, _chess_name(loser, loser_id))
        del active_games[ch_id]
        await asyncio.sleep(5)
        await message.channel.delete()
        return

    parsed = parse_move(content)
    if not parsed:
        await message.reply("Invalid format. Type like `e2 e4` or type `resign`.", delete_after=5)
        return

    white_turn   = game["white_turn"]
    current_id   = game["white"] if white_turn else game["black"]
    if message.author.id != current_id:
        await message.reply("It's not your turn.", delete_after=5)
        return

    board           = game["board"]
    en_passant      = game.get("en_passant")
    castling_rights = game.get("castling_rights", {"K": True, "Q": True, "k": True, "q": True})
    (fr, fc), (tr, tc) = parsed

    legal, reason = is_legal_move(board, fr, fc, tr, tc, white_turn, en_passant, castling_rights)
    if not legal:
        await message.reply(f"Not possible: {reason}", delete_after=5)
        return

    # Track en passant
    piece = board[fr][fc]
    new_en_passant = None
    if piece and piece.upper() == 'P' and abs(tr - fr) == 2:
        new_en_passant = ((fr + tr) // 2, fc)

    # Update castling rights
    new_cr = dict(castling_rights)
    if piece == 'K':
        new_cr["K"] = False; new_cr["Q"] = False
    elif piece == 'k':
        new_cr["k"] = False; new_cr["q"] = False
    elif piece == 'R':
        if fr == 7 and fc == 7: new_cr["K"] = False
        if fr == 7 and fc == 0: new_cr["Q"] = False
    elif piece == 'r':
        if fr == 0 and fc == 7: new_cr["k"] = False
        if fr == 0 and fc == 0: new_cr["q"] = False
    # Rook captured on its starting square
    if (tr, tc) == (7, 7): new_cr["K"] = False
    if (tr, tc) == (7, 0): new_cr["Q"] = False
    if (tr, tc) == (0, 7): new_cr["k"] = False
    if (tr, tc) == (0, 0): new_cr["q"] = False

    new_board  = apply_move(board, fr, fc, tr, tc, en_passant, castling_rights)
    next_white = not white_turn

    # Check for checkmate or stalemate
    in_check   = is_in_check(new_board, next_white)
    has_moves  = has_legal_moves(new_board, next_white, new_en_passant, new_cr)

    game["board"]            = new_board
    game["white_turn"]       = next_white
    game["en_passant"]       = new_en_passant
    game["castling_rights"]  = new_cr

    white_player = message.guild.get_member(game["white"])
    black_player = message.guild.get_member(game["black"])
    next_player  = white_player if next_white else black_player
    next_id      = game["white"] if next_white else game["black"]
    board_str    = render_chess(new_board)

    if not has_moves:
        if in_check:
            chess_winner_id = game["black"] if next_white else game["white"]
            chess_loser_id  = game["white"] if next_white else game["black"]
            chess_winner = black_player if next_white else white_player
            chess_loser  = white_player if next_white else black_player
            result = f"**Checkmate!** {_chess_mention(chess_winner, chess_winner_id)} wins!"
            log_game_result(game["guild_id"], "chess",
                            chess_winner_id, _chess_name(chess_winner, chess_winner_id),
                            chess_loser_id, _chess_name(chess_loser, chess_loser_id))
        else:
            result = "**Stalemate!** It's a draw."
        content = (
            f"**Chess**\n{_chess_mention(white_player, game['white'])} ♔ vs {_chess_mention(black_player, game['black'])} ♚\n\n"
            f"{board_str}\n\n{result}"
        )
        try:
            old_msg = await message.channel.fetch_message(game["msg_id"])
            await old_msg.edit(content=content)
        except Exception:
            await message.channel.send(content)
        await post_result(game["guild_id"], message.guild, f"**Chess:** {result}")
        del active_games[ch_id]
        await asyncio.sleep(5)
        await message.channel.delete()
    else:
        check_str = " *(check!)*" if in_check else ""
        content = (
            f"**Chess**\n{_chess_mention(white_player, game['white'])} ♔ vs {_chess_mention(black_player, game['black'])} ♚\n\n"
            f"{board_str}\n\n"
            f"**{_chess_mention(next_player, next_id)}'s turn ({'White' if next_white else 'Black'})**{check_str}\n"
            f"Type your move like `e2 e4` · Castling: `e1 g1`/`e1 c1` · Type `resign` to forfeit"
        )
        try:
            old_msg = await message.channel.fetch_message(game["msg_id"])
            await old_msg.edit(content=content)
        except Exception:
            msg = await message.channel.send(content)
            game["msg_id"] = msg.id
        try:
            await message.delete()
        except Exception:
            pass


async def handle_hangman_guess(message, game, ch_id, letter):
    word    = game["word"]
    guessed = game["guessed"]
    if letter in guessed:
        await message.reply("Already guessed that letter!", delete_after=3)
        return

    guessed.append(letter)
    if letter not in word:
        game["wrong"] += 1

    display   = " ".join(c if c in guessed else "\\_" for c in word)
    wrong     = game["wrong"]
    stage     = HANGMAN_STAGES[min(wrong, 6)]
    guessers  = game["guesser_order"]
    # Advance to next guesser, skipping anyone who has left the server
    next_idx = (game["current_guesser"] + 1) % len(guessers)
    for _ in range(len(guessers)):
        if message.guild.get_member(guessers[next_idx]):
            break
        next_idx = (next_idx + 1) % len(guessers)
    game["current_guesser"] = next_idx
    next_player = message.guild.get_member(guessers[next_idx])

    if all(c in guessed for c in word):
        content = f"{stage}\n**Word:** {display}\n\n🎉 The word was **{word}**! {message.author.mention} got the last letter!"
        await message.channel.send(content)
        await post_result(game["guild_id"], message.guild, f"**Hangman:** Word was **{word}** — {message.author.mention} completed it!")
        log_game_result(game["guild_id"], "hangman",
                        message.author.id, message.author.display_name)
        del active_games[ch_id]
        await asyncio.sleep(5)
        await message.channel.delete()
    elif wrong >= 6:
        content = f"{stage}\n**Word:** {display}\n\n💀 Game over! The word was **{word}**."
        await message.channel.send(content)
        await post_result(game["guild_id"], message.guild, f"**Hangman:** Nobody guessed **{word}**.")
        del active_games[ch_id]
        await asyncio.sleep(5)
        await message.channel.delete()
    else:
        content = (
            f"{stage}\n**Word:** {display}\n"
            f"**Guessed:** {', '.join(guessed)}\n"
            f"**Wrong:** {wrong}/6\n\n"
            f"{next_player.mention if next_player else 'Next player'}'s turn — type a letter!"
        )
        await message.channel.send(content)


class HangmanWordView(discord.ui.View):
    def __init__(self, channel, players, guild_id, word_player):
        super().__init__(timeout=120)
        self.channel     = channel
        self.players     = players
        self.guild_id    = guild_id
        self.word_player = word_player

    @discord.ui.button(label="Enter Word", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.word_player.id:
            await interaction.response.send_message("Only the word picker enters the word.", ephemeral=True)
            return
        await interaction.response.send_modal(WordModal(self.channel, self.players, self.guild_id))


async def run_hangman(channel, players, guild_id):
    active_games[str(channel.id)] = {"game": "hangman_waiting"}
    word_player = random.choice(players)
    view = HangmanWordView(channel, players, guild_id, word_player)
    await channel.send(
        f"**Hangman!** {word_player.mention} has been chosen to pick the word!\n"
        f"Click below to enter it secretly:",
        view=view
    )
    # Poll until game finishes naturally or times out (10 min)
    for _ in range(120):
        await asyncio.sleep(5)
        if str(channel.id) not in active_games:
            return  # game ended — run_game finally handles channel deletion
    ch_id = str(channel.id)
    if ch_id in active_games:
        del active_games[ch_id]
        try:
            await channel.send("⏰ Hangman timed out — game cancelled.")
        except Exception:
            pass


@tree.command(name="leaderboard", description="Show the top economy balances for this server")
async def leaderboard(interaction: discord.Interaction):
    guild_id  = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return
    canonical = get_canonical_guild(guild_id)
    eco = load_json(ECONOMY_FILE).get(canonical, {})
    if not eco:
        await interaction.response.send_message("No economy data yet!", ephemeral=True)
        return
    sorted_players = sorted(eco.items(), key=lambda x: x[1].get("balance", 0), reverse=True)[:10]
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, data) in enumerate(sorted_players):
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        name   = data.get("name", f"User {uid}")
        bal    = data.get("balance", 0)
        lines.append(f"{prefix} {name} — **${bal}**")
    await interaction.response.send_message("**Economy Leaderboard:**\n" + "\n".join(lines))


@tree.command(name="wins", description="Show your win/loss record across all games")
async def wins(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return
    uid = str(interaction.user.id)
    if not os.path.exists(GAME_LOG_FILE):
        await interaction.response.send_message("No game history yet.", ephemeral=True)
        return
    with open(GAME_LOG_FILE, "r") as f:
        try: log_data = json.load(f)
        except Exception: log_data = {}
    entries        = log_data.get(guild_id, [])
    wins_by_game   = {}
    losses_by_game = {}
    for e in entries:
        game = e.get("game", "?")
        if e.get("winner_id") == uid:
            wins_by_game[game] = wins_by_game.get(game, 0) + 1
        if e.get("loser_id") == uid:
            losses_by_game[game] = losses_by_game.get(game, 0) + 1
    all_games = set(list(wins_by_game) + list(losses_by_game))
    if not all_games:
        await interaction.response.send_message("No game history for you yet.", ephemeral=True)
        return
    lines = []
    for g in sorted(all_games):
        w = wins_by_game.get(g, 0)
        l = losses_by_game.get(g, 0)
        lines.append(f"**{g}**: {w}W / {l}L")
    total_w = sum(wins_by_game.values())
    total_l = sum(losses_by_game.values())
    await interaction.response.send_message(
        f"**Win/Loss Record for {interaction.user.display_name}:**\n"
        + "\n".join(lines)
        + f"\n\n**Total:** {total_w}W / {total_l}L",
        ephemeral=True
    )


@tree.command(name="waitlist", description="Show the current waitlist for a game")
@app_commands.describe(game="Game name: dice, ttt, c4, chess, hol, confusion, hangman")
async def waitlist_cmd(interaction: discord.Interaction, game: str):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return
    game = game.lower()
    if game not in GAME_INFO:
        await interaction.response.send_message(
            f"Unknown game. Options: {', '.join(GAME_INFO.keys())}", ephemeral=True
        )
        return
    data = get_guild_data(guild_id)
    wl   = data.get("waitlists", {}).get(game, [])
    info = GAME_INFO[game]
    min_p = info.get("min", 2)
    if not wl:
        await interaction.response.send_message(
            f"**{info.get('name', game)}** waitlist is empty. Use `/game {game}` to join!", ephemeral=True
        )
        return
    names = ", ".join(p.get("name", "?") for p in wl)
    await interaction.response.send_message(
        f"**{info.get('name', game)}** waitlist ({len(wl)}/{min_p} minimum): {names}", ephemeral=True
    )


@tree.command(name="cancelwait", description="Remove yourself from a game waitlist")
@app_commands.describe(game="Game name: dice, ttt, c4, chess, hol, confusion, hangman")
async def cancelwait(interaction: discord.Interaction, game: str):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return
    game = game.lower()
    if game not in GAME_INFO:
        await interaction.response.send_message(
            f"Unknown game. Options: {', '.join(GAME_INFO.keys())}", ephemeral=True
        )
        return
    # Load games_data fresh
    all_data = load_json(GAMES_FILE)
    gdata    = all_data.get(guild_id, {})
    wl       = gdata.get("waitlists", {}).get(game, [])
    before   = len(wl)
    new_wl   = [p for p in wl if p.get("id") != interaction.user.id]
    if len(new_wl) == before:
        await interaction.response.send_message(
            f"You're not in the **{GAME_INFO[game].get('name', game)}** waitlist.", ephemeral=True
        )
        return
    if "waitlists" not in gdata:
        gdata["waitlists"] = {}
    gdata["waitlists"][game] = new_wl
    all_data[guild_id] = gdata
    save_json(GAMES_FILE, all_data)
    if guild_id in games_data and "waitlists" in games_data[guild_id]:
        games_data[guild_id]["waitlists"][game] = new_wl
    await interaction.response.send_message(
        f"Removed you from the **{GAME_INFO[game].get('name', game)}** waitlist.", ephemeral=True
    )


@tree.command(name="gamestats", description="Show server-wide game statistics")
async def gamestats(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return
    if not os.path.exists(GAME_LOG_FILE):
        await interaction.response.send_message("No game history yet.", ephemeral=True)
        return
    with open(GAME_LOG_FILE, "r") as f:
        try: log_data = json.load(f)
        except Exception: log_data = {}
    entries = log_data.get(guild_id, [])
    if not entries:
        await interaction.response.send_message("No game history yet.", ephemeral=True)
        return
    by_type  = {}
    wins_cnt = {}
    for e in entries:
        g = e.get("game", "?")
        by_type[g] = by_type.get(g, 0) + 1
        wn = e.get("winner_name")
        if wn:
            wins_cnt[wn] = wins_cnt.get(wn, 0) + 1
    most_popular = max(by_type, key=by_type.get)
    top_winners  = sorted(wins_cnt.items(), key=lambda x: x[1], reverse=True)[:3]
    total        = sum(by_type.values())
    lines = [f"**Total games played:** {total}", f"**Most popular:** {most_popular} ({by_type[most_popular]} games)"]
    if top_winners:
        lines.append("**Top winners:**")
        for i, (name, w) in enumerate(top_winners, 1):
            lines.append(f"  {i}. {name} — {w} wins")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="rematch", description="Challenge a specific user to a 1v1 game directly")
@app_commands.describe(
    user="Who to challenge",
    game="Game: ttt, c4, chess, hol, hangman"
)
async def rematch(interaction: discord.Interaction, user: discord.Member, game: str):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Games bot is disabled.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't challenge a bot.", ephemeral=True)
        return
    game = game.lower()
    valid_1v1 = {"ttt", "c4", "chess", "hol", "hangman"}
    if game not in valid_1v1:
        await interaction.response.send_message(
            f"Rematches are only available for 1v1 games: {', '.join(sorted(valid_1v1))}",
            ephemeral=True
        )
        return
    data = get_guild_data(guild_id)
    if not data.get("categories", {}).get(game):
        await interaction.response.send_message(
            "Games not set up for this server yet. Use `/setup`.", ephemeral=True
        )
        return
    # Check balances for both players (mods exempt)
    game_cost = get_game_cost(guild_id)
    canonical = get_canonical_guild(guild_id)
    for p_member in [interaction.user, user]:
        if not is_mod(p_member):
            bal = get_balance(canonical, str(p_member.id))
            if bal < game_cost:
                await interaction.response.send_message(
                    f"{p_member.mention} doesn't have enough coins (need ${game_cost}).", ephemeral=True
                )
                return
    players = [
        {"id": interaction.user.id, "name": interaction.user.display_name},
        {"id": user.id, "name": user.display_name},
    ]
    data = get_guild_data(guild_id)
    await interaction.response.send_message(
        f"⚔️ {user.mention} vs {interaction.user.mention} — **{game.upper()}** starting now!",
        ephemeral=False
    )
    await launch_game(game, interaction.guild, guild_id, players, data, canonical, game_cost, interaction.channel)


@client.event
async def on_ready():
    await tree.sync()
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"Games Bot logged in as {client.user}")


client.run(TOKEN)